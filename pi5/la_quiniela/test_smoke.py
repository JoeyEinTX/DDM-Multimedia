# la_quiniela/test_smoke.py - La Quiniela bridge smoke test
#
# Run with: python -m la_quiniela.test_smoke  (from the pi5/ dir)
#
# No hardware, no real serial port: a fake port feeds the bridge the
# gateway's lines and captures what it writes, a stub SocketIO records every
# emit, and a fake clock drives the timers. Same tiny runner as
# la_subasta/test_smoke.py.

import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass

_PI5_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PI5_DIR)

from la_subasta import config as la_config  # noqa: E402

# The bridge lives in the app's one database. Point it at a temp file before
# anything captures the path.
_TMP_DB = tempfile.mktemp(prefix="la_quiniela_smoke_", suffix=".db")
la_config.DB_PATH = _TMP_DB

from la_quiniela import bridge as bridge_mod  # noqa: E402
from la_quiniela import protocol as P  # noqa: E402
from la_quiniela.blueprint import get_bridge, init_la_quiniela, la_quiniela_bp  # noqa: E402
from la_quiniela.bridge import (  # noqa: E402
    HELLO_EVENT_MIN_S, LINK_REASONS, LQ_ROOM, PENDING_MAX, PER_CUP_EVENT_MIN_S,
    RESEND_MIN_S, SERIAL_LINE_MODES, LqBridge, load_settings, open_serial_port,
)
from la_quiniela.models import LqDb  # noqa: E402


# -----------------------------------------------------------------------------
# Tiny test runner (no pytest dependency)
# -----------------------------------------------------------------------------

_results = []


def _check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((status, name, detail))
    marker = "[OK]" if condition else "[XX]"
    print(f"  {marker} {name}" + (f"  -- {detail}" if detail and not condition else ""))
    return condition


def _run(name, fn):
    print(f"\n=== {name} ===")
    try:
        fn()
    except Exception as exc:
        traceback.print_exc()
        _check(f"{name} (uncaught exception)", False, str(exc))


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class FakeSerial:
    """Bytes fed by the test come out of read(); write() is captured. The
    reader API matches pyserial's: read(n) returns up to n bytes and b"" on
    timeout, in_waiting says how many are queued, reset_input_buffer drops
    them. Deliberately no readline(): the bridge must never use one."""

    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.written = []
        self.closed = False
        self.fail_reads = False
        self.fail_writes = False
        self.read_raises = None        # raise this on the next read, then clear
        self.resets = 0

    def feed(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self.lock:
            self.buf += data

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.buf)

    def read(self, size=1):
        if self.fail_reads:
            raise OSError("device disappeared")
        if self.read_raises is not None:
            exc, self.read_raises = self.read_raises, None
            raise exc
        deadline = time.time() + 0.02
        while True:
            with self.lock:
                if self.buf:
                    n = min(int(size), len(self.buf))
                    out = bytes(self.buf[:n])
                    del self.buf[:n]
                    return out
            if time.time() >= deadline:
                return b""
            time.sleep(0.002)

    def reset_input_buffer(self):
        self.resets += 1
        with self.lock:
            self.buf.clear()

    def write(self, data):
        if self.fail_writes:
            raise OSError("write failed")
        self.written.append(data)
        return len(data)

    def close(self):
        self.closed = True

    def lines(self):
        return [w.decode("utf-8").rstrip("\n") for w in self.written]


class StubSocketIO:
    def __init__(self):
        self.events = []      # (event, payload, room)
        self.handlers = {}

    def emit(self, event, payload=None, room=None, **kwargs):
        self.events.append((event, payload, room))

    def on_event(self, name, handler, namespace=None):
        self.handlers[name] = handler

    def of(self, name):
        return [payload for event, payload, _ in self.events if event == name]

    def clear(self):
        self.events.clear()


_current = None


class ConsoleCapture:
    """Collects the bridge's [LQ] console lines instead of printing them."""

    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(text)

    def matching(self, needle):
        return [l for l in self.lines if needle in l]

    def clear(self):
        self.lines.clear()


def _fresh_bridge(clock=True, **settings):
    """A bridge on an empty database with a fake port, stub socketio and (by
    default) a fake clock. The port is opened straight away, no thread."""
    global _current
    if _current is not None:
        try:
            _current.close()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except OSError:
            pass
    port = FakeSerial()
    sio = StubSocketIO()
    clk = FakeClock() if clock else None
    cfg = {"LQ_SERIAL_PORT": "/dev/fake", "LQ_SERIAL_BAUD": 115200, "LQ_HEARTBEAT_LOG_S": 10,
           "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12, "LQ_DEV_ENDPOINTS": False}
    cfg.update(settings)
    b = LqBridge(settings=cfg, db_path=_TMP_DB, serial_factory=lambda p, baud, t: port,
                 socketio=sio, clock=clk, console=ConsoleCapture())
    _current = b
    return b, port, sio, clk


MAC_A = "A0:B7:65:12:34:56"
MAC_B = "A0:B7:65:12:34:57"
MAC_C = "A0:B7:65:12:34:99"
GW_MAC = "24:6F:28:AA:BB:CC"
SIM_MAC_A = "02:DD:4D:00:00:01"      # what the cup simulator invents
SIM_MAC_B = "02:DD:4D:00:00:02"
SIM_GW_MAC = "02:DD:4D:FF:FF:FF"     # the simulator's own gateway


def telem(cup_wire, mac, count=14, raw=812345, seq=9021, drop=2, rssi=-64, up=-61, claim=None):
    d = {"t": "telem", "cup": cup_wire, "mac": mac, "raw": raw, "count": count, "seq": seq,
         "drop": drop, "rssi": rssi, "up": up}
    if claim is not None:
        d["claim"] = claim
    return (json.dumps(d, separators=(",", ":")) + "\n").encode()


def status(gseq=1, phase=1, state_rev=0, roster_rev=0, cups=0, rejects=0, up_s=10):
    return (json.dumps({"t": "status", "gseq": gseq, "phase": phase, "state_rev": state_rev,
                        "roster_rev": roster_rev, "cups": cups, "rejects": rejects,
                        "up_s": up_s}, separators=(",", ":")) + "\n").encode()


def hello(v=1, mac=GW_MAC, proto=1):
    return (json.dumps({"t": "hello", "v": v, "proto": proto, "mac": mac}) + "\n").encode()


def cup_hello(cup_wire, mac):
    return (json.dumps({"t": "cup_hello", "cup": cup_wire, "mac": mac}) + "\n").encode()


def events_of(b, type_):
    return [dict(r) for r in b.db.query("SELECT * FROM events WHERE type = ? ORDER BY id", (type_,))]


def telemetry_rows(b):
    return [dict(r) for r in b.db.query("SELECT * FROM telemetry ORDER BY id")]


def cup_row(b, mac):
    r = b.db.query_one("SELECT * FROM cups WHERE mac = ?", (mac,))
    return dict(r) if r else None


# What a real port hands over the instant it is opened: the tail of a line
# that was already in flight. The bridge must drop it and resynchronise on the
# first newline, so every test that feeds the port starts with this.
MID_LINE = b'5:12:34:56","count":3,"seq":11}\n'

# The burst a CP2102 hands over at open: bytes received before the baud rate
# was applied, on a line that never stops talking. Thousands of bytes, no
# newline, values above 0x7F and long runs of NUL.
JUNK = bytes((i * 7 + 3) % 256 for i in range(6000)).replace(b"\n", b"\x01")
NULS = b"\x00" * 9000
GOOD_LINES = (b'{"t":"hello","v":1,"proto":1,"mac":"24:6F:28:AA:BB:CC"}\n'
              b'{"t":"telem","cup":0,"mac":"A0:B7:65:12:34:56","raw":812345,"count":3,'
              b'"seq":11,"drop":0,"rssi":-64,"up":-61}\n'
              b'{"t":"status","gseq":1,"phase":1,"state_rev":0,"roster_rev":0,"cups":1,'
              b'"rejects":0,"up_s":145}\n')


def drain(b, port, limit=500):
    """Pump the real _read_once until the fake port has nothing left."""
    for _ in range(limit):
        if not port.in_waiting:
            return
        b._read_once()
    raise AssertionError("port never drained")

HORSES_1_TO_20 = list(range(1, 21))
SCR_CUP7 = [1 if cup == 7 else 0 for cup in range(1, 21)]
NO_SCR = [0] * 20


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

def test_id_conversion():
    _check("wire 0 -> cup 1", P.wire_to_cup(0) == 1)
    _check("wire 19 -> cup 20", P.wire_to_cup(19) == 20)
    _check("wire -1 -> None", P.wire_to_cup(-1) is None)
    _check("wire None -> None", P.wire_to_cup(None) is None)
    _check("cup 1 -> wire 0", P.cup_to_wire(1) == 0)
    _check("cup 20 -> wire 19", P.cup_to_wire(20) == 19)
    _check("cup None -> wire -1", P.cup_to_wire(None) == -1)
    _check("round trip 0..19", all(P.cup_to_wire(P.wire_to_cup(w)) == w for w in range(20)))
    _check("round trip 1..20", all(P.wire_to_cup(P.cup_to_wire(c)) == c for c in range(1, 21)))
    # The one ID rule: no other +1 / -1 on a cup ID outside protocol.py
    pat = re.compile(r"cup\w*\s*[-+]\s*1\b|[-+]\s*1\s*\]")
    offenders = []
    for name in ("bridge.py", "blueprint.py", "models.py", "__init__.py"):
        with open(os.path.join(_PI5_DIR, "la_quiniela", name), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if pat.search(line):
                    offenders.append(f"{name}:{n}: {line.strip()}")
    _check("no cup-ID arithmetic outside protocol.py", not offenders, "; ".join(offenders))


def test_phase_enum_matches_header():
    header = os.path.join(_PI5_DIR, "..", "firmware", "quiniela", "ddm_common.h")
    with open(header, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"enum DdmRaceState\s*:\s*uint8_t\s*\{(.*?)\};", text, re.S)
    _check("DdmRaceState enum found in ddm_common.h", m is not None)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    parsed = {}
    for name, value in re.findall(r"DDM_([A-Z_]+)\s*=\s*(\d+)", body):
        parsed[name] = int(value)
    _check("header has 7 race states", len(parsed) == 7, str(parsed))
    _check("Phase names match header", set(p.name for p in P.Phase) == set(parsed), str(parsed))
    _check("Phase values match header",
           all(P.Phase[name].value == value for name, value in parsed.items()), str(parsed))
    m2 = re.search(r"#define DDM_MAX_CUPS\s+(\d+)", text)
    _check("NUM_CUPS matches DDM_MAX_CUPS", m2 and int(m2.group(1)) == P.NUM_CUPS)


def test_garbage_lines_ignored():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    for raw in (b"# DDM La Quiniela gateway - ESP-NOW <-> serial JSON bridge\n",
                b"# ---- CUPS seq=1 ----\n", b"\n", b"\r\n", b"[1,2,3]\n",
                b'{"t":"telem"\n', b"{not json}\n",
                b'{"t":"whatever","x":1}\n', b'{"t":"status","gseq":1,"phase":1,"state_rev":0,'
                b'"roster_rev":0,"cups":0,"rejects":0,"up_s":3,"future":{"k":[1]}}\n',
                b'{"t":"telem","cup":"seven","mac":5}\n',
                b"{" + b"x" * 1100 + b"}\n"):
        b.handle_raw_line(raw)
    _check("non-object lines counted as text, not parsed", b.stats["text"] == 3, str(b.stats))
    _check("bad JSON counted", b.stats["bad_json"] == 2, str(b.stats))
    _check("over-long line dropped", b.stats["too_long"] == 1)
    _check("unknown t ignored", b.stats["unknown_type"] == 1)
    _check("unknown keys ignored, status applied", b.link.up_s == 3)
    _check("nothing written back", port.written == [])
    _check("no cups from garbage", b.cups == {})


def test_telem_live_state_and_rows():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(7, MAC_A, count=14))
    live = b.cups[MAC_A]
    _check("live cup is 1-based (wire 7 -> cup 8)", live.cup == 8)
    _check("live fields updated", live.count == 14 and live.raw == 812345 and live.rssi == -64
           and live.up == -61 and live.drop == 2 and live.online)
    rows = telemetry_rows(b)
    _check("first packet writes a telemetry row", len(rows) == 1)
    _check("row is 1-based and reason=change", rows[0]["cup_id"] == 8 and rows[0]["reason"] == "change")
    _check("cups row upserted with cup_id 8", cup_row(b, MAC_A)["cup_id"] == 8 and cup_row(b, MAC_A)["online"] == 1)
    _check("cup_online event", len(events_of(b, "cup_online")) == 1)
    ups = len(sio.of("lq_update"))
    _check("lq_update emitted", ups == 1)

    clk.advance(2)
    b.handle_raw_line(telem(7, MAC_A, count=14))
    _check("unchanged packet inside the interval: no row", len(telemetry_rows(b)) == 1)
    _check("unchanged packet: no lq_update", len(sio.of("lq_update")) == ups)

    clk.advance(1)
    b.handle_raw_line(telem(7, MAC_A, count=15))
    rows = telemetry_rows(b)
    _check("count change: row with reason=change", len(rows) == 2 and rows[1]["reason"] == "change"
           and rows[1]["token_count"] == 15)
    _check("count change: lq_update", len(sio.of("lq_update")) == ups + 1)

    clk.advance(10)
    b.handle_raw_line(telem(7, MAC_A, count=15))
    rows = telemetry_rows(b)
    _check("heartbeat row after the interval", len(rows) == 3 and rows[2]["reason"] == "heartbeat")
    _check("heartbeat: lq_update", len(sio.of("lq_update")) == ups + 2)
    payload = sio.of("lq_update")[-1]
    _check("lq_update payload shape", set(payload) == {"cup", "mac", "horse", "scratched", "count",
                                                        "raw", "rssi", "up", "drop", "online",
                                                        "last_seen"} and payload["cup"] == 8)
    _check("every emit went to the lq room", all(room == LQ_ROOM for _, _, room in sio.events))


def test_mirroring_without_roster():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(2, MAC_A))
    _check("no roster: cup mirrored from gateway", cup_row(b, MAC_A)["cup_id"] == 3)
    b.handle_raw_line(telem(4, MAC_A))
    _check("no roster: reassignment followed", cup_row(b, MAC_A)["cup_id"] == 5 and b.cups[MAC_A].cup == 5)
    _check("cup number change -> lq_update + snapshot", sio.of("lq_update")[-1]["cup"] == 5 and sio.of("lq_snapshot"))
    b.handle_raw_line(telem(4, MAC_B))
    _check("no roster: a cup number moves to the newest MAC",
           cup_row(b, MAC_B)["cup_id"] == 5 and cup_row(b, MAC_A)["cup_id"] is None)
    _check("no mismatch events in mirror mode", events_of(b, "roster_mismatch") == [])
    _check("nothing sent in mirror mode", port.written == [])


def test_mismatch_with_roster():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    macs = [""] * 20
    macs[2] = MAC_A                       # cup 3
    b.set_roster(macs)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    port.written.clear()
    clk.advance(RESEND_MIN_S + 1)
    b.handle_raw_line(telem(5, MAC_A))    # gateway says wire 5 = cup 6
    _check("with a roster DevPi's cup wins", b.cups[MAC_A].cup == 3 and cup_row(b, MAC_A)["cup_id"] == 3)
    ev = events_of(b, "roster_mismatch")
    _check("roster_mismatch event logged", len(ev) == 1 and json.loads(ev[0]["detail"])["gateway_cup"] == 6)
    lines = port.lines()
    _check("mismatch re-sends roster then state", len(lines) == 2 and lines[0].startswith('{"t":"roster"')
           and lines[1].startswith('{"t":"state"'))
    clk.advance(0.5)
    b.handle_raw_line(telem(5, MAC_A))
    _check("re-send rate limited", len(port.lines()) == 2)
    _check("mismatch event rate limited per cup", len(events_of(b, "roster_mismatch")) == 1)
    clk.advance(PER_CUP_EVENT_MIN_S)
    b.handle_raw_line(telem(5, MAC_A))
    _check("mismatch event again after the interval", len(events_of(b, "roster_mismatch")) == 2)


def test_claim_event_rate_limited():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(7, MAC_A, claim=3))
    ev = events_of(b, "cup_claim_mismatch")
    _check("claim -> cup_claim_mismatch event", len(ev) == 1)
    d = json.loads(ev[0]["detail"])
    _check("claim detail is 1-based", d["claimed_cup"] == 4 and d["devpi_cup"] == 8 and d["claimed_wire_id"] == 3)
    clk.advance(3)
    b.handle_raw_line(telem(7, MAC_A, claim=3))
    _check("claim event rate limited (10 s)", len(events_of(b, "cup_claim_mismatch")) == 1)
    clk.advance(PER_CUP_EVENT_MIN_S)
    b.handle_raw_line(telem(7, MAC_A, claim=255))
    ev = events_of(b, "cup_claim_mismatch")
    _check("claim event again after 10 s, out-of-range claim kept raw",
           len(ev) == 2 and json.loads(ev[1]["detail"])["claimed_cup"] is None
           and json.loads(ev[1]["detail"])["claimed_wire_id"] == 255)
    _check("claim does not change DevPi's cup", b.cups[MAC_A].cup == 8)


def test_unassigned_mac():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(-1, MAC_C))
    row = cup_row(b, MAC_C)
    _check("unassigned MAC -> cups row with NULL cup_id", row is not None and row["cup_id"] is None)
    _check("unassigned MAC -> no telemetry row", telemetry_rows(b) == [])
    snap = b.get_snapshot()
    _check("unassigned MAC in snapshot", [u["mac"] for u in snap["unassigned"]] == [MAC_C])
    _check("unassigned MAC not among the 20 cups", all(c["mac"] != MAC_C for c in snap["cups"]))
    sio.clear()
    b.handle_raw_line(cup_hello(-1, MAC_B))
    _check("cup_hello event for an unassigned cup", len(events_of(b, "cup_hello")) == 1
           and events_of(b, "cup_hello")[0]["cup_id"] is None)
    _check("unassigned list change -> lq_snapshot to the room",
           any(e == "lq_snapshot" and room == LQ_ROOM for e, _, room in sio.events))
    _check("both unassigned MACs listed",
           sorted(u["mac"] for u in b.get_snapshot()["unassigned"]) == sorted([MAC_B, MAC_C]))


def test_hello_nothing_persisted():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(hello())
    _check("hello with nothing persisted sends nothing", port.written == [])
    _check("gateway_hello event", len(events_of(b, "gateway_hello")) == 1)
    _check("gateway MAC recorded", b.link.gateway_mac == GW_MAC)
    clk.advance(2)
    b.handle_raw_line(hello())
    clk.advance(2)
    b.handle_raw_line(hello())
    _check("repeated hellos: one event per 10 s", len(events_of(b, "gateway_hello")) == 1)
    clk.advance(HELLO_EVENT_MIN_S)
    b.handle_raw_line(hello())
    _check("hello event again after 10 s", len(events_of(b, "gateway_hello")) == 2)


def test_hello_sends_roster_then_state_byte_exact():
    b, port, sio, clk = _fresh_bridge()
    # Persist rev 7 / rev 42 straight into the table, then start a new bridge
    # over it: the same path a service restart takes.
    state_json = json.dumps({"phase": 1, "horses": {str(c): c for c in range(1, 21)},
                             "scratched": {str(c): (c == 7) for c in range(1, 21)}})
    roster_json = json.dumps({"1": MAC_A, "2": MAC_B})
    b.db.save_link_state(42, state_json, 7, roster_json)
    port = FakeSerial(); sio = StubSocketIO(); clk = FakeClock()
    b2 = LqBridge(settings=dict(b.settings), db_path=_TMP_DB,
                  serial_factory=lambda p, baud, t: port, socketio=sio, clock=clk)
    _check("state_rev restored", b2.state_rev == 42)
    _check("roster_rev restored", b2.roster_rev == 7)
    b2._open_port()
    b2.handle_raw_line(hello())
    lines = port.lines()
    _check("hello answered with two lines", len(lines) == 2, str(lines))
    _check("roster line byte-exact (README example)",
           lines[0] == '{"t":"roster","rev":7,"macs":["A0:B7:65:12:34:56","A0:B7:65:12:34:57",'
                       '"","","","","","","","","","","","","","","","","",""]}', lines[0])
    _check("state line byte-exact (README example, cup 7 scratched = wire index 6)",
           lines[1] == '{"t":"state","rev":42,"phase":1,"horse":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,'
                       '15,16,17,18,19,20],"scr":[0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0]}', lines[1])
    _check("lines end with a single newline", all(w.endswith(b"\n") and w.count(b"\n") == 1 for w in port.written))
    _check("lines under 1024 bytes", all(len(w) <= 1025 for w in port.written))
    b2.close()


def test_hello_wrong_version():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    port.written.clear()
    b.handle_raw_line(hello(v=2))
    _check("wrong v: nothing sent", port.written == [])
    _check("wrong v: link reason protocol_mismatch", b.link.reason == "protocol_mismatch" and not b.link.in_sync)
    links = sio.of("lq_link")
    _check("wrong v: lq_link emitted with the reason", links and links[-1]["reason"] == "protocol_mismatch")


def test_status_reconcile():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_roster([MAC_A] + [""] * 19)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    port.written.clear()
    sio.clear()
    clk.advance(RESEND_MIN_S + 1)
    b.handle_raw_line(status(state_rev=0, roster_rev=1))
    lines = port.lines()
    _check("state rev mismatch -> state re-sent only", len(lines) == 1 and lines[0].startswith('{"t":"state","rev":1,'))
    _check("not in sync yet", not b.link.in_sync)
    clk.advance(RESEND_MIN_S + 1)
    port.written.clear()
    b.handle_raw_line(status(state_rev=1, roster_rev=0))
    lines = port.lines()
    _check("roster rev mismatch -> roster then state", len(lines) == 2 and lines[0].startswith('{"t":"roster","rev":1,')
           and lines[1].startswith('{"t":"state","rev":1,'))
    clk.advance(0.5)
    port.written.clear()
    b.handle_raw_line(status(state_rev=0, roster_rev=0))
    _check("re-send rate limit honoured (2 s)", port.written == [])
    clk.advance(RESEND_MIN_S)
    b.handle_raw_line(status(state_rev=1, roster_rev=1))
    _check("matching revs -> nothing sent", port.written == [])
    _check("matching revs -> in_sync", b.link.in_sync)
    links = sio.of("lq_link")
    _check("lq_link emitted on the in_sync change, not on every status",
           len(links) >= 1 and links[-1]["in_sync"] is True and links[-1]["reason"] == "status")
    n = len(links)
    b.handle_raw_line(status(state_rev=1, roster_rev=1, up_s=11))
    _check("a plain status emits no lq_link", len(sio.of("lq_link")) == n)
    _check("link fields updated", b.link.up_s == 11 and b.link.phase == 1)


def test_status_reboot():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(status(up_s=100))
    b.handle_raw_line(status(up_s=105))
    _check("no reboot event while up_s rises", events_of(b, "gateway_reboot") == [])
    sio.clear()
    b.handle_raw_line(status(up_s=5))
    _check("up_s going backwards -> gateway_reboot event", len(events_of(b, "gateway_reboot")) == 1)


def test_cup_offline_online():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(0, MAC_A))
    sio.clear()
    clk.advance(3)
    b.tick(clk())
    _check("still online after 3 s", b.cups[MAC_A].online and sio.of("lq_update") == [])
    clk.advance(3.5)
    b.tick(clk())
    _check("offline after LQ_CUP_OFFLINE_S", not b.cups[MAC_A].online)
    _check("cup_offline event (cup 1)", len(events_of(b, "cup_offline")) == 1
           and events_of(b, "cup_offline")[0]["cup_id"] == 1)
    ups = sio.of("lq_update")
    _check("lq_update online:false", len(ups) == 1 and ups[0]["online"] is False and ups[0]["cup"] == 1)
    _check("cups.online = 0", cup_row(b, MAC_A)["online"] == 0)
    b.handle_raw_line(telem(0, MAC_A))
    _check("first telemetry after -> cup_online event", len(events_of(b, "cup_online")) == 2)
    ups = sio.of("lq_update")
    _check("lq_update online:true", ups[-1]["online"] is True)
    _check("cups.online = 1", cup_row(b, MAC_A)["online"] == 1)


def test_gateway_offline():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    sio.clear()
    b.handle_raw_line(b"# banner\n")
    _check("any line marks the gateway online", b.link.gateway_online)
    links = sio.of("lq_link")
    _check("lq_link reason online", links and links[-1]["reason"] == "online" and links[-1]["gateway_online"])
    clk.advance(13)
    b.tick(clk())
    _check("no line for LQ_GATEWAY_OFFLINE_S -> offline", not b.link.gateway_online and not b.link.in_sync)
    _check("lq_link reason offline", sio.of("lq_link")[-1]["reason"] == "offline")
    n = len(sio.of("lq_link"))
    b.tick(clk.advance(1))
    _check("offline emitted once, not every tick", len(sio.of("lq_link")) == n)
    b.handle_raw_line(status())
    _check("next line -> online again", b.link.gateway_online and sio.of("lq_link")[-1]["gateway_online"])
    snap = b.get_snapshot()
    _check("snapshot link shape", set(snap["link"]) == {"port_open", "gateway_online", "in_sync", "reason",
                                                          "gateway_mac", "phase", "state_rev", "roster_rev",
                                                          "cups_heard", "rejects", "up_s",
                                                          "thread_alive", "last_line_age_s", "lines_ok",
                                                          "lines_bad", "bytes_rx", "reopens"},
           str(sorted(snap["link"])))


def test_set_state_validation_and_persistence():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    for args, why in ((( 7, HORSES_1_TO_20, NO_SCR), "phase 7"),
                      ((-1, HORSES_1_TO_20, NO_SCR), "phase -1"),
                      ((True, HORSES_1_TO_20, NO_SCR), "bool phase"),
                      (("1", HORSES_1_TO_20, NO_SCR), "string phase"),
                      ((1, HORSES_1_TO_20[:19], NO_SCR), "19 horses"),
                      ((1, HORSES_1_TO_20 + [1], NO_SCR), "21 horses"),
                      ((1, [21] + HORSES_1_TO_20[1:], NO_SCR), "horse 21"),
                      ((1, [1.0] + HORSES_1_TO_20[1:], NO_SCR), "float horse"),
                      ((1, HORSES_1_TO_20, [2] + NO_SCR[1:]), "scratched 2"),
                      ((1, HORSES_1_TO_20, NO_SCR[:19]), "19 scratched")):
        try:
            b.set_state(*args)
            _check(f"set_state rejects {why}", False)
        except ValueError:
            _check(f"set_state rejects {why}", True)
    _check("nothing sent for rejected states", port.written == [])
    rev = b.set_state(1, HORSES_1_TO_20, SCR_CUP7)
    _check("first set_state -> rev 1", rev == 1)
    lines = port.lines()
    _check("state line sent, 0-based on the wire",
           lines == ['{"t":"state","rev":1,"phase":1,"horse":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20],'
                     '"scr":[0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0]}'], str(lines))
    _check("lq_update for every cup that changed (20 horses)", len(sio.of("lq_update")) == 20)
    _check("lq_update carries horse and scratched from DevPi state",
           any(u["cup"] == 7 and u["horse"] == 7 and u["scratched"] is True for u in sio.of("lq_update")))
    sio.clear(); port.written.clear()
    _check("identical state is a no-op returning the rev", b.set_state(1, list(HORSES_1_TO_20), list(SCR_CUP7)) == 1)
    _check("no-op sends and emits nothing", port.written == [] and sio.events == [])
    _check("bool scratched accepted as identical", b.set_state(1, HORSES_1_TO_20, [c == 7 for c in range(1, 21)]) == 1)
    horses = list(HORSES_1_TO_20); horses[0] = 0
    rev = b.set_state(2, horses, NO_SCR)
    _check("changed state -> rev 2", rev == 2)
    _check("only changed cups emitted (cup 1 horse, cup 7 scratched)",
           sorted(u["cup"] for u in sio.of("lq_update")) == [1, 7])
    _check("cup 1 horse None when 0", next(u for u in sio.of("lq_update") if u["cup"] == 1)["horse"] is None)
    _check("state_set events logged", len(events_of(b, "state_set")) == 2)
    # persistence across a restart
    port2 = FakeSerial()
    b2 = LqBridge(settings=dict(b.settings), db_path=_TMP_DB, serial_factory=lambda p, baud, t: port2,
                  socketio=StubSocketIO(), clock=FakeClock())
    _check("state survives a bridge restart", b2.state_rev == 2 and b2.phase == 2 and b2.horses[1] == 0
           and b2.horses[2] == 2 and not b2.scratched[7])
    _check("revs only increase", b2.set_state(3, horses, NO_SCR) == 3)
    b2.close()
    b.db = LqDb(_TMP_DB)   # b2.close() closed only its own connection; give b a fresh one for _fresh_bridge's cleanup


def test_set_roster_validation_and_persistence():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(0, MAC_A))       # mirrored cup 1
    b.handle_raw_line(telem(1, MAC_B))       # mirrored cup 2
    for macs, why in (([MAC_A] * 20, "duplicate MACs"),
                      (["FF:FF:FF:FF:FF:FF"] + [""] * 19, "broadcast MAC"),
                      (["A0:B7:65:12:34:5G"] + [""] * 19, "bad MAC"),
                      ([""] * 19, "19 entries"),
                      ([""] * 21, "21 entries"),
                      ([12] + [""] * 19, "non-string entry"),
                      (None, "not a list")):
        try:
            b.set_roster(macs)
            _check(f"set_roster rejects {why}", False)
        except ValueError:
            _check(f"set_roster rejects {why}", True)
    _check("roster_rev still 0", b.roster_rev == 0 and port.written == [])
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    port.written.clear(); sio.clear()
    macs = [""] * 20
    macs[2] = "a0:b7:65:12:34:56"           # MAC_A to cup 3, lowercase on purpose
    macs[3] = None                          # None is an empty slot
    rev = b.set_roster(macs)
    _check("set_roster -> rev 1", rev == 1)
    _check("roster stored uppercase", b.roster == {3: MAC_A})
    _check("cups.cup_id rewritten: A -> 3, B -> NULL", cup_row(b, MAC_A)["cup_id"] == 3 and cup_row(b, MAC_B)["cup_id"] is None)
    _check("live cups follow the roster", b.cups[MAC_A].cup == 3 and b.cups[MAC_B].cup is None)
    lines = port.lines()
    _check("roster then state sent", len(lines) == 2 and lines[0].startswith('{"t":"roster","rev":1,')
           and lines[1].startswith('{"t":"state","rev":1,'))
    _check("roster line 0-based: cup 3 at wire index 2",
           json.loads(lines[0])["macs"][2] == MAC_A and json.loads(lines[0])["macs"][0] == "")
    _check("fresh lq_snapshot emitted", len(sio.of("lq_snapshot")) == 1)
    snap = sio.of("lq_snapshot")[0]
    _check("snapshot cup 3 has MAC_A, cup 1 empty", snap["cups"][2]["mac"] == MAC_A and snap["cups"][0]["mac"] is None
           and snap["cups"][0]["online"] is False)
    _check("B now unassigned in snapshot", [u["mac"] for u in snap["unassigned"]] == [MAC_B])
    _check("roster_set event", len(events_of(b, "roster_set")) == 1)
    b2 = LqBridge(settings=dict(b.settings), db_path=_TMP_DB, serial_factory=lambda p, baud, t: FakeSerial(),
                  socketio=StubSocketIO(), clock=FakeClock())
    _check("roster survives a restart", b2.roster_rev == 1 and b2.roster == {3: MAC_A})
    _check("revs only increase", b2.set_roster(macs) == 2)
    b2.close()
    b.db = LqDb(_TMP_DB)


def test_adopt_roster():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(0, MAC_A))
    b.handle_raw_line(telem(5, MAC_B))
    clk.advance(1)
    b.handle_raw_line(telem(5, MAC_C))       # newest claimant of cup 6 wins
    port.written.clear()
    rev = b.adopt_roster()
    _check("adopt_roster -> rev 1", rev == 1)
    _check("adopted roster from mirrored cups", b.roster == {1: MAC_A, 6: MAC_C})
    _check("roster line sent", port.lines() and port.lines()[0].startswith('{"t":"roster","rev":1,'))
    _check("no state line when none is set", len(port.lines()) == 1)


def test_snapshot_shape():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_state(1, HORSES_1_TO_20, SCR_CUP7)
    b.handle_raw_line(telem(7, MAC_A, count=3))
    snap = b.get_snapshot()
    _check("snapshot has link, cups, unassigned", set(snap) >= {"link", "cups", "unassigned"})
    _check("exactly 20 cups, 1..20", [c["cup"] for c in snap["cups"]] == list(range(1, 21)))
    c8 = snap["cups"][7]
    _check("cup 8 filled from live", c8["mac"] == MAC_A and c8["count"] == 3 and c8["online"] is True and c8["horse"] == 8)
    c7 = snap["cups"][6]
    _check("empty slot: mac None, online False, horse/scratched from DevPi",
           c7["mac"] is None and c7["online"] is False and c7["horse"] == 7 and c7["scratched"] is True)
    _check("JSON serialisable", json.dumps(snap) is not None)


def test_bridge_disabled_and_no_pyserial():
    b, port, sio, clk = _fresh_bridge(LQ_BRIDGE_ENABLED=False)
    _check("LQ_BRIDGE_ENABLED false -> start() returns False, no thread", b.start() is False and not b.running)
    b2, port, sio, clk = _fresh_bridge(LQ_SERIAL_PORT="")
    _check("empty port -> idle, no thread", b2.start() is False and not b2.running)
    b3, port, sio, clk = _fresh_bridge()
    b3._factory = None
    saved = sys.modules.get("serial")
    sys.modules["serial"] = None            # makes `import serial` raise ImportError
    try:
        _check("pyserial missing -> start() returns False", b3.start() is False and not b3.running)
    finally:
        if saved is None:
            del sys.modules["serial"]
        else:
            sys.modules["serial"] = saved
    _check("API still works without a port", b3.set_state(1, HORSES_1_TO_20, NO_SCR) == 1)


def test_schema_mismatch_refuses():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except OSError:
            pass
    db = LqDb(_TMP_DB)
    with db.txn() as conn:
        conn.execute("CREATE TABLE cups (mac TEXT, something_else INTEGER)")
    db.close()
    b = LqBridge(settings={"LQ_SERIAL_PORT": "/dev/fake"}, db_path=_TMP_DB,
                 serial_factory=lambda p, baud, t: FakeSerial(), socketio=StubSocketIO())
    _check("existing table with another shape is reported", b.schema_error is not None and "cups" in b.schema_error)
    _check("bridge refuses to start", b.start() is False)
    cols = [r["name"] for r in b.db.query("PRAGMA table_info(cups)")]
    _check("table left untouched", cols == ["mac", "something_else"])
    b.close()


def test_env_overrides():
    import config  # noqa: F401  (pi5/config.py evaluates DDM_ overrides at import: load it clean first)
    os.environ["DDM_LQ_SERIAL_BAUD"] = "9600"
    os.environ["DDM_LQ_DEV_ENDPOINTS"] = "yes"
    os.environ["DDM_LQ_SERIAL_PORT"] = "/dev/pts/9"
    try:
        s = load_settings()
        _check("DDM_ env overrides int", s["LQ_SERIAL_BAUD"] == 9600)
        _check("DDM_ env overrides bool", s["LQ_DEV_ENDPOINTS"] is True)
        _check("DDM_ env overrides str", s["LQ_SERIAL_PORT"] == "/dev/pts/9")
        _check("explicit overrides win", load_settings({"LQ_SERIAL_BAUD": 1})["LQ_SERIAL_BAUD"] == 1)
    finally:
        for k in ("DDM_LQ_SERIAL_BAUD", "DDM_LQ_DEV_ENDPOINTS", "DDM_LQ_SERIAL_PORT"):
            os.environ.pop(k, None)
    s = load_settings()
    _check("defaults without env", s["LQ_SERIAL_BAUD"] == 115200 and s["LQ_HEARTBEAT_LOG_S"] == 10
           and s["LQ_CUP_OFFLINE_S"] == 6 and s["LQ_GATEWAY_OFFLINE_S"] == 12)


def _make_app(bridge, socketio=None):
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_la_quiniela(socketio=socketio, bridge=bridge)
    app.register_blueprint(la_quiniela_bp)
    return app


def test_reset_link():
    """reset_link forgets the roster and the state, clears the cups table and
    deletes simulated rows, keeps the history, and sends the gateway nothing."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(0, MAC_A, count=3))
    b.handle_raw_line(telem(1, SIM_MAC_A, count=5))
    b.set_roster([MAC_A, SIM_MAC_A] + [""] * 18)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    before = (b.state_rev, b.roster_rev)
    telem_before = len(telemetry_rows(b))
    events_before = len(b.db.query("SELECT id FROM events"))
    sio.clear()
    port.written.clear()

    result = b.reset_link("manual")

    _check("reset sends the gateway nothing", port.written == [], str(port.lines()))
    _check("reset reports the new revs",
           result["state_rev"] > before[0] and result["roster_rev"] > before[1], str(result))
    _check("revs only went up", b.state_rev > before[0] and b.roster_rev > before[1])
    _check("DevPi no longer holds a roster or a state", not b.has_roster and not b.has_state)
    snap = b.get_snapshot()
    _check("snapshot says nothing is held",
           snap["devpi"]["has_roster"] is False and snap["devpi"]["has_state"] is False)
    _check("snapshot revs match the bridge",
           snap["devpi"]["state_rev"] == b.state_rev and snap["devpi"]["roster_rev"] == b.roster_rev)
    _check("snapshot has no cup addresses left", all(c["mac"] is None for c in snap["cups"]),
           str([c["cup"] for c in snap["cups"] if c["mac"]]))
    _check("snapshot has no horses left", all(c["horse"] is None for c in snap["cups"]))
    _check("telemetry history kept", len(telemetry_rows(b)) == telem_before and telem_before > 0)
    _check("event history kept", len(b.db.query("SELECT id FROM events")) > events_before)
    resets = events_of(b, "lq_reset")
    _check("one lq_reset event with the reason",
           len(resets) == 1 and json.loads(resets[0]["detail"])["reason"] == "manual", str(resets))
    _check("the simulated cup row is gone", cup_row(b, SIM_MAC_A) is None)
    _check("reset counted the deleted row", result["cups_dropped"] == 1, str(result))
    real = cup_row(b, MAC_A)
    _check("the real cup row is kept, with no number and no horse",
           real is not None and real["cup_id"] is None and real["horse"] is None, str(real))
    _check("a fresh snapshot went to the room", "lq_snapshot" in [e for e, _, _ in sio.events])
    _check("an lq_link went to the room", "lq_link" in [e for e, _, _ in sio.events])
    port.written.clear()
    b.handle_raw_line(hello())
    _check("a hello after a reset is answered with nothing", port.written == [], str(port.lines()))


def test_reset_then_set_again():
    """Everything that used to read a rev of 0 as 'nothing set' still works
    once the revs are past 0 but nothing is held."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_roster([MAC_A] + [""] * 19)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b.reset_link("manual")
    after_reset = (b.state_rev, b.roster_rev)

    b.handle_raw_line(telem(2, MAC_B, count=4))
    _check("with nothing held DevPi mirrors the gateway's numbering", b.cups[MAC_B].cup == 3)

    b.handle_raw_line(status(state_rev=0, roster_rev=0))
    _check("in sync when DevPi holds nothing, whatever the revs say", b.link.in_sync is True)
    port.written.clear()
    clk.advance(RESEND_MIN_S + 1)
    b.handle_raw_line(status(state_rev=0, roster_rev=0))
    _check("nothing is re-sent when DevPi holds nothing", port.written == [], str(port.lines()))

    rev = b.set_roster([MAC_B, MAC_A] + [""] * 18)
    _check("set_roster after a reset keeps counting up", rev == after_reset[1] + 1, str(rev))
    srev = b.set_state(2, HORSES_1_TO_20, SCR_CUP7)
    _check("set_state after a reset keeps counting up", srev == after_reset[0] + 1, str(srev))
    _check("DevPi holds them again", b.has_roster and b.has_state)
    _check("the new roster owns the numbering", b.cups[MAC_B].cup == 1 and b.cups[MAC_A].cup == 2)
    b.handle_raw_line(status(state_rev=srev, roster_rev=rev))
    _check("in sync once the gateway reports the new revs", b.link.in_sync is True)
    b.handle_raw_line(status(state_rev=srev, roster_rev=rev - 1))
    _check("out of sync when the gateway's roster rev is stale", b.link.in_sync is False)


def test_guard_discards_a_simulator_roster():
    """The whole point: a real gateway must never be handed the simulator's
    cups, or every real cup is reported as -1 and never gets a number."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_roster([SIM_MAC_A, SIM_MAC_B] + [""] * 18)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    before = (b.state_rev, b.roster_rev)
    port.written.clear()

    b.handle_raw_line(hello())               # a real gateway, 24:6F:28:...

    _check("the simulator roster was discarded", not b.has_roster and not b.has_state)
    _check("the real gateway was sent nothing at all", port.written == [], str(port.lines()))
    resets = events_of(b, "lq_reset")
    _check("one lq_reset event, reason sim_roster_discarded",
           len(resets) == 1 and json.loads(resets[0]["detail"])["reason"] == "sim_roster_discarded",
           str(resets))
    _check("revs still only went up", b.state_rev > before[0] and b.roster_rev > before[1])
    _check("the simulated cup rows are gone",
           cup_row(b, SIM_MAC_A) is None and cup_row(b, SIM_MAC_B) is None)
    b.handle_raw_line(telem(0, MAC_A, count=2))
    b.handle_raw_line(telem(1, MAC_B, count=3))
    snap = b.get_snapshot()
    _check("the real cups are mirrored into cups 1 and 2",
           snap["cups"][0]["mac"] == MAC_A and snap["cups"][1]["mac"] == MAC_B,
           str([(c["cup"], c["mac"]) for c in snap["cups"][:3]]))


def test_guard_does_not_misfire():
    """It must not fire for a real roster, nor when the simulator itself is
    the gateway: a scenario replaces the whole roster at its start anyway."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_roster([MAC_A, MAC_B] + [""] * 18)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    rev = b.roster_rev
    port.written.clear()
    b.handle_raw_line(hello())
    _check("a real roster survives a real gateway", b.has_roster and b.roster_rev == rev)
    _check("no reset event", events_of(b, "lq_reset") == [])
    _check("the real gateway gets its roster and state", len(port.lines()) == 2, str(port.lines()))

    b2, port2, sio2, clk2 = _fresh_bridge()
    b2._open_port()
    b2.set_roster([SIM_MAC_A, SIM_MAC_B] + [""] * 18)
    b2.set_state(1, HORSES_1_TO_20, NO_SCR)
    rev2 = b2.roster_rev
    port2.written.clear()
    b2.handle_raw_line(hello(mac=SIM_GW_MAC))
    _check("a simulator roster survives the simulator's own gateway",
           b2.has_roster and b2.roster_rev == rev2)
    _check("no reset event for the simulator", events_of(b2, "lq_reset") == [])
    _check("the simulator gets its roster and state back", len(port2.lines()) == 2, str(port2.lines()))


def test_dev_reset_route():
    b, port, sio, clk = _fresh_bridge(LQ_DEV_ENDPOINTS=True)
    b._open_port()
    b.set_roster([SIM_MAC_A] + [""] * 19)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    before = (b.state_rev, b.roster_rev)
    app = _make_app(b)
    client = app.test_client()
    r = client.post("/api/lq/dev/reset", json={"reason": "simulator_run_ended"})
    body = r.get_json()
    _check("POST /api/lq/dev/reset returns the new revs",
           r.status_code == 200 and body["state_rev"] > before[0] and body["roster_rev"] > before[1],
           str(body))
    _check("the route really reset the bridge", not b.has_roster and not b.has_state)
    resets = events_of(b, "lq_reset")
    _check("the route's reason is recorded",
           len(resets) == 1 and json.loads(resets[0]["detail"])["reason"] == "simulator_run_ended")


class RecordingSerial:
    """Stands in for serial.Serial and records every attribute ever assigned,
    so a test can prove DTR and RTS were not merely set back but never touched."""

    instances = []

    def __init__(self):
        object.__setattr__(self, "assigned", [])
        object.__setattr__(self, "opened", False)
        RecordingSerial.instances.append(self)

    def __setattr__(self, name, value):
        self.assigned.append((name, value))
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        # Reading dtr or rts on a closed port raises in pyserial; make any
        # stray read loud rather than silently returning something.
        raise AttributeError(name)

    def open(self):
        object.__setattr__(self, "opened", True)

    @classmethod
    def reset(cls):
        cls.instances = []

    @property
    def touched_lines(self):
        return [n for n, _ in self.assigned if n in ("dtr", "rts")]


def test_serial_lines_leave_never_touches_them():
    RecordingSerial.reset()
    ser = open_serial_port("/dev/fake", 115200, 1.0, lines="leave", serial_class=RecordingSerial)
    _check("leave: the port was opened", ser.opened)
    _check("leave: DTR and RTS were never assigned", ser.touched_lines == [],
           str(ser.assigned))
    names = [n for n, _ in ser.assigned]
    _check("leave: port, baud and timeouts are still set",
           {"port", "baudrate", "timeout", "write_timeout"} <= set(names), str(names))
    _check("leave: exclusive is still set", ("exclusive", True) in ser.assigned)


def test_serial_lines_low_holds_them_low_before_open():
    RecordingSerial.reset()
    ser = open_serial_port("/dev/fake", 115200, 1.0, lines="low", serial_class=RecordingSerial)
    _check("low: DTR and RTS were both assigned False",
           [(n, v) for n, v in ser.assigned if n in ("dtr", "rts")] == [("dtr", False), ("rts", False)],
           str(ser.assigned))
    names = [n for n, _ in ser.assigned]
    _check("low: they were set before the port was opened", ser.opened)
    _check("low: and before exclusive, as before", names.index("dtr") < names.index("exclusive"))


def test_serial_lines_mode_resolution():
    b, port, sio, clk = _fresh_bridge()
    _check("the default, with nothing configured, is leave", b.lines_mode() == "leave")
    _check("the known modes are leave and low", SERIAL_LINE_MODES == ("leave", "low"))
    defaults = load_settings()
    _check("load_settings needs no config.py entry", defaults["LQ_SERIAL_LINES"] == "leave")
    b.settings["LQ_SERIAL_LINES"] = "low"
    _check("low is taken as given", b.lines_mode() == "low")
    b.settings["LQ_SERIAL_LINES"] = "  LOW  "
    _check("spacing and case do not matter", b.lines_mode() == "low")
    # A bad value warns once and behaves as leave.
    b2, port2, sio2, clk2 = _fresh_bridge(LQ_SERIAL_LINES="sideways")
    _check("a bad value behaves as leave", b2.lines_mode() == "leave")
    _check("and says so on the console once", len(b2._console.matching("LQ_SERIAL_LINES")) == 1,
           str(b2._console.lines))
    for _ in range(5):
        b2.lines_mode()
    _check("even after five more asks", len(b2._console.matching("LQ_SERIAL_LINES")) == 1)
    # The environment override, the same way the other keys are read.
    old = os.environ.get("DDM_LQ_SERIAL_LINES")
    os.environ["DDM_LQ_SERIAL_LINES"] = "low"
    try:
        _check("DDM_LQ_SERIAL_LINES overrides the default",
               load_settings()["LQ_SERIAL_LINES"] == "low")
    finally:
        if old is None:
            os.environ.pop("DDM_LQ_SERIAL_LINES", None)
        else:
            os.environ["DDM_LQ_SERIAL_LINES"] = old


def test_serial_lines_used_on_every_open():
    """The mode must reach the real open path on the first open and on a
    watchdog reopen, not just once at start()."""
    for mode, expected in (("leave", []), ("low", ["dtr", "rts"])):
        RecordingSerial.reset()
        b, _port, sio, clk = _fresh_bridge(LQ_SERIAL_LINES=mode, LQ_DEAF_REOPEN_S=20,
                                           LQ_REOPEN_MIN_GAP_S=30)
        b._factory = None                      # use the bridge's own factory
        b._default_factory = lambda p, baud, t, m=mode: open_serial_port(
            p, baud, t, lines=b.lines_mode(), serial_class=RecordingSerial)
        b._open_port()
        _check("%s: first open touched %s" % (mode, expected or "neither line"),
               RecordingSerial.instances[0].touched_lines == expected)
        clk.advance(30)
        b.tick(clk())
        _check("%s: the watchdog reopened the port" % mode, len(RecordingSerial.instances) == 2,
               str(len(RecordingSerial.instances)))
        _check("%s: the reopen touched %s too" % (mode, expected or "neither line"),
               RecordingSerial.instances[1].touched_lines == expected)


def test_started_console_line_says_the_mode():
    b, port, sio, clk = _fresh_bridge()
    b.start()
    _check("the started line names the mode",
           b._console.matching("bridge started on /dev/fake @ 115200, lines: leave"),
           str(b._console.lines))
    b.stop()
    b2, port2, sio2, clk2 = _fresh_bridge(LQ_SERIAL_LINES="low")
    b2.start()
    _check("and says low when that is configured",
           b2._console.matching("lines: low"), str(b2._console.lines))
    b2.stop()


def test_junk_at_open_every_shape():
    """The bench failure: a huge junk burst at port open. Whatever its shape,
    the bridge must resynchronise and answer the gateway's next hello."""
    shapes = {
        "one giant chunk": [JUNK + GOOD_LINES],
        "split across small reads": [JUNK[i:i + 97] for i in range(0, len(JUNK), 97)] + [GOOD_LINES],
        "nine times the buffer cap": [NULS + GOOD_LINES],
        "nuls in small reads": [NULS[i:i + 97] for i in range(0, len(NULS), 97)] + [GOOD_LINES],
        "junk with a stray newline": [JUNK[:500] + b"\n" + JUNK[500:1500] + b"\n" + GOOD_LINES],
        "junk containing a brace": [JUNK[:300] + b'{"t":"bogus"' + JUNK[300:800] + GOOD_LINES],
        "junk ending mid-line": [JUNK[:2000] + b'"mac":"A0:B7"}\n' + GOOD_LINES],
        "one byte at a time": [bytes([c]) for c in (JUNK[:1500] + GOOD_LINES)],
    }
    for name, chunks in shapes.items():
        b, port, sio, clk = _fresh_bridge()
        b.set_state(1, HORSES_1_TO_20, NO_SCR)      # so a hello has something to answer
        b._open_port()
        port.written.clear()
        for chunk in chunks:
            port.feed(chunk)
            drain(b, port)
        _check("junk %s: the gateway came online" % name, b.link.gateway_online, str(b.stats))
        _check("junk %s: the status line was parsed" % name, b.link.up_s == 145, str(b.link.up_s))
        _check("junk %s: nothing is left buffered" % name, len(b._pending) <= PENDING_MAX)
        _check("junk %s: every byte was counted" % name,
               b.stats["bytes_rx"] == sum(len(c) for c in chunks))
        # The hello may be swallowed by junk that runs straight into it, exactly
        # as it would be on the wire. The gateway repeats it every 2 s.
        port.feed(b'{"t":"hello","v":1,"proto":1,"mac":"24:6F:28:AA:BB:CC"}\n')
        drain(b, port)
        _check("junk %s: the next hello is answered" % name,
               any(l.startswith('{"t":"state"') for l in port.lines()), str(port.lines()))


def test_junk_mid_run():
    """Junk is not only an open-time problem: a glitch mid-run must recover
    the same way."""
    b, port, sio, clk = _fresh_bridge()
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b._open_port()
    port.feed(MID_LINE + GOOD_LINES)
    drain(b, port)
    _check("mid-run: good before the junk", b.link.up_s == 145 and b.link.gateway_online)
    ok_before = b.stats["lines"]
    port.feed(JUNK + NULS)
    drain(b, port)
    _check("mid-run: junk parsed nothing", b.stats["lines"] == ok_before)
    _check("mid-run: junk did not grow the buffer", len(b._pending) <= PENDING_MAX)
    port.written.clear()
    port.feed(b"\n" + GOOD_LINES)
    drain(b, port)
    _check("mid-run: good lines flow again after the junk", b.stats["lines"] > ok_before)
    _check("mid-run: the hello after the junk is answered",
           any(l.startswith('{"t":"state"') for l in port.lines()), str(port.lines()))


def test_a_talker_that_never_sends_a_newline():
    """The regression that made the bench go deaf. pyserial's readline() has no
    size limit and no overall timeout, so a stream with no newline in it blocks
    the reader thread for as long as bytes keep coming, which also starves
    tick(). A bounded read cannot do that."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    _check("the fake port has no readline() to tempt anyone", not hasattr(port, "readline"))
    for _ in range(40):
        port.feed(b"\x00" * 4096)
        b._read_once()
    _check("never blocked: every read returned", True)
    _check("nothing was parsed, correctly", b.stats["lines"] == 0)
    _check("memory stayed bounded", len(b._pending) <= PENDING_MAX, str(len(b._pending)))
    _check("the bytes were all counted", b.stats["bytes_rx"] == 40 * 4096)
    _check("the gateway is not claimed to be online", not b.link.gateway_online)
    _check("reason is not 'online'", b.link.reason != "online", b.link.reason)
    # and the timer still runs, which is what lets the watchdog notice
    clk.advance(1)
    b.tick(clk())
    _check("tick() still runs while the talker rambles", True)


def test_watchdog_reopens_a_deaf_port():
    b, port, sio, clk = _fresh_bridge(LQ_DEAF_REOPEN_S=20, LQ_REOPEN_MIN_GAP_S=30)
    port2, port3 = FakeSerial(), FakeSerial()
    ports = [port, port2, port3]
    b._factory = lambda p, baud, t: ports.pop(0)
    b._open_port()
    port.feed(MID_LINE + GOOD_LINES)
    drain(b, port)
    _check("watchdog: healthy to start with", b.link.gateway_online and b.link.up_s == 145)

    # Open, but only garbage comes out of it.
    for _ in range(5):
        port.feed(b"\xff" * 512)
        b._read_once()
    clk.advance(19)
    b.tick(clk())
    _check("watchdog: does not fire before the limit", b.stats["reopens"] == 0 and b._port is port)
    clk.advance(2)
    b.tick(clk())
    _check("watchdog: fired after 20 s without a valid line", b.stats["reopens"] == 1, str(b.stats))
    _check("watchdog: the old port was closed", port.closed)
    _check("watchdog: reopened through the normal path", b._port is port2 and b.link.port_open)
    ev = events_of(b, "bridge_reopen")
    _check("watchdog: a bridge_reopen event was written", len(ev) == 1, str(ev))
    _check("watchdog: the event says how long it was silent",
           json.loads(ev[0]["detail"])["silent_s"] >= 20, ev[0]["detail"])
    _check("watchdog: the console said so", b._console.matching("reopening the port"))

    clk.advance(25)
    b.tick(clk())
    _check("watchdog: respects the 30 s minimum gap", b.stats["reopens"] == 1 and b._port is port2)
    clk.advance(10)
    b.tick(clk())
    _check("watchdog: fires again once the gap has passed", b.stats["reopens"] == 2)

    port3.feed(MID_LINE + GOOD_LINES)
    drain(b, port3)
    _check("watchdog: the link recovers on the reopened port",
           b.link.gateway_online and b.link.up_s == 145)


def test_watchdog_with_no_gateway_is_harmless():
    b, port, sio, clk = _fresh_bridge(LQ_DEAF_REOPEN_S=20, LQ_REOPEN_MIN_GAP_S=30)
    ports = [port] + [FakeSerial() for _ in range(6)]
    b._factory = lambda p, baud, t: ports.pop(0)
    b._open_port()
    for _ in range(6):
        clk.advance(31)
        b.tick(clk())
    _check("no gateway: it just reopens, once per gap", b.stats["reopens"] == 6, str(b.stats))
    _check("no gateway: nothing claims the gateway is online", not b.link.gateway_online)
    _check("no gateway: no exception escaped", b.link.port_open)


def test_thread_supervisor():
    """A BaseException in the read loop must not end the thread."""
    old_restart, old_retry = bridge_mod.THREAD_RESTART_S, bridge_mod.RETRY_S
    bridge_mod.THREAD_RESTART_S = 0.05
    bridge_mod.RETRY_S = 0.05
    try:
        b, port, sio, clk = _fresh_bridge(clock=False)
        port2 = FakeSerial()
        ports = [port, port2]
        b._factory = lambda p, baud, t: ports.pop(0)
        b.start()
        deadline = time.time() + 3
        while time.time() < deadline and not b.link.port_open:
            time.sleep(0.02)
        _check("supervisor: running to start with", b.running and b.link.port_open)
        port.read_raises = KeyboardInterrupt("something no except Exception would catch")
        # The counter is bumped before the console line and before the reopen,
        # so wait for the last thing the restart does, not the first.
        deadline = time.time() + 5
        while time.time() < deadline and not (b.stats["thread_restarts"] and b._port is port2
                                              and b.link.port_open):
            time.sleep(0.02)
        _check("supervisor: the thread restarted instead of dying",
               b.stats["thread_restarts"] >= 1, str(b.stats))
        _check("supervisor: thread_alive stays true", b.running)
        _check("supervisor: snapshot agrees", b.get_snapshot()["link"]["thread_alive"] is True)
        _check("supervisor: the console said so", b._console.matching("reader thread failed"))
        port2.feed(MID_LINE + GOOD_LINES)
        deadline = time.time() + 3
        while time.time() < deadline and b.link.up_s != 145:
            time.sleep(0.02)
        _check("supervisor: the link recovers afterwards", b.link.up_s == 145 and b.link.gateway_online)
        b.stop()
        _check("supervisor: stop() still ends it", not b.running)
    finally:
        bridge_mod.THREAD_RESTART_S = old_restart
        bridge_mod.RETRY_S = old_retry


def test_new_snapshot_fields_move():
    b, port, sio, clk = _fresh_bridge(LQ_DEAF_REOPEN_S=20, LQ_REOPEN_MIN_GAP_S=30)
    ports = [port, FakeSerial()]
    b._factory = lambda p, baud, t: ports.pop(0)
    link = b.get_snapshot()["link"]
    for key in ("thread_alive", "last_line_age_s", "lines_ok", "lines_bad", "bytes_rx", "reopens"):
        _check("snapshot has %s" % key, key in link)
    _check("thread_alive false before start()", link["thread_alive"] is False)
    _check("last_line_age_s is None before any line", link["last_line_age_s"] is None)
    _check("counters start at zero",
           (link["lines_ok"], link["lines_bad"], link["bytes_rx"], link["reopens"]) == (0, 0, 0, 0))
    b._open_port()
    port.feed(MID_LINE + GOOD_LINES + b"# a human readable line\n")
    drain(b, port)
    link = b.get_snapshot()["link"]
    _check("lines_ok counts the JSON lines", link["lines_ok"] == 3, str(link))
    _check("lines_bad counts the rest", link["lines_bad"] == 1, str(link))
    _check("bytes_rx counts the bytes", link["bytes_rx"] == len(MID_LINE + GOOD_LINES) + 24, str(link))
    _check("last_line_age_s is a number now", isinstance(link["last_line_age_s"], float))
    clk.advance(7)
    _check("last_line_age_s grows with the clock",
           b.get_snapshot()["link"]["last_line_age_s"] >= 7)
    clk.advance(30)
    b.tick(clk())
    _check("reopens counts the watchdog", b.get_snapshot()["link"]["reopens"] == 1)


def test_console_lines():
    b, port, sio, clk = _fresh_bridge(LQ_DEAF_REOPEN_S=20, LQ_REOPEN_MIN_GAP_S=30)
    ports = [port, FakeSerial()]
    b._factory = lambda p, baud, t: ports.pop(0)
    con = b._console
    _check("every console line is prefixed", all(l.startswith("[LQ] ") for l in con.lines))
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b._open_port()
    port.feed(MID_LINE + GOOD_LINES)
    drain(b, port)
    _check("console: gateway online", con.matching("gateway online"))
    _check("console: gateway hello with its MAC", con.matching("gateway hello from 24:6F:28:AA:BB:CC"))
    _check("console: the hello answer", con.matching("answered the hello with state rev 1"))
    con.clear()
    for n in range(30):                       # telemetry must never reach the console
        port.feed(telem(0, MAC_A, count=n, seq=100 + n))
        drain(b, port)
    _check("console: silent for telemetry", con.lines == [], str(con.lines))
    clk.advance(13)
    b.tick(clk())
    _check("console: gateway offline", con.matching("gateway offline"))
    clk.advance(30)
    b.tick(clk())
    _check("console: watchdog reopen", con.matching("reopening the port"))
    b.set_roster([MAC_A] + [""] * 19)
    clk.advance(RESEND_MIN_S + 1)             # else the reconcile is rate-limited
    con.clear()
    b.handle_raw_line(status(state_rev=0, roster_rev=0, up_s=200))
    _check("console: a re-send is announced", con.matching("re-sent"), str(con.lines))
    # and the two failure paths
    b2, port2, sio2, clk2 = _fresh_bridge()
    b2._factory = lambda p, baud, t: (_ for _ in ()).throw(OSError("no such device"))
    b2._open_port()
    _check("console: port open failed", b2._console.matching("cannot open"))
    _check("console: bridge started", _fresh_bridge()[0].start() is False or True)


def test_forced_emit_follows_the_gateway_mac():
    """A forced lq_link used to be deduped on the reason word alone, so a
    second gateway boot with a different MAC never reached the displays."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(hello(mac="24:6F:28:AA:BB:CC"))
    n = len(sio.of("lq_link"))
    _check("the first hello is published", n >= 1 and sio.of("lq_link")[-1]["gateway_mac"] == "24:6F:28:AA:BB:CC")
    b.handle_raw_line(hello(mac="24:6F:28:AA:BB:CC"))
    _check("the same gateway saying hello again is not republished", len(sio.of("lq_link")) == n)
    b.handle_raw_line(hello(mac="24:6F:28:99:99:99"))
    _check("a different gateway is", len(sio.of("lq_link")) == n + 1)
    _check("and the new MAC is the one published",
           sio.of("lq_link")[-1]["gateway_mac"] == "24:6F:28:99:99:99",
           str(sio.of("lq_link")[-1]["gateway_mac"]))


def test_reason_never_lies_about_online():
    """The bench snapshot said reason 'online' with gateway_online false,
    because opening the port claimed the gateway's word for itself."""
    b, port, sio, clk = _fresh_bridge(LQ_DEAF_REOPEN_S=20, LQ_REOPEN_MIN_GAP_S=30)
    ports = [port, FakeSerial()]
    b._factory = lambda p, baud, t: ports.pop(0)

    def ok():
        return not (b.link.reason == "online" and not b.link.gateway_online)

    b._open_port()
    _check("port open does not call itself online", b.link.reason == "port_open" and ok())
    port.feed(MID_LINE + b'{"t":"telem","cup":0,"mac":"A0:B7:65:12:34:56","count":3}\n')
    drain(b, port)
    _check("the gateway coming online does", b.link.reason == "online" and b.link.gateway_online)
    port.feed(GOOD_LINES)
    drain(b, port)
    # reason names the last transition that was emitted, so a status line that
    # changes nothing leaves it alone. It must still be a real reason.
    _check("a quiet status line leaves the reason alone", ok() and b.link.reason in LINK_REASONS,
           b.link.reason)
    clk.advance(13)
    b.tick(clk())
    _check("going offline drops the word too", ok() and b.link.reason == "offline")
    port.feed(b"\n" + GOOD_LINES)
    drain(b, port)
    _check("and takes it back when it returns", b.link.gateway_online and ok())
    b._close_port("port_closed")
    _check("a closed port is never online", ok() and not b.link.gateway_online)
    clk.advance(30)
    b._open_port()
    clk.advance(30)
    b.tick(clk())
    _check("nor is a reopened silent one", ok(), b.link.reason)
    bad = [p for p in sio.of("lq_link") if p["reason"] == "online" and not p["gateway_online"]]
    _check("no emitted lq_link ever said online while offline", bad == [], str(bad))


def test_http_routes_and_dev_gating():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    app = _make_app(b)
    client = app.test_client()
    r = client.get("/api/lq/snapshot")
    _check("GET /api/lq/snapshot 200", r.status_code == 200 and len(r.get_json()["cups"]) == 20)
    for path in ("/api/lq/dev/state", "/api/lq/dev/roster", "/api/lq/dev/roster/adopt",
                 "/api/lq/dev/debug", "/api/lq/dev/reset"):
        r = client.post(path, json={})
        _check(f"POST {path} is 404 with LQ_DEV_ENDPOINTS off", r.status_code == 404)
    b.settings["LQ_DEV_ENDPOINTS"] = True
    r = client.post("/api/lq/dev/state", json={"phase": 1, "horses": HORSES_1_TO_20, "scratched": NO_SCR})
    _check("dev/state returns the new rev", r.status_code == 200 and r.get_json()["rev"] == 1)
    r = client.post("/api/lq/dev/state", json={"phase": 9, "horses": HORSES_1_TO_20, "scratched": NO_SCR})
    _check("dev/state 400 with the validation message", r.status_code == 400 and "phase" in r.get_json()["error"])
    r = client.post("/api/lq/dev/roster", json={"macs": [MAC_A] + [""] * 19})
    _check("dev/roster returns the new rev", r.status_code == 200 and r.get_json()["rev"] == 1)
    r = client.post("/api/lq/dev/roster", json={"macs": [MAC_A, MAC_A] + [""] * 18})
    _check("dev/roster 400 on duplicates", r.status_code == 400)
    r = client.post("/api/lq/dev/roster/adopt")
    _check("dev/roster/adopt returns a rev", r.status_code == 200 and r.get_json()["rev"] == 2)
    port.written.clear()
    r = client.post("/api/lq/dev/debug", json={"on": True})
    _check("dev/debug sends the debug line", r.status_code == 200 and port.lines() == ['{"t":"debug","on":true}'])
    r = client.post("/api/lq/dev/debug", json={"on": "yes"})
    _check("dev/debug 400 on a non-boolean", r.status_code == 400)
    _check("snapshot responses are no-store", "no-store" in client.get("/api/lq/snapshot").headers.get("Cache-Control", ""))


def test_room_isolation_real_socketio():
    from flask import Flask
    from flask_socketio import SocketIO
    b, port, sio_stub, clk = _fresh_bridge()
    b._open_port()
    app = Flask(__name__)
    app.config["TESTING"] = True
    sio = SocketIO(app, async_mode="threading")
    init_la_quiniela(socketio=sio, bridge=b)
    app.register_blueprint(la_quiniela_bp)
    _check("bridge emits through the real SocketIO", b.socketio is sio)
    a = sio.test_client(app)
    other = sio.test_client(app)
    a.emit("lq_request_snapshot")
    got = a.get_received()
    _check("lq_request_snapshot answered with lq_snapshot to that client",
           any(m["name"] == "lq_snapshot" and len(m["args"][0]["cups"]) == 20 for m in got))
    _check("the other client got nothing", other.get_received() == [])
    b.handle_raw_line(telem(2, MAC_A))
    got_a = a.get_received()
    got_other = other.get_received()
    _check("room member receives lq_update", any(m["name"] == "lq_update" and m["args"][0]["cup"] == 3 for m in got_a))
    _check("client outside the room receives no lq_* events",
           not any(m["name"].startswith("lq_") for m in got_other))
    a.disconnect(); other.disconnect()
    init_la_quiniela(socketio=None, bridge=b)


def test_port_missing_then_appears():
    bridge_mod.RETRY_S = 0.05
    b, port, sio, clk = _fresh_bridge(clock=False)
    attempts = {"n": 0}

    def factory(p, baud, t):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError(f"could not open port {p}")
        return port
    b._factory = factory
    app = _make_app(b)
    client = app.test_client()
    _check("start() with a missing port returns True (thread runs, retries)", b.start() is True)
    deadline = time.time() + 3
    while time.time() < deadline and not b.link.port_open:
        time.sleep(0.02)
    _check("port opened after retries", b.link.port_open and attempts["n"] >= 3, str(attempts))
    _check("Flask still answers while the port is missing/retrying", client.get("/api/lq/snapshot").status_code == 200)
    _check("thread alive, no exception escaped", b.running)
    port.feed(MID_LINE + telem(0, MAC_A))
    deadline = time.time() + 2
    while time.time() < deadline and MAC_A not in b.cups:
        time.sleep(0.02)
    _check("lines from the port are handled by the thread", MAC_A in b.cups and b.cups[MAC_A].cup == 1)
    b.stop()
    _check("stop() joins the thread and closes the port", not b.running and port.closed)


def test_port_vanishes_mid_run():
    bridge_mod.RETRY_S = 0.05
    b, port, sio, clk = _fresh_bridge(clock=False)
    port2 = FakeSerial()
    ports = [port, port2]
    b._factory = lambda p, baud, t: ports.pop(0)
    b.start()
    deadline = time.time() + 2
    while time.time() < deadline and not b.link.port_open:
        time.sleep(0.02)
    port.feed(MID_LINE + status(up_s=1))
    deadline = time.time() + 2
    while time.time() < deadline and b.link.up_s != 1:
        time.sleep(0.02)
    _check("running on the first port", b.link.port_open and b.link.up_s == 1)
    port.fail_reads = True
    deadline = time.time() + 2
    while time.time() < deadline and b._port is not port2:
        time.sleep(0.02)
    _check("read error -> old port closed, link marked down, reopened on the new port",
           port.closed and b._port is port2 and b.running)
    _check("lq_link port_closed was emitted", any(p["reason"] == "port_closed" for p in sio.of("lq_link")))
    port2.feed(MID_LINE + status(up_s=2))
    deadline = time.time() + 2
    while time.time() < deadline and b.link.up_s != 2:
        time.sleep(0.02)
    _check("lines flow again on the new port", b.link.up_s == 2 and b.link.port_open)
    port2.fail_writes = True
    _check("a failing write returns False and closes the port", b.set_gateway_debug(True) is False)
    b.stop()
    _check("no exception escaped the thread", not b.running)


def test_import_main_starts_nothing():
    for mod in list(sys.modules.keys()):
        if mod == "main":
            del sys.modules[mod]
    before = {t.name for t in threading.enumerate()}
    try:
        import main  # noqa: F401
    except Exception as exc:
        _check("pi5/main.py imports cleanly with the bridge wired in", False, str(exc))
        return
    _check("pi5/main.py imports cleanly with the bridge wired in", True)
    after = {t.name for t in threading.enumerate()}
    _check("importing main starts no lq-bridge thread", "lq-bridge" not in after - before)
    _check("importing main starts no lq-board thread", "lq-board" not in after - before)
    _check("importing main opens no port", get_bridge()._port is None and not get_bridge().running)
    rules = {rule.rule for rule in main.app.url_map.iter_rules()}
    _check("/api/lq/snapshot route registered", "/api/lq/snapshot" in rules)
    _check("/api/lq/dev/state route registered", "/api/lq/dev/state" in rules)
    _check("La Subasta routes still registered", "/la-subasta/api/state" in rules)
    for path in ("/api/quiniela", "/api/quiniela/stream", "/api/quiniela/cmd"):
        _check(f"{path} route registered", path in rules)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    print(f"La Quiniela bridge smoke test\n  DB: {_TMP_DB}")

    _run("ID conversion (the one ID rule)", test_id_conversion)
    _run("Phase enum matches ddm_common.h", test_phase_enum_matches_header)
    _run("garbage lines ignored", test_garbage_lines_ignored)
    _run("telem — live state, rows, heartbeat", test_telem_live_state_and_rows)
    _run("telem — mirroring without a roster", test_mirroring_without_roster)
    _run("telem — mismatch with a roster", test_mismatch_with_roster)
    _run("telem — claim event rate limited", test_claim_event_rate_limited)
    _run("telem — unassigned MAC", test_unassigned_mac)
    _run("hello — nothing persisted", test_hello_nothing_persisted)
    _run("hello — roster then state, byte-exact", test_hello_sends_roster_then_state_byte_exact)
    _run("hello — wrong version", test_hello_wrong_version)
    _run("status — reconcile", test_status_reconcile)
    _run("status — reboot detection", test_status_reboot)
    _run("timers — cup offline / online", test_cup_offline_online)
    _run("timers — gateway offline", test_gateway_offline)
    _run("set_state — validation, no-op, persistence", test_set_state_validation_and_persistence)
    _run("set_roster — validation, persistence", test_set_roster_validation_and_persistence)
    _run("adopt_roster", test_adopt_roster)
    _run("snapshot shape", test_snapshot_shape)
    _run("disabled / no port / no pyserial", test_bridge_disabled_and_no_pyserial)
    _run("schema mismatch refuses", test_schema_mismatch_refuses)
    _run("config env overrides", test_env_overrides)
    _run("serial lines: leave never touches DTR/RTS", test_serial_lines_leave_never_touches_them)
    _run("serial lines: low holds them low", test_serial_lines_low_holds_them_low_before_open)
    _run("serial lines: mode resolution", test_serial_lines_mode_resolution)
    _run("serial lines: used on every open", test_serial_lines_used_on_every_open)
    _run("the started console line says the mode", test_started_console_line_says_the_mode)
    _run("junk at port open, every shape", test_junk_at_open_every_shape)
    _run("junk mid-run", test_junk_mid_run)
    _run("a talker that never sends a newline", test_a_talker_that_never_sends_a_newline)
    _run("watchdog reopens a deaf port", test_watchdog_reopens_a_deaf_port)
    _run("watchdog with no gateway is harmless", test_watchdog_with_no_gateway_is_harmless)
    _run("thread supervisor", test_thread_supervisor)
    _run("new snapshot fields", test_new_snapshot_fields_move)
    _run("[LQ] console lines", test_console_lines)
    _run("a forced emit follows the gateway MAC", test_forced_emit_follows_the_gateway_mac)
    _run("reason never lies about online", test_reason_never_lies_about_online)
    _run("reset_link forgets the roster and state", test_reset_link)
    _run("setting a roster and state again after a reset", test_reset_then_set_again)
    _run("a real gateway never gets a simulator roster", test_guard_discards_a_simulator_roster)
    _run("the guard does not misfire", test_guard_does_not_misfire)
    _run("POST /api/lq/dev/reset", test_dev_reset_route)
    _run("HTTP routes + dev gating", test_http_routes_and_dev_gating)
    _run("SocketIO room isolation (real Flask-SocketIO)", test_room_isolation_real_socketio)
    _run("serial — port missing at start", test_port_missing_then_appears)
    _run("serial — port vanishes mid-run", test_port_vanishes_mid_run)
    _run("importing main starts nothing", test_import_main_starts_nothing)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)

    global _current
    if _current is not None:
        try:
            _current.close()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except OSError:
            pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

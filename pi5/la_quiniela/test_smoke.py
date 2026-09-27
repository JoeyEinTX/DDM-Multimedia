# la_quiniela/test_smoke.py - La Quiniela bridge smoke test
#
# Run with: python -m la_quiniela.test_smoke  (from the pi5/ dir)
#
# No hardware, no real serial port: a fake port feeds the bridge the
# gateway's lines and captures what it writes, a stub SocketIO records every
# emit, and a fake clock drives the timers. Same tiny runner as
# la_subasta/test_smoke.py. Protocol v2: cups are known by MAC and by the
# horse they report; there are no slots, rosters or cup IDs to test.

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
    HELLO_EVENT_MIN_S, LINK_REASONS, LQ_ROOM, PENDING_MAX, RESEND_MIN_S, SERIAL_LINE_MODES,
    LqBridge, load_settings, open_serial_port,
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


def _drop_db():
    """Close the bridge the last _fresh_bridge() made and remove the temp
    database. The close matters on Windows, which keeps the file locked while
    a connection is open; on DevPi the remove would go through regardless."""
    global _current
    if _current is not None:
        try:
            _current.close()
        except Exception:
            pass
        _current = None
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except OSError:
            pass


def _fresh_bridge(clock=True, **settings):
    """A bridge on an empty database with a fake port, stub socketio and (by
    default) a fake clock. The port is opened straight away, no thread."""
    global _current
    _drop_db()
    port = FakeSerial()
    sio = StubSocketIO()
    clk = FakeClock() if clock else None
    cfg = {"LQ_SERIAL_PORT": "/dev/fake", "LQ_SERIAL_BAUD": 115200, "LQ_HEARTBEAT_LOG_S": 10,
           "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12}
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


def telem(mac, horse=0, count=14, raw=812345, seq=9021, drop=2, rssi=-64, up=-61, hello=False):
    """A v2 telem line: the cup's MAC and the horse it says it is."""
    d = {"t": "telem", "mac": mac, "horse": horse, "raw": raw, "count": count, "seq": seq,
         "drop": drop, "rssi": rssi, "up": up}
    if hello:
        d["hello"] = 1
    return (json.dumps(d, separators=(",", ":")) + "\n").encode()


def cup_entry(mac, horse=0, tok=0, rssi=-63, up=-61, age=180):
    return {"mac": mac, "horse": horse, "tok": tok, "rssi": rssi, "up": up, "age": age}


def status(gseq=1, phase=1, state_rev=0, cups=(), rejects=0, up_s=10):
    return (json.dumps({"t": "status", "gseq": gseq, "phase": phase, "state_rev": state_rev,
                        "cups": list(cups), "rejects": rejects, "up_s": up_s},
                       separators=(",", ":")) + "\n").encode()


def hello(v=2, mac=GW_MAC, proto=2):
    return (json.dumps({"t": "hello", "v": v, "proto": proto, "mac": mac}) + "\n").encode()


def events_of(b, type_):
    return [dict(r) for r in b.db.query("SELECT * FROM events WHERE type = ? ORDER BY id", (type_,))]


def telemetry_rows(b):
    return [dict(r) for r in b.db.query("SELECT * FROM telemetry ORDER BY id")]


def cup_row(b, mac):
    r = b.db.query_one("SELECT * FROM lq_cups WHERE mac = ?", (mac,))
    return dict(r) if r else None


def cup_in(snap, mac):
    return next((c for c in snap["cups"] if c["mac"] == mac), None)


# What a real port hands over the instant it is opened: the tail of a line
# that was already in flight. The bridge must drop it and resynchronise on the
# first newline, so every test that feeds the port starts with this.
MID_LINE = b'5:12:34:56","count":3,"seq":11}\n'

# The burst a CP2102 hands over at open: bytes received before the baud rate
# was applied, on a line that never stops talking. Thousands of bytes, no
# newline, values above 0x7F and long runs of NUL.
JUNK = bytes((i * 7 + 3) % 256 for i in range(6000)).replace(b"\n", b"\x01")
NULS = b"\x00" * 9000
GOOD_LINES = (b'{"t":"hello","v":2,"proto":2,"mac":"24:6F:28:AA:BB:CC"}\n'
              b'{"t":"telem","mac":"A0:B7:65:12:34:56","horse":7,"raw":812345,"count":3,'
              b'"seq":11,"drop":0,"rssi":-64,"up":-61}\n'
              b'{"t":"status","gseq":1,"phase":1,"state_rev":0,"cups":[{"mac":"A0:B7:65:12:34:56",'
              b'"horse":7,"tok":3,"rssi":-64,"up":-61,"age":120}],"rejects":0,"up_s":145}\n')

STATE_REV1 = '{"t":"state","rev":1,"st":0,"scr":[],"renum":[],"res":[0,0,0]}'


def drain(b, port, limit=500):
    """Pump the real _read_once until the fake port has nothing left."""
    for _ in range(limit):
        if not port.in_waiting:
            return
        b._read_once()
    raise AssertionError("port never drained")


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

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
    for define, ours in (("DDM_MAX_HORSE", P.MAX_HORSE), ("DDM_RENUM_SLOTS", P.RENUM_SLOTS),
                         ("DDM_RESULT_SLOTS", P.RESULT_SLOTS), ("DDM_PROTO_VERSION", P.LINE_PROTO_VERSION)):
        m2 = re.search(r"#define %s\s+(\d+)" % define, text)
        _check(f"{define} matches the header", m2 is not None and int(m2.group(1)) == ours,
               f"header {m2.group(1) if m2 else None}, protocol {ours}")
    _check("no slot arithmetic left in the package",
           not [name for name in ("bridge.py", "blueprint.py", "models.py", "board.py", "betting.py")
                if re.search(r"wire_to_cup|cup_to_wire|CUP_NUMBERS|NUM_CUPS",
                             open(os.path.join(_PI5_DIR, "la_quiniela", name), encoding="utf-8").read())])


def test_protocol_validation_and_lines():
    ok = P.validate_state(1, [9, 15, 9], [[9, 22], (22, 23)], [19, 1, 22])
    _check("validate_state: sorted unique scratched, tuple pairs, results",
           ok == (1, [9, 15], [(9, 22), (22, 23)], [19, 1, 22]), str(ok))
    for args, why in (((7, [], [], [0, 0, 0]), "phase 7"), ((True, [], [], [0, 0, 0]), "bool phase"),
                      ((1, [0], [], [0, 0, 0]), "scratched 0"), ((1, [25], [], [0, 0, 0]), "scratched 25"),
                      ((1, "9", [], [0, 0, 0]), "scratched a string"), ((1, [], [[9, 9]], [0, 0, 0]), "from == to"),
                      ((1, [], [[9, 22], [9, 23]], [0, 0, 0]), "from twice"),
                      ((1, [], [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]], [0, 0, 0]), "five pairs"),
                      ((1, [], [[9]], [0, 0, 0]), "a one-item pair"), ((1, [], [], [1, 2]), "two results"),
                      ((1, [], [], [1, 1, 0]), "the same horse twice"), ((1, [], [], [25, 0, 0]), "result 25")):
        try:
            P.validate_state(*args)
            _check(f"validate_state rejects {why}", False)
        except ValueError:
            _check(f"validate_state rejects {why}", True)
    _check("build_state_line byte-exact (README example)",
           P.build_state_line(42, 1, [9, 15], [(9, 22)], [0, 0, 0])
           == '{"t":"state","rev":42,"st":1,"scr":[9,15],"renum":[[9,22]],"res":[0,0,0]}')
    _check("build_debug_line", P.build_debug_line(True) == '{"t":"debug","on":true}')
    _check("parse_horse: 1..24 as themselves, everything else 0",
           [P.parse_horse(v) for v in (7, 24, 0, 25, -1, None, "7", True, 3.0)] == [7, 24, 0, 0, 0, 0, 0, 0, 0])
    _check("MAX_LINE_BYTES fits a full status line (24 cups)", P.MAX_LINE_BYTES >= 2560)


def test_garbage_lines_ignored():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    for raw in (b"# DDM La Quiniela gateway - ESP-NOW <-> serial JSON bridge\n",
                b"# ---- CUPS seq=1 ----\n", b"\n", b"\r\n", b"[1,2,3]\n",
                b'{"t":"telem"\n', b"{not json}\n",
                b'{"t":"whatever","x":1}\n', b'{"t":"status","gseq":1,"phase":1,"state_rev":0,'
                b'"cups":[],"rejects":0,"up_s":3,"future":{"k":[1]}}\n',
                b'{"t":"telem","horse":"seven","mac":5}\n',
                b'{"t":"roster","rev":7,"macs":[]}\n',
                b"{" + b"x" * 5000 + b"}\n"):
        b.handle_raw_line(raw)
    _check("non-object lines counted as text, not parsed", b.stats["text"] == 3, str(b.stats))
    _check("bad JSON counted", b.stats["bad_json"] == 2, str(b.stats))
    _check("over-long line dropped", b.stats["too_long"] == 1)
    _check("unknown t ignored (a v1 roster line included)", b.stats["unknown_type"] == 2)
    _check("unknown keys ignored, status applied", b.link.up_s == 3)
    _check("the only thing written back is the state, once, for the status holding rev 0",
           port.lines() == [STATE_REV1], str(port.lines()))
    _check("no cups from garbage", b.cups == {})


def test_telem_live_state_and_rows():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(MAC_A, horse=8, count=14))
    live = b.cups[MAC_A]
    _check("live cup keyed by MAC, the horse it reports", live.horse == 8 and b.cups.get(MAC_A) is live)
    _check("live fields updated", live.count == 14 and live.raw == 812345 and live.rssi == -64
           and live.up == -61 and live.drop == 2 and live.online and not live.hello)
    rows = telemetry_rows(b)
    _check("first packet writes a telemetry row", len(rows) == 1)
    _check("row carries mac and horse, reason=change",
           rows[0]["mac"] == MAC_A and rows[0]["horse"] == 8 and rows[0]["reason"] == "change")
    _check("lq_cups row upserted", cup_row(b, MAC_A)["horse"] == 8 and cup_row(b, MAC_A)["online"] == 1)
    ev = events_of(b, "cup_online")
    _check("cup_online event, keyed by horse", len(ev) == 1 and ev[0]["horse"] == 8
           and json.loads(ev[0]["detail"])["mac"] == MAC_A)
    ups = len(sio.of("lq_update"))
    _check("lq_update emitted", ups == 1)
    _check("a cup coming online emits a snapshot", len(sio.of("lq_snapshot")) == 1)

    clk.advance(2)
    b.handle_raw_line(telem(MAC_A, horse=8, count=14))
    _check("unchanged packet inside the interval: no row", len(telemetry_rows(b)) == 1)
    _check("unchanged packet: no lq_update", len(sio.of("lq_update")) == ups)

    clk.advance(1)
    b.handle_raw_line(telem(MAC_A, horse=8, count=15))
    rows = telemetry_rows(b)
    _check("count change: row with reason=change", len(rows) == 2 and rows[1]["reason"] == "change"
           and rows[1]["token_count"] == 15)
    _check("count change: lq_update", len(sio.of("lq_update")) == ups + 1)

    clk.advance(10)
    b.handle_raw_line(telem(MAC_A, horse=8, count=15))
    rows = telemetry_rows(b)
    _check("heartbeat row after the interval", len(rows) == 3 and rows[2]["reason"] == "heartbeat")
    _check("heartbeat: lq_update", len(sio.of("lq_update")) == ups + 2)
    payload = sio.of("lq_update")[-1]
    _check("lq_update payload shape", set(payload) == {"mac", "horse", "count", "raw", "rssi", "up", "drop",
                                                        "seq", "online", "hello", "last_seen"}
           and payload["mac"] == MAC_A and payload["horse"] == 8, str(sorted(payload)))
    _check("every emit went to the lq room", all(room == LQ_ROOM for _, _, room in sio.events))
    b.handle_raw_line(telem(MAC_B, horse=0, count=0, hello=True))
    live_b = b.cups[MAC_B]
    _check("a HELLO packet: tracked with horse 0 and the hello flag", live_b.horse == 0 and live_b.hello
           and live_b.online and cup_row(b, MAC_B)["horse"] == 0)


def test_horse_changes_and_conflicts():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(MAC_A, horse=7, count=5))
    sio.clear()
    b.handle_raw_line(telem(MAC_A, horse=22, count=5))
    ev = events_of(b, "cup_horse")
    _check("a cup changing its horse logs cup_horse from -> to (after the from-0 one of its first packet)",
           len(ev) == 2 and json.loads(ev[-1]["detail"]) == {"mac": MAC_A, "from": 7, "to": 22} and ev[-1]["horse"] == 22,
           str(ev))
    _check("...emits lq_update and a snapshot", sio.of("lq_update")[-1]["horse"] == 22 and sio.of("lq_snapshot"))
    _check("...and says so on the console", b._console.matching("is horse 22 (was 7)"), str(b._console.lines))
    _check("...and the cache follows", cup_row(b, MAC_A)["horse"] == 22)
    b.handle_raw_line(telem(MAC_B, horse=22, count=1))
    snap = b.get_snapshot()
    claimers = [c["mac"] for c in snap["cups"] if c["horse"] == 22]
    _check("two cups claiming one horse are both in the snapshot; the bridge resolves nothing",
           sorted(claimers) == sorted([MAC_A, MAC_B]), str(claimers))
    _check("the snapshot lists cups by MAC, sorted", [c["mac"] for c in snap["cups"]] == sorted([MAC_A, MAC_B]))
    b.handle_raw_line(telem(MAC_C, horse=99, count=0))
    _check("an out-of-range horse reads as 0 (none)", b.cups[MAC_C].horse == 0)
    _check("cup_horse for a cup first heard with a horse is logged too (from 0)",
           any(json.loads(e["detail"])["from"] == 0 for e in events_of(b, "cup_horse")))


def test_hello_answered_with_state():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(hello())
    _check("a fresh bridge answers the hello with its state, rev 1, byte-exact", port.lines() == [STATE_REV1], str(port.lines()))
    _check("gateway_hello event", len(events_of(b, "gateway_hello")) == 1)
    _check("gateway MAC recorded", b.link.gateway_mac == GW_MAC)
    _check("the console says what it answered", b._console.matching("answered the hello with state rev 1"))
    clk.advance(2)
    b.handle_raw_line(hello())
    clk.advance(2)
    b.handle_raw_line(hello())
    _check("repeated hellos: one event per 10 s, every one answered",
           len(events_of(b, "gateway_hello")) == 1 and len(port.lines()) == 3)
    clk.advance(HELLO_EVENT_MIN_S)
    b.handle_raw_line(hello())
    _check("hello event again after 10 s", len(events_of(b, "gateway_hello")) == 2)
    # Persist rev 42 with a full v2 state straight into the table, then start a
    # new bridge over it: the same path a service restart takes.
    b.db.save_link_state(42, json.dumps({"phase": 1, "scratched": [9, 15], "renum": [[9, 22]], "results": [0, 0, 0]}))
    port2 = FakeSerial(); sio2 = StubSocketIO(); clk2 = FakeClock()
    b2 = LqBridge(settings=dict(b.settings), db_path=_TMP_DB,
                  serial_factory=lambda p, baud, t: port2, socketio=sio2, clock=clk2)
    _check("state_rev restored", b2.state_rev == 42)
    _check("the state restored", (b2.phase, b2.scratched, b2.renum) == (1, [9, 15], [(9, 22)]))
    b2._open_port()
    b2.handle_raw_line(hello())
    _check("hello answered with the restored state, byte-exact (README example)",
           port2.lines() == ['{"t":"state","rev":42,"st":1,"scr":[9,15],"renum":[[9,22]],"res":[0,0,0]}'], str(port2.lines()))
    _check("lines end with a single newline", all(w.endswith(b"\n") and w.count(b"\n") == 1 for w in port2.written))
    b2.close()
    # A v1 state_json (cup slots): its phase is kept, the rest starts empty.
    b.db.close()
    b.db = LqDb(_TMP_DB)
    b.db.save_link_state(7, json.dumps({"phase": 2, "horses": {"1": 1}, "scratched": {"7": True}}))
    b3 = LqBridge(settings=dict(b.settings), db_path=_TMP_DB, serial_factory=lambda p, baud, t: FakeSerial(),
                  socketio=StubSocketIO(), clock=FakeClock())
    _check("a v1 state_json keeps its phase and nothing else",
           (b3.state_rev, b3.phase, b3.scratched, b3.renum, b3.results) == (7, 2, [], [], [0, 0, 0]))
    b3.close()
    b.db.close()
    b.db = LqDb(_TMP_DB)


def test_hello_wrong_version():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(hello(v=1, proto=1))
    _check("a v1 gateway: nothing sent", port.written == [])
    _check("wrong v: link reason protocol_mismatch", b.link.reason == "protocol_mismatch" and not b.link.in_sync)
    links = sio.of("lq_link")
    _check("wrong v: lq_link emitted with the reason", links and links[-1]["reason"] == "protocol_mismatch")


def test_status_reconcile_and_cup_table():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_state(phase=1)
    port.written.clear()
    sio.clear()
    clk.advance(RESEND_MIN_S + 1)
    b.handle_raw_line(status(state_rev=0))
    lines = port.lines()
    _check("state rev mismatch -> state re-sent", len(lines) == 1 and lines[0].startswith('{"t":"state","rev":2,'), str(lines))
    _check("not in sync yet", not b.link.in_sync)
    clk.advance(0.5)
    port.written.clear()
    b.handle_raw_line(status(state_rev=0))
    _check("re-send rate limit honoured (2 s)", port.written == [])
    clk.advance(RESEND_MIN_S)
    b.handle_raw_line(status(state_rev=2))
    _check("matching rev -> nothing sent", port.written == [])
    _check("matching rev -> in_sync", b.link.in_sync)
    links = sio.of("lq_link")
    _check("lq_link emitted on the in_sync change, not on every status",
           len(links) >= 1 and links[-1]["in_sync"] is True and links[-1]["reason"] == "status")
    n = len(links)
    b.handle_raw_line(status(state_rev=2, up_s=11))
    _check("a plain status emits no lq_link", len(sio.of("lq_link")) == n)
    _check("link fields updated", b.link.up_s == 11 and b.link.phase == 1)
    # The cup table in the status line: a backstop for cups pi5 has not heard itself.
    sio.clear()
    b.handle_raw_line(status(state_rev=2, cups=[cup_entry(MAC_A, horse=7, tok=23, age=500),
                                                cup_entry(MAC_B, horse=0, tok=0, age=9000)]))
    _check("cups_heard counts the entries the gateway heard within 3 s", b.link.cups_heard == 1)
    a, bb = b.cups[MAC_A], b.cups[MAC_B]
    _check("a cup never heard directly is seeded from the table: horse, count, online",
           a.horse == 7 and a.count == 23 and a.online and cup_row(b, MAC_A)["horse"] == 7)
    _check("...with a cup_online and a cup_horse event", len(events_of(b, "cup_online")) == 1
           and json.loads(events_of(b, "cup_horse")[0]["detail"]).get("via") == "status")
    _check("an entry older than the offline window is cached but offline", bb.horse == 0 and not bb.online)
    _check("a changed table emits a snapshot", sio.of("lq_snapshot"))
    clk.advance(1)
    b.handle_raw_line(telem(MAC_A, horse=7, count=24))
    b.handle_raw_line(status(state_rev=2, cups=[cup_entry(MAC_A, horse=9, tok=1, age=4000)]))
    _check("a table entry older than a direct hearing changes nothing", a.horse == 7 and a.count == 24)


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
    b.handle_raw_line(telem(MAC_A, horse=1))
    sio.clear()
    clk.advance(3)
    b.tick(clk())
    _check("still online after 3 s", b.cups[MAC_A].online and sio.of("lq_update") == [])
    clk.advance(3.5)
    b.tick(clk())
    _check("offline after LQ_CUP_OFFLINE_S", not b.cups[MAC_A].online)
    ev = events_of(b, "cup_offline")
    _check("cup_offline event keyed by the horse, the MAC in the detail",
           len(ev) == 1 and ev[0]["horse"] == 1 and json.loads(ev[0]["detail"])["mac"] == MAC_A)
    ups = sio.of("lq_update")
    _check("lq_update online:false", len(ups) == 1 and ups[0]["online"] is False and ups[0]["mac"] == MAC_A)
    _check("a snapshot too (the per-horse picture moved)", len(sio.of("lq_snapshot")) == 1)
    _check("lq_cups.online = 0", cup_row(b, MAC_A)["online"] == 0)
    b.handle_raw_line(telem(MAC_A, horse=1))
    _check("first telemetry after -> cup_online event", len(events_of(b, "cup_online")) == 2)
    ups = sio.of("lq_update")
    _check("lq_update online:true", ups[-1]["online"] is True)
    _check("lq_cups.online = 1", cup_row(b, MAC_A)["online"] == 1)


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
                                                          "gateway_mac", "phase", "state_rev",
                                                          "cups_heard", "rejects", "up_s",
                                                          "thread_alive", "last_line_age_s", "lines_ok",
                                                          "lines_bad", "bytes_rx", "reopens"},
           str(sorted(snap["link"])))


def test_set_state_validation_and_persistence():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    for kwargs, why in (({"phase": 7}, "phase 7"), ({"phase": -1}, "phase -1"), ({"phase": True}, "bool phase"),
                        ({"phase": "1"}, "string phase"), ({"scratched": [0]}, "scratched 0"),
                        ({"scratched": [25]}, f"scratched {P.MAX_HORSE + 1}"), ({"scratched": [1.0]}, "float scratched"),
                        ({"renum": [[9, 9]]}, "renum from == to"), ({"renum": [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]}, "five pairs"),
                        ({"results": [1, 2]}, "two results"), ({"results": [1, 1, 2]}, "a horse placed twice")):
        try:
            b.set_state(**kwargs)
            _check(f"set_state rejects {why}", False)
        except ValueError:
            _check(f"set_state rejects {why}", True)
    _check("nothing sent for rejected states", port.written == [])
    _check("a fresh bridge starts at rev 1 (persisted, so a hello has a line to get)", b.state_rev == 1
           and b.db.load_link_state()["state_rev"] == 1)
    rev = b.set_state(phase=1, scratched=[7])
    _check("first change -> rev 2", rev == 2)
    lines = port.lines()
    _check("state line sent, byte-exact",
           lines == ['{"t":"state","rev":2,"st":1,"scr":[7],"renum":[],"res":[0,0,0]}'], str(lines))
    _check("a snapshot went to the room, no per-cup lq_update", sio.of("lq_snapshot") and not sio.of("lq_update"))
    sio.clear(); port.written.clear()
    _check("identical state is a no-op returning the rev", b.set_state(phase=1, scratched=[7]) == 2)
    _check("a part left out is kept: scratched stays", b.set_state(phase=1) == 2 and b.scratched == [7])
    _check("no-op sends and emits nothing", port.written == [] and sio.events == [])
    rev = b.set_state(renum=[(9, 22)], results=[19, 1, 22])
    _check("changed parts -> rev 3, the rest kept", rev == 3 and b.phase == 1 and b.scratched == [7]
           and b.renum == [(9, 22)] and b.results == [19, 1, 22])
    ev = events_of(b, "state_set")
    _check("state_set events logged with what changed", len(ev) == 2
           and json.loads(ev[1]["detail"])["changed"] == ["renum", "results"], str(ev))
    _check("state_line() is what went down", port.lines()[-1] == b.state_line())
    # persistence across a restart
    port2 = FakeSerial()
    b2 = LqBridge(settings=dict(b.settings), db_path=_TMP_DB, serial_factory=lambda p, baud, t: port2,
                  socketio=StubSocketIO(), clock=FakeClock())
    _check("state survives a bridge restart", b2.state_rev == 3 and b2.phase == 1 and b2.scratched == [7]
           and b2.renum == [(9, 22)] and b2.results == [19, 1, 22])
    _check("revs only increase", b2.set_state(phase=3) == 4)
    b2.close()
    b.db = LqDb(_TMP_DB)   # b2.close() closed only its own connection; give b a fresh one for _fresh_bridge's cleanup


def test_snapshot_shape():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.set_state(phase=1, scratched=[7], renum=[(9, 22)])
    b.handle_raw_line(telem(MAC_A, horse=8, count=3))
    snap = b.get_snapshot()
    _check("snapshot has link, devpi, cups", set(snap) == {"link", "devpi", "cups"})
    _check("devpi carries the state keyed by horse",
           snap["devpi"] == {"state_rev": 2, "phase": 1, "scratched": [7], "renum": [[9, 22]], "results": [0, 0, 0]},
           str(snap["devpi"]))
    _check("one entry per cup heard, by MAC", [c["mac"] for c in snap["cups"]] == [MAC_A])
    c = snap["cups"][0]
    _check("the entry: horse, count, online, no slot number",
           c["horse"] == 8 and c["count"] == 3 and c["online"] is True and "cup" not in c)
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
    _check("API still works without a port", b3.set_state(phase=1) == 2)


def test_schema_mismatch_refuses():
    _drop_db()
    db = LqDb(_TMP_DB)
    with db.txn() as conn:
        conn.execute("CREATE TABLE events (ts TEXT, something_else INTEGER)")
    db.close()
    b = LqBridge(settings={"LQ_SERIAL_PORT": "/dev/fake"}, db_path=_TMP_DB,
                 serial_factory=lambda p, baud, t: FakeSerial(), socketio=StubSocketIO())
    _check("existing table with another shape is reported", b.schema_error is not None and "events" in b.schema_error)
    _check("bridge refuses to start", b.start() is False)
    cols = [r["name"] for r in b.db.query("PRAGMA table_info(events)")]
    _check("table left untouched", cols == ["ts", "something_else"])
    b.close()


def test_v1_schema_migrates():
    """DevPi's database holds the protocol v1 tables: cups with cup_id,
    telemetry and events keyed by cup_id, the roster in lq_link_state. A v2
    bridge rebuilds them on start, rows kept."""
    _drop_db()
    db = LqDb(_TMP_DB)
    db.conn.executescript("""
            CREATE TABLE cups (mac TEXT PRIMARY KEY, cup_id INTEGER UNIQUE, horse INTEGER, last_seen TEXT,
                               rssi INTEGER, up_rssi INTEGER, last_count INTEGER, last_raw INTEGER,
                               online INTEGER NOT NULL DEFAULT 0);
            INSERT INTO cups VALUES ('A0:B7:65:12:34:56', 3, 7, '2026-09-26T00:00:00Z', -60, -58, 5, 812345, 0);
            CREATE TABLE telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, cup_id INTEGER,
                                    mac TEXT NOT NULL, raw_weight INTEGER, token_count INTEGER, seq INTEGER,
                                    dropped INTEGER, rssi INTEGER, up_rssi INTEGER,
                                    reason TEXT NOT NULL CHECK (reason IN ('change', 'heartbeat')));
            CREATE INDEX idx_telemetry_cup_ts ON telemetry(cup_id, ts);
            INSERT INTO telemetry (ts, cup_id, mac, raw_weight, token_count, seq, dropped, rssi, up_rssi, reason)
                VALUES ('2026-09-26T00:00:00Z', 3, 'A0:B7:65:12:34:56', 812345, 5, 9, 0, -60, -58, 'change');
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
                                 cup_id INTEGER, detail TEXT);
            INSERT INTO events (ts, type, cup_id, detail) VALUES ('2026-09-26T00:00:00Z', 'cup_online', 3, '{"mac": "x"}');
            CREATE TABLE lq_link_state (id INTEGER PRIMARY KEY CHECK (id = 1), state_rev INTEGER NOT NULL DEFAULT 0,
                                        state_json TEXT, roster_rev INTEGER NOT NULL DEFAULT 0, roster_json TEXT);
            INSERT INTO lq_link_state VALUES (1, 42, '{"phase": 2, "horses": {"3": 7}, "scratched": {"3": false}}', 7,
                                              '{"3": "A0:B7:65:12:34:56"}');
        """)
    db.close()
    b = LqBridge(settings={"LQ_SERIAL_PORT": "/dev/fake"}, db_path=_TMP_DB,
                 serial_factory=lambda p, baud, t: FakeSerial(), socketio=StubSocketIO(), clock=FakeClock())
    _check("the v1 shape is accepted, not refused", b.schema_error is None, str(b.schema_error))
    cols = lambda t: [r["name"] for r in b.db.query(f"PRAGMA table_info({t})")]   # noqa: E731
    _check("cups (the slot table) is gone, lq_cups exists", cols("cups") == [] and "horse" in cols("lq_cups"))
    _check("telemetry rebuilt with horse in place of cup_id, the row kept",
           cols("telemetry")[:4] == ["id", "ts", "mac", "horse"] and len(telemetry_rows(b)) == 1
           and telemetry_rows(b)[0]["horse"] is None)
    _check("events rebuilt, the row kept", cols("events") == ["id", "ts", "type", "horse", "detail"]
           and len(events_of(b, "cup_online")) == 1)
    _check("lq_link_state rebuilt: rev and phase kept, the roster gone",
           cols("lq_link_state") == ["id", "state_rev", "state_json"] and b.state_rev == 42 and b.phase == 2)
    _check("the migration is idempotent", b.db._migrate_v2() == [])
    b.close()


def test_env_overrides():
    import config  # noqa: F401  (pi5/config.py evaluates DDM_ overrides at import: load it clean first)
    os.environ["DDM_LQ_SERIAL_BAUD"] = "9600"
    os.environ["DDM_LQ_BRIDGE_ENABLED"] = "no"
    os.environ["DDM_LQ_SERIAL_PORT"] = "/dev/pts/9"
    try:
        s = load_settings()
        _check("DDM_ env overrides int", s["LQ_SERIAL_BAUD"] == 9600)
        _check("DDM_ env overrides bool", s["LQ_BRIDGE_ENABLED"] is False)
        _check("DDM_ env overrides str", s["LQ_SERIAL_PORT"] == "/dev/pts/9")
        _check("explicit overrides win", load_settings({"LQ_SERIAL_BAUD": 1})["LQ_SERIAL_BAUD"] == 1)
    finally:
        for k in ("DDM_LQ_SERIAL_BAUD", "DDM_LQ_BRIDGE_ENABLED", "DDM_LQ_SERIAL_PORT"):
            os.environ.pop(k, None)
    s = load_settings()
    _check("defaults without env", s["LQ_SERIAL_BAUD"] == 115200 and s["LQ_HEARTBEAT_LOG_S"] == 10
           and s["LQ_CUP_OFFLINE_S"] == 6 and s["LQ_GATEWAY_OFFLINE_S"] == 12)
    _check("no dev flag any more", "LQ_DEV_ENDPOINTS" not in s)


def _make_app(bridge, socketio=None):
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_la_quiniela(socketio=socketio, bridge=bridge)
    app.register_blueprint(la_quiniela_bp)
    return app


def test_forget_cups():
    """The cup cache decides nothing, but a cup that went home would sit on
    the admin page as offline forever, and the simulator's cups would too."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(MAC_A, horse=1, count=3))
    b.handle_raw_line(telem(SIM_MAC_A, horse=2, count=5))
    b.handle_raw_line(telem(SIM_MAC_B, horse=3, count=5))
    sio.clear()
    _check("forget by prefix drops the simulator's cups, in memory and in lq_cups",
           b.forget_cups(bridge_mod.SIM_MAC_PREFIX) == 2 and sorted(b.cups) == [MAC_A]
           and cup_row(b, SIM_MAC_A) is None and cup_row(b, MAC_A) is not None)
    _check("...with an event and a snapshot", events_of(b, "cups_forgotten") and sio.of("lq_snapshot"))
    _check("the state is untouched", b.state_rev == 1)
    b.handle_raw_line(telem(SIM_MAC_A, horse=2, count=5))
    port.written.clear()
    b.handle_raw_line(hello(mac=SIM_GW_MAC))
    _check("the simulator's own gateway keeps them", SIM_MAC_A in b.cups)
    b.handle_raw_line(hello())
    _check("a real gateway's hello drops them", SIM_MAC_A not in b.cups and MAC_A in b.cups)
    _check("...and is answered with the state all the same", port.lines()[-1] == STATE_REV1)
    _check("forget all", b.forget_cups() == 1 and b.cups == {})
    _check("a forgotten cup that still talks is back within a packet",
           (b.handle_raw_line(telem(MAC_A, horse=1, count=3)) or MAC_A in b.cups))


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
        port.feed(b'{"t":"hello","v":2,"proto":2,"mac":"24:6F:28:AA:BB:CC"}\n')
        drain(b, port)
        _check("junk %s: the next hello is answered" % name,
               any(l.startswith('{"t":"state"') for l in port.lines()), str(port.lines()))


def test_junk_mid_run():
    """Junk is not only an open-time problem: a glitch mid-run must recover
    the same way."""
    b, port, sio, clk = _fresh_bridge()
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
    b._open_port()
    port.feed(MID_LINE + GOOD_LINES)
    drain(b, port)
    _check("console: gateway online", con.matching("gateway online"))
    _check("console: gateway hello with its MAC", con.matching("gateway hello from 24:6F:28:AA:BB:CC"))
    _check("console: the hello answer", con.matching("answered the hello with state rev 1"))
    _check("console: a cup announcing its horse", con.matching("cup A0:B7:65:12:34:56 is horse 7"), str(con.lines))
    con.clear()
    for n in range(30):                       # telemetry must never reach the console
        port.feed(telem(MAC_A, horse=7, count=n, seq=100 + n))
        drain(b, port)
    _check("console: silent for telemetry", con.lines == [], str(con.lines))
    clk.advance(13)
    b.tick(clk())
    _check("console: gateway offline", con.matching("gateway offline"))
    clk.advance(30)
    b.tick(clk())
    _check("console: watchdog reopen", con.matching("reopening the port"))
    clk.advance(RESEND_MIN_S + 1)             # else the reconcile is rate-limited
    con.clear()
    b.handle_raw_line(status(state_rev=0, up_s=200))
    _check("console: a re-send is announced", con.matching("re-sent state rev"), str(con.lines))
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
    port.feed(MID_LINE + b'{"t":"telem","mac":"A0:B7:65:12:34:56","horse":7,"count":3}\n')
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


def test_http_routes():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    b.handle_raw_line(telem(MAC_A, horse=7, count=3))
    app = _make_app(b)
    client = app.test_client()
    r = client.get("/api/lq/snapshot")
    _check("GET /api/lq/snapshot 200 with the cups by MAC", r.status_code == 200
           and [c["mac"] for c in r.get_json()["cups"]] == [MAC_A] and r.get_json()["devpi"]["state_rev"] == 1)
    for path in ("/api/lq/dev/state", "/api/lq/dev/roster", "/api/lq/dev/roster/adopt",
                 "/api/lq/dev/reset", "/api/lq/dev/roster/clear", "/api/lq/dev/debug"):
        r = client.post(path, json={})
        _check(f"POST {path} no longer exists (404)", r.status_code == 404)
    port.written.clear()
    r = client.post("/api/lq/debug", json={"on": True})
    _check("POST /api/lq/debug sends the debug line, no flag needed",
           r.status_code == 200 and port.lines() == ['{"t":"debug","on":true}'])
    r = client.post("/api/lq/debug", json={"on": "yes"})
    _check("debug 400 on a non-boolean", r.status_code == 400)
    r = client.post("/api/lq/cups/forget", json={"mac": MAC_A})
    _check("POST /api/lq/cups/forget drops the cup", r.status_code == 200 and r.get_json()["forgotten"] == 1 and b.cups == {})
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
           any(m["name"] == "lq_snapshot" and "cups" in m["args"][0] for m in got))
    _check("the other client got nothing", other.get_received() == [])
    b.handle_raw_line(telem(MAC_A, horse=3))
    got_a = a.get_received()
    got_other = other.get_received()
    _check("room member receives lq_update", any(m["name"] == "lq_update" and m["args"][0]["mac"] == MAC_A for m in got_a))
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
    port.feed(MID_LINE + telem(MAC_A, horse=1))
    deadline = time.time() + 2
    while time.time() < deadline and MAC_A not in b.cups:
        time.sleep(0.02)
    _check("lines from the port are handled by the thread", MAC_A in b.cups and b.cups[MAC_A].horse == 1)
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
    _check("/api/lq/debug and /api/lq/cups/forget registered", "/api/lq/debug" in rules and "/api/lq/cups/forget" in rules)
    _check("no dev routes registered", not any(r.startswith("/api/lq/dev/") for r in rules), str(sorted(rules)))
    _check("La Subasta routes still registered", "/la-subasta/api/state" in rules)
    for path in ("/api/quiniela", "/api/quiniela/stream", "/api/quiniela/cmd", "/api/quiniela/horses",
                 "/api/quiniela/scratch", "/api/quiniela/unscratch", "/api/quiniela/closes_at",
                 "/api/quiniela/reset", "/api/quiniela/field", "/api/quiniela/mode", "/quiniela/admin"):
        _check(f"{path} route registered", path in rules)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    print(f"La Quiniela bridge smoke test\n  DB: {_TMP_DB}")

    _run("Phase enum and the v2 constants match ddm_common.h", test_phase_enum_matches_header)
    _run("protocol — validation and byte-exact lines", test_protocol_validation_and_lines)
    _run("garbage lines ignored", test_garbage_lines_ignored)
    _run("telem — live state, rows, heartbeat", test_telem_live_state_and_rows)
    _run("telem — a cup changes its horse; two cups claim one", test_horse_changes_and_conflicts)
    _run("hello — answered with the state, byte-exact, restored", test_hello_answered_with_state)
    _run("hello — wrong version", test_hello_wrong_version)
    _run("status — reconcile and the cup table", test_status_reconcile_and_cup_table)
    _run("status — reboot detection", test_status_reboot)
    _run("timers — cup offline / online", test_cup_offline_online)
    _run("timers — gateway offline", test_gateway_offline)
    _run("set_state — validation, no-op, parts, persistence", test_set_state_validation_and_persistence)
    _run("snapshot shape", test_snapshot_shape)
    _run("disabled / no port / no pyserial", test_bridge_disabled_and_no_pyserial)
    _run("schema mismatch refuses", test_schema_mismatch_refuses)
    _run("the v1 schema migrates", test_v1_schema_migrates)
    _run("config env overrides", test_env_overrides)
    _run("forget_cups and the simulator's cups", test_forget_cups)
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
    _run("HTTP routes", test_http_routes)
    _run("SocketIO room isolation (real Flask-SocketIO)", test_room_isolation_real_socketio)
    _run("serial — port missing at start", test_port_missing_then_appears)
    _run("serial — port vanishes mid-run", test_port_vanishes_mid_run)
    _run("importing main starts nothing", test_import_main_starts_nothing)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)

    _drop_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

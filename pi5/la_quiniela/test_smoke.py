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
    HELLO_EVENT_MIN_S, LQ_ROOM, PER_CUP_EVENT_MIN_S, RESEND_MIN_S, LqBridge, load_settings,
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
    """Lines fed by the test come out of readline(); write() is captured."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.written = []
        self.closed = False
        self.fail_reads = False
        self.fail_writes = False

    def feed(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.inbox.put(data)

    def readline(self):
        if self.fail_reads:
            raise OSError("device disappeared")
        try:
            return self.inbox.get(timeout=0.02)
        except queue.Empty:
            return b""

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
                 socketio=sio, clock=clk)
    _current = b
    return b, port, sio, clk


MAC_A = "A0:B7:65:12:34:56"
MAC_B = "A0:B7:65:12:34:57"
MAC_C = "A0:B7:65:12:34:99"
GW_MAC = "24:6F:28:AA:BB:CC"


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
                                                          "cups_heard", "rejects", "up_s"})


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


def test_http_routes_and_dev_gating():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    app = _make_app(b)
    client = app.test_client()
    r = client.get("/api/lq/snapshot")
    _check("GET /api/lq/snapshot 200", r.status_code == 200 and len(r.get_json()["cups"]) == 20)
    for path in ("/api/lq/dev/state", "/api/lq/dev/roster", "/api/lq/dev/roster/adopt", "/api/lq/dev/debug"):
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
    port.feed(telem(0, MAC_A))
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
    port.feed(status(up_s=1))
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
    port2.feed(status(up_s=2))
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
    _check("importing main opens no port", get_bridge()._port is None and not get_bridge().running)
    rules = {rule.rule for rule in main.app.url_map.iter_rules()}
    _check("/api/lq/snapshot route registered", "/api/lq/snapshot" in rules)
    _check("/api/lq/dev/state route registered", "/api/lq/dev/state" in rules)
    _check("La Subasta routes still registered", "/la-subasta/api/state" in rules)


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

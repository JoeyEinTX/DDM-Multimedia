# la_quiniela/test_simulator.py - La Quiniela cup simulator smoke test
#
# Run with: python -m la_quiniela.test_simulator  (from the pi5/ dir)
#
# Same hand-rolled runner as la_subasta/test_smoke.py. The protocol and model
# tests run in memory in an instant; the end-to-end tests start the real
# bridge in-process against the simulator over a real pty and take real
# time (the cadences never compress).

import io
import json
import os
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
_README = os.path.join(_PI5_DIR, "..", "firmware", "quiniela", "README.md")

from la_subasta import config as la_config  # noqa: E402

_TMP_DB = tempfile.mktemp(prefix="la_quiniela_sim_", suffix=".db")
la_config.DB_PATH = _TMP_DB

from la_quiniela.sim import protocol as W  # noqa: E402
from la_quiniela.sim.model import (  # noqa: E402
    COUNTS_PER_TOKEN, CupSim, GatewaySim, cup_mac, tick_cup,
)
from la_quiniela.sim.protocol import Phase, cup_to_slot, slot_to_cup  # noqa: E402
from la_quiniela.sim.runner import ApiOperator, Simulator, VirtualClock  # noqa: E402
from la_quiniela.sim.scenarios import SCENARIOS  # noqa: E402

import random  # noqa: E402

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
# Helpers
# -----------------------------------------------------------------------------

def readme_example(heading: str) -> dict:
    """The first ```json example under '#### `heading`' in the README."""
    with open(_README, encoding="utf-8") as f:
        text = f.read()
    start = text.index("#### `%s`" % heading)
    block = re.search(r"```json\n(.*?)\n```", text[start:], re.S).group(1)
    return json.loads(block)


def json_lines(lines):
    return [json.loads(l) for l in lines if l.startswith("{")]


def of_type(lines, t):
    return [o for o in json_lines(lines) if o.get("t") == t]


def new_gw(now=1000.0, **kw):
    gw = GatewaySim(now, random.Random(1), **kw)
    return gw


class LocalOperator:
    """A fake DevPi for in-memory runs: applies operator steps by feeding the
    gateway the same lines the bridge would send, and answers a hello with
    the roster then the state like the bridge does."""

    def __init__(self, sim):
        self.sim = sim
        self.state_rev = 0
        self.roster_rev = 0
        self.state_line = None
        self.roster_line = None
        sim.on_tx = self.on_line

    def _send(self, line):
        self.sim.gw.handle_line(line.encode(), self.sim.clock.now())

    def state(self, phase, horses, scratched):
        self.state_rev += 1
        self.state_line = json.dumps({"t": "state", "rev": self.state_rev, "phase": int(phase),
                                      "horse": list(horses), "scr": list(scratched)}, separators=(",", ":"))
        self._send(self.state_line)

    def roster(self, macs):
        self.roster_rev += 1
        self.roster_line = json.dumps({"t": "roster", "rev": self.roster_rev, "macs": list(macs)},
                                      separators=(",", ":"))
        self._send(self.roster_line)

    def adopt(self):
        macs = [""] * 20
        for c in self.sim.cups.values():
            if c.powered and c.cup_id is not None:
                macs[c.cup_id] = c.mac
        self.roster(macs)

    def on_line(self, line):
        if line.startswith('{"t":"hello"'):
            if self.roster_line:
                self._send(self.roster_line)
            if self.state_line:
                self._send(self.state_line)


def in_memory_run(name, seed=7, ideal=True, speed=1.0):
    sim = Simulator(link=None, seed=seed, ideal=ideal, speed=speed, quiet=True, clock=VirtualClock(),
                    out=lambda s: None, operator_timeout=30)
    sim.operator = LocalOperator(sim)
    rc = sim.run(scenario=name)
    return sim, rc


HELLO_PKT = None


# -----------------------------------------------------------------------------
# Protocol
# -----------------------------------------------------------------------------

def test_uplink_lines_match_readme():
    gw = new_gw()
    banner = gw.out
    _check("boot banner lines start with '# '", all(l.startswith("# ") for l in banner if not l.startswith("{")))
    _check("banner says DDM_AUTO_DEMO=0", any("build: DDM_AUTO_DEMO=0" in l for l in banner))
    hello = of_type(banner, "hello")
    _check("hello sent at boot", len(hello) == 1)
    _check("hello keys match README", list(hello[0]) == list(readme_example("hello")))
    _check("hello v and proto are 1", hello[0]["v"] == 1 and hello[0]["proto"] == 1)
    ex = readme_example("telem")
    line = json.loads(W.telem_line(7, "A0:B7:65:12:34:56", 812345, 14, 9021, 2, -64, -61))
    _check("telem keys match README", list(line) == list(ex))
    _check("telem example reproduced byte for byte",
           W.telem_line(7, "A0:B7:65:12:34:56", 812345, 14, 9021, 2, -64, -61)
           == '{"t":"telem","cup":7,"mac":"A0:B7:65:12:34:56","raw":812345,"count":14,"seq":9021,"drop":2,"rssi":-64,"up":-61}')
    withclaim = json.loads(W.telem_line(7, "A0:B7:65:12:34:56", 1, 1, 1, 0, -60, -60, claim=3))
    _check("telem claim is the last key, only when present", list(withclaim) == list(ex) + ["claim"])
    _check("cup_hello keys match README", list(json.loads(W.cup_hello_line(7, "A0:B7:65:12:34:56"))) == list(readme_example("cup_hello")))
    _check("status keys match README",
           list(json.loads(W.status_line(10412, 1, 42, 7, 18, 0, 5230))) == list(readme_example("status")))
    _check("status example reproduced byte for byte",
           W.status_line(10412, 1, 42, 7, 18, 0, 5230)
           == '{"t":"status","gseq":10412,"phase":1,"state_rev":42,"roster_rev":7,"cups":18,"rejects":0,"up_s":5230}')
    _check("err keys match README", list(json.loads(W.err_line("parse", b'{"t":"sta'))) == list(readme_example("err")))
    _check("err parse example reproduced byte for byte",
           W.err_line("parse", b'{"t":"sta') == '{"t":"err","msg":"parse","line":"{\\"t\\":\\"sta"}')
    _check("err overflow has no line key", W.err_line("overflow") == '{"t":"err","msg":"overflow"}')
    _check("excerpt: 40 chars, quotes and backslashes escaped, control dropped, high bytes \\u00XX",
           W.excerpt(b'ab"c\\d\x01e\xc3\xa9' + b"z" * 60) == 'ab\\"c\\\\de\\u00C3\\u00A9' + "z" * 30)
    _check("cup_to_slot / slot_to_cup", cup_to_slot(1) == 0 and cup_to_slot(20) == 19 and slot_to_cup(0) == 1 and slot_to_cup(19) == 20)
    _check("fake MACs", cup_mac(1) == "02:DD:4D:00:00:01" and cup_mac(20) == "02:DD:4D:00:00:14"
           and cup_mac(21) == "02:DD:4D:00:00:15" and cup_mac(22) == "02:DD:4D:00:00:16")
    src = open(os.path.join(_PI5_DIR, "la_quiniela", "sim", "protocol.py"), encoding="utf-8").read()
    _check("simulator protocol does not import the bridge's helpers",
           "from la_quiniela.protocol import Phase" in src and "wire_to_cup" not in src and "build_state_line" not in src)
    for name in ("model.py", "runner.py", "scenarios.py", "link.py"):
        s = open(os.path.join(_PI5_DIR, "la_quiniela", "sim", name), encoding="utf-8").read()
        _check("sim/%s imports neither the bridge, main.py nor pyserial" % name,
               "la_quiniela.bridge" not in s and "import main" not in s and "import serial" not in s
               and "la_quiniela.protocol import" not in s.replace("la_quiniela.sim.protocol", ""))


def test_downlink_validation():
    gw = new_gw()
    gw.out.clear()
    H = json.dumps(list(range(1, 21))); S = json.dumps([0] * 20)

    def send(line, now=1001.0):
        gw.out.clear()
        gw.handle_line(line if isinstance(line, bytes) else line.encode(), now)
        return list(gw.out)

    out = send('{"t":"sta')
    _check("bad JSON -> err parse with the README excerpt", out == ['{"t":"err","msg":"parse","line":"{\\"t\\":\\"sta"}'])
    cases = [
        ('{"t":"state","rev":0,"phase":1,"horse":%s,"scr":%s}' % (H, S), "rev 0"),
        ('{"t":"state","phase":1,"horse":%s,"scr":%s}' % (H, S), "rev missing"),
        ('{"t":"state","rev":true,"phase":1,"horse":%s,"scr":%s}' % (H, S), "rev bool"),
        ('{"t":"state","rev":1.0,"phase":1,"horse":%s,"scr":%s}' % (H, S), "rev float"),
        ('{"t":"state","rev":1,"phase":7,"horse":%s,"scr":%s}' % (H, S), "phase 7"),
        ('{"t":"state","rev":1,"phase":-1,"horse":%s,"scr":%s}' % (H, S), "phase -1"),
        ('{"t":"state","rev":1,"phase":1,"horse":[1,2,3],"scr":%s}' % S, "19 horses"),
        ('{"t":"state","rev":1,"phase":1,"horse":%s,"scr":%s}' % (json.dumps([21] + [1] * 19), S), "horse 21"),
        ('{"t":"state","rev":1,"phase":1,"horse":%s,"scr":%s}' % (json.dumps([-1] + [1] * 19), S), "horse -1"),
        ('{"t":"state","rev":1,"phase":1,"horse":%s,"scr":%s}' % (H, json.dumps([2] + [0] * 19)), "scr 2"),
        ('{"t":"state","rev":1,"phase":1,"horse":%s}' % H, "scr missing"),
        ('{"t":"roster","rev":1,"macs":%s}' % json.dumps([""] * 19), "19 macs"),
        ('{"t":"roster","rev":1,"macs":%s}' % json.dumps(["02:DD:4D:00:00:01"] * 2 + [""] * 18), "duplicate MAC"),
        ('{"t":"roster","rev":1,"macs":%s}' % json.dumps(["FF:FF:FF:FF:FF:FF"] + [""] * 19), "broadcast MAC"),
        ('{"t":"roster","rev":1,"macs":%s}' % json.dumps(["02:DD:4D:00:00:0G"] + [""] * 19), "bad MAC"),
        ('{"t":"roster","rev":1,"macs":%s}' % json.dumps([1] + [""] * 19), "non-string MAC"),
        ('{"t":"roster","macs":%s}' % json.dumps([""] * 20), "roster rev missing"),
        ('{"t":"debug","on":"yes"}', "debug on not boolean"),
        ('{"x":1}', "missing t"),
        ('{"t":5}', "t not a string"),
    ]
    for line, why in cases:
        out = send(line)
        _check("invalid: %s" % why, len(out) == 1 and out[0].startswith('{"t":"err","msg":"invalid","line":"'),
               str(out))
    _check("rejected lines changed nothing", gw.state_rev == 0 and gw.roster_rev == 0 and not gw.broadcasting)
    out = send('{"t":"whatever","x":[1,2]}')
    _check("unknown t ignored silently", out == [])
    out = send('{"t":"state","rev":3,"phase":2,"horse":%s,"scr":%s,"extra":{"k":1}}' % (H, S))
    _check("unknown keys ignored, state applied, status follows", gw.state_rev == 3 and gw.phase == 2
           and len(of_type(out, "status")) == 1 and of_type(out, "status")[0]["state_rev"] == 3)
    gw.out.clear()
    gw.handle_bytes(b"{" + b"x" * 1100 + b"}\n")
    _check("overflow -> exactly one err overflow, no excerpt", gw.out == ['{"t":"err","msg":"overflow"}'])
    gw.out.clear()
    gw.handle_bytes(b'{"t":"debug","on":true}\r\n')
    _check("after an overflow the next line (with CR) works", gw.debug is True and len(of_type(gw.out, "status")) == 1)
    out = send("help")
    _check("non-JSON line -> typed command reply, every line '# '", out and all(l.startswith("# ") for l in out) and "Commands" in out[0])
    out = send("bogus")
    _check("unknown command -> '# ERR'", out == ["# ERR unknown command, try: help"])
    out = send("")
    _check("empty line ignored", out == [])


def test_silent_boot_hello_status():
    gw = new_gw(now=0.0)
    hellos = lambda: len(of_type(gw.out, "hello"))
    _check("silent at boot: no state broadcast, gseq 0", not gw.broadcasting and gw.gseq == 0)
    cups = []
    for t in (0.5, 1.0, 1.5, 2.0, 2.5, 4.0, 4.5):
        gw.tick(t, cups)
    _check("hello repeats every 2 s while silent", hellos() == 3)
    _check("no status before 5 s", len(of_type(gw.out, "status")) == 0)
    gw.tick(5.0, cups)
    st = of_type(gw.out, "status")
    _check("status at 5 s with gseq 0, revs 0, up_s 5", len(st) == 1 and st[0]["gseq"] == 0 and st[0]["state_rev"] == 0
           and st[0]["roster_rev"] == 0 and st[0]["up_s"] == 5 and st[0]["phase"] == 1)
    gw.out.clear()
    gw.handle_line(('{"t":"state","rev":9,"phase":1,"horse":%s,"scr":%s}' % (json.dumps(list(range(1, 21))), json.dumps([0] * 20))).encode(), 5.2)
    st = of_type(gw.out, "status")
    _check("status immediately after the applied state", len(st) == 1 and st[0]["state_rev"] == 9)
    _check("'# [bcast]' line printed once", sum(1 for l in gw.out if l.startswith("# [bcast]")) == 1)
    gw.out.clear()
    for i in range(115):                      # 5.3 .. 11.0 in 50 ms steps, like the real loop
        gw.tick(5.3 + i * 0.05, cups)
    _check("hello stops after the first state", hellos() == 0)
    _check("gseq advances by one every 500 ms", 11 <= gw.gseq <= 12, str(gw.gseq))
    _check("status every 5 s continues", len(of_type(gw.out, "status")) == 1)
    gw.out.clear()
    gw.handle_line(('{"t":"roster","rev":2,"macs":%s}' % json.dumps([cup_mac(1)] + [""] * 19)).encode(), 11.5)
    _check("status after an applied roster", len(of_type(gw.out, "status")) == 1 and of_type(gw.out, "status")[0]["roster_rev"] == 2)
    gw.out.clear()
    gw.handle_line(b'{"t":"debug","on":true}', 11.6)
    _check("status after an applied debug", len(of_type(gw.out, "status")) == 1 and gw.debug)
    gw.out.clear()
    gw.tick(16.5, cups)
    _check("debug on: summary table printed as '# ' lines", any("---- CUPS" in l for l in gw.out) and all(l.startswith("# ") or l.startswith("{") for l in gw.out))
    gw2 = new_gw(now=0.0, auto_demo=True)
    _check("--auto-demo: broadcasting from boot, banner says DDM_AUTO_DEMO=1",
           gw2.broadcasting and gw2.demo and any("DDM_AUTO_DEMO=1" in l for l in gw2.out))
    gw2.tick(3.5, cups)
    _check("--auto-demo: horses walk and gseq advances", gw2.gseq > 0 and any(h > 0 for h in gw2.horse))


def test_roster_ownership_and_claim():
    gw = new_gw(now=0.0)
    rng = random.Random(2)
    a = CupSim(1, rng, ideal=True, stagger=0); b = CupSim(2, rng, ideal=True, stagger=0)
    x = CupSim(21, rng, ideal=True, stagger=0)
    for c in (a, b):
        c.power_on(0.0)
    gw.out.clear()
    gw.recv_hello(a, 1.0); gw.recv_hello(b, 1.1)
    ch = of_type(gw.out, "cup_hello")
    _check("before a roster: HELLOs get the next free slot in order", [c["cup"] for c in ch] == [0, 1]
           and a.cup_id == 0 and b.cup_id == 1)
    _check("NEWCUP lines printed", sum(1 for l in gw.out if l.startswith("# NEWCUP")) == 2)
    gw.out.clear()
    gw.handle_line(('{"t":"roster","rev":1,"macs":%s}' % json.dumps([b.mac, "", "", "", "", a.mac] + [""] * 14)).encode(), 2.0)
    _check("roster applied: A moved to slot 5, B to slot 0", gw.roster[5] == a.mac and gw.roster[0] == b.mac)
    gw.out.clear()
    gw.recv_telem(a, 3.0)
    t = of_type(gw.out, "telem")[0]
    _check("moved cup's next telem carries cup 5 and claim 0", t["cup"] == 5 and t["claim"] == 0)
    _check("then it adopts the new ID", a.cup_id == 5)
    gw.out.clear()
    gw.recv_telem(a, 5.0)
    _check("following telem has no claim", "claim" not in of_type(gw.out, "telem")[0])
    gw.out.clear()
    gw.emit_status(5.5)
    _check("status cups counts only roster cups heard within 3 s (A yes, B stale)", of_type(gw.out, "status")[0]["cups"] == 1)
    x.power_on(5.0)
    gw.out.clear()
    gw.recv_hello(x, 6.0)
    ch = of_type(gw.out, "cup_hello")
    _check("after a roster: unknown MAC -> cup_hello with cup -1, no slot", ch[0]["cup"] == -1 and x.cup_id is None)
    gw.out.clear()
    tick_cup(x, gw, 7.5)
    _check("unassigned cup keeps sending HELLO", len(of_type(gw.out, "cup_hello")) == 1 and x.cup_id is None)
    gw.out.clear()
    gw.recv_telem(x, 8.0)
    t = of_type(gw.out, "telem")[0]
    _check("unknown MAC telemetry: cup -1 with claim 255", t["cup"] == -1 and t["claim"] == 255)
    gw.out.clear()
    gw.emit_status(9.0)
    _check("status cups is 0 once every roster cup is stale", of_type(gw.out, "status")[0]["cups"] == 0)
    gw.reboot(20.0)
    _check("reboot: revs, gseq and up_s back to 0, roster forgotten, hello again",
           gw.state_rev == 0 and gw.roster_rev == 0 and gw.gseq == 0 and gw.up_s(20.0) == 0
           and all(m is None for m in gw.roster) and len(of_type(gw.out, "hello")) == 1)
    gw.out.clear()
    gw.recv_telem(a, 21.0)
    _check("bench mode after a reboot: telemetry from an unknown MAC re-adopts it", gw.slot_of(a.mac) == 0 and a.cup_id == 0)


def test_scale_model():
    rng = random.Random(5)
    ideal = CupSim(3, rng, ideal=True, stagger=0)
    ideal.power_on(0.0)
    ideal.drop(7, 0.0)
    _check("--ideal: count exact right after a drop", ideal.sample(0.01)[1] == 7)
    ideal.take(2, 1.0)
    _check("--ideal: take", ideal.sample(1.0)[1] == 5 and ideal.tokens == 5)
    noisy = CupSim(4, random.Random(9), ideal=False, stagger=0)
    noisy.power_on(0.0)
    noisy.drop(40, 0.0)
    worst = 0
    for i in range(3000):
        t = i * 0.5
        raw, count = noisy.sample(t)
        worst = max(worst, abs(count - 40))
    _check("noise + overshoot: count within one token over a long run", worst <= 1, str(worst))
    settled = [noisy.sample(100.0 + i)[1] for i in range(50)]
    _check("settled noisy count is exact (noise is 1/60 of a token)", all(c == 40 for c in settled))
    raw, _ = noisy.sample(200.0)
    _check("raw = tare + tokens * 6212 +- noise", abs(raw - (noisy.tare + 40 * COUNTS_PER_TOKEN)) <= 100)
    tare_before, tokens_before = noisy.tare, noisy.tokens
    noisy.power_off(); noisy.power_on(300.0)
    _check("a rebooted cup keeps tare and tokens and reports the same count",
           noisy.tare == tare_before and noisy.tokens == tokens_before and noisy.sample(301.0)[1] == 40
           and noisy.cup_id is None)
    _check("rssi wanders inside -75..-55", all(-75 <= v <= -55 for v in (noisy.rssi, noisy.up)))


def test_in_memory_scenarios():
    for name in SCENARIOS:
        sim, rc = in_memory_run(name)
        _check("scenario %s completes in memory with a fake DevPi" % name, rc == 0 and not sim.failures, "; ".join(sim.failures))
        exp = sim.expectation()
        _check("scenario %s: all 20 slots filled and online at the end" % name,
               all(e["mac"] and e["online"] for e in exp["cups"].values()))
    sim, rc = in_memory_run("scratch-rebet", seed=11)
    before = next(int(re.search(r"before the scratch: (\d+)", n).group(1)) for n in sim.notes if "before the scratch" in n)
    after = next(int(re.search(r"after the re-bet: (\d+)", n).group(1)) for n in sim.notes if "after the re-bet" in n)
    _check("scratch-rebet: cup 5 empty at the end and total tokens conserved (%d before, %d after)" % (before, after),
           sim.tokens_in(5) == 0 and before == after and before > 0 and rc == 0 and not sim.failures)
    sim, rc = in_memory_run("gateway-reboot", seed=3)
    _check("gateway-reboot: the gateway booted twice and got roster before state the second time",
           sim.gw.boots == 2 and [k for k, _ in sim.gw.applied_log][:2] == ["roster", "state"])
    sim, rc = in_memory_run("cup-swap", seed=4)
    exp = sim.expectation()
    _check("cup-swap: cup 7 is the spare, online, with the dead cup's tokens",
           exp["cups"][7]["mac"] == cup_mac(21) and exp["cups"][7]["online"] and exp["cups"][7]["count"] > 0)
    sim, rc = in_memory_run("empty-show-cup", seed=2)
    _check("empty-show-cup: cup 14 empty, finish order printed", sim.tokens_in(14) == 0 and sim.intended_finish == [3, 9, 14])
    sim, rc = in_memory_run("normal", seed=1, ideal=False)
    _check("scenario normal completes with noise on", rc == 0)
    sim = Simulator(link=None, seed=1, ideal=True, quiet=True, clock=VirtualClock(), out=lambda s: None, operator_timeout=2)
    rc = sim.run(scenario="normal")
    _check("with no operator an operator step times out with a clear failure",
           rc == 1 and any("no roster line from DevPi within 2 s" in f for f in sim.failures), "; ".join(sim.failures))


# -----------------------------------------------------------------------------
# End to end: the real bridge over a real pty
# -----------------------------------------------------------------------------

def _fresh_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except OSError:
            pass


def _e2e(name, seed=1, speed=50.0, settle=15.0):
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-sim-test-"))
    bridge = LqBridge(settings={"LQ_SERIAL_PORT": link.slave_path, "LQ_SERIAL_BAUD": 115200,
                                "LQ_HEARTBEAT_LOG_S": 10, "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12},
                      db_path=_TMP_DB)
    started = bridge.start()
    sim = Simulator(link=link, seed=seed, ideal=True, speed=speed, quiet=True, out=lambda s: None,
                    operator=ApiOperator(bridge), settle_s=settle, operator_timeout=40)
    t0 = time.time()
    rc = sim.run(scenario=name, check=bridge.get_snapshot)
    elapsed = time.time() - t0
    snap = bridge.get_snapshot()
    events = [r["type"] for r in bridge.db.query("SELECT type FROM events ORDER BY id")]
    bridge.close()
    link.close()
    return started, sim, rc, snap, events, elapsed, link, bridge


def test_e2e_normal():
    started, sim, rc, snap, events, elapsed, link, bridge = _e2e("normal")
    _check("real bridge opened the pty with pyserial, unchanged", started and bridge._factory.__name__ == "pyserial_factory")
    _check("normal: PASS against the bridge's snapshot (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    _check("normal: 20 cups online in the snapshot", sum(1 for c in snap["cups"] if c["online"]) == 20)
    _check("normal: token counts match exactly", all(c["count"] == sim.expectation()["cups"][c["cup"]]["count"] for c in snap["cups"]))
    _check("normal: bridge sent roster and state lines", bridge.stats["sent"] >= 6)
    _check("normal: phase AFTER_PARTY reached on the gateway", sim.gw.phase == int(Phase.AFTER_PARTY))
    _check("normal: no bad JSON, some '# ' text dropped", bridge.stats["bad_json"] == 0 and bridge.stats["text"] > 0)
    print("      elapsed %.1f s, bridge stats %s" % (elapsed, bridge.stats))


def test_operator_retry():
    """DevPi is not listening when the simulator starts: LQ_SIMULATOR.md tells
    Joey to start this window first. A request that DevPi does not take must be
    asked again, not end the scenario."""

    class FlakyOperator(LocalOperator):
        def __init__(self, sim, refusals):
            super().__init__(sim)
            self.refusals = refusals
            self.roster_attempts = 0

        def roster(self, macs):
            self.roster_attempts += 1
            if self.roster_attempts <= self.refusals:
                raise RuntimeError("[Errno 111] Connection refused")
            super().roster(macs)

    sim = Simulator(link=None, seed=7, ideal=True, speed=1.0, quiet=True, clock=VirtualClock(),
                    out=lambda s: None, operator_timeout=60)
    op = FlakyOperator(sim, refusals=2)
    sim.operator = op
    rc = sim.run(scenario="normal")
    _check("a refused operator request is retried, not fatal", rc == 0, "; ".join(sim.failures))
    _check("it was asked once more than it refused", op.roster_attempts == 3, str(op.roster_attempts))
    _check("the roster still ended up complete", all(sim.gw.roster[cup_to_slot(c)] == cup_mac(c) for c in range(1, 21)))


def test_e2e_short_roster_repaired():
    """DevPi keeps its roster between runs. One that is missing cups - the
    shape an adopt leaves behind when it runs before DevPi has heard from
    every cup - must not spoil the next run: the prologue names all 20 MACs,
    so the missing cups are added back."""
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    missing = {4, 5, 6, 7, 15, 16, 17}
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-sim-test-"))
    bridge = LqBridge(settings={"LQ_SERIAL_PORT": link.slave_path, "LQ_SERIAL_BAUD": 115200,
                                "LQ_HEARTBEAT_LOG_S": 10, "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12},
                      db_path=_TMP_DB)
    stale = ["" if c in missing else cup_mac(c) for c in range(1, 21)]
    rev = bridge.set_roster(stale)              # what an earlier run left behind
    bridge.start()
    sim = Simulator(link=link, seed=1, ideal=True, speed=50.0, quiet=True, out=lambda s: None,
                    operator=ApiOperator(bridge), settle_s=15.0, operator_timeout=40)
    t0 = time.time()
    rc = sim.run(scenario="normal", check=bridge.get_snapshot)
    elapsed = time.time() - t0
    snap = bridge.get_snapshot()
    bridge.close()
    link.close()
    _check("short roster: DevPi started with 13 cups", rev > 0 and len(missing) == 7)
    _check("short roster: normal still PASSes (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    _check("short roster: all 20 cups are back in the snapshot",
           all(c["mac"] == cup_mac(c["cup"]) for c in snap["cups"]),
           str([c["cup"] for c in snap["cups"] if c["mac"] != cup_mac(c["cup"])]))
    _check("short roster: the 7 missing cups are online again",
           all(c["online"] for c in snap["cups"] if c["cup"] in missing))
    _check("short roster: the gateway was told a newer roster",
           bridge.roster_rev > rev, "%s -> %s" % (rev, bridge.roster_rev))
    print("      elapsed %.1f s, roster rev %d -> %d" % (elapsed, rev, bridge.roster_rev))


def test_e2e_gateway_reboot():
    started, sim, rc, snap, events, elapsed, link, bridge = _e2e("gateway-reboot")
    _check("gateway-reboot: PASS (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    log = sim.gw.applied_log
    _check("gateway-reboot: bridge re-sent roster then state on its own", sim.gw.boots == 2 and [k for k, _ in log][:2] == ["roster", "state"], str(log))
    _check("gateway-reboot: in_sync came back true", snap["link"]["in_sync"] is True)
    _check("gateway-reboot: gateway_reboot event logged (the second hello is inside the bridge's 10 s hello-event rate limit)",
           "gateway_reboot" in events and events.count("gateway_hello") >= 1, str([e for e in events if e.startswith("gateway")]))
    print("      elapsed %.1f s" % elapsed)


def test_e2e_cup_swap():
    started, sim, rc, snap, events, elapsed, link, bridge = _e2e("cup-swap")
    _check("cup-swap: PASS (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    c7 = next(c for c in snap["cups"] if c["cup"] == 7)
    _check("cup-swap: cup 7 is the spare's MAC, online, with the tokens",
           c7["mac"] == cup_mac(21) and c7["online"] and c7["count"] == sim.expectation()["cups"][7]["count"])
    _check("cup-swap: cup_offline for cup 7 then a cup_hello for the spare were logged", "cup_offline" in events)
    print("      elapsed %.1f s" % elapsed)


def test_bridge_disconnect_reconnect():
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-sim-test-"))
    sim = Simulator(link=link, seed=2, ideal=True, quiet=True, out=lambda s: None)
    th = threading.Thread(target=lambda: sim.run(scenario=None, duration=14), daemon=True)
    th.start()
    time.sleep(2.5)
    _check("simulator runs with nobody listening", th.is_alive() and sim.gw.gseq == 0 and link.dropped_writes >= 0)
    b1 = LqBridge(settings={"LQ_SERIAL_PORT": link.slave_path, "LQ_SERIAL_BAUD": 115200,
                            "LQ_HEARTBEAT_LOG_S": 10, "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12}, db_path=_TMP_DB)
    b1.start()
    time.sleep(3)
    _check("first bridge sees lines", b1.stats["lines"] > 0 and b1.link.gateway_online)
    b1.set_state(1, list(range(1, 21)), [0] * 20)
    time.sleep(1)
    _check("state line reached the simulator", sim.gw.state_rev == 1)
    b1.close()
    time.sleep(3)
    _check("simulator still running after the bridge disconnected", th.is_alive())
    b2 = LqBridge(settings={"LQ_SERIAL_PORT": link.slave_path, "LQ_SERIAL_BAUD": 115200,
                            "LQ_HEARTBEAT_LOG_S": 10, "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12}, db_path=_TMP_DB)
    b2.start()
    time.sleep(3)
    _check("second bridge reconnects and sees lines", b2.stats["lines"] > 0 and b2.link.port_open)
    b2.close()
    sim.stop()
    th.join(5)
    link.close()
    _check("simulator stopped cleanly, symlink removed", not th.is_alive() and not os.path.exists(link.link_path))


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    print(f"La Quiniela cup simulator smoke test\n  DB: {_TMP_DB}")
    _run("uplink lines match the README", test_uplink_lines_match_readme)
    _run("downlink validation", test_downlink_validation)
    _run("silent boot, hello, status", test_silent_boot_hello_status)
    _run("roster ownership and the roster-move claim", test_roster_ownership_and_claim)
    _run("scale model", test_scale_model)
    _run("scenarios in memory (fake DevPi)", test_in_memory_scenarios)
    _run("a refused operator request is retried", test_operator_retry)
    _run("end to end — normal (real bridge over a pty)", test_e2e_normal)
    _run("end to end — DevPi remembers a short roster", test_e2e_short_roster_repaired)
    _run("end to end — gateway-reboot", test_e2e_gateway_reboot)
    _run("end to end — cup-swap", test_e2e_cup_swap)
    _run("bridge disconnect and reconnect mid-run", test_bridge_disconnect_reconnect)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    _fresh_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

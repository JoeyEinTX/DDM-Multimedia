# la_quiniela/test_simulator.py - La Quiniela cup simulator smoke test
#
# Run with: python -m la_quiniela.test_simulator  (from the pi5/ dir)
#
# Same hand-rolled runner as la_subasta/test_smoke.py. The protocol and model
# tests run in memory in an instant; the end-to-end tests start the real
# bridge in-process against the simulator over a real pty and take real
# time (the cadences never compress). The pty tests need os.openpty (Linux,
# DevPi); on a platform without it they are reported as skipped.
#
# Protocol v2: a cup is known by its MAC and the horse number it reports.
# There are no slots, rosters or cup IDs anywhere in here.

import io
import json
import logging
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

from la_quiniela import protocol as BP  # noqa: E402  (the bridge's side, for the cross-check only)
from la_quiniela.sim import protocol as W  # noqa: E402
from la_quiniela.sim.model import (  # noqa: E402
    COUNTS_PER_TOKEN, GATEWAY_MAC, SIM_MAC_PREFIX, CupSim, GatewaySim, cup_mac, tick_cup,
)
from la_quiniela.sim.protocol import Phase  # noqa: E402
from la_quiniela.sim.runner import ApiOperator, Simulator, VirtualClock  # noqa: E402
from la_quiniela.sim.scenarios import SCENARIOS  # noqa: E402

import random  # noqa: E402

HAVE_PTY = hasattr(os, "openpty")

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

def readme_text():
    with open(_README, encoding="utf-8") as f:
        return f.read()


def readme_example_line(heading: str, after: str = "") -> str:
    """The first ```json example under '#### `heading`' in the README, as
    the line itself; `after` picks the heading past that marker (the up and
    the down `state` lines share a name)."""
    text = readme_text()
    start = text.index("#### `%s`" % heading, text.index(after) if after else 0)
    return re.search(r"```json\n(.*?)\n```", text[start:], re.S).group(1)


def readme_example(heading: str, after: str = "") -> dict:
    return json.loads(readme_example_line(heading, after))


def json_lines(lines):
    return [json.loads(l) for l in lines if l.startswith("{")]


def of_type(lines, t):
    return [o for o in json_lines(lines) if o.get("t") == t]


def new_gw(now=1000.0, **kw):
    return GatewaySim(now, random.Random(1), **kw)


def state_line(rev, phase, scratched=(), renum=(), results=(0, 0, 0)):
    """A v2 down state line, built here and not by the bridge."""
    return json.dumps({"t": "state", "rev": int(rev), "st": int(phase), "scr": sorted(scratched),
                       "renum": [[int(f), int(t)] for f, t in renum], "res": list(results)},
                      separators=(",", ":"))


class LocalOperator:
    """A fake DevPi for in-memory runs. It holds the state DevPi would hold
    (phase, no-replacement scratches, replacement records, results), sends
    the gateway the same state line the bridge would, answers a hello with
    it, and sends the renumber pairs by the board's rule (betting.py,
    _renum_plan), from the cups' reports as they arrive: a record's pair
    while a cup reports its `was`, an undone record's pair back while a cup
    reports its `now` (the record's pair waiting meanwhile), the undo kept
    until no cup is on `now` and every cup on `was` has reported
    RENUM_SETTLE_S after it."""

    CUP_ONLINE_S = 6.0         # a report this recent counts as an online cup (the bridge's LQ_CUP_OFFLINE_S here)

    def __init__(self, sim):
        self.sim = sim
        self.rev = 0
        self.phase = int(Phase.PRE_RACE)
        self.scratched = set()
        self.records = {}          # was -> now (replacement scratches)
        self.undo = []             # (to, was, when) undone records being sent back, oldest first
        self.reports = {}          # mac -> (horse, when) the cups' last reports
        self.results = [0, 0, 0]
        self.sent = []
        self.pairs = []            # the pairs in the last line sent
        sim.on_tx = self.on_line

    def _on(self, horse, online_only=True):
        now = self.sim.clock.now()
        return [m for m, (h, t) in self.reports.items() if h == horse and (not online_only or now - t < self.CUP_ONLINE_S)]

    def _plan(self):
        live_u = [(to, was) for to, was, _ in self.undo if self._on(to)]
        back = {was for _, was in live_u}
        live_a = {w for w in self.records if self._on(w) and w not in back}
        grown = True
        while grown:
            grown = False
            for w in self.records:
                if w not in live_a and w not in back and any(self.records[g] == w for g in live_a):
                    live_a.add(w)
                    grown = True
        return live_u + sorted((w, n) for w, n in self.records.items() if w in live_a)

    def _send_state(self):
        self.rev += 1
        self.pairs = self._plan()
        line = state_line(self.rev, self.phase, self.scratched, self.pairs, self.results)
        self.sent.append(line)
        self.sim.gw.handle_line(line.encode(), self.sim.clock.now())

    def _resend(self):
        if self.sent:
            self.sim.gw.handle_line(self.sent[-1].encode(), self.sim.clock.now())

    # what the board's routes do
    def state(self, phase):
        self.phase = int(phase)
        self._send_state()

    def scratch(self, horse, replacement=None, name=None):
        if replacement is None:
            self.scratched.add(int(horse))
        else:
            self.records[int(horse)] = int(replacement)
        self._send_state()

    def unscratch(self, horse):
        horse = int(horse)
        if horse in self.records:
            now = self.records.pop(horse)
            self.undo = [u for u in self.undo if u[0] != now] + [(now, horse, self.sim.clock.now())]
        else:
            self.scratched.discard(horse)
        self._send_state()

    def reset(self):
        self.phase = int(Phase.PRE_RACE)
        self.results = [0, 0, 0]
        self._send_state()
        return {"race_state": 0}

    def on_line(self, line):
        if line.startswith('{"t":"hello"'):
            self._resend()                              # the bridge answers every hello with its state
        elif line.startswith('{"t":"telem"'):
            msg = json.loads(line)
            now = self.sim.clock.now()
            self.reports[msg.get("mac")] = (int(msg.get("horse") or 0), now)
            from la_quiniela.betting import RENUM_SETTLE_S
            replacements = set(self.records.values())
            self.undo = [(to, was, t) for to, was, t in self.undo
                         if to not in replacements                                # a replacement again: nothing to do
                         and (self._on(to, online_only=False)                    # a cup still on it
                              or any(self.reports[m][1] <= t + RENUM_SETTLE_S for m in self._on(was, online_only=False)))]
            if self._plan() != self.pairs:
                self._send_state()


def in_memory_run(name, seed=7, ideal=True, speed=1.0):
    sim = Simulator(link=None, seed=seed, ideal=ideal, speed=speed, quiet=True, clock=VirtualClock(),
                    out=lambda s: None, operator_timeout=30)
    sim.operator = LocalOperator(sim)
    rc = sim.run(scenario=name)
    return sim, rc


# -----------------------------------------------------------------------------
# Protocol
# -----------------------------------------------------------------------------

def test_uplink_lines_match_readme():
    gw = new_gw()
    banner = gw.out
    _check("boot banner lines start with '# '", all(l.startswith("# ") for l in banner if not l.startswith("{")))
    _check("banner says DDM_AUTO_DEMO=0", any("build: DDM_AUTO_DEMO=0" in l for l in banner))
    _check("banner names proto v2, line proto v2 and a 24-cup table", any("proto v2, line proto v2" in l and "24" in l for l in banner))
    hello = of_type(banner, "hello")
    _check("hello sent at boot", len(hello) == 1)
    _check("hello keys match README", list(hello[0]) == list(readme_example("hello")))
    _check("hello v and proto are 2", hello[0]["v"] == 2 and hello[0]["proto"] == 2)
    _check("hello example reproduced byte for byte", W.hello_line("24:6F:28:AA:BB:CC") == readme_example_line("hello"))
    ex = readme_example("telem")
    line = json.loads(W.telem_line("A0:B7:65:12:34:56", 7, 812345, 14, 9021, 2, -64, -61))
    _check("telem keys match README: mac then horse, no cup", list(line) == list(ex) and "cup" not in line)
    _check("telem example reproduced byte for byte",
           W.telem_line("A0:B7:65:12:34:56", 7, 812345, 14, 9021, 2, -64, -61) == readme_example_line("telem"))
    withhello = json.loads(W.telem_line("A0:B7:65:12:34:56", 0, 1, 1, 1, 0, -60, -60, hello=True))
    _check("telem hello is the last key, 1, only when present", list(withhello) == list(ex) + ["hello"] and withhello["hello"] == 1)
    ex_status = readme_example("status")
    cups = [W.cup_entry("A0:B7:65:12:34:56", 7, 23, -63, -61, 180), W.cup_entry("A0:B7:65:12:34:57", 0, 0, -70, -66, 900)]
    st = json.loads(W.status_line(10412, 1, 42, cups, 0, 5230))
    _check("status keys match README (no roster_rev, cups is the table)", list(st) == list(ex_status) and "roster_rev" not in st)
    _check("status cups[] entry keys match README", list(st["cups"][0]) == list(ex_status["cups"][0]))
    _check("status example reproduced byte for byte", W.status_line(10412, 1, 42, cups, 0, 5230) == readme_example_line("status"))
    _check("err keys match README", list(json.loads(W.err_line("parse", b'{"t":"sta'))) == list(readme_example("err")))
    _check("err parse example reproduced byte for byte", W.err_line("parse", b'{"t":"sta') == readme_example_line("err"))
    _check("err overflow has no line key", W.err_line("overflow") == '{"t":"err","msg":"overflow"}')
    _check("excerpt: 40 chars, quotes and backslashes escaped, control dropped, high bytes \\u00XX",
           W.excerpt(b'ab"c\\d\x01e\xc3\xa9' + b"z" * 60) == 'ab\\"c\\\\de\\u00C3\\u00A9' + "z" * 30)
    up = W.state_report_line(1234, False, "A4:F0:0F:5E:0B:08", 1, [15, 9], [(9, 22)], [0, 0, 0],
                             [W.cup_entry("20:50:0D:11:D9:AC", 7, 23, -63, -61, 180)])
    _check("up state (typed json) keys match README", list(json.loads(up)) == list(readme_example("state")))
    _check("up state example reproduced byte for byte (scr ascending)", up == readme_example_line("state"))
    down = readme_example_line("state", after="### Down:")
    kind, data = W.parse_downlink(down.encode())
    _check("the README's down state line parses: rev 42, st 1, scr {9, 15}, renum [(9, 22)], res [0, 0, 0]",
           (kind, data) == ("state", {"rev": 42, "st": 1, "scr": {9, 15}, "renum": [(9, 22)], "res": [0, 0, 0]}), str(data))
    _check("the bridge's own builder writes that README line byte for byte (the two sides agree)",
           BP.build_state_line(42, 1, [9, 15], [(9, 22)], [0, 0, 0]) == down, BP.build_state_line(42, 1, [9, 15], [(9, 22)], [0, 0, 0]))
    dbg = readme_example_line("debug")
    _check("the README's debug line parses and the bridge builds it byte for byte",
           W.parse_downlink(dbg.encode()) == ("debug", True) and BP.build_debug_line(True) == dbg)
    _check("fake MACs", cup_mac(1) == "02:DD:4D:00:00:01" and cup_mac(20) == "02:DD:4D:00:00:14"
           and cup_mac(21) == "02:DD:4D:00:00:15" and cup_mac(22) == "02:DD:4D:00:00:16"
           and all(cup_mac(n).startswith(SIM_MAC_PREFIX) for n in range(1, 23)) and GATEWAY_MAC.startswith(SIM_MAC_PREFIX))
    src = open(os.path.join(_PI5_DIR, "la_quiniela", "sim", "protocol.py"), encoding="utf-8").read()
    _check("simulator protocol does not import the bridge's helpers",
           "from la_quiniela.protocol import Phase" in src and "build_state_line" not in src and "parse_horse" not in src)
    _check("no slots, rosters or cup IDs in the simulator's protocol",
           not any(w in src for w in ("cup_to_slot", "slot_to_cup", "cup_id", "roster_line", "cup_hello_line")))
    for name in ("model.py", "runner.py", "scenarios.py", "link.py"):
        s = open(os.path.join(_PI5_DIR, "la_quiniela", "sim", name), encoding="utf-8").read()
        _check("sim/%s imports neither the bridge, main.py nor pyserial" % name,
               "la_quiniela.bridge" not in s and "import main" not in s and "import serial" not in s
               and "la_quiniela.protocol import" not in s.replace("la_quiniela.sim.protocol", ""))
        _check("sim/%s has no cup_id" % name, "cup_id" not in s)


def test_downlink_validation():
    gw = new_gw()
    gw.out.clear()

    def send(line, now=1001.0):
        gw.out.clear()
        gw.handle_line(line if isinstance(line, bytes) else line.encode(), now)
        return list(gw.out)

    out = send('{"t":"sta')
    _check("bad JSON -> err parse with the README excerpt", out == ['{"t":"err","msg":"parse","line":"{\\"t\\":\\"sta"}'])
    ok = '"st":1,"scr":[],"renum":[],"res":[0,0,0]'
    cases = [
        ('{"t":"state","rev":0,%s}' % ok, "rev 0"),
        ('{"t":"state",%s}' % ok, "rev missing"),
        ('{"t":"state","rev":true,%s}' % ok, "rev bool"),
        ('{"t":"state","rev":1.0,%s}' % ok, "rev float"),
        ('{"t":"state","rev":1,"st":7,"scr":[],"renum":[],"res":[0,0,0]}', "st 7"),
        ('{"t":"state","rev":1,"st":-1,"scr":[],"renum":[],"res":[0,0,0]}', "st -1"),
        ('{"t":"state","rev":1,"scr":[],"renum":[],"res":[0,0,0]}', "st missing"),
        ('{"t":"state","rev":1,"st":1,"renum":[],"res":[0,0,0]}', "scr missing"),
        ('{"t":"state","rev":1,"st":1,"scr":{},"renum":[],"res":[0,0,0]}', "scr not an array"),
        ('{"t":"state","rev":1,"st":1,"scr":[0],"renum":[],"res":[0,0,0]}', "scr entry 0"),
        ('{"t":"state","rev":1,"st":1,"scr":[25],"renum":[],"res":[0,0,0]}', "scr entry 25"),
        ('{"t":"state","rev":1,"st":1,"scr":[true],"renum":[],"res":[0,0,0]}', "scr entry bool"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"res":[0,0,0]}', "renum missing"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[[1,21],[2,22],[3,23],[4,24],[5,21]],"res":[0,0,0]}', "5 renum pairs"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[[9]],"res":[0,0,0]}', "renum entry not a pair"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[[0,22]],"res":[0,0,0]}', "renum from 0"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[[9,25]],"res":[0,0,0]}', "renum to 25"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[[9,9]],"res":[0,0,0]}', "renum from == to"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[[9,22],[9,23]],"res":[0,0,0]}', "renum from twice"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[]}', "res missing"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[],"res":[0,0]}', "res 2 entries"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[],"res":[0,0,0,0]}', "res 4 entries"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[],"res":[25,0,0]}', "res entry 25"),
        ('{"t":"state","rev":1,"st":1,"scr":[],"renum":[],"res":[0,-1,0]}', "res entry -1"),
        ('{"t":"debug","on":"yes"}', "debug on not boolean"),
        ('{"x":1}', "missing t"),
        ('{"t":5}', "t not a string"),
    ]
    for line, why in cases:
        out = send(line)
        _check("invalid: %s" % why, len(out) == 1 and out[0].startswith('{"t":"err","msg":"invalid","line":"'), str(out))
    _check("rejected lines changed nothing", gw.state_rev == 0 and not gw.broadcasting and not gw.scratched
           and gw.renum == [] and gw.results == [0, 0, 0])
    out = send('{"t":"whatever","x":[1,2]}')
    _check("unknown t ignored silently", out == [])
    out = send('{"t":"roster","rev":7,"macs":["02:DD:4D:00:00:01"]}')
    _check("a v1 roster line is an unknown type: ignored without a word", out == [] and gw.state_rev == 0)
    out = send('{"t":"state","rev":3,"st":2,"scr":[20],"renum":[[9,22]],"res":[0,0,0],"extra":{"k":1}}')
    _check("unknown keys ignored, state applied, status follows with the (empty) table",
           gw.state_rev == 3 and gw.phase == 2 and gw.scratched == {20} and gw.renum == [(9, 22)]
           and len(of_type(out, "status")) == 1 and of_type(out, "status")[0]["state_rev"] == 3
           and of_type(out, "status")[0]["cups"] == [], str(out))
    _check("'# [bcast]' printed once, the broadcast is on", gw.broadcasting and sum(1 for l in out if l.startswith("# [bcast]")) == 1)
    out = send('{"t":"state","rev":3,"st":2,"scr":[20],"renum":[[9,22]],"res":[0,0,0]}')
    _check("the same line again is applied again (idempotent), status again", gw.state_rev == 3 and len(of_type(out, "status")) == 1)
    gw.out.clear()
    gw.handle_bytes(b"{" + b"x" * 1100 + b"}\n")
    _check("overflow -> exactly one err overflow, no excerpt", gw.out == ['{"t":"err","msg":"overflow"}'])
    gw.out.clear()
    gw.handle_bytes(b'{"t":"debug","on":true}\r\n')
    _check("after an overflow the next line (with CR) works", gw.debug is True and len(of_type(gw.out, "status")) == 1)
    out = send("help")
    _check("non-JSON line -> typed command reply, every line '# '", out and all(l.startswith("# ") for l in out) and "Commands" in out[0])
    _check("help lists the v2 commands and none of the v1 ones",
           all(any(w in l for l in out) for w in ("scratch <horse>", "renum <from> <to>", "results <w> <p> <s>", "cups"))
           and not any("roster" in l or "horse <cup" in l for l in out), str(out))
    out = send("bogus")
    _check("unknown command -> '# ERR'", out == ["# ERR unknown command, try: help"])
    for v1 in ("horse 1 7", "roster", "scratch 1"):
        out = send(v1)
        _check("v1 command %r -> '# ERR unknown command'" % v1, out == ["# ERR unknown command, try: help"], str(out))
    out = send("")
    _check("empty line ignored", out == [])
    out = send("[1,2,3]")
    _check("a non-object JSON-looking line is a command, unknown", out == ["# ERR unknown command, try: help"])
    # the typed v2 commands
    out = send("state 7")
    _check("state 7 -> ERR", out == ["# ERR state 0-6"])
    out = send("state 4")
    _check("state 4 -> OK, phase 4", out == ["# OK state=4"] and gw.phase == 4)
    out = send("scratch 25 1")
    _check("scratch 25 -> ERR", out == ["# ERR horse 1-24"])
    out = send("scratch 20 1")
    _check("scratch 20 1 -> OK, the bit set", out == ["# OK scratch horse=20 -> 1"] and 20 in gw.scratched)
    out = send("scratch 20 0")
    _check("scratch 20 0 -> OK, the bit cleared", out == ["# OK scratch horse=20 -> 0"] and 20 not in gw.scratched)
    out = send("renum 9 9")
    _check("renum 9 9 -> ERR", out == ["# ERR to 0-24, not 9"])
    out = send("renum 9 23")
    _check("renum 9 23 replaces the pair for from 9", out == ["# OK renum 9 -> 23"] and gw.renum == [(9, 23)])
    for f, t in ((1, 21), (2, 22), (3, 24)):
        send("renum %d %d" % (f, t))
    out = send("renum 4 21")
    _check("a fifth pair -> ERR no free slot", out == ["# ERR no free renum slot (4 in use)"] and len(gw.renum) == 4)
    out = send("renum 9 0")
    _check("renum 9 0 removes the pair", out == ["# OK renum 9 removed"] and (9, 23) not in gw.renum and len(gw.renum) == 3)
    out = send("results 19 1 22")
    _check("results -> OK", out == ["# OK results win=19 place=1 show=22"] and gw.results == [19, 1, 22])
    out = send("results 0 0 25")
    _check("results 25 -> ERR", out == ["# ERR results 0-24 each"] and gw.results == [19, 1, 22])
    out = send("results 0 0 0")
    _check("results 0 0 0 clears", gw.results == [0, 0, 0])
    out = send("cups")
    _check("cups -> the table as '# CUPS' lines", out and all(l.startswith("# CUPS") for l in out))
    out = send("json")
    st = of_type(out, "state")
    _check("json -> one up state line with the packet and the table",
           len(st) == 1 and st[0]["st"] == 4 and st[0]["renum"] == [[1, 21], [2, 22], [3, 24]] and st[0]["cups"] == []
           and st[0]["mac"] == GATEWAY_MAC and st[0]["demo"] == 0, str(st))
    out = send("demo")
    _check("demo toggles on", out == ["# [demo] on"] and gw.demo)
    send('{"t":"state","rev":4,"st":1,"scr":[],"renum":[],"res":[0,0,0]}')
    _check("a state line turns demo off and replaces everything", not gw.demo and gw.state_rev == 4 and gw.renum == [] and gw.phase == 1)


def test_silent_boot_hello_status():
    gw = new_gw(now=0.0)
    hellos = lambda: len(of_type(gw.out, "hello"))  # noqa: E731
    _check("silent at boot: no state broadcast, gseq 0, PRE_RACE", not gw.broadcasting and gw.gseq == 0 and gw.phase == 0)
    cups = []
    for t in (0.5, 1.0, 1.5, 2.0, 2.5, 4.0, 4.5):
        gw.tick(t, cups)
    _check("hello repeats every 2 s while silent", hellos() == 3)
    _check("no status before 5 s", len(of_type(gw.out, "status")) == 0)
    gw.tick(5.0, cups)
    st = of_type(gw.out, "status")
    _check("status at 5 s with gseq 0, state_rev 0, an empty table, up_s 5, phase 0",
           len(st) == 1 and st[0]["gseq"] == 0 and st[0]["state_rev"] == 0 and st[0]["cups"] == []
           and st[0]["up_s"] == 5 and st[0]["phase"] == 0, str(st))
    gw.out.clear()
    gw.handle_line(state_line(9, 1).encode(), 5.2)
    st = of_type(gw.out, "status")
    _check("status immediately after the applied state", len(st) == 1 and st[0]["state_rev"] == 9 and st[0]["phase"] == 1)
    _check("'# [bcast]' line printed once", sum(1 for l in gw.out if l.startswith("# [bcast]")) == 1)
    gw.out.clear()
    for i in range(115):                      # 5.3 .. 11.0 in 50 ms steps, like the real loop
        gw.tick(5.3 + i * 0.05, cups)
    _check("hello stops after the first state", hellos() == 0)
    _check("gseq advances by one every 500 ms", 11 <= gw.gseq <= 12, str(gw.gseq))
    _check("status every 5 s continues", len(of_type(gw.out, "status")) == 1)
    gw.out.clear()
    gw.handle_line(b'{"t":"debug","on":true}', 11.6)
    _check("status after an applied debug", len(of_type(gw.out, "status")) == 1 and gw.debug)
    gw.out.clear()
    gw.tick(16.5, cups)
    _check("debug on: summary table printed as '# ' lines",
           any("---- CUPS" in l for l in gw.out) and all(l.startswith("# ") or l.startswith("{") for l in gw.out))
    gw2 = new_gw(now=0.0, auto_demo=True)
    _check("--auto-demo: broadcasting from boot, banner says DDM_AUTO_DEMO=1",
           gw2.broadcasting and gw2.demo and any("DDM_AUTO_DEMO=1" in l for l in gw2.out))
    gw2.tick(3.5, cups)
    _check("--auto-demo: WINNER with the results walking, gseq advances",
           gw2.gseq > 0 and gw2.phase == int(Phase.WINNER) and any(gw2.results) and len(set(gw2.results)) == 3,
           str((gw2.gseq, gw2.phase, gw2.results)))


def test_cup_table_and_the_cup_side():
    gw = new_gw(now=0.0)
    rng = random.Random(2)
    a = CupSim(1, rng, ideal=True, stagger=0)
    b = CupSim(2, rng, ideal=True, stagger=0)
    x = CupSim(21, rng, ideal=True, stagger=0)
    _check("cups 1..20 come out of the box set to their number; a spare to none", a.horse == 1 and b.horse == 2 and x.horse == 0)
    for c in (a, b):
        c.power_on(0.0)
    gw.out.clear()
    gw.recv_packet(a, 1.0, hello=True)
    t = of_type(gw.out, "telem")
    _check("a HELLO packet is a telem line with hello 1 and the cup's horse",
           len(t) == 1 and t[0]["mac"] == a.mac and t[0]["horse"] == 1 and t[0]["hello"] == 1 and list(t[0])[-1] == "hello", str(t))
    _check("...and the cup is in the table, marked as still saying hello", gw.cups[a.mac]["horse"] == 1 and gw.cups[a.mac]["hello"] is True)
    gw.out.clear()
    gw.recv_packet(b, 1.1, hello=False)
    t = of_type(gw.out, "telem")
    _check("a telemetry packet has no hello key", len(t) == 1 and "hello" not in t[0] and t[0]["horse"] == 2)
    gw.out.clear()
    gw.emit_status(1.5)
    st = of_type(gw.out, "status")[0]
    _check("status lists the table in the order first heard, with horse, tok and age in ms",
           [(e["mac"], e["horse"], e["tok"], e["age"]) for e in st["cups"]] == [(a.mac, 1, 0, 500), (b.mac, 2, 0, 400)], str(st["cups"]))
    x.power_on(1.0)
    gw.out.clear()
    gw.recv_packet(x, 2.0, hello=True)
    _check("a spare with no horse: telem horse 0, listed with horse 0 (nobody has set it)",
           of_type(gw.out, "telem")[0]["horse"] == 0 and gw.cups[x.mac]["horse"] == 0)
    _check("cups heard within 3 s", gw.cups_heard(2.0) == 3 and gw.cups_heard(4.5) == 1)
    x.set_horse(7, 3.0)
    _check("set_horse on a cup that has not heard the gateway brings the next HELLO forward", x.next_hello == 3.0)
    gw.out.clear()
    gw.recv_packet(x, 3.0, hello=True)
    _check("the touch menu's HORSE: the next packet says 7 and the table follows",
           of_type(gw.out, "telem")[0]["horse"] == 7 and gw.cups[x.mac]["horse"] == 7)
    b.set_horse(7)
    gw.recv_packet(b, 3.1, hello=False)
    both = [e["mac"] for e in gw.cup_entries(3.2) if e["horse"] == 7]
    _check("two cups claiming one horse are both in the table: the gateway resolves nothing", sorted(both) == sorted([b.mac, x.mac]))
    more = [CupSim(k, rng, ideal=True, stagger=0) for k in range(30, 30 + (W.MAX_CUPS - len(gw.cups)))]
    for c in more:
        c.power_on(9.0)
        gw.recv_packet(c, 10.0, hello=True)
    for c in (a, b, x):
        gw.recv_packet(c, 10.0, hello=False)      # everybody fresh
    _check("the table holds 24 cups", len(gw.cups) == W.MAX_CUPS)
    extra = CupSim(99, rng, ideal=True, stagger=0)
    extra.power_on(9.0)
    gw.out.clear()
    gw.recv_packet(extra, 10.0, hello=True)
    _check("a 25th fresh cup: '# ERR cup table full', the telem line still printed, the cup not tracked",
           any(l.startswith("# ERR cup table full") for l in gw.out) and len(of_type(gw.out, "telem")) == 1 and extra.mac not in gw.cups)
    gw.out.clear()
    gw.recv_packet(extra, 20.0, hello=True)
    _check("once an entry is stale the stalest is evicted for a new cup", extra.mac in gw.cups and a.mac not in gw.cups and len(gw.cups) == W.MAX_CUPS)
    gw.forget_stale(700.0)
    _check("a cup silent for 10 minutes leaves the table", gw.cups == {})
    gw.out.clear()
    gw.reboot(800.0)
    _check("reboot: rev, gseq and up_s back to 0, the table empty, silent, hello again",
           gw.state_rev == 0 and gw.gseq == 0 and gw.up_s(800.0) == 0 and gw.cups == {} and not gw.broadcasting
           and gw.hello_active and len(of_type(gw.out, "hello")) == 1)
    # the cup's loop: HELLO every second until a state packet, telemetry every 2 s after
    gw3 = new_gw(now=0.0)
    c = CupSim(5, rng, ideal=True, stagger=0)
    c.power_on(0.0)
    gw3.out.clear()
    for t in (0.1, 0.2, 0.7, 1.2, 1.25, 2.2):
        tick_cup(c, gw3, t)
    hellos = [l for l in of_type(gw3.out, "telem") if l.get("hello") == 1]
    _check("HELLO at 0.2, 1.2, 2.2 s while the gateway is unknown", len(hellos) == 3 and len(of_type(gw3.out, "telem")) == 3
           and not c.gateway_known)
    gw3.handle_line(state_line(1, 1).encode(), 2.3)
    gw3.tick(2.3, [c])
    _check("the first state packet makes the gateway known", c.gateway_known and c.seq == 1 and c.race_state == 1)
    gw3.out.clear()
    for t in (2.5, 2.6, 3.0, 4.6, 5.0, 6.6):
        tick_cup(c, gw3, t)
    telem = of_type(gw3.out, "telem")
    _check("then telemetry every 2 s, no hello key", len(telem) == 3 and all("hello" not in l for l in telem), str(len(telem)))
    c.set_horse(6, 7.0)
    _check("set_horse with the gateway known brings the next telemetry forward", c.next_telem == 7.0)
    # renumber pairs, the scratched bit, the results
    c.hear_state(2, 1, set(), [(5, 22)], [0, 0, 0], 8.0)
    _check("a pair whose from is not my horse leaves me alone", c.horse == 6 and c.renums == 0)
    c.hear_state(3, 1, set(), [(6, 22)], [0, 0, 0], 8.5)
    _check("a pair whose from is my horse makes me its to", c.horse == 22 and c.renums == 1)
    c.hear_state(4, 1, set(), [(6, 22)], [0, 0, 0], 9.0)
    _check("the same pair again is harmless", c.horse == 22 and c.renums == 1)
    c.hear_state(5, 1, set(), [(6, 22), (22, 23)], [0, 0, 0], 9.5)
    _check("pairs are walked in order: a chain is followed in one packet", c.horse == 23 and c.renums == 2)
    c.hear_state(6, 1, {23}, [], [0, 0, 0], 10.0)
    _check("my bit in scratched -> scratched", c.scratched is True)
    c.hear_state(7, 1, {6}, [], [0, 0, 0], 10.5)
    _check("somebody else's bit -> not scratched", c.scratched is False)
    c.hear_state(8, int(Phase.WINNER), set(), [], [23, 1, 2], 11.0)
    _check("WINNER with my horse first -> place 0 (WIN)", c.place == 0)
    c.hear_state(9, int(Phase.AFTER_PARTY), set(), [], [1, 23, 2], 11.5)
    _check("AFTER_PARTY keeps showing the results: PLACE", c.place == 1)
    c.hear_state(10, int(Phase.RUNNING), set(), [], [1, 23, 2], 12.0)
    _check("in any other state the results are not shown", c.place is None)
    c.hear_state(13, 1, set(), [], [0, 0, 0], 12.5)
    _check("a gap in the gateway's seq counts as drops", c.dropped == 2 and c.seq == 13)
    c.hear_state(1, 1, set(), [], [0, 0, 0], 13.0)
    _check("a seq that went backwards (gateway reboot) resyncs without counting drops", c.dropped == 2 and c.seq == 1)
    c.power_off()
    c.power_on(20.0)
    _check("a reboot keeps the horse (NVS) and the tokens, forgets the gateway",
           c.horse == 23 and not c.gateway_known and c.seq == 0 and c.dropped == 0)


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
    noisy.power_off()
    noisy.power_on(300.0)
    _check("a rebooted cup keeps tare, tokens and its horse and reports the same count",
           noisy.tare == tare_before and noisy.tokens == tokens_before and noisy.sample(301.0)[1] == 40
           and noisy.horse == 4 and not noisy.gateway_known)
    _check("rssi wanders inside -75..-55", all(-75 <= v <= -55 for v in (noisy.rssi, noisy.up)))


def test_in_memory_scenarios():
    for name in SCENARIOS:
        sim, rc = in_memory_run(name)
        _check("scenario %s completes in memory with a fake DevPi" % name, rc == 0 and not sim.failures, "; ".join(sim.failures))
        exp = sim.expectation()
        powered = [n for n in range(1, 21) if sim.cups[n].powered]
        _check("scenario %s: every powered cup 1..20 says its own horse and is online at the end" % name,
               all(exp["cups"][cup_mac(n)]["horse"] == n and exp["cups"][cup_mac(n)]["online"] for n in powered),
               str({n: exp["cups"][cup_mac(n)] for n in powered if exp["cups"][cup_mac(n)]["horse"] != n}))
        _check("scenario %s: the gateway ended in sync with the fake DevPi" % name,
               sim.gw.state_rev == sim.operator.rev and sim.gw.phase == sim.operator.phase, str((sim.gw.state_rev, sim.operator.rev)))
    sim, rc = in_memory_run("scratch-rebet", seed=11)
    before = next(int(re.search(r"before the scratch: (\d+)", n).group(1)) for n in sim.notes if "before the scratch" in n)
    after = next(int(re.search(r"after the re-bet: (\d+)", n).group(1)) for n in sim.notes if "after the re-bet" in n)
    _check("scratch-rebet: cup 5 empty at the end and total tokens conserved (%d before, %d after)" % (before, after),
           sim.tokens_in(5) == 0 and before == after and before > 0 and rc == 0 and not sim.failures)
    _check("scratch-rebet: horse 5's bit is in the gateway's packet and cup 5 shows its X",
           sim.gw.scratched == {5} and sim.cup(5).scratched)
    sim, rc = in_memory_run("scratch-renumber", seed=5)
    _check("scratch-renumber: cup 9 went to 22 and came back (two pairs followed), tokens kept",
           rc == 0 and sim.cup(9).horse == 9 and sim.cup(9).renums == 2 and sim.tokens_in(9) > 0, "; ".join(sim.failures))
    _check("scratch-renumber: the undo pair left the line once the cup was back on 9",
           sim.operator.undo == [] and sim.gw.renum == [], str((sim.operator.undo, sim.gw.renum)))
    for name, renums in (("undo-quick", None), ("undo-cup-off", 2), ("undo-rescratch", 4)):
        sim, rc = in_memory_run(name, seed=5)
        _check("%s: cup 9 ends on 9 with its tokens, nothing held, nothing in the line%s"
               % (name, "" if renums is None else ", %d renumbers in all (no bounce)" % renums),
               rc == 0 and not sim.failures and sim.cup(9).horse == 9 and sim.tokens_in(9) > 0
               and sim.operator.undo == [] and sim.gw.renum == []
               and (renums is None or sim.cup(9).renums == renums),
               "; ".join(sim.failures) + " " + str((sim.cup(9).horse, sim.cup(9).renums, sim.operator.undo, sim.gw.renum)))
    sim, rc = in_memory_run("gateway-reboot", seed=3)
    _check("gateway-reboot: the gateway booted twice and the state line was the first thing applied after the reboot",
           sim.gw.boots == 2 and [k for k, _ in sim.gw.applied_log][sim.reboot_log_mark:][:1] == ["state"])
    sim, rc = in_memory_run("cup-swap", seed=4)
    exp = sim.expectation()
    _check("cup-swap: the spare says horse 7, online, with the dead cup's tokens; the dead cup is offline",
           exp["cups"][cup_mac(21)]["horse"] == 7 and exp["cups"][cup_mac(21)]["online"] and exp["cups"][cup_mac(21)]["count"] > 0
           and exp["cups"][cup_mac(7)]["online"] is False, str(exp["cups"].get(cup_mac(21))))
    _check("cup-swap: expected events name the spare's cup_horse for horse 7",
           "cup_horse for horse 7" in exp["events"] and "cup_offline for horse 7" in exp["events"], str(exp["events"]))
    sim, rc = in_memory_run("empty-show-cup", seed=2)
    _check("empty-show-cup: cup 14 empty, finish order printed", sim.tokens_in(14) == 0 and sim.intended_finish == [3, 9, 14])
    sim, rc = in_memory_run("dropout-reboot", seed=6)
    _check("dropout-reboot: the expected events are by horse", sim.expectation()["events"]
           == ["cup_offline for horse 7", "cup_online for horse 7", "no cup_offline for horse 12"], str(sim.expectation()["events"]))
    sim, rc = in_memory_run("normal", seed=1, ideal=False)
    _check("scenario normal completes with noise on", rc == 0)
    sim = Simulator(link=None, seed=1, ideal=True, quiet=True, clock=VirtualClock(), out=lambda s: None, operator_timeout=2)
    rc = sim.run(scenario="normal")
    _check("with no operator an operator step times out with a clear failure",
           rc == 1 and any("no state line from DevPi within 2 s" in f for f in sim.failures), "; ".join(sim.failures))


def test_operator_retry():
    """DevPi is not listening when the simulator starts: LQ_SIMULATOR.md tells
    Joey to start this window first. A request that DevPi does not take must be
    asked again, not end the scenario."""

    class FlakyOperator(LocalOperator):
        def __init__(self, sim, refusals):
            super().__init__(sim)
            self.refusals = refusals
            self.attempts = 0

        def state(self, phase):
            self.attempts += 1
            if self.attempts <= self.refusals:
                raise RuntimeError("[Errno 111] Connection refused")
            super().state(phase)

    sim = Simulator(link=None, seed=7, ideal=True, speed=1.0, quiet=True, clock=VirtualClock(),
                    out=lambda s: None, operator_timeout=60)
    op = FlakyOperator(sim, refusals=2)
    sim.operator = op
    rc = sim.run(scenario="normal")
    _check("a refused operator request is retried, not fatal", rc == 0, "; ".join(sim.failures))
    _check("the first step was asked once more than it was refused; the other five once each", op.attempts == 8, str(op.attempts))
    _check("the party still reached AFTER_PARTY", sim.gw.phase == int(Phase.AFTER_PARTY))


def test_a_final_check_that_does_not_match_is_a_failure():
    """The end-of-run comparison with DevPi's snapshot printed its MISMATCH lines and failed the run, but left
    sim.failures empty, so a caller (the end-to-end tests) could only say FAIL. Each mismatch is a failure now."""
    sim = Simulator(link=None, seed=1, ideal=True, quiet=True, clock=VirtualClock(), out=lambda s: None, settle_s=1)
    sim.operator = LocalOperator(sim)
    rc = sim.run(scenario="normal", check=lambda: {"cups": [], "link": {"in_sync": False}})
    _check("a snapshot with no cups in it: the run fails", rc == 1 and sim.result is False)
    _check("...and says what did not match, and only that: every cup missing, the link out of sync",
           len(sim.failures) == 21 and sum("missing from the snapshot" in f for f in sim.failures) == 20
           and any("in_sync is False" in f for f in sim.failures), "; ".join(sim.failures)[:300])


def test_a_restart_marks_the_cached_cups_offline_in_the_table():
    """lq_cups keeps each cup's last state between runs. A cup that was online when pi5 stopped was left online in its
    row for good if it never reported again (a cup that no longer exists); the bridge says offline at start, in the
    snapshot and now in the rows too."""
    from la_quiniela.bridge import LqBridge
    from la_quiniela.models import LqDb
    _fresh_db()
    db = LqDb(_TMP_DB)
    db.init_schema()
    db.upsert_cup(cup_mac(1), 1, "2026-09-26T00:00:00Z", -60, -58, 3, 812345, True)
    db.upsert_cup("02:DD:4D:00:00:63", 7, "2026-09-26T00:00:00Z", -60, -58, 9, 812345, True)
    db.close()
    bridge = LqBridge(settings={"LQ_SERIAL_PORT": "", **BRIDGE_CFG}, db_path=_TMP_DB, console=_quiet_console)
    rows = {r["mac"]: r["online"] for r in bridge.db.load_cups()}
    bridge.close()
    _fresh_db()
    _check("both cached cups are offline in their rows after a start, and nothing else about them changed",
           rows == {cup_mac(1): 0, "02:DD:4D:00:00:63": 0}, str(rows))


# -----------------------------------------------------------------------------
# End to end: the real bridge over a real pty
# -----------------------------------------------------------------------------

def _quiet_console(text):
    """The bridge's [LQ] console lines are asserted on in test_smoke; here they
    would just scribble over the suite's own output."""
    return None


def _fresh_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except OSError:
            pass


BRIDGE_CFG = {"LQ_SERIAL_BAUD": 115200, "LQ_HEARTBEAT_LOG_S": 10, "LQ_CUP_OFFLINE_S": 6,
              "LQ_GATEWAY_OFFLINE_S": 12, "LQ_DEAF_REOPEN_S": 300, "LQ_REOPEN_MIN_GAP_S": 300}


def _board_for(bridge):
    from la_quiniela.betting import BettingBoard
    d = tempfile.mkdtemp(prefix="lq_sim_board_")
    return BettingBoard(bridge=bridge, log_dir=os.path.join(d, "logs"), results_path=os.path.join(d, "results.json"))


def _e2e(name, seed=1, speed=50.0, settle=15.0, lines=None):
    """The real bridge and a real betting board (its thread keeps the state
    line in step) against the simulator over a pty."""
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-sim-test-"))
    bridge = LqBridge(settings={"LQ_SERIAL_PORT": link.slave_path, **BRIDGE_CFG,
                                **({} if lines is None else {"LQ_SERIAL_LINES": lines})},
                      db_path=_TMP_DB, console=_quiet_console)
    started = bridge.start()
    board = _board_for(bridge)
    board.start()
    sim = Simulator(link=link, seed=seed, ideal=True, speed=speed, quiet=True, out=lambda s: None,
                    operator=ApiOperator(board), settle_s=settle, operator_timeout=40)
    t0 = time.time()
    rc = sim.run(scenario=name, check=bridge.get_snapshot)
    elapsed = time.time() - t0
    snap = bridge.get_snapshot()
    events = [r["type"] for r in bridge.db.query("SELECT type FROM events ORDER BY id")]
    board.stop()
    bridge.close()
    link.close()
    return started, sim, rc, snap, events, elapsed, link, bridge, board


def _by_mac(snap):
    return {c["mac"]: c for c in snap["cups"]}


def test_e2e_normal():
    started, sim, rc, snap, events, elapsed, link, bridge, board = _e2e("normal")
    _check("real bridge opened the pty with real pyserial, through its own factory",
           started and bridge._factory == bridge._default_factory)
    _check("normal: the default line handling is 'leave'", bridge.lines_mode() == "leave")
    _check("normal: PASS against the bridge's snapshot (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    _check("normal: 20 cups online in the snapshot, each saying its horse", sum(1 for c in snap["cups"] if c["online"]) == 20
           and all(_by_mac(snap)[cup_mac(n)]["horse"] == n for n in range(1, 21)))
    _check("normal: token counts match exactly, by MAC",
           all(c["count"] == sim.expectation()["cups"][c["mac"]]["count"] for c in snap["cups"]))
    _check("normal: the bridge sent the hello answer and one state line per phase", bridge.stats["sent"] >= 7, str(bridge.stats))
    _check("normal: phase AFTER_PARTY reached on the gateway", sim.gw.phase == int(Phase.AFTER_PARTY))
    _check("normal: no bad JSON, some '# ' text dropped", bridge.stats["bad_json"] == 0 and bridge.stats["text"] > 0)
    _check("normal: a clean open is left alone: no bad open, no reopen, no event of either",
           bridge.stats["bad_opens"] == 0 and bridge.stats["reopens"] == 0
           and "bridge_bad_open" not in events and "bridge_reopen" not in events, str(bridge.stats))
    _check("normal: cup_online and cup_horse events, no v1 event types",
           "cup_online" in events and "cup_horse" in events and not any(e in ("lq_reset", "roster_set", "cup_hello") for e in events))
    print("      elapsed %.1f s, bridge stats %s" % (elapsed, bridge.stats))


def test_e2e_stale_cache_corrected():
    """DevPi keeps its cup cache (lq_cups) between runs. Stale rows from an
    earlier run (a wrong horse, an extra cup) must not spoil the next one:
    every cup reports its own horse and the cache follows."""
    from la_quiniela.bridge import LqBridge
    from la_quiniela.models import LqDb
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    db = LqDb(_TMP_DB)
    db.init_schema()
    for n in range(1, 8):
        db.upsert_cup(cup_mac(n), 0, "2026-09-26T00:00:00Z", -60, -58, 3, 812345, True)     # horse forgotten, wrong count
    db.upsert_cup("02:DD:4D:00:00:63", 7, "2026-09-26T00:00:00Z", -60, -58, 9, 812345, True)  # a cup that no longer exists
    db.close()
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-sim-test-"))
    bridge = LqBridge(settings={"LQ_SERIAL_PORT": link.slave_path, **BRIDGE_CFG}, db_path=_TMP_DB, console=_quiet_console)
    _check("stale cache: the bridge loaded 8 cached cups, none online", len(bridge.get_snapshot()["cups"]) == 8
           and not any(c["online"] for c in bridge.get_snapshot()["cups"]))
    bridge.start()
    board = _board_for(bridge)
    board.start()
    sim = Simulator(link=link, seed=1, ideal=True, speed=50.0, quiet=True, out=lambda s: None,
                    operator=ApiOperator(board), settle_s=15.0, operator_timeout=40)
    t0 = time.time()
    rc = sim.run(scenario="normal", check=bridge.get_snapshot)
    elapsed = time.time() - t0
    snap = bridge.get_snapshot()
    rows = {r["mac"]: r for r in bridge.db.load_cups()}
    board.stop()
    bridge.close()
    link.close()
    _check("stale cache: normal still PASSes (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    _check("stale cache: cups 1..7 say their own horse again, online", all(_by_mac(snap)[cup_mac(n)]["horse"] == n
                                                                             and _by_mac(snap)[cup_mac(n)]["online"] for n in range(1, 8)))
    _check("stale cache: the cup that no longer exists is still listed, offline, and decides nothing",
           _by_mac(snap)["02:DD:4D:00:00:63"]["online"] is False and _by_mac(snap)[cup_mac(7)]["horse"] == 7)
    _check("stale cache: lq_cups followed", all(rows[cup_mac(n)]["horse"] == n for n in range(1, 21))
           and rows["02:DD:4D:00:00:63"]["online"] == 0)
    print("      elapsed %.1f s" % elapsed)


def test_e2e_normal_with_lines_low():
    """The other DTR/RTS mode must still work end to end on a real port."""
    started, sim, rc, snap, events, elapsed, link, bridge, board = _e2e("normal", lines="low")
    _check("lines=low: the bridge opened the pty", started)
    _check("lines=low: the mode is what was configured", bridge.lines_mode() == "low")
    _check("lines=low: normal still PASSes (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    _check("lines=low: 20 cups online in the snapshot", sum(1 for c in snap["cups"] if c["online"]) == 20)
    print("      elapsed %.1f s" % elapsed)


def test_e2e_gateway_reboot():
    started, sim, rc, snap, events, elapsed, link, bridge, board = _e2e("gateway-reboot")
    _check("gateway-reboot: PASS (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    log = sim.gw.applied_log
    _check("gateway-reboot: the bridge re-sent the state on its own, first thing after the reboot",
           sim.gw.boots == 2 and [k for k, _ in log][sim.reboot_log_mark:][:1] == ["state"], str(log))
    _check("gateway-reboot: in_sync came back true", snap["link"]["in_sync"] is True)
    _check("gateway-reboot: gateway_reboot event logged (the second hello is inside the bridge's 10 s hello-event rate limit)",
           "gateway_reboot" in events and events.count("gateway_hello") >= 1, str([e for e in events if e.startswith("gateway")]))
    print("      elapsed %.1f s" % elapsed)


def test_e2e_cup_swap():
    started, sim, rc, snap, events, elapsed, link, bridge, board = _e2e("cup-swap")
    _check("cup-swap: PASS (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    spare, dead = _by_mac(snap)[cup_mac(21)], _by_mac(snap)[cup_mac(7)]
    _check("cup-swap: the spare says horse 7, online, with the tokens; the dead cup is offline, still saying 7",
           spare["horse"] == 7 and spare["online"] and spare["count"] == sim.expectation()["cups"][cup_mac(21)]["count"]
           and dead["online"] is False and dead["horse"] == 7, str((spare, dead)))
    _check("cup-swap: cup_offline, then the spare's cup_online and cup_horse were logged",
           "cup_offline" in events and events.index("cup_offline") < len(events) - 1 and "cup_horse" in events)
    print("      elapsed %.1f s" % elapsed)


def test_e2e_scratch_renumber():
    started, sim, rc, snap, events, elapsed, link, bridge, board = _e2e("scratch-renumber")
    _check("scratch-renumber: PASS (%.0f s)" % elapsed, rc == 0, "; ".join(sim.failures))
    c9 = _by_mac(snap)[cup_mac(9)]
    _check("scratch-renumber: cup 9 is horse 9 again in the snapshot, online, tokens kept",
           c9["horse"] == 9 and c9["online"] and c9["count"] == sim.tokens_in(9), str(c9))
    _check("scratch-renumber: the record is gone and the undo pair left the state line once the cup reported 9",
           board.store.scratches() == {} and bridge.renum == [] and board.undo_renums() == [], str((bridge.renum, board.undo_renums())))
    _check("scratch-renumber: the name the scratch gave horse 22 went with the Undo (b59382a: Undo puts the names back as they were)",
           not board.store.horses().get(22, {}).get("name"), str(board.store.horses().get(22)))
    _check("scratch-renumber: cup_horse events for 22 and back to 9, no cup_offline",
           events.count("cup_horse") >= 22 and "cup_offline" not in events, str(events.count("cup_horse")))
    print("      elapsed %.1f s" % elapsed)


def test_e2e_undo_brings_the_cup_back():
    """Gap review M7 / B11 against the real bridge and board: an Undo 0.3 s to 2.5 s after a replacement scratch
    (the cup's last report still the old number), with the cup off for five minutes, and an undo followed within a
    second by another scratch: the cup always ends where the records say, nothing left in the line, and the board
    logged each cup that followed a pair (renum_ack)."""
    for name in ("undo-quick", "undo-cup-off", "undo-rescratch"):
        started, sim, rc, snap, events, elapsed, link, bridge, board = _e2e(name)
        c9 = _by_mac(snap)[cup_mac(9)]
        _check("%s: PASS (%.0f s)" % (name, elapsed), rc == 0, "; ".join(sim.failures))
        _check("%s: cup 9 is horse 9 in the snapshot, online, tokens kept" % name,
               c9["horse"] == 9 and c9["online"] and c9["count"] == sim.tokens_in(9), str(c9))
        _check("%s: no record, nothing left to undo, no pair in the line" % name,
               board.store.scratches() == {} and board.undo_renums() == [] and bridge.renum == [],
               str((board.store.scratches(), board.undo_renums(), bridge.renum)))
        _check("%s: the board recorded each cup that followed a pair (renum_ack)" % name, "renum_ack" in events, str(set(events)))


def test_e2e_real_gateway_after_a_simulator_run():
    """A simulator session leaves twenty pretend cups in DevPi's cache. When
    the real gateway is plugged back in and DevPi restarts, its hello must
    drop them, be answered with the state, and the real cups must show up
    by their own numbers."""
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()

    def wait_until(pred, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    # 1. A full simulator run, which leaves twenty fake MACs in the cache.
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-sim-test-"))
    bridge = LqBridge(settings=dict(BRIDGE_CFG, LQ_SERIAL_PORT=link.slave_path), db_path=_TMP_DB, console=_quiet_console)
    bridge.start()
    board = _board_for(bridge)
    board.start()
    sim = Simulator(link=link, seed=1, ideal=True, speed=50.0, quiet=True, out=lambda s: None,
                    operator=ApiOperator(board), settle_s=15.0, operator_timeout=40)
    rc = sim.run(scenario="normal", check=bridge.get_snapshot)
    sim_rev = bridge.state_rev
    board.stop()
    bridge.close()
    link.close()
    _check("simulator run finished first", rc == 0, "; ".join(sim.failures))

    # 2. DevPi restarts with the real gateway plugged in.
    link2 = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-real-test-"))
    bridge2 = LqBridge(settings=dict(BRIDGE_CFG, LQ_SERIAL_PORT=link2.slave_path), db_path=_TMP_DB, console=_quiet_console)
    _check("the restarted bridge loaded the simulator's 20 cups from the cache, offline",
           sum(1 for c in bridge2.get_snapshot()["cups"] if c["mac"].startswith(SIM_MAC_PREFIX)) == 20
           and not any(c["online"] for c in bridge2.get_snapshot()["cups"]))
    _check("...and its state (the phase the run ended in)", bridge2.state_rev == sim_rev and bridge2.phase == int(Phase.AFTER_PARTY))
    bridge2.start()
    real_gw = "24:6F:28:AA:BB:CC"
    real_cups = ["24:6F:28:11:22:%02X" % (n + 1) for n in range(4)]
    wait_until(lambda: bridge2.link.port_open)
    link2.drain()

    # A real port hands over the tail of whatever was in flight when it was
    # opened. The bridge must drop that and resynchronise on the newline.
    link2.write_line('5:12:34:56","count":3,"seq":11}')
    link2.write_line(W.hello_line(real_gw))
    gone = wait_until(lambda: not any(c["mac"].startswith(SIM_MAC_PREFIX) for c in bridge2.get_snapshot()["cups"]))
    _check("the real gateway's hello dropped the pretend cups from the cache", gone)
    answered = wait_until(lambda: len(link2.drain_peek()) >= 1) if hasattr(link2, "drain_peek") else True
    time.sleep(0.5)
    got = link2.drain()
    _check("the hello was answered with exactly one line: the state, at DevPi's rev",
           answered and len(got) == 1 and json.loads(got[0]) == json.loads(BP.build_state_line(
               bridge2.state_rev, bridge2.phase, bridge2.scratched, bridge2.renum, bridge2.results)), str(got))
    events = [r["type"] for r in bridge2.db.query("SELECT type FROM events ORDER BY id")]
    _check("a cups_forgotten event was logged, no lq_reset", events.count("cups_forgotten") == 1 and "lq_reset" not in events, str(events[-5:]))
    _check("the rev did not move: a hello changes nothing DevPi holds", bridge2.state_rev == sim_rev)

    # 3. Four real cups report, each saying its own horse.
    for i, mac in enumerate(real_cups):
        link2.write_line(W.telem_line(mac, i + 1, 810000 + i, 0, 1, 0, -60, -58))
    ok = wait_until(lambda: all(mac in _by_mac(bridge2.get_snapshot()) for mac in real_cups))
    snap = bridge2.get_snapshot()
    sim_rows = bridge2.db.query_one("SELECT COUNT(*) AS n FROM lq_cups WHERE mac LIKE '02:DD:4D:%'")["n"]
    bridge2.close()
    link2.close()
    _check("all four real cups are in the snapshot", ok, str([(c["mac"], c["horse"]) for c in snap["cups"]]))
    _check("each with the horse it said, online", all(_by_mac(snap)[mac]["horse"] == i + 1 and _by_mac(snap)[mac]["online"]
                                                      for i, mac in enumerate(real_cups)), str(snap["cups"]))
    _check("no simulated cup is left anywhere in the snapshot", not any(c["mac"].startswith(SIM_MAC_PREFIX) for c in snap["cups"]))
    _check("the simulated cup rows are gone from lq_cups", sim_rows == 0, str(sim_rows))


def test_e2e_newline_free_torrent_over_a_real_port():
    """The bench failure, with real pyserial on a real port. A talker that
    never sends a newline used to block the reader thread outright: port open,
    gateway never online, timers starved, and only an app restart to cure it.
    The reader must stay responsive and the watchdog must reopen the port."""
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    link = PtyLink(link_path=tempfile.mktemp(prefix="ddm-lq-junk-"))
    cfg = dict(BRIDGE_CFG, LQ_SERIAL_PORT=link.slave_path, LQ_DEAF_REOPEN_S=3, LQ_REOPEN_MIN_GAP_S=2)
    bridge = LqBridge(settings=cfg, db_path=_TMP_DB, console=_quiet_console)

    def push(data, budget=5.0):
        sent, end = 0, time.time() + budget
        while sent < len(data) and time.time() < end:
            try:
                sent += os.write(link.master, data[sent:])
            except BlockingIOError:
                time.sleep(0.005)
            except OSError:
                break
        return sent

    def wait(pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    try:
        started = bridge.start()
        _check("torrent: the real bridge opened the real port", started and wait(lambda: bridge.link.port_open))
        # No newline anywhere in it, and it never stops.
        torrent = bytes(b for b in range(1, 256) if b != 0x0A) * 8      # ~2 kB, no 0x0A
        pushed = 0
        end = time.time() + 6
        while time.time() < end:
            pushed += push(torrent, budget=0.3)
        _check("torrent: a lot of newline-free bytes went in", pushed > 20000, str(pushed))
        _check("torrent: the reader thread is still alive", bridge.running)
        _check("torrent: caught as a bad open first (bytes the gateway never sends, no valid line, 3 s)",
               bridge.stats["bad_opens"] >= 1
               and len(bridge.db.query("SELECT id FROM events WHERE type = 'bridge_bad_open'")) >= 1,
               str(bridge.stats))
        snap = bridge.get_snapshot()["link"]
        _check("torrent: the bytes were read, not blocked on", snap["bytes_rx"] > 20000, str(snap))
        _check("torrent: nothing was parsed from it", snap["lines_ok"] == 0, str(snap))
        _check("torrent: the gateway is not claimed online", snap["gateway_online"] is False)
        _check("torrent: reason does not say online", snap["reason"] != "online", snap["reason"])
        _check("torrent: memory stayed bounded", len(bridge._pending) <= 4 * 1024, str(len(bridge._pending)))
        _check("torrent: the watchdog noticed and reopened", wait(lambda: bridge.stats["reopens"] >= 1, 10),
               str(bridge.stats))
        _check("torrent: a bridge_reopen event was written",
               len(bridge.db.query("SELECT id FROM events WHERE type = 'bridge_reopen'")) >= 1)
        # Now talk properly, as the gateway does: a hello every 2 s until it is
        # answered. The link must come up without restarting anything. (A
        # single push can land inside a reopen's settle and be dropped with
        # the junk, as it would be on the wire.)
        came_back, end = False, time.time() + 20
        while not came_back and time.time() < end:
            push(b"\n" + W.hello_line("24:6F:28:AA:BB:CC").encode() + b"\n")
            push(W.status_line(1, 1, 0, [], 0, 145).encode() + b"\n")
            came_back = wait(lambda: bridge.link.gateway_online and bridge.link.up_s == 145, 2)
        _check("torrent: the link recovers on its own, no restart", came_back,
               str(bridge.get_snapshot()["link"]))
        _check("torrent: thread_alive is true throughout",
               bridge.get_snapshot()["link"]["thread_alive"] is True)
    finally:
        bridge.close()
        link.close()


# What a UART at 460800 makes of the gateway's 115200 hello (test_smoke has
# the same bytes): runs of one bit value, no newline, no JSON. 2 KB of it.
NOISE = bytes.fromhex("f8 f8 78 00 80 00 f8 78 00 80 80 f8 80 80 00 80"
                      " 00 f8 78 78 78 f8 78 f8 78 f8 78 00 80 00 78 80") * 64


class _BridgeLog(logging.Handler):
    """The bridge's WARNING lines, what the journal gets, counted instead of printed."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def matching(self, needle):
        return [l for l in self.lines if needle in l]

    def __enter__(self):
        logging.getLogger("la_quiniela.bridge").addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("la_quiniela.bridge").removeHandler(self)
        return False


def _wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _noise_while(link, keep_going, per_s=8192):
    """Noise into the port from the gateway's side, about per_s bytes a
    second, for as long as keep_going() says."""
    def run():
        while keep_going():
            try:
                os.write(link.master, NOISE)
            except BlockingIOError:
                pass
            except OSError:
                return
            time.sleep(len(NOISE) / per_s)
    th = threading.Thread(target=run, name="lq-test-noise", daemon=True)
    th.start()
    return th


def _gateway_on(link, seconds):
    """The simulator's gateway (and its cups) coming onto the line now: it
    boots and says hello at once, as the firmware does."""
    sim = Simulator(link=link, seed=4, ideal=True, quiet=True, out=lambda s: None)
    th = threading.Thread(target=lambda: sim.run(scenario=None, duration=seconds), name="lq-test-sim",
                          daemon=True)
    th.start()
    return sim, th


def _bad_open_bridge(prefix, **cfg):
    from la_quiniela.bridge import LqBridge
    from la_quiniela.sim.link import PtyLink
    _fresh_db()
    link = PtyLink(link_path=tempfile.mktemp(prefix=prefix))
    bridge = LqBridge(settings=dict(BRIDGE_CFG, LQ_SERIAL_PORT=link.slave_path, **cfg), db_path=_TMP_DB,
                      console=_quiet_console)
    return link, bridge


def _events(bridge, type_):
    return [json.loads(r["detail"]) for r in
            bridge.db.query("SELECT detail FROM events WHERE type = ? ORDER BY id", (type_,))]


def test_e2e_bad_first_open():
    """DevPi's cold boots over a real port, with real pyserial: the first open
    gets noise (the gateway is not on the line yet); the moment the bridge has
    given that open up and opened again, the simulator's gateway is there and
    says hello, which is what a clean reopen finds. The link must be up a few
    seconds after the first open (3 s, the next tick, the reopen), not 20."""
    from la_quiniela.bridge import BAD_OPEN_S
    link, bridge = _bad_open_bridge("ddm-lq-badopen-", LQ_DEAF_REOPEN_S=20, LQ_REOPEN_MIN_GAP_S=30)
    sim = th = None
    try:
        with _BridgeLog() as log:
            bridge.start()
            _check("bad first open: the real bridge opened the real port", _wait_for(lambda: bridge.link.port_open, 10))
            first_open = bridge._open_mono
            _noise_while(link, lambda: bridge.stats["bad_opens"] == 0 and time.monotonic() - first_open < 10)
            _check("bad first open: given up as a bad open",
                   _wait_for(lambda: bridge.stats["bad_opens"] >= 1, 10), str(bridge.stats))
            _wait_for(lambda: bridge._open_mono != first_open and bridge.link.port_open, 5)
            sim, th = _gateway_on(link, 12)
            up = _wait_for(lambda: bridge.stats["lines"] > 0 and bridge.link.gateway_online, 10)
            took = bridge.link.last_good_line_mono - first_open if up else None
            _check("bad first open: the link is up %.1f s after the first open" % (took or -1),
                   up and took <= BAD_OPEN_S + 2, str(bridge.get_snapshot()["link"]))
            _check("bad first open: the hello was answered: the gateway holds rev 1",
                   _wait_for(lambda: sim.gw.state_rev == 1, 5), str(sim.gw.state_rev))
            bad = _events(bridge, "bridge_bad_open")
            _check("bad first open: one bridge_bad_open event, with the first 32 bytes in hex",
                   len(bad) == 1 and bad[0]["hex"] == NOISE[:32].hex(" ") and bad[0]["bytes"] > 0
                   and bad[0]["quick"] == 1, str(bad))
            _check("bad first open: one log line", len(log.lines) == 1 and "bad open" in log.lines[0],
                   str(log.lines))
            _check("bad first open: the watchdog never fired",
                   bridge.stats["reopens"] == 0 and not _events(bridge, "bridge_reopen"))
            print("      link up %.1f s after the first open; the event: %s" % (took or -1, bad[:1]))
    finally:
        if sim is not None:
            sim.stop()
            th.join(5)
        bridge.close()
        link.close()


def test_e2e_silent_first_open():
    """A port that says nothing is the watchdog's, as before (its 20 s scaled
    down to 4 s here): it reopens, the gateway comes on, the link comes up,
    and nothing about a bad open is logged or recorded."""
    link, bridge = _bad_open_bridge("ddm-lq-silent-", LQ_DEAF_REOPEN_S=4, LQ_REOPEN_MIN_GAP_S=4)
    sim = th = None
    try:
        with _BridgeLog() as log:
            bridge.start()
            _check("silent first open: the port opened", _wait_for(lambda: bridge.link.port_open, 10))
            _check("silent first open: the watchdog reopened it",
                   _wait_for(lambda: bridge.stats["reopens"] >= 1, 10), str(bridge.stats))
            sim, th = _gateway_on(link, 10)
            _check("silent first open: the link comes up once the gateway talks",
                   _wait_for(lambda: bridge.stats["lines"] > 0 and bridge.link.gateway_online, 10))
            reopen = _events(bridge, "bridge_reopen")
            _check("silent first open: a bridge_reopen event with 0 bytes, as before",
                   len(reopen) >= 1 and reopen[0]["bytes_rx"] == 0, str(reopen))
            _check("silent first open: no bad open, no bridge_bad_open event",
                   bridge.stats["bad_opens"] == 0 and not _events(bridge, "bridge_bad_open"))
            _check("silent first open: only the watchdog's log lines",
                   log.lines and all("no valid line for" in l for l in log.lines), str(log.lines))
    finally:
        if sim is not None:
            sim.stop()
            th.join(5)
        bridge.close()
        link.close()


def test_e2e_noise_forever():
    """Noise on every open for 22 s (the watchdog's 20 s scaled down to 4 s):
    three quick reopens, then the watchdog alone, and one log line each."""
    from la_quiniela.bridge import BAD_OPEN_QUICK_MAX
    link, bridge = _bad_open_bridge("ddm-lq-noise-", LQ_DEAF_REOPEN_S=4, LQ_REOPEN_MIN_GAP_S=4)
    try:
        with _BridgeLog() as log:
            bridge.start()
            _check("noise forever: the port opened", _wait_for(lambda: bridge.link.port_open, 10))
            t0 = time.time()
            noise = _noise_while(link, lambda: time.time() - t0 < 22)
            noise.join(30)
            bridge.stop()                        # so the counts and the log lines agree
            stats = dict(bridge.stats)
            _check("noise forever: %d quick reopens, then the watchdog's (%d)" % (BAD_OPEN_QUICK_MAX, stats["reopens"]),
                   stats["bad_opens"] == BAD_OPEN_QUICK_MAX and stats["reopens"] >= 1, str(stats))
            _check("noise forever: the bytes were read (%d)" % stats["bytes_rx"], stats["bytes_rx"] > 50000)
            bad = _events(bridge, "bridge_bad_open")
            _check("noise forever: bridge_bad_open events quick 1, 2, 3",
                   [d["quick"] for d in bad] == [1, 2, 3], str(bad))
            _check("noise forever: one log line per reopen and one when the quick ones are spent (%d)" % len(log.lines),
                   len(log.matching("bad open")) == BAD_OPEN_QUICK_MAX
                   and len(log.matching("quick reopens")) == 1
                   and len(log.matching("no valid line for")) == stats["reopens"]
                   and len(log.lines) == BAD_OPEN_QUICK_MAX + 1 + stats["reopens"], "\n".join(log.lines))
            _check("noise forever: no valid line, the gateway not claimed online",
                   stats["lines"] == 0 and not bridge.link.gateway_online)
    finally:
        bridge.close()
        link.close()


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
    b1 = LqBridge(settings=dict(BRIDGE_CFG, LQ_SERIAL_PORT=link.slave_path), db_path=_TMP_DB, console=_quiet_console)
    b1.start()
    time.sleep(3)
    _check("first bridge sees lines and the gateway", b1.stats["lines"] > 0 and b1.link.gateway_online)
    _check("its hello was answered with rev 1, so the gateway broadcasts and the cups have found it",
           sim.gw.state_rev == 1 and sim.gw.broadcasting and sum(1 for c in sim.cups.values() if c.gateway_known) > 0)
    b1.set_state(phase=1)
    time.sleep(1)
    _check("the state line reached the simulator: rev 2, BETTING_OPEN", sim.gw.state_rev == 2 and sim.gw.phase == 1)
    b1.close()
    time.sleep(3)
    _check("simulator still running after the bridge disconnected", th.is_alive())
    b2 = LqBridge(settings=dict(BRIDGE_CFG, LQ_SERIAL_PORT=link.slave_path), db_path=_TMP_DB, console=_quiet_console)
    b2.start()
    time.sleep(3)
    _check("second bridge reconnects and sees lines", b2.stats["lines"] > 0 and b2.link.port_open)
    _check("...and restored rev 2 from the database", b2.state_rev == 2 and b2.phase == 1)
    b2.close()
    sim.stop()
    th.join(5)
    link.close()
    _check("simulator stopped cleanly, symlink removed", not th.is_alive() and not os.path.exists(link.link_path))


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

E2E = [
    ("end to end — normal (real bridge over a pty)", test_e2e_normal),
    ("end to end — a stale cup cache is corrected", test_e2e_stale_cache_corrected),
    ("end to end — normal with lines=low", test_e2e_normal_with_lines_low),
    ("end to end — gateway-reboot", test_e2e_gateway_reboot),
    ("end to end — cup-swap", test_e2e_cup_swap),
    ("end to end — scratch-renumber", test_e2e_scratch_renumber),
    ("end to end — undo-quick, undo-cup-off, undo-rescratch (M7, B11)", test_e2e_undo_brings_the_cup_back),
    ("end to end — a real gateway after a simulator run", test_e2e_real_gateway_after_a_simulator_run),
    ("end to end — a newline-free torrent on a real port", test_e2e_newline_free_torrent_over_a_real_port),
    ("end to end — noise on the first open, a clean reopen", test_e2e_bad_first_open),
    ("end to end — a silent first open is the watchdog's", test_e2e_silent_first_open),
    ("end to end — noise on every open", test_e2e_noise_forever),
    ("bridge disconnect and reconnect mid-run", test_bridge_disconnect_reconnect),
]


def main():
    print(f"La Quiniela cup simulator smoke test\n  DB: {_TMP_DB}")
    _run("uplink lines match the README", test_uplink_lines_match_readme)
    _run("downlink validation and the typed commands", test_downlink_validation)
    _run("silent boot, hello, status", test_silent_boot_hello_status)
    _run("the cup table and the cup side (HELLO, telemetry, renum, scratched, results)", test_cup_table_and_the_cup_side)
    _run("scale model", test_scale_model)
    _run("scenarios in memory (fake DevPi)", test_in_memory_scenarios)
    _run("a refused operator request is retried", test_operator_retry)
    _run("a final check that does not match is a failure", test_a_final_check_that_does_not_match_is_a_failure)
    _run("a restart marks the cached cups offline in the table", test_a_restart_marks_the_cached_cups_offline_in_the_table)
    if HAVE_PTY:
        for name, fn in E2E:
            _run(name, fn)
    else:
        print("\n%d end-to-end tests SKIPPED: no os.openpty on this platform (they run on DevPi)" % len(E2E))

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total"
          + ("" if HAVE_PTY else f" ({len(E2E)} end-to-end tests skipped: no pty)"))
    print("=" * 50)
    _fresh_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

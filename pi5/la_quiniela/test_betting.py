# la_quiniela/test_betting.py - La Quiniela betting board tests
#
# Run with: python -m la_quiniela.test_betting  (from the pi5/ dir)
#
# No hardware, no real serial port, no network. The model is fed hand-built
# get_snapshot() dicts and, for a few tests, a real LqBridge over
# test_smoke's FakeSerial; the routes run on a Flask test app. Same tiny
# runner as test_smoke.py, whose fakes and line builders are imported
# (importing it is safe: it only runs under __main__, and it repoints
# la_subasta.config.DB_PATH at a temp file, which is what we want).

import datetime
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass

_PI5_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PI5_DIR)

from la_quiniela import test_smoke as S  # noqa: E402  (repoints the DB before the bridge loads)
from la_quiniela import betting  # noqa: E402
from la_quiniela import board as board_mod  # noqa: E402
from la_quiniela import protocol as P  # noqa: E402
from la_quiniela.betting import (  # noqa: E402
    DEFAULTS, MAX_EVENTS, SSE_QUEUE_SIZE, BettingBoard, load_board_settings, prizes_for,
    round_half_up, sse_events, validate_cmd,
)
from la_quiniela.blueprint import init_la_quiniela, la_quiniela_bp  # noqa: E402
from la_quiniela.board import (  # noqa: E402
    DEMO_REFUSED, JSON_REFUSED, USAGE_CLOSES_AT, USAGE_HORSE, USAGE_SCRATCH, USAGE_STATE, get_board,
    init_board, quiniela_board_bp, start_board, stop_board,
)
from la_quiniela.horses import HorseStore, parse_names_text  # noqa: E402
from la_quiniela.models import LqDb  # noqa: E402
from la_quiniela.test_smoke import (  # noqa: E402
    HORSES_1_TO_20, MAC_A, MAC_B, NO_SCR, SCR_CUP7, FakeClock, _fresh_bridge, drain, status, telem,
)

MODEL_KEYS = {"link_ok", "race_state", "race_state_name", "token_value", "pot", "total_tokens",
              "horses", "leader", "events", "updated", "board_states",
              # additive since the payout model: see "Betting board" in LQ_BRIDGE.md
              "now", "closes_at", "prizes", "split", "chyron", "names_rev", "scratches"}
LOGGER = "la_quiniela.betting"
UNASSIGNED = {"tokens": 0, "share": 0, "scratched": False, "online": False, "cup": None,
              "name": "", "replaced": None}
SPLIT = {"win": 0.60, "place": 0.25, "show": 0.15}


# -----------------------------------------------------------------------------
# Tiny test runner (no pytest dependency)
# -----------------------------------------------------------------------------

_results = []


def _check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    _results.append((status_, name, detail))
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

class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, needle=None):
        return [r.getMessage() for r in self.records
                if needle is None or needle in r.getMessage()]


class capture_logs:
    """with capture_logs("la_quiniela.betting") as cap: ... cap.messages("x")"""

    def __init__(self, name, level=logging.WARNING):
        self.logger = logging.getLogger(name)
        self.level = level
        self.handler = LogCapture()

    def __enter__(self):
        self._old_level = self.logger.level
        self.logger.setLevel(self.level)
        self.logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._old_level)


_tmpdirs = []


def tmpdir():
    d = tempfile.mkdtemp(prefix="lq_board_")
    _tmpdirs.append(d)
    return Path(d)


def fresh_board(bridge=None, **settings):
    """A board with injected clocks and a temp log dir. Returns (board, wall, log_dir)."""
    wall = FakeClock(1_700_000_000.0)
    log_dir = tmpdir() / "logs"
    b = BettingBoard(bridge=bridge, settings=settings or None, clock=FakeClock(1000.0),
                     wall=wall, log_dir=log_dir)
    return b, wall, log_dir


def cup_entry(cup, horse=None, count=None, scratched=False, online=False, mac=None, **extra):
    """One get_snapshot()["cups"] entry, 1-based cup, the bridge's shape."""
    if mac is None and horse:
        mac = "A0:B7:65:00:00:%02X" % cup
    d = {"cup": cup, "mac": mac, "horse": horse, "scratched": scratched, "count": count,
         "raw": None, "rssi": None, "up": None, "drop": None, "online": online, "last_seen": None}
    d.update(extra)
    return d


def snap(phase=1, cups=(), port_open=True, gateway_online=True, has_state=True, state_rev=1,
         roster_rev=0, has_roster=False, fill=True):
    """A get_snapshot() dict. With fill (the default) the 20 cup slots are
    always present, the given entries dropped in by cup number; fill=False
    passes the cups list through untouched, for the odd-input tests."""
    if fill:
        by_cup = {c["cup"]: c for c in cups if isinstance(c, dict) and c.get("cup") in P.CUP_NUMBERS}
        cups_list = [by_cup.get(n, cup_entry(n)) for n in P.CUP_NUMBERS]
    else:
        cups_list = list(cups)
    return {
        "link": {"port_open": port_open, "gateway_online": gateway_online,
                 "in_sync": gateway_online, "reason": "status", "gateway_mac": None,
                 "phase": phase, "state_rev": state_rev, "roster_rev": roster_rev,
                 "cups_heard": 0, "rejects": 0, "up_s": 10, "thread_alive": True,
                 "last_line_age_s": 0.1, "lines_ok": 1, "lines_bad": 0, "bytes_rx": 1,
                 "reopens": 0},
        "devpi": {"state_rev": state_rev, "roster_rev": roster_rev, "phase": phase,
                  "has_state": has_state, "has_roster": has_roster},
        "cups": cups_list,
        "unassigned": [],
    }


def log_lines(log_dir):
    files = list(Path(log_dir).glob("quiniela_*.jsonl")) if Path(log_dir).exists() else []
    if not files:
        return None, []
    text = files[0].read_text(encoding="utf-8")
    return files[0], [json.loads(line) for line in text.splitlines() if line]


def _make_board_app(bridge, **board_kwargs):
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_la_quiniela(socketio=None, bridge=bridge)
    board_kwargs.setdefault("log_dir", tmpdir() / "logs")
    board_kwargs.setdefault("wall", FakeClock(1_700_000_000.0))
    init_board(bridge=bridge, **board_kwargs)
    app.register_blueprint(la_quiniela_bp)
    app.register_blueprint(quiniela_board_bp)
    return app


def state_line(rev, phase, horses, scratched):
    """What the bridge must have written for set_state(phase, horses, scratched)."""
    return P.build_state_line(rev, phase, dict(zip(P.CUP_NUMBERS, horses)),
                              dict(zip(P.CUP_NUMBERS, [bool(s) for s in scratched])))


ZERO_HORSES = [0] * 20
NO_SCR_B = [False] * 20


# -----------------------------------------------------------------------------
# Model
# -----------------------------------------------------------------------------

def test_empty_snapshot_model_shape():
    b, wall, _ = fresh_board()
    _check("an empty BETTING_OPEN snapshot changes the model", b.apply_snapshot(snap(phase=1)))
    m = b.model()
    _check("exactly the 18 model keys", set(m) == MODEL_KEYS, str(sorted(m)))
    _check("link_ok true from port_open + gateway_online", m["link_ok"] is True)
    _check("race_state 1 / BETTING_OPEN", (m["race_state"], m["race_state_name"]) == (1, "BETTING_OPEN"))
    _check("token_value from settings", m["token_value"] == float(DEFAULTS["TOKEN_VALUE"]))
    _check("pot 0.0, total 0", m["pot"] == 0.0 and m["total_tokens"] == 0)
    _check("horses 1..20", sorted(m["horses"], key=int) == [str(n) for n in range(1, 21)])
    _check("every horse unassigned", all(h == UNASSIGNED for h in m["horses"].values()))
    _check("leader None, events []", m["leader"] is None and m["events"] == [])
    _check("updated is the wall time of the change", m["updated"] == wall.t)
    _check("board_states from settings", m["board_states"] == list(DEFAULTS["QUINIELA_BOARD_STATES"]))
    _check("model() is a fresh copy", b.model() is not b.model() and b.model() == m)
    _check("model_json() is the compact JSON", b.model_json() == json.dumps(m, separators=(",", ":")))


def test_tokens_share_leader_online_scratched():
    b, wall, _ = fresh_board()
    b.apply_snapshot(snap(cups=[
        cup_entry(1, horse=7, count=23, online=True),
        cup_entry(2, horse=3, count=10, scratched=True, online=False),
        cup_entry(3, horse=12, count=0, online=True),
    ]))
    m = b.model()
    _check("total 33", m["total_tokens"] == 33)
    _check("pot = unscratched tokens * token_value, 2 dp (horse 3's 10 are out)",
           m["pot"] == round(23 * float(DEFAULTS["TOKEN_VALUE"]), 2), str(m["pot"]))
    h7, h3, h12 = m["horses"]["7"], m["horses"]["3"], m["horses"]["12"]
    _check("horse 7 entry", h7 == {"tokens": 23, "share": round(23 / 33, 4), "scratched": False,
                                   "online": True, "cup": 1, "name": "", "replaced": None}, str(h7))
    _check("horse 3 tokens/share", h3["tokens"] == 10 and h3["share"] == round(10 / 33, 4))
    _check("horse 3 scratched and offline", h3["scratched"] is True and h3["online"] is False)
    _check("horse 3 cup is the 1-based cup number", h3["cup"] == 2)
    _check("horse 12 online with share 0", h12["online"] is True and h12["share"] == 0)
    _check("leader 7", m["leader"] == 7)
    b.apply_snapshot(snap(cups=[cup_entry(20, horse=1, count=1, online=True)]))
    _check("cup 20 reads as cup 20 (no 0-based slot anywhere)", b.model()["horses"]["1"]["cup"] == 20)


def test_leader_none_without_tokens_and_lowest_on_tie():
    b, _, _ = fresh_board()
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=4), cup_entry(2, horse=9)]))
    _check("no tokens -> leader None", b.model()["leader"] is None)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=9, count=5), cup_entry(2, horse=4, count=5)]))
    _check("tie -> lowest horse number", b.model()["leader"] == 4)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=9, count=5, scratched=True), cup_entry(2, horse=4, count=4)]))
    _check("a scratched horse is not excluded from the lead", b.model()["leader"] == 9)


def test_odd_entries_never_raise():
    b, _, _ = fresh_board()
    b.apply_snapshot(snap(fill=False, cups=[
        {"cup": 1, "horse": 5, "count": None, "online": False},              # never heard: count None
        {"cup": 2, "horse": None, "count": 4},                               # no horse
        {"cup": 3, "horse": 0, "count": 4},                                  # horse 0 = unassigned
        {"cup": 4, "horse": 21, "count": 4},                                 # out of range
        {"cup": "5", "horse": "8", "count": "6", "scratched": "1", "online": "true"},  # strings
        {"horse": 9, "count": 3},                                            # no cup
        "garbage",                                                           # not a dict
        {"cup": 7, "horse": 10, "count": -4, "online": 1},                   # negative clamps to 0
        {"cup": 0, "horse": 11, "count": 2},                                 # cup out of range
        {"cup": 21, "horse": 13, "count": 2},
        {"cup": 8, "horse": 14, "count": 2.0, "online": True, "scratched": None},
    ]))
    m = b.model()
    _check("count None reads as 0 tokens", m["horses"]["5"] == {"tokens": 0, "share": 0, "scratched": False,
                                                                "online": False, "cup": 1, "name": "",
                                                                "replaced": None}, str(m["horses"]["5"]))
    _check("string fields are coerced", m["horses"]["8"] == {"tokens": 6, "share": 0.75, "scratched": True,
                                                              "online": True, "cup": 5, "name": "",
                                                              "replaced": None}, str(m["horses"]["8"]))
    _check("negative tokens clamp to 0, cup kept", m["horses"]["10"]["tokens"] == 0 and m["horses"]["10"]["cup"] == 7
           and m["horses"]["10"]["online"] is True)
    _check("float count, None scratched", m["horses"]["14"]["tokens"] == 2 and m["horses"]["14"]["scratched"] is False)
    for n in (1, 2, 4, 9, 11, 12, 13, 20):
        _check(f"horse {n} untouched", m["horses"][str(n)] == UNASSIGNED, str(m["horses"][str(n)]))
    _check("total 8", m["total_tokens"] == 8)
    # Missing / wrong-typed sections never raise either.
    for odd in ({}, None, [], "x", {"devpi": {"phase": "x"}, "cups": {"cup": 1}, "link": "nope"},
                {"devpi": None, "cups": None, "link": None}, {"cups": [None, 1, []]}):
        try:
            b.apply_snapshot(odd)
            _check(f"odd snapshot {odd!r} does not raise", True)
        except Exception as exc:
            _check(f"odd snapshot {odd!r} does not raise", False, repr(exc))
    m = b.model()
    _check("odd snapshot -> link down, PRE_RACE, no tokens",
           m["link_ok"] is False and m["race_state"] == 0 and m["total_tokens"] == 0)


def test_events_diff_newest_first_last_eight():
    b, wall, _ = fresh_board()
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=23), cup_entry(2, horse=3, count=2)]))
    _check("the first snapshot is the baseline, not a bet", b.model()["events"] == [])
    wall.advance(1)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=24), cup_entry(2, horse=3, count=2)]))
    _check("one drop -> one event with delta and ts",
           b.model()["events"] == [{"horse": 7, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    wall.advance(1)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=24), cup_entry(2, horse=3, count=0)]))
    events = b.model()["events"]
    _check("newest first", [e["horse"] for e in events] == [3, 7])
    _check("removal is a negative delta", events[0]["delta"] == -2 and events[0]["ts"] == wall.t)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=24), cup_entry(2, horse=3, count=0)]))
    _check("an unchanged snapshot adds nothing", len(b.model()["events"]) == 2)
    for i in range(1, 11):
        wall.advance(1)
        b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=24 + i), cup_entry(2, horse=3, count=0)]))
    events = b.model()["events"]
    _check(f"at most {MAX_EVENTS} events", len(events) == MAX_EVENTS)
    _check("all deltas +1", [e["delta"] for e in events] == [1] * MAX_EVENTS)
    _check("newest first by ts", events[0]["ts"] > events[-1]["ts"])
    _check("tokens followed", b.model()["horses"]["7"]["tokens"] == 34)


def test_reset_and_remap_produce_no_ghost_bets():
    """The 2026-09-25 bench defect: POST /api/lq/dev/reset zeroed the board and
    the ticker showed #3 -50 and #2 -42. A moved roster_rev (reset_link,
    set_roster, adopt_roster) is a fresh baseline that clears the events; a
    horse moved between cups gets no event; a real drop or removal on the
    same cup still does."""
    b, wall, log_dir = fresh_board()

    def live(roster_rev, h3=50, h2=42, state_rev=1, phase=1):
        return snap(phase=phase, state_rev=state_rev, roster_rev=roster_rev, has_roster=True,
                    cups=[cup_entry(1, horse=3, count=h3, online=True),
                          cup_entry(2, horse=2, count=h2, online=True)])

    b.apply_snapshot(live(1))
    wall.advance(1)
    b.apply_snapshot(live(1, h3=51))
    _check("a bet before the reset is an event",
           b.model()["events"] == [{"horse": 3, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    _check("pot before the reset", b.model()["pot"] == 93.0)

    # reset_link(): both revs bump, has_state/has_roster false, every cup's horse None,
    # the counts themselves still sit on the cups.
    wall.advance(1)
    after_reset = snap(phase=0, state_rev=2, roster_rev=2, has_state=False, has_roster=False,
                       cups=[cup_entry(1, count=51, online=True, mac="A0:B7:65:00:00:01"),
                             cup_entry(2, count=42, online=True, mac="A0:B7:65:00:00:02")])
    _check("the reset snapshot changes the model", b.apply_snapshot(after_reset))
    m = b.model()
    _check("after the reset: nothing bet, PRE_RACE",
           m["total_tokens"] == 0 and m["pot"] == 0.0 and m["race_state"] == 0)
    _check("the ticker is empty: no -51 / -42 ghosts", m["events"] == [], str(m["events"]))
    _, lines = log_lines(log_dir)
    _check("the log records the zeroing as a baseline, not bets",
           lines[-1].get("baseline") is True
           and {"horse": 3, "tokens": [51, 0]} in lines[-1]["changes"], str(lines[-1]))
    wall.advance(1)
    _check("a repeat of the zero picture is not a change", b.apply_snapshot(after_reset) is False)
    _check("still no events", b.model()["events"] == [])

    # adopt_roster() (roster_rev 3) then `horse 1 3` / `horse 2 2` (state_rev 3): the counts
    # come back onto the horses. Neither is a bet.
    wall.advance(1)
    b.apply_snapshot(snap(phase=0, state_rev=2, roster_rev=3, has_state=False, has_roster=True,
                          cups=[cup_entry(1, count=51, online=True, mac="A0:B7:65:00:00:01"),
                                cup_entry(2, count=42, online=True, mac="A0:B7:65:00:00:02")]))
    _check("adopt alone: no events", b.model()["events"] == [])
    _, lines = log_lines(log_dir)
    _check("adopt alone: the rev move is logged as a baseline with no changes",
           lines[-1].get("baseline") is True and lines[-1]["changes"] == [], str(lines[-1]))
    wall.advance(1)
    b.apply_snapshot(live(3, h3=51, h2=42, state_rev=3, phase=0))
    m = b.model()
    _check("horses back on cups that already hold tokens: counts show, pot back",
           m["horses"]["3"]["tokens"] == 51 and m["pot"] == 93.0)
    _check("... but no +51 / +42 ghost bets", m["events"] == [], str(m["events"]))
    _, lines = log_lines(log_dir)
    _check("... and the log says the counts came with the cups (no baseline flag, not bets)",
           "baseline" not in lines[-1]
           and {"horse": 3, "tokens": [0, 51], "cup": [None, 1]} in lines[-1]["changes"]
           and {"horse": 2, "tokens": [0, 42], "cup": [None, 2]} in lines[-1]["changes"], str(lines[-1]))

    # From here on the same cups carry the same horses: real movement counts.
    wall.advance(1)
    b.apply_snapshot(live(3, h3=52, h2=42, state_rev=4, phase=1))
    _check("a real drop after the reset is an event (state_rev alone never clears)",
           b.model()["events"] == [{"horse": 3, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    wall.advance(1)
    b.apply_snapshot(live(3, h3=52, h2=41, state_rev=4, phase=1))
    _check("a real removal is still a negative event",
           b.model()["events"][0] == {"horse": 2, "delta": -1, "ts": wall.t}, str(b.model()["events"]))
    _check("older events kept", len(b.model()["events"]) == 2)
    _, lines = log_lines(log_dir)
    _check("a bet's log entry is plain: no cup move, no baseline flag",
           "baseline" not in lines[-1] and lines[-1]["changes"] == [{"horse": 2, "tokens": [42, 41]}],
           str(lines[-1]))

    # A horse moved to another cup: its tokens jump to that cup's count, no event.
    wall.advance(1)
    b.apply_snapshot(snap(phase=1, state_rev=5, roster_rev=3, has_roster=True,
                          cups=[cup_entry(1, horse=3, count=52, online=True),
                                cup_entry(2, count=41, online=True, mac="A0:B7:65:00:00:02"),
                                cup_entry(4, horse=2, count=7, online=True)]))
    m = b.model()
    _check("horse 2 now on cup 4 with that cup's count",
           m["horses"]["2"] == {"tokens": 7, "share": round(7 / 59, 4), "scratched": False,
                                "online": True, "cup": 4, "name": "", "replaced": None}, str(m["horses"]["2"]))
    _check("no event for the re-mapping",
           len(m["events"]) == 2 and m["events"][0]["horse"] == 2 and m["events"][0]["delta"] == -1)
    _, lines = log_lines(log_dir)
    _check("the re-mapping is logged with the cup move",
           {"horse": 2, "tokens": [41, 7], "cup": [2, 4]} in lines[-1]["changes"], str(lines[-1]))
    wall.advance(1)
    b.apply_snapshot(snap(phase=1, state_rev=5, roster_rev=3, has_roster=True,
                          cups=[cup_entry(1, horse=3, count=52, online=True),
                                cup_entry(4, horse=2, count=8, online=True)]))
    _check("a drop on the new cup is a bet again",
           b.model()["events"][0] == {"horse": 2, "delta": 1, "ts": wall.t})

    # A second reset with events on the board clears them outright.
    wall.advance(1)
    b.apply_snapshot(snap(phase=0, state_rev=6, roster_rev=4, has_state=False, has_roster=False,
                          cups=[cup_entry(1, count=52, online=True, mac="A0:B7:65:00:00:01"),
                                cup_entry(4, count=8, online=True, mac="A0:B7:65:00:00:04")]))
    _check("a reset clears the events that were showing", b.model()["events"] == [])


def test_duplicate_horse_keeps_lowest_cup_and_warns_once():
    b, _, _ = fresh_board()
    cups = [cup_entry(4, horse=7, count=5), cup_entry(2, horse=7, count=9), cup_entry(3, horse=7, count=1)]
    with capture_logs(LOGGER) as cap:
        b.apply_snapshot(snap(cups=cups))
        b.apply_snapshot(snap(cups=cups, state_rev=2))
        b.apply_snapshot(snap(cups=cups, state_rev=3))
    warnings = cap.messages("both claim horse 7")
    _check("one WARNING per distinct (kept, dup) pair", len(warnings) == 2, str(warnings))
    _check("the message names the cups and the kept one",
           "cups 2 and 3 both claim horse 7; keeping cup 2" in warnings
           and "cups 2 and 4 both claim horse 7; keeping cup 2" in warnings, str(warnings))
    _check("warned at WARNING on la_quiniela.betting", all(r.levelno == logging.WARNING for r in cap.records))
    h7 = b.model()["horses"]["7"]
    _check("lowest cup number wins", h7["cup"] == 2 and h7["tokens"] == 9)
    _check("the losers' tokens do not count", b.model()["total_tokens"] == 9)


def test_race_state_names():
    b, _, _ = fresh_board()
    for st, name in {0: "PRE_RACE", 1: "BETTING_OPEN", 2: "FINAL_CALL", 3: "AT_THE_POST",
                     4: "RUNNING", 5: "WINNER", 6: "AFTER_PARTY"}.items():
        b.apply_snapshot(snap(phase=st))
        m = b.model()
        _check(f"phase {st} -> {name}", (m["race_state"], m["race_state_name"]) == (st, name))
    b.apply_snapshot(snap(phase=9))
    _check("unknown phase -> STATE_<n>", b.model()["race_state_name"] == "STATE_9")
    s = snap(phase=2)
    del s["devpi"]["phase"]
    b.apply_snapshot(s)
    _check("missing phase -> 0", b.model()["race_state"] == 0)
    s = snap(phase="x")
    b.apply_snapshot(s)
    _check("junk phase -> 0", b.model()["race_state"] == 0 and b.model()["race_state_name"] == "PRE_RACE")
    _check("names come from protocol.Phase", betting.RACE_STATE_NAMES == {int(p): p.name for p in P.Phase})


def test_link_ok_follows_the_snapshot():
    b, wall, _ = fresh_board()
    q = b.subscribe()
    _check("link_ok false to start", b.model()["link_ok"] is False and b.link_ok() is False)
    _check("up: published", b.apply_snapshot(snap()) and b.model()["link_ok"] is True and q.qsize() == 1)
    _check("same again: not published", b.apply_snapshot(snap()) is False and q.qsize() == 1)
    wall.advance(1)
    _check("gateway offline: published", b.apply_snapshot(snap(gateway_online=False)))
    m = b.model()
    _check("link_ok false, other fields kept", m["link_ok"] is False and m["race_state"] == 1
           and m["updated"] == wall.t)
    _check("two publishes so far", q.qsize() == 2)
    q.get_nowait()
    _check("the published model says link down", json.loads(q.get_nowait())["link_ok"] is False)
    _check("port closed while gateway 'online' is still down (no change)",
           b.apply_snapshot(snap(port_open=False, gateway_online=True)) is False and b.model()["link_ok"] is False)
    _check("both up again restores it", b.apply_snapshot(snap()) and b.model()["link_ok"] is True)
    s = snap()
    del s["link"]
    _check("no link section -> down", b.apply_snapshot(s) and b.model()["link_ok"] is False)


def test_unchanged_snapshot_is_not_published():
    b, wall, _ = fresh_board()
    q = b.subscribe()
    _check("first snapshot published", b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1, online=True)])))
    wall.advance(1)
    _check("identical snapshot not published",
           b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1, online=True)], state_rev=2)) is False)
    _check("still one item queued", q.qsize() == 1)
    _check("a cup going quiet (online flips) is published",
           b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1, online=False)])) and q.qsize() == 2)
    _check("updated bumped only on change", b.model()["updated"] == wall.t)
    wall.advance(1)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1, online=False)]))
    _check("updated untouched by a no-change apply", b.model()["updated"] == wall.t - 1)


def test_subscriber_queue_drops_oldest_when_full():
    b, _, _ = fresh_board()
    q = b.subscribe()
    for i in range(SSE_QUEUE_SIZE + 8):
        b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=i)]))
    _check(f"queue capped at {SSE_QUEUE_SIZE}", q.qsize() == SSE_QUEUE_SIZE)
    newest = None
    while not q.empty():
        newest = json.loads(q.get_nowait())
    _check("the newest model survived", newest["horses"]["7"]["tokens"] == SSE_QUEUE_SIZE + 7)
    b.unsubscribe(q)
    _check("unsubscribe", b.subscriber_count() == 0)
    b.unsubscribe(q)
    _check("unsubscribe twice is harmless", b.subscriber_count() == 0)


def test_token_value_and_board_states_from_settings():
    b, _, _ = fresh_board(TOKEN_VALUE=0.5, QUINIELA_BOARD_STATES=[1, 2])
    m = b.model()
    _check("token_value from settings before any snapshot", m["token_value"] == 0.5)
    _check("board_states from settings", m["board_states"] == [1, 2])
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=4)]))
    _check("pot = 4 * 0.5", b.model()["pot"] == 2.0)
    b.settings["TOKEN_VALUE"] = 2
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=5)]))
    _check("a settings change is read at the next apply", b.model()["pot"] == 10.0 and b.model()["token_value"] == 2.0)
    _check("a board with no settings uses DEFAULTS", BettingBoard().settings == DEFAULTS
           and BettingBoard().settings is not DEFAULTS)


# -----------------------------------------------------------------------------
# Event log
# -----------------------------------------------------------------------------

def test_log_writes_one_line_per_model_change():
    b, wall, log_dir = fresh_board()
    _check("log dir created lazily", not log_dir.exists())
    b.apply_snapshot(snap(phase=1, cups=[cup_entry(1, horse=7, count=23)]))
    path, lines = log_lines(log_dir)
    _check("a log file exists", path is not None)
    _check("named quiniela_<local date>.jsonl",
           path is not None and path.name == f"quiniela_{datetime.date.today().isoformat()}.jsonl",
           str(path))
    _check("one line, the exact record", lines == [{
        "ts": round(wall.t, 3), "race_state": 1,
        "changes": [{"horse": 7, "tokens": [0, 23]}, {"race_state": [0, 1]}],
        "total_tokens": 23,
    }], str(lines))
    wall.advance(2)
    b.apply_snapshot(snap(phase=2, cups=[cup_entry(1, horse=7, count=24, scratched=True)]))
    _, lines = log_lines(log_dir)
    _check("second change logged", len(lines) == 2)
    _check("tokens, scratched and race_state changes in order", lines[1]["changes"] == [
        {"horse": 7, "tokens": [23, 24]},
        {"horse": 7, "scratched": [False, True]},
        {"race_state": [1, 2]},
    ], str(lines[1]))
    _check("total on the record", lines[1]["total_tokens"] == 24)
    b.apply_snapshot(snap(phase=2, cups=[cup_entry(1, horse=7, count=24, scratched=True)]))
    b.apply_snapshot(snap(phase=2, cups=[cup_entry(1, horse=7, count=24, scratched=True, online=True)]))
    b.apply_snapshot(snap(phase=2, cups=[cup_entry(1, horse=7, count=24, scratched=True, online=True)],
                          gateway_online=False))
    _, lines = log_lines(log_dir)
    _check("same again, an online flip and a link flip: no line", len(lines) == 2)
    _check("the lines are compact JSON",
           "\n".join(json.dumps(l, separators=(",", ":")) for l in lines) + "\n"
           == path.read_text(encoding="utf-8"))


def test_log_disabled_by_settings():
    b, _, log_dir = fresh_board(QUINIELA_LOG=False)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1)]))
    _check("QUINIELA_LOG false -> nothing written", not log_dir.exists())
    b.settings["QUINIELA_LOG"] = True
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=2)]))
    _check("turning it on is read at the next change", log_lines(log_dir)[0] is not None)


def test_log_write_failure_warns_once_and_disables():
    b, _, log_dir = fresh_board()
    log_dir.parent.mkdir(parents=True, exist_ok=True)
    log_dir.write_text("not a directory")       # mkdir() on it raises OSError
    with capture_logs(LOGGER) as cap:
        b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1)]))
        b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=2)]))
    _check("one WARNING then silence", len(cap.messages("event log disabled")) == 1, str(cap.messages()))
    _check("the model is unaffected", b.model()["horses"]["7"]["tokens"] == 2)


def test_log_module_dir_is_the_default():
    saved = betting.LOG_DIR
    betting.LOG_DIR = tmpdir() / "module_logs"
    try:
        b = BettingBoard(wall=FakeClock(1_700_000_000.0))     # no log_dir given
        b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1)]))
        _check("written under betting.LOG_DIR", len(list(betting.LOG_DIR.glob("quiniela_*.jsonl"))) == 1)
    finally:
        betting.LOG_DIR = saved
    _check("the default LOG_DIR is pi5/data", betting.LOG_DIR == Path(_PI5_DIR) / "data")


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

ENV_KEYS = ("DDM_TOKEN_VALUE", "DDM_QUINIELA_LOG", "DDM_QUINIELA_BOARD_STATES",
            "DDM_LQ_SPLIT_WIN", "DDM_LQ_SPLIT_PLACE", "DDM_LQ_SPLIT_SHOW", "DDM_LQ_CHYRON_LINES")


class fake_config:
    """Swap a bare module in for pi5/config.py while load_board_settings runs."""

    def __init__(self, **attrs):
        self.mod = types.ModuleType("config")
        for k, v in attrs.items():
            setattr(self.mod, k, v)

    def __enter__(self):
        self.saved = sys.modules.get("config")
        sys.modules["config"] = self.mod
        return self.mod

    def __exit__(self, *exc):
        if self.saved is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = self.saved


def test_settings_resolution():
    saved_env = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    try:
        with fake_config():
            s = load_board_settings()
            _check("defaults with nothing configured", s == DEFAULTS, str(s))
            _check("the list is a copy", s["QUINIELA_BOARD_STATES"] is not DEFAULTS["QUINIELA_BOARD_STATES"])
        with fake_config(TOKEN_VALUE=2, QUINIELA_LOG=0, QUINIELA_BOARD_STATES=(1, 2)):
            s = load_board_settings()
            _check("config.py attributes win over defaults",
                   s == {**DEFAULTS, "TOKEN_VALUE": 2.0, "QUINIELA_LOG": False, "QUINIELA_BOARD_STATES": [1, 2]}, str(s))
        with fake_config(TOKEN_VALUE="abc", QUINIELA_LOG="maybe", QUINIELA_BOARD_STATES=[1, "x"]):
            with capture_logs(LOGGER) as cap:
                s = load_board_settings()
            _check("junk config values keep the defaults", s == DEFAULTS, str(s))
            _check("and warn once each", len(cap.messages("ignoring config.py")) == 3, str(cap.messages()))
        os.environ["DDM_TOKEN_VALUE"] = "0.25"
        os.environ["DDM_QUINIELA_LOG"] = "no"
        os.environ["DDM_QUINIELA_BOARD_STATES"] = "1, 2,5"
        with fake_config(TOKEN_VALUE=2.0):
            s = load_board_settings()
        _check("env float", s["TOKEN_VALUE"] == 0.25)
        _check("env bool", s["QUINIELA_LOG"] is False)
        _check("env comma list with spaces", s["QUINIELA_BOARD_STATES"] == [1, 2, 5])
        for raw, expected in (("1", True), ("true", True), ("YES", True), ("on", True),
                              ("0", False), ("false", False), ("off", False)):
            os.environ["DDM_QUINIELA_LOG"] = raw
            _check(f"env bool {raw!r} -> {expected}", load_board_settings()["QUINIELA_LOG"] is expected)
        os.environ["DDM_QUINIELA_BOARD_STATES"] = ""
        _check("an empty state list means the board never owns the TV",
               load_board_settings()["QUINIELA_BOARD_STATES"] == [])
        os.environ["DDM_TOKEN_VALUE"] = "abc"
        os.environ["DDM_QUINIELA_LOG"] = "junk"
        os.environ["DDM_QUINIELA_BOARD_STATES"] = "1,x"
        with fake_config():
            with capture_logs(LOGGER) as cap:
                s = load_board_settings()
        _check("junk env keeps the defaults", s == DEFAULTS, str(s))
        _check("junk env warns once each", len(cap.messages("ignoring environment")) == 3, str(cap.messages()))
        os.environ["DDM_TOKEN_VALUE"] = "nan"
        with capture_logs(LOGGER) as cap:
            _check("nan is junk too", load_board_settings()["TOKEN_VALUE"] == 1.0 and cap.messages("nan"))
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        with fake_config(TOKEN_VALUE=2.0):
            s = load_board_settings({"TOKEN_VALUE": 3, "QUINIELA_BOARD_STATES": "2,3", "EXTRA": 1})
        _check("explicit overrides win and are coerced", s["TOKEN_VALUE"] == 3.0 and s["QUINIELA_BOARD_STATES"] == [2, 3])
        _check("unknown override keys pass through", s["EXTRA"] == 1)
        with capture_logs(LOGGER) as cap:
            s = load_board_settings({"TOKEN_VALUE": "junk"})
        _check("a junk override is skipped with a warning", s["TOKEN_VALUE"] == 1.0 and cap.messages("override"))
        _check("the real pi5/config.py resolves without raising", isinstance(load_board_settings(), dict))
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_huge_int_settings_never_raise():
    """float() of an int beyond float range raises OverflowError, which the
    settings coercion and the model's token value must treat like any other
    junk value: a WARNING and the default, never an exception at import or
    in a request."""
    huge = 10 ** 400
    saved_env = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    try:
        with capture_logs(LOGGER) as cap:
            with fake_config(TOKEN_VALUE=huge):
                s = load_board_settings()
        _check("config.py TOKEN_VALUE = 10**400 keeps the default", s["TOKEN_VALUE"] == 1.0, str(s))
        _check("and warns once", len(cap.messages("ignoring config.py")) == 1, str(cap.messages()))
        try:
            b = BettingBoard(settings={"TOKEN_VALUE": huge}, log_dir=tmpdir())   # never the real pi5/data log
            _check("a board built with a huge int token value serves the default",
                   b.model()["token_value"] == 1.0, str(b.model()["token_value"]))
            _check("apply_snapshot() with it never raises",
                   b.apply_snapshot(snap(cups=[cup_entry(7, horse=7, count=2, online=True)])) is True
                   and b.model()["pot"] == 2.0, str(b.model()["pot"]))
        except OverflowError as exc:
            _check("a huge int token value never raises", False, repr(exc))
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v


# -----------------------------------------------------------------------------
# Fed from a real bridge
# -----------------------------------------------------------------------------

def test_real_bridge_feeds_the_board():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, log_dir = fresh_board(bridge=b)
    _check("a bare bridge digests to the empty model (no change)", board.refresh() is False)
    m = board.model()
    _check("link down: port open but no gateway yet", m["link_ok"] is False and m["race_state"] == 0)
    b.set_state(1, HORSES_1_TO_20, SCR_CUP7)
    _check("set_state changes the model", board.refresh())
    m = board.model()
    _check("phase from devpi", (m["race_state"], m["race_state_name"]) == (1, "BETTING_OPEN"))
    _check("horse n on cup n, 1-based", all(m["horses"][str(n)]["cup"] == n for n in range(1, 21)))
    _check("scratched from devpi state", m["horses"]["7"]["scratched"] is True and m["horses"]["8"]["scratched"] is False)
    _check("no telemetry yet: 0 tokens, offline", m["total_tokens"] == 0 and not any(h["online"] for h in m["horses"].values()))
    b.handle_raw_line(telem(6, MAC_A, count=3))          # wire 6 -> cup 7 -> horse 7
    _check("telemetry changes the model", board.refresh())
    m = board.model()
    _check("count -> tokens on the horse of that cup", m["horses"]["7"] == {"tokens": 3, "share": 1.0, "scratched": True,
                                                                             "online": True, "cup": 7, "name": "",
                                                                             "replaced": None}, str(m["horses"]["7"]))
    _check("any line puts the gateway online -> link_ok", m["link_ok"] is True)
    _check("leader, pot (horse 7 is scratched at the gateway: its 3 tokens count but are out of the pot)",
           m["leader"] == 7 and m["pot"] == 0.0 and m["total_tokens"] == 3, str((m["leader"], m["pot"])))
    _check("the first count after the baseline is a drop event", m["events"] == [{"horse": 7, "delta": 3, "ts": wall.t}], str(m["events"]))
    wall.advance(1)
    b.handle_raw_line(telem(6, MAC_A, count=5))
    board.refresh()
    _check("a second drop", board.model()["events"][0] == {"horse": 7, "delta": 2, "ts": wall.t})
    _, lines = log_lines(log_dir)
    _check("the log followed", [l["changes"] for l in lines][-1] == [{"horse": 7, "tokens": [3, 5]}], str(lines))
    clk.advance(7)
    b.tick(clk())
    board.refresh()
    _check("cup offline after LQ_CUP_OFFLINE_S: online false, tokens kept",
           board.model()["horses"]["7"]["online"] is False and board.model()["horses"]["7"]["tokens"] == 5)
    clk.advance(13)
    b.tick(clk())
    board.refresh()
    _check("gateway offline -> link_ok false", board.model()["link_ok"] is False)
    b.handle_raw_line(status())
    board.refresh()
    _check("a status line brings it back", board.model()["link_ok"] is True)
    b.reset_link("test")
    board.refresh()
    m = board.model()
    _check("reset_link: PRE_RACE, no horses on cups", m["race_state"] == 0
           and all(h["cup"] is None for h in m["horses"].values()), str(m["horses"]["7"]))
    _check("reset_link: the ticker is cleared, no -5 ghost", m["events"] == [], str(m["events"]))
    _, lines = log_lines(log_dir)
    _check("reset_link: the log record is a baseline", lines[-1].get("baseline") is True, str(lines[-1]))
    _check("refresh() with no bridge is False", BettingBoard().refresh() is False)


def test_listener_hook():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    hits = []
    b.add_listener(lambda: hits.append("a"))
    n = len(hits)
    b.handle_raw_line(telem(0, MAC_A, count=1))
    _check("a telem line (new cup) calls the listener", len(hits) > n)
    n = len(hits)
    b.handle_raw_line(telem(0, MAC_A, count=2))
    _check("a count change calls the listener", len(hits) > n)
    n = len(hits)
    b.set_state(1, ZERO_HORSES, NO_SCR)
    n2 = len(hits)
    _check("set_state calls the listener", n2 > n)
    sio.clear()
    b.set_state(2, ZERO_HORSES, NO_SCR)                  # phase only: no horse changed
    _check("a phase-only set_state emits nothing to SocketIO", sio.events == [], str(sio.events))
    _check("...but still calls the listener", len(hits) == n2 + 1, str(len(hits) - n2))
    n = len(hits)
    _check("an identical set_state is a no-op", b.set_state(2, ZERO_HORSES, NO_SCR) == b.state_rev and len(hits) == n)
    b.reset_link("test")
    _check("reset_link calls the listener", len(hits) > n)
    n = len(hits)
    clk.advance(13)
    b.tick(clk())
    _check("the offline timer (lq_link emit) calls the listener", len(hits) > n)
    # A listener that raises is logged and dropped; the others still run.
    order = []
    b.add_listener(lambda: (_ for _ in ()).throw(RuntimeError("listener bug")))
    b.add_listener(lambda: order.append("last"))
    with capture_logs("la_quiniela.bridge") as cap:
        b.handle_raw_line(telem(0, MAC_A, count=3))
    _check("a failing listener is a WARNING, not an exception", cap.messages("listener") and
           all(r.levelno == logging.WARNING for r in cap.records), str(cap.messages()))
    _check("the listeners after it still ran", order == ["last"] * len(order) and order)
    _check("the bridge carried on", b.cups[MAC_A].count == 3)
    # The board's wake() is exactly such a listener.
    board = BettingBoard(bridge=b)
    b.add_listener(board.wake)
    b.handle_raw_line(telem(0, MAC_A, count=4))
    _check("wake() set the event", board._wake.is_set())


def test_board_thread_picks_up_changes():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    b.add_listener(board.wake)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    _check("start() -> True", board.start() is True and board.running)
    _check("start() again is idempotent", board.start() is True)
    _check("the thread is named lq-board", "lq-board" in {t.name for t in threading.enumerate()})
    deadline = time.time() + 2
    while time.time() < deadline and board.model()["race_state"] != 1:
        time.sleep(0.02)
    _check("the state set before start() is picked up", board.model()["race_state"] == 1)
    port.feed(S.MID_LINE + telem(6, MAC_A, count=5))     # a fresh port drops everything before the first newline
    drain(b, port)
    deadline = time.time() + 2
    while time.time() < deadline and board.model()["horses"]["7"]["tokens"] != 5:
        time.sleep(0.02)
    _check("a telem line reaches the model through the listener within a second",
           board.model()["horses"]["7"]["tokens"] == 5 and board.model()["link_ok"] is True)
    b.link.gateway_online = False            # no emit, no listener: only the 1 s timeout sees it
    deadline = time.time() + 2.5
    while time.time() < deadline and board.model()["link_ok"]:
        time.sleep(0.02)
    _check("a silent change is picked up by the 1 s refresh", board.model()["link_ok"] is False)
    board.stop()
    _check("stop() ends the thread", not board.running and "lq-board" not in {t.name for t in threading.enumerate()})
    board.stop()
    _check("stop() twice is harmless", not board.running)


def test_refresh_is_serialised():
    """Two overlapping refresh() calls (the board thread and a start_board()
    on a running board) apply their snapshots in order: the later snapshot
    wins, and an older one can never regress the model or invent a negative
    drop."""
    inside = threading.Event()
    release = threading.Event()
    calls = []

    class GatedBridge:
        def get_snapshot(self):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:                 # the first caller parks here holding a stale picture
                inside.set()
                release.wait(5)
                return snap(cups=[cup_entry(7, horse=7, count=1, online=True)])
            return snap(cups=[cup_entry(7, horse=7, count=5, online=True)])

    board, wall, _ = fresh_board(bridge=GatedBridge())
    slow = threading.Thread(target=board.refresh, name="refresh-slow", daemon=True)
    slow.start()
    _check("the first refresh is parked inside get_snapshot()", inside.wait(5))
    fast = threading.Thread(target=board.refresh, name="refresh-fast", daemon=True)
    fast.start()
    fast.join(0.3)
    _check("the second refresh waits for the first", fast.is_alive() and calls == ["refresh-slow"], str(calls))
    release.set()
    slow.join(5)
    fast.join(5)
    _check("both finished", not slow.is_alive() and not fast.is_alive())
    m = board.model()
    _check("the later snapshot wins", m["horses"]["7"]["tokens"] == 5, str(m["horses"]["7"]))
    _check("one positive drop, never a negative delta",
           m["events"] == [{"horse": 7, "delta": 4, "ts": wall.t}], str(m["events"]))


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

def test_validate_cmd():
    _check("state 1", validate_cmd("state 1") == ("state 1", None))
    _check("stripped", validate_cmd("  horse 0 7 \n") == ("horse 0 7", None))
    for word in ("state", "horse", "scratch", "demo", "roster", "json"):
        _check(f"{word} allowed", validate_cmd(word)[1] is None)
    for bad, msg in (("", "empty command"), ("   ", "empty command"), (None, "cmd must be a string"),
                     (3, "cmd must be a string"), ("help", "command not allowed: help"),
                     ("debug on", "command not allowed: debug"), ("STATE 1", "command not allowed: STATE"),
                     ("state\n1", "command must be a single line"), ("state\r1", "command must be a single line"),
                     ("x" * 201, "command longer than 200 characters")):
        text, error = validate_cmd(bad)
        _check(f"{bad!r:.20} rejected: {msg}", text is None and error is not None and error.startswith(msg), str(error))
    _check("the allowed list is spelled out", validate_cmd("help")[1]
           == "command not allowed: help (allowed: demo horse json roster scratch state)")
    _check("exactly 200 chars is allowed", validate_cmd("json " + "1" * 195)[1] is None)


def test_cmd_whitelist_400s():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    for bad in ({"cmd": ""}, {"cmd": "   "}, {"cmd": "reboot"}, {"cmd": "State 1"}, {"cmd": "state 1\nreboot"},
                {"cmd": "state 1\rreboot"}, {"cmd": "state " + "1" * 200}, {"cmd": 5}, {"nope": "state 1"},
                ["state 1"]):
        r = client.post("/api/quiniela/cmd", json=bad)
        body = r.get_json()
        _check(f"{bad!r:.30} -> 400 ok:false with an error", r.status_code == 400 and body["ok"] is False and body["error"],
               f"{r.status_code} {body}")
    r = client.post("/api/quiniela/cmd", data="not json at all", content_type="application/json")
    _check("non-JSON body -> 400", r.status_code == 400 and r.get_json()["error"] == "cmd must be a string")
    r = client.post("/api/quiniela/cmd")
    _check("empty POST -> 400", r.status_code == 400)
    _check("nothing reached the port", port.written == [])


def test_cmd_without_bridge_is_503():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    get_board().bridge = None
    for good in ("state 1", "horse 1 7", "scratch 1 1", "roster", "  state 1  "):
        r = client.post("/api/quiniela/cmd", json={"cmd": good})
        _check(f"{good!r} without a bridge -> 503", r.status_code == 503
               and r.get_json() == {"ok": False, "error": "bridge not initialised"}, f"{r.status_code} {r.get_json()}")
    r = client.post("/api/quiniela/cmd", json={"cmd": "demo"})
    _check("demo is refused (400) before the bridge is needed", r.status_code == 400)
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 9"})
    _check("usage errors are 400 before the bridge is needed", r.status_code == 400 and r.get_json()["error"] == USAGE_STATE)
    r = client.get("/api/quiniela")
    _check("GET /api/quiniela still answers with link_ok false", r.status_code == 200 and r.get_json()["link_ok"] is False)


def test_cmd_state():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 1"})
    body = r.get_json()
    _check("state 1 -> 200 ok, rev 1, phase 1, gateway offline",
           r.status_code == 200 and body == {"ok": True, "rev": 1, "phase": 1, "gateway_online": False}, str(body))
    _check("the bridge holds the state", b.has_state and b.phase == 1 and b.state_rev == 1)
    _check("the downlink state line, byte-exact via build_state_line",
           port.lines() == [state_line(1, 1, ZERO_HORSES, NO_SCR_B)], str(port.lines()))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("the model follows", m["race_state"] == 1 and m["race_state_name"] == "BETTING_OPEN")
    r = client.post("/api/quiniela/cmd", json={"cmd": "  state 6  "})
    _check("whitespace tolerated, rev 2, phase 6", r.get_json()["rev"] == 2 and r.get_json()["phase"] == 6)
    _check("second line, rev 2", port.lines()[-1] == state_line(2, 6, ZERO_HORSES, NO_SCR_B))
    port.written.clear()
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 6"})
    _check("the same state again is a no-op with the same rev", r.status_code == 200 and r.get_json()["rev"] == 2
           and port.written == [])
    b.handle_raw_line(status(phase=6, state_rev=2))
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 0"})
    _check("gateway_online true once the gateway has spoken", r.get_json()["gateway_online"] is True
           and r.get_json()["rev"] == 3)
    _check("state 0 sent even with the gateway online", port.lines()[-1] == state_line(3, 0, ZERO_HORSES, NO_SCR_B))


def test_cmd_horse_and_scratch_1_based():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    b.set_state(1, ZERO_HORSES, NO_SCR)
    port.written.clear()
    r = client.post("/api/quiniela/cmd", json={"cmd": "horse 1 7"})
    body = r.get_json()
    _check("horse 1 7 -> 200 with cup and horse", r.status_code == 200 and body["ok"] is True and body["rev"] == 2
           and body["cup"] == 1 and body["horse"] == 7 and body["phase"] == 1, str(body))
    horses = [7] + [0] * 19
    _check("cup 1 is wire index 0 on the line", port.lines() == [state_line(2, 1, horses, NO_SCR_B)], str(port.lines()))
    _check("the bridge's cup 1 has horse 7", b.horses[1] == 7)
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("the model says horse 7 is on cup 1", m["horses"]["7"]["cup"] == 1)
    r = client.post("/api/quiniela/cmd", json={"cmd": "horse 20 3"})
    horses[19] = 3
    _check("horse 20 3 -> wire index 19", r.get_json()["rev"] == 3 and port.lines()[-1] == state_line(3, 1, horses, NO_SCR_B),
           str(port.lines()[-1]))
    _check("phase untouched by horse", b.phase == 1 and r.get_json()["phase"] == 1)
    r = client.post("/api/quiniela/cmd", json={"cmd": "scratch 20 1"})
    scr = [False] * 19 + [True]
    _check("scratch 20 1 -> 200 with scratched true", r.get_json()["cup"] == 20 and r.get_json()["scratched"] is True
           and r.get_json()["rev"] == 4, str(r.get_json()))
    _check("scr[19] on the line", port.lines()[-1] == state_line(4, 1, horses, scr), str(port.lines()[-1]))
    get_board().refresh()
    _check("the model says horse 3 is scratched", client.get("/api/quiniela").get_json()["horses"]["3"]["scratched"] is True)
    r = client.post("/api/quiniela/cmd", json={"cmd": "scratch 20 0"})
    _check("scratch 20 0 clears it", r.get_json()["scratched"] is False and port.lines()[-1] == state_line(5, 1, horses, NO_SCR_B))
    r = client.post("/api/quiniela/cmd", json={"cmd": "horse 1 0"})
    horses[0] = 0
    _check("horse 1 0 unassigns", r.get_json()["horse"] == 0 and b.horses[1] == 0 and port.lines()[-1] == state_line(6, 1, horses, NO_SCR_B))
    get_board().refresh()
    _check("the model drops horse 7", client.get("/api/quiniela").get_json()["horses"]["7"]["cup"] is None)
    r = client.post("/api/quiniela/cmd", json={"cmd": "horse 20 3"})
    _check("re-stating the same horse is a no-op", r.get_json()["rev"] == 6)
    # Works before any state was set too: the lists start from the snapshot's zeros.
    b2, port2, sio2, clk2 = _fresh_bridge()
    b2._open_port()
    client2 = _make_board_app(b2).test_client()
    r = client2.post("/api/quiniela/cmd", json={"cmd": "horse 3 12"})
    _check("horse before any state: phase 0, rev 1", r.get_json() == {"ok": True, "rev": 1, "phase": 0, "gateway_online": False,
                                                                     "cup": 3, "horse": 12}, str(r.get_json()))
    _check("...and the line carries it", port2.lines() == [state_line(1, 0, [0, 0, 12] + [0] * 17, NO_SCR_B)], str(port2.lines()))


def test_cmd_roster():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    r = client.post("/api/quiniela/cmd", json={"cmd": "roster"})
    body = r.get_json()
    _check("roster -> 200 ok", r.status_code == 200 and body["ok"] is True)
    _check("20 entries, all null, rev 0, no roster", body["roster"] == [None] * 20 and body["roster_rev"] == 0
           and body["has_roster"] is False, str(body))
    b.handle_raw_line(telem(2, MAC_A))               # mirrored: wire 2 -> cup 3
    body = client.post("/api/quiniela/cmd", json={"cmd": "roster"}).get_json()
    _check("a mirrored cup shows at its 1-based position", body["roster"][2] == MAC_A and body["roster"][0] is None
           and body["has_roster"] is False)
    b.set_roster([MAC_B] + [""] * 19)
    port.written.clear()
    body = client.post("/api/quiniela/cmd", json={"cmd": "roster"}).get_json()
    _check("DevPi's roster once set", body["roster"][0] == MAC_B and body["roster"][2] is None
           and body["roster_rev"] == 1 and body["has_roster"] is True, str(body))
    _check("roster writes nothing to the port", port.written == [])
    _check("exact keys", set(body) == {"ok", "roster", "roster_rev", "has_roster"})


def test_cmd_demo_and_json_refused():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    for cmd in ("demo", "demo 1", "demo off"):
        r = client.post("/api/quiniela/cmd", json={"cmd": cmd})
        _check(f"{cmd!r} -> 400 with the demo message", r.status_code == 400
               and r.get_json() == {"ok": False, "error": DEMO_REFUSED}, str(r.get_json()))
    for cmd in ("json", "json 1", "json 0"):
        r = client.post("/api/quiniela/cmd", json={"cmd": cmd})
        _check(f"{cmd!r} -> 400 with the json message", r.status_code == 400
               and r.get_json() == {"ok": False, "error": JSON_REFUSED}, str(r.get_json()))
    _check("the demo message", DEMO_REFUSED == "demo is not routed through pi5: the bridge speaks the JSON line "
           "protocol, and every state line turns demo off")
    _check("the json message", JSON_REFUSED == "json is not routed through pi5: the bridge already reads the "
           "gateway's protocol, and the up state line would exceed its 1024-byte cap")
    _check("nothing reached the port", port.written == [])


def test_cmd_usage_errors():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    cases = [("state", USAGE_STATE), ("state 7", USAGE_STATE), ("state -1", USAGE_STATE), ("state x", USAGE_STATE),
             ("state 1 2", USAGE_STATE), ("state 1.0", USAGE_STATE),
             ("horse", USAGE_HORSE), ("horse 1", USAGE_HORSE), ("horse 1 2 3", USAGE_HORSE), ("horse 0 7", USAGE_HORSE),
             ("horse 21 1", USAGE_HORSE), ("horse 1 21", USAGE_HORSE), ("horse 1 -1", USAGE_HORSE), ("horse a b", USAGE_HORSE),
             ("scratch", USAGE_SCRATCH), ("scratch 1", USAGE_SCRATCH), ("scratch 0 1", USAGE_SCRATCH),
             ("scratch 21 1", USAGE_SCRATCH), ("scratch 1 2", USAGE_SCRATCH), ("scratch 1 yes", USAGE_SCRATCH),
             ("scratch 1 1 1", USAGE_SCRATCH)]
    for cmd, usage in cases:
        r = client.post("/api/quiniela/cmd", json={"cmd": cmd})
        _check(f"{cmd!r} -> 400 {usage}", r.status_code == 400 and r.get_json() == {"ok": False, "error": usage},
               f"{r.status_code} {r.get_json()}")
    _check("nothing reached the port", port.written == [] and not b.has_state)
    # A ValueError out of set_state (cannot happen through the parser, so provoke it) is a 400 with its text.
    original = b.set_state

    def boom(*args, **kwargs):
        raise ValueError("boom from set_state")
    b.set_state = boom
    try:
        r = client.post("/api/quiniela/cmd", json={"cmd": "state 1"})
        _check("ValueError from set_state -> 400 with str(exc)",
               r.status_code == 400 and r.get_json() == {"ok": False, "error": "boom from set_state"}, str(r.get_json()))
    finally:
        b.set_state = original


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

def test_model_route():
    b, port, sio, clk = _fresh_bridge()
    client = _make_board_app(b).test_client()
    r = client.get("/api/quiniela")
    _check("GET /api/quiniela 200 JSON", r.status_code == 200 and r.mimetype == "application/json")
    _check("Cache-Control: no-store", r.headers.get("Cache-Control") == "no-store", str(r.headers.get("Cache-Control")))
    m = r.get_json()
    _check("the 18 keys", set(m) == MODEL_KEYS, str(sorted(m)))
    _check("fresh: link down, PRE_RACE", m["link_ok"] is False and m["race_state"] == 0 and m["race_state_name"] == "PRE_RACE")
    _check("20 horses, no tokens, no leader", len(m["horses"]) == 20 and m["total_tokens"] == 0 and m["leader"] is None)
    _check("token_value and board_states", m["token_value"] == float(DEFAULTS["TOKEN_VALUE"])
           and m["board_states"] == list(DEFAULTS["QUINIELA_BOARD_STATES"]))
    _check("get_board() is the one init_board made", get_board().bridge is b)


def test_model_route_follows_the_bridge():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    b.set_state(2, HORSES_1_TO_20, NO_SCR)
    b.handle_raw_line(telem(6, MAC_A, count=23))
    b.handle_raw_line(telem(2, MAC_B, count=2))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("link up", m["link_ok"] is True)
    _check("FINAL_CALL", m["race_state_name"] == "FINAL_CALL")
    _check("horse 7 (cup 7) 23 tokens, horse 3 (cup 3) 2", m["horses"]["7"]["tokens"] == 23 and m["horses"]["3"]["tokens"] == 2)
    _check("leader 7, total 25", m["leader"] == 7 and m["total_tokens"] == 25)
    _check("cups are 1-based in the model", m["horses"]["7"]["cup"] == 7 and m["horses"]["3"]["cup"] == 3)


def test_stream_generator():
    b, wall, _ = fresh_board()
    gen = sse_events(b, heartbeat_s=0.05, wall=wall)
    first = next(gen)
    _check("first chunk is data: <model>", first.startswith("data: ") and first.endswith("\n\n"))
    model = json.loads(first[len("data: "):])
    _check("the first chunk is the current model", model["link_ok"] is False and model["board_states"] == [1, 2, 3, 4])
    _check("compact JSON", first == "data: " + b.model_json() + "\n\n")
    _check("subscribed", b.subscriber_count() == 1)
    ping = next(gen)
    _check("ping chunk, byte-exact", ping == ": heartbeat\n\nevent: ping\ndata: {\"ts\":%s}\n\n" % json.dumps(round(wall.t, 3)), repr(ping))
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=5)]))
    update = next(gen)
    _check("a published model follows as data:", update.startswith("data: ") and json.loads(update[6:])["total_tokens"] == 5)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=5)]))
    _check("no change: the next chunk is a ping", next(gen).startswith(": heartbeat\n\nevent: ping\n"))
    gen.close()
    _check("closing the generator unsubscribes", b.subscriber_count() == 0)


def test_stream_route_headers_first_event_and_ping():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b.handle_raw_line(telem(6, MAC_A, count=3))
    get_board().refresh()
    saved = betting.SSE_HEARTBEAT_S
    betting.SSE_HEARTBEAT_S = 0.1
    try:
        resp = client.get("/api/quiniela/stream", buffered=False)
        try:
            _check("200 text/event-stream", resp.status_code == 200 and resp.mimetype == "text/event-stream")
            _check("Cache-Control exactly no-cache", resp.headers["Cache-Control"] == "no-cache", resp.headers["Cache-Control"])
            _check("X-Accel-Buffering no", resp.headers["X-Accel-Buffering"] == "no")
            _check("Connection keep-alive", resp.headers["Connection"] == "keep-alive")
            chunks = iter(resp.response)
            first = next(chunks).decode("utf-8")
            _check("first chunk is the model", first.startswith("data: ") and json.loads(first[6:])["horses"]["7"]["tokens"] == 3)
            second = next(chunks).decode("utf-8")
            _check("second chunk is the heartbeat + ping", ": heartbeat\n\n" in second and "event: ping\ndata: {\"ts\":" in second, repr(second))
            _check("one subscriber while open", get_board().subscriber_count() == 1)
        finally:
            resp.close()
        _check("closing the response unsubscribes", get_board().subscriber_count() == 0)
    finally:
        betting.SSE_HEARTBEAT_S = saved


def test_init_and_start_board():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    _make_board_app(b)
    first = get_board()
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    _check("start_board refreshes synchronously first", start_board() is True and first.model()["race_state"] == 1)
    _check("the thread runs", first.running)
    init_board(bridge=b, log_dir=tmpdir())
    _check("init_board again stops the old board", not first.running and get_board() is not first)
    _check("stop_board with a stopped board is fine", stop_board() is None)
    _check("start_board again", start_board() is True and get_board().running)
    stop_board()
    _check("stop_board ends it", not get_board().running)
    board = init_board(bridge=b, settings={"TOKEN_VALUE": 5}, log_dir=tmpdir())
    _check("init_board settings go through load_board_settings", board.settings["TOKEN_VALUE"] == 5.0
           and board.model()["token_value"] == 5.0)
    saved = board_mod._board
    board_mod._board = None
    try:
        _check("start_board with no board is False", start_board() is False)
        try:
            get_board()
            _check("get_board raises before init", False)
        except RuntimeError:
            _check("get_board raises before init", True)
    finally:
        board_mod._board = saved



# -----------------------------------------------------------------------------
# Payout: prizes, names, scratches, closing time, the admin routes
# -----------------------------------------------------------------------------

DERBY_2024 = ["Dornoch", "Sierra Leone", "Mystik Dan", "Catching Freedom", "Catalytic", "Just Steel",
              "Honor Marie", "Just a Touch", "Encino", "T O Password", "Forever Young", "Track Phantom",
              "West Saratoga", "Endlessly", "Domestic Product", "Grand Mo the First", "Fierceness",
              "Stronghold", "Resilience", "Society Man"]
DERBY_TEXT = "\n".join(f"{n}. {name}" for n, name in enumerate(DERBY_2024, 1))


def test_round_half_up_and_prizes():
    for value, expected in ((38.5, 39), (2.5, 3), (0.5, 1), (1.5, 2), (23.1, 23), (23.5, 24), (92, 92),
                            (0, 0), ("4.50", 5)):
        _check(f"round_half_up({value!r}) == {expected}", round_half_up(value) == expected, str(round_half_up(value)))
    _check("round() would have said 38 and 2 (banker's rounding)", round(38.5) == 38 and round(2.5) == 2)
    _check("pot 154 -> place 39, show 23, win 92", prizes_for(154, SPLIT) == {"win": 92, "place": 39, "show": 23},
           str(prizes_for(154, SPLIT)))
    _check("...summing to 154", sum(prizes_for(154, SPLIT).values()) == 154)
    _check("pot 0 -> 0/0/0", prizes_for(0, SPLIT) == {"win": 0, "place": 0, "show": 0})
    _check("pot 1 -> win 1, place 0, show 0", prizes_for(1, SPLIT) == {"win": 1, "place": 0, "show": 0})
    _check("pot 2 -> place 0.5 rounds up to 1, show 0, win 1", prizes_for(2, SPLIT) == {"win": 1, "place": 1, "show": 0})
    _check("pot 10 -> place 2.5 is 3 (round() says 2)", prizes_for(10, SPLIT)["place"] == 3 and round(10 * 0.25) == 2)
    _check("pot 30 -> show 4.5 is 5 (round() says 4)", prizes_for(30, SPLIT)["show"] == 5 and round(30 * 0.15) == 4)
    _check("pot 154.0 (a float pot) is the same as 154", prizes_for(154.0, SPLIT) == prizes_for(154, SPLIT))
    _check("every pot 0..400 sums exactly to itself",
           all(sum(prizes_for(pot, SPLIT).values()) == pot for pot in range(0, 401)))
    _check("a fractional pot rounds half up before win is taken: 3.5 -> 4 = 2 + 1 + 1",
           prizes_for(3.5, SPLIT) == {"win": 2, "place": 1, "show": 1}, str(prizes_for(3.5, SPLIT)))
    for junk in (float("inf"), float("nan"), "x", None):
        _check(f"prizes_for({junk!r}) never raises: 0/0/0", prizes_for(junk, SPLIT) == {"win": 0, "place": 0, "show": 0})
    _check("a missing split key is 0/0/0 too", prizes_for(10, {}) == {"win": 0, "place": 0, "show": 0})
    b, wall, _ = fresh_board()
    m = b.model()
    _check("the empty model carries the split, zero prizes and the chyron",
           m["split"] == SPLIT and m["prizes"] == {"win": 0, "place": 0, "show": 0}
           and m["chyron"] == DEFAULTS["LQ_CHYRON_LINES"] and m["closes_at"] is None and m["names_rev"] == 0
           and m["scratches"] == [], str(m))
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=154, online=True)]))
    m = b.model()
    _check("pot 154 on the board -> the prizes", m["pot"] == 154.0 and m["prizes"] == {"win": 92, "place": 39, "show": 23})
    b2, _, _ = fresh_board(LQ_SPLIT_WIN=0.5, LQ_SPLIT_PLACE=0.3, LQ_SPLIT_SHOW=0.2, TOKEN_VALUE=2)
    b2.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=5, online=True)]))
    m = b2.model()
    _check("split from settings, pot 10 at $2 a token -> 5 / 3 / 2",
           m["split"] == {"win": 0.5, "place": 0.3, "show": 0.2} and m["pot"] == 10.0
           and m["prizes"] == {"win": 5, "place": 3, "show": 2}, str((m["split"], m["pot"], m["prizes"])))


def test_now_is_stamped_at_serialisation_only():
    b, wall, _ = fresh_board()
    q = b.subscribe()
    _check("first snapshot published", b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1, online=True)])))
    published = json.loads(q.get_nowait())
    _check("the published model carries now = the server clock", published["now"] == wall.t, str(published.get("now")))
    m = b.model()
    _check("model() carries now", m["now"] == wall.t and m["updated"] == wall.t)
    _check("model_json() carries now, compact", b.model_json() == json.dumps(m, separators=(",", ":")))
    _check("the stored model never holds now", "now" not in b._model and '"now"' not in b._json)
    wall.advance(3)
    _check("the same snapshot 3 s later publishes nothing",
           b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1, online=True)])) is False and q.qsize() == 0)
    m = b.model()
    _check("...but now moved on and updated did not", m["now"] == wall.t and m["updated"] == wall.t - 3)
    gen = sse_events(b, heartbeat_s=0.05, wall=wall)
    first = next(gen)
    _check("the SSE first chunk carries now", json.loads(first[6:])["now"] == wall.t)
    gen.close()


def test_names_and_replacement_scratch_in_the_model():
    b, wall, log_dir = fresh_board()
    picture = snap(cups=[cup_entry(1, horse=9, count=12, online=True), cup_entry(2, horse=3, count=5, online=True)])
    b.apply_snapshot(picture)
    q = b.subscribe()
    _, lines = log_lines(log_dir)
    n_lines = len(lines)
    _check("no names yet: every name empty, names_rev 0, no scratches",
           all(h["name"] == "" and h["replaced"] is None for h in b.model()["horses"].values())
           and b.model()["names_rev"] == 0 and b.model()["scratches"] == [])
    _check("the board's store is memory-only without a bridge", b.store._db is None)
    b._wake.clear()
    _check("set_names bumps names_rev and wakes the board",
           b.store.set_names({9: "  Encino ", 3: "Fierceness"}) is True and b.store.names_rev == 1 and b._wake.is_set())
    _check("the same names again change nothing", b.store.set_names({9: "Encino"}) is False and b.store.names_rev == 1)
    _check("stored as typed (stripped)", b.store.horses()[9] == {"name": "Encino", "replaced": None})
    wall.advance(1)
    _check("the next apply of the SAME snapshot is a change (names differ)", b.apply_snapshot(picture) is True)
    m = b.model()
    _check("served upper-cased", m["horses"]["9"]["name"] == "ENCINO" and m["horses"]["3"]["name"] == "FIERCENESS")
    _check("names_rev in the model", m["names_rev"] == 1)
    _check("published once", q.qsize() == 1)
    _check("...with no events: names are not bets", m["events"] == [], str(m["events"]))
    _check("...and no log line: nothing about tokens moved", len(log_lines(log_dir)[1]) == n_lines)
    done = b.store.scratch_replace(9, " Epic Ride ")
    _check("scratch_replace returns was / now as typed", done == {"was": "Encino", "now": "Epic Ride"}, str(done))
    b.apply_snapshot(picture)
    m = b.model()
    _check("horse 9: name EPIC RIDE, replaced ENCINO, tokens kept, not scratched at the gateway",
           m["horses"]["9"] == {"tokens": 12, "share": round(12 / 17, 4), "scratched": False, "online": True,
                                "cup": 1, "name": "EPIC RIDE", "replaced": "ENCINO"}, str(m["horses"]["9"]))
    _check("scratches lists it, upper-cased", m["scratches"] == [{"horse": 9, "was": "ENCINO", "now": "EPIC RIDE"}])
    _check("the pot still counts the cup: 17, prizes 10 / 4 / 3", m["pot"] == 17.0 and m["total_tokens"] == 17
           and m["prizes"] == {"win": 10, "place": 4, "show": 3}, str((m["pot"], m["prizes"])))
    _check("names_rev 2, still no events, still no log line",
           m["names_rev"] == 2 and m["events"] == [] and len(log_lines(log_dir)[1]) == n_lines)
    wall.advance(1)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=9, count=13, online=True), cup_entry(2, horse=3, count=5, online=True)]))
    _check("a real drop after the scratch is still an event (same cup, same roster)",
           b.model()["events"] == [{"horse": 9, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    _check("a replacement of a horse with no name: was ''", b.store.scratch_replace(1, "Late Entry") == {"was": "", "now": "Late Entry"})
    b.apply_snapshot(picture)
    _check("...shows in scratches with was ''", {"horse": 1, "was": "", "now": "LATE ENTRY"} in b.model()["scratches"])
    _check("unscratch_replace restores the name", b.store.unscratch_replace(9) is True
           and b.store.horses()[9] == {"name": "Encino", "replaced": None})
    _check("unscratch_replace with nothing to undo is False", b.store.unscratch_replace(9) is False)
    b.store.unscratch_replace(1)
    b.apply_snapshot(picture)
    m = b.model()
    _check("model back to ENCINO, no scratches", m["horses"]["9"]["name"] == "ENCINO"
           and m["horses"]["9"]["replaced"] is None and m["scratches"] == [] and m["names_rev"] == 5)
    for bad in ((0, "x"), (21, "x"), ("x", "x"), (9, ""), (9, "   "), (9, 5), (9, "x" * 81)):
        try:
            b.store.scratch_replace(*bad)
            _check(f"scratch_replace{bad!r:.30} rejected", False)
        except ValueError:
            _check(f"scratch_replace{bad!r:.30} rejected", True)
    for bad in ({0: "x"}, {"21": "x"}, {9: 5}, {9: "x" * 81}):
        try:
            b.store.set_names(bad)
            _check(f"set_names({bad!r:.30}) rejected", False)
        except ValueError:
            _check(f"set_names({bad!r:.30}) rejected", True)
    _check("nothing was written by the rejected calls", b.store.names_rev == 5)
    # An injected store is used as given, and the empty model already carries its names.
    store = HorseStore()
    store.set_names({4: "Catching Freedom"})
    store.set_closes_at(1_700_000_900.0)
    b3 = BettingBoard(store=store, wall=FakeClock(1_700_000_000.0))
    _check("an injected store is the board's store", b3.store is store and store.on_change == b3.wake)
    m = b3.model()
    _check("the empty model already carries the store's names and closing time",
           m["horses"]["4"]["name"] == "CATCHING FREEDOM" and m["closes_at"] == 1_700_000_900.0 and m["names_rev"] == 1)


def test_parse_names_text():
    names = parse_names_text(DERBY_TEXT)
    _check("20 prefixed lines -> 20 names", names == {n: name for n, name in enumerate(DERBY_2024, 1)}, str(names))
    plain = parse_names_text("\n".join(DERBY_2024))
    _check("20 plain lines -> post-position order", plain == names)
    mixed = parse_names_text("Dornoch\n\n#3 Mystik Dan\n7 Honor Marie\n5) Fierce\n6: Just A Touch\n20.Society Man\n  12 .  Track Phantom ")
    _check("line order, blank leaves alone, every punctuated prefix wins over line order, and in a list that is "
           "not consistently numbered a bare '7 Honor Marie' is line 4's name",
           mixed == {1: "Dornoch", 3: "Mystik Dan", 4: "7 Honor Marie", 5: "Fierce", 6: "Just A Touch",
                     20: "Society Man", 12: "Track Phantom"}, str(mixed))
    bare = parse_names_text("\n".join(f"{n} {name}" for n, name in enumerate(DERBY_2024, 1)))
    _check("a consistently numbered list with bare 'N name' lines: the numbers are prefixes", bare == names, str(bare))
    bare_mixed = parse_names_text("1. Dornoch\n\n3 Mystik Dan\n#7 Honor Marie\n5")
    _check("every line numbered, any prefix form (blank lines allowed): bare 'N name' still a prefix",
           bare_mixed == {1: "Dornoch", 3: "Mystik Dan", 7: "Honor Marie", 5: ""}, str(bare_mixed))
    belles = parse_names_text("Dornoch\n8 Belles\nSierra Leone")
    _check("in a plain post-order list '8 Belles' is horse 2's name, not horse 8's",
           belles == {1: "Dornoch", 2: "8 Belles", 3: "Sierra Leone"}, str(belles))
    _check("...and a punctuated prefix in that same list still wins", parse_names_text("Dornoch\n8. Belles") == {1: "Dornoch", 8: "Belles"})
    _check("a bare number clears that horse's name", parse_names_text("9.") == {9: ""} and parse_names_text("9") == {9: ""})
    _check("a bare number alone on a line clears even in a plain list", parse_names_text("Dornoch\n9\nMystik") == {1: "Dornoch", 9: "", 3: "Mystik"})
    _check("7UP is a name, not a prefix", parse_names_text("7UP") == {1: "7UP"})
    _check("a year is a name too", parse_names_text("2024 Derby") == {1: "2024 Derby"})
    _check("inner whitespace folded, ends stripped", parse_names_text("  Sierra   Leone  ") == {1: "Sierra Leone"})
    _check("empty text -> nothing to change", parse_names_text("") == {} and parse_names_text("\n\n") == {})
    _check("blank lines past the 20th are fine", parse_names_text("\n".join(DERBY_2024) + "\n\n\n") == plain)
    gap = "\n".join(name if n != 3 else "" for n, name in enumerate(DERBY_2024, 1))
    _check("prefixed lines past the 20th are fine", parse_names_text(gap + "\n3. Mystik")[3] == "Mystik")
    for bad, why in (("\n".join(DERBY_2024) + "\nExtra", "more than 20"), ("21. X", "not in 1-20"), ("0. X", "not in 1-20"),
                     ("#0 X", "not in 1-20"), ("3. A\n3. B", "named twice"), ("A\n1. B", "named twice"),
                     (5, "must be a string"), ("x" * 81, "longer than 80")):
        try:
            parse_names_text(bad)
            _check(f"{bad!r:.30} -> ValueError {why}", False)
        except ValueError as exc:
            _check(f"{bad!r:.30} -> ValueError {why}", why in str(exc), str(exc))


def test_second_replacement_keeps_the_scratched_horse():
    b, wall, log_dir = fresh_board()
    picture = snap(cups=[cup_entry(1, horse=9, count=12, online=True)])
    b.apply_snapshot(picture)
    b.store.set_names({9: "Encino"})
    done = b.store.scratch_replace(9, "Epic Ride")
    _check("first replacement: was Encino", done == {"was": "Encino", "now": "Epic Ride"}, str(done))
    done = b.store.scratch_replace(9, "Third Horse")
    _check("second replacement: was is still Encino, the horse actually scratched",
           done == {"was": "Encino", "now": "Third Horse"}, str(done))
    _check("stored: name Third Horse, replaced Encino (Epic Ride never ran and is dropped)",
           b.store.horses()[9] == {"name": "Third Horse", "replaced": "Encino"}, str(b.store.horses()[9]))
    _check("names_rev bumped twice", b.store.names_rev == 3)
    b.apply_snapshot(picture)
    m = b.model()
    _check("the chyron pair is ENCINO -> THIRD HORSE", m["scratches"] == [{"horse": 9, "was": "ENCINO", "now": "THIRD HORSE"}]
           and m["horses"]["9"]["replaced"] == "ENCINO", str(m["scratches"]))
    _check("undo goes straight back to Encino", b.store.unscratch_replace(9) is True
           and b.store.horses()[9] == {"name": "Encino", "replaced": None})
    _check("nothing left to undo", b.store.unscratch_replace(9) is False)
    # An unnamed horse: "" marks it as replaced, and a second replacement keeps that "".
    done = b.store.scratch_replace(3, "Late Entry")
    _check("unnamed horse replaced: was ''", done == {"was": "", "now": "Late Entry"})
    done = b.store.scratch_replace(3, "Later Entry")
    _check("...and again: was '' still, so undo still works", done == {"was": "", "now": "Later Entry"}
           and b.store.horses()[3] == {"name": "Later Entry", "replaced": ""})
    _check("undo of an unnamed horse clears the name", b.store.unscratch_replace(3) is True
           and b.store.horses()[3] == {"name": "", "replaced": None})
    # Through the routes, on a real bridge and database.
    br, port, sio, clk = _fresh_bridge()
    br._open_port()
    client = _make_board_app(br).test_client()
    br.set_state(1, HORSES_1_TO_20, NO_SCR)
    client.put("/api/quiniela/horses", json={"text": DERBY_TEXT})
    client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": "Epic Ride"})
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": "Third Horse"})
    body = r.get_json()
    _check("POST scratch twice -> 200, was Encino both times", r.status_code == 200 and body["was"] == "Encino"
           and body["now"] == "Third Horse" and body["names_rev"] == 3, str(body))
    m = client.get("/api/quiniela").get_json()
    _check("model: THIRD HORSE replacing ENCINO", m["horses"]["9"] ["name"] == "THIRD HORSE"
           and m["horses"]["9"]["replaced"] == "ENCINO" and m["scratches"] == [{"horse": 9, "was": "ENCINO", "now": "THIRD HORSE"}])
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("unscratch restores Encino in one step", r.status_code == 200 and r.get_json()["name"] == "Encino"
           and HorseStore(br.db).horses()[9] == {"name": "Encino", "replaced": None}, str(r.get_json()))


def test_kind2_scratch_removes_tokens_from_the_pot():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    _check("the board's store lives on the bridge's database", board.store._db is b.db)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b.handle_raw_line(telem(8, MAC_A, count=10))         # wire 8 -> cup 9 -> horse 9
    b.handle_raw_line(telem(2, MAC_B, count=5))          # wire 2 -> cup 3 -> horse 3
    board.refresh()
    m = board.model()
    _check("pot 15 of 15 tokens, prizes 9 / 4 / 2", m["pot"] == 15.0 and m["total_tokens"] == 15
           and m["prizes"] == {"win": 9, "place": 4, "show": 2}, str((m["pot"], m["prizes"])))
    scr = [1 if cup == 9 else 0 for cup in range(1, 21)]
    b.set_state(1, HORSES_1_TO_20, scr)                  # the gateway kind: the cup's scratched flag
    board.refresh()
    m = board.model()
    _check("horse 9 scratched at the gateway", m["horses"]["9"]["scratched"] is True and m["horses"]["9"]["tokens"] == 10)
    _check("its tokens leave the pot: 5, prizes 3 / 1 / 1", m["pot"] == 5.0 and m["prizes"] == {"win": 3, "place": 1, "show": 1},
           str((m["pot"], m["prizes"])))
    _check("total_tokens still counts every cup: 15", m["total_tokens"] == 15)
    _check("share unchanged (of every cup)", m["horses"]["9"]["share"] == round(10 / 15, 4))
    _check("a gateway scratch is not a replacement: no scratches entry, no name change, and names_rev (the "
           "names store's revision) stays put; the bridge's state rev is what moved",
           m["scratches"] == [] and m["horses"]["9"]["replaced"] is None and m["names_rev"] == 0)
    _check("no event for the scratch", m["events"] == [], str(m["events"]))
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    board.refresh()
    _check("unscratched at the gateway: the pot is back", board.model()["pot"] == 15.0)


def test_reset_clears_closes_at_and_keeps_names():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    _check("reset_count starts at 0 in the snapshot", b.get_snapshot()["devpi"]["reset_count"] == 0)
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b.handle_raw_line(telem(8, MAC_A, count=10))
    board.refresh()
    board.store.set_names({9: "Encino", 3: "Fierceness"})
    board.store.scratch_replace(3, "Late Entry")
    board.store.set_closes_at(1_700_000_600.0)
    board.refresh()
    m = board.model()
    _check("before the reset: names, a replacement, closes_at", m["horses"]["9"]["name"] == "ENCINO"
           and m["scratches"] == [{"horse": 3, "was": "FIERCENESS", "now": "LATE ENTRY"}] and m["closes_at"] == 1_700_000_600.0)
    _check("persisted: a second store on the same db reads them", HorseStore(b.db).closes_at == 1_700_000_600.0
           and HorseStore(b.db).horses()[9]["name"] == "Encino" and HorseStore(b.db).names_rev == 2)
    b.reset_link("test")
    _check("reset_count moved", b.get_snapshot()["devpi"]["reset_count"] == 1)
    board.refresh()
    m = board.model()
    _check("after the reset: closes_at cleared", m["closes_at"] is None and board.store.closes_at is None)
    _check("...in the database too", HorseStore(b.db).closes_at is None)
    _check("...names and the replacement stay, names_rev untouched", m["horses"]["9"]["name"] == "ENCINO"
           and m["scratches"] == [{"horse": 3, "was": "FIERCENESS", "now": "LATE ENTRY"}] and m["names_rev"] == 2)
    _check("...PRE_RACE, no cups, no ghosts", m["race_state"] == 0 and m["events"] == [] and m["pot"] == 0.0)
    board.store.set_closes_at(1_700_000_900.0)
    board.refresh()
    _check("a second refresh without a reset keeps a new closes_at", board.model()["closes_at"] == 1_700_000_900.0)
    # DevPi's database has the old four tables: check_shape passes and init_schema adds the two.
    path = str(tmpdir() / "old.db")
    db = LqDb(path)
    db.init_schema()
    db.conn.executescript("DROP TABLE lq_horses; DROP TABLE lq_board;")
    _check("check_shape() passes on a database without the new tables", db.check_shape() is None)
    db.init_schema()
    _check("init_schema() adds them, empty", db.load_horses() == {} and db.load_board() == {"names_rev": 0, "closes_at": None})
    db.save_horse(9, "Encino", None)
    db.save_horse(9, "Epic Ride", "Encino")
    db.save_board(3, 12.5)
    _check("save_horse upserts, save_board updates", db.load_horses() == {9: {"name": "Epic Ride", "replaced": "Encino"}}
           and db.load_board() == {"names_rev": 3, "closes_at": 12.5})
    _check("a store loads them", HorseStore(db).horses()[9] == {"name": "Epic Ride", "replaced": "Encino"}
           and HorseStore(db).names_rev == 3 and HorseStore(db).closes_at == 12.5)
    db.conn.executescript("DROP TABLE lq_horses; DROP TABLE lq_board;")
    with capture_logs("la_quiniela.horses", logging.ERROR) as cap:
        store = HorseStore(db)
    _check("a database without the tables leaves a memory-only store and one ERROR",
           store._db is None and len(cap.messages("cannot read")) == 1 and store.set_names({1: "x"}) is True)
    db.close()


def test_routes_horses_get_and_put():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    r = client.get("/api/quiniela/horses")
    _check("GET /api/quiniela/horses 200, no-store", r.status_code == 200 and r.headers.get("Cache-Control") == "no-store")
    _check("20 entries, empty", r.get_json() == {str(n): {"name": "", "replaced": None} for n in range(1, 21)}, str(r.get_json()))
    r = client.put("/api/quiniela/horses", json={"text": DERBY_TEXT})
    body = r.get_json()
    _check("PUT text -> ok, names_rev 1, horses as typed", r.status_code == 200 and body["ok"] is True and body["names_rev"] == 1
           and body["horses"]["9"] == {"name": "Encino", "replaced": None} and body["horses"]["17"]["name"] == "Fierceness", str(body))
    m = client.get("/api/quiniela").get_json()
    _check("the model follows synchronously, upper-cased", m["horses"]["9"]["name"] == "ENCINO" and m["names_rev"] == 1)
    _check("GET round trip as typed", client.get("/api/quiniela/horses").get_json() == body["horses"])
    r = client.put("/api/quiniela/horses", json={"text": "Dornoch\n\n#3 Mystik Dan II\n7 Honor Marie II\n5) Fierce\n6: Just A Touch\n20. Society Man II"})
    h = r.get_json()["horses"]
    _check("line order, blank line leaves horse 2, punctuated prefixes win over line order, and in this plain "
           "(not consistently numbered) list a bare '7 Honor Marie II' is line 4's name", r.status_code == 200
           and h["1"]["name"] == "Dornoch" and h["2"]["name"] == "Sierra Leone" and h["3"]["name"] == "Mystik Dan II"
           and h["4"]["name"] == "7 Honor Marie II" and h["7"]["name"] == "Honor Marie" and h["5"]["name"] == "Fierce"
           and h["6"]["name"] == "Just A Touch" and h["20"]["name"] == "Society Man II", str(h))
    _check("names_rev 2", r.get_json()["names_rev"] == 2)
    r = client.put("/api/quiniela/horses", json={"text": "1. Dornoch"})
    _check("an unchanged name does not bump names_rev", r.status_code == 200 and r.get_json()["names_rev"] == 2)
    for bad, why in (({"text": "\n".join(DERBY_2024) + "\nExtra"}, "more than 20"), ({"text": "21. X"}, "not in 1-20"),
                     ({"text": "0. X"}, "not in 1-20"), ({"text": 5}, "must be a string"), ({"1": {"nope": 1}}, "expected"),
                     ({"1": "Dornoch"}, "expected"), ({"0": {"name": "x"}}, "not in 1-20"), ({"1": {"name": 5}}, "must be a string"),
                     ({"1": {"name": "x", "replaced": 5}}, "must be a string")):
        r = client.put("/api/quiniela/horses", json=bad)
        _check(f"PUT {bad!r:.40} -> 400 {why}", r.status_code == 400 and r.get_json()["ok"] is False
               and why in r.get_json()["error"], f"{r.status_code} {r.get_json()}")
    r = client.put("/api/quiniela/horses", data="[]", content_type="application/json")
    _check("a non-object body -> 400", r.status_code == 400)
    _check("nothing changed by the 400s", client.get("/api/quiniela/horses").get_json() == h)
    r = client.put("/api/quiniela/horses", json={"1": {"name": "Dornoch II", "replaced": "Dornoch"}, "2": {"name": "Sierra Leone"}})
    _check("the dict shape: name required, replaced optional", r.status_code == 200
           and r.get_json()["horses"]["1"] == {"name": "Dornoch II", "replaced": "Dornoch"}
           and r.get_json()["horses"]["2"] == {"name": "Sierra Leone", "replaced": None}, str(r.get_json()))
    m = client.get("/api/quiniela").get_json()
    _check("...and the model lists the replacement", m["scratches"] == [{"horse": 1, "was": "DORNOCH", "now": "DORNOCH II"}])
    r = client.put("/api/quiniela/horses", json={"1": {"name": "Dornoch", "replaced": None}})
    _check("replaced null clears it", r.get_json()["horses"]["1"] == {"name": "Dornoch", "replaced": None}
           and client.get("/api/quiniela").get_json()["scratches"] == [])
    _check("persisted on the bridge's database", HorseStore(b.db).horses()[3]["name"] == "Mystik Dan II")


def test_routes_scratch_and_unscratch():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    b.set_state(1, HORSES_1_TO_20, NO_SCR)
    b.handle_raw_line(telem(8, MAC_A, count=4))          # cup 9 -> horse 9
    client.put("/api/quiniela/horses", json={"text": DERBY_TEXT})
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": "Epic Ride"})
    body = r.get_json()
    _check("scratch with a replacement -> kind replacement", r.status_code == 200
           and body == {"ok": True, "kind": "replacement", "horse": 9, "was": "Encino", "now": "Epic Ride", "names_rev": 2}, str(body))
    _check("nothing went to the gateway", port.written == [])
    m = client.get("/api/quiniela").get_json()
    _check("the model: EPIC RIDE replacing ENCINO, tokens kept, not scratched, pot 4",
           m["horses"]["9"]["name"] == "EPIC RIDE" and m["horses"]["9"]["replaced"] == "ENCINO"
           and m["horses"]["9"]["scratched"] is False and m["horses"]["9"]["tokens"] == 4 and m["pot"] == 4.0
           and m["scratches"] == [{"horse": 9, "was": "ENCINO", "now": "EPIC RIDE"}], str(m["horses"]["9"]))
    _check("no event", m["events"] == [], str(m["events"]))
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("unscratch undoes the replacement first", r.status_code == 200 and r.get_json()["kind"] == "replacement"
           and r.get_json()["name"] == "Encino", str(r.get_json()))
    m = client.get("/api/quiniela").get_json()
    _check("the model: ENCINO back, no scratches", m["horses"]["9"]["name"] == "ENCINO" and m["scratches"] == [])
    for body in ({"horse": 9}, {"horse": 9, "replacement": ""}, {"horse": 9, "replacement": None}, {"horse": "9", "replacement": "  "}):
        r = client.post("/api/quiniela/scratch", json=body)
        got = r.get_json()
        _check(f"{body!r:.45} -> kind gateway on cup 9", r.status_code == 200 and got["ok"] is True and got["kind"] == "gateway"
               and got["cup"] == 9 and got["horse"] == 9 and got["scratched"] is True and got["gateway_online"] is True, str(got))
    scr = [False] * 20
    scr[8] = True
    _check("one state line went down (the repeats were no-ops), byte-exact",
           port.lines() == [state_line(2, 1, HORSES_1_TO_20, scr)], str(port.lines()))
    m = client.get("/api/quiniela").get_json()
    _check("the model: scratched, tokens out of the pot", m["horses"]["9"]["scratched"] is True and m["horses"]["9"]["tokens"] == 4
           and m["total_tokens"] == 4 and m["pot"] == 0.0 and m["prizes"] == {"win": 0, "place": 0, "show": 0})
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    got = r.get_json()
    _check("unscratch clears the gateway flag", r.status_code == 200 and got["kind"] == "gateway" and got["scratched"] is False
           and got["rev"] == 3 and port.lines()[-1] == state_line(3, 1, HORSES_1_TO_20, [False] * 20), str(got))
    _check("the pot is back", client.get("/api/quiniela").get_json()["pot"] == 4.0)
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("neither kind applies -> 400", r.status_code == 400 and r.get_json() == {"ok": False, "error": "horse 9 is not scratched"})
    b.set_state(1, [0 if h == 15 else h for h in HORSES_1_TO_20], NO_SCR)
    r = client.post("/api/quiniela/scratch", json={"horse": 15})
    _check("a horse on no cup -> 400", r.status_code == 400 and r.get_json()["error"] == "horse 15 is not on any cup")
    r = client.post("/api/quiniela/scratch", json={"horse": 15, "replacement": "Sub"})
    _check("...but a replacement scratch needs no cup", r.status_code == 200 and r.get_json()["kind"] == "replacement")
    for bad in ({"horse": 0}, {"horse": 21}, {"horse": "x"}, {"horse": True}, {"horse": None}, {}, [], "x",
                {"horse": 9, "replacement": 5}):
        for path in ("/api/quiniela/scratch", "/api/quiniela/unscratch"):
            if path.endswith("unscratch") and bad == {"horse": 9, "replacement": 5}:
                continue
            r = client.post(path, json=bad)
            _check(f"POST {path[14:]} {bad!r:.30} -> 400", r.status_code == 400 and r.get_json()["ok"] is False, f"{r.status_code} {r.get_json()}")
    get_board().bridge = None
    r = client.post("/api/quiniela/scratch", json={"horse": 9})
    _check("gateway kind without a bridge -> 503", r.status_code == 503 and r.get_json()["error"] == "bridge not initialised")
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": "Epic Ride"})
    _check("a replacement scratch needs no bridge", r.status_code == 200 and r.get_json()["kind"] == "replacement")
    _check("...and the model followed without a bridge", client.get("/api/quiniela").get_json()["horses"]["9"]["name"] == "EPIC RIDE")
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("...and is undone without one", r.status_code == 200 and r.get_json()["kind"] == "replacement")
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("nothing left to undo without a bridge -> 400", r.status_code == 400)


def test_routes_closes_at():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    wall = FakeClock(1_700_000_000.0)
    client = _make_board_app(b, wall=wall).test_client()
    r = client.put("/api/quiniela/closes_at", json={"at": 1_700_000_500})
    _check("PUT at -> ok", r.status_code == 200 and r.get_json() == {"ok": True, "closes_at": 1_700_000_500.0}, str(r.get_json()))
    _check("the model follows", client.get("/api/quiniela").get_json()["closes_at"] == 1_700_000_500.0)
    _check("persisted: a second store on the same db", HorseStore(b.db).closes_at == 1_700_000_500.0)
    r = client.put("/api/quiniela/closes_at", json={"in_minutes": 30})
    _check("PUT in_minutes 30 -> now + 1800 by the server's clock", r.get_json() == {"ok": True, "closes_at": wall.t + 1800.0}, str(r.get_json()))
    _check("names_rev untouched by the closing time", client.get("/api/quiniela").get_json()["names_rev"] == 0)
    r = client.put("/api/quiniela/closes_at", json={"at": None})
    _check("PUT at null clears", r.get_json() == {"ok": True, "closes_at": None}
           and client.get("/api/quiniela").get_json()["closes_at"] is None and HorseStore(b.db).closes_at is None)
    for bad in ({}, {"at": "x"}, {"at": True}, {"in_minutes": -1}, {"in_minutes": "x"}, {"in_minutes": None}, {"when": 5}):
        r = client.put("/api/quiniela/closes_at", json=bad)
        _check(f"PUT {bad!r:.30} -> 400 usage", r.status_code == 400 and r.get_json() == {"ok": False, "error": USAGE_CLOSES_AT},
               f"{r.status_code} {r.get_json()}")
    r = client.put("/api/quiniela/closes_at", data="5", content_type="application/json")
    _check("a non-object body -> 400", r.status_code == 400)
    r = client.put("/api/quiniela/closes_at", json={"at": 1e400})
    _check("an infinite time -> 400", r.status_code == 400 and "finite" in r.get_json()["error"], str(r.get_json()))


def test_admin_page():
    b, port, sio, clk = _fresh_bridge()
    client = _make_board_app(b).test_client()
    r = client.get("/quiniela/admin")
    _check("GET /quiniela/admin 200 text/html", r.status_code == 200 and r.mimetype == "text/html")
    html = r.get_data(as_text=True)
    for section in ("names", "scratches", "closes", "status"):
        _check(f"section id={section!r} present", f'id="{section}"' in html)
    _check("viewport meta for phones", 'name="viewport"' in html)
    _check("talks to the routes", all(path in html for path in ("/api/quiniela/horses", "/api/quiniela/scratch",
                                                                "/api/quiniela/unscratch", "/api/quiniela/closes_at", "/api/quiniela")))
    _check("no CDN, no external script", "<script src=" not in html and "https://" not in html and "http://" not in html)
    _check("no race-state buttons", "/api/quiniela/cmd" not in html)
    _check("well under 400 lines", html.count("\n") < 400, str(html.count("\n")))


def test_settings_new_keys():
    saved_env = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    try:
        with fake_config(LQ_SPLIT_WIN=0.5, LQ_SPLIT_PLACE=0.3, LQ_SPLIT_SHOW="0.2", LQ_CHYRON_LINES=("A", " B ", "")):
            s = load_board_settings()
        _check("splits from config.py, a string coerced", (s["LQ_SPLIT_WIN"], s["LQ_SPLIT_PLACE"], s["LQ_SPLIT_SHOW"]) == (0.5, 0.3, 0.2))
        _check("chyron from a tuple, stripped, empties dropped", s["LQ_CHYRON_LINES"] == ["A", "B"])
        os.environ["DDM_LQ_SPLIT_PLACE"] = "0.30"
        os.environ["DDM_LQ_SPLIT_SHOW"] = "0.10"
        os.environ["DDM_LQ_CHYRON_LINES"] = " ONE | TWO ||THREE "
        with fake_config():
            s = load_board_settings()
        _check("env splits", s["LQ_SPLIT_PLACE"] == 0.3 and s["LQ_SPLIT_SHOW"] == 0.1 and s["LQ_SPLIT_WIN"] == 0.6)
        _check("env chyron split on |", s["LQ_CHYRON_LINES"] == ["ONE", "TWO", "THREE"])
        os.environ["DDM_LQ_CHYRON_LINES"] = ""
        _check("an empty env chyron is an empty list", load_board_settings()["LQ_CHYRON_LINES"] == [])
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        with capture_logs(LOGGER) as cap:
            with fake_config(LQ_SPLIT_WIN=1.5, LQ_SPLIT_PLACE=-0.1, LQ_SPLIT_SHOW=True, LQ_CHYRON_LINES=[1, 2]):
                s = load_board_settings()
        _check("junk splits and chyron keep the defaults", s == DEFAULTS, str(s))
        _check("...warned once each", len(cap.messages("ignoring config.py")) == 4, str(cap.messages()))
        betting._split_warned.clear()
        with capture_logs(LOGGER) as cap:
            with fake_config(LQ_SPLIT_WIN=0.5):
                load_board_settings()
                load_board_settings()
        _check("splits that do not sum to 1 warn once", len(cap.messages("not 1")) == 1, str(cap.messages()))
        with capture_logs(LOGGER) as cap:
            with fake_config(LQ_SPLIT_WIN=0.6004):
                load_board_settings()
        _check("within 0.001 is fine", cap.messages("not 1") == [])
        _check("the real pi5/config.py has the keys and they sum to 1",
               abs(sum(load_board_settings()[k] for k in betting.SPLIT_KEYS) - 1.0) < 1e-9)
    finally:
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    print(f"La Quiniela betting board test\n  DB: {S._TMP_DB}")

    _run("model — empty snapshot shape", test_empty_snapshot_model_shape)
    _run("model — tokens, share, leader, online, scratched", test_tokens_share_leader_online_scratched)
    _run("model — leader None / lowest on tie", test_leader_none_without_tokens_and_lowest_on_tie)
    _run("model — odd entries never raise", test_odd_entries_never_raise)
    _run("model — events newest first, last eight", test_events_diff_newest_first_last_eight)
    _run("model — duplicate horse keeps lowest cup, warns once", test_duplicate_horse_keeps_lowest_cup_and_warns_once)
    _run("model — race state names", test_race_state_names)
    _run("model — link_ok follows the snapshot", test_link_ok_follows_the_snapshot)
    _run("model — unchanged snapshot not published", test_unchanged_snapshot_is_not_published)
    _run("model — subscriber queue drops the oldest", test_subscriber_queue_drops_oldest_when_full)
    _run("model — token value and board states from settings", test_token_value_and_board_states_from_settings)
    _run("log — one line per model change", test_log_writes_one_line_per_model_change)
    _run("log — disabled by settings", test_log_disabled_by_settings)
    _run("log — write failure warns once and disables", test_log_write_failure_warns_once_and_disables)
    _run("log — module LOG_DIR is the default", test_log_module_dir_is_the_default)
    _run("settings resolution", test_settings_resolution)
    _run("settings — a huge int never raises", test_huge_int_settings_never_raise)
    _run("bridge — a real bridge feeds the board", test_real_bridge_feeds_the_board)
    _run("bridge — listener hook", test_listener_hook)
    _run("bridge — the board thread picks up changes", test_board_thread_picks_up_changes)
    _run("bridge — refresh() is serialised", test_refresh_is_serialised)
    _run("cmd — validate_cmd", test_validate_cmd)
    _run("cmd — whitelist 400s", test_cmd_whitelist_400s)
    _run("cmd — without a bridge is 503", test_cmd_without_bridge_is_503)
    _run("cmd — state", test_cmd_state)
    _run("cmd — horse / scratch, 1-based cups", test_cmd_horse_and_scratch_1_based)
    _run("model — reset and re-mapping produce no ghost bets", test_reset_and_remap_produce_no_ghost_bets)
    _run("cmd — roster", test_cmd_roster)
    _run("cmd — demo / json refused", test_cmd_demo_and_json_refused)
    _run("cmd — usage errors", test_cmd_usage_errors)
    _run("routes — GET /api/quiniela", test_model_route)
    _run("routes — the model follows the bridge", test_model_route_follows_the_bridge)
    _run("routes — SSE generator", test_stream_generator)
    _run("routes — GET /api/quiniela/stream headers, first event, ping", test_stream_route_headers_first_event_and_ping)
    _run("routes — init_board / start_board / stop_board", test_init_and_start_board)
    _run("payout — round_half_up and prizes_for", test_round_half_up_and_prizes)
    _run("payout — now is stamped at serialisation only", test_now_is_stamped_at_serialisation_only)
    _run("payout — names and a replacement scratch in the model", test_names_and_replacement_scratch_in_the_model)
    _run("payout — parse_names_text", test_parse_names_text)
    _run("payout — a second replacement keeps the scratched horse", test_second_replacement_keeps_the_scratched_horse)
    _run("payout — a gateway scratch takes its tokens out of the pot", test_kind2_scratch_removes_tokens_from_the_pot)
    _run("payout — reset clears closes_at, keeps names; the tables", test_reset_clears_closes_at_and_keeps_names)
    _run("payout — GET/PUT /api/quiniela/horses", test_routes_horses_get_and_put)
    _run("payout — POST /api/quiniela/scratch and /unscratch", test_routes_scratch_and_unscratch)
    _run("payout — PUT /api/quiniela/closes_at", test_routes_closes_at)
    _run("payout — GET /quiniela/admin", test_admin_page)
    _run("settings — the payout keys", test_settings_new_keys)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)

    stop_board()
    if S._current is not None:
        try:
            S._current.close()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(S._TMP_DB + suffix)
        except OSError:
            pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

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
#
# Protocol v2: a cup is known by its MAC and the horse it reports, so a
# snapshot's cups[] entries carry mac and horse, the model's horses[n].cup
# is a MAC, and a renumber is the cup itself reporting the new number.

import datetime
import io
import json
import logging
import os
import sqlite3
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
    DEFAULTS, MAX_EVENTS, MODE_LABELS, MODE_STATES, SSE_QUEUE_SIZE, UNDO_RENUM_S, BettingBoard, clean_weather, clear_results,
    load_board_settings,
    prizes_for, read_results, round_half_up, sse_events, validate_cmd,
)
from la_quiniela.blueprint import init_la_quiniela, la_quiniela_bp  # noqa: E402
from la_quiniela.bridge import LqBridge  # noqa: E402
from la_quiniela.board import (  # noqa: E402
    DEMO_REFUSED, JSON_REFUSED, REPLACEMENT_SHAPE, USAGE_CLOSES_AT, USAGE_ODDS, USAGE_RACE, USAGE_STATE,
    get_board, init_board, migrate_race_setup, quiniela_board_bp, start_board, stop_board,
)
from la_quiniela import odds as odds_mod  # noqa: E402
from la_quiniela import racetime  # noqa: E402
from la_quiniela.horses import HorseStore, horse_at, in_field, parse_names_text, post_of  # noqa: E402
from la_quiniela.models import LqDb  # noqa: E402
from la_quiniela.test_smoke import (  # noqa: E402
    GW_MAC, MAC_A, MAC_B, MAC_C, FakeClock, _fresh_bridge, drain, hello, status, telem,
)
from la_quiniela.test_smoke import cup_entry as status_cup  # noqa: E402  (a status line's cups[] entry)

MODEL_KEYS = {"link_ok", "race_state", "race_state_name", "token_value", "pot", "total_tokens",
              "horses", "leader", "events", "updated", "board_states",
              # additive since the payout model: see "Betting board" in LQ_BRIDGE.md
              "now", "closes_at", "prizes", "split", "chyron", "names_rev", "scratches",
              # additive since protocol v2
              "cups_online", "cups_no_horse", "results",
              # additive since pi5 holds the figures at the post
              "closing",
              # additive since race info lives in La Quiniela (the race; each horse's odds too)
              "race",
              # additive with the crawl's live items: pi5's weather
              "weather",
              # additive with the counted pot: the scale pot frozen at the post, the hand count, whether there is one
              "pot_scale", "pot_counted", "hand_counted"}
LOGGER = "la_quiniela.betting"
UNASSIGNED = {"tokens": 0, "share": 0, "scratched": False, "online": False, "cup": None,
              "conflict": False, "cups": [], "name": "", "replaced": None, "in_field": False, "odds": None}
NO_RESULTS = None   # the model's results until the dashboard has them


def unassigned(n):
    """The empty entry for horse n: in the field for 1..20, not for the also-eligibles 21..24."""
    return dict(UNASSIGNED, in_field=int(n) <= 20)

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
    """A board with injected clocks, a temp log dir and a temp results file.
    Returns (board, wall, log_dir)."""
    wall = FakeClock(1_700_000_000.0)
    log_dir = tmpdir() / "logs"
    b = BettingBoard(bridge=bridge, settings=settings or None, clock=FakeClock(1000.0),
                     wall=wall, log_dir=log_dir, results_path=log_dir.parent / "results.json")
    return b, wall, log_dir


def mac_of(n):
    """A fixed fake MAC for the n-th hand-built cup."""
    return "A0:B7:65:00:00:%02X" % int(n)


def cup_entry(mac, horse=None, count=None, online=False, last_seen=None, **extra):
    """One get_snapshot()["cups"] entry, the bridge's shape: a cup by MAC
    (an int is turned into a fixed fake MAC) and the horse it reports."""
    if isinstance(mac, int):
        mac = mac_of(mac)
    d = {"mac": mac, "horse": int(horse or 0), "count": count, "raw": None, "rssi": None, "up": None,
         "drop": None, "seq": None, "online": online, "hello": False, "last_seen": last_seen}
    d.update(extra)
    return d


def snap(phase=1, cups=(), port_open=True, gateway_online=True, state_rev=1,
         scratched=(), renum=(), results=(0, 0, 0)):
    """A get_snapshot() dict: the link, DevPi's state keyed by horse, and
    the cups heard (passed through as given)."""
    return {
        "link": {"port_open": port_open, "gateway_online": gateway_online,
                 "in_sync": gateway_online, "reason": "status", "gateway_mac": None,
                 "phase": phase, "state_rev": state_rev, "cups_heard": 0, "rejects": 0,
                 "up_s": 10, "thread_alive": True, "last_line_age_s": 0.1, "lines_ok": 1,
                 "lines_bad": 0, "bytes_rx": 1, "reopens": 0},
        "devpi": {"state_rev": state_rev, "phase": phase, "scratched": list(scratched),
                  "renum": [list(p) for p in renum], "results": list(results)},
        "cups": list(cups),
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
    board_kwargs.setdefault("results_path", tmpdir() / "results.json")
    init_board(bridge=bridge, **board_kwargs)
    app.register_blueprint(la_quiniela_bp)
    app.register_blueprint(quiniela_board_bp)
    return app


def state_line(rev, phase, scratched=(), renum=(), results=(0, 0, 0)):
    """What the bridge must have written, byte-exact."""
    return P.build_state_line(rev, phase, scratched, renum, results)


def write_results(board, win, place, show):
    Path(board._results_path).parent.mkdir(parents=True, exist_ok=True)
    Path(board._results_path).write_text(json.dumps({"win": win, "place": place, "show": show,
                                                     "timestamp": "2026-05-02T22:10:00"}), encoding="utf-8")


DERBY_2024 = ["Dornoch", "Sierra Leone", "Mystik Dan", "Catching Freedom", "Catalytic", "Just Steel",
              "Honor Marie", "Just a Touch", "Encino", "T O Password", "Forever Young", "Track Phantom",
              "West Saratoga", "Endlessly", "Domestic Product", "Grand Mo the First", "Fierceness",
              "Stronghold", "Resilience", "Society Man"]
DERBY_TEXT = "\n".join(f"{n}. {name}" for n, name in enumerate(DERBY_2024, 1))
ALSO_ELIGIBLE = ["Mugatu", "Ocelli", "Epic Ride", "Society Girl"]      # 21..24 in these tests
FIELD_24 = DERBY_2024 + ALSO_ELIGIBLE
FIELD_24_TEXT = "\n".join(f"{n}. {name}" for n, name in enumerate(FIELD_24, 1))


# -----------------------------------------------------------------------------
# Model
# -----------------------------------------------------------------------------

def test_empty_snapshot_model_shape():
    b, wall, _ = fresh_board()
    _check("an empty BETTING_OPEN snapshot changes the model", b.apply_snapshot(snap(phase=1)))
    m = b.model()
    _check("exactly the 27 model keys", set(m) == MODEL_KEYS and len(MODEL_KEYS) == 27, str(sorted(m)))
    _check("link_ok true from port_open + gateway_online", m["link_ok"] is True)
    _check("race_state 1 / BETTING_OPEN", (m["race_state"], m["race_state_name"]) == (1, "BETTING_OPEN"))
    _check("token_value from settings", m["token_value"] == float(DEFAULTS["TOKEN_VALUE"]))
    _check("pot 0.0, total 0", m["pot"] == 0.0 and m["total_tokens"] == 0)
    _check("horses 1..24", sorted(m["horses"], key=int) == [str(n) for n in range(1, 25)])
    _check("every horse unassigned: 1-20 in the field, 21-24 not; no cup, no conflict",
           all(h == unassigned(n) for n, h in m["horses"].items()), str(m["horses"]["21"]))
    _check("leader None, events []", m["leader"] is None and m["events"] == [])
    _check("no cups online, none without a horse, no results, no closing figures",
           m["cups_online"] == 0 and m["cups_no_horse"] == 0 and m["results"] == NO_RESULTS and m["closing"] is None)
    _check("updated is the wall time of the change", m["updated"] == wall.t)
    _check("board_states from settings", m["board_states"] == list(DEFAULTS["QUINIELA_BOARD_STATES"]))
    _check("the board keeps the TV through WINNER: 1..5, released in 0 and 6",
           DEFAULTS["QUINIELA_BOARD_STATES"] == [1, 2, 3, 4, 5])
    _check("model() is a fresh copy", b.model() is not b.model() and b.model() == m)
    _check("model_json() is the compact JSON", b.model_json() == json.dumps(m, separators=(",", ":")))


def test_tokens_share_leader_online_conflict():
    b, wall, _ = fresh_board()
    b.apply_snapshot(snap(scratched=[3], cups=[
        cup_entry(MAC_A, horse=7, count=23, online=True),
        cup_entry(MAC_B, horse=3, count=10, online=False),
        cup_entry(MAC_C, horse=12, count=0, online=True),
        cup_entry(4, horse=0, count=2, online=True),          # a cup with no horse set yet
    ]))
    m = b.model()
    _check("total 33: a cup with no horse counts for nobody", m["total_tokens"] == 33)
    _check("pot = unscratched tokens * token_value, 2 dp (horse 3's 10 are out)",
           m["pot"] == round(23 * float(DEFAULTS["TOKEN_VALUE"]), 2), str(m["pot"]))
    h7, h3, h12 = m["horses"]["7"], m["horses"]["3"], m["horses"]["12"]
    _check("horse 7 entry: cup is the MAC, one claimer, no conflict",
           h7 == {"tokens": 23, "share": round(23 / 33, 4), "scratched": False, "online": True, "cup": MAC_A,
                  "conflict": False, "cups": [MAC_A], "name": "", "replaced": None, "in_field": True, "odds": None}, str(h7))
    _check("horse 3 tokens/share", h3["tokens"] == 10 and h3["share"] == round(10 / 33, 4))
    _check("horse 3 scratched (the state's bit) and offline, so out of the field",
           h3["scratched"] is True and h3["online"] is False and h3["in_field"] is False)
    _check("horse 3's cup is its MAC even offline", h3["cup"] == MAC_B)
    _check("horse 12 online with share 0", h12["online"] is True and h12["share"] == 0)
    _check("leader 7", m["leader"] == 7)
    _check("cups_online counts every online cup, cups_no_horse the one at horse 0",
           m["cups_online"] == 3 and m["cups_no_horse"] == 1)
    b.apply_snapshot(snap(cups=[cup_entry(MAC_A, horse=7, count=23, online=True, last_seen="2026-09-27T10:00:00Z"),
                                cup_entry(MAC_B, horse=7, count=2, online=True, last_seen="2026-09-27T10:00:05Z")]))
    h7 = b.model()["horses"]["7"]
    _check("two online cups claiming 7: conflict, both listed, the most recently heard shown",
           h7["conflict"] is True and h7["cups"] == [MAC_B, MAC_A] and h7["cup"] == MAC_B and h7["tokens"] == 2, str(h7))
    b.apply_snapshot(snap(cups=[cup_entry(MAC_A, horse=7, count=23, online=False, last_seen="2026-09-27T10:00:00Z"),
                                cup_entry(MAC_B, horse=7, count=2, online=True, last_seen="2026-09-27T10:00:05Z")]))
    h7 = b.model()["horses"]["7"]
    _check("a dead cup still claiming 7 beside a live one (a spare swapped in): no conflict, the live one shown",
           h7["conflict"] is False and h7["cups"] == [MAC_B] and h7["cup"] == MAC_B and h7["online"] is True, str(h7))
    b.apply_snapshot(snap(cups=[cup_entry(MAC_A, horse=7, count=23, online=False, last_seen="2026-09-27T10:00:00Z")]))
    h7 = b.model()["horses"]["7"]
    _check("only an offline cup claims 7: shown offline with its last count",
           h7["online"] is False and h7["cup"] == MAC_A and h7["tokens"] == 23 and h7["cups"] == [MAC_A])


def test_leader_none_without_tokens_and_lowest_on_tie():
    b, _, _ = fresh_board()
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=4), cup_entry(2, horse=9)]))
    _check("no tokens -> leader None", b.model()["leader"] is None)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=9, count=5), cup_entry(2, horse=4, count=5)]))
    _check("tie -> lowest horse number", b.model()["leader"] == 4)
    b.apply_snapshot(snap(scratched=[9], cups=[cup_entry(1, horse=9, count=5), cup_entry(2, horse=4, count=4)]))
    _check("a scratched horse is not excluded from the lead", b.model()["leader"] == 9)


def test_odd_entries_never_raise():
    b, _, _ = fresh_board()
    b.apply_snapshot(snap(cups=[
        {"mac": mac_of(1), "horse": 5, "count": None, "online": False},              # never heard: count None
        {"mac": mac_of(2), "horse": None, "count": 4},                               # no horse
        {"mac": mac_of(3), "horse": 0, "count": 4},                                  # horse 0 = none
        {"mac": mac_of(4), "horse": 25, "count": 4},                                 # out of range (24 is the cap)
        {"mac": mac_of(5), "horse": "8", "count": "6", "online": "true"},            # strings
        {"horse": 9, "count": 3},                                                    # no mac
        "garbage",                                                                   # not a dict
        {"mac": mac_of(7), "horse": 10, "count": -4, "online": 1},                   # negative clamps to 0
        {"mac": "", "horse": 11, "count": 2},                                        # empty mac
        {"mac": mac_of(8), "horse": 14, "count": 2.0, "online": True, "last_seen": None},
    ]))
    m = b.model()
    _check("count None reads as 0 tokens", m["horses"]["5"] == dict(UNASSIGNED, cup=mac_of(1), cups=[mac_of(1)], in_field=True),
           str(m["horses"]["5"]))
    _check("string fields are coerced", m["horses"]["8"] == {"tokens": 6, "share": 0.75, "scratched": False,
                                                              "online": True, "cup": mac_of(5), "conflict": False, "cups": [mac_of(5)],
                                                              "name": "", "replaced": None, "in_field": True, "odds": None}, str(m["horses"]["8"]))
    _check("negative tokens clamp to 0, cup kept", m["horses"]["10"]["tokens"] == 0 and m["horses"]["10"]["cup"] == mac_of(7)
           and m["horses"]["10"]["online"] is True)
    _check("float count", m["horses"]["14"]["tokens"] == 2 and m["horses"]["14"]["scratched"] is False)
    for n in (1, 2, 4, 9, 11, 12, 13, 20, 21, 24):
        _check(f"horse {n} untouched", m["horses"][str(n)] == unassigned(n), str(m["horses"][str(n)]))
    _check("total 8", m["total_tokens"] == 8)
    # Missing / wrong-typed sections never raise either.
    for odd in ({}, None, [], "x", {"devpi": {"phase": "x", "scratched": "no", "results": 5}, "cups": {"mac": 1}, "link": "nope"},
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


def test_renumber_and_reset_produce_no_ghost_bets():
    """A cup that reports a new number (a replacement scratch followed, or a
    spare set to a dead cup's horse) brings its count with it: not a bet. A
    baseline (the between-races reset) clears the ticker outright; a real
    drop or removal on the same cup still counts."""
    b, wall, log_dir = fresh_board()

    def live(h3=50, h2=42, state_rev=1, phase=1, cup3=1, cup2=2):
        return snap(phase=phase, state_rev=state_rev,
                    cups=[cup_entry(cup3, horse=3, count=h3, online=True),
                          cup_entry(cup2, horse=2, count=h2, online=True)])

    b.apply_snapshot(live())
    wall.advance(1)
    b.apply_snapshot(live(h3=51))
    _check("a bet before the reset is an event",
           b.model()["events"] == [{"horse": 3, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    _check("pot before the reset", b.model()["pot"] == 93.0)

    wall.advance(1)
    _check("a baseline apply of the same picture is a change (the events go)",
           b.apply_snapshot(live(h3=51, phase=0, state_rev=2), baseline=True))
    m = b.model()
    _check("after the reset: the counts stay (the tokens are still in the cups), PRE_RACE",
           m["total_tokens"] == 93 and m["pot"] == 93.0 and m["race_state"] == 0)
    _check("the ticker is empty: no ghosts", m["events"] == [], str(m["events"]))
    _, lines = log_lines(log_dir)
    _check("the log records the reset as a baseline", lines[-1].get("baseline") is True
           and lines[-1].get("reset") == "betting" and {"race_state": [1, 0]} in lines[-1]["changes"], str(lines[-1]))
    wall.advance(1)
    _check("a repeat of the picture is not a change", b.apply_snapshot(live(h3=51, phase=0, state_rev=2)) is False)
    _check("still no events", b.model()["events"] == [])

    # From here on the same cups carry the same horses: real movement counts.
    wall.advance(1)
    b.apply_snapshot(live(h3=52, h2=42, state_rev=3))
    _check("a real drop after the reset is an event",
           b.model()["events"] == [{"horse": 3, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    wall.advance(1)
    b.apply_snapshot(live(h3=52, h2=41, state_rev=3))
    _check("a real removal is still a negative event",
           b.model()["events"][0] == {"horse": 2, "delta": -1, "ts": wall.t}, str(b.model()["events"]))
    _check("older events kept", len(b.model()["events"]) == 2)
    _, lines = log_lines(log_dir)
    _check("a bet's log entry is plain: no cup move, no baseline flag",
           "baseline" not in lines[-1] and lines[-1]["changes"] == [{"horse": 2, "tokens": [42, 41]}],
           str(lines[-1]))

    # Horse 2 is now claimed by another cup (a spare set to 2, the old cup gone): its
    # tokens jump to that cup's count, no event.
    wall.advance(1)
    b.apply_snapshot(snap(phase=1, state_rev=4,
                          cups=[cup_entry(1, horse=3, count=52, online=True),
                                cup_entry(4, horse=2, count=7, online=True)]))
    m = b.model()
    _check("horse 2 now on cup 4 with that cup's count",
           m["horses"]["2"] == {"tokens": 7, "share": round(7 / 59, 4), "scratched": False, "online": True,
                                "cup": mac_of(4), "conflict": False, "cups": [mac_of(4)], "name": "", "replaced": None,
                                "in_field": True, "odds": None}, str(m["horses"]["2"]))
    _check("no event for the cup change",
           len(m["events"]) == 2 and m["events"][0]["horse"] == 2 and m["events"][0]["delta"] == -1)
    _, lines = log_lines(log_dir)
    _check("the cup change is logged with the move (the MACs)",
           {"horse": 2, "tokens": [41, 7], "cup": [mac_of(2), mac_of(4)]} in lines[-1]["changes"], str(lines[-1]))
    wall.advance(1)
    b.apply_snapshot(snap(phase=1, state_rev=4,
                          cups=[cup_entry(1, horse=3, count=52, online=True),
                                cup_entry(4, horse=2, count=8, online=True)]))
    _check("a drop on the new cup is a bet again",
           b.model()["events"][0] == {"horse": 2, "delta": 1, "ts": wall.t})

    # A second reset with events on the board clears them outright.
    wall.advance(1)
    b.apply_snapshot(snap(phase=0, state_rev=5,
                          cups=[cup_entry(1, horse=3, count=52, online=True),
                                cup_entry(4, horse=2, count=8, online=True)]), baseline=True)
    _check("a reset clears the events that were showing", b.model()["events"] == [])


def test_duplicate_horse_is_a_conflict_and_warns_once():
    b, _, _ = fresh_board()
    cups = [cup_entry(4, horse=7, count=5, online=True, last_seen="2026-09-27T10:00:04Z"),
            cup_entry(2, horse=7, count=9, online=True, last_seen="2026-09-27T10:00:02Z"),
            cup_entry(3, horse=7, count=1, online=True, last_seen="2026-09-27T10:00:03Z")]
    with capture_logs(LOGGER) as cap:
        b.apply_snapshot(snap(cups=cups))
        b.apply_snapshot(snap(cups=cups, state_rev=2))
        b.apply_snapshot(snap(cups=cups, state_rev=3))
    warnings = cap.messages("all claim horse 7")
    _check("one WARNING per distinct set of claimers", len(warnings) == 1, str(warnings))
    _check("the message names the cups and the one shown",
           warnings == ["cups %s, %s, %s all claim horse 7; showing %s" % (mac_of(4), mac_of(3), mac_of(2), mac_of(4))],
           str(warnings))
    _check("warned at WARNING on la_quiniela.betting", all(r.levelno == logging.WARNING for r in cap.records))
    h7 = b.model()["horses"]["7"]
    _check("the most recently heard cup is shown, every claimer listed, conflict flagged",
           h7["cup"] == mac_of(4) and h7["tokens"] == 5 and h7["conflict"] is True
           and h7["cups"] == [mac_of(4), mac_of(3), mac_of(2)], str(h7))
    _check("the others' tokens do not count", b.model()["total_tokens"] == 5)
    with capture_logs(LOGGER) as cap:
        b.apply_snapshot(snap(cups=cups[:2], state_rev=4))
    _check("a different set of claimers warns again", len(cap.messages("all claim horse 7")) == 1)


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
    b.apply_snapshot(snap(phase=2, scratched=[7], cups=[cup_entry(1, horse=7, count=24)]))
    _, lines = log_lines(log_dir)
    _check("second change logged", len(lines) == 2)
    _check("tokens, scratched and race_state changes in order", lines[1]["changes"] == [
        {"horse": 7, "tokens": [23, 24]},
        {"horse": 7, "scratched": [False, True]},
        {"race_state": [1, 2]},
    ], str(lines[1]))
    _check("total on the record", lines[1]["total_tokens"] == 24)
    b.apply_snapshot(snap(phase=2, scratched=[7], cups=[cup_entry(1, horse=7, count=24)]))
    b.apply_snapshot(snap(phase=2, scratched=[7], cups=[cup_entry(1, horse=7, count=24, online=True)]))
    b.apply_snapshot(snap(phase=2, scratched=[7], cups=[cup_entry(1, horse=7, count=24, online=True)],
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
        b = BettingBoard(wall=FakeClock(1_700_000_000.0), results_path=tmpdir() / "r.json")   # no log_dir given
        b.apply_snapshot(snap(cups=[cup_entry(1, horse=7, count=1)]))
        _check("written under betting.LOG_DIR", len(list(betting.LOG_DIR.glob("quiniela_*.jsonl"))) == 1)
    finally:
        betting.LOG_DIR = saved
    _check("the default LOG_DIR is pi5/data", betting.LOG_DIR == Path(_PI5_DIR) / "data")
    _check("the default results file is the dashboard's, under pi5/data",
           betting.RESULTS_FILE == Path(_PI5_DIR) / "data" / "results.json")


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
            b = BettingBoard(settings={"TOKEN_VALUE": huge}, log_dir=tmpdir(), results_path=tmpdir() / "r.json")
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
    b.set_state(phase=1)
    _check("set_state changes the model", board.refresh())
    m = board.model()
    _check("phase from devpi", (m["race_state"], m["race_state_name"]) == (1, "BETTING_OPEN"))
    _check("no cups yet: no horse has one, 0 tokens, offline", all(h["cup"] is None for h in m["horses"].values())
           and m["total_tokens"] == 0 and not any(h["online"] for h in m["horses"].values()))
    board.store.scratch_gateway(7)
    board.refresh()
    _check("a no-replacement scratch in the store goes down as the horse's bit",
           port.lines()[-1] == state_line(3, 1, [7]) and b.scratched == [7], str(port.lines()))
    m = board.model()
    _check("scratched in the model", m["horses"]["7"]["scratched"] is True and m["horses"]["8"]["scratched"] is False)
    b.handle_raw_line(telem(MAC_A, horse=7, count=3))          # a cup that says it is horse 7
    _check("telemetry changes the model", board.refresh())
    m = board.model()
    _check("count -> tokens on the horse the cup reports",
           m["horses"]["7"] == {"tokens": 3, "share": 1.0, "scratched": True, "online": True, "cup": MAC_A,
                                "conflict": False, "cups": [MAC_A], "name": "", "replaced": None, "in_field": False, "odds": None},
           str(m["horses"]["7"]))
    _check("any line puts the gateway online -> link_ok", m["link_ok"] is True)
    _check("leader, pot (horse 7 is scratched: its 3 tokens count but are out of the pot)",
           m["leader"] == 7 and m["pot"] == 0.0 and m["total_tokens"] == 3, str((m["leader"], m["pot"])))
    _check("the count that came with a cup newly heard on the horse is not a bet: no event",
           m["events"] == [], str(m["events"]))
    _, lines = log_lines(log_dir)
    _check("...the log carries it as a cup move", lines[-1]["changes"] == [{"horse": 7, "tokens": [0, 3], "cup": [None, MAC_A]}],
           str(lines[-1]))
    _check("cups_online 1", m["cups_online"] == 1 and m["cups_no_horse"] == 0)
    wall.advance(1)
    b.handle_raw_line(telem(MAC_A, horse=7, count=5))
    board.refresh()
    _check("a drop in that cup is an event", board.model()["events"] == [{"horse": 7, "delta": 2, "ts": wall.t}],
           str(board.model()["events"]))
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
    b.handle_raw_line(telem(MAC_B, horse=0, count=0, hello=True))       # a cup nobody has set yet
    board.refresh()
    m = board.model()
    _check("a cup at horse 0 is counted (the other cup is offline), and is in no row",
           m["cups_online"] == 1 and m["cups_no_horse"] == 1 and all(h["cup"] != MAC_B for h in m["horses"].values()))
    _check("refresh() with no bridge is False", BettingBoard(results_path=tmpdir() / "r.json").refresh() is False)


def test_listener_hook():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    hits = []
    b.add_listener(lambda: hits.append("a"))
    n = len(hits)
    b.handle_raw_line(telem(MAC_A, horse=1, count=1))
    _check("a telem line (new cup) calls the listener", len(hits) > n)
    n = len(hits)
    b.handle_raw_line(telem(MAC_A, horse=1, count=2))
    _check("a count change calls the listener", len(hits) > n)
    n = len(hits)
    b.set_state(phase=1)
    n2 = len(hits)
    _check("set_state calls the listener (it emits a snapshot)", n2 > n and sio.of("lq_snapshot"))
    n = len(hits)
    _check("an identical set_state is a no-op", b.set_state(phase=1) == b.state_rev and len(hits) == n)
    b.forget_cups()
    _check("forget_cups calls the listener", len(hits) > n)
    n = len(hits)
    clk.advance(13)
    b.tick(clk())
    _check("the offline timer (lq_link emit) calls the listener", len(hits) > n)
    # A listener that raises is logged and dropped; the others still run.
    order = []
    b.add_listener(lambda: (_ for _ in ()).throw(RuntimeError("listener bug")))
    b.add_listener(lambda: order.append("last"))
    with capture_logs("la_quiniela.bridge") as cap:
        b.handle_raw_line(telem(MAC_A, horse=1, count=3))
    _check("a failing listener is a WARNING, not an exception", cap.messages("listener") and
           all(r.levelno == logging.WARNING for r in cap.records), str(cap.messages()))
    _check("the listeners after it still ran", order == ["last"] * len(order) and order)
    _check("the bridge carried on", b.cups[MAC_A].count == 3)
    # The board's wake() is exactly such a listener.
    board = BettingBoard(bridge=b, results_path=tmpdir() / "r.json")
    b.add_listener(board.wake)
    b.handle_raw_line(telem(MAC_A, horse=1, count=4))
    _check("wake() set the event", board._wake.is_set())


def test_board_thread_picks_up_changes():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    b.add_listener(board.wake)
    b.set_state(phase=1)
    _check("start() -> True", board.start() is True and board.running)
    _check("start() again is idempotent", board.start() is True)
    _check("the thread is named lq-board", "lq-board" in {t.name for t in threading.enumerate()})
    deadline = time.time() + 2
    while time.time() < deadline and board.model()["race_state"] != 1:
        time.sleep(0.02)
    _check("the state set before start() is picked up", board.model()["race_state"] == 1)
    port.feed(S.MID_LINE + telem(MAC_A, horse=7, count=5))     # a fresh port drops everything before the first newline
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
        state_rev = 1

        def get_snapshot(self):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:                 # the first caller parks here holding a stale picture
                inside.set()
                release.wait(5)
                return snap(cups=[cup_entry(7, horse=7, count=1, online=True)])
            return snap(cups=[cup_entry(7, horse=7, count=5, online=True)])

        def set_state(self, **kwargs):
            return 1

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
    _check("stripped", validate_cmd("  state 3 \n") == ("state 3", None))
    for word in ("state", "demo", "json"):
        _check(f"{word} allowed", validate_cmd(word)[1] is None)
    for bad, msg in (("", "empty command"), ("   ", "empty command"), (None, "cmd must be a string"),
                     (3, "cmd must be a string"), ("help", "command not allowed: help"),
                     ("horse 1 7", "command not allowed: horse"), ("scratch 1 1", "command not allowed: scratch"),
                     ("roster", "command not allowed: roster"),
                     ("debug on", "command not allowed: debug"), ("STATE 1", "command not allowed: STATE"),
                     ("state\n1", "command must be a single line"), ("state\r1", "command must be a single line"),
                     ("x" * 201, "command longer than 200 characters")):
        text, error = validate_cmd(bad)
        _check(f"{bad!r:.20} rejected: {msg}", text is None and error is not None and error.startswith(msg), str(error))
    _check("the allowed list is spelled out: the v1 horse / scratch / roster commands are gone",
           validate_cmd("help")[1] == "command not allowed: help (allowed: demo json state)")
    _check("exactly 200 chars is allowed", validate_cmd("json " + "1" * 195)[1] is None)


def test_cmd_whitelist_400s():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    for bad in ({"cmd": ""}, {"cmd": "   "}, {"cmd": "reboot"}, {"cmd": "State 1"}, {"cmd": "state 1\nreboot"},
                {"cmd": "state 1\rreboot"}, {"cmd": "state " + "1" * 200}, {"cmd": 5}, {"nope": "state 1"},
                ["state 1"], {"cmd": "horse 1 7"}, {"cmd": "scratch 1 1"}, {"cmd": "roster"}):
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
    for good in ("state 1", "  state 1  "):
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
    _check("state 1 -> 200 ok, rev 2 (the bridge starts at 1), phase 1, gateway offline",
           r.status_code == 200 and body == {"ok": True, "rev": 2, "phase": 1, "gateway_online": False}, str(body))
    _check("the bridge holds the state", b.phase == 1 and b.state_rev == 2)
    _check("the downlink state line, byte-exact via build_state_line",
           port.lines() == [state_line(2, 1)], str(port.lines()))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("the model follows", m["race_state"] == 1 and m["race_state_name"] == "BETTING_OPEN")
    r = client.post("/api/quiniela/cmd", json={"cmd": "  state 6  "})
    _check("whitespace tolerated, rev 3, phase 6", r.get_json()["rev"] == 3 and r.get_json()["phase"] == 6)
    _check("second line, rev 3", port.lines()[-1] == state_line(3, 6))
    port.written.clear()
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 6"})
    _check("the same state again is a no-op with the same rev", r.status_code == 200 and r.get_json()["rev"] == 3
           and port.written == [])
    b.handle_raw_line(status(phase=6, state_rev=3))
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 0"})
    _check("gateway_online true once the gateway has spoken", r.get_json()["gateway_online"] is True
           and r.get_json()["rev"] == 4)
    _check("state 0 sent even with the gateway online", port.lines()[-1] == state_line(4, 0))
    get_board().store.scratch_gateway(7)
    get_board().refresh()
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 1"})
    _check("a state change keeps the rest of the line (the scratched bit)", port.lines()[-1] == state_line(6, 1, [7]),
           str(port.lines()[-1]))


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
           "gateway's protocol, and the up state line is the gateway's report, not a command")
    _check("nothing reached the port", port.written == [])


def test_cmd_usage_errors():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    cases = [("state", USAGE_STATE), ("state 7", USAGE_STATE), ("state -1", USAGE_STATE), ("state x", USAGE_STATE),
             ("state 1 2", USAGE_STATE), ("state 1.0", USAGE_STATE)]
    for cmd, usage in cases:
        r = client.post("/api/quiniela/cmd", json={"cmd": cmd})
        _check(f"{cmd!r} -> 400 {usage}", r.status_code == 400 and r.get_json() == {"ok": False, "error": usage},
               f"{r.status_code} {r.get_json()}")
    _check("nothing reached the port", port.written == [] and b.state_rev == 1)
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
    _check("the 27 keys", set(m) == MODEL_KEYS, str(sorted(m)))
    _check("fresh: link down, PRE_RACE", m["link_ok"] is False and m["race_state"] == 0 and m["race_state_name"] == "PRE_RACE")
    _check("24 horses, no tokens, no leader", len(m["horses"]) == 24 and m["total_tokens"] == 0 and m["leader"] is None)
    _check("token_value and board_states", m["token_value"] == float(DEFAULTS["TOKEN_VALUE"])
           and m["board_states"] == list(DEFAULTS["QUINIELA_BOARD_STATES"]))
    _check("get_board() is the one init_board made", get_board().bridge is b)


def test_model_route_follows_the_bridge():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    b.set_state(phase=2)
    b.handle_raw_line(telem(MAC_A, horse=7, count=23))
    b.handle_raw_line(telem(MAC_B, horse=3, count=2))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("link up", m["link_ok"] is True)
    _check("FINAL_CALL", m["race_state_name"] == "FINAL_CALL")
    _check("horse 7 23 tokens, horse 3 2", m["horses"]["7"]["tokens"] == 23 and m["horses"]["3"]["tokens"] == 2)
    _check("leader 7, total 25", m["leader"] == 7 and m["total_tokens"] == 25)
    _check("the cups are MACs in the model", m["horses"]["7"]["cup"] == MAC_A and m["horses"]["3"]["cup"] == MAC_B)


def test_stream_generator():
    b, wall, _ = fresh_board()
    gen = sse_events(b, heartbeat_s=0.05, wall=wall)
    first = next(gen)
    _check("first chunk is data: <model>", first.startswith("data: ") and first.endswith("\n\n"))
    model = json.loads(first[len("data: "):])
    _check("the first chunk is the current model", model["link_ok"] is False and model["board_states"] == [1, 2, 3, 4, 5])
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
    b.set_state(phase=1)
    b.handle_raw_line(telem(MAC_A, horse=7, count=3))
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
    b.set_state(phase=1)
    _check("start_board refreshes synchronously first", start_board() is True and first.model()["race_state"] == 1)
    _check("the thread runs", first.running)
    init_board(bridge=b, log_dir=tmpdir(), results_path=tmpdir() / "r.json")
    _check("init_board again stops the old board", not first.running and get_board() is not first)
    _check("stop_board with a stopped board is fine", stop_board() is None)
    _check("start_board again", start_board() is True and get_board().running)
    stop_board()
    _check("stop_board ends it", not get_board().running)
    board = init_board(bridge=b, settings={"TOKEN_VALUE": 5}, log_dir=tmpdir(), results_path=tmpdir() / "r.json")
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
    _check("no names yet: every name empty, no replaced, names_rev 0, no scratches",
           all(h["name"] == "" and h["replaced"] is None for h in b.model()["horses"].values())
           and b.model()["names_rev"] == 0 and b.model()["scratches"] == [])
    _check("the board's store is memory-only without a bridge", b.store._db is None)
    b._wake.clear()
    _check("set_names bumps names_rev and wakes the board (an also-eligible can be named ahead of time)",
           b.store.set_names({9: "  Encino ", 3: "Fierceness", 22: "Ocelli"}) is True and b.store.names_rev == 1 and b._wake.is_set())
    _check("the same names again change nothing", b.store.set_names({9: "Encino"}) is False and b.store.names_rev == 1)
    _check("stored as typed (stripped), name only", b.store.horses()[9] == {"name": "Encino"}, str(b.store.horses()[9]))
    wall.advance(1)
    _check("the next apply of the SAME snapshot is a change (names differ)", b.apply_snapshot(picture) is True)
    m = b.model()
    _check("served upper-cased", m["horses"]["9"]["name"] == "ENCINO" and m["horses"]["3"]["name"] == "FIERCENESS")
    _check("the also-eligible's name is served too, out of the field", m["horses"]["22"]["name"] == "OCELLI"
           and m["horses"]["22"]["in_field"] is False and m["horses"]["22"]["cup"] is None, str(m["horses"]["22"]))
    _check("names_rev in the model", m["names_rev"] == 1)
    _check("published once", q.qsize() == 1)
    _check("...with no events: names are not bets", m["events"] == [], str(m["events"]))
    _check("...and no log line: nothing about tokens moved", len(log_lines(log_dir)[1]) == n_lines)
    # The renumber, the way the routes do it: the record goes into the store,
    # and the cup that was 9 reports 22 in the next snapshot (it followed the pair).
    done = b.store.scratch_replace(9, 22)
    _check("scratch_replace returns was / now with the names as typed",
           done == {"was": {"number": 9, "name": "Encino"}, "now": {"number": 22, "name": "Ocelli"}}, str(done))
    _check("names_rev 2, the record on file", b.store.names_rev == 2 and b.store.scratches() == {9: 22})
    _check("the record lookups", b.store.replacement_of(9) == 22 and b.store.replaced_by(22) == 9
           and b.store.replacement_of(22) is None and b.store.replaced_by(9) is None
           and b.store.active_number(9) == 22 and b.store.active_number(3) == 3)
    _check("desired_state() carries the pair", b.desired_state(picture)[1] == [(9, 22)])
    wall.advance(1)
    renumbered = snap(renum=[(9, 22)], cups=[cup_entry(1, horse=22, count=12, online=True), cup_entry(2, horse=3, count=5, online=True)])
    b.apply_snapshot(renumbered)
    m = b.model()
    _check("horse 22: the cup and its 12 tokens, OCELLI replacing ENCINO, in the field, not scratched",
           m["horses"]["22"] == {"tokens": 12, "share": round(12 / 17, 4), "scratched": False, "online": True,
                                 "cup": mac_of(1), "conflict": False, "cups": [mac_of(1)], "name": "OCELLI",
                                 "replaced": "ENCINO", "in_field": True, "odds": None}, str(m["horses"]["22"]))
    _check("horse 9: no cup, no tokens, out of the field, its name kept",
           m["horses"]["9"] == dict(UNASSIGNED, name="ENCINO"), str(m["horses"]["9"]))
    _check("scratches lists the record, upper-cased",
           m["scratches"] == [{"was": {"number": 9, "name": "ENCINO"}, "now": {"number": 22, "name": "OCELLI"}}], str(m["scratches"]))
    _check("the pot still counts the cup: 17, prizes 10 / 4 / 3", m["pot"] == 17.0 and m["total_tokens"] == 17
           and m["prizes"] == {"win": 10, "place": 4, "show": 3}, str((m["pot"], m["prizes"])))
    _check("names_rev 2 and NO events: the count came with the cup", m["names_rev"] == 2 and m["events"] == [], str(m["events"]))
    _, lines = log_lines(log_dir)
    _check("the log carries the cup marks, not a bet",
           lines[-1]["changes"] == [{"horse": 9, "tokens": [12, 0], "cup": [mac_of(1), None]},
                                    {"horse": 22, "tokens": [0, 12], "cup": [None, mac_of(1)]}] and "baseline" not in lines[-1], str(lines[-1]))
    wall.advance(1)
    renumbered = snap(renum=[(9, 22)], cups=[cup_entry(1, horse=22, count=13, online=True), cup_entry(2, horse=3, count=5, online=True)])
    b.apply_snapshot(renumbered)
    _check("a real drop after the renumber is an event on 22 (same cup)",
           b.model()["events"] == [{"horse": 22, "delta": 1, "ts": wall.t}], str(b.model()["events"]))
    # What the records alone can refuse (the routes add what needs the bridge).
    for bad, why in (((9, 23), "horse 9 is not in the field"), ((3, 22), "22 is in use"), ((3, 9), "9 is in use"),
                     ((3, 3), "3 is in use"), ((0, 21), "not in 1-24"), ((25, 21), "not in 1-24"),
                     (("x", 21), "must be a number 1-24"), ((3, 0), "not in 1-24"), ((3, 25), "not in 1-24"),
                     ((3, "x"), "must be a number 1-24"), ((3, 21, 5), "must be a string"), ((3, 21, "x" * 81), "longer than 80")):
        try:
            b.store.scratch_replace(*bad)
            _check(f"scratch_replace{bad!r:.30} rejected: {why}", False)
        except ValueError as exc:
            _check(f"scratch_replace{bad!r:.30} rejected: {why}", why in str(exc), str(exc))
    for bad in ({0: "x"}, {"25": "x"}, {9: 5}, {9: "x" * 81}):
        try:
            b.store.set_names(bad)
            _check(f"set_names({bad!r:.30}) rejected", False)
        except ValueError:
            _check(f"set_names({bad!r:.30}) rejected", True)
    _check("nothing was written by the rejected calls", b.store.names_rev == 2 and b.store.scratches() == {9: 22})
    # A replacement of a horse with no name, the name given to the record's now.
    done = b.store.scratch_replace(1, 21, " Late Entry ")
    _check("an unnamed horse replaced: was '', and the name given (stripped) becomes now's",
           done == {"was": {"number": 1, "name": ""}, "now": {"number": 21, "name": "Late Entry"}}, str(done))
    b.apply_snapshot(renumbered)
    m = b.model()
    _check("...first in scratches (ordered by was.number) with was ''",
           m["scratches"][0] == {"was": {"number": 1, "name": ""}, "now": {"number": 21, "name": "LATE ENTRY"}}, str(m["scratches"]))
    _check("21 is in the field with replaced '' (a record, an unnamed horse); 1 is out",
           m["horses"]["21"]["in_field"] is True and m["horses"]["21"]["replaced"] == "" and m["horses"]["1"]["in_field"] is False)
    _check("a replacement with an empty name keeps the stored one", b.store.unscratch_replace(1) == 21
           and b.store.scratch_replace(1, 21, "")["now"] == {"number": 21, "name": "Late Entry"})
    _check("unscratch_replace returns the now and removes the record", b.store.unscratch_replace(9) == 22
           and b.store.scratches() == {1: 21} and b.store.names_rev == 6)
    _check("unscratch_replace with nothing to undo is None, no bump", b.store.unscratch_replace(9) is None and b.store.names_rev == 6)
    _check("22's name stays stored", b.store.horses()[22] == {"name": "Ocelli"})
    b.store.unscratch_replace(1)
    b.apply_snapshot(snap(cups=[cup_entry(1, horse=9, count=13, online=True), cup_entry(2, horse=3, count=5, online=True)]))
    m = b.model()
    _check("model back: ENCINO on its cup with the tokens and in the field, 22 out with no replaced, no scratches, names_rev 7",
           m["horses"]["9"]["name"] == "ENCINO" and m["horses"]["9"]["cup"] == mac_of(1) and m["horses"]["9"]["tokens"] == 13
           and m["horses"]["9"]["in_field"] is True and m["horses"]["22"]["in_field"] is False
           and m["horses"]["22"]["replaced"] is None and m["scratches"] == [] and m["names_rev"] == 7, str(m["horses"]["9"]))
    _check("...and the undo produced no event either (the cup moved back)", m["events"] == [{"horse": 22, "delta": 1, "ts": wall.t}],
           str(m["events"]))
    # An injected store is used as given, and the empty model already carries its names.
    store = HorseStore()
    store.set_names({4: "Catching Freedom"})
    store.set_closes_at(1_700_000_900.0)
    b3 = BettingBoard(store=store, wall=FakeClock(1_700_000_000.0), results_path=tmpdir() / "r.json")
    _check("an injected store is the board's store", b3.store is store and store.on_change == b3.wake)
    m = b3.model()
    _check("the empty model already carries the store's names and closing time",
           m["horses"]["4"]["name"] == "CATCHING FREEDOM" and m["closes_at"] == 1_700_000_900.0 and m["names_rev"] == 1)


def test_in_field_rules():
    _check("a plain field: 1-20 in, 21-24 out", [n for n in range(1, 25) if in_field(n, {})] == list(range(1, 21)))
    _check("a replaced field: 9 out, 22 in, 21 still out", in_field(9, {9: 22}) is False and in_field(22, {9: 22}) is True
           and in_field(21, {9: 22}) is False)
    _check("a no-replacement scratch: 20 out, nobody in for it", in_field(20, {}, {20}) is False and in_field(19, {}, {20}) is True)
    _check("a now that was scratched with no replacement is out", in_field(22, {9: 22}, {22}) is False)
    _check("a chain: 22 out again, 23 in", in_field(22, {9: 22, 22: 23}) is False and in_field(23, {9: 22, 22: 23}) is True)
    _check("the was wins over the 1-20 rule and over being a now", in_field(3, {3: 21}) is False and in_field(21, {3: 21, 21: 24}) is False)
    # In the model, from the snapshot and the store together.
    b, _, _ = fresh_board()
    b.apply_snapshot(snap(cups=[cup_entry(n, horse=n, count=1) for n in range(1, 21)]))
    m = b.model()
    _check("model, plain field: 1-20 in_field true, 21-24 false",
           [n for n in range(1, 25) if m["horses"][str(n)]["in_field"]] == list(range(1, 21)) and m["scratches"] == [])
    b.apply_snapshot(snap(cups=[cup_entry(n, horse=n, count=1) for n in range(1, 21) if n != 15]))
    _check("model: a horse on no cup is still in the field (1-20 rule)", b.model()["horses"]["15"]["in_field"] is True
           and b.model()["horses"]["15"]["cup"] is None)
    b.store.scratch_replace(9, 22)
    b.apply_snapshot(snap(cups=[cup_entry(n, horse=(22 if n == 9 else n), count=1) for n in range(1, 21)]))
    m = b.model()
    _check("model, replaced field: 9 out, 22 in on the cup that was 9", m["horses"]["9"]["in_field"] is False and m["horses"]["22"]["in_field"] is True
           and m["horses"]["22"]["cup"] == mac_of(9) and [n for n in range(1, 25) if m["horses"][str(n)]["in_field"]] == [n for n in range(1, 23) if n not in (9, 21)])
    b.apply_snapshot(snap(scratched=[20], cups=[cup_entry(n, horse=(22 if n == 9 else n), count=1) for n in range(1, 21)]))
    m = b.model()
    _check("model, the state's bit for 20 (no record yet): 20 out, listed with now null after the record",
           m["horses"]["20"]["in_field"] is False and m["horses"]["20"]["scratched"] is True
           and m["scratches"] == [{"was": {"number": 9, "name": ""}, "now": {"number": 22, "name": ""}},
                                  {"was": {"number": 20, "name": ""}, "now": None}], str(m["scratches"]))
    b.apply_snapshot(snap(scratched=[22], cups=[cup_entry(n, horse=(22 if n == 9 else n), count=1) for n in range(1, 21)]))
    m = b.model()
    _check("model, the now itself scratched: 22 out, one scratches entry (the record) for 9, one for 22",
           m["horses"]["22"]["in_field"] is False
           and m["scratches"] == [{"was": {"number": 9, "name": ""}, "now": {"number": 22, "name": ""}},
                                  {"was": {"number": 22, "name": ""}, "now": None}], str(m["scratches"]))
    b.apply_snapshot(snap(cups=[cup_entry(n, horse=(23 if n == 3 else n), count=1) for n in range(1, 21)]))
    m = b.model()
    _check("model, a cup saying it is 23 with no record: 23 is not in the field (an also-eligible only stands in through a record), 3 still is",
           m["horses"]["23"]["in_field"] is False and m["horses"]["23"]["cup"] == mac_of(3) and m["horses"]["3"]["in_field"] is True)


def test_parse_names_text():
    names = parse_names_text(DERBY_TEXT)
    _check("20 prefixed lines -> 20 names", names == {n: name for n, name in enumerate(DERBY_2024, 1)}, str(names))
    plain = parse_names_text("\n".join(DERBY_2024))
    _check("20 plain lines -> post-position order", plain == names)
    full = parse_names_text("\n".join(FIELD_24))
    _check("24 plain lines -> 24 names, 21-24 the also-eligibles", full == {n: name for n, name in enumerate(FIELD_24, 1)}
           and full[22] == "Ocelli", str(full))
    _check("24 prefixed lines likewise", parse_names_text(FIELD_24_TEXT) == full)
    _check("a '22.' prefix names the also-eligible; so do '#24' and '21)'",
           parse_names_text("22. Ocelli") == {22: "Ocelli"} and parse_names_text("#24 X\n21) Y") == {24: "X", 21: "Y"})
    _check("a 21st plain line is horse 21", parse_names_text("\n".join(DERBY_2024) + "\nMugatu")[21] == "Mugatu")
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
    _check("blank lines past the 24th are fine", parse_names_text("\n".join(FIELD_24) + "\n\n\n") == full)
    gap = "\n".join(name if n != 3 else "" for n, name in enumerate(FIELD_24, 1))
    _check("prefixed lines past the 24th are fine", parse_names_text(gap + "\n3. Mystik")[3] == "Mystik")
    for bad, why in (("\n".join(FIELD_24) + "\nExtra", "more than 24"), ("25. X", "not in 1-24"), ("0. X", "not in 1-24"),
                     ("#0 X", "not in 1-24"), ("3. A\n3. B", "named twice"), ("A\n1. B", "named twice"),
                     (5, "must be a string"), ("x" * 81, "longer than 80")):
        try:
            parse_names_text(bad)
            _check(f"{bad!r:.30} -> ValueError {why}", False)
        except ValueError as exc:
            _check(f"{bad!r:.30} -> ValueError {why}", why in str(exc), str(exc))


def test_routes_scratch_rejections():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    for n in range(1, 21):
        if n != 15:                                                  # no cup says it is 15: 15 is in the field on no cup
            b.handle_raw_line(telem(mac_of(n), horse=n, count=0))
    client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": "Epic Ride"})
    _check("the old string shape -> 400 that says the shape",
           r.status_code == 400 and r.get_json() == {"ok": False, "error": REPLACEMENT_SHAPE}, str(r.get_json()))
    cases = ((({"horse": 9, "replacement": {"number": 3, "name": "X"}}), "3 is in use"),          # a cup says it is 3
             (({"horse": 9, "replacement": {"number": 15}}), "15 is in use"),                      # in the field, on no cup
             (({"horse": 9, "replacement": {"number": 9}}), "9 is in use"),                        # N == H
             (({"horse": 25, "replacement": {"number": 22}}), "horse must be a number 1-24"),
             (({"horse": 21, "replacement": {"number": 22}}), "horse 21 is not in the field"),     # an also-eligible not standing in
             (({"horse": 9, "replacement": {"number": 0}}), "replacement number must be 1-24"),
             (({"horse": 9, "replacement": {"number": 25}}), "replacement number must be 1-24"),
             (({"horse": 9, "replacement": {"number": "x"}}), "replacement number must be 1-24"),
             (({"horse": 9, "replacement": {"number": True}}), REPLACEMENT_SHAPE),
             (({"horse": 9, "replacement": {}}), REPLACEMENT_SHAPE),
             (({"horse": 9, "replacement": 22}), REPLACEMENT_SHAPE),
             (({"horse": 9, "replacement": [22]}), REPLACEMENT_SHAPE),
             (({"horse": 9, "replacement": ""}), REPLACEMENT_SHAPE),
             (({"horse": 9, "replacement": {"number": 22, "name": 5}}), "replacement name must be a string"),
             (({"horse": 9, "replacement": {"number": 22, "name": "x" * 81}}), "replacement name longer than 80 characters"))
    for body, why in cases:
        r = client.post("/api/quiniela/scratch", json=body)
        _check(f"{body!r:.62} -> 400 {why}", r.status_code == 400 and r.get_json() == {"ok": False, "error": why},
               f"{r.status_code} {r.get_json()}")
    _check("nothing reached the gateway, no record, names_rev untouched",
           port.written == [] and get_board().store.scratches() == {} and get_board().store.names_rev == 1)
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    _check("9 -> 22 goes through: the pair [9, 22] in one state line, the cup that said 9 named", r.status_code == 200
           and r.get_json()["cup"] == mac_of(9) and r.get_json()["renum"] == [9, 22] and len(port.lines()) == 1
           and port.lines()[0] == state_line(2, 0, [], [(9, 22)]), str((r.get_json(), port.lines())))
    b.handle_raw_line(telem(mac_of(9), horse=22, count=0))        # the cup followed the pair
    b.handle_raw_line(telem(mac_of(20), horse=20, count=0))
    client.post("/api/quiniela/scratch", json={"horse": 20})      # 20 scratched with no replacement
    port.written.clear()
    for body, why in (({"horse": 9, "replacement": {"number": 23}}, "horse 9 is already scratched"),   # the was of a record
                      ({"horse": 3, "replacement": {"number": 9}}, "9 is in use"),                     # the was of a record
                      ({"horse": 3, "replacement": {"number": 22}}, "22 is in use"),                   # the now of a record
                      ({"horse": 20, "replacement": {"number": 23}}, "horse 20 is already scratched"),  # no-replacement record
                      ({"horse": 3, "replacement": {"number": 20}}, "20 is in use"),                   # a cup says it is 20, and a record
                      ({"horse": 9}, "horse 9 is already scratched")):                                 # the no-replacement kind on a horse that left
        r = client.post("/api/quiniela/scratch", json=body)
        _check(f"{body!r:.62} -> 400 {why}", r.status_code == 400 and r.get_json() == {"ok": False, "error": why},
               f"{r.status_code} {r.get_json()}")
    _check("still nothing more to the gateway, the two records", port.written == [] and get_board().store.scratches() == {9: 22, 20: None})
    # A second also-eligible can stand in for another horse, and 22 (in the field) can itself be replaced.
    r = client.post("/api/quiniela/scratch", json={"horse": 3, "replacement": {"number": 21}})
    _check("3 -> 21: a second record, 21 unnamed", r.status_code == 200 and r.get_json()["now"] == {"number": 21, "name": "Mugatu"}
           and get_board().store.scratches() == {9: 22, 20: None, 3: 21}, str(r.get_json()))
    r = client.post("/api/quiniela/scratch", json={"horse": 22, "replacement": {"number": 23}})
    _check("22 -> 23: records chain, both pairs in the line, the cup that says 22 named", r.status_code == 200
           and r.get_json()["cup"] == mac_of(9) and get_board().store.scratches() == {9: 22, 20: None, 3: 21, 22: 23}
           and b.renum == [(3, 21), (9, 22), (22, 23)], str((r.get_json(), b.renum)))
    b.handle_raw_line(telem(mac_of(9), horse=23, count=0))        # the cup walked the chain to 23
    b.handle_raw_line(telem(mac_of(3), horse=21, count=0))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("the model: 9 and 22 out, 23 in on the cup that was 9 replacing OCELLI, four scratches ordered by was",
           m["horses"]["9"]["in_field"] is False and m["horses"]["22"]["in_field"] is False and m["horses"]["23"]["in_field"] is True
           and m["horses"]["23"]["cup"] == mac_of(9) and m["horses"]["23"]["replaced"] == "OCELLI"
           and [s["was"]["number"] for s in m["scratches"]] == [3, 9, 20, 22], str(m["scratches"]))
    _check("no free number left among 21-24 but 24", [n for n in range(21, 25) if not any(
        n in (s["was"]["number"], (s["now"] or {}).get("number")) for s in m["scratches"])] == [24])
    # A chain is undone last record first: with 22 -> 23 standing, dropping the 9 -> 22 record would leave the cup on 23
    # with no record for it and 22 out of the field with no way back.
    port.written.clear()
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("undo 9 while 22 -> 23 stands -> 400 undo 22 first, nothing moved",
           r.status_code == 400 and r.get_json() == {"ok": False, "error": "horse 9: undo 22 first"} and port.written == []
           and get_board().store.scratches() == {9: 22, 20: None, 3: 21, 22: 23}, f"{r.status_code} {r.get_json()}")
    r = client.post("/api/quiniela/unscratch", json={"horse": 22})
    _check("undo 22 first: the pair [23, 22] goes down, the cup that says 23 named", r.status_code == 200
           and r.get_json()["cup"] == mac_of(9) and r.get_json()["renum"] == [23, 22] and (23, 22) in b.renum
           and (22, 23) not in b.renum, str((r.get_json(), b.renum)))
    b.handle_raw_line(telem(mac_of(9), horse=22, count=0))        # the cup went back to 22
    get_board().refresh()
    _check("...and the undo pair leaves the line once the cup reports 22", (23, 22) not in b.renum, str(b.renum))
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("then undo 9: [22, 9] down, 3 -> 21 and the 20 record still stand", r.status_code == 200 and r.get_json()["renum"] == [22, 9]
           and get_board().store.scratches() == {20: None, 3: 21}, str(r.get_json()))


def test_scratch_before_any_cup_reports_the_horse():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    body = r.get_json()
    _check("a scratch before any cup says 9: recorded, cup null, the pair goes down all the same",
           r.status_code == 200 and body == {"ok": True, "kind": "replacement", "cup": None, "renum": [9, 22], "rev": 2,
                                             "gateway_online": False, "names_rev": 2,
                                             "was": {"number": 9, "name": "Encino"}, "now": {"number": 22, "name": "Ocelli"}}
           and port.lines() == [state_line(2, 0, [], [(9, 22)])], str((body, port.lines())))
    m = client.get("/api/quiniela").get_json()
    _check("the model: 9 out, 22 in on no cup, the record listed",
           m["horses"]["9"]["in_field"] is False and m["horses"]["22"]["in_field"] is True and m["horses"]["22"]["cup"] is None
           and m["scratches"] == [{"was": {"number": 9, "name": "ENCINO"}, "now": {"number": 22, "name": "OCELLI"}}], str(m["scratches"]))
    # A cup set to 9 on its own screen hears the pair and reports 22.
    b.handle_raw_line(telem(MAC_A, horse=22, count=0))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("a cup reporting 22 is 22's cup; 9 has none", m["horses"]["22"]["cup"] == MAC_A and m["horses"]["9"]["cup"] is None)
    b.handle_raw_line(telem(MAC_B, horse=24, count=1))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("a cup set to an also-eligible that is the now of no record: listed on it, not in the field, its tokens counted but not a row",
           m["horses"]["24"]["cup"] == MAC_B and m["horses"]["24"]["in_field"] is False and m["horses"]["24"]["tokens"] == 1
           and m["total_tokens"] == 1, str(m["horses"]["24"]))
    n_lines = len(port.lines())
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    body = r.get_json()
    _check("undo: the record goes, [22, 9] goes down, the cup that says 22 named",
           r.status_code == 200 and body["kind"] == "replacement" and body["cup"] == MAC_A and body["renum"] == [22, 9]
           and get_board().store.scratches() == {} and len(port.lines()) == n_lines + 1
           and port.lines()[-1] == state_line(b.state_rev, 0, [], [(22, 9)]), str(body))
    # A chain: 9 -> 22, then 22 -> 23; the cup walks it; undo walks back one step at a time.
    b.handle_raw_line(telem(MAC_A, horse=9, count=0))
    get_board().refresh()
    client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22}})
    r = client.post("/api/quiniela/scratch", json={"horse": 22, "replacement": {"number": 23, "name": "Epic Ride"}})
    _check("9 -> 22 -> 23: both pairs in the line", r.status_code == 200 and b.renum == [(9, 22), (22, 23)], str(b.renum))
    b.handle_raw_line(telem(MAC_A, horse=23, count=0))
    get_board().refresh()
    _check("the cup walked the chain to 23", client.get("/api/quiniela").get_json()["horses"]["23"]["cup"] == MAC_A)
    r = client.post("/api/quiniela/unscratch", json={"horse": 22})
    _check("undo 22: [23, 22] down, the 9 -> 22 record stands", r.status_code == 200 and r.get_json()["renum"] == [23, 22]
           and get_board().store.scratches() == {9: 22} and b.renum == [(9, 22), (23, 22)], str(b.renum))
    b.handle_raw_line(telem(MAC_A, horse=22, count=0))
    get_board().refresh()
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("undo 9: [22, 9] down, no records", r.status_code == 200 and r.get_json()["renum"] == [22, 9]
           and get_board().store.scratches() == {} and b.renum == [(22, 9)], str(b.renum))
    _check("23's name stays stored", get_board().store.horses()[23] == {"name": "Epic Ride"})
    r = client.post("/api/quiniela/unscratch", json={"horse": 22})
    _check("nothing left to undo -> 400", r.status_code == 400 and r.get_json()["error"] == "horse 22 is not scratched")


def test_kind2_scratch_removes_tokens_from_the_pot():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    _check("the board's store lives on the bridge's database", board.store._db is b.db)
    b.set_state(phase=1)
    b.handle_raw_line(telem(MAC_A, horse=9, count=10))
    b.handle_raw_line(telem(MAC_B, horse=3, count=5))
    board.refresh()
    m = board.model()
    _check("pot 15 of 15 tokens, prizes 9 / 4 / 2", m["pot"] == 15.0 and m["total_tokens"] == 15
           and m["prizes"] == {"win": 9, "place": 4, "show": 2}, str((m["pot"], m["prizes"])))
    port.written.clear()
    board.store.scratch_gateway(9)                       # the no-replacement kind: a record
    board.refresh()
    _check("its bit goes down in one state line", port.lines() == [state_line(3, 1, [9])], str(port.lines()))
    m = board.model()
    _check("horse 9 scratched, out of the field", m["horses"]["9"]["scratched"] is True and m["horses"]["9"]["tokens"] == 10
           and m["horses"]["9"]["in_field"] is False)
    _check("its tokens leave the pot: 5, prizes 3 / 1 / 1", m["pot"] == 5.0 and m["prizes"] == {"win": 3, "place": 1, "show": 1},
           str((m["pot"], m["prizes"])))
    _check("total_tokens still counts every cup: 15", m["total_tokens"] == 15)
    _check("share unchanged (of every cup)", m["horses"]["9"]["share"] == round(10 / 15, 4))
    _check("scratches carries {was, now: null}, no replaced, names_rev bumped by the record",
           m["scratches"] == [{"was": {"number": 9, "name": ""}, "now": None}] and m["horses"]["9"]["replaced"] is None
           and m["names_rev"] == 1 and board.store.scratches() == {9: None}, str(m["scratches"]))
    _check("no event for the scratch", m["events"] == [], str(m["events"]))
    board.store.unscratch_gateway(9)
    board.refresh()
    _check("unscratched: the bit leaves the line, the pot is back, 9 in the field, no scratches",
           port.lines()[-1] == state_line(4, 1) and board.model()["pot"] == 15.0
           and board.model()["horses"]["9"]["in_field"] is True and board.model()["scratches"] == [])
    b.set_state(scratched=[3])                           # a bit set on the bridge behind the store's back
    board.refresh()
    _check("the store is the source: refresh() takes a stray bit back out", b.scratched == [] and port.lines()[-1] == state_line(6, 1))


def test_no_replacement_scratch_is_about_the_horse():
    """A no-replacement scratch is recorded in pi5 whether or not a cup says
    it is that horse; the horse's bit rides in every state line, so a cup
    set to it later draws its X too; unscratch mirrors it; a reset keeps
    the record."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    wall = FakeClock(1_700_000_000.0)
    client = _make_board_app(b, wall=wall, log_dir=tmpdir() / "logs").test_client()
    b.set_state(phase=1)
    b.handle_raw_line(telem(MAC_A, horse=7, count=3))                # one cup, horse 7
    client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("before: 20 in the field on no cup, pot 3", m["horses"]["20"]["in_field"] is True and m["horses"]["20"]["cup"] is None and m["pot"] == 3.0)
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 20})
    body = r.get_json()
    _check("scratch 20 with no cup: 200, kind gateway, cup null, names_rev 2, the bit sent",
           r.status_code == 200 and body["ok"] is True and body["kind"] == "gateway" and body["horse"] == 20
           and body["cup"] is None and body["names_rev"] == 2 and body["scratched"] is True, str(body))
    _check("one state line with 20's bit", port.lines() == [state_line(3, 1, [20])], str(port.lines()))
    m = client.get("/api/quiniela").get_json()
    h20 = m["horses"]["20"]
    _check("the model: 20 scratched, out of the field, no cup, 0 tokens", h20["scratched"] is True and h20["in_field"] is False
           and h20["cup"] is None and h20["tokens"] == 0, str(h20))
    _check("scratches carries {was 20, now null}", m["scratches"] == [{"was": {"number": 20, "name": "SOCIETY MAN"}, "now": None}], str(m["scratches"]))
    _check("pot unchanged, no events", m["pot"] == 3.0 and m["events"] == [], str((m["pot"], m["events"])))
    _check("persisted: {20: None} on the bridge's database", HorseStore(b.db).scratches() == {20: None})
    r = client.post("/api/quiniela/scratch", json={"horse": 20})
    _check("scratching it again -> 400 already scratched", r.status_code == 400 and r.get_json()["error"] == "horse 20 is already scratched", str(r.get_json()))
    r = client.post("/api/quiniela/scratch", json={"horse": 20, "replacement": {"number": 21, "name": "Great White"}})
    _check("a replacement for it now -> 400 already scratched", r.status_code == 400 and r.get_json()["error"] == "horse 20 is already scratched", str(r.get_json()))
    r = client.post("/api/quiniela/scratch", json={"horse": 8, "replacement": {"number": 20, "name": "x"}})
    _check("20 as somebody's replacement -> 400 in use", r.status_code == 400 and r.get_json()["error"] == "20 is in use", str(r.get_json()))
    # A cup set to 20 on its own screen later: the bit is already in the line, so it draws its X at once.
    b.handle_raw_line(telem(MAC_B, horse=20, count=4))               # four tokens, to be handed back
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("20 on its cup, scratched, its 4 tokens out of the pot: pot 3, total 7",
           m["horses"]["20"]["cup"] == MAC_B and m["horses"]["20"]["scratched"] is True and m["horses"]["20"]["tokens"] == 4
           and m["pot"] == 3.0 and m["total_tokens"] == 7, str((m["horses"]["20"], m["pot"], m["total_tokens"])))
    # Unscratch mirrors it: record gone, the bit out of the line, back in the field with its tokens in the pot.
    port.written.clear()
    r = client.post("/api/quiniela/unscratch", json={"horse": 20})
    body = r.get_json()
    _check("unscratch 20: 200, kind gateway, the cup named, names_rev 3", r.status_code == 200 and body["kind"] == "gateway"
           and body["cup"] == MAC_B and body["scratched"] is False and body["names_rev"] == 3, str(body))
    _check("one state line without the bit", port.lines() == [state_line(4, 1)], str(port.lines()))
    m = client.get("/api/quiniela").get_json()
    _check("20 back in the field, its 4 tokens in the pot: 7, no scratches", m["horses"]["20"]["in_field"] is True
           and m["horses"]["20"]["scratched"] is False and m["pot"] == 7.0 and m["scratches"] == [], str((m["horses"]["20"], m["pot"])))
    _check("the record is gone", HorseStore(b.db).scratches() == {})
    r = client.post("/api/quiniela/unscratch", json={"horse": 20})
    _check("unscratching again -> 400", r.status_code == 400 and r.get_json()["error"] == "horse 20 is not scratched", str(r.get_json()))
    # A reset keeps the record and the bit.
    client.post("/api/quiniela/scratch", json={"horse": 20})
    r = client.post("/api/quiniela/reset")
    _check("after a reset the record stands and the bit is still in the line", r.status_code == 200
           and HorseStore(b.db).scratches() == {20: None} and b.scratched == [20] and port.lines()[-1] == state_line(b.state_rev, 0, [20]),
           str(port.lines()[-1]))
    m = client.get("/api/quiniela").get_json()
    _check("...and the model still shows 20 scratched on its cup", m["horses"]["20"]["scratched"] is True and m["horses"]["20"]["cup"] == MAC_B)


def test_lq_scratches_migration():
    """lq_scratches from c70d894 has `now INTEGER NOT NULL` and is live on
    DevPi; a no-replacement scratch is a row with now NULL, so init_schema()
    rebuilds the table, rows kept."""
    path = str(tmpdir() / "devpi_scratches.db")
    db = LqDb(path)
    db.conn.executescript("""
        CREATE TABLE lq_scratches (
            was INTEGER PRIMARY KEY CHECK (was BETWEEN 1 AND 24),
            now INTEGER NOT NULL CHECK (now BETWEEN 1 AND 24)
        );
        INSERT INTO lq_scratches VALUES (9, 22);
    """)
    try:
        db.conn.execute("INSERT INTO lq_scratches (was, now) VALUES (20, NULL)")
        _check("the old table refuses now NULL", False)
    except sqlite3.IntegrityError:
        _check("the old table refuses now NULL", True)
    _check("check_shape() passes on the old table", db.check_shape() is None)
    db.init_schema()
    sql = db.query_one("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lq_scratches'")["sql"]
    _check("init_schema() rebuilt lq_scratches with a nullable now", "NOT NULL" not in sql and "IS NULL" in sql, sql)
    _check("...the row kept", db.load_scratches() == {9: 22})
    _check("...no lq_scratches_new left behind", db.query_one("SELECT name FROM sqlite_master WHERE name = 'lq_scratches_new'") is None)
    db.save_scratch(20, None)
    _check("a no-replacement scratch fits now", db.load_scratches() == {9: 22, 20: None})
    _check("the store reads both kinds back", HorseStore(db).scratches() == {9: 22, 20: None}
           and HorseStore(db).record(20) == ("gateway", None) and HorseStore(db).record(9) == ("replacement", 22)
           and HorseStore(db).gateway_scratches() == {20})
    _check("a second init_schema() finds nothing to migrate", db._migrate_lq_scratches() is False and db.load_scratches() == {9: 22, 20: None})
    _check("check_shape() still passes", db.check_shape() is None)
    db.close()


def test_reset_clears_closes_at_and_keeps_names():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    b.set_state(phase=1)
    b.handle_raw_line(telem(MAC_A, horse=9, count=10))
    board.refresh()
    board.store.set_names({9: "Encino", 3: "Fierceness"})
    board.store.scratch_replace(3, 21, "Late Entry")
    board.store.set_closes_at(1_700_000_600.0)
    board.refresh()
    m = board.model()
    record = {"was": {"number": 3, "name": "FIERCENESS"}, "now": {"number": 21, "name": "LATE ENTRY"}}
    _check("before the reset: names, a record, closes_at", m["horses"]["9"]["name"] == "ENCINO"
           and m["scratches"] == [record] and m["closes_at"] == 1_700_000_600.0, str(m["scratches"]))
    _check("persisted: a second store on the same db reads them", HorseStore(b.db).closes_at == 1_700_000_600.0
           and HorseStore(b.db).horses()[9]["name"] == "Encino" and HorseStore(b.db).names_rev == 2
           and HorseStore(b.db).scratches() == {3: 21})
    board.reset_betting()
    m = board.model()
    _check("after the reset: closes_at cleared", m["closes_at"] is None and board.store.closes_at is None)
    _check("...in the database too", HorseStore(b.db).closes_at is None)
    _check("...names and the record stay, names_rev untouched, the pair still in the line", m["horses"]["9"]["name"] == "ENCINO"
           and m["scratches"] == [record] and m["names_rev"] == 2 and m["horses"]["21"]["in_field"] is True and b.renum == [(3, 21)])
    _check("...PRE_RACE, the cup still on its horse, no ghosts", m["race_state"] == 0 and m["events"] == []
           and m["horses"]["9"]["cup"] == MAC_A and m["pot"] == 10.0)
    board.store.set_closes_at(1_700_000_900.0)
    board.refresh()
    _check("a second refresh without a reset keeps a new closes_at", board.model()["closes_at"] == 1_700_000_900.0)
    # A database with only the bridge's tables: check_shape passes and init_schema adds the three.
    path = str(tmpdir() / "old.db")
    db = LqDb(path)
    db.init_schema()
    db.conn.executescript("DROP TABLE lq_horses; DROP TABLE lq_scratches; DROP TABLE lq_board;")
    _check("check_shape() passes on a database without the board's tables", db.check_shape() is None)
    db.init_schema()
    _check("init_schema() adds them, empty", db.load_horses() == {} and db.load_scratches() == {}
           and db.load_board() == {"names_rev": 0, "closes_at": None})
    db.save_horse(9, "Encino")
    db.save_horse(9, "Epic Ride")
    db.save_board(3, 12.5)
    _check("save_horse upserts, save_board updates", db.load_horses() == {9: {"name": "Epic Ride", "replaced": None}}
           and db.load_board() == {"names_rev": 3, "closes_at": 12.5})
    _check("a store loads them", HorseStore(db).horses()[9] == {"name": "Epic Ride"}
           and HorseStore(db).names_rev == 3 and HorseStore(db).closes_at == 12.5)
    db.conn.executescript("DROP TABLE lq_horses; DROP TABLE lq_scratches; DROP TABLE lq_board;")
    with capture_logs("la_quiniela.horses", logging.ERROR) as cap:
        store = HorseStore(db)
    _check("a database without the tables leaves a memory-only store and one ERROR",
           store._db is None and len(cap.messages("cannot read")) == 1 and store.set_names({1: "x"}) is True)
    db.close()


def test_lq_horses_migration_and_scratches_table():
    """DevPi's la_subasta.db has lq_horses with CHECK (horse BETWEEN 1 AND 20)
    from 275a64f; init_schema() rebuilds it for 1..24 and adds lq_scratches."""
    path = str(tmpdir() / "devpi.db")
    db = LqDb(path)
    db.conn.executescript("""
        CREATE TABLE lq_horses (
            horse    INTEGER PRIMARY KEY CHECK (horse BETWEEN 1 AND 20),
            name     TEXT    NOT NULL DEFAULT '',
            replaced TEXT
        );
        INSERT INTO lq_horses VALUES (9, 'Encino', NULL), (17, 'Fierceness', NULL), (3, 'Mystik Dan', 'Dornoch');
        CREATE TABLE lq_board (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            names_rev INTEGER NOT NULL DEFAULT 0,
            closes_at REAL
        );
        INSERT INTO lq_board VALUES (1, 4, NULL);
    """)
    try:
        db.conn.execute("INSERT INTO lq_horses (horse, name) VALUES (22, 'Ocelli')")
        _check("the old CHECK refuses horse 22", False)
    except sqlite3.IntegrityError:
        _check("the old CHECK refuses horse 22", True)
    _check("check_shape() passes on the old table (it compares column names only)", db.check_shape() is None)
    db.init_schema()
    sql = db.query_one("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lq_horses'")["sql"]
    _check("init_schema() rebuilt lq_horses with the 1..24 CHECK", "BETWEEN 1 AND 24" in sql and "BETWEEN 1 AND 20" not in sql, sql)
    _check("...rows kept, the legacy replaced value included",
           db.load_horses() == {3: {"name": "Mystik Dan", "replaced": "Dornoch"}, 9: {"name": "Encino", "replaced": None},
                                17: {"name": "Fierceness", "replaced": None}}, str(db.load_horses()))
    _check("...lq_board untouched", db.load_board() == {"names_rev": 4, "closes_at": None})
    _check("...no lq_horses_new left behind, lq_scratches created empty",
           db.query_one("SELECT name FROM sqlite_master WHERE name = 'lq_horses_new'") is None and db.load_scratches() == {})
    _check("check_shape() still passes", db.check_shape() is None)
    db.save_horse(22, "Ocelli")
    _check("horse 22 fits now", db.load_horses()[22] == {"name": "Ocelli", "replaced": None})
    _check("a second init_schema() finds nothing to migrate and changes nothing",
           db._migrate_lq_horses() is False and db.init_schema() is None and db.load_horses()[22]["name"] == "Ocelli"
           and db.load_horses()[3]["replaced"] == "Dornoch")
    # An lq_horses_new left by an interrupted run does not block the rebuild.
    db.conn.executescript("CREATE TABLE lq_horses_new (x INTEGER); DROP TABLE lq_horses;"
                          "CREATE TABLE lq_horses (horse INTEGER PRIMARY KEY CHECK (horse BETWEEN 1 AND 20), "
                          "name TEXT NOT NULL DEFAULT '', replaced TEXT); INSERT INTO lq_horses VALUES (5, 'Catalytic', NULL);")
    db.init_schema()
    _check("a stale lq_horses_new is dropped and the rebuild goes through",
           db.load_horses() == {5: {"name": "Catalytic", "replaced": None}} and db.query_one(
               "SELECT name FROM sqlite_master WHERE name = 'lq_horses_new'") is None
           and "BETWEEN 1 AND 24" in db.query_one("SELECT sql FROM sqlite_master WHERE name = 'lq_horses'")["sql"])
    db.conn.execute("UPDATE lq_horses SET replaced = 'Dornoch' WHERE horse = 5")
    # lq_scratches: one row per replacement record, was the key.
    db.save_scratch(9, 22)
    db.save_scratch(9, 23)
    db.save_scratch(1, 21)
    _check("save_scratch upserts on was", db.load_scratches() == {9: 23, 1: 21}, str(db.load_scratches()))
    db.delete_scratch(9)
    db.delete_scratch(9)
    _check("delete_scratch removes, twice is fine", db.load_scratches() == {1: 21})
    for bad in ((0, 21), (25, 21), (9, 0), (9, 25)):
        try:
            db.save_scratch(*bad)
            _check(f"lq_scratches CHECK refuses {bad}", False)
        except sqlite3.IntegrityError:
            _check(f"lq_scratches CHECK refuses {bad}", True)
    _check("...and nothing was written by them", db.load_scratches() == {1: 21})
    # The legacy replaced column: one WARNING at load, the store ignores it, the next save clears it.
    with capture_logs("la_quiniela.horses") as cap:
        store = HorseStore(db)
    _check("one WARNING for the legacy name-swap on horse 5",
           cap.messages() == ["La Quiniela horses: legacy name-swap replacement on horse 5 ignored; "
                              "scratch it again with a number"], str(cap.messages()))
    _check("the store ignores it and loads the record from lq_scratches",
           store.horses()[5] == {"name": "Catalytic"} and store.scratches() == {1: 21} and store.names_rev == 4)
    store.set_names({5: "Catalytic II"})
    _check("the next save of that horse writes replaced NULL", db.load_horses()[5] == {"name": "Catalytic II", "replaced": None})
    with capture_logs("la_quiniela.horses") as cap:
        HorseStore(db)
    _check("...so the next load has nothing to warn about", cap.messages() == [], str(cap.messages()))
    _check("a store without a database has no records", HorseStore().scratches() == {})
    # A fresh database gets the new shape straight away; a wrong lq_scratches is reported like any other table.
    db2 = LqDb(str(tmpdir() / "fresh.db"))
    db2.init_schema()
    _check("a fresh database: lq_horses 1..24, lq_scratches, check_shape passes",
           "BETWEEN 1 AND 24" in db2.query_one("SELECT sql FROM sqlite_master WHERE name = 'lq_horses'")["sql"]
           and db2.load_scratches() == {} and db2.check_shape() is None and db2._migrate_lq_horses() is False)
    db2.conn.executescript("DROP TABLE lq_scratches; CREATE TABLE lq_scratches (was INTEGER, now INTEGER, extra TEXT);")
    _check("a wrong-shaped lq_scratches is reported, never altered", (db2.check_shape() or "").startswith("table lq_scratches already exists"))
    db.close()
    db2.close()


def test_routes_horses_get_and_put():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    r = client.get("/api/quiniela/horses")
    _check("GET /api/quiniela/horses 200, no-store", r.status_code == 200 and r.headers.get("Cache-Control") == "no-store")
    _check("24 entries, empty, name only", r.get_json() == {str(n): {"name": ""} for n in range(1, 25)}, str(r.get_json()))
    r = client.put("/api/quiniela/horses", json={"text": DERBY_TEXT})
    body = r.get_json()
    _check("PUT text -> ok, names_rev 1, horses as typed", r.status_code == 200 and body["ok"] is True and body["names_rev"] == 1
           and body["horses"]["9"] == {"name": "Encino"} and body["horses"]["17"]["name"] == "Fierceness"
           and body["horses"]["22"] == {"name": ""}, str(body))
    m = client.get("/api/quiniela").get_json()
    _check("the model follows synchronously, upper-cased", m["horses"]["9"]["name"] == "ENCINO" and m["names_rev"] == 1)
    _check("GET round trip as typed", client.get("/api/quiniela/horses").get_json() == body["horses"])
    r = client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})
    h = r.get_json()["horses"]
    _check("PUT 24 lines: the also-eligibles named, names_rev 2", r.status_code == 200 and r.get_json()["names_rev"] == 2
           and h["21"]["name"] == "Mugatu" and h["22"]["name"] == "Ocelli" and h["24"]["name"] == "Society Girl", str(h))
    r = client.put("/api/quiniela/horses", json={"text": "22. Ocelli II"})
    _check("PUT a '22.' prefix names the also-eligible alone", r.status_code == 200 and r.get_json()["horses"]["22"] == {"name": "Ocelli II"}
           and r.get_json()["horses"]["21"]["name"] == "Mugatu" and client.get("/api/quiniela").get_json()["horses"]["22"]["name"] == "OCELLI II")
    client.put("/api/quiniela/horses", json={"text": DERBY_TEXT + "\n21.\n22.\n23.\n24."})
    r = client.put("/api/quiniela/horses", json={"text": "Dornoch\n\n#3 Mystik Dan II\n7 Honor Marie II\n5) Fierce\n6: Just A Touch\n20. Society Man II"})
    h = r.get_json()["horses"]
    _check("line order, blank line leaves horse 2, punctuated prefixes win over line order, and in this plain "
           "(not consistently numbered) list a bare '7 Honor Marie II' is line 4's name", r.status_code == 200
           and h["1"]["name"] == "Dornoch" and h["2"]["name"] == "Sierra Leone" and h["3"]["name"] == "Mystik Dan II"
           and h["4"]["name"] == "7 Honor Marie II" and h["7"]["name"] == "Honor Marie" and h["5"]["name"] == "Fierce"
           and h["6"]["name"] == "Just A Touch" and h["20"]["name"] == "Society Man II", str(h))
    _check("names_rev 5", r.get_json()["names_rev"] == 5)
    r = client.put("/api/quiniela/horses", json={"text": "1. Dornoch"})
    _check("an unchanged name does not bump names_rev", r.status_code == 200 and r.get_json()["names_rev"] == 5)
    for bad, why in (({"text": "\n".join(FIELD_24) + "\nExtra"}, "more than 24"), ({"text": "25. X"}, "not in 1-24"),
                     ({"text": "0. X"}, "not in 1-24"), ({"text": 5}, "must be a string"), ({"1": {"nope": 1}}, "expected"),
                     ({"1": "Dornoch"}, "expected"), ({"0": {"name": "x"}}, "not in 1-24"), ({"25": {"name": "x"}}, "not in 1-24"),
                     ({"1": {"name": 5}}, "must be a string")):
        r = client.put("/api/quiniela/horses", json=bad)
        _check(f"PUT {bad!r:.40} -> 400 {why}", r.status_code == 400 and r.get_json()["ok"] is False
               and why in r.get_json()["error"], f"{r.status_code} {r.get_json()}")
    r = client.put("/api/quiniela/horses", data="[]", content_type="application/json")
    _check("a non-object body -> 400", r.status_code == 400)
    _check("nothing changed by the 400s", client.get("/api/quiniela/horses").get_json() == h)
    r = client.put("/api/quiniela/horses", json={"1": {"name": "Dornoch II", "replaced": "Dornoch"}, "2": {"name": "Sierra Leone"},
                                                 "23": {"name": "Epic Ride"}})
    _check("the dict shape: name required, a legacy replaced key ignored, 1..24", r.status_code == 200
           and r.get_json()["horses"]["1"] == {"name": "Dornoch II"} and r.get_json()["horses"]["2"] == {"name": "Sierra Leone"}
           and r.get_json()["horses"]["23"] == {"name": "Epic Ride"}, str(r.get_json()))
    m = client.get("/api/quiniela").get_json()
    _check("...and the model lists no scratch for it (replaced is not settable)", m["scratches"] == [] and m["horses"]["1"]["replaced"] is None)
    r = client.put("/api/quiniela/horses", json={"1": {"name": "Dornoch", "replaced": None}})
    _check("replaced null is ignored too", r.get_json()["horses"]["1"] == {"name": "Dornoch"}
           and client.get("/api/quiniela").get_json()["scratches"] == [])
    _check("persisted on the bridge's database", HorseStore(b.db).horses()[3]["name"] == "Mystik Dan II"
           and HorseStore(b.db).horses()[23] == {"name": "Epic Ride"})


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


def test_routes_scratch_and_unscratch():
    """The renumber on a real bridge, through the routes: 9 -> 22 sends the
    pair, the cup that says 9 comes back saying 22 with its tokens, no
    events, the pot never moves; undo sends the pair back until the cup
    reports 9. Then the no-replacement kind, and the routes without a
    bridge."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    wall = FakeClock(1_700_000_000.0)
    log_dir = tmpdir() / "logs"
    client = _make_board_app(b, wall=wall, log_dir=log_dir).test_client()
    b.set_state(phase=1)
    b.handle_raw_line(telem(MAC_A, horse=9, count=10))
    b.handle_raw_line(telem(MAC_B, horse=3, count=5))
    client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})     # refreshes: the baseline
    wall.advance(1)
    b.handle_raw_line(telem(MAC_A, horse=9, count=12))          # a real bet on 9, so the ticker has something to keep
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("before: 9 on its cup with 12 tokens, one event, pot 17, names_rev 1",
           m["horses"]["9"]["cup"] == MAC_A and m["horses"]["9"]["tokens"] == 12 and m["events"] == [{"horse": 9, "delta": 2, "ts": wall.t}]
           and m["pot"] == 17.0 and m["names_rev"] == 1, str((m["horses"]["9"], m["events"], m["pot"])))
    events_before = m["events"]
    port.written.clear()
    wall.advance(1)
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    body = r.get_json()
    _check("scratch 9 -> 22: kind replacement, the cup that says 9, the pair, names_rev 2, names as typed",
           r.status_code == 200 and body == {"ok": True, "kind": "replacement", "cup": MAC_A, "renum": [9, 22], "rev": 3,
                                             "gateway_online": True, "names_rev": 2,
                                             "was": {"number": 9, "name": "Encino"}, "now": {"number": 22, "name": "Ocelli"}}, str(body))
    _check("one state line went down with the pair, byte-exact",
           port.lines() == [state_line(3, 1, [], [(9, 22)])], str(port.lines()))
    _check("the bridge holds the pair", b.renum == [(9, 22)])
    m = client.get("/api/quiniela").get_json()
    _check("until the cup reports 22, 9 still shows on it (the record is in, the cup has not spoken)",
           m["horses"]["9"]["cup"] == MAC_A and m["horses"]["9"]["in_field"] is False and m["horses"]["22"]["cup"] is None
           and m["horses"]["22"]["in_field"] is True, str((m["horses"]["9"], m["horses"]["22"])))
    b.handle_raw_line(telem(MAC_A, horse=22, count=12))         # the cup followed the pair
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("the model: 22 on the cup with the 12 tokens, in the field, OCELLI replacing ENCINO",
           m["horses"]["22"] == {"tokens": 12, "share": round(12 / 17, 4), "scratched": False, "online": True, "cup": MAC_A,
                                 "conflict": False, "cups": [MAC_A], "name": "OCELLI", "replaced": "ENCINO", "in_field": True, "odds": None},
           str(m["horses"]["22"]))
    _check("9 left the field: no cup, 0 tokens, name kept",
           m["horses"]["9"] == dict(UNASSIGNED, name="ENCINO"), str(m["horses"]["9"]))
    _check("scratches carries the record", m["scratches"] == [{"was": {"number": 9, "name": "ENCINO"}, "now": {"number": 22, "name": "OCELLI"}}])
    _check("NO events: the ticker is exactly as before", m["events"] == events_before, str(m["events"]))
    _check("the pot did not move: 17, total 17, prizes 10 / 4 / 3", m["pot"] == 17.0 and m["total_tokens"] == 17
           and m["prizes"] == {"win": 10, "place": 4, "show": 3}, str((m["pot"], m["prizes"])))
    _check("names_rev bumped once", m["names_rev"] == 2)
    _, lines = log_lines(log_dir)
    _check("the log: cup marks on both horses, not a bet, not a baseline",
           lines[-1]["changes"] == [{"horse": 9, "tokens": [12, 0], "cup": [MAC_A, None]}, {"horse": 22, "tokens": [0, 12], "cup": [None, MAC_A]}]
           and "baseline" not in lines[-1], str(lines[-1]))
    _check("persisted: the record and the name on the bridge's database",
           HorseStore(b.db).scratches() == {9: 22} and HorseStore(b.db).horses()[22] == {"name": "Ocelli"})
    wall.advance(1)
    b.handle_raw_line(telem(MAC_A, horse=22, count=13))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("a bet in the cup after the renumber is a bet on 22", m["events"][0] == {"horse": 22, "delta": 1, "ts": wall.t}
           and m["horses"]["22"]["tokens"] == 13 and m["pot"] == 18.0, str(m["events"]))
    events_before = m["events"]
    # Undo.
    wall.advance(1)
    port.written.clear()
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    body = r.get_json()
    _check("unscratch 9: kind replacement, the cup that says 22, the pair back, names_rev 3",
           r.status_code == 200 and body == {"ok": True, "kind": "replacement", "cup": MAC_A, "renum": [22, 9], "rev": 4,
                                             "gateway_online": True, "names_rev": 3,
                                             "was": {"number": 9, "name": "Encino"}, "now": {"number": 22, "name": "Ocelli"}}, str(body))
    _check("the line carries [22, 9] and no longer [9, 22], byte-exact", port.lines() == [state_line(4, 1, [], [(22, 9)])], str(port.lines()))
    b.handle_raw_line(telem(MAC_A, horse=9, count=13))          # the cup went back
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("9 back on its cup with the 13 tokens, in the field", m["horses"]["9"]["cup"] == MAC_A and m["horses"]["9"]["tokens"] == 13
           and m["horses"]["9"]["in_field"] is True and m["horses"]["9"]["replaced"] is None, str(m["horses"]["9"]))
    _check("22 out of the field, no cup, no tokens, its name still stored",
           m["horses"]["22"]["in_field"] is False and m["horses"]["22"]["cup"] is None and m["horses"]["22"]["tokens"] == 0
           and m["horses"]["22"]["name"] == "OCELLI" and client.get("/api/quiniela/horses").get_json()["22"] == {"name": "Ocelli"})
    _check("no events from the undo, pot still 18, no scratches", m["events"] == events_before and m["pot"] == 18.0 and m["scratches"] == [],
           str(m["events"]))
    _check("the undo pair left the line once the cup reported 9", b.renum == [] and port.lines()[-1] == state_line(5, 1), str(port.lines()))
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("neither kind applies -> 400", r.status_code == 400 and r.get_json() == {"ok": False, "error": "horse 9 is not scratched"})
    # The no-replacement kind: recorded in pi5 (was 9, now None) and the bit goes down. A repeat is a 400.
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 9})
    got = r.get_json()
    _check("{'horse': 9} -> kind gateway, the cup that says 9, names_rev 4", r.status_code == 200 and got["ok"] is True and got["kind"] == "gateway"
           and got["cup"] == MAC_A and got["horse"] == 9 and got["scratched"] is True and got["gateway_online"] is True
           and got["names_rev"] == 4 and got["was"] == {"number": 9, "name": "Encino"} and got["rev"] == 6, str(got))
    for body in ({"horse": 9, "replacement": None}, {"horse": "9"}):
        r = client.post("/api/quiniela/scratch", json=body)
        _check(f"{body!r:.45} again -> 400 already scratched", r.status_code == 400 and r.get_json()["error"] == "horse 9 is already scratched", str(r.get_json()))
    _check("one state line went down, byte-exact", port.lines() == [state_line(6, 1, [9])], str(port.lines()))
    _check("persisted as a record with no replacement", HorseStore(b.db).scratches() == {9: None})
    m = client.get("/api/quiniela").get_json()
    _check("the model: scratched, out of the field, tokens out of the pot, a no-replacement entry in scratches",
           m["horses"]["9"]["scratched"] is True and m["horses"]["9"]["in_field"] is False and m["horses"]["9"]["tokens"] == 13
           and m["total_tokens"] == 18 and m["pot"] == 5.0 and m["prizes"] == {"win": 3, "place": 1, "show": 1}
           and m["scratches"] == [{"was": {"number": 9, "name": "ENCINO"}, "now": None}], str((m["pot"], m["scratches"])))
    _check("names_rev bumped by the record, no events", m["names_rev"] == 4 and m["events"] == events_before)
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    got = r.get_json()
    _check("unscratch clears the bit and the record", r.status_code == 200 and got["kind"] == "gateway" and got["scratched"] is False
           and got["rev"] == 7 and port.lines()[-1] == state_line(7, 1) and HorseStore(b.db).scratches() == {}, str(got))
    m = client.get("/api/quiniela").get_json()
    _check("the pot is back, 9 in the field, no scratches", m["pot"] == 18.0 and m["horses"]["9"]["in_field"] is True and m["scratches"] == [])
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 15})
    got = r.get_json()
    _check("the no-replacement kind on a horse no cup says: recorded, cup null, the bit sent all the same",
           r.status_code == 200 and got["kind"] == "gateway" and got["cup"] is None and port.lines() == [state_line(8, 1, [15])], str(got))
    m = client.get("/api/quiniela").get_json()
    _check("...15 scratched and out of the field with no cup, in scratches with now null",
           m["horses"]["15"]["scratched"] is True and m["horses"]["15"]["in_field"] is False and m["horses"]["15"]["cup"] is None
           and m["scratches"] == [{"was": {"number": 15, "name": "DOMESTIC PRODUCT"}, "now": None}], str(m["scratches"]))
    r = client.post("/api/quiniela/unscratch", json={"horse": 15})
    _check("...and undone", r.status_code == 200 and r.get_json()["kind"] == "gateway" and r.get_json()["cup"] is None, str(r.get_json()))
    port.written.clear()
    r = client.post("/api/quiniela/scratch", json={"horse": 15, "replacement": {"number": 21}})
    _check("...a replacement scratch needs no cup either: recorded with cup null, the pair sent", r.status_code == 200
           and r.get_json()["kind"] == "replacement" and r.get_json()["cup"] is None and port.lines() == [state_line(10, 1, [], [(15, 21)])],
           str(r.get_json()))
    m = client.get("/api/quiniela").get_json()
    _check("...21 in the field on no cup, DOMESTIC PRODUCT as replaced, its own stored name",
           m["horses"]["21"]["in_field"] is True and m["horses"]["21"]["cup"] is None and m["horses"]["21"]["replaced"] == "DOMESTIC PRODUCT"
           and m["horses"]["21"]["name"] == "MUGATU" and m["horses"]["15"]["in_field"] is False, str(m["horses"]["21"]))
    for bad in ({"horse": 0}, {"horse": 25}, {"horse": "x"}, {"horse": True}, {"horse": None}, {}, [], "x"):
        for path in ("/api/quiniela/scratch", "/api/quiniela/unscratch"):
            r = client.post(path, json=bad)
            _check(f"POST {path[14:]} {bad!r:.30} -> 400", r.status_code == 400 and r.get_json()["ok"] is False, f"{r.status_code} {r.get_json()}")
    get_board().bridge = None
    r = client.post("/api/quiniela/scratch", json={"horse": 9})
    _check("the no-replacement kind without a bridge: recorded, cup null, rev null", r.status_code == 200 and r.get_json()["kind"] == "gateway"
           and r.get_json()["cup"] is None and r.get_json()["rev"] is None, str(r.get_json()))
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("...and undone without one", r.status_code == 200 and r.get_json()["kind"] == "gateway")
    r = client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    _check("a replacement scratch needs no bridge: recorded, cup null", r.status_code == 200 and r.get_json()["kind"] == "replacement"
           and r.get_json()["cup"] is None, str(r.get_json()))
    _check("...and the model followed without a bridge", client.get("/api/quiniela").get_json()["horses"]["22"]["replaced"] == "ENCINO")
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("...and is undone without one", r.status_code == 200 and r.get_json()["kind"] == "replacement" and r.get_json()["cup"] is None)
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("nothing left to undo without a bridge -> 400", r.status_code == 400)


def test_renum_lifecycle():
    """What the state line carries over time: a record's pair for as long as
    the record stands; an undone pair sent back for UNDO_RENUM_S or until a
    cup reports the restored number; a fresh record that contradicts a
    pending undo wins; at most four pairs, records first."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    mono = FakeClock(5000.0)
    board = BettingBoard(bridge=b, clock=mono, wall=FakeClock(1_700_000_000.0), log_dir=tmpdir(), results_path=tmpdir() / "r.json")
    board.store.scratch_replace(9, 22)
    board.refresh()
    _check("a record: its pair in the line", b.renum == [(9, 22)] and port.lines()[-1] == state_line(2, 0, [], [(9, 22)]))
    board.queue_undo_renum(22, 9)
    board.store.unscratch_replace(9)
    board.refresh()
    _check("undone: the pair back, in the line", b.renum == [(22, 9)] and board.undo_renums() == [(22, 9)])
    mono.advance(UNDO_RENUM_S - 1)
    board.refresh()
    _check("still sent a second before the deadline", b.renum == [(22, 9)])
    mono.advance(2)
    board.refresh()
    _check("dropped after UNDO_RENUM_S with no cup reporting 9", b.renum == [] and board.undo_renums() == [])
    board.store.scratch_replace(9, 22)
    board.refresh()
    board.queue_undo_renum(22, 9)
    board.store.unscratch_replace(9)
    board.refresh()
    b.handle_raw_line(telem(MAC_A, horse=9, count=1))
    board.refresh()
    _check("dropped the moment a cup reports the restored number", b.renum == [] and board.undo_renums() == [])
    board.store.scratch_replace(9, 22)
    board.refresh()
    board.queue_undo_renum(22, 9)
    board.store.unscratch_replace(9)
    board.refresh()
    board.store.scratch_replace(9, 22)                   # scratched again while the undo is pending
    board.refresh()
    _check("a fresh record contradicting a pending undo wins: no ping-pong", b.renum == [(9, 22)] and board.undo_renums() == [])
    board.store.unscratch_replace(9)
    board.store.scratch_replace(1, 21)
    board.store.scratch_replace(2, 22)
    board.store.scratch_replace(3, 23)
    board.store.scratch_replace(4, 24)
    for to, was in ((21, 1), (22, 2), (10, 5)):        # (10, 5): as if 5 -> 10 had been undone
        board.queue_undo_renum(to, was)
    with capture_logs(LOGGER) as cap:
        board.refresh()
    _check("four records fill the slots; the undo pairs they contradict are dropped, the other waits, one warning",
           b.renum == [(1, 21), (2, 22), (3, 23), (4, 24)] and board.undo_renums() == [(10, 5)]
           and len(cap.messages("waiting for a free slot")) == 1, str((b.renum, board.undo_renums(), cap.messages())))
    with capture_logs(LOGGER) as cap:
        board.refresh()
    _check("...warned once, not on every refresh", cap.messages() == [], str(cap.messages()))
    mono.advance(1)
    board.store.unscratch_replace(4)
    board.queue_undo_renum(24, 4)
    board.refresh()
    _check("a freed slot takes the oldest waiting undo pair first", b.renum == [(1, 21), (2, 22), (3, 23), (10, 5)], str(b.renum))
    mono.advance(UNDO_RENUM_S - 1)                         # the older pair's minute is up, the newer one has a second left
    board.refresh()
    _check("...and the next one once that expires", b.renum == [(1, 21), (2, 22), (3, 23), (24, 4)]
           and board.undo_renums() == [(24, 4)], str(b.renum))
    _check("desired_state() says the same", board.desired_state(b.get_snapshot())[1] == b.renum)


def test_results_from_the_dashboard_file():
    """The dashboard's POST /api/results writes pi5/data/results.json; the
    board reads it into the state line's res and the model's results, and
    Reset betting clears it."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, _ = fresh_board(bridge=b)
    b.set_state(phase=5)
    board.refresh()
    _check("no file: results 0 0 0 and null in the model", b.results == [0, 0, 0] and board.model()["results"] is None)
    _check("...null in the JSON too", json.loads(board.model_json())["results"] is None and '"results":null' in board.model_json())
    _check("read_results with no file", read_results(board._results_path) == [0, 0, 0])
    write_results(board, 19, 1, 22)
    port.written.clear()
    board.refresh()
    _check("the file goes into the state line", port.lines() == [state_line(3, 5, [], [], [19, 1, 22])], str(port.lines()))
    _check("...and the model", board.model()["results"] == {"win": 19, "place": 1, "show": 22})
    write_results(board, 19, 1, 22)
    port.written.clear()
    board.refresh()
    _check("the same results again send nothing", port.written == [])
    write_results(board, 7, 7, 3)
    board.refresh()
    _check("a file naming a horse twice reads as no results", b.results == [0, 0, 0] and board.model()["results"] == NO_RESULTS)
    Path(board._results_path).write_text("not json", encoding="utf-8")
    board.refresh()
    _check("a broken file reads as no results, no exception", b.results == [0, 0, 0])
    write_results(board, "12", 25, None)
    board.refresh()
    _check("strings are coerced, out-of-range and missing entries read as 0", b.results == [12, 0, 0] and board.model()["results"]["win"] == 12)
    _check("one place named: the dict, the other two null (the results screen waits for all three)",
           board.model()["results"] == {"win": 12, "place": None, "show": None})
    write_results(board, 19, 1, 22)
    board.refresh()
    done = board.reset_betting()
    _check("Reset betting clears the results, the file included", done["race_state"] == 0 and b.results == [0, 0, 0]
           and not Path(board._results_path).exists() and board.model()["results"] == NO_RESULTS and b.phase == 0, str(done))
    _check("clear_results on a missing file is False", clear_results(board._results_path) is False)


# -----------------------------------------------------------------------------
# The figures at the post (the model's closing)
# -----------------------------------------------------------------------------

POST_CUPS = [cup_entry(1, horse=19, count=4, online=True), cup_entry(2, horse=1, count=11, online=True),
             cup_entry(3, horse=22, count=7, online=True), cup_entry(4, horse=7, count=23, online=True)]


def post_cups(**counts):
    """POST_CUPS with other counts: post_cups(h7=24, h19=0)."""
    by_horse = {int(k[1:]): v for k, v in counts.items()}
    return [dict(c, count=by_horse.get(c["horse"], c["count"])) for c in POST_CUPS]


def tokens_of(**counts):
    """What closing's horses must be: every horse "1".."24" with its tokens."""
    by_horse = {int(k[1:]): v for k, v in counts.items()}
    return {str(n): {"tokens": by_horse.get(n, 0)} for n in range(1, 25)}


def test_closing_figures():
    """closing: the pot, the prizes, the token count and every horse's
    tokens as they were when betting closed. Taken the first time the race
    state is 3, 4 or 5 with none held; no count moves them afterwards; Reset
    betting and a return to 0 or 1 drop them; 2 and 6 leave them."""
    b, wall, log_dir = fresh_board()
    b.apply_snapshot(snap(phase=1, cups=POST_CUPS))
    _check("betting open: no closing figures", b.model()["closing"] is None)
    wall.advance(5)
    b.apply_snapshot(snap(phase=2, cups=post_cups(h7=24)))
    _check("final call: none yet", b.model()["closing"] is None)
    wall.advance(5)
    b.apply_snapshot(snap(phase=3, cups=post_cups(h7=24)))
    m = b.model()
    c = m["closing"]
    _check("at the post (2 -> 3): the pot, the prizes, the token count, every horse's tokens and when",
           c == {"pot": 46.0, "prizes": {"win": 27, "place": 12, "show": 7}, "total_tokens": 46,
                 "horses": tokens_of(h19=4, h1=11, h22=7, h7=24), "at": round(wall.t, 3)}, str(c))
    _check("...the live fields' own values and shapes at that moment",
           (c["pot"], c["prizes"], c["total_tokens"]) == (m["pot"], m["prizes"], m["total_tokens"])
           and all(c["horses"][k]["tokens"] == h["tokens"] for k, h in m["horses"].items()))
    _check("...in the JSON the TV gets", json.loads(b.model_json())["closing"] == c)
    _, lines = log_lines(log_dir)
    _check("the log records them beside the state change",
           {"closing": {"pot": 46.0, "prizes": {"win": 27, "place": 12, "show": 7}, "total_tokens": 46}} in lines[-1]["changes"]
           and {"race_state": [2, 3]} in lines[-1]["changes"], str(lines[-1]))
    # A late token, the race, the results, the cups emptied for the draw.
    wall.advance(5)
    b.apply_snapshot(snap(phase=3, cups=post_cups(h7=25)))
    wall.advance(5)
    b.apply_snapshot(snap(phase=4, cups=post_cups(h7=25)))
    wall.advance(5)
    b.apply_snapshot(snap(phase=5, cups=post_cups(h7=25, h19=0, h1=0, h22=0), results=(19, 1, 22)))
    m = b.model()
    _check("later token changes move the live fields, never the closing figures",
           m["closing"] == c and m["pot"] == 25.0 and m["prizes"] == {"win": 15, "place": 6, "show": 4}
           and m["horses"]["19"]["tokens"] == 0 and m["results"] == {"win": 19, "place": 1, "show": 22}, str(m["pot"]))
    b.apply_snapshot(snap(phase=2, cups=post_cups(h7=25, h19=0, h1=0, h22=0)))
    _check("FINAL CALL pressed after the draw: kept", b.model()["closing"] == c)
    b.apply_snapshot(snap(phase=5, cups=post_cups(h7=25, h19=0, h1=0, h22=0)))
    _check("...and WINNER again shows the same figures, not ones taken again from the emptied cups", b.model()["closing"] == c)
    b.apply_snapshot(snap(phase=6, cups=post_cups(h7=25, h19=0, h1=0, h22=0)))
    _check("AFTER_PARTY keeps them", b.model()["closing"] == c)
    b.apply_snapshot({})
    _check("a snapshot with no phase at all (no bridge) leaves them", b.model()["closing"] == c)
    b.apply_snapshot(snap(phase=1, cups=post_cups(h7=25, h19=0, h1=0, h22=0)))
    _check("a return to BETTING OPEN drops them", b.model()["closing"] is None)
    _, lines = log_lines(log_dir)
    _check("...logged as dropped", {"closing": None} in lines[-1]["changes"], str(lines[-1]))
    b.apply_snapshot(snap(phase=3, cups=POST_CUPS))
    _check("closed again: taken again, from the cups as they are now", b.model()["closing"]["pot"] == 45.0)
    done = b.reset_betting()
    _check("Reset betting drops them (here with no bridge)", b.model()["closing"] is None and done["race_state"] == 0)

    b2, wall2, _ = fresh_board()
    b2.apply_snapshot(snap(phase=0, cups=POST_CUPS))
    b2.apply_snapshot(snap(phase=3, cups=POST_CUPS))
    c2 = b2.model()["closing"]
    _check("0 -> 3: taken at the post", c2 is not None and c2["pot"] == 45.0 and c2["horses"]["7"] == {"tokens": 23}, str(c2))
    b3, wall3, _ = fresh_board()
    b3.apply_snapshot(snap(phase=1, cups=POST_CUPS))
    b3.apply_snapshot(snap(phase=4, cups=post_cups(h7=30)))
    c3 = b3.model()["closing"]
    _check("1 -> 4 (AT THE POST skipped): taken on the way into RUNNING, from that snapshot",
           c3 is not None and c3["pot"] == 52.0 and c3["horses"]["7"] == {"tokens": 30}, str(c3))
    b3.apply_snapshot(snap(phase=3, cups=post_cups(h7=31)))
    _check("...and not taken again by a later AT THE POST", b3.model()["closing"] == c3)
    b4, wall4, _ = fresh_board()
    b4.apply_snapshot(snap(phase=1, cups=POST_CUPS))
    b4.apply_snapshot(snap(phase=5, cups=post_cups(h7=26)))
    _check("1 -> 5 (HEARTBEAT or SET WINNERS straight from betting): taken on the way into WINNER",
           b4.model()["closing"] is not None and b4.model()["closing"]["pot"] == 48.0)
    b5, wall5, _ = fresh_board()
    b5.apply_snapshot(snap(phase=1, cups=POST_CUPS))
    b5.apply_snapshot(snap(phase=6, cups=POST_CUPS))
    _check("AFTER_PARTY straight from betting takes none", b5.model()["closing"] is None)

    # On a real bridge: taken in the very model that first says AT_THE_POST, and saved.
    br, port, sio, clk = _fresh_bridge()
    br._open_port()
    board, wall6, _ = fresh_board(bridge=br)
    for mac, horse, count in ((MAC_A, 19, 4), (MAC_B, 1, 11), (MAC_C, 7, 23)):
        br.handle_raw_line(telem(mac, horse=horse, count=count))
    board.set_mode("BETTING_60")
    q = board.subscribe()
    board.set_mode("AT_THE_GATE")
    first = json.loads(q.get_nowait())
    board.unsubscribe(q)
    c = board.model()["closing"]
    _check("on a real bridge: the first model that says AT_THE_POST already carries them",
           first["race_state"] == 3 and first["closing"] == c and c["pot"] == 38.0
           and c["prizes"] == {"win": 22, "place": 10, "show": 6}, str(first.get("closing")))
    _check("...saved in lq_closing", HorseStore(br.db).closing == c)
    br.handle_raw_line(telem(MAC_C, horse=7, count=0))
    board.set_mode("FINISH")
    _check("a cup emptied during the race changes nothing there", board.model()["closing"] == c and board.model()["pot"] == 15.0)
    board.set_mode("BETTING_30")
    _check("60/30 MIN again (state 1) drops them, on disk too", board.model()["closing"] is None and HorseStore(br.db).closing is None)
    board.set_mode("CHAOS")
    _check("RUNNING straight from there takes them again", board.model()["closing"] is not None
           and HorseStore(br.db).closing == board.model()["closing"])
    board.reset_betting()
    _check("Reset betting drops them, on disk too", board.model()["closing"] is None and HorseStore(br.db).closing is None)
    # Saving is best effort: a database error costs the copy on disk, not the figures.
    board.set_mode("BETTING_60")

    def refuse(_closing):
        raise sqlite3.OperationalError("database is locked")
    board.store.set_closing = refuse
    with capture_logs(LOGGER) as cap:
        board.set_mode("AT_THE_GATE")
        board.set_mode("BETTING_60")
        board.set_mode("AT_THE_GATE")
    _check("a database error: the model still carries them, one WARNING",
           board.model()["closing"] is not None and board.model()["race_state"] == 3
           and len(cap.messages("cannot save the closing figures")) == 1, str(cap.messages()))


def test_lq_closing_on_an_existing_database():
    """DevPi's database is older than lq_closing: the bridge's start adds the
    table (no schema error, nothing else touched), and until it has, a store
    still reads the names and only the closing figures are missing."""
    path = str(tmpdir() / "before_lq_closing.db")
    db = LqDb(path)
    db.init_schema()
    db.conn.execute("DROP TABLE lq_closing")          # the shape before this change
    db.save_horse(9, "Encino")
    with capture_logs("la_quiniela.horses", logging.ERROR) as cap:
        store = HorseStore(db)
    _check("without lq_closing: the names are read, the closing figures are None, one ERROR naming the table",
           store.horses()[9]["name"] == "Encino" and store.closing is None
           and len(cap.messages("lq_closing")) == 1 and store._db is db, str(cap.messages()))
    db.close()
    b = LqBridge(settings={"LQ_SERIAL_PORT": "/dev/fake"}, db_path=path, serial_factory=lambda p, baud, t: S.FakeSerial(),
                 socketio=S.StubSocketIO(), clock=FakeClock(), console=S.ConsoleCapture())
    try:
        _check("the bridge's start adds lq_closing and passes the shape check",
               b.schema_error is None and b.db._columns("lq_closing") == ["id", "closing"])
        _check("...the names are where they were, no closing figures yet",
               HorseStore(b.db).horses()[9]["name"] == "Encino" and HorseStore(b.db).closing is None)
    finally:
        b.close()


def test_results_and_closing_survive_a_restart():
    """pi5 restarted in WINNER after the cups were emptied for the draw: the
    results (the dashboard's file) and the closing figures come back, so the
    TV comes back to the results screen with the numbers at the post."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    board, wall, log_dir = fresh_board(bridge=b)
    board.store.set_names({19: "Golden Tempo", 1: "Renegade", 22: "Ocelli", 7: "Danon Bourbon"})
    for mac, horse, count in ((mac_of(19), 19, 4), (mac_of(1), 1, 11), (mac_of(22), 22, 7), (mac_of(7), 7, 23)):
        b.handle_raw_line(telem(mac, horse=horse, count=count))
    board.set_mode("BETTING_60")
    board.set_mode("AT_THE_GATE")
    board.set_mode("FINISH")
    write_results(board, 19, 1, 22)          # what the dashboard's POST /api/results saves
    board.set_mode("RESULTS")
    before = board.model()
    _check("before: WINNER, the results, the closing figures (pot $45)",
           before["race_state"] == 5 and before["results"] == {"win": 19, "place": 1, "show": 22}
           and before["closing"]["pot"] == 45.0 and before["closing"]["horses"]["19"] == {"tokens": 4}, str(before["closing"]))
    for horse in (19, 1, 22):
        b.handle_raw_line(telem(mac_of(horse), horse=horse, count=0))     # emptied for the draw
    board.refresh()
    _check("the winners' cups emptied: the live pot falls to $23, the closing figures stay",
           board.model()["pot"] == 23.0 and board.model()["closing"] == before["closing"])
    # The restart: the process goes; a new bridge and a new board come up over
    # the same database and the same results file (main.py no longer deletes it).
    settings = dict(b.settings)
    b.close()
    port2 = S.FakeSerial()
    b2 = LqBridge(settings=settings, db_path=S._TMP_DB, serial_factory=lambda p, baud, t: port2,
                  socketio=S.StubSocketIO(), clock=FakeClock(), console=S.ConsoleCapture())
    S._current = b2                             # the next _fresh_bridge() closes it
    board2 = BettingBoard(bridge=b2, clock=FakeClock(1000.0), wall=FakeClock(1_700_000_600.0),
                          log_dir=log_dir, results_path=board._results_path)
    _check("the new board serves them before its first snapshot", board2.model()["closing"] == before["closing"])
    board2.refresh()
    m = board2.model()
    _check("after the restart: WINNER, the results and the closing figures exactly as they were",
           m["race_state"] == 5 and m["results"] == {"win": 19, "place": 1, "show": 22}
           and m["closing"] == before["closing"], str((m["race_state"], m["results"])))
    _check("...the live figures are the cups as last heard: emptied", m["pot"] == 23.0 and m["horses"]["19"]["tokens"] == 0)
    _check("...the names too", m["horses"]["19"]["name"] == "GOLDEN TEMPO")
    _check("the results file is still there, and the gateway's state still carries them",
           read_results(board._results_path) == [19, 1, 22] and b2.results == [19, 1, 22] and b2.phase == 5)
    b2._open_port()
    b2.handle_raw_line(hello())
    _check("a gateway saying hello after the restart gets WINNER with the results",
           port2.lines()[-1] == state_line(b2.state_rev, 5, [], [], [19, 1, 22]), str(port2.lines()))
    board2.reset_betting()
    _check("Reset betting clears both, the file and lq_closing included",
           board2.model()["closing"] is None and board2.model()["results"] is None
           and not Path(board._results_path).exists() and HorseStore(b2.db).closing is None)


# -----------------------------------------------------------------------------
# The counted pot: the host's hand count of the cash box
# -----------------------------------------------------------------------------

# 150 tokens on the scales: the scale prizes are 89 / 38 / 23 (150 x .25 = 37.5
# and 150 x .15 = 22.5 round up). A hand count of $154 gives 92 / 39 / 23.
COUNT_CUPS = [cup_entry(1, horse=19, count=4, online=True), cup_entry(2, horse=1, count=11, online=True),
              cup_entry(3, horse=22, count=7, online=True), cup_entry(4, horse=7, count=128, online=True)]
SCALE_PRIZES = {"win": 89, "place": 38, "show": 23}
COUNTED_PRIZES = {"win": 92, "place": 39, "show": 23}
CLOSING_KEYS = {"pot", "prizes", "total_tokens", "horses", "at"}


def _tokens_by_horse(model):
    return {k: h["tokens"] for k, h in model["horses"].items()}


def test_counted_pot_model():
    """The hand count: the pot and all three prizes come from it, through
    prizes_for, with the same split and rounding; bets per horse stay as the
    scales read them; it is held with the figures at the post, through 4, 5,
    6 and 2, and dropped by 0, 1 and Reset betting; it can be entered in 3-5
    only, and only as a whole number of dollars."""
    b, wall, log_dir = fresh_board()
    b.apply_snapshot(snap(phase=1, cups=COUNT_CUPS))
    m = b.model()
    _check("before the post: no figures, so no scale figure, no count",
           (m["pot_scale"], m["pot_counted"], m["hand_counted"]) == (None, None, False) and m["closing"] is None
           and m["pot"] == 150.0 and m["prizes"] == SCALE_PRIZES, str((m["pot"], m["prizes"])))
    b.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    m0 = b.model()
    c0 = m0["closing"]
    _check("at the post: the scale figures, frozen, nothing counted yet",
           (m0["pot"], m0["prizes"]) == (150.0, SCALE_PRIZES) and m0["pot_scale"] == 150.0
           and m0["pot_counted"] is None and m0["hand_counted"] is False
           and (c0["pot"], c0["prizes"]) == (150.0, SCALE_PRIZES), str(c0))
    _check("closing keeps the five keys it always had", set(c0) == CLOSING_KEYS, str(sorted(c0)))
    bets = _tokens_by_horse(m0)

    done = b.set_pot_counted(154)
    _check("set 154: the reply says the count, the scale pot, the new pot and prizes, saved",
           done == {"pot_counted": 154, "pot_scale": 150.0, "pot": 154.0, "prizes": COUNTED_PRIZES,
                    "hand_counted": True, "race_state": 3, "saved": True}, str(done))
    m = b.model()
    c = m["closing"]
    _check("$154 -> WIN 92 / PLACE 39 / SHOW 23 in the model's pot and prizes ...",
           m["pot"] == 154.0 and m["prizes"] == COUNTED_PRIZES and sum(m["prizes"].values()) == 154, str((m["pot"], m["prizes"])))
    _check("... and in closing's, which is what the TV board paints",
           c["pot"] == 154.0 and c["prizes"] == COUNTED_PRIZES and set(c) == CLOSING_KEYS, str(c))
    _check("pot_scale is still the scale pot at the post; pot_counted and hand_counted say there is a count",
           (m["pot_scale"], m["pot_counted"], m["hand_counted"]) == (150.0, 154, True))
    _check("bets per horse, the token total and closing's tokens are the scales', unchanged",
           _tokens_by_horse(m) == bets and m["total_tokens"] == 150 and c["total_tokens"] == 150
           and c["horses"] == c0["horses"] and c["at"] == c0["at"] and m["leader"] == m0["leader"])
    _check("the JSON the TV gets carries it",
           json.loads(b.model_json())["closing"]["pot"] == 154.0 and json.loads(b.model_json())["pot_counted"] == 154)
    _check("the store's record holds it beside the figures (and the model's closing does not repeat it)",
           b.store.closing["pot_counted"] == 154 and b.store.closing["pot"] == 150.0 and "pot_counted" not in c)

    # The same function: the scale pot and the counted pot both go through prizes_for, no other code does the sum.
    calls = []
    real = betting.prizes_for

    def spy(pot, split):
        calls.append(pot)
        return real(pot, split)
    betting.prizes_for = spy
    try:
        b.set_pot_counted(153)
        m = b.model()
    finally:
        betting.prizes_for = real
    _check("the live scale pot (150) and the count (153) were both turned into prizes by prizes_for",
           150.0 in calls and 153 in calls, str(calls))
    _check("... 153 -> 92 / 38 / 23", m["prizes"] == real(153, SPLIT) == {"win": 92, "place": 38, "show": 23}, str(m["prizes"]))
    here = Path(__file__).resolve().parent
    offenders = [p.name for p in sorted(here.glob("*.py"))
                 if not p.name.startswith("test_") and p.name != "betting.py"
                 and any(s in p.read_text(encoding="utf-8") for s in ("prizes_for", "round_half_up", "LQ_SPLIT"))]
    _check("no other module of La Quiniela computes prizes: only betting.py names the split and the rounding",
           offenders == [], str(offenders))

    for pot in (0, 1, 2, 3, 7, 10, 30, 99, 154, 155, 1000, 9999, 10000):
        b.set_pot_counted(pot)
        m = b.model()
        _check(f"a count of {pot}: prizes_for's figures, summing to the pot, the same in closing",
               m["prizes"] == real(pot, SPLIT) and sum(m["prizes"].values()) == pot and m["pot"] == float(pot)
               and m["closing"]["prizes"] == m["prizes"] and m["closing"]["pot"] == float(pot) and m["hand_counted"] is True,
               str((m["pot"], m["prizes"])))
    b.set_pot_counted(150)
    m = b.model()
    _check("a count equal to the scale pot is still a hand count",
           m["hand_counted"] is True and m["pot_counted"] == 150 and m["pot"] == 150.0 and m["prizes"] == SCALE_PRIZES)

    # Clear: the scale figures come back, exactly as a board that was never counted has them.
    b.set_pot_counted(152)
    b.set_pot_counted(154)
    _check("entering again overwrites", b.model()["pot_counted"] == 154 and b.model()["pot"] == 154.0)
    done = b.set_pot_counted(None)
    m = b.model()
    ref, _, _ = fresh_board()
    ref.apply_snapshot(snap(phase=1, cups=COUNT_CUPS))
    ref.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    drop = ("updated", "now")
    _check("Clear: the scale pot and prizes are back, nothing counted",
           (m["pot"], m["prizes"], m["pot_counted"], m["hand_counted"]) == (150.0, SCALE_PRIZES, None, False)
           and (m["closing"]["pot"], m["closing"]["prizes"]) == (150.0, SCALE_PRIZES) and done["pot_counted"] is None
           and done["hand_counted"] is False and done["pot"] == 150.0 and done["prizes"] == SCALE_PRIZES, str(done))
    _check("...and the model is the one a never-counted board has, key for key",
           {k: v for k, v in m.items() if k not in drop} == {k: v for k, v in ref.model().items() if k not in drop})
    _check("...the record holds no count any more", "pot_counted" not in b.store.closing)
    _check("clearing when there is none changes nothing", b.set_pot_counted(None)["pot_counted"] is None)

    # Held: 3 -> 4 -> 5 -> 6, 2, and the cups emptied for the draw.
    b.set_pot_counted(152)
    held = b.model()["closing"]
    EMPTY = [dict(c_, count=0) for c_ in COUNT_CUPS]
    for phase, cups in ((4, COUNT_CUPS), (5, EMPTY), (6, EMPTY)):
        b.apply_snapshot(snap(phase=phase, cups=cups))
        m = b.model()
        _check(f"state {phase}: the count and the figures at the post are held, and the pot is the count's",
               m["pot_counted"] == 152 and m["hand_counted"] is True and m["closing"] == held and m["pot"] == 152.0
               and m["prizes"] == real_prizes(152) and m["pot_scale"] == 150.0, str((m["pot_counted"], m["pot"])))
    b.apply_snapshot(snap(phase=2, cups=EMPTY))
    m = b.model()
    _check("state 2 (FINAL CALL after the draw): the count is held with the figures, but betting is open again, "
           "so the model's pot and prizes are the live ones and hand_counted says they are not the count",
           m["pot_counted"] == 152 and m["closing"] == held and m["pot_scale"] == 150.0 and m["hand_counted"] is False
           and m["pot"] == 0.0 and m["prizes"] == {"win": 0, "place": 0, "show": 0}, str((m["pot_counted"], m["pot"], m["hand_counted"])))
    b.apply_snapshot(snap(phase=5, cups=EMPTY))
    m = b.model()
    _check("...and WINNER again: the count's, unchanged",
           m["pot_counted"] == 152 and m["hand_counted"] is True and m["pot"] == 152.0 and m["closing"] == held)
    _check("...the cups emptied for the draw show as they are (the count does not touch bets)",
           all(h["tokens"] == 0 for h in b.model()["horses"].values()) and b.model()["total_tokens"] == 0)
    b.apply_snapshot({})
    _check("a snapshot with no phase (no bridge) leaves it", b.model()["pot_counted"] == 152)

    # Dropped by 1, by 0 and by Reset betting; a later post starts clean.
    b.apply_snapshot(snap(phase=1, cups=COUNT_CUPS))
    m = b.model()
    _check("state 1 (BETTING OPEN again) drops it with the figures: the live scale figures, nothing counted",
           (m["pot_counted"], m["pot_scale"], m["hand_counted"], m["closing"]) == (None, None, False, None)
           and m["pot"] == 150.0 and m["prizes"] == SCALE_PRIZES)
    b.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    _check("the next post is taken fresh: no count comes back", b.model()["pot_counted"] is None
           and b.model()["closing"]["pot"] == 150.0)
    b.set_pot_counted(140)
    b.apply_snapshot(snap(phase=0, cups=COUNT_CUPS))
    _check("state 0 (PRE_RACE) drops it too", b.model()["pot_counted"] is None and b.model()["closing"] is None)
    b.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    b.set_pot_counted(140)
    done = b.reset_betting()
    _check("Reset betting drops it with the figures", b.model()["pot_counted"] is None and b.model()["closing"] is None
           and b.model()["hand_counted"] is False and done["race_state"] == 0)

    # Refused outside 3-5 and with no figures at the post; nothing changes.
    refusal = {}
    for phase in (0, 1, 2):
        b.apply_snapshot(snap(phase=phase, cups=COUNT_CUPS))
        try:
            b.set_pot_counted(152)
            refusal[phase] = None
        except betting.CountRefused as exc:
            refusal[phase] = str(exc)
        _check(f"state {phase}: refused with a plain message, nothing held",
               refusal[phase] is not None and "after betting closes" in refusal[phase] and b.model()["pot_counted"] is None,
               str(refusal[phase]))
    b.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    b.set_pot_counted(152)
    b.apply_snapshot(snap(phase=6, cups=EMPTY))
    for attempt in (150, None):
        try:
            b.set_pot_counted(attempt)
            refusal[6] = None
        except betting.CountRefused as exc:
            refusal[6] = str(exc)
        _check(f"state 6, {'a new count' if attempt is not None else 'Clear'}: refused, the saved count is read-only",
               refusal[6] is not None and "read-only" in refusal[6] and b.model()["pot_counted"] == 152, str(refusal[6]))
    b.apply_snapshot(snap(phase=2, cups=EMPTY))
    try:
        b.set_pot_counted(None)
        refusal[2] = None
    except betting.CountRefused as exc:
        refusal[2] = str(exc)
    _check("state 2 with a held count: refused as well, the count stays", refusal[2] is not None and b.model()["pot_counted"] == 152)
    b.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    b._closing = None                       # a post with no figures (what a database without them would leave)
    try:
        b.set_pot_counted(152)
        none_yet = None
    except betting.CountRefused as exc:
        none_yet = str(exc)
    _check("state 3 but no figures at the post: refused, and it says why",
           none_yet is not None and "no figures at the post" in none_yet, str(none_yet))

    # Whole dollars only, 0 to the ceiling.
    b.apply_snapshot(snap(phase=1, cups=COUNT_CUPS))
    b.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    b.set_pot_counted(152)
    for bad in (True, False, 152.0, 1.5, float("nan"), "152", "", -1, 10001, 10 ** 9, [152], {"amount": 1}):
        try:
            b.set_pot_counted(bad)
            rejected = False
        except ValueError as exc:
            rejected = "whole number of dollars" in str(exc) or "outside 0 to 10000" in str(exc)
        _check(f"{bad!r:.20}: rejected with a plain message, the count it had stays",
               rejected and b.model()["pot_counted"] == 152)
    _check("the ceiling is 10000", betting.COUNTED_POT_MAX == 10000)
    b.set_pot_counted(0)
    m = b.model()
    _check("0 is a count (the cash box was empty): pot 0, prizes 0 / 0 / 0, hand counted",
           (m["pot"], m["prizes"], m["pot_counted"], m["hand_counted"]) == (0.0, {"win": 0, "place": 0, "show": 0}, 0, True))

    # Pushed at once, and logged.
    b2, wall2, log2 = fresh_board()
    b2.apply_snapshot(snap(phase=1, cups=COUNT_CUPS))
    b2.apply_snapshot(snap(phase=3, cups=COUNT_CUPS))
    q = b2.subscribe()
    b2.set_pot_counted(154)
    pushed = [json.loads(q.get_nowait())]
    _check("setting it publishes the model to the stream straight away, with the count in it",
           q.qsize() == 0 and pushed[0]["pot_counted"] == 154 and pushed[0]["closing"]["pot"] == 154.0
           and pushed[0]["prizes"] == COUNTED_PRIZES)
    b2.set_pot_counted(154)
    _check("the same count again publishes nothing", q.qsize() == 0)
    b2.set_pot_counted(None)
    pushed.append(json.loads(q.get_nowait()))
    _check("clearing it publishes too, with the scale figures back",
           pushed[1]["pot_counted"] is None and pushed[1]["pot"] == 150.0 and pushed[1]["prizes"] == SCALE_PRIZES)
    _, lines = log_lines(log2)
    _check("the log records each entry, state and tokens beside it",
           {"ts": 1_700_000_000.0, "race_state": 3, "changes": [{"pot_counted": [None, 154]}], "total_tokens": 150} in lines
           and {"ts": 1_700_000_000.0, "race_state": 3, "changes": [{"pot_counted": [154, None]}], "total_tokens": 150} in lines,
           str(lines[-2:]))

    # A stored value that is not a count is ignored, never raised.
    for junk in ("x", -3, True, 1.5, None):
        store = HorseStore(None)
        store.set_closing(dict(c0, pot_counted=junk))
        b3 = BettingBoard(store=store, clock=FakeClock(1000.0), wall=FakeClock(1_700_000_000.0),
                          log_dir=tmpdir() / "logs", results_path=tmpdir() / "results.json")
        m = b3.model()
        _check(f"a stored pot_counted of {junk!r} is not a count: the figures at the post, as taken",
               (m["pot_counted"], m["hand_counted"]) == (None, False) and m["closing"] == c0
               and m["pot_scale"] == 150.0, str((m["pot_counted"], m["closing"])))


def test_counted_pot_route_and_restart():
    """PUT /api/quiniela/counted_pot on a real bridge: 409 before the post,
    400 for a bad amount, the count in the model and on disk, held through the
    race, kept by a restart of pi5, cleared by Clear, by Reset betting and by
    state 1; the TV is told at once."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    board = get_board()
    for mac, horse, count in ((MAC_A, 19, 4), (MAC_B, 1, 11), (MAC_C, 7, 135)):
        b.handle_raw_line(telem(mac, horse=horse, count=count))
    board.set_mode("BETTING_60")
    url = "/api/quiniela/counted_pot"
    r = client.put(url, json={"amount": 152})
    _check("state 1: 409 with a plain message, nothing held",
           r.status_code == 409 and r.get_json()["ok"] is False and "after betting closes" in r.get_json()["error"]
           and board.model()["pot_counted"] is None, str(r.get_json()))
    board.set_mode("AT_THE_GATE")
    m = client.get("/api/quiniela").get_json()
    _check("at the post: the scale pot of 150, frozen", m["pot"] == 150.0 and m["pot_scale"] == 150.0 and m["closing"]["pot"] == 150.0
           and m["pot_counted"] is None and m["hand_counted"] is False)
    for bad in ({}, {"amount": "x"}, {"amount": "152"}, {"amount": 1.5}, {"amount": True}, {"amount": -1}, {"amount": 10001},
                {"amount": [1]}, {"sum": 5}):
        r = client.put(url, json=bad)
        _check(f"PUT {bad!r:.30} -> 400", r.status_code == 400 and r.get_json()["ok"] is False and r.get_json()["error"]
               and board.model()["pot_counted"] is None, f"{r.status_code} {r.get_json()}")
    r = client.put(url, json={})
    _check("a body with no amount gets the usage line", r.get_json() == {"ok": False, "error": board_mod.USAGE_COUNTED_POT})
    r = client.put(url, data="5", content_type="application/json")
    _check("a non-object body -> 400", r.status_code == 400)
    q = board.subscribe()
    r = client.put(url, json={"amount": 152})
    body = r.get_json()
    _check("PUT 152: ok, the count, the scale pot, the pot and prizes from the count, saved",
           r.status_code == 200 and body == {"ok": True, "pot_counted": 152, "pot_scale": 150.0, "pot": 152.0,
                                             "prizes": real_prizes(152), "hand_counted": True, "race_state": 3, "saved": True},
           str(body))
    pushed = json.loads(q.get_nowait())
    board.unsubscribe(q)
    _check("the TV's stream got the model at once, with the count", pushed["pot_counted"] == 152 and pushed["closing"]["pot"] == 152.0
           and pushed["prizes"] == real_prizes(152))
    m = client.get("/api/quiniela").get_json()
    _check("GET /api/quiniela: the counted figures everywhere, the bets as the cups read them",
           m["pot"] == 152.0 and m["prizes"] == real_prizes(152) and m["closing"]["pot"] == 152.0
           and m["closing"]["prizes"] == real_prizes(152) and m["pot_scale"] == 150.0 and m["pot_counted"] == 152
           and m["hand_counted"] is True and m["horses"]["7"]["tokens"] == 135 and m["closing"]["horses"]["7"] == {"tokens": 135}
           and m["total_tokens"] == 150)
    _check("saved with the figures at the post", HorseStore(b.db).closing["pot_counted"] == 152
           and HorseStore(b.db).closing["pot"] == 150.0)
    r = client.put(url, json={"amount": 154})
    _check("entering again overwrites", r.get_json()["pot"] == 154.0 and r.get_json()["prizes"] == COUNTED_PRIZES
           and HorseStore(b.db).closing["pot_counted"] == 154)

    # The race goes on: the count is held, whatever the cups do.
    board.set_mode("FINISH")
    board.set_mode("RESULTS")
    b.handle_raw_line(telem(MAC_C, horse=7, count=0))
    board.refresh()
    m = client.get("/api/quiniela").get_json()
    _check("through RUNNING and WINNER, with a cup emptied: still the count (the cups' live bets fell)",
           m["pot_counted"] == 154 and m["pot"] == 154.0 and m["horses"]["7"]["tokens"] == 0 and m["closing"]["horses"]["7"] == {"tokens": 135})
    board.set_mode("RESET")
    _check("AFTER_PARTY (the dashboard's RESET) keeps it", board.model()["race_state"] == 6 and board.model()["pot_counted"] == 154)
    r = client.put(url, json={"amount": 100})
    _check("state 6: 409, the saved count is read-only", r.status_code == 409 and "read-only" in r.get_json()["error"]
           and board.model()["pot_counted"] == 154, str(r.get_json()))
    r = client.put(url, json={"amount": None})
    _check("...Clear too", r.status_code == 409 and board.model()["pot_counted"] == 154)

    # A restart of pi5: a new bridge and board over the same database.
    settings = dict(b.settings)
    log_dir = board._log_dir
    b.close()
    port2 = S.FakeSerial()
    b2 = LqBridge(settings=settings, db_path=S._TMP_DB, serial_factory=lambda p, baud, t: port2,
                  socketio=S.StubSocketIO(), clock=FakeClock(), console=S.ConsoleCapture())
    S._current = b2                             # the next _fresh_bridge() closes it
    board2 = BettingBoard(bridge=b2, clock=FakeClock(1000.0), wall=FakeClock(1_700_000_600.0),
                          log_dir=log_dir, results_path=board._results_path)
    _check("a restarted pi5 serves the count (and the figures) before its first snapshot, which knows no race state yet",
           board2.model()["pot_counted"] == 154 and board2.model()["closing"]["pot"] == 154.0
           and board2.model()["hand_counted"] is False and board2.model()["race_state"] == 0)
    board2.refresh()
    m = board2.model()
    _check("... and after it: the same count, the same scale pot, the same prizes",
           m["race_state"] == 6 and m["pot_counted"] == 154 and m["pot_scale"] == 150.0 and m["prizes"] == COUNTED_PRIZES
           and m["closing"]["prizes"] == COUNTED_PRIZES and m["hand_counted"] is True)
    board2.set_mode("AT_THE_GATE")
    _check("the held figures are not taken again at a later post (3 after 6), so neither is the count",
           board2.model()["pot_counted"] == 154)
    board2.set_mode("BETTING_30")
    _check("state 1 clears it, on disk too", board2.model()["pot_counted"] is None and board2.model()["closing"] is None
           and HorseStore(b2.db).closing is None)
    board2.set_mode("AT_THE_GATE")
    _check("a fresh post has no count", board2.model()["pot_counted"] is None and board2.model()["hand_counted"] is False)
    board2.set_pot_counted(99)
    board2.reset_betting()
    _check("Reset betting clears it, on disk too", board2.model()["pot_counted"] is None and board2.model()["closing"] is None
           and HorseStore(b2.db).closing is None)

    # A database that refuses the write: the count is held, and the reply says it was not stored.
    board2.set_mode("BETTING_60")
    board2.set_mode("AT_THE_GATE")
    client2 = _make_board_app(b2).test_client()
    board3 = get_board()
    board3.refresh()                        # what start_board() does at startup: the model knows the race state

    def refuse(_closing):
        raise sqlite3.OperationalError("database is locked")
    board3.store.set_closing = refuse
    with capture_logs(LOGGER) as cap:
        r = client2.put(url, json={"amount": 120})
    _check("the database refuses: 200, the count is held, saved false, one WARNING",
           r.status_code == 200 and r.get_json()["saved"] is False and r.get_json()["pot_counted"] == 120
           and board3.model()["pot_counted"] == 120 and len(cap.messages("cannot save the closing figures")) == 1, str(r.get_json()))


def real_prizes(pot):
    return prizes_for(pot, SPLIT)


def test_admin_page():
    b, port, sio, clk = _fresh_bridge()
    client = _make_board_app(b).test_client()
    r = client.get("/quiniela/admin")
    _check("GET /quiniela/admin 200 text/html", r.status_code == 200 and r.mimetype == "text/html")
    html = r.get_data(as_text=True)
    for section in ("race", "names", "scratches", "closes"):
        _check(f"section id={section!r} present", f'id="{section}"' in html)
    _check("the Race section comes first", html.index('id="race"') < html.index('id="names"'))
    _check("viewport meta for phones", 'name="viewport"' in html)
    _check("talks to the routes", all(path in html for path in ("/api/quiniela/horses", "/api/quiniela/scratch",
                                                                "/api/quiniela/unscratch", "/api/quiniela/closes_at", "/api/quiniela",
                                                                "/api/quiniela/reset")))
    _check("no CDN, no external script", "<script src=" not in html and "https://" not in html and "http://" not in html)
    # Build-order item 5: the race state changes on the Control Center only (its buttons run the LEDs with it).
    _check("no state buttons and nothing that sets a state: no data-state, no state command, no cmd route",
           not any(s in html for s in ("data-state", 'cmd: "state', "/api/quiniela/cmd", 'id="states"', "state-status")))
    _check("the race state, read-only, from the model's race_state: number and name, all seven names",
           'id="state-now"' in html and "stateLine(model.race_state)" in html and 'n + " · " + STATES[n]' in html
           and all(s in html for s in ('"PRE-RACE"', '"BETTING OPEN"', '"FINAL CALL"', '"AT THE POST"', '"RUNNING"', '"WINNER"',
                                       '"AFTER PARTY"')))
    _check("...and a model that cannot be read says so on that line, never an old state as the live one",
           'renderState("cannot reach pi5", false)' in html and "renderState(why, false)" in html)
    _check("a plain link to the Control Center", '<a id="state-link" class="state-link" href="/"' in html
           and ">Change the race state on the Control Center</a>" in html)
    _check("the figures and Reset betting behind a confirm()",
           all(s in html for s in ("fig-pot", "fig-win", "fig-place", "fig-show", "fig-bets", "reset-betting"))
           and 'confirm("Reset betting?' in html and "horses_with_tokens" in html)
    _check("the Horses list: one row per horse from the model, the four statuses, nothing to click",
           all(s in html for s in ('id="horses"', 'id="horses-line"', "renderHorses", "\\u26a0", "\\u25cb no cup", "\\u25cf online",
                                   "\\u25cf offline", "cups_online", "cups_no_horse", "with no horse", "CLOTH"))
           and "data-cup-horse" not in html and "<select data-cup" not in html)
    _check("the cups table, the picker, Adopt, Forget cups, the snapshot and the CUP n note are gone",
           not any(s in html for s in ("cups-adopt", "cups-forget", "/api/lq/", 'id="cups"', "CUP n", "plus one", "horse command takes",
                                       'cmd: "horse "', "dev_endpoints")))
    _check("WIN / PLACE / SHOW tags from the results", all(s in html for s in ('class="tag win"', 'class="tag place"', 'class="tag show"'))
           and "res.win === n" in html)
    _check("24 name lines and the also-eligibles caption", 'rows="24"' in html and "also-eligibles" in html)
    _check("a replacement number picker, a name box, No replacement and Undo",
           all(s in html for s in ("<select", "data-repl-num", "data-repl-name", "data-norepl", "data-undo", "No replacement")))
    _check("sends the replacement as {number, name}", "replacement = { number:" in html)
    _check("Undo is withheld on a chained record, with the reason", '" · undo #"' in html and "disabled" in html)
    _check("reads in_field and scratches from the model", "in_field" in html and "scratches" in html)
    _check("loadModel exists, runs at load, every 5 s and after every action (it went missing once)",
           "function loadModel(" in html and "loadModel(true)" in html and "setInterval(function () { loadModel(false); }, 5000)" in html
           and html.count("loadModel(") >= 6, str(html.count("loadModel(")))
    _check("no gateway-flag wording", "at the gateway" not in html)
    _check("Race info at the top of the setup area: after the Race section, before the names",
           html.index('id="race"') < html.index('id="race-info"') < html.index('id="names"'))
    _check("...a name, a date, a post time, Save and its reply line, talking to /api/quiniela/race",
           all(s in html for s in ('id="race-name"', 'type="date"', 'id="race-date"', 'type="time"', 'id="race-time"',
                                   'id="race-save"', 'id="race-status"', '"/api/quiniela/race"', '"PUT", "/api/quiniela/race"'))
           and "KENTUCKY DERBY" in html)
    _check("the Counted pot box: after the figures, before Reset betting, hidden until the model says states 3-5 (or a saved count in 6)",
           html.index('id="fig-note"') < html.index('id="counted"') < html.index('id="reset-betting"')
           and 'id="counted" class="counted" hidden' in html and "st >= 3 && st <= 5" in html and "st === 6 && has" in html)
    _check("...a number box with the phone's numeric keypad, a big Save, Clear, and a reply line",
           'id="counted-input" type="text" inputmode="numeric" pattern="[0-9]*"' in html and 'id="counted-save" class="primary big"' in html
           and 'id="counted-clear"' in html and 'id="counted-status"' in html and "min-height: 68px" in html)
    _check("...it saves with PUT /api/quiniela/counted_pot, reads pot_scale, pot_counted and hand_counted from the model, and shows the difference",
           '"PUT", "/api/quiniela/counted_pot"' in html and "model.pot_scale" in html and "model.pot_counted" in html
           and "model.hand_counted" in html and 'id="cmp-scale"' in html and 'id="cmp-counted"' in html and 'id="cmp-diff"' in html
           and '"same"' in html and chr(0x2212) in html)
    _check("...the figures say which they are (hand counted, or the scales'), and the page does no prize arithmetic of its own",
           "hand counted" in html and ("Pot " + chr(0xb7) + " scale") in html and "model.split" not in html and "* 0.2" not in html and "* 0.6" not in html)
    _check("well under 700 lines", html.count("\n") < 700, str(html.count("\n")))


def test_reset_betting_route():
    """POST /api/quiniela/reset: PRE_RACE, the closing time, the ticker and
    the results cleared, the cups' current counts the baseline. Names and
    both kinds of scratch stay; the cups keep their numbers, which are
    theirs. Tokens still in a cup are not an error: the pot reads them."""
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    board = get_board()
    for n in range(1, 21):
        b.handle_raw_line(telem(mac_of(n), horse=n, count=10 if n == 9 else 5 if n == 3 else 0))
    b.handle_raw_line(telem(MAC_C, horse=0, count=0, hello=True))            # a cup nobody has set
    b.handle_raw_line(status(cups=[status_cup(mac_of(n), horse=n) for n in range(1, 21)]))
    client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})
    r = client.post("/api/quiniela/scratch", json={"horse": 3, "replacement": {"number": 21, "name": FIELD_24[20]}})
    _check("setup: replacement scratch 3 -> 21", r.status_code == 200 and r.get_json()["cup"] == mac_of(3), r.get_data(as_text=True))
    b.handle_raw_line(telem(mac_of(3), horse=21, count=5))                   # the cup followed the pair
    r = client.post("/api/quiniela/scratch", json={"horse": 15})
    _check("setup: no-replacement scratch of 15", r.status_code == 200 and r.get_json()["cup"] == mac_of(15), r.get_data(as_text=True))
    client.put("/api/quiniela/closes_at", json={"in_minutes": 30})
    write_results(board, 9, 21, 2)
    client.post("/api/quiniela/cmd", json={"cmd": "state 2"})
    board.refresh()
    m = board.model()
    _check("before: FINAL_CALL, pot 15 (10 on 9, 5 on 21), a closing time, a record of each kind, results, 21 cups online, one unset",
           m["race_state"] == 2 and m["pot"] == 15.0 and m["horses"]["9"]["tokens"] == 10 and m["horses"]["21"]["tokens"] == 5
           and m["closes_at"] is not None and [(s["was"]["number"], s["now"] and s["now"]["number"]) for s in m["scratches"]] == [(3, 21), (15, None)]
           and m["results"] == {"win": 9, "place": 21, "show": 2} and m["cups_online"] == 21 and m["cups_no_horse"] == 1, str(m["pot"]))
    names_rev = m["names_rev"]
    n_lines = len(port.lines())
    r = client.post("/api/quiniela/reset")
    body = r.get_json()
    _check("POST /api/quiniela/reset 200 ok", r.status_code == 200 and body["ok"] is True, r.get_data(as_text=True))
    _check("...PRE_RACE; the pot reads the tokens still in the cups and names those horses",
           body["race_state"] == 0 and body["pot"] == 15.0 and body["total_tokens"] == 15
           and body["horses_with_tokens"] == [9, 21], str(body))
    _check("...events 0, closes_at null, cups_online 21, the new rev, gateway_online a bool",
           body["events"] == 0 and body["closes_at"] is None and body["cups_online"] == 21 and body["rev"] == b.state_rev
           and isinstance(body["gateway_online"], bool) and body["names_rev"] == names_rev, str(body))
    lines = port.lines()
    _check("exactly one line to the gateway: PRE_RACE with the same bit and pair, the results cleared, byte-exact",
           len(lines) == n_lines + 1 and lines[-1] == state_line(b.state_rev, 0, [15], [(3, 21)]), str(lines[-1:]))
    m = board.model()
    _check("the model: PRE_RACE, pot 15, tokens still on 9 and 21 on their cups, no events, no closes_at, no results",
           m["race_state"] == 0 and m["pot"] == 15.0 and m["horses"]["9"]["tokens"] == 10 and m["horses"]["9"]["cup"] == mac_of(9)
           and m["horses"]["21"]["tokens"] == 5 and m["horses"]["21"]["cup"] == mac_of(3) and m["events"] == []
           and m["closes_at"] is None and m["results"] == NO_RESULTS, str(m["events"]))
    _check("...names, both scratch kinds and the also-eligible kept, names_rev untouched",
           m["horses"]["9"]["name"] == FIELD_24[8].upper() and m["horses"]["21"]["in_field"] is True
           and m["horses"]["21"]["replaced"] == FIELD_24[2].upper()
           and m["horses"]["15"]["scratched"] is True and m["horses"]["15"]["in_field"] is False
           and m["names_rev"] == names_rev and board.store.scratches() == {3: 21, 15: None})
    _check("...persisted", HorseStore(b.db).closes_at is None and HorseStore(b.db).scratches() == {3: 21, 15: None})
    _, log = log_lines(board._log_dir)
    _check("the log: a baseline record marked as the betting reset, with the state change",
           log[-1].get("baseline") is True and log[-1].get("reset") == "betting" and {"race_state": [2, 0]} in log[-1]["changes"], str(log[-1]))
    b.handle_raw_line(telem(mac_of(9), horse=9, count=11))
    board.refresh()
    m = board.model()
    _check("a drop after the reset is the only event", [(e["horse"], e["delta"]) for e in m["events"]] == [(9, 1)] and m["pot"] == 16.0, str(m["events"]))
    b.handle_raw_line(telem(mac_of(9), horse=9, count=0))
    b.handle_raw_line(telem(mac_of(3), horse=21, count=0))
    board.refresh()
    _check("emptying the cups shows as removals", sorted(e["delta"] for e in board.model()["events"]) == [-11, -5, 1], str(board.model()["events"]))
    r = client.post("/api/quiniela/reset")
    body = r.get_json()
    _check("reset with empty cups: pot 0, no tokens, no horses named, no events",
           body["pot"] == 0.0 and body["total_tokens"] == 0 and body["horses_with_tokens"] == [] and body["events"] == 0, str(body))
    n = len(log_lines(board._log_dir)[1])
    client.post("/api/quiniela/reset")
    _, log = log_lines(board._log_dir)
    _check("a reset that changes nothing still leaves its trace", len(log) == n + 1 and log[-1].get("reset") == "betting" and log[-1]["changes"] == [])
    bd, wall, _ = fresh_board()
    done = bd.reset_betting()
    _check("reset_betting() without a bridge: rev None, pot 0", done["rev"] is None and done["pot"] == 0.0 and done["cups_online"] == 0, str(done))


def test_removed_routes_and_conflict_on_a_real_bridge():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    for path in ("/api/lq/dev/state", "/api/lq/dev/roster", "/api/lq/dev/roster/adopt", "/api/lq/dev/roster/clear", "/api/lq/dev/reset"):
        r = client.post(path, json={})
        _check(f"POST {path} is gone (404)", r.status_code == 404)
    for cmd in ("horse 1 7", "scratch 1 1", "roster"):
        r = client.post("/api/quiniela/cmd", json={"cmd": cmd})
        _check(f"the v1 command {cmd!r} is refused", r.status_code == 400 and "not allowed" in r.get_json()["error"], str(r.get_json()))
    b.handle_raw_line(telem(MAC_A, horse=7, count=4))
    b.handle_raw_line(telem(MAC_B, horse=7, count=1))
    b.handle_raw_line(telem(MAC_C, horse=0, count=0, hello=True))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    h7 = m["horses"]["7"]
    _check("two cups claiming 7 on a real bridge: conflict, both MACs, the first listed shown with its count",
           h7["conflict"] is True and sorted(h7["cups"]) == sorted([MAC_A, MAC_B]) and h7["cup"] == h7["cups"][0]
           and h7["tokens"] == {MAC_A: 4, MAC_B: 1}[h7["cup"]] and h7["online"] is True, str(h7))
    _check("a cup at horse 0 is counted, not in any row", m["cups_online"] == 3 and m["cups_no_horse"] == 1
           and all(MAC_C not in h["cups"] for h in m["horses"].values()))
    clk.advance(7)
    b.tick(clk())
    b.handle_raw_line(telem(MAC_A, horse=7, count=4))
    get_board().refresh()
    m = client.get("/api/quiniela").get_json()
    _check("once the other cup has gone quiet the conflict is over (a spare swapped in)",
           m["horses"]["7"]["conflict"] is False and m["horses"]["7"]["cups"] == [MAC_A] and m["horses"]["7"]["tokens"] == 4, str(m["horses"]["7"]))


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


# The table of the brief, adjusted to the modes the dashboard has: its
# thirteen buttons by the names they use, HEARTBEAT added at 5 (the race is
# over; the dashboard's own map calls it OFFICIAL) and RESET standing in for
# the after-party mode it does not have.
EXPECTED_MODE_STATES = {
    "WELCOME": 0, "TEST": 0, "STANDBY": 0,
    "BETTING_60": 1, "BETTING_30": 1,
    "FINAL_CALL": 2,
    "AT_THE_GATE": 3,
    "GATES_BURST": 4, "CHAOS": 4, "FINISH": 4,
    "RESULTS": 5, "HEARTBEAT_COOLDOWN": 5,
    "RESET": 6,
}


def test_modes_set_the_race_state():
    """One race state: every dashboard mode means a race state, and the cmd
    route, the admin page's buttons and the modes all set the same value."""
    _check("the table: thirteen modes, the expected states", MODE_STATES == EXPECTED_MODE_STATES, str(MODE_STATES))
    _check("every mode has a label and every state 0..6 has a mode",
           set(MODE_LABELS) == set(MODE_STATES) and set(MODE_STATES.values()) == set(range(7)))
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    log_dir = tmpdir() / "logs"
    client = _make_board_app(b, log_dir=log_dir).test_client()
    r = client.get("/api/quiniela/mode")
    body = r.get_json()
    _check("GET /api/quiniela/mode before anything was set: state 0, no mode, no source, the table, no-store",
           r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
           and body == {"ok": True, "state": 0, "state_name": "PRE_RACE", "mode": None, "label": None,
                        "source": None, "modes": EXPECTED_MODE_STATES}, str(body))
    names = {0: "PRE_RACE", 1: "BETTING_OPEN", 2: "FINAL_CALL", 3: "AT_THE_POST", 4: "RUNNING", 5: "WINNER",
             6: "AFTER_PARTY"}
    # Walk the modes in an order in which every one changes the state, so every one sends a line.
    order = ["BETTING_60", "WELCOME", "BETTING_30", "TEST", "FINAL_CALL", "STANDBY", "AT_THE_GATE", "GATES_BURST",
             "FINAL_CALL", "CHAOS", "AT_THE_GATE", "FINISH", "RESULTS", "RESET", "HEARTBEAT_COOLDOWN"]
    _check("the walk covers all thirteen modes", set(order) == set(EXPECTED_MODE_STATES))
    rev = b.state_rev
    for mode in order:
        state = EXPECTED_MODE_STATES[mode]
        port.written.clear()
        r = client.post("/api/quiniela/mode", json={"mode": mode})
        body = r.get_json()
        rev += 1
        _check(f"mode {mode} -> state {state} {names[state]}",
               r.status_code == 200 and body == {"ok": True, "mode": mode, "state": state, "state_name": names[state],
                                                 "source": "dashboard", "rev": rev, "gateway_online": False}
               and b.phase == state and port.lines() == [state_line(rev, state)], f"{body} {port.lines()}")
        m = client.get("/api/quiniela").get_json()
        info = client.get("/api/quiniela/mode").get_json()
        _check(f"...the model and GET mode say so ({MODE_LABELS[mode]})",
               m["race_state"] == state and m["race_state_name"] == names[state]
               and (info["state"], info["mode"], info["label"], info["source"]) == (state, mode, MODE_LABELS[mode], "dashboard"),
               str(info))
    # 60 MIN then 30 MIN: two modes, one state. No line, no new rev; the mode is the newer one.
    client.post("/api/quiniela/mode", json={"mode": "BETTING_60"})
    rev = b.state_rev
    port.written.clear()
    r = client.post("/api/quiniela/mode", json={"mode": "betting_30"})
    _check("a second mode of the same state sends nothing and keeps the rev; the mode is the newer one (case forgiven)",
           r.status_code == 200 and r.get_json()["rev"] == rev and r.get_json()["mode"] == "BETTING_30"
           and port.written == [] and client.get("/api/quiniela/mode").get_json()["mode"] == "BETTING_30")
    # The state-only route (tests, the simulator, a curl) is the same path and the same value as a mode.
    port.written.clear()
    r = client.post("/api/quiniela/cmd", json={"cmd": "state 3"})
    info = client.get("/api/quiniela/mode").get_json()
    _check("cmd state 3 (the state-only route): the same state, set directly",
           r.status_code == 200 and r.get_json() == {"ok": True, "rev": rev + 1, "phase": 3, "gateway_online": False}
           and (info["state"], info["state_name"], info["mode"], info["label"], info["source"])
           == (3, "AT_THE_POST", None, None, "cmd") and port.lines() == [state_line(rev + 1, 3)], str(info))
    _check("...and the model the admin page reads its state line from follows", client.get("/api/quiniela").get_json()["race_state"] == 3)
    r = client.post("/api/quiniela/mode", json={"mode": "AT_THE_GATE"})
    info = client.get("/api/quiniela/mode").get_json()
    _check("the dashboard's AT THE GATE on top of it: the same state, no line, the mode named",
           r.get_json()["rev"] == rev + 1 and (info["state"], info["mode"], info["source"]) == (3, "AT_THE_GATE", "dashboard"))
    client.post("/api/quiniela/cmd", json={"cmd": "state 1"})
    info = client.get("/api/quiniela/mode").get_json()
    _check("a mode is only named while it explains the state", (info["state"], info["mode"]) == (1, None))
    html = client.get("/quiniela/admin").get_data(as_text=True)
    _check("the admin page sets no state: it shows the model's race_state, read-only",
           "/api/quiniela/cmd" not in html and 'cmd: "state' not in html and "stateLine(model.race_state)" in html)
    # Something the bridge was told behind the board's back: the mode no longer explains the state.
    client.post("/api/quiniela/mode", json={"mode": "FINAL_CALL"})
    b.set_state(phase=4)
    info = client.get("/api/quiniela/mode").get_json()
    _check("a phase set on the bridge directly: GET mode reports the state, no mode", (info["state"], info["mode"]) == (4, None), str(info))
    # The state line carries everything in one go: a scratch made without a refresh rides along.
    get_board().store.scratch_gateway(7)
    write_results(get_board(), 19, 1, 22)
    port.written.clear()
    rev = b.state_rev
    r = client.post("/api/quiniela/mode", json={"mode": "RESULTS"})
    _check("WINNER goes down in ONE line with the results (and the scratched bit)",
           r.status_code == 200 and port.lines() == [state_line(rev + 1, 5, [7], [], [19, 1, 22])], str(port.lines()))
    # Reset betting is a state change too: PRE_RACE, set by the reset.
    client.post("/api/quiniela/reset")
    info = client.get("/api/quiniela/mode").get_json()
    _check("after Reset betting: state 0, source reset, no mode", (info["state"], info["mode"], info["source"]) == (0, None, "reset"), str(info))
    _, lines = log_lines(log_dir)
    _check("the log recorded the state changes", {"race_state": [4, 5]} in [c for l in lines for c in l["changes"]])
    # Errors
    for bad in ({"mode": "PARTY"}, {"mode": ""}, {"mode": 5}, {"mode": None}, {}, [], {"mode": "state 1"}):
        r = client.post("/api/quiniela/mode", json=bad)
        _check(f"POST mode {bad!r:.30} -> 400 naming the modes", r.status_code == 400 and r.get_json()["ok"] is False
               and "unknown mode" in r.get_json()["error"] and "BETTING_60" in r.get_json()["error"], str(r.get_json()))
    before = (b.phase, b.state_rev)
    _check("...and nothing moved", (b.phase, b.state_rev) == before)
    get_board().bridge = None
    r = client.post("/api/quiniela/mode", json={"mode": "WELCOME"})
    _check("without a bridge -> 503", r.status_code == 503 and r.get_json() == {"ok": False, "error": "bridge not initialised"})
    info = client.get("/api/quiniela/mode").get_json()
    _check("GET mode without a bridge still answers, from the model", info["ok"] is True and info["state"] == 0)
    bd, wall, _ = fresh_board()
    try:
        bd.set_mode("WELCOME")
        _check("set_mode without a bridge raises RuntimeError", False)
    except RuntimeError:
        _check("set_mode without a bridge raises RuntimeError", True)
    b2, port2, sio2, clk2 = _fresh_bridge()
    b2._open_port()
    bd2, _, _ = fresh_board(bridge=b2)
    for bad in (7, -1, "x", None):
        try:
            bd2.set_race_state(bad)
            _check(f"set_race_state({bad!r}) raises ValueError", False)
        except ValueError:
            _check(f"set_race_state({bad!r}) raises ValueError", True)
    try:
        bd2.set_mode("PARTY")
        _check("set_mode('PARTY') raises ValueError", False)
    except ValueError:
        _check("set_mode('PARTY') raises ValueError", True)
    _check("a refused state sent nothing", port2.written == [] and b2.phase == 0)


def test_field_by_post():
    """GET /api/quiniela/field: what the dashboard's SET WINNERS pickers and
    results tote show. A post is a place on the mantle (and its LED cup); the
    horse that runs from it is the post's own or the one standing in for it."""
    records = {9: 22, 22: 23, 20: None, 3: 21}
    _check("horse_at follows the records to the end of the chain",
           [horse_at(p, records) for p in (1, 3, 9, 20)] == [1, 21, 23, None])
    _check("horse_at with no records is the post itself", [horse_at(p, {}) for p in (1, 20)] == [1, 20])
    _check("post_of walks back: 23 runs from post 9, 21 from 3, 22 stood in for 9",
           [post_of(h, records) for h in (23, 21, 22, 7)] == [9, 3, 9, 7])
    _check("post_of an also-eligible standing in for nobody is None", post_of(24, records) is None and post_of(21, {}) is None)
    _check("a record that loops never hangs", horse_at(9, {9: 22, 22: 9}) in (9, 22) and post_of(22, {9: 22, 22: 9}) in (9, None))
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    r = client.get("/api/quiniela/field")
    body = r.get_json()
    _check("GET /api/quiniela/field 200, no-store", r.status_code == 200 and r.headers.get("Cache-Control") == "no-store")
    _check("exactly names_rev, posts, names", set(body) == {"names_rev", "posts", "names"}, str(sorted(body)))
    _check("no names yet: posts 1..20 in order, each its own horse, the label says HORSE n",
           [p["post"] for p in body["posts"]] == list(range(1, 21))
           and body["posts"][6] == {"post": 7, "horse": 7, "name": "", "label": "7 \u00b7 HORSE 7"}, str(body["posts"][6]))
    _check("names carries all 24, empty", body["names"] == {str(n): "" for n in range(1, 25)} and body["names_rev"] == 0)
    client.put("/api/quiniela/horses", json={"text": FIELD_24_TEXT})
    body = client.get("/api/quiniela/field").get_json()
    _check("names upper-cased, the label is 'number \u00b7 NAME'",
           body["posts"][18] == {"post": 19, "horse": 19, "name": "RESILIENCE", "label": "19 \u00b7 RESILIENCE"}
           and body["names"]["22"] == "OCELLI" and body["names_rev"] == 1, str(body["posts"][18]))
    client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    client.post("/api/quiniela/scratch", json={"horse": 20})
    body = client.get("/api/quiniela/field").get_json()
    _check("a replaced post shows the replacement's number and name, and what it replaces",
           body["posts"][8] == {"post": 9, "horse": 22, "name": "OCELLI", "label": "22 \u00b7 OCELLI", "replaces": 9},
           str(body["posts"][8]))
    _check("a post scratched with no replacement is not offered: 19 posts, no post 20",
           [p["post"] for p in body["posts"]] == list(range(1, 20)), str([p["post"] for p in body["posts"]]))
    _check("the horses offered are exactly the model's field",
           sorted(p["horse"] for p in body["posts"])
           == sorted(int(n) for n, h in client.get("/api/quiniela").get_json()["horses"].items() if h["in_field"]))
    client.post("/api/quiniela/scratch", json={"horse": 22, "replacement": {"number": 23}})
    body = client.get("/api/quiniela/field").get_json()
    _check("a chain: post 9 now offers 23", body["posts"][8]["horse"] == 23 and body["posts"][8]["replaces"] == 9
           and body["posts"][8]["label"] == "23 \u00b7 EPIC RIDE", str(body["posts"][8]))
    client.post("/api/quiniela/unscratch", json={"horse": 22})
    client.post("/api/quiniela/unscratch", json={"horse": 9})
    client.post("/api/quiniela/unscratch", json={"horse": 20})
    body = client.get("/api/quiniela/field").get_json()
    _check("all undone: the twenty posts, each its own horse again",
           [(p["post"], p["horse"]) for p in body["posts"]] == [(n, n) for n in range(1, 21)]
           and not any("replaces" in p for p in body["posts"]))
    get_board().bridge = None
    _check("the field needs no bridge", client.get("/api/quiniela/field").status_code == 200)


# -----------------------------------------------------------------------------
# One home for race info: the race, the track's odds, the old Race Setup file
# -----------------------------------------------------------------------------

# The 2027 Derby's post as the tests enter it: 5:57 PM on a Central clock
# (daylight time in May), 22:57 UTC. And the same time of day in January
# (standard time, 23:57 UTC).
POST_2027 = 1_809_212_220.0             # 2027-05-01 17:57 CDT
POST_JAN = 1_800_057_420.0              # 2027-01-15 17:57 CST


def test_race_info_round_trip():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    m = client.get("/api/quiniela").get_json()
    _check("nothing set: KENTUCKY DERBY, no year, no post time, the race's clock",
           m["race"] == {"name": "KENTUCKY DERBY", "year": None, "post_at": None, "post_local": None,
                         "tz": "America/Chicago"}, str(m["race"]))
    r = client.get("/api/quiniela/race")
    _check("GET /api/quiniela/race: the same, the typed name empty, no date or time, no-store",
           r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
           and r.get_json() == {"ok": True, "race": m["race"], "name": "", "date": None, "time": None}, str(r.get_json()))
    r = client.put("/api/quiniela/race", json={"name": "Kentucky Derby", "date": "2027-05-01", "time": "17:57"})
    body = r.get_json()
    want = {"name": "KENTUCKY DERBY", "year": 2027, "post_at": POST_2027, "post_local": "5:57 PM CDT",
            "tz": "America/Chicago"}
    _check("PUT name, date and time on the race's clock: 5:57 PM CDT is 22:57 UTC, the year from the date",
           r.status_code == 200 and body == {"ok": True, "race": want, "name": "Kentucky Derby",
                                              "date": "2027-05-01", "time": "17:57"}, str(body))
    _check("the model carries it", client.get("/api/quiniela").get_json()["race"] == want)
    _check("persisted as typed: a second store on the same database",
           HorseStore(b.db).race() == {"name": "Kentucky Derby", "year": 2027, "post_at": POST_2027})
    _check("GET gives the form back what was entered", client.get("/api/quiniela/race").get_json()
           == {"ok": True, "race": want, "name": "Kentucky Derby", "date": "2027-05-01", "time": "17:57"})
    r = client.put("/api/quiniela/race", json={"date": "2027-01-15", "time": "17:57"})
    _check("a January date is standard time: 5:57 PM CST, 23:57 UTC; the name left alone",
           r.get_json()["race"]["post_at"] == POST_JAN and r.get_json()["race"]["post_local"] == "5:57 PM CST"
           and r.get_json()["race"]["name"] == "KENTUCKY DERBY", str(r.get_json()))
    r = client.put("/api/quiniela/race", json={"post_at": POST_2027})
    _check("or a unix time: the year follows it", r.get_json()["race"] == want, str(r.get_json()))
    board = get_board()
    board.reset_betting()
    _check("Reset betting does not touch it", client.get("/api/quiniela").get_json()["race"] == want)
    r = client.put("/api/quiniela/race", json={"name": "Preakness"})
    _check("a name alone: the post time stays", r.get_json()["race"]["name"] == "PREAKNESS"
           and r.get_json()["race"]["post_at"] == POST_2027)
    r = client.put("/api/quiniela/race", json={"name": "", "date": "", "time": ""})
    _check("empty name, date and time: the default name, no post time, no year",
           r.get_json()["race"] == {"name": "KENTUCKY DERBY", "year": None, "post_at": None, "post_local": None,
                                    "tz": "America/Chicago"} and r.get_json()["date"] is None, str(r.get_json()))
    for bad, why in (({}, "usage"), ({"date": "2027-05-01"}, "go together"), ({"time": "17:57"}, "go together"),
                     ({"date": "May 1", "time": "17:57"}, "YYYY-MM-DD"), ({"date": "2027-05-01", "time": "5:57 PM"}, "HH:MM"),
                     ({"date": "2027-05-01", "time": "25:00"}, "HH:MM"), ({"post_at": "soon"}, "usage"),
                     ({"post_at": True}, "usage"), ({"name": "x" * 81}, "longer"), ({"date": 5, "time": "17:57"}, "usage")):
        r = client.put("/api/quiniela/race", json=bad)
        _check(f"PUT {bad!r:.40} -> 400 ({why})", r.status_code == 400 and why in r.get_json()["error"]
               and r.get_json()["ok"] is False, f"{r.status_code} {r.get_json()}")
    r = client.put("/api/quiniela/race", data="5", content_type="application/json")
    _check("a non-object body -> 400 usage", r.status_code == 400 and r.get_json()["error"] == USAGE_RACE)
    _check("nothing was changed by the refused ones", HorseStore(b.db).race() == {"name": "", "year": None, "post_at": None})


def test_race_clock_and_the_store():
    # Another zone in config: the same instant on another clock.
    board, wall, _ = fresh_board(LQ_RACE_TZ="America/New_York")
    board.store.set_race(post_at=POST_2027)
    _check("LQ_RACE_TZ America/New_York: the same post reads 6:57 PM EDT",
           board.race_view()["post_local"] == "6:57 PM EDT" and board.race_view()["tz"] == "America/New_York")
    _check("the race's clock: May is CDT, January CST, and both round-trip",
           racetime.describe(POST_2027)["label"] == "5:57 PM CDT" and racetime.describe(POST_JAN)["label"] == "5:57 PM CST"
           and racetime.local_to_epoch("2027-05-01", "17:57") == POST_2027
           and racetime.local_to_epoch("2027-01-15", "17:57") == POST_JAN
           and racetime.describe(POST_2027)["iso"] == "2027-05-01T17:57:00-05:00")
    _check("the daylight-time change: 1:59 CST, then 3:00 CDT a minute later (14 March 2027)",
           racetime.describe(racetime.local_to_epoch("2027-03-14", "01:59"))["label"] == "1:59 AM CST"
           and racetime.describe(racetime.local_to_epoch("2027-03-14", "01:59") + 60)["label"] == "3:00 AM CDT")
    store = HorseStore()
    for bad in ({"year": 1800}, {"year": "twenty"}, {"year": True}, {"post_at": float("inf")}, {"post_at": "x"},
                {"name": 5}):
        try:
            store.set_race(**bad)
            ok = False
        except ValueError:
            ok = True
        _check(f"set_race({bad!r}) refused", ok and store.race() == {"name": "", "year": None, "post_at": None})
    changes = []
    store.on_change = lambda: changes.append(1)
    store.set_race(name="Kentucky Derby", year=2027)
    store.set_race(name="Kentucky Derby")
    _check("a change calls on_change once, a write that changes nothing does not", changes == [1])
    # A database from before lq_race: the bridge's start adds it; until then a
    # store reads everything else and keeps the race in memory.
    path = str(tmpdir() / "before_lq_race.db")
    db = LqDb(path)
    db.init_schema()
    db.conn.execute("DROP TABLE lq_race")
    db.save_horse(9, "Encino")
    with capture_logs("la_quiniela.horses", logging.ERROR) as cap:
        old = HorseStore(db)
    _check("without lq_race: the names are read, the race is empty, one ERROR naming the table",
           old.horses()[9]["name"] == "Encino" and old.race_empty and len(cap.messages("lq_race")) == 1, str(cap.messages()))
    db.close()
    b = LqBridge(settings={"LQ_SERIAL_PORT": "/dev/fake"}, db_path=path, serial_factory=lambda p, baud, t: S.FakeSerial(),
                 socketio=S.StubSocketIO(), clock=FakeClock(), console=S.ConsoleCapture())
    try:
        _check("the bridge's start adds lq_race and passes the shape check",
               b.schema_error is None and b.db._columns("lq_race") == ["id", "name", "year", "post_at", "migrated"])
        s = HorseStore(b.db)
        s.set_race(name="Kentucky Derby", post_at=POST_2027, year=2027)
        _check("...and it keeps the race from then on", HorseStore(b.db).race()["post_at"] == POST_2027)
    finally:
        b.close()


def test_odds_in_the_model():
    b, port, sio, clk = _fresh_bridge()
    b._open_port()
    client = _make_board_app(b).test_client()
    board = get_board()
    _check("no odds: every horse's odds null", all(h["odds"] is None for h in client.get("/api/quiniela").get_json()["horses"].values()))
    kept = board.set_odds({"1": "5-2", 22: "30-1", "9": "", "25": "9-1", "x": "1-1", "3": "  8-1 ", "4": None,
                           "5": "NOT ODDS AT ALL", "6": True, " 7 ": "even"})
    _check("kept by program number: 1..24, non-empty, short, upper-cased; the rest dropped",
           kept == {1: "5-2", 22: "30-1", 3: "8-1", 7: "EVEN"}, str(kept))
    board.refresh()
    m = client.get("/api/quiniela").get_json()
    _check("the model: horses[n].odds, null where there are none",
           [m["horses"][str(n)]["odds"] for n in (1, 2, 3, 7, 9, 22, 24)] == ["5-2", None, "8-1", "EVEN", None, "30-1", None])
    board.store.set_names({9: "Encino", 22: "Ocelli"})
    client.post("/api/quiniela/scratch", json={"horse": 9, "replacement": {"number": 22}})
    board.set_odds({9: "8-1", 22: "30-1"})
    board.refresh()
    m = client.get("/api/quiniela").get_json()
    _check("keyed by program number: 22 standing in for 9 has 22's odds, never post 9's",
           m["horses"]["22"]["odds"] == "30-1" and m["horses"]["22"]["in_field"] and m["horses"]["9"]["odds"] == "8-1")
    r = client.put("/api/quiniela/odds", json={"odds": {"7": "9-2", "21": "20-1"}})
    _check("PUT /api/quiniela/odds replaces them all (the morning line by hand)",
           r.status_code == 200 and r.get_json() == {"ok": True, "odds": {"21": "20-1", "7": "9-2"}}
           and client.get("/api/quiniela").get_json()["horses"]["22"]["odds"] is None, str(r.get_json()))
    odds_mod.set_odds_poller(None)
    r = client.get("/api/quiniela/odds")
    _check("GET /api/quiniela/odds: the odds, no poller running", r.status_code == 200 and r.get_json()["odds"] == {"21": "20-1", "7": "9-2"}
           and r.get_json()["polling"] is False)
    _check("no poller initialised: start and stop say so (503)",
           client.post("/api/quiniela/odds/start").status_code == 503 and client.post("/api/quiniela/odds/stop").status_code == 503)
    for bad in ({}, {"odds": 5}, {"odds": ["5-2"]}):
        r = client.put("/api/quiniela/odds", json=bad)
        _check(f"PUT odds {bad!r} -> 400 usage", r.status_code == 400 and r.get_json()["error"] == USAGE_ODDS)
    r = client.put("/api/quiniela/odds", json={"odds": None})
    _check("{odds: null} clears them", r.get_json() == {"ok": True, "odds": {}}
           and all(h["odds"] is None for h in client.get("/api/quiniela").get_json()["horses"].values()))

    # The poller, with a fetch of its own: it asks about the store's race and
    # hands what comes back to the board; nothing back leaves the odds alone.
    asked, emitted, replies = [], [], [{"1": "6-1", "22": "12-1"}, None]
    board.store.set_race(name="Kentucky Derby", post_at=POST_2027)

    def fetch(race):
        asked.append((race["name"], race["year"]))
        return replies.pop(0)
    poller = odds_mod.OddsPoller(fetch, board.set_odds, board.race_view, emit=emitted.append)
    _check("a round with odds: the board has them, keyed by number, and odds_update is emitted",
           poller.poll_once() is True and board.odds() == {1: "6-1", 22: "12-1"} and asked == [("KENTUCKY DERBY", 2027)]
           and emitted[0]["odds"] == {"1": "6-1", "22": "12-1"} and poller.last_update is not None)
    _check("a round with nothing (no internet): the odds stay as they were",
           poller.poll_once() is False and board.odds() == {1: "6-1", 22: "12-1"} and len(emitted) == 1)
    no_key = odds_mod.OddsPoller(fetch, board.set_odds, board.race_view, enabled=False)
    _check("without an API key it does not start", no_key.start() == {"ok": False, "error": "ANTHROPIC_API_KEY not configured",
                                                                       "status": 503} and not no_key.polling())
    replies[:] = [{"2": "3-1"}] * 5
    odds_mod.set_odds_poller(poller)
    try:
        r = client.post("/api/quiniela/odds/start", json={"interval": 5})
        _check("POST start: at least 60 s between rounds, running", r.status_code == 200
               and r.get_json() == {"ok": True, "interval": 60} and poller.polling())
        _check("a second start is refused (409)", client.post("/api/quiniela/odds/start").status_code == 409)
        deadline = time.time() + 5
        while board.odds() != {2: "3-1"} and time.time() < deadline:
            time.sleep(0.02)
        _check("the thread's first round lands in the model", board.odds() == {2: "3-1"})
        r = client.post("/api/quiniela/odds/stop")
        poller._thread.join(5)
        _check("POST stop: it stops", r.get_json() == {"ok": True, "stopped": True} and not poller.polling())
    finally:
        poller.stop()
        odds_mod.set_odds_poller(None)
    _check("reading a reply: fenced JSON, prose around it, a bare object, {odds: {...}}",
           odds_mod.odds_from_reply(odds_mod.extract_json('```json\n{"odds": {"1": "5-2"}}\n```')) == {"1": "5-2"}
           and odds_mod.odds_from_reply(odds_mod.extract_json('Here: {"3": "8-1", "22": null} as asked')) == {"3": "8-1", "22": ""}
           and odds_mod.odds_from_reply(odds_mod.extract_json("no json here")) is None
           and odds_mod.odds_from_reply(["5-2"]) is None)


def test_migrate_race_setup_once():
    board, wall, _ = fresh_board()
    d = tmpdir()
    path = d / "race_setup.json"
    _check("no file: nothing to do, nothing remembered",
           migrate_race_setup(path, board) == {"file": False, "copied_post_at": None, "copied_names": 0, "before": False}
           and not board.store.race_migrated)
    path.write_text(json.dumps({"race_name": "Derby de Mayo 2026", "post_time": "18:57",
                                "horses": {"1": "Sovereignty", "2": "Journalism", "3": "", "21": "Not a post"},
                                "odds": {"1": "5-2"}}), encoding="utf-8")
    with capture_logs("la_quiniela.board", logging.INFO) as cap:
        out = migrate_race_setup(path, board)
    old_post = racetime.local_to_epoch("2026-05-02", "18:57", "America/New_York")
    _check("the first start: the old post time (6:57 PM ET on Derby day 2026) and the two names copied",
           out == {"file": True, "copied_post_at": old_post, "copied_names": 2, "before": False}
           and board.store.race() == {"name": "", "year": 2026, "post_at": old_post}
           and board.store.name_of(1) == "Sovereignty" and board.store.name_of(3) == "", str(out))
    _check("...on the race's clock that is 5:57 PM CDT", board.race_view()["post_local"] == "5:57 PM CDT")
    _check("...the file is left where it is, and the log says it is obsolete",
           path.exists() and any("obsolete" in msg for msg in cap.messages()), str(cap.messages()))
    board.store.set_race(post_at=None, year=None)
    board.store.set_names({1: ""})
    _check("once: a later start copies nothing again, even with the race and the names emptied",
           migrate_race_setup(path, board) == {"file": True, "copied_post_at": None, "copied_names": 0, "before": True}
           and board.store.race_empty and board.store.name_of(1) == "")
    # A store that already has race info and names: neither is overwritten.
    other, _, _ = fresh_board()
    other.store.set_race(post_at=POST_2027)
    other.store.set_names({5: "Great White"})
    out = migrate_race_setup(path, other)
    _check("race info and names already set: nothing copied, the file looked at",
           out["copied_post_at"] is None and out["copied_names"] == 0 and other.store.race()["post_at"] == POST_2027
           and other.store.name_of(1) == "" and other.store.race_migrated, str(out))
    third, _, _ = fresh_board()
    path.write_text("{not json", encoding="utf-8")
    with capture_logs("la_quiniela.board", logging.WARNING) as cap:
        out = migrate_race_setup(path, third)
    _check("an unreadable file: a warning, nothing copied, looked at once",
           out["copied_post_at"] is None and third.store.race_empty and third.store.race_migrated
           and any("cannot read" in msg for msg in cap.messages()), str(cap.messages()))


def test_weather_in_the_model():
    board, wall, _ = fresh_board()
    _check("no weather: null", board.model()["weather"] is None)
    kept = board.set_weather({"location": " Dallas ", "temp_f": 87.6, "condition": "Partly  cloudy"})
    board.refresh()
    _check("pi5's weather in the model: the place, a whole number of degrees, the sky",
           kept == {"location": "Dallas", "temp_f": 88, "condition": "Partly cloudy"} and board.model()["weather"] == kept,
           str(board.model()["weather"]))
    _check("what counts", clean_weather({"temp_f": "x", "condition": None, "location": 5}) is None
           and clean_weather({"temp_f": True}) is None and clean_weather("88F") is None
           and clean_weather({"temp_f": 90}) == {"location": None, "temp_f": 90, "condition": None}
           and clean_weather({"location": "x" * 50, "temp_f": None, "condition": "Sunny"})["location"] == "x" * 40)
    board.set_weather(None)
    board.refresh()
    _check("None clears it", board.model()["weather"] is None)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    print(f"La Quiniela betting board test\n  DB: {S._TMP_DB}")

    _run("model — empty snapshot shape", test_empty_snapshot_model_shape)
    _run("model — tokens, share, leader, online, conflict", test_tokens_share_leader_online_conflict)
    _run("model — leader None / lowest on tie", test_leader_none_without_tokens_and_lowest_on_tie)
    _run("model — odd entries never raise", test_odd_entries_never_raise)
    _run("model — events newest first, last eight", test_events_diff_newest_first_last_eight)
    _run("model — a renumber and a reset produce no ghost bets", test_renumber_and_reset_produce_no_ghost_bets)
    _run("model — two cups on one horse is a conflict, warned once", test_duplicate_horse_is_a_conflict_and_warns_once)
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
    _run("renumber — in_field: a plain field, a replaced field, a no-replacement scratch", test_in_field_rules)
    _run("payout — parse_names_text", test_parse_names_text)
    _run("renumber — POST /api/quiniela/scratch rejections", test_routes_scratch_rejections)
    _run("renumber — a scratch before any cup says the horse", test_scratch_before_any_cup_reports_the_horse)
    _run("payout — a no-replacement scratch takes its tokens out of the pot", test_kind2_scratch_removes_tokens_from_the_pot)
    _run("scratch — a no-replacement scratch is about the horse, not the cup", test_no_replacement_scratch_is_about_the_horse)
    _run("scratch — the lq_scratches migration (now nullable)", test_lq_scratches_migration)
    _run("payout — reset clears closes_at, keeps names; the tables", test_reset_clears_closes_at_and_keeps_names)
    _run("renumber — the lq_horses migration and the lq_scratches table", test_lq_horses_migration_and_scratches_table)
    _run("payout — GET/PUT /api/quiniela/horses", test_routes_horses_get_and_put)
    _run("renumber — POST /api/quiniela/scratch and /unscratch on a real bridge", test_routes_scratch_and_unscratch)
    _run("renumber — the lifecycle of the pairs in the state line", test_renum_lifecycle)
    _run("results — from the dashboard's file into the state line", test_results_from_the_dashboard_file)
    _run("closing — the figures at the post: taken, held, dropped, saved", test_closing_figures)
    _run("closing — a database from before lq_closing gains it at start", test_lq_closing_on_an_existing_database)
    _run("closing — the results and the closing figures survive a restart", test_results_and_closing_survive_a_restart)
    _run("counted pot — the model: same function, held, dropped, refused, bad input", test_counted_pot_model)
    _run("counted pot — PUT /api/quiniela/counted_pot, a restart, Clear, Reset betting", test_counted_pot_route_and_restart)
    _run("payout — PUT /api/quiniela/closes_at", test_routes_closes_at)
    _run("payout — GET /quiniela/admin", test_admin_page)
    _run("reset — POST /api/quiniela/reset", test_reset_betting_route)
    _run("v2 — the removed routes are gone; a conflict and a horse-0 cup on a real bridge", test_removed_routes_and_conflict_on_a_real_bridge)
    _run("settings — the payout keys", test_settings_new_keys)
    _run("names — the field by post (GET /api/quiniela/field)", test_field_by_post)
    _run("one race state — each mode, the cmd route and the admin page agree", test_modes_set_the_race_state)
    _run("race info — the routes, the model, the database, Reset betting keeps it", test_race_info_round_trip)
    _run("race info — the race's clock, the store's checks, lq_race on an old database", test_race_clock_and_the_store)
    _run("odds — by program number, null when missing; the poller and its routes", test_odds_in_the_model)
    _run("race info — the old Race Setup file, once", test_migrate_race_setup_once)
    _run("crawl — pi5's weather in the model", test_weather_in_the_model)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

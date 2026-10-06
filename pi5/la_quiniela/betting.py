# la_quiniela/betting.py - The live betting board, fed from the bridge
#
# The splash display's TV board renders one JSON model: tokens per horse,
# shares, the leader, the last few drops, the race state and whether the
# link is up. That model used to be built on the splash Pi from the
# gateway's own `json 1` state stream; now it is built here, from what the
# bridge already knows (get_snapshot()), and the splash fetches it over HTTP.
#
# Nothing in this file reads the serial port. BettingBoard.apply_snapshot()
# digests one get_snapshot() dict (pure, so tests need no bridge), refresh()
# takes a snapshot from the bridge, applies it and keeps the gateway's state
# line in step with the store (the scratched bits, the renumber pairs, the
# results), and a daemon thread calls refresh() whenever the bridge's
# listener wakes it, and once a second regardless (so link_ok follows a
# gateway that has gone quiet).
#
# The model JSON, the SSE bytes and validate_cmd() are a contract with the
# board page (splash_display/static/js/quiniela_board.js) and must not
# change; the one documented difference from the old splash model is what
# horses[n].cup holds: since protocol v2 it is the MAC of the cup claiming
# that horse (null when none does), never a cup number. The board only tests
# it for null. Additive keys: conflict and cups per horse, cups_online,
# cups_no_horse, results and closing on the model. results is {"win",
# "place", "show"} (horse numbers) once the dashboard has them, null until
# then: in WINNER the TV board shows its results screen from them, and the
# frozen betting board under OFFICIAL RESULTS COMING while it is null.
# closing is the figures as they were at the post (pot, prizes, total_tokens
# and every horse's tokens, the live fields' shapes, plus "at"): taken when
# the race state first reaches AT_THE_POST (or RUNNING or WINNER when that
# was skipped), persisted, and dropped only by the between-races reset or a
# state that reopens betting (PRE_RACE, BETTING_OPEN); null while there are
# none. The TV board shows them in 3, 4 and 5, so a page loaded after the
# cups were emptied for the draw, a second screen or a restart of pi5 all
# show the numbers at the post. The live fields keep following the cups.
# The counted pot: the scales are estimates, so after betting closes the host
# counts the cash box and enters the dollars (set_pot_counted(), PUT
# /api/quiniela/counted_pot, states 3-5). The count is stored inside the
# closing record (its "pot_counted"), so it is saved, held and dropped exactly
# as the figures at the post are. While it is held the pot and the three
# prizes are the count's (prizes_for(count), the same split and rounding), in
# closing's pot and prizes and in the model's own from the post to the end
# (3-6; in FINAL CALL betting is open again and the model's are the live
# ones); the bets per horse stay the scales'. pot_scale is the scale pot frozen
# at the post, pot_counted the count (or null), hand_counted whether the
# model's pot and prizes are the count's.
# race is the race itself from the store (name, year, post time as unix
# seconds and as it reads on the race's clock, and that clock's zone): the
# one home of race information, which the TV's countdown and roster slides
# read. horses[n].odds is the real track's odds for that program number, a
# string, from the odds poller (odds.py) when it runs, else null: they are
# for the slideshow, never for La Quiniela, which pays no odds. weather is
# pi5's weather ({"location", "temp_f", "condition"}, from the dashboard's
# source, fed by main.py) or null, for the TV's crawl.
#
# How La Quiniela pays, which is what the additive keys carry: a token is one
# dollar and one raffle ticket. After the race one token is drawn from the WIN
# cup, one from PLACE, one from SHOW, and each drawn token's owner takes that
# cup's whole prize, a fixed fraction of the pot (prizes_for()). Nobody
# splits anything and there are no odds; the only number per horse is how
# many tokens are in its cup. Names, the scratch records and the closing
# time come from the HorseStore (horses.py), never from the gateway.
#
# Horses are numbers 1..24 (protocol.MAX_HORSE): 1..20 the field, 21..24 the
# also-eligibles. The cup owns its number (protocol v2): pi5 learns which
# horses have cups by listening, and two cups claiming one horse is a
# conflict the model reports, not something pi5 resolves. A replacement
# scratch is a renumber (The Puma, #9, out; Ocelli in as #22, on the same
# cup): the store's record makes the board send the pair [9, 22] down, the
# cup that was 9 becomes 22, and because its tokens simply show up under the
# new number, the same-cup rule below means a renumber produces no event and
# never changes the pot.

import json
import logging
import os
import queue
import threading
import time
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from la_quiniela import protocol as P
from la_quiniela import racetime
from la_quiniela.horses import HorseStore, in_field

log = logging.getLogger("la_quiniela.betting")

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent          # pi5/
LOG_DIR = BASE_DIR / "data"          # quiniela_YYYY-MM-DD.jsonl lives here (git-ignored)
RESULTS_FILE = LOG_DIR / "results.json"   # the dashboard's results (main.py RESULTS_FILE, POST /api/results)

HORSE_COUNT = P.MAX_HORSE            # 24: the model's horses map is keyed "1".."24"
MAX_EVENTS = 8                       # the "recent drops" ring the board shows
SSE_HEARTBEAT_S = 5.0                # ping after this much silence
SSE_QUEUE_SIZE = 32                  # per-subscriber queue; the oldest is dropped when full
REFRESH_S = 1.0                      # the board thread's timeout between wake-ups
UNDO_RENUM_S = 60.0                  # an undone renumber is sent back (to -> was) this long, or until a cup reports was

CMD_WHITELIST = frozenset({"state", "demo", "json"})

# Betting is closed from AT_THE_POST on: the closing figures are taken the
# first time the race state is one of these with none held, and dropped when
# it is one of the reopening states (or by the between-races reset).
# FINAL_CALL and AFTER_PARTY leave them as they are.
CLOSED_STATES = frozenset({int(P.Phase.AT_THE_POST), int(P.Phase.RUNNING), int(P.Phase.WINNER)})
REOPEN_STATES = frozenset({int(P.Phase.PRE_RACE), int(P.Phase.BETTING_OPEN)})

# The hand count (the counted pot): after betting closes the host counts the
# cash box's BETS compartment and types the dollars, and from then on the pot
# and all three prizes come from that number. It can be entered in the
# CLOSED_STATES once the figures at the post exist, and it lives inside that
# very record (its "pot_counted" key), so it is saved, held and dropped
# exactly as they are. COUNTED_POT_MAX only catches a typo (a party's pot is a
# few hundred dollars).
COUNTED_POT_MAX = 10000
# From the post to the end (3, 4, 5, 6) the model's pot and prizes are the
# count's. The count is held through FINAL_CALL like the figures at the post,
# but betting is open again there, so in 2 the model's pot is the live one.
COUNT_IN_FORCE = CLOSED_STATES | frozenset({int(P.Phase.AFTER_PARTY)})

# One race state. The dashboard's modes (its thirteen buttons, which drive
# the LEDs) are the source of truth, and La Quiniela's race state (the cups,
# the TV board) is derived from the mode through this table. The keys are
# the names the dashboard's buttons already use. A button names its mode to
# POST /api/quiniela/mode; SET WINNERS and RESET are told by the dashboard
# routes they call (/api/results once the results are applied,
# /api/results/clear).
MODE_STATES: Dict[str, int] = {
    "WELCOME": 0, "TEST": 0, "STANDBY": 0,          # PRE_RACE
    "BETTING_60": 1, "BETTING_30": 1,               # BETTING_OPEN   60 MIN, 30 MIN
    "FINAL_CALL": 2,                                # FINAL_CALL
    "AT_THE_GATE": 3,                               # AT_THE_POST
    "GATES_BURST": 4, "CHAOS": 4, "FINISH": 4,      # RUNNING        THEY'RE OFF, CHAOS, FINISH
    "RESULTS": 5,                                   # WINNER         SET WINNERS, once the results are applied
    "HEARTBEAT_COOLDOWN": 5,                        # WINNER         HEARTBEAT: the race is over, the results may still be coming
    "RESET": 6,                                     # AFTER_PARTY    the dashboard has no party mode: RESET ends the race
}
# What each button says, for messages.
MODE_LABELS: Dict[str, str] = {
    "WELCOME": "WELCOME", "TEST": "TEST", "STANDBY": "STANDBY",
    "BETTING_60": "60 MIN", "BETTING_30": "30 MIN", "FINAL_CALL": "FINAL CALL",
    "AT_THE_GATE": "AT THE GATE", "GATES_BURST": "THEY'RE OFF", "CHAOS": "CHAOS", "FINISH": "FINISH",
    "RESULTS": "SET WINNERS", "HEARTBEAT_COOLDOWN": "HEARTBEAT", "RESET": "RESET",
}
CMD_MAX_LEN = 200

# Configuration keys, their defaults, and the environment override DDM_<key>.
# The names are the ones the splash display used, so its config carries over.
DEFAULTS: Dict[str, Any] = {
    "TOKEN_VALUE": 1.0,                       # dollars per token, for the POT
    "QUINIELA_LOG": True,                     # write pi5/data/quiniela_YYYY-MM-DD.jsonl
    "QUINIELA_BOARD_STATES": [1, 2, 3, 4, 5], # race states in which the board owns the TV (5: the results)
    "LQ_SPLIT_WIN": 0.60,                     # the WIN prize's share of the pot (it takes the remainder)
    "LQ_SPLIT_PLACE": 0.25,                   # the PLACE prize's share, rounded half up to whole dollars
    "LQ_SPLIT_SHOW": 0.15,                    # the SHOW prize's share, likewise
    "LQ_CHYRON_LINES": [                      # what crawls along the bottom of the board
        "TOTALS BASED ON CHEAP CHINESE ELECTRONICS - FINAL RESULTS HAND COUNTED",
        "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS",
    ],
    "LQ_RACE_TZ": racetime.DEFAULT_TZ,        # the race's clock: the post time is entered and shown in it
}
DEFAULT_RACE_NAME = "KENTUCKY DERBY"          # the race's name while none is stored
ODDS_MAX_LEN = 7                              # "50-1", "5-2", "EVEN": anything longer is not odds
WEATHER_TEXT_MAX = 40                         # a location or a condition longer than this is cut
SPLIT_KEYS = ("LQ_SPLIT_WIN", "LQ_SPLIT_PLACE", "LQ_SPLIT_SHOW")
SPLIT_SUM_TOLERANCE = 0.001

RACE_STATE_NAMES: Dict[int, str] = {int(p.value): p.name for p in P.Phase}


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def race_state_name(state: int) -> str:
    return RACE_STATE_NAMES.get(state, f"STATE_{state}")


# -----------------------------------------------------------------------------
# Coercion helpers: a snapshot field is never allowed to raise
# -----------------------------------------------------------------------------

def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Coerce a JSON value to int, or return default. Never raises."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _as_bool(value: Any) -> bool:
    """bool() with the obvious readings of numeric and text values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "0", "false", "no", "off", "none", "null"):
            return False
        return True
    return bool(value)


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

def _coerce_setting(key: str, value: Any, source: str) -> Tuple[bool, Any]:
    """(ok, coerced). ok is False, after one WARNING, for a value that is not
    of the key's kind; the caller then keeps what it had."""
    try:
        if key == "TOKEN_VALUE":
            if isinstance(value, bool):
                raise ValueError("a bool is not a price")
            out = float(value if not isinstance(value, str) else value.strip())
            if out != out or out in (float("inf"), float("-inf")):
                raise ValueError("not a finite number")
            return True, out
        if key in SPLIT_KEYS:
            if isinstance(value, bool):
                raise ValueError("a bool is not a fraction")
            out = float(value if not isinstance(value, str) else value.strip())
            if out != out or not 0.0 <= out <= 1.0:
                raise ValueError("expected a fraction between 0 and 1")
            return True, out
        if key == "LQ_CHYRON_LINES":
            if isinstance(value, str):
                items = value.split("|")
            elif isinstance(value, (list, tuple)):
                items = list(value)
            else:
                raise ValueError('expected a list of strings or a "|"-separated string')
            lines: List[str] = []
            for item in items:
                if not isinstance(item, str):
                    raise ValueError(f"{item!r} is not a string")
                if item.strip():
                    lines.append(item.strip())
            return True, lines
        if key == "QUINIELA_LOG":
            if isinstance(value, bool):
                return True, value
            if isinstance(value, int):
                return True, value != 0
            if isinstance(value, str):
                text = value.strip().lower()
                if text in ("1", "true", "yes", "on"):
                    return True, True
                if text in ("0", "false", "no", "off"):
                    return True, False
            raise ValueError("expected 1/true/yes/on or 0/false/no/off")
        if key == "LQ_RACE_TZ":
            if not isinstance(value, str) or not value.strip():
                raise ValueError('expected a time zone name such as "America/Chicago"')
            return True, value.strip()
        if key == "QUINIELA_BOARD_STATES":
            if isinstance(value, str):
                parts = [p.strip() for p in value.split(",")]
                items: List[Any] = [p for p in parts if p]
            elif isinstance(value, (list, tuple)):
                items = list(value)
            else:
                raise ValueError("expected a list of ints or a comma-separated string")
            out_list: List[int] = []
            for item in items:
                if isinstance(item, bool) or not isinstance(item, (int, str)):
                    raise ValueError(f"{item!r} is not an int")
                out_list.append(int(item.strip() if isinstance(item, str) else item))
            return True, out_list
        return True, value
    except (TypeError, ValueError, OverflowError) as exc:   # OverflowError: float() of a huge int
        log.warning("La Quiniela board: ignoring %s %s=%r (%s)", source, key, value, exc)
        return False, None


def load_board_settings(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """DEFAULTS, then the same-named attributes of pi5/config.py, then the
    DDM_<KEY> environment variables (DDM_TOKEN_VALUE a float, DDM_QUINIELA_LOG
    a bool, DDM_QUINIELA_BOARD_STATES a comma list such as "1,2,3,4,5", the
    DDM_LQ_SPLIT_* fractions, DDM_LQ_CHYRON_LINES a "|"-separated list), then
    explicit overrides. A value that does not parse is logged and skipped;
    nothing here raises, so importing the app cannot fail on a typo. Three
    splits that do not sum to 1 are warned about once (win takes the
    remainder regardless, so the prizes still sum to the pot)."""
    settings: Dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        import config as app_config  # pi5/config.py, on sys.path when the app runs
    except Exception:
        app_config = None
    for key in DEFAULTS:
        if app_config is not None and hasattr(app_config, key):
            ok, value = _coerce_setting(key, getattr(app_config, key), "config.py")
            if ok:
                settings[key] = value
        raw = os.environ.get("DDM_" + key)
        if raw is not None:
            ok, value = _coerce_setting(key, raw, "environment")
            if ok:
                settings[key] = value
    for key, value in (overrides or {}).items():
        if key in DEFAULTS:
            ok, value = _coerce_setting(key, value, "override")
            if not ok:
                continue
        settings[key] = value
    _warn_split_sum(settings)
    return settings


_split_warned: set = set()


def _warn_split_sum(settings: Dict[str, Any]) -> None:
    try:
        parts = tuple(float(settings[k]) for k in SPLIT_KEYS)
    except (KeyError, TypeError, ValueError):
        return
    if abs(sum(parts) - 1.0) <= SPLIT_SUM_TOLERANCE or parts in _split_warned:
        return
    _split_warned.add(parts)
    log.warning("La Quiniela board: LQ_SPLIT_WIN + LQ_SPLIT_PLACE + LQ_SPLIT_SHOW = %.4f, not 1; "
                "WIN takes the remainder after PLACE and SHOW", sum(parts))


# -----------------------------------------------------------------------------
# Prizes
# -----------------------------------------------------------------------------

def round_half_up(value: Any) -> int:
    """To the nearest whole number, halves up: 38.5 -> 39. Python's round()
    is banker's rounding and gives 38, which is not how a prize is called."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def prizes_for(pot: Any, split: Dict[str, Any]) -> Dict[str, int]:
    """Whole-dollar prizes that sum to the pot: PLACE and SHOW are their
    fractions of the pot rounded half up, WIN is whatever is left, so
    pot 154 -> 92 / 39 / 23 and pot 1 -> 1 / 0 / 0. The arithmetic is done in
    Decimal from the printed values, so 154 * 0.25 is exactly 38.50. A pot
    that is not a whole number of dollars (a fractional token value) is
    itself rounded half up before WIN is taken."""
    try:
        whole = Decimal(str(pot))
        place = round_half_up(whole * Decimal(str(split["place"])))
        show = round_half_up(whole * Decimal(str(split["show"])))
        win = round_half_up(whole) - place - show
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return {"win": 0, "place": 0, "show": 0}
    return {"win": win, "place": place, "show": show}


# -----------------------------------------------------------------------------
# Results: the dashboard's file
# -----------------------------------------------------------------------------

def read_results(path: Any = None) -> List[int]:
    """[win, place, show] from the dashboard's results file (main.py writes
    {"win","place","show","timestamp"} on POST /api/results); [0, 0, 0] when
    there is no file, it does not parse, a number is out of 1..24 or two
    are the same. Never raises."""
    target = Path(path) if path is not None else RESULTS_FILE
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return [0] * P.RESULT_SLOTS
    if not isinstance(data, dict):
        return [0] * P.RESULT_SLOTS
    out: List[int] = []
    for key in ("win", "place", "show"):
        h = _as_int(data.get(key), 0) or 0
        out.append(h if 1 <= h <= HORSE_COUNT else 0)
    named = [h for h in out if h]
    if len(set(named)) != len(named):
        return [0] * P.RESULT_SLOTS
    return out


def clear_results(path: Any = None) -> bool:
    """Remove the dashboard's results file. True if there was one."""
    target = Path(path) if path is not None else RESULTS_FILE
    try:
        os.remove(target)
        return True
    except OSError:
        return False


# -----------------------------------------------------------------------------
# The model
# -----------------------------------------------------------------------------

def _unassigned() -> Dict[str, Any]:
    """A horse no cup claims. name / replaced / in_field are filled in by
    _name_horses(): in_field is true for 1..20 unless scratched, so a horse
    in the field with no cup yet still reads in_field true; 21..24 read
    false until they stand in for someone."""
    return {"tokens": 0, "share": 0.0, "scratched": False, "online": False, "cup": None,
            "conflict": False, "cups": [], "name": "", "replaced": None, "in_field": False, "odds": None}


def clean_odds(odds: Any) -> Dict[int, str]:
    """{program number: odds} from whatever an odds source hands over: keys
    that are numbers 1..24 (ints or strings), values non-empty strings of at
    most ODDS_MAX_LEN characters ("5-2", "50-1"), upper-cased. Anything else
    is dropped, so a horse with no odds simply has none."""
    out: Dict[int, str] = {}
    if not isinstance(odds, dict):
        return out
    for key, value in odds.items():
        n = _as_int(key)
        if n is None or not 1 <= n <= HORSE_COUNT or value is None or isinstance(value, bool):
            continue
        text = " ".join(str(value).split()).upper()
        if text and len(text) <= ODDS_MAX_LEN:
            out[n] = text
    return out


def clean_weather(weather: Any) -> Optional[Dict[str, Any]]:
    """{"location": "Dallas", "temp_f": 88, "condition": "Sunny"} from what
    main.py's weather feed hands over: strings trimmed and cut at
    WEATHER_TEXT_MAX, the temperature a whole number; a missing or odd part
    is None. None when nothing is left."""
    if not isinstance(weather, dict):
        return None

    def text(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())[:WEATHER_TEXT_MAX]
        return value or None

    temp = weather.get("temp_f")
    try:
        temp_f = None if temp is None or isinstance(temp, bool) else int(round(float(temp)))
    except (TypeError, ValueError, OverflowError):
        temp_f = None
    out = {"location": text(weather.get("location")), "temp_f": temp_f,
           "condition": text(weather.get("condition"))}
    return out if any(v is not None for v in out.values()) else None


def _with_now(text: str, now: float) -> str:
    """The compact model JSON with the server's clock appended. The stored
    model never holds "now" (it would make every snapshot a change), so it is
    stamped here, at serialisation, onto the closing brace of a non-empty
    object."""
    return text[:-1] + ',"now":' + _dumps(now) + "}"


def _results_dict(results: Any) -> Optional[Dict[str, Optional[int]]]:
    """The model's results: {"win", "place", "show"} as horse numbers, or
    None while no place is named. A place not named yet is None in the dict
    (the dashboard names all three at once, so that takes a hand-made file);
    the TV's results screen waits for all three."""
    out: Dict[str, Optional[int]] = {"win": None, "place": None, "show": None}
    if isinstance(results, (list, tuple)):
        for key, value in zip(("win", "place", "show"), results):
            h = _as_int(value, 0) or 0
            out[key] = h if 1 <= h <= HORSE_COUNT else None
    if all(v is None for v in out.values()):
        return None
    return out


def _closing_figures(pot: float, prizes: Dict[str, int], total: int,
                     horses: Dict[str, Dict[str, Any]], at: float) -> Dict[str, Any]:
    """The model's closing: the live fields as they are now, in the same
    shapes (pot, prizes, total_tokens, and horses "1".."24" each with its
    tokens), plus "at", the wall time they were taken."""
    return {"pot": pot, "prizes": dict(prizes), "total_tokens": total,
            "horses": {key: {"tokens": h["tokens"]} for key, h in horses.items()},
            "at": round(at, 3)}


def _closing_note(closing: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the log records of the closing figures: the pot, the prizes and
    the token count (each horse's count is in the log already), or None
    when they were dropped."""
    if closing is None:
        return None
    return {"pot": closing["pot"], "prizes": closing["prizes"], "total_tokens": closing["total_tokens"]}


class CountRefused(Exception):
    """The hand count cannot be entered (or cleared) now: betting is not
    closed, or the figures at the post do not exist yet. The message is
    plain; PUT /api/quiniela/counted_pot answers it with a 409."""


def counted_pot_arg(value: Any) -> int:
    """The hand count as a whole number of dollars, 0..COUNTED_POT_MAX. A
    bool, a float, a string or anything else is a ValueError with a plain
    message (the route's 400)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("the count must be a whole number of dollars, 0 to %d (no cents, no $ sign)"
                         % COUNTED_POT_MAX)
    if not 0 <= value <= COUNTED_POT_MAX:
        raise ValueError("%d dollars is outside 0 to %d" % (value, COUNTED_POT_MAX))
    return value


def _stored_count(closing: Optional[Dict[str, Any]]) -> Optional[int]:
    """The hand count a closing record holds, or None: no record, no count,
    or a stored value that is not a whole number of dollars of 0 or more
    (ignored, never raised: a bad row must not take the board down)."""
    if not isinstance(closing, dict):
        return None
    value = closing.get("pot_counted")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _closing_view(closing: Optional[Dict[str, Any]], split: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """The model's closing from the stored record: the same five keys it
    always had (pot, prizes, total_tokens, horses, at). Without a hand count
    the pot and prizes are the scale figures as taken at the post; with one
    the pot is the count and the prizes are prizes_for(count, split), the very
    function the scale pot went through. The stored "pot_counted" itself is
    the model's own key, not part of closing."""
    if closing is None:
        return None
    view = {key: value for key, value in closing.items() if key != "pot_counted"}
    counted = _stored_count(closing)
    if counted is not None:
        view["pot"] = float(counted)
        view["prizes"] = prizes_for(counted, split)
    return view


def _money_view(closing: Optional[Dict[str, Any]], pot: float, prizes: Dict[str, int],
                split: Dict[str, float], race_state: int) -> Dict[str, Any]:
    """What the model says about the money once the figures at the post and
    the hand count are known; pot and prizes are the live scale figures from
    the cups. closing: _closing_view(), always the count's when there is one.
    pot_scale: the scale pot frozen at the post (None while there are no
    figures at the post). pot_counted: the hand count held in whole dollars,
    or None. hand_counted: whether the pot and prizes below are the count's,
    which is when one is held and the race is in COUNT_IN_FORCE; then pot and
    prizes are the count's, else the live ones."""
    counted = _stored_count(closing)
    view = _closing_view(closing, split)
    scale = closing.get("pot") if isinstance(closing, dict) else None
    in_force = counted is not None and view is not None and race_state in COUNT_IN_FORCE
    if in_force:
        pot, prizes = view["pot"], view["prizes"]
    return {"pot": pot, "prizes": prizes, "closing": view, "pot_scale": scale,
            "pot_counted": counted, "hand_counted": in_force}


class BettingBoard:
    """The betting model, its event log, its SSE subscribers and the thread
    that keeps it current.

    apply_snapshot() digests one bridge snapshot (get_snapshot()); refresh()
    fetches one from the bridge, applies it, and pushes whatever the state
    line should carry (scratched bits, renumber pairs, results) when the
    bridge's copy differs. Both publish the full model to every subscriber
    queue when, and only when, the model changed.

    The board never talks to the serial port. It reads the bridge's picture;
    the bridge stays the source of truth for the phase and for which cups it
    has heard, the store for names and scratches, the dashboard's results
    file for the results.

    clock is a monotonic seconds source (the undo-renumber timer runs on
    it); wall is unix time, for the timestamps in the model and the log.
    Both are injectable for tests, as are log_dir and results_path.

    store is the HorseStore of names, scratches and the closing time: by
    default one on the bridge's database (memory-only without a bridge). Its
    on_change is wired to wake(), so an admin write refreshes the model.
    """

    def __init__(
        self,
        bridge: Any = None,
        settings: Optional[Dict[str, Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        log_dir: Optional[Path] = None,
        store: Optional[HorseStore] = None,
        results_path: Optional[Any] = None,
    ) -> None:
        self.bridge = bridge
        self.settings: Dict[str, Any] = {k: (list(v) if isinstance(v, list) else v)
                                         for k, v in DEFAULTS.items()}
        self.settings.update(settings or {})
        self._clock = clock
        self._wall = wall
        self._log_dir = log_dir            # None = module-level LOG_DIR, read at write time
        self._results_path = Path(results_path) if results_path is not None else RESULTS_FILE
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()   # serialises refresh(): snapshot then apply, in order
        self._subs: List["queue.Queue[str]"] = []
        self._seen_state = False
        self._events: List[Dict[str, Any]] = []
        self._undo_renum: Dict[Tuple[int, int], float] = {}   # (to, was) -> deadline on self._clock
        self._race_mode: Optional[str] = None      # the dashboard mode that last set the race state
        self._race_source: Optional[str] = None    # "dashboard" | "cmd" | "reset"; None: nothing set since start
        self._dup_warned: set = set()
        self._sync_warned: set = set()
        self._log_enabled = True
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.store: HorseStore = store if store is not None else HorseStore(getattr(bridge, "db", None))
        if self.store.on_change is None:
            self.store.on_change = self.wake
        self._closing: Optional[Dict[str, Any]] = self.store.closing   # survives a restart (lq_closing)
        self._closing_warned = False
        self._last_snap: Any = {}                  # the snapshot the model was last built from (set_pot_counted rebuilds from it)
        self._odds: Dict[int, str] = {}           # program number -> the track's odds (set_odds)
        self._weather: Optional[Dict[str, Any]] = None   # pi5's weather, for the TV's crawl (set_weather)
        self._model: Dict[str, Any] = self._empty_model()
        self._json: str = _dumps(self._model)

    # -- settings ------------------------------------------------------------
    def _token_value(self) -> float:
        try:
            return float(self.settings.get("TOKEN_VALUE", DEFAULTS["TOKEN_VALUE"]))
        except (TypeError, ValueError, OverflowError):     # OverflowError: float() of a huge int
            return float(DEFAULTS["TOKEN_VALUE"])

    def _board_states(self) -> List[int]:
        try:
            return [int(s) for s in self.settings.get("QUINIELA_BOARD_STATES",
                                                       DEFAULTS["QUINIELA_BOARD_STATES"])]
        except (TypeError, ValueError):
            return list(DEFAULTS["QUINIELA_BOARD_STATES"])

    def _split(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for name, key in (("win", "LQ_SPLIT_WIN"), ("place", "LQ_SPLIT_PLACE"), ("show", "LQ_SPLIT_SHOW")):
            ok, value = _coerce_setting(key, self.settings.get(key, DEFAULTS[key]), "settings")
            out[name] = float(value) if ok else float(DEFAULTS[key])
        return out

    def _chyron(self) -> List[str]:
        ok, value = _coerce_setting("LQ_CHYRON_LINES",
                                    self.settings.get("LQ_CHYRON_LINES", DEFAULTS["LQ_CHYRON_LINES"]),
                                    "settings")
        return list(value) if ok else list(DEFAULTS["LQ_CHYRON_LINES"])

    def _race_tz(self) -> str:
        ok, value = _coerce_setting("LQ_RACE_TZ", self.settings.get("LQ_RACE_TZ", DEFAULTS["LQ_RACE_TZ"]),
                                    "settings")
        return value if ok else DEFAULTS["LQ_RACE_TZ"]

    def now(self) -> float:
        """The server's clock, as stamped into every serialised model."""
        return self._wall()

    # -- the race ------------------------------------------------------------
    def race_view(self) -> Dict[str, Any]:
        """The model's race: {"name", "year", "post_at", "post_local", "tz"}.
        name is the store's, upper-cased, DEFAULT_RACE_NAME while none is
        stored; year the stored one, else the post time's; post_at unix
        seconds or None; post_local the post time on the race's clock ("5:57
        PM CDT") or None; tz that clock's zone, for the pages that show the
        time of day or a date."""
        info = self.store.race()
        tz = self._race_tz()
        post_at = info.get("post_at")
        local = racetime.describe(post_at, tz) if post_at is not None else None
        year = info.get("year")
        if year is None and local is not None:
            year = local["year"]
        return {
            "name": (info.get("name") or DEFAULT_RACE_NAME).upper(),
            "year": year,
            "post_at": post_at,
            "post_local": local["label"] if local is not None else None,
            "tz": tz,
        }

    def race_form(self) -> Dict[str, Any]:
        """What the admin page's Race info form shows: {"race": race_view(),
        "name": the name as typed ("" for the default), "date": "2027-05-01"
        and "time": "17:57" on the race's clock, both None while no post time
        is set}."""
        race = self.race_view()
        local = racetime.describe(race["post_at"], race["tz"]) if race["post_at"] is not None else None
        return {"race": race, "name": self.store.race().get("name") or "",
                "date": local["date"] if local else None, "time": local["time"] if local else None}

    # -- the track's odds -------------------------------------------------------
    def set_odds(self, odds: Any) -> Dict[int, str]:
        """The track's odds by program number (clean_odds() decides what
        counts); None or {} clears them. A change wakes the board thread, so
        the model carries them within a second. Returns what was kept."""
        clean = clean_odds(odds)
        with self._lock:
            changed = clean != self._odds
            self._odds = clean
        if changed:
            self.wake()
        return dict(clean)

    def odds(self) -> Dict[int, str]:
        with self._lock:
            return dict(self._odds)

    # -- the weather --------------------------------------------------------------
    def set_weather(self, weather: Any) -> Optional[Dict[str, Any]]:
        """pi5's weather for the TV's crawl (clean_weather() decides what
        counts); None clears it. A change wakes the board thread. Returns
        what was kept."""
        clean = clean_weather(weather)
        with self._lock:
            changed = clean != self._weather
            self._weather = clean
        if changed:
            self.wake()
        return dict(clean) if clean is not None else None

    # -- model ---------------------------------------------------------------
    def _empty_model(self) -> Dict[str, Any]:
        horses = {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)}
        split = self._split()
        money = _money_view(self._closing, 0.0, prizes_for(0.0, split), split, 0)
        return {
            "link_ok": False,
            "race_state": 0,
            "race_state_name": race_state_name(0),
            "token_value": self._token_value(),
            "pot": money["pot"],
            "total_tokens": 0,
            "horses": self._name_horses(horses)[0],
            "leader": None,
            "events": [],
            "updated": self._wall(),
            "board_states": self._board_states(),
            "closes_at": self.store.closes_at,
            "prizes": money["prizes"],
            "split": split,
            "chyron": self._chyron(),
            "names_rev": self.store.names_rev,
            "scratches": [],
            "cups_online": 0,
            "cups_no_horse": 0,
            "results": _results_dict(None),
            "closing": money["closing"],
            "pot_scale": money["pot_scale"],
            "pot_counted": money["pot_counted"],
            "hand_counted": money["hand_counted"],
            "race": self.race_view(),
            "weather": self._weather,
        }

    def _name_horses(self, horses: Dict[str, Dict[str, Any]], state_scratched: Iterable[int] = ()
                     ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        """Add what the store knows to every horse entry and list the
        scratches. Mutates and returns horses.

        name: the store's name, upper-cased ("" when unset). in_field: the
        rule in horses.in_field(), from the store's records: 1..20 true
        unless scratched either kind, 21..24 true only while standing in for
        a scratched horse. replaced: the upper-cased name of the horse n
        stands in for (the "was" of the record whose "now" is n), else None.
        odds: the track's odds for program number n (set_odds()), else None.
        scratches: one entry per scratch ordered by was.number, {"was":
        {"number", "name"}, "now": {"number", "name"}} for a replacement
        record and {"was": {...}, "now": None} for a no-replacement scratch;
        names upper-cased, "" when unnamed.

        A no-replacement scratch is about the horse, not the cup: the record
        (now None) marks the horse scratched whether or not a cup claims it,
        so it is out of the field and its tokens (if any) out of the pot from
        the moment it is recorded; refresh() puts its bit in the gateway's
        state line. state_scratched is what the bridge currently sends (the
        same set, once refresh() has caught up): a horse in either reads
        scratched."""
        names = self.store.horses()
        records = self.store.scratches()
        for was, now in records.items():
            if now is None:
                horses[str(was)]["scratched"] = True
        for h in state_scratched:
            n = _as_int(h)
            if n is not None and 1 <= n <= HORSE_COUNT:
                horses[str(n)]["scratched"] = True
        gateway = {n for n in range(1, HORSE_COUNT + 1) if horses[str(n)]["scratched"]}
        by_now = {now: was for was, now in records.items() if now is not None}

        def named(n: int) -> Dict[str, Any]:
            return {"number": n, "name": ((names.get(n) or {}).get("name") or "").upper()}

        odds = self._odds
        for n in range(1, HORSE_COUNT + 1):
            entry = horses[str(n)]
            entry["name"] = named(n)["name"]
            entry["replaced"] = named(by_now[n])["name"] if n in by_now else None
            entry["in_field"] = in_field(n, records, gateway)
            entry["odds"] = odds.get(n)
        scratches: List[Dict[str, Any]] = []
        for was in sorted(set(records) | gateway):
            now = records.get(was)
            scratches.append({"was": named(was), "now": named(now) if now is not None else None})
        return horses, scratches

    def model(self) -> Dict[str, Any]:
        """A fresh copy of the current model (safe to mutate), stamped with
        "now", the server's clock."""
        with self._lock:
            text = self._json
        return json.loads(_with_now(text, self._wall()))

    def model_json(self) -> str:
        """The current model as the compact JSON the SSE stream sends,
        stamped with "now"."""
        with self._lock:
            text = self._json
        return _with_now(text, self._wall())

    def link_ok(self) -> bool:
        with self._lock:
            return bool(self._model["link_ok"])

    def _digest(self, snap: Any) -> Tuple[Dict[str, Dict[str, Any]], int, int, bool, Dict[str, Any]]:
        """Turn one bridge snapshot into (horses, race_state, total_tokens,
        link_ok, extras).

        Every horse 1..24 gets an entry. A cup claims a horse through the
        "horse" it reports; the claimers of a horse are ordered online first,
        most recently heard first, and the first one is the cup whose count
        the horse shows ("cup" is its MAC). Two online cups claiming the same
        horse is a conflict: "conflict" true and both MACs in "cups", with
        one WARNING per (horse, cups). A cup that has gone offline while
        another took its horse (a spare swapped in) is not a conflict.
        extras: cups_online (every online cup, horse or not), cups_no_horse
        (online cups reporting horse 0), the state's scratched list and
        results, and phase_known (the snapshot carried a phase at all: an
        empty one, a board with no bridge, reads as state 0 without saying
        so). Missing or odd fields never raise."""
        if not isinstance(snap, dict):
            snap = {}
        devpi = snap.get("devpi")
        if not isinstance(devpi, dict):
            devpi = {}
        link = snap.get("link")
        if not isinstance(link, dict):
            link = {}
        st = _as_int(devpi.get("phase"), 0)
        if st is None:
            st = 0
        link_ok = _as_bool(link.get("port_open")) and _as_bool(link.get("gateway_online"))
        cups = snap.get("cups")
        if not isinstance(cups, list):
            cups = []

        by_horse: Dict[int, List[Dict[str, Any]]] = {}
        cups_online = 0
        cups_no_horse = 0
        for entry in cups:
            if not isinstance(entry, dict):
                continue
            mac = entry.get("mac")
            if not isinstance(mac, str) or not mac:
                continue
            horse = _as_int(entry.get("horse"), 0) or 0
            online = _as_bool(entry.get("online"))
            if online:
                cups_online += 1
                if horse == 0:
                    cups_no_horse += 1
            if 1 <= horse <= HORSE_COUNT:
                by_horse.setdefault(horse, []).append(entry)

        horses = {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)}
        for horse, claimers in by_horse.items():
            claimers.sort(key=lambda e: str(e.get("last_seen") or ""), reverse=True)
            claimers.sort(key=lambda e: 0 if _as_bool(e.get("online")) else 1)
            online_claimers = [e for e in claimers if _as_bool(e.get("online"))]
            listed = online_claimers or claimers
            primary = listed[0]
            macs = [str(e.get("mac")) for e in listed]
            conflict = len(online_claimers) > 1
            if conflict:
                key = (horse, tuple(macs))
                if key not in self._dup_warned:
                    self._dup_warned.add(key)
                    log.warning("cups %s all claim horse %d; showing %s", ", ".join(macs), horse, macs[0])
            tokens = _as_int(primary.get("count"), 0) or 0     # None until the first telem line
            horses[str(horse)] = {
                "tokens": max(0, tokens),
                "share": 0.0,
                "scratched": False,
                "online": bool(online_claimers),
                "cup": str(primary.get("mac")),
                "conflict": conflict,
                "cups": macs,
            }

        total = sum(h["tokens"] for h in horses.values())
        if total:
            for h in horses.values():
                h["share"] = round(h["tokens"] / total, 4)
        extras = {
            "cups_online": cups_online,
            "cups_no_horse": cups_no_horse,
            "scratched": devpi.get("scratched") if isinstance(devpi.get("scratched"), list) else [],
            "results": devpi.get("results") if isinstance(devpi.get("results"), list) else [],
            "phase_known": _as_int(devpi.get("phase")) is not None,
        }
        return horses, st, total, link_ok, extras

    def apply_snapshot(self, snap: Any, baseline: bool = False) -> bool:
        """Digest one bridge snapshot. Returns True if the model changed (and
        was published to subscribers). Pure: no bridge, no port.

        Events are the per-horse token deltas between this snapshot and the
        last one. The first snapshot ever is a baseline, and so is any
        applied with baseline=True (the between-races reset): the events
        list is cleared and no count is diffed, so what sits in the cups is
        the starting point, not a bet. A horse whose cup changed (the MAC
        claiming it moved, or went away) gets no event for the count that
        came with the cup: a renumber (a replacement scratch, the cup that
        was 9 now reporting 22) is exactly such a move, so it yields no event
        and other horses' bets in the same snapshot still count. A real
        removal (a token lifted out of a cup, same cup) is still a negative
        event.

        The log tells the two apart from bets: a baseline is written as such
        even when nothing else moved, and a count that came with a cup move
        carries the move ("cup": [from, to], the MACs) in its change.

        The closing figures (the model's closing) are taken here, from this
        snapshot's pot, prizes and counts, the first time the race state is
        3, 4 or 5 with none held; after that no count moves them. The
        between-races reset (baseline=True) and a state of 0 or 1 drop them;
        2 and 6 leave them, and so does a snapshot with no phase. Taking or
        dropping them is saved (lq_closing) before the model is published,
        and logged as a change ({"closing": {pot, prizes, total_tokens}} or
        {"closing": null})."""
        now_w = self._wall()
        horses, race_state, total, link_ok, extras = self._digest(snap)
        self._last_snap = snap

        leader: Optional[int] = None
        best = 0
        for n in range(1, HORSE_COUNT + 1):
            tokens = horses[str(n)]["tokens"]
            if tokens > best:
                leader, best = n, tokens

        record: Optional[Dict[str, Any]] = None
        with self._lock:
            horses, scratches = self._name_horses(horses, extras["scratched"])

            old = self._model
            old_horses = old["horses"]

            # A fresh baseline: the first snapshot ever, or the between-races
            # reset. Neither produces events.
            fresh = baseline or not self._seen_state
            changes: List[Dict[str, Any]] = []
            new_events: List[Dict[str, Any]] = []
            for n in range(1, HORSE_COUNT + 1):
                key = str(n)
                before, after = old_horses[key], horses[key]
                if before["tokens"] != after["tokens"]:
                    change: Dict[str, Any] = {"horse": n, "tokens": [before["tokens"], after["tokens"]]}
                    # A bet or a removal is a count that changed on the SAME cup;
                    # a horse whose cup changed brings that cup's count with it,
                    # which is not a token moving. Outside a baseline record
                    # (flagged as a whole) the log entry says so, or a replay
                    # would read the jump as a bet. The one blind spot: a token
                    # that lands in a renumbered cup in the very snapshot that
                    # carries the renumber is counted (count, pot, log) but not
                    # tickered, because on that horse the count change is also
                    # a cup move.
                    moved = before["cup"] != after["cup"]
                    if moved and not fresh:
                        change["cup"] = [before["cup"], after["cup"]]
                    changes.append(change)
                    if not fresh and not moved:
                        new_events.append(
                            {"horse": n, "delta": after["tokens"] - before["tokens"], "ts": now_w}
                        )
                if before["scratched"] != after["scratched"]:
                    changes.append(
                        {"horse": n, "scratched": [before["scratched"], after["scratched"]]}
                    )
            if old["race_state"] != race_state:
                changes.append({"race_state": [old["race_state"], race_state]})
            reset = fresh and self._seen_state      # a reset, not the first snapshot
            self._seen_state = True
            if reset:
                self._events = []                   # no ghost removals on the ticker
            elif new_events:
                self._events = (new_events + self._events)[:MAX_EVENTS]

            token_value = self._token_value()
            # The pot is what the prizes are drawn from: a horse scratched
            # with no replacement is out of the game and its tokens are
            # handed back to be re-bet, so they leave the pot; total_tokens still
            # counts every cup. A replacement scratch keeps the cup counting
            # under its new number, so a renumber never changes the pot.
            live = sum(h["tokens"] for h in horses.values() if not h["scratched"])
            pot = round(live * token_value, 2)
            split = self._split()
            prizes = prizes_for(pot, split)

            # The figures at the post: taken once when betting closes, then
            # held through the race, the draw (the cups being emptied) and a
            # restart, until betting starts over.
            closing = self._closing
            if baseline:
                closing = None
            elif extras["phase_known"]:
                if race_state in REOPEN_STATES:
                    closing = None
                elif race_state in CLOSED_STATES and closing is None:
                    closing = _closing_figures(pot, prizes, total, horses, now_w)
            if closing != self._closing:
                changes.append({"closing": _closing_note(closing)})
                self._closing = closing
                self._save_closing(closing)

            # The money the model reports: the live scale figures, unless the
            # host has hand counted the cash box (the count is in the record
            # above), when the pot and every prize come from the count.
            money = _money_view(closing, pot, prizes, split, race_state)

            model = {
                "link_ok": bool(link_ok),
                "race_state": race_state,
                "race_state_name": race_state_name(race_state),
                "token_value": token_value,
                "pot": money["pot"],
                "total_tokens": total,
                "horses": horses,
                "leader": leader,
                "events": list(self._events),
                "updated": old["updated"],
                "board_states": self._board_states(),
                "closes_at": self.store.closes_at,
                "prizes": money["prizes"],
                "split": split,
                "chyron": self._chyron(),
                "names_rev": self.store.names_rev,
                "scratches": scratches,
                "cups_online": extras["cups_online"],
                "cups_no_horse": extras["cups_no_horse"],
                "results": _results_dict(extras["results"]),
                "closing": money["closing"],
                "pot_scale": money["pot_scale"],
                "pot_counted": money["pot_counted"],
                "hand_counted": money["hand_counted"],
                "race": self.race_view(),
                "weather": dict(self._weather) if self._weather is not None else None,
            }
            changed = model != old
            if changed:
                model["updated"] = now_w
                self._set_locked(model)
            if changes or reset or baseline:     # a reset leaves a trace even when nothing else moved
                record = {
                    "ts": round(now_w, 3),
                    "race_state": race_state,
                    "changes": changes,
                    "total_tokens": total,
                }
                if reset:
                    record["baseline"] = True       # counts moved by a reset, not by bets
                if baseline:
                    record["reset"] = "betting"     # the between-races reset
        if record is not None:
            self._write_log(record)     # outside the lock: it touches the SD card
        return changed

    def _save_closing(self, closing: Optional[Dict[str, Any]]) -> bool:
        """Persist the closing figures, and the hand count inside them (board
        lock held; a few times a race at most). A database error is logged
        once and costs only the copy on disk: the model carries the figures
        either way. True when they were saved."""
        try:
            self.store.set_closing(closing)
            return True
        except Exception as exc:
            if not self._closing_warned:
                self._closing_warned = True
                log.warning("La Quiniela board: cannot save the closing figures (%s); "
                            "they will not survive a restart", exc)
            return False

    def set_pot_counted(self, amount: Any) -> Dict[str, Any]:
        """The hand count: the host counted the cash box's BETS compartment
        and `amount` is the whole dollars. None clears it, which puts the
        scale figures back. Entering again overwrites.

        The count goes into the figures-at-the-post record (the "pot_counted"
        key of lq_closing's row), so it is saved with them, survives a
        restart, and is dropped by Reset betting and by a state of 0 or 1,
        never by 3, 4, 5 or 6. The model then carries it: pot and prizes (and
        closing's) from prizes_for(count), pot_scale, pot_counted and
        hand_counted. Bets per horse are the scales' and do not move.

        ValueError (a plain message) for an amount that is not a whole number
        of dollars from 0 to COUNTED_POT_MAX; CountRefused when the race is
        not in 3, 4 or 5 or there are no figures at the post yet. The model
        is rebuilt and published at once, so the TV does not wait for its
        next poll. Returns what PUT /api/quiniela/counted_pot reports: {
        "pot_counted", "pot_scale", "pot", "prizes", "hand_counted",
        "race_state", "saved"} ("saved": false when the database refused the
        write: the count is held, but it would not survive a restart)."""
        count = None if amount is None else counted_pot_arg(amount)
        with self._lock:
            state = int(self._model["race_state"])
            closing = self._closing
            if state not in CLOSED_STATES:
                raise CountRefused(
                    "The count can only be entered after betting closes (AT THE POST, RUNNING or WINNER); "
                    "the race is in %s%s." % (race_state_name(state).replace("_", " "),
                                              ": the saved count is read-only, Reset betting clears it"
                                              if state == int(P.Phase.AFTER_PARTY) and _stored_count(closing) is not None
                                              else ""))
            if closing is None:
                raise CountRefused("There are no figures at the post yet, so there is nothing to count against; "
                                   "they are taken when the race reaches AT THE POST.")
            before = _stored_count(closing)
            changed = count != before
            saved = True
            if changed:
                record = {key: value for key, value in closing.items() if key != "pot_counted"}
                if count is not None:
                    record["pot_counted"] = count
                self._closing = record
                saved = self._save_closing(record)
            total = sum(h["tokens"] for h in self._model["horses"].values())
            now_w = self._wall()
        if changed:
            self._write_log({"ts": round(now_w, 3), "race_state": state,
                             "changes": [{"pot_counted": [before, count]}], "total_tokens": total})
            # Rebuild from the snapshot the model was last built from (under
            # the refresh lock, so no older picture can overwrite a newer one
            # and invent a drop): the count is the only thing that changed.
            with self._refresh_lock:
                self.apply_snapshot(self._last_snap)
        model = self.model()
        return {"pot_counted": model["pot_counted"], "pot_scale": model["pot_scale"], "pot": model["pot"],
                "prizes": model["prizes"], "hand_counted": model["hand_counted"],
                "race_state": model["race_state"], "saved": saved}

    def refresh(self) -> bool:
        """Take the bridge's snapshot, apply it, and keep the gateway's state
        line in step with the store and the results file. Returns True if the
        model changed. With no bridge the empty picture is applied instead
        (link down, no cups), so names and the closing time from the store
        still reach the model; that is a change only if the store moved.

        Serialised: two overlapping calls (the board thread and a
        start_board() on a running board) apply their snapshots in the order
        they were taken, so an older picture can never overwrite a newer one
        and invent a negative drop."""
        bridge = self.bridge
        if bridge is None:
            with self._refresh_lock:
                return self.apply_snapshot({})
        # Lock order is refresh lock -> bridge RLock (get_snapshot) -> board
        # lock (apply), never the reverse: the bridge calls our listener
        # (wake(), an Event set) while holding its RLock, and Flask request
        # threads take only the board lock (model()) or only the bridge lock
        # (the cmd route), so no cycle exists. The snapshot is still taken
        # WITHOUT the board's own lock, so a request reading model() never
        # waits on the bridge.
        with self._refresh_lock:
            snap = bridge.get_snapshot()
            changed = self.apply_snapshot(snap)
            if self._sync_gateway(bridge, snap):
                changed = self.apply_snapshot(bridge.get_snapshot()) or changed
            return changed

    # -- what goes down to the cups --------------------------------------------
    def queue_undo_renum(self, to: int, was: int) -> None:
        """An undone replacement scratch: send [to, was] for UNDO_RENUM_S, or
        until a cup reports `was`, so the cup that became `to` goes back."""
        with self._lock:
            self._undo_renum[(int(to), int(was))] = self._clock() + UNDO_RENUM_S

    def undo_renums(self) -> List[Tuple[int, int]]:
        """The undo pairs still being sent, oldest first (tests)."""
        with self._lock:
            return sorted(self._undo_renum, key=self._undo_renum.get)

    def desired_state(self, snap: Any = None) -> Tuple[List[int], List[Tuple[int, int]], List[int]]:
        """(scratched, renum, results) as the gateway's state line should
        carry them: the store's no-replacement scratches; the replacement
        records as [was, now] pairs, then the undo pairs still pending
        (dropped when their time is up or when a cup reports the number they
        restore, or when a fresh record contradicts them); the dashboard's
        results. At most RENUM_SLOTS pairs: records first, one warning when
        an undo pair has to wait."""
        records = self.store.scratches()
        scratched = sorted(was for was, now in records.items() if now is None)
        pairs: List[Tuple[int, int]] = sorted((was, now) for was, now in records.items() if now is not None)
        reported = set()
        if isinstance(snap, dict):
            for entry in snap.get("cups") or []:
                if isinstance(entry, dict) and _as_bool(entry.get("online")):
                    h = _as_int(entry.get("horse"), 0) or 0
                    if h:
                        reported.add(h)
        now_mono = self._clock()
        with self._lock:
            for pair in list(self._undo_renum):
                to, was = pair
                # Contradicted by a record: the number it restores is scratched
                # again (f == was), a record moves cups onto the number it moves
                # them off (t == to: they would bounce), or a record has the
                # same from (the gateway refuses a from twice). The record wins.
                stale = (self._undo_renum[pair] <= now_mono or was in reported
                         or any(f == was or t == to or f == to for f, t in pairs))
                if stale:
                    del self._undo_renum[pair]
            undo = sorted(self._undo_renum, key=self._undo_renum.get)
        for pair in undo:
            if any(f == pair[0] for f, _ in pairs):
                continue
            pairs.append(pair)
        if len(pairs) > P.RENUM_SLOTS:
            waiting = pairs[P.RENUM_SLOTS:]
            key = tuple(waiting)
            if key not in self._sync_warned:
                self._sync_warned.add(key)
                log.warning("La Quiniela board: %d renumber pair(s) waiting for a free slot: %s",
                            len(waiting), waiting)
            pairs = pairs[:P.RENUM_SLOTS]
        return scratched, pairs, read_results(self._results_path)

    def _sync_gateway(self, bridge: Any, snap: Any) -> bool:
        """Push the desired scratched bits, renumber pairs and results to the
        bridge when its state line differs. Returns whether a set_state()
        went out; never raises (a refused state is logged once per value)."""
        scratched, pairs, results = self.desired_state(snap)
        devpi = snap.get("devpi") if isinstance(snap, dict) and isinstance(snap.get("devpi"), dict) else {}
        current = (list(devpi.get("scratched") or []),
                   [tuple(p) for p in (devpi.get("renum") or []) if isinstance(p, (list, tuple)) and len(p) == 2],
                   list(devpi.get("results") or []))
        if current == (scratched, pairs, results):
            return False
        try:
            bridge.set_state(scratched=scratched, renum=pairs, results=results)
        except ValueError as exc:
            key = ("refused", str(exc))
            if key not in self._sync_warned:
                self._sync_warned.add(key)
                log.warning("La Quiniela board: the bridge refused the state (%s): scratched %s, renum %s, results %s",
                            exc, scratched, pairs, results)
            return False
        return True

    # -- the one race state ------------------------------------------------------
    def set_race_state(self, state: Any, mode: Optional[str] = None, source: str = "cmd") -> Dict[str, Any]:
        """The one path every race-state change takes: a dashboard mode
        (set_mode()), the admin page's seven buttons and `state N` on POST
        /api/quiniela/cmd. The shared value is the bridge's phase (persisted,
        and carried by the rev the gateway acknowledges); `mode` is the
        dashboard mode that set it, None when it was set directly.

        One state line goes down with the new state and whatever else the
        line should carry at this moment (scratched bits, renumber pairs,
        the results), so a cup never shows WINNER a moment before it knows
        who won. Raises ValueError for a state outside 0..6 and RuntimeError
        without a bridge. Returns {"rev", "state", "state_name", "mode",
        "source", "gateway_online"}."""
        bridge = self.bridge
        if bridge is None:
            raise RuntimeError("bridge not initialised")
        phase = P.validate_phase(state)
        with self._refresh_lock:
            scratched, pairs, results = self.desired_state(bridge.get_snapshot())
            rev = bridge.set_state(phase=phase, scratched=scratched, renum=pairs, results=results)
            with self._lock:
                self._race_mode, self._race_source = mode, source
            snap = bridge.get_snapshot()
            self.apply_snapshot(snap)
        link = snap.get("link") if isinstance(snap.get("link"), dict) else {}
        return {"rev": rev, "state": phase, "state_name": race_state_name(phase), "mode": mode,
                "source": source, "gateway_online": bool(link.get("gateway_online", False))}

    def set_mode(self, mode: Any, source: str = "dashboard") -> Dict[str, Any]:
        """A dashboard mode: the race state it means (MODE_STATES), through
        set_race_state(). ValueError for a name that is not a mode."""
        key = str(mode).strip().upper() if isinstance(mode, str) else ""
        if key not in MODE_STATES:
            raise ValueError("unknown mode %r; one of %s" % (mode, " ".join(MODE_STATES)))
        return self.set_race_state(MODE_STATES[key], mode=key, source=source)

    def race_mode(self) -> Dict[str, Any]:
        """{"state", "state_name", "mode", "label", "source"}: the race state
        as the bridge holds it now, and the dashboard mode that set it. The
        mode is only named while it still explains the state: a state set
        directly since (the admin page, `state N`, a reset) has mode None."""
        bridge = self.bridge
        if bridge is not None:
            state = int(bridge.phase)
        else:
            with self._lock:
                state = int(self._model["race_state"])
        with self._lock:
            mode, source = self._race_mode, self._race_source
        if mode is not None and MODE_STATES.get(mode) != state:
            mode = None
        return {"state": state, "state_name": race_state_name(state), "mode": mode,
                "label": MODE_LABELS.get(mode) if mode else None, "source": source}

    def reset_betting(self) -> Dict[str, Any]:
        """The between-races reset: betting starts over. PRE_RACE, the
        results cleared (the dashboard's file too), the closing time and the
        closing figures cleared, and the bridge's picture applied as a fresh
        baseline: the events are cleared and no count is diffed, so what
        sits in a cup right now is the starting point, not a bet. Tokens
        still in a cup are not an error, the pot simply reads them, and the
        reply names those horses so the admin page can say so. Names and
        both kinds of scratch are not touched; the cups keep their numbers,
        which are theirs.

        Serialised with refresh() under the same lock, so the board thread
        (woken by set_state()) applies its snapshot after this one and finds
        nothing to do. Returns what POST /api/quiniela/reset reports."""
        bridge = self.bridge
        rev: Optional[int] = None
        with self._refresh_lock:
            clear_results(self._results_path)
            if bridge is not None:
                rev = bridge.set_state(phase=int(P.Phase.PRE_RACE), results=[0] * P.RESULT_SLOTS)
                with self._lock:
                    self._race_mode, self._race_source = None, "reset"
                snap: Any = bridge.get_snapshot()
            else:
                snap = {}
            self.store.clear_closes_at()
            self.apply_snapshot(snap, baseline=True)
        model = self.model()
        if not isinstance(snap, dict):
            snap = {}
        link = snap.get("link") if isinstance(snap.get("link"), dict) else {}
        with_tokens = [n for n in range(1, HORSE_COUNT + 1) if model["horses"][str(n)]["tokens"]]
        log.info("La Quiniela board: betting reset; pot %s on %d token(s), %d cup(s) online",
                 model["pot"], model["total_tokens"], model["cups_online"])
        return {
            "race_state": model["race_state"],
            "pot": model["pot"],
            "total_tokens": model["total_tokens"],
            "horses_with_tokens": with_tokens,
            "cups_online": model["cups_online"],
            "events": len(model["events"]),
            "closes_at": model["closes_at"],
            "rev": rev,
            "gateway_online": bool(link.get("gateway_online", False)),
            "names_rev": model["names_rev"],
        }

    def _set_locked(self, model: Dict[str, Any]) -> None:
        self._model = model
        self._json = _dumps(model)
        if self._subs:
            stamped = _with_now(self._json, self._wall())
            for q in self._subs:
                _offer(q, stamped)

    # -- subscribers ---------------------------------------------------------
    def subscribe(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=SSE_QUEUE_SIZE)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # -- the thread ----------------------------------------------------------
    def wake(self) -> None:
        """What the bridge listener calls: sets an Event and nothing else, so
        it is safe on the reader thread with the bridge lock held."""
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the refresh thread (idempotent). It refreshes on every wake()
        and at least once a second, so link_ok follows a gateway that has
        gone quiet even though the bridge emits nothing then."""
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lq-board", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(REFRESH_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.refresh()
            except Exception:       # a model bug must not end the thread
                log.exception("La Quiniela board: refresh failed")

    # -- event log -----------------------------------------------------------
    def _write_log(self, record: Dict[str, Any]) -> None:
        """One compact JSON line per model change. A write failure logs one
        WARNING and disables the log for the rest of the process."""
        if not self._log_enabled or not _as_bool(self.settings.get("QUINIELA_LOG", True)):
            return
        directory = Path(self._log_dir) if self._log_dir is not None else LOG_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"quiniela_{date.today().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_dumps(record) + "\n")
        except OSError as exc:
            self._log_enabled = False
            log.warning("event log disabled: cannot write under %s: %s", directory, exc)


def _offer(q: "queue.Queue[str]", item: str) -> None:
    """Non-blocking put; when the subscriber is not keeping up, drop its
    oldest item (every item is a full model, so the newest is all it needs)."""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

def validate_cmd(cmd: Any) -> Tuple[Optional[str], Optional[str]]:
    """Check one command line. Returns (clean_cmd, None) or (None, error).
    Only the first word is whitelisted; the route that translates the
    command onto the bridge checks the arguments. Since protocol v2 there is
    no horse, scratch or roster command: a cup's number is set on the cup,
    and scratches go through POST /api/quiniela/scratch."""
    if not isinstance(cmd, str):
        return None, "cmd must be a string"
    text = cmd.strip()
    if not text:
        return None, "empty command"
    if len(text) > CMD_MAX_LEN:
        return None, f"command longer than {CMD_MAX_LEN} characters"
    if "\n" in text or "\r" in text:
        return None, "command must be a single line"
    word = text.split()[0]
    if word not in CMD_WHITELIST:
        return None, f"command not allowed: {word} (allowed: {' '.join(sorted(CMD_WHITELIST))})"
    return text, None


# -----------------------------------------------------------------------------
# Server-Sent Events
# -----------------------------------------------------------------------------

def sse_events(
    board: BettingBoard,
    heartbeat_s: Optional[float] = None,
    wall: Callable[[], float] = time.time,
) -> Iterator[str]:
    """Generator behind GET /api/quiniela/stream.

    First yields the current model as a ``data:`` event, then every published
    model as it arrives. After heartbeat_s (default SSE_HEARTBEAT_S) of
    silence it yields a ``: heartbeat`` comment plus an ``event: ping`` (an
    EventSource cannot see comments, so the ping is what the page watches).
    """
    period = heartbeat_s if heartbeat_s is not None else SSE_HEARTBEAT_S
    q = board.subscribe()
    try:
        yield "data: " + board.model_json() + "\n\n"
        while True:
            try:
                payload = q.get(timeout=period)
            except queue.Empty:
                yield ": heartbeat\n\nevent: ping\ndata: " + _dumps({"ts": round(wall(), 3)}) + "\n\n"
                continue
            yield "data: " + payload + "\n\n"
    finally:
        board.unsubscribe(q)

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
# takes a snapshot from the bridge and applies it, and a daemon thread calls
# refresh() whenever the bridge's listener wakes it, and once a second
# regardless (so link_ok follows a gateway that has gone quiet).
#
# The model JSON, the SSE bytes and validate_cmd() are a contract with the
# board page (splash_display/static/js/quiniela_board.js) and must not
# change; the one documented difference from the old splash model is that
# horses[n].cup is the 1-based cup number (pi5's ID rule) rather than the
# gateway's 0-based slot. The board only tests it for null.
#
# How La Quiniela pays, which is what the additive keys carry: a token is one
# dollar and one raffle ticket. After the race one token is drawn from the WIN
# cup, one from PLACE, one from SHOW, and each drawn token's owner takes that
# cup's whole prize, a fixed fraction of the pot (prizes_for()). Nobody
# splits anything and there are no odds; the only number per horse is how
# many tokens are in its cup. Names, replacements and the closing time come
# from the HorseStore (horses.py), never from the gateway.

import json
import logging
import os
import queue
import threading
import time
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from la_quiniela import protocol as P
from la_quiniela.horses import HorseStore

log = logging.getLogger("la_quiniela.betting")

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent          # pi5/
LOG_DIR = BASE_DIR / "data"          # quiniela_YYYY-MM-DD.jsonl lives here (git-ignored)

HORSE_COUNT = P.MAX_HORSE            # 20
MAX_EVENTS = 8                       # the "recent drops" ring the board shows
SSE_HEARTBEAT_S = 5.0                # ping after this much silence
SSE_QUEUE_SIZE = 32                  # per-subscriber queue; the oldest is dropped when full
REFRESH_S = 1.0                      # the board thread's timeout between wake-ups

CMD_WHITELIST = frozenset({"state", "horse", "scratch", "demo", "roster", "json"})
CMD_MAX_LEN = 200

# Configuration keys, their defaults, and the environment override DDM_<key>.
# The names are the ones the splash display used, so its config carries over.
DEFAULTS: Dict[str, Any] = {
    "TOKEN_VALUE": 1.0,                       # dollars per token, for the POT
    "QUINIELA_LOG": True,                     # write pi5/data/quiniela_YYYY-MM-DD.jsonl
    "QUINIELA_BOARD_STATES": [1, 2, 3, 4],    # race states in which the board owns the TV
    "LQ_SPLIT_WIN": 0.60,                     # the WIN prize's share of the pot (it takes the remainder)
    "LQ_SPLIT_PLACE": 0.25,                   # the PLACE prize's share, rounded half up to whole dollars
    "LQ_SPLIT_SHOW": 0.15,                    # the SHOW prize's share, likewise
    "LQ_CHYRON_LINES": [                      # what crawls along the bottom of the board
        "TOTALS BASED ON CHEAP CHINESE ELECTRONICS \u00b7 FINAL RESULTS HAND COUNTED",
        "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS",
    ],
}
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
    a bool, DDM_QUINIELA_BOARD_STATES a comma list such as "1,2,3,4", the
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
# The model
# -----------------------------------------------------------------------------

def _unassigned() -> Dict[str, Any]:
    return {"tokens": 0, "share": 0.0, "scratched": False, "online": False, "cup": None,
            "name": "", "replaced": None}


def _roster_rev_of(snap: Any) -> Optional[int]:
    """devpi.roster_rev from a snapshot, or None when it is missing. The bridge
    bumps it in reset_link() and set_roster() (adopt_roster() included) and
    never in set_state(), so a change between two consecutive snapshots is
    exactly a reset-shaped transition: counts moved without a bet."""
    devpi = snap.get("devpi") if isinstance(snap, dict) else None
    return _as_int(devpi.get("roster_rev")) if isinstance(devpi, dict) else None


def _reset_count_of(snap: Any) -> Optional[int]:
    """devpi.reset_count: how many times this bridge has reset_link()'d. A
    move between two snapshots clears the closing time (names stay)."""
    devpi = snap.get("devpi") if isinstance(snap, dict) else None
    return _as_int(devpi.get("reset_count")) if isinstance(devpi, dict) else None


def _with_now(text: str, now: float) -> str:
    """The compact model JSON with the server's clock appended. The stored
    model never holds "now" (it would make every snapshot a change), so it is
    stamped here, at serialisation, onto the closing brace of a non-empty
    object."""
    return text[:-1] + ',"now":' + _dumps(now) + "}"


class BettingBoard:
    """The betting model, its event log, its SSE subscribers and the thread
    that keeps it current.

    apply_snapshot() digests one bridge snapshot (get_snapshot()); refresh()
    fetches one from the bridge and applies it. Both publish the full model
    to every subscriber queue when, and only when, the model changed.

    The board never talks to the serial port. It reads the bridge's picture
    and the bridge stays the source of truth for phase, horses and the roster.

    clock is a monotonic seconds source, kept for symmetry with the splash
    board (link_ok now comes from the snapshot, so nothing times out here);
    wall is unix time, for the timestamps in the model and the log. Both are
    injectable for tests, as is log_dir.

    store is the HorseStore of names, replacements and the closing time: by
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
    ) -> None:
        self.bridge = bridge
        self.settings: Dict[str, Any] = {k: (list(v) if isinstance(v, list) else v)
                                         for k, v in DEFAULTS.items()}
        self.settings.update(settings or {})
        self._clock = clock
        self._wall = wall
        self._log_dir = log_dir            # None = module-level LOG_DIR, read at write time
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()   # serialises refresh(): snapshot then apply, in order
        self._subs: List["queue.Queue[str]"] = []
        self._seen_state = False
        self._roster_rev: Optional[int] = None    # devpi.roster_rev of the last snapshot applied
        self._reset_count: Optional[int] = None   # devpi.reset_count of the last snapshot applied
        self._events: List[Dict[str, Any]] = []
        self._dup_warned: set = set()
        self._log_enabled = True
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.store: HorseStore = store if store is not None else HorseStore(getattr(bridge, "db", None))
        if self.store.on_change is None:
            self.store.on_change = self.wake
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

    def now(self) -> float:
        """The server's clock, as stamped into every serialised model."""
        return self._wall()

    # -- model ---------------------------------------------------------------
    def _empty_model(self) -> Dict[str, Any]:
        horses = {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)}
        split = self._split()
        return {
            "link_ok": False,
            "race_state": 0,
            "race_state_name": race_state_name(0),
            "token_value": self._token_value(),
            "pot": 0.0,
            "total_tokens": 0,
            "horses": self._name_horses(horses)[0],
            "leader": None,
            "events": [],
            "updated": self._wall(),
            "board_states": self._board_states(),
            "closes_at": self.store.closes_at,
            "prizes": prizes_for(0.0, split),
            "split": split,
            "chyron": self._chyron(),
            "names_rev": self.store.names_rev,
            "scratches": [],
        }

    def _name_horses(self, horses: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]],
                                                                      List[Dict[str, Any]]]:
        """Add the store's name / replaced (upper-cased) to every horse entry
        and list the replacement scratches. Mutates and returns horses."""
        scratches: List[Dict[str, Any]] = []
        names = self.store.horses()
        for n in range(1, HORSE_COUNT + 1):
            entry = names.get(n) or {}
            name = (entry.get("name") or "").upper()
            replaced = entry.get("replaced")
            replaced = replaced.upper() if isinstance(replaced, str) else None
            horses[str(n)]["name"] = name
            horses[str(n)]["replaced"] = replaced
            if replaced is not None:
                scratches.append({"horse": n, "was": replaced, "now": name})
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

    def _digest(self, snap: Any) -> Tuple[Dict[str, Dict[str, Any]], int, int, bool]:
        """Turn one bridge snapshot into (horses, race_state, total_tokens,
        link_ok).

        Every horse 1..20 gets an entry. A cup claims a horse through its
        "horse" field; two cups claiming the same horse keep the lowest cup
        number, with one WARNING per (horse, kept, dup). Missing or odd
        fields never raise.
        """
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

        entries: List[Tuple[int, int, Dict[str, Any]]] = []
        for entry in cups:
            if not isinstance(entry, dict):
                continue
            cup = _as_int(entry.get("cup"))
            horse = _as_int(entry.get("horse"))
            if cup is None or not (1 <= cup <= P.NUM_CUPS):
                continue
            if horse is None or not (1 <= horse <= HORSE_COUNT):
                continue
            entries.append((cup, horse, entry))
        entries.sort(key=lambda e: e[0])

        horses = {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)}
        claimed: Dict[int, int] = {}
        for cup, horse, entry in entries:
            if horse in claimed:
                key = (horse, claimed[horse], cup)
                if key not in self._dup_warned:
                    self._dup_warned.add(key)
                    log.warning(
                        "cups %d and %d both claim horse %d; keeping cup %d",
                        claimed[horse], cup, horse, claimed[horse],
                    )
                continue
            claimed[horse] = cup
            tokens = _as_int(entry.get("count"), 0) or 0     # None until the first telem line
            horses[str(horse)] = {
                "tokens": max(0, tokens),
                "share": 0.0,
                "scratched": _as_bool(entry.get("scratched")),
                "online": _as_bool(entry.get("online")),
                "cup": cup,
            }

        total = sum(h["tokens"] for h in horses.values())
        if total:
            for h in horses.values():
                h["share"] = round(h["tokens"] / total, 4)
        return horses, st, total, link_ok

    def apply_snapshot(self, snap: Any) -> bool:
        """Digest one bridge snapshot. Returns True if the model changed (and
        was published to subscribers). Pure: no bridge, no port.

        Events are the per-horse token deltas between this snapshot and the
        last one, except across a reset-shaped transition: when devpi.roster_rev
        moved (reset_link(), set_roster(), adopt_roster()) every count that
        changed did so because horses were forgotten or cups re-mapped, not
        because a token moved, so this snapshot is a fresh baseline and the
        events list is cleared rather than filled with ghost removals. Likewise
        a horse re-mapped to another cup (or unassigned) gets no event for the
        count that came with the cup. A real removal (a token lifted out of a
        cup, same cup, same roster) is still a negative event.

        The log tells the two apart from bets: a reset is written as a
        baseline record even when nothing else moved, and a count that came
        with a cup move carries the move ("cup": [from, to]) in its change."""
        now_w = self._wall()
        horses, race_state, total, link_ok = self._digest(snap)
        roster_rev = _roster_rev_of(snap)
        reset_count = _reset_count_of(snap)

        leader: Optional[int] = None
        best = 0
        for n in range(1, HORSE_COUNT + 1):
            tokens = horses[str(n)]["tokens"]
            if tokens > best:
                leader, best = n, tokens

        record: Optional[Dict[str, Any]] = None
        with self._lock:
            # A reset_link() since the last snapshot ends the betting window:
            # the closing time is cleared. Names survive a reset. The store's
            # lock is a leaf (its on_change only sets our wake Event), so this
            # is safe under our own lock.
            if (reset_count is not None and self._reset_count is not None
                    and reset_count != self._reset_count):
                self.store.clear_closes_at()
            if reset_count is not None:
                self._reset_count = reset_count
            horses, scratches = self._name_horses(horses)

            old = self._model
            old_horses = old["horses"]

            # A fresh baseline: the first snapshot ever, or the first after a
            # reset-shaped transition (roster_rev moved). Neither produces events.
            fresh = (not self._seen_state
                     or (roster_rev is not None and self._roster_rev is not None
                         and roster_rev != self._roster_rev))
            changes: List[Dict[str, Any]] = []
            new_events: List[Dict[str, Any]] = []
            for n in range(1, HORSE_COUNT + 1):
                key = str(n)
                before, after = old_horses[key], horses[key]
                if before["tokens"] != after["tokens"]:
                    change: Dict[str, Any] = {"horse": n, "tokens": [before["tokens"], after["tokens"]]}
                    # A bet or a removal is a count that changed on the SAME cup;
                    # a horse moved to another cup (or unassigned) brings that
                    # cup's count with it, which is not a token moving. Outside
                    # a baseline record (flagged as a whole) the log entry says
                    # so, or a replay would read the jump as a bet.
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
            if roster_rev is not None:
                self._roster_rev = roster_rev
            if reset:
                self._events = []                   # no ghost removals on the ticker
            elif new_events:
                self._events = (new_events + self._events)[:MAX_EVENTS]

            token_value = self._token_value()
            # The pot is what the prizes are drawn from: a cup scratched at
            # the gateway (no replacement) is out of the game and its tokens
            # are refunded by hand, so they leave the pot; total_tokens still
            # counts every cup. A replacement scratch keeps the cup counting.
            live = sum(h["tokens"] for h in horses.values() if not h["scratched"])
            pot = round(live * token_value, 2)
            split = self._split()
            model = {
                "link_ok": bool(link_ok),
                "race_state": race_state,
                "race_state_name": race_state_name(race_state),
                "token_value": token_value,
                "pot": pot,
                "total_tokens": total,
                "horses": horses,
                "leader": leader,
                "events": list(self._events),
                "updated": old["updated"],
                "board_states": self._board_states(),
                "closes_at": self.store.closes_at,
                "prizes": prizes_for(pot, split),
                "split": split,
                "chyron": self._chyron(),
                "names_rev": self.store.names_rev,
                "scratches": scratches,
            }
            changed = model != old
            if changed:
                model["updated"] = now_w
                self._set_locked(model)
            if changes or reset:     # a reset leaves a trace even when nothing else moved
                record = {
                    "ts": round(now_w, 3),
                    "race_state": race_state,
                    "changes": changes,
                    "total_tokens": total,
                }
                if reset:
                    record["baseline"] = True       # counts moved by a reset, not by bets
        if record is not None:
            self._write_log(record)     # outside the lock: it touches the SD card
        return changed

    def refresh(self) -> bool:
        """Take the bridge's snapshot and apply it. Returns True if the model
        changed. With no bridge the empty picture is applied instead (link
        down, no cups), so names and the closing time from the store still
        reach the model; that is a change only if the store moved.

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
            return self.apply_snapshot(snap)

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
    command onto the bridge checks the arguments."""
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

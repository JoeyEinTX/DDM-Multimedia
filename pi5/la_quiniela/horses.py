# la_quiniela/horses.py - Horse names, replacement scratches and the closing time
#
# What the betting board knows about a horse beyond its cup: a name typed by
# the operator, and the scratches. Horses are numbers 1..24: 1..20 are the
# field, 21..24 the also-eligibles, whose names can be entered ahead of time
# but who are not in the field until they replace someone. At Churchill an
# also-eligible that draws in keeps its own program number (in the 2026
# Derby The Puma, #9, scratched and Ocelli ran as #22, not as #9), so a
# replacement scratch is a RENUMBER: the record {was: 9, now: 22} says horse
# 9 left the field and the cup that was 9 is now 22 (its tokens come along,
# because it is the same cup; nothing moves on the mantle). Since protocol
# v2 the cup owns its number: the board turns the record into the renumber
# pair [9, 22] in the gateway's state line and the cup adopts it on its own.
# A no-replacement scratch is a record with now None; the board turns it
# into the horse's bit in the state line. The board's model derives
# in_field / replaced / scratches from the records and the names.
#
# For the board as a whole, when betting closes, and the figures as they were
# when it did (the board's closing figures, which only the board writes).
# And the race itself: its name, its year and its post time, the one store
# of race information (the TV's countdown and roster slides and /api/race
# read it through the board's model; Reset betting leaves it alone). None of
# it comes from the gateway, so none of it lives in the bridge; the board
# reads this store when it builds its model and the admin routes write it.
#
# Names are stored as typed. The board upper-cases them when it serves them.
# With a database the rows are lq_horses, lq_scratches, lq_board, lq_closing
# and lq_race (models.py); without one (tests) the store is memory only. One
# lock; the on_change callback is invoked after every write, outside the
# lock, so the board's wake() (an Event set) is a safe listener.

import json
import logging
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

from la_quiniela import protocol as P

log = logging.getLogger("la_quiniela.horses")

HORSE_COUNT = P.MAX_HORSE            # 24: every number a horse can have
FIELD_SIZE = 20                      # 1..20 start in the field; 21..24 are the also-eligibles
NAME_MAX_LEN = 80                    # a sanity cap for the tile; nothing on the board is longer
YEAR_RANGE = (1900, 2999)            # a race's year, if one is given
_KEEP = object()                     # set_race(): leave this field as it is

# "7. Name", "#7 Name", "7) Name", "7: Name", or a bare "7" (clears the name)
# always name horse 7. "7 Name" (a number, whitespace, then the name) does so
# only when every non-blank line in the text starts with a number, i.e. the
# text is a numbered list; in a plain post-order list a name that starts with
# a number ("8 Belles") is a name, not a prefix for horse 8. "7UP" is a name
# everywhere: a number is a prefix only when a separator, whitespace or the
# end of the line follows it.
_PREFIX = re.compile(r"^(?P<hash>#)?(?P<num>\d{1,2})(?:(?P<sep>\s*[.):]\s*)|(?P<space>\s+)|$)")


def _is_prefix(m: "re.Match[str]", numbered: bool) -> bool:
    """A "#7 X", "7. X", "7) X", "7: X" or a lone "7" is always a prefix; a
    bare "7 X" only in a numbered list (every non-blank line numbered)."""
    return m.group("hash") is not None or m.group("space") is None or numbered


def _clean_name(value: Any, what: str = "name") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    text = " ".join(value.split())          # strip, and fold inner runs of whitespace
    if len(text) > NAME_MAX_LEN:
        raise ValueError(f"{what} longer than {NAME_MAX_LEN} characters")
    return text


def _horse_number(value: Any, what: str = "horse") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number 1-{HORSE_COUNT}")
    try:
        horse = int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number 1-{HORSE_COUNT}") from None
    if not 1 <= horse <= HORSE_COUNT:
        raise ValueError(f"{what} {horse} is not in 1-{HORSE_COUNT}")
    return horse


def parse_names_text(text: Any) -> Dict[int, str]:
    """One name per line in post-position order (line 1 is horse 1, lines
    21..24 the also-eligibles). A leading "7." / "#7" / "7)" / "7:" names the
    horse instead and wins over the line's position, and so does "7 " when
    every non-blank line is numbered (a numbered list; in a plain list "8
    Belles" is a name); a blank line leaves that horse alone; a bare "7"
    clears horse 7's name. ValueError for an unprefixed line beyond the 24th,
    a prefix outside 1..24, or a horse named twice."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    lines = [(position, raw.strip()) for position, raw in enumerate(text.splitlines(), 1)]
    lines = [(position, line) for position, line in lines if line]
    matches = {position: _PREFIX.match(line) for position, line in lines}
    numbered = all(matches[position] is not None for position, _ in lines)
    names: Dict[int, str] = {}
    for position, line in lines:
        m = matches[position]
        if m is not None and _is_prefix(m, numbered):
            horse = int(m.group("num"))
            if not 1 <= horse <= HORSE_COUNT:
                raise ValueError(f"line {position}: horse {horse} is not in 1-{HORSE_COUNT}")
            name = line[m.end():]
        else:
            if position > HORSE_COUNT:
                raise ValueError(f"line {position}: more than {HORSE_COUNT} names")
            horse, name = position, line
        if horse in names:
            raise ValueError(f"line {position}: horse {horse} is named twice")
        names[horse] = _clean_name(name, f"line {position}")
    return names


def _parse_closing(text: Any) -> Optional[Dict[str, Any]]:
    """The stored closing figures, or None: nothing stored, or text that is
    not a JSON object (warned about, never raised: a bad row must not cost
    the names)."""
    if text is None:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        log.warning("La Quiniela horses: the stored closing figures are unreadable; ignored")
        return None
    return value


def in_field(horse: int, records: Dict[int, Optional[int]], gateway_scratched: Iterable[int] = ()) -> bool:
    """The one rule, pure: 1..20 are in the field unless scratched (either
    kind); 21..24 only while standing in for a scratched horse. The "now" of
    a replacement record is in the field; the "was" of any record (a
    replacement, or a no-replacement scratch recorded as was -> None), or a
    horse whose cup carries the gateway's scratched flag, is not, whatever
    its number."""
    if horse in records or horse in gateway_scratched:
        return False
    return horse <= FIELD_SIZE or horse in records.values()


def horse_at(post: int, records: Dict[int, Optional[int]]) -> Optional[int]:
    """The horse that runs from post `post`. A post is a place on the mantle,
    1..20, and the LED cup there: the post's own number, followed through the
    replacement records to the end of the chain (after 9 -> 22 it is 22 that
    runs from post 9; after 9 -> 22 -> 23 it is 23), because a replacement
    scratch renumbers the cup and nothing moves. None when the chain ends in
    a no-replacement scratch: nobody runs from that post."""
    horse, seen = int(post), set()
    while horse in records and horse not in seen:
        seen.add(horse)
        now = records[horse]
        if now is None:
            return None
        horse = now
    return horse


def post_of(horse: int, records: Dict[int, Optional[int]]) -> Optional[int]:
    """The post horse `horse` runs from: its own number for 1..20, and for a
    horse standing in for a scratched one the post of the horse it replaced
    (22 standing in for 9 runs from post 9, where its cup sits). None for an
    also-eligible standing in for nobody."""
    by_now = {now: was for was, now in records.items() if now is not None}
    horse, seen = int(horse), set()
    while horse in by_now and horse not in seen:
        seen.add(horse)
        horse = by_now[horse]
    return horse if 1 <= horse <= FIELD_SIZE else None


def _finite_time(value: Any, what: str) -> Optional[float]:
    """A unix time as a float, or None. ValueError for a bool, a non-number
    or an infinite / NaN value."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a unix time or null")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a unix time or null") from None
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"{what} must be a finite unix time")
    return out


class HorseStore:
    """Names for horses 1..24, the scratch records, closes_at, the board's
    closing figures and the race (name, year, post time).

    A record is was -> now: a replacement scratch (the cup that was `was`
    becomes `now`, through the renumber pair the board sends) or, with now
    None, a no-replacement scratch (the horse is out, its tokens refunded;
    its bit goes in the state line from BettingBoard.refresh()). Both kinds
    live in lq_scratches and survive a reset.

    db is an LqDb (models.py) or None for a memory-only store. on_change is a
    no-argument callable invoked after every write, outside the lock."""

    def __init__(self, db: Any = None, on_change: Optional[Callable[[], None]] = None) -> None:
        self._db = db
        self.on_change = on_change
        self._lock = threading.Lock()
        self._horses: Dict[int, Dict[str, str]] = {
            n: {"name": ""} for n in range(1, HORSE_COUNT + 1)}
        self._scratches: Dict[int, Optional[int]] = {}      # was -> now, or None: no replacement
        self._names_rev = 0
        self._closes_at: Optional[float] = None
        self._closing: Optional[Dict[str, Any]] = None
        self._race: Dict[str, Any] = {"name": "", "year": None, "post_at": None}
        self._race_migrated = False
        if db is not None:
            self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            for horse, row in self._db.load_horses().items():
                if 1 <= horse <= HORSE_COUNT:
                    self._horses[horse] = {"name": row.get("name") or ""}
                    if row.get("replaced") is not None:
                        # The name-swap replacement from before the renumber
                        # rule. There is no number to recover, so it is left
                        # alone (the column is written NULL on the next save).
                        log.warning("La Quiniela horses: legacy name-swap replacement on horse %d "
                                    "ignored; scratch it again with a number", horse)
            for was, now in self._db.load_scratches().items():
                if 1 <= was <= HORSE_COUNT and (now is None or (1 <= now <= HORSE_COUNT and was != now)):
                    self._scratches[was] = now
            board = self._db.load_board()
            self._names_rev = int(board.get("names_rev") or 0)
            closes = board.get("closes_at")
            self._closes_at = float(closes) if closes is not None else None
        except (sqlite3.Error, TypeError, ValueError) as exc:
            # A database without the tables (schema refused) must not take the
            # board down: carry on in memory, and say so once.
            log.error("La Quiniela horses: cannot read lq_horses / lq_scratches / lq_board (%s); "
                      "names will not persist", exc)
            self._db = None
            return
        try:
            self._closing = _parse_closing(self._db.load_closing())
        except sqlite3.Error as exc:
            # lq_closing is newer than the rest: without it only the closing
            # figures are lost (the board warns again if it cannot save them).
            log.error("La Quiniela horses: cannot read lq_closing (%s); "
                      "the closing figures will not survive a restart", exc)
        try:
            race = self._db.load_race()
            self._race = {"name": race["name"], "year": race["year"], "post_at": race["post_at"]}
            self._race_migrated = race["migrated"]
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            # lq_race is the newest table: without it the race info is kept
            # in memory only (a write says so again).
            log.error("La Quiniela horses: cannot read lq_race (%s); "
                      "the race name and post time will not survive a restart", exc)

    def _save_horse(self, horse: int) -> None:
        if self._db is not None:
            self._db.save_horse(horse, self._horses[horse]["name"] or "")

    def _save_scratch(self, was: int) -> None:
        if self._db is not None:
            if was in self._scratches:
                self._db.save_scratch(was, self._scratches[was])
            else:
                self._db.delete_scratch(was)

    def _save_board(self) -> None:
        if self._db is not None:
            self._db.save_board(self._names_rev, self._closes_at)

    def _save_race(self) -> None:
        if self._db is not None:
            try:
                self._db.save_race(self._race["name"], self._race["year"], self._race["post_at"],
                                   self._race_migrated)
            except sqlite3.Error as exc:
                log.error("La Quiniela horses: cannot save the race info (%s); "
                          "it is kept until pi5 restarts", exc)

    def _changed(self) -> None:
        fn = self.on_change
        if fn is not None:
            try:
                fn()
            except Exception:           # a listener bug must not fail the write
                log.exception("La Quiniela horses: on_change failed")

    # -- reads ----------------------------------------------------------------

    def horses(self) -> Dict[int, Dict[str, str]]:
        """{1..24: {"name": as typed}}, a copy."""
        with self._lock:
            return {n: dict(entry) for n, entry in self._horses.items()}

    def name_of(self, horse: int) -> str:
        with self._lock:
            return self._horses[horse]["name"]

    def scratches(self) -> Dict[int, Optional[int]]:
        """{was: now} for every record, a copy; now is None for a
        no-replacement scratch."""
        with self._lock:
            return dict(self._scratches)

    def replacement_of(self, was: int) -> Optional[int]:
        """The "now" of the replacement record whose "was" is this horse;
        None when there is no record or it is a no-replacement scratch."""
        with self._lock:
            return self._scratches.get(was)

    def record(self, was: int) -> Optional[Tuple[str, Optional[int]]]:
        """("replacement", now) or ("gateway", None) for the record whose
        "was" is this horse; None when it has none."""
        with self._lock:
            if was not in self._scratches:
                return None
            now = self._scratches[was]
            return ("gateway", None) if now is None else ("replacement", now)

    def gateway_scratches(self) -> Set[int]:
        """The horses scratched with no replacement (records with now None)."""
        with self._lock:
            return {was for was, now in self._scratches.items() if now is None}

    def replaced_by(self, now: int) -> Optional[int]:
        """The "was" of the record whose "now" is this horse, or None."""
        with self._lock:
            for was, n in self._scratches.items():
                if n == now:
                    return was
        return None

    def active_number(self, horse: int) -> int:
        """Follow the records: the number a cup assigned `horse` should carry
        (9 -> 22 after The Puma's scratch; 22 -> 23 if 22 was scratched in
        turn). A horse with no record is its own answer."""
        with self._lock:
            seen = set()
            while self._scratches.get(horse) is not None and horse not in seen:
                seen.add(horse)
                horse = self._scratches[horse]
            return horse

    def in_field(self, horse: int, gateway_scratched: Iterable[int] = ()) -> bool:
        with self._lock:
            return in_field(horse, self._scratches, gateway_scratched)

    def field(self) -> Dict[str, Any]:
        """The field as it stands, for La Subasta (la_subasta/field.py),
        which runs in this process and reads it here rather than over HTTP.
        One snapshot under one lock, so the names, the records and names_rev
        agree: {"names_rev": N, "horses": [...], "scratches": {was: now}}.
        horses: every horse in_field (the rule above), by program number,
        {"number": 22, "name": "OCELLI", "replaces": 9}: the name upper-cased
        as the board serves it ("" when none is stored), replaces the horse
        it stands in for or None. A replacement is listed under its own
        number and the horse it replaced is absent. scratches: every record,
        as scratches() gives them."""
        with self._lock:
            records = dict(self._scratches)
            by_now = {now: was for was, now in records.items() if now is not None}
            horses = [{"number": n, "name": self._horses[n]["name"].upper(), "replaces": by_now.get(n)}
                      for n in range(1, HORSE_COUNT + 1) if in_field(n, records)]
            return {"names_rev": self._names_rev, "horses": horses, "scratches": records}

    @property
    def names_rev(self) -> int:
        with self._lock:
            return self._names_rev

    @property
    def closes_at(self) -> Optional[float]:
        with self._lock:
            return self._closes_at

    @property
    def closing(self) -> Optional[Dict[str, Any]]:
        """The closing figures as last saved (a copy), or None."""
        with self._lock:
            return json.loads(json.dumps(self._closing)) if self._closing is not None else None

    def race(self) -> Dict[str, Any]:
        """{"name": as typed ("" while unset), "year": int or None,
        "post_at": unix time or None}, a copy."""
        with self._lock:
            return dict(self._race)

    @property
    def race_empty(self) -> bool:
        """Nothing about the race has been set: no name, no year, no post time."""
        with self._lock:
            return not self._race["name"] and self._race["year"] is None and self._race["post_at"] is None

    @property
    def race_migrated(self) -> bool:
        """The old Race Setup file has been looked at once (see board.py)."""
        with self._lock:
            return self._race_migrated

    # -- names ----------------------------------------------------------------

    def set_names(self, names: Dict[Any, Any]) -> bool:
        """Set the given horses' names (1..24). Only the horses given are
        touched. Strips. Bumps names_rev once if anything changed. Returns
        whether it did. Validates everything before it writes anything."""
        clean: Dict[int, str] = {}
        for key, value in (names or {}).items():
            horse = _horse_number(key)
            clean[horse] = _clean_name(value, f"horse {horse} name")
        with self._lock:
            touched = []
            for horse, name in clean.items():
                if self._horses[horse]["name"] != name:
                    self._horses[horse] = {"name": name}
                    touched.append(horse)
            if not touched:
                return False
            self._names_rev += 1
            for horse in touched:
                self._save_horse(horse)
            self._save_board()
        self._changed()
        return True

    # -- replacement scratches --------------------------------------------------

    def scratch_replace(self, was: Any, now: Any, name: Any = None) -> Dict[str, Any]:
        """Record that horse `was` left the field and horse `now` stands in
        for it (the cup's renumbering itself is the bridge's business; see
        board.py). A non-empty name is stored as now's name, else the stored
        one is kept. Bumps names_rev once. Returns {"was": {"number", "name"},
        "now": {"number", "name"}} with the names as typed.

        Only what the records alone can tell is checked here: was != now, was
        not already scratched, now not the was or now of another record. The
        route adds what needs the bridge (is `was` in the field, is `now`
        carried by a cup or in the field)."""
        was = _horse_number(was)
        now = _horse_number(now, "replacement number")
        new_name = _clean_name(name, "replacement name") if name is not None else ""
        if was == now:
            raise ValueError(f"{now} is in use")
        with self._lock:
            if self._scratches.get(was, 0) is None:
                raise ValueError(f"horse {was} is already scratched")
            if was in self._scratches:
                raise ValueError(f"horse {was} is not in the field")
            if now in self._scratches or now in self._scratches.values():
                raise ValueError(f"{now} is in use")
            self._scratches[was] = now
            if new_name and self._horses[now]["name"] != new_name:
                self._horses[now] = {"name": new_name}
                self._save_horse(now)
            self._names_rev += 1
            self._save_scratch(was)
            self._save_board()
            result = {"was": {"number": was, "name": self._horses[was]["name"]},
                      "now": {"number": now, "name": self._horses[now]["name"]}}
        self._changed()
        return result

    def unscratch_replace(self, was: Any) -> Optional[int]:
        """Remove the replacement record whose "was" is this horse. Returns
        its "now" (whose name stays stored), or None when there is no such
        record (a no-replacement record is left alone: see
        unscratch_gateway). Bumps names_rev once when it did something."""
        was = _horse_number(was)
        with self._lock:
            if self._scratches.get(was) is None:
                return None
            now = self._scratches.pop(was)
            self._names_rev += 1
            self._save_scratch(was)
            self._save_board()
        self._changed()
        return now

    # -- no-replacement scratches -----------------------------------------------

    def scratch_gateway(self, was: Any) -> Dict[str, Any]:
        """Record that horse `was` is scratched with no replacement: out of
        the field, tokens refunded. The cup carrying it, if any, gets the
        gateway's scratched flag from the caller (board.py) or from the
        board's refresh() when a cup is assigned later. Bumps names_rev once.
        Returns {"was": {"number", "name"}}. ValueError when the horse is
        already scratched either way."""
        was = _horse_number(was)
        with self._lock:
            if was in self._scratches:
                raise ValueError(f"horse {was} is already scratched")
            self._scratches[was] = None
            self._names_rev += 1
            self._save_scratch(was)
            self._save_board()
            result = {"was": {"number": was, "name": self._horses[was]["name"]}}
        self._changed()
        return result

    def unscratch_gateway(self, was: Any) -> bool:
        """Remove the no-replacement record of this horse. Returns whether
        there was one. Bumps names_rev once when it did something."""
        was = _horse_number(was)
        with self._lock:
            if was not in self._scratches or self._scratches[was] is not None:
                return False
            del self._scratches[was]
            self._names_rev += 1
            self._save_scratch(was)
            self._save_board()
        self._changed()
        return True

    # -- closing time ---------------------------------------------------------

    def set_closes_at(self, when: Optional[float]) -> Optional[float]:
        """Persisted; no names_rev bump. None clears."""
        if when is not None:
            if isinstance(when, bool):
                raise ValueError("closes_at must be a unix time or null")
            try:
                when = float(when)
            except (TypeError, ValueError):
                raise ValueError("closes_at must be a unix time or null") from None
            if when != when or when in (float("inf"), float("-inf")):
                raise ValueError("closes_at must be a finite unix time")
        with self._lock:
            if when == self._closes_at:
                return when
            self._closes_at = when
            self._save_board()
        self._changed()
        return when

    def clear_closes_at(self) -> None:
        self.set_closes_at(None)

    # -- the race -----------------------------------------------------------------

    def set_race(self, name: Any = _KEEP, year: Any = _KEEP, post_at: Any = _KEEP) -> Dict[str, Any]:
        """Set what is given of the race: its name (stored as typed, "" for
        the default), its year (an int, or None) and its post time (a unix
        time, or None to clear it). Validates everything before it writes
        anything; persisted; no names_rev bump. Returns race()."""
        clean: Dict[str, Any] = {}
        if name is not _KEEP:
            clean["name"] = _clean_name(name, "race name")
        if year is not _KEEP:
            if year is not None:
                if isinstance(year, bool):
                    raise ValueError("year must be a number")
                try:
                    year = int(str(year).strip(), 10)
                except (TypeError, ValueError):
                    raise ValueError("year must be a number") from None
                if not YEAR_RANGE[0] <= year <= YEAR_RANGE[1]:
                    raise ValueError(f"year {year} is not in {YEAR_RANGE[0]}-{YEAR_RANGE[1]}")
            clean["year"] = year
        if post_at is not _KEEP:
            clean["post_at"] = _finite_time(post_at, "post time")
        with self._lock:
            before = dict(self._race)
            self._race.update(clean)
            changed = self._race != before
            if changed:
                self._save_race()
            result = dict(self._race)
        if changed:
            self._changed()
        return result

    def mark_race_migrated(self) -> None:
        """Remember that the old Race Setup file has been looked at."""
        with self._lock:
            if self._race_migrated:
                return
            self._race_migrated = True
            self._save_race()

    # -- the closing figures ----------------------------------------------------

    def set_closing(self, closing: Optional[Dict[str, Any]]) -> None:
        """Persist the board's closing figures; None clears them. The board
        is their only writer and publishes them itself, so no on_change. A
        database error is the caller's to handle (the figures are kept in
        memory either way)."""
        text = json.dumps(closing, separators=(",", ":")) if closing is not None else None
        with self._lock:
            self._closing = json.loads(text) if text is not None else None
            if self._db is not None:
                self._db.save_closing(text)

# la_quiniela/horses.py - Horse names, replacement scratches and the closing time
#
# What the betting board knows about a horse beyond its cup: a name typed by
# the operator, and the replacement scratches. Horses are numbers 1..24:
# 1..20 are the field, 21..24 the also-eligibles, whose names can be entered
# ahead of time but who are not in the field until they replace someone. At
# Churchill an also-eligible that draws in keeps its own program number (in
# the 2026 Derby The Puma, #9, scratched and Ocelli ran as #22, not as #9),
# so a replacement scratch is a RENUMBER: the record {was: 9, now: 22} says
# horse 9 left the field and the cup that was 9 now carries 22 (its tokens
# come along, because it is the same cup; nothing moves on the mantle). The
# cup's number itself is the bridge's state; this store keeps the record and
# the names, and the board's model derives in_field / replaced / scratches
# from the two together. A no-replacement scratch (the gateway's scratched
# flag on the cup) is bridge state and is never stored here.
#
# For the board as a whole, when betting closes. None of it comes from the
# gateway, so none of it lives in the bridge; the board reads this store when
# it builds its model and the admin routes write it.
#
# Names are stored as typed. The board upper-cases them when it serves them.
# With a database the rows are lq_horses, lq_scratches and lq_board
# (models.py); without one (tests) the store is memory only. One lock; the
# on_change callback is invoked after every write, outside the lock, so the
# board's wake() (an Event set) is a safe listener.

import logging
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, Iterable, Optional

from la_quiniela import protocol as P

log = logging.getLogger("la_quiniela.horses")

HORSE_COUNT = P.MAX_HORSE            # 24: every number a horse can have
FIELD_SIZE = 20                      # 1..20 start in the field; 21..24 are the also-eligibles
NAME_MAX_LEN = 80                    # a sanity cap for the tile; nothing on the board is longer

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


def in_field(horse: int, records: Dict[int, int], gateway_scratched: Iterable[int] = ()) -> bool:
    """The one rule, pure: 1..20 are in the field unless scratched (either
    kind); 21..24 only while standing in for a scratched horse. The "now" of
    a replacement record is in the field; the "was" of one, or a horse whose
    cup carries the gateway's scratched flag, is not, whatever its number."""
    if horse in records or horse in gateway_scratched:
        return False
    return horse <= FIELD_SIZE or horse in records.values()


class HorseStore:
    """Names for horses 1..24, the replacement records and closes_at.

    db is an LqDb (models.py) or None for a memory-only store. on_change is a
    no-argument callable invoked after every write, outside the lock."""

    def __init__(self, db: Any = None, on_change: Optional[Callable[[], None]] = None) -> None:
        self._db = db
        self.on_change = on_change
        self._lock = threading.Lock()
        self._horses: Dict[int, Dict[str, str]] = {
            n: {"name": ""} for n in range(1, HORSE_COUNT + 1)}
        self._scratches: Dict[int, int] = {}      # was -> now
        self._names_rev = 0
        self._closes_at: Optional[float] = None
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
                if 1 <= was <= HORSE_COUNT and 1 <= now <= HORSE_COUNT and was != now:
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

    def scratches(self) -> Dict[int, int]:
        """{was: now} for every replacement record, a copy."""
        with self._lock:
            return dict(self._scratches)

    def replacement_of(self, was: int) -> Optional[int]:
        """The "now" of the record whose "was" is this horse, or None."""
        with self._lock:
            return self._scratches.get(was)

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
            while horse in self._scratches and horse not in seen:
                seen.add(horse)
                horse = self._scratches[horse]
            return horse

    def in_field(self, horse: int, gateway_scratched: Iterable[int] = ()) -> bool:
        with self._lock:
            return in_field(horse, self._scratches, gateway_scratched)

    @property
    def names_rev(self) -> int:
        with self._lock:
            return self._names_rev

    @property
    def closes_at(self) -> Optional[float]:
        with self._lock:
            return self._closes_at

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
        """Remove the record whose "was" is this horse. Returns its "now"
        (whose name stays stored), or None when there is no such record.
        Bumps names_rev once when it did something."""
        was = _horse_number(was)
        with self._lock:
            now = self._scratches.pop(was, None)
            if now is None:
                return None
            self._names_rev += 1
            self._save_scratch(was)
            self._save_board()
        self._changed()
        return now

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

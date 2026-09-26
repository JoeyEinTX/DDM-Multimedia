# la_quiniela/horses.py - Horse names, replacements and the closing time
#
# What the betting board knows about a horse beyond its cup: a name typed by
# the operator, the name of the scratched horse this number now stands in for
# (a "replacement" scratch keeps the number live and the cup counting), and,
# for the board as a whole, when betting closes. None of it comes from the
# gateway, so none of it lives in the bridge; the board reads this store when
# it builds its model and the admin routes write it.
#
# Names are stored as typed. The board upper-cases them when it serves them.
# With a database the rows are lq_horses and lq_board (models.py); without
# one (tests) the store is memory only. One lock; the on_change callback is
# invoked after every write, outside the lock, so the board's wake() (an
# Event set) is a safe listener.

import logging
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, Optional

from la_quiniela import protocol as P

log = logging.getLogger("la_quiniela.horses")

HORSE_COUNT = P.MAX_HORSE            # 20
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


def _horse_number(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("horse must be a number 1-20")
    try:
        horse = int(str(value).strip(), 10)
    except (TypeError, ValueError):
        raise ValueError("horse must be a number 1-20") from None
    if not 1 <= horse <= HORSE_COUNT:
        raise ValueError(f"horse {horse} is not in 1-{HORSE_COUNT}")
    return horse


def parse_names_text(text: Any) -> Dict[int, str]:
    """One name per line in post-position order (line 1 is horse 1). A leading
    "7." / "#7" / "7)" / "7:" names the horse instead and wins over the line's
    position, and so does "7 " when every non-blank line is numbered (a
    numbered list; in a plain list "8 Belles" is a name); a blank line leaves
    that horse alone; a bare "7" clears horse 7's name. ValueError for an
    unprefixed line beyond the 20th, a prefix outside 1..20, or a horse named
    twice."""
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


class HorseStore:
    """Names, replacements and closes_at for horses 1..20.

    db is an LqDb (models.py) or None for a memory-only store. on_change is a
    no-argument callable invoked after every write, outside the lock."""

    def __init__(self, db: Any = None, on_change: Optional[Callable[[], None]] = None) -> None:
        self._db = db
        self.on_change = on_change
        self._lock = threading.Lock()
        self._horses: Dict[int, Dict[str, Optional[str]]] = {
            n: {"name": "", "replaced": None} for n in range(1, HORSE_COUNT + 1)}
        self._names_rev = 0
        self._closes_at: Optional[float] = None
        if db is not None:
            self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            for horse, row in self._db.load_horses().items():
                if 1 <= horse <= HORSE_COUNT:
                    self._horses[horse] = {"name": row.get("name") or "",
                                           "replaced": row.get("replaced")}
            board = self._db.load_board()
            self._names_rev = int(board.get("names_rev") or 0)
            closes = board.get("closes_at")
            self._closes_at = float(closes) if closes is not None else None
        except (sqlite3.Error, TypeError, ValueError) as exc:
            # A database without the tables (schema refused) must not take the
            # board down: carry on in memory, and say so once.
            log.error("La Quiniela horses: cannot read lq_horses / lq_board (%s); "
                      "names will not persist", exc)
            self._db = None

    def _save_horse(self, horse: int) -> None:
        if self._db is not None:
            entry = self._horses[horse]
            self._db.save_horse(horse, entry["name"] or "", entry["replaced"])

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

    def horses(self) -> Dict[int, Dict[str, Optional[str]]]:
        """{1..20: {"name": as typed, "replaced": as typed or None}}, a copy."""
        with self._lock:
            return {n: dict(entry) for n, entry in self._horses.items()}

    @property
    def names_rev(self) -> int:
        with self._lock:
            return self._names_rev

    @property
    def closes_at(self) -> Optional[float]:
        with self._lock:
            return self._closes_at

    # -- names ----------------------------------------------------------------

    def set_names(self, names: Dict[Any, Any],
                  replaced: Optional[Dict[Any, Any]] = None) -> bool:
        """Set the given horses' names (and, for the horses in replaced, the
        name they stand in for; None clears it). Only the horses given are
        touched. Strips. Bumps names_rev once if anything changed. Returns
        whether it did. Validates everything before it writes anything."""
        clean: Dict[int, Dict[str, Optional[str]]] = {}
        for key, value in (names or {}).items():
            horse = _horse_number(key)
            clean.setdefault(horse, {})["name"] = _clean_name(value, f"horse {horse} name")
        for key, value in (replaced or {}).items():
            horse = _horse_number(key)
            text = _clean_name(value, f"horse {horse} replaced") if value is not None else None
            clean.setdefault(horse, {})["replaced"] = text or None
        with self._lock:
            touched = []
            for horse, fields in clean.items():
                entry = self._horses[horse]
                new = dict(entry, **fields)
                if new != entry:
                    self._horses[horse] = new
                    touched.append(horse)
            if not touched:
                return False
            self._names_rev += 1
            for horse in touched:
                self._save_horse(horse)
            self._save_board()
        self._changed()
        return True

    def scratch_replace(self, horse: Any, replacement: Any) -> Dict[str, Optional[str]]:
        """Scratch with a replacement: the number stays live, the old name
        becomes replaced and the new one name. A second replacement of the
        same number keeps the horse that was actually scratched in replaced
        (the first replacement never ran, so it is simply dropped) and undo
        goes straight back to it. Returns {"was", "now"}: was is the scratched
        horse's name as typed, "" when it had none (that "" is what marks the
        horse as replaced; None means no replacement)."""
        horse = _horse_number(horse)
        name = _clean_name(replacement, "replacement")
        if not name:
            raise ValueError("replacement must not be empty")
        with self._lock:
            entry = self._horses[horse]
            was = entry["replaced"] if entry["replaced"] is not None else (entry["name"] or "")
            self._horses[horse] = {"name": name, "replaced": was}
            self._names_rev += 1
            self._save_horse(horse)
            self._save_board()
        self._changed()
        return {"was": was, "now": name}

    def unscratch_replace(self, horse: Any) -> bool:
        """Undo a replacement: the scratched horse's name comes back. False
        when the horse has no replacement."""
        horse = _horse_number(horse)
        with self._lock:
            entry = self._horses[horse]
            if entry["replaced"] is None:
                return False
            self._horses[horse] = {"name": entry["replaced"], "replaced": None}
            self._names_rev += 1
            self._save_horse(horse)
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

# la_subasta/field.py - The horses La Subasta sells: La Quiniela's field
#
# La Subasta owns no horse data. Its horses are La Quiniela's, read in this
# process through HorseStore.field() (never over HTTP): the names typed on
# the LQ admin page (lq_horses) and the field as the betting board has it
# (in_field: 1..20 unless scratched, 21..24 only while standing in for a
# scratched horse). A horse is its program number, 1..24, and that number is
# La Subasta's horse_id in bids, ownership and payouts. A replacement is sold
# under its own number (2026: #22 Ocelli ran for #9 The Puma, so the list
# shows 22 and no 9). Names are upper-cased as the board shows them; a horse
# with no name yet is HORSE n.
#
# Without a La Quiniela board (an app that never called init_board()) the
# field is what an empty store gives, 1..20 with no names, and the log says
# so once. main.py always makes the board.
#
# A store that could not read its database at start (HorseStore.load_failed)
# is the same empty store, and says so: Field.degraded. The list is still
# served, but it is a default and not the operator's field, so scratches.py
# does not apply it to the auction.

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from la_subasta.config import MAX_HORSE

log = logging.getLogger(__name__)

# Saddle-cloth colours by program number, (cloth, number), copied from the LQ
# board's SADDLE table (splash_display/static/js/quiniela_board.js): 1-20 the
# Kentucky Derby's, 21-24 the placeholders the board and the cups show for
# the also-eligibles.
SADDLE_CLOTHS: Dict[int, Tuple[str, str]] = {
    1: ("#E31837", "#FFFFFF"), 2: ("#FFFFFF", "#000000"), 3: ("#0033A0", "#FFFFFF"),
    4: ("#FFCD00", "#000000"), 5: ("#00843D", "#FFFFFF"), 6: ("#000000", "#FFD700"),
    7: ("#FF6600", "#000000"), 8: ("#FF69B4", "#000000"), 9: ("#40E0D0", "#000000"),
    10: ("#663399", "#FFFFFF"), 11: ("#808080", "#E31837"), 12: ("#32CD32", "#000000"),
    13: ("#8B4513", "#FFFFFF"), 14: ("#800000", "#FFCD00"), 15: ("#C4B7A6", "#000000"),
    16: ("#87CEEB", "#E31837"), 17: ("#000080", "#FFFFFF"), 18: ("#228B22", "#FFCD00"),
    19: ("#00008B", "#E31837"), 20: ("#FF00FF", "#FFCD00"),
    21: ("#FFDAB9", "#000000"), 22: ("#008080", "#FFFFFF"), 23: ("#808000", "#FFFFFF"),
    24: ("#2F4F4F", "#FFFFFF"),
}
SADDLE_FALLBACK: Tuple[str, str] = ("#808080", "#FFFFFF")

# Where the store comes from: None for La Quiniela's board (the app), or a
# no-argument callable returning a HorseStore (tests).
_source: Optional[Callable[[], Any]] = None
_standin: Any = None
_warned = False


def set_store_source(fn: Optional[Callable[[], Any]]) -> None:
    """Read the field from fn() instead of La Quiniela's board; None goes
    back to the board."""
    global _source
    _source = fn


def lq_store() -> Any:
    """La Quiniela's HorseStore, or None when there is no board."""
    if _source is not None:
        return _source()
    try:
        from la_quiniela.board import get_board
        return get_board().store
    except RuntimeError:
        return None


def display_name(number: int, name: str) -> str:
    """The name as the board shows it, or HORSE n when none is stored."""
    return name or f"HORSE {number}"


def _horse(entry: Dict[str, Any]) -> Dict[str, Any]:
    number = int(entry["number"])
    cloth, digits = SADDLE_CLOTHS.get(number, SADDLE_FALLBACK)
    return {
        "horse_id": number,
        "saddle_cloth": number,
        "name": display_name(number, entry.get("name") or ""),
        "replaces": entry.get("replaces"),
        "saddle_cloth_color": cloth,
        "saddle_cloth_text_color": digits,
    }


class Field:
    """One read of the field: the horses in it by program number, and why a
    number is not one of them."""

    def __init__(self, snapshot: Dict[str, Any], live: bool) -> None:
        self.live = live                        # False: the stand-in, no board
        self.degraded = bool(snapshot.get("degraded"))   # the store could not read its database
        self.names_rev: Optional[int] = snapshot.get("names_rev") if live else None
        self.scratches: Dict[int, Optional[int]] = dict(snapshot.get("scratches") or {})
        self.horses: List[Dict[str, Any]] = [_horse(h) for h in snapshot.get("horses") or []]
        self._by_number = {h["horse_id"]: h for h in self.horses}

    def numbers(self) -> List[int]:
        return [h["horse_id"] for h in self.horses]

    def __contains__(self, number: Any) -> bool:
        return number in self._by_number

    def __len__(self) -> int:
        return len(self.horses)

    def get(self, number: int) -> Optional[Dict[str, Any]]:
        """A copy of the horse's entry, or None when it is not in the field."""
        entry = self._by_number.get(number)
        return dict(entry) if entry is not None else None

    def refusal(self, number: int) -> str:
        """What a guest is told when a bid names a horse that is not in it.
        A replacement can be scratched in turn (9 -> 22, then 22 -> 23): the
        horse that runs in its place is the end of that chain, here 23."""
        if number in self.scratches:
            from la_quiniela.horses import horse_at
            runs = horse_at(number, self.scratches)
            if runs is not None:
                return f"#{number} is not in the field: scratched, #{runs} runs in its place"
            return f"#{number} is not in the field: scratched"
        return f"#{number} is not in the field"


def current() -> Field:
    """The field as it stands now."""
    global _standin, _warned
    store = lq_store()
    if store is not None:
        return Field(store.field(), live=True)
    if not _warned:
        _warned = True
        log.warning("La Subasta: no La Quiniela board; selling 1-20 with no names until there is one")
    if _standin is None:
        from la_quiniela.horses import HorseStore
        _standin = HorseStore()
    return Field(_standin.field(), live=False)


def valid_number(number: Any) -> bool:
    """A program number La Subasta can know about at all: 1..MAX_HORSE."""
    return isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= MAX_HORSE

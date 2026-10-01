# la_subasta/scratches.py - A scratch recorded on the LQ admin page, applied to the auction
#
# A scratch is entered once, on La Quiniela's admin page, and lives in La
# Quiniela's store (lq_scratches). La Subasta has no scratch button and no
# scratch table of its own: a horse is scratched for the auction when it is
# not in La Quiniela's field (field.py). What the auction then does is its own
# rule, "bid refunded, horse removed" (DDM_La_Subasta_Spec.md, Scratches):
#
#   - Auction open or in its final hour: the horse leaves the list and every
#     bid on it is voided (voided = 1, voided_reason = 'scratched'), so nobody
#     is charged for it.
#   - Auction locked or after: the ownership row is voided the same way (the
#     row stays, as the record of what was refunded), and so are the bids, so
#     the owner's total owed drops by the winning bid, the horse is out of the
#     pot and it cannot pay out. If the owner had already been marked paid,
#     the bidders' ledger shows the refund owed (bidding.ledger()). The House
#     rule is for unsold horses, never for a scratched one.
#   - A replacement scratch (9 -> 22) does both halves at once: 9 as above,
#     and 22 enters the list as a fresh horse with no bids.
#   - Undo on the LQ admin page puts the field back; it does not un-void
#     bids (9 comes back with none). Undoing a replacement takes the stand-in
#     out of the field again, and its bids are voided the same way.
#
# When: on change, never on a timer. sync() runs from the store's change
# listener (HorseStore.add_listener: every names / scratch write, on the
# writer's thread), once when pi5 starts (follow_la_quiniela() from main.py,
# after the board exists), and before a La Subasta request if names_rev has
# moved since the last sync (a listener that failed is caught up there).
# It is idempotent: it voids only what is still active on a horse that is
# not in the field, so a restart, a second listener call or a request finds
# nothing left to do and voids nothing twice.
#
# Live: each horse that left the field is announced with horse_scratched and
# the new field with field_changed (SocketIO), and the guest page re-reads
# its list, so the horse disappears without a reload. A field counts as seen
# only once its pushes have gone out: if one raises after apply() committed,
# the next sync() finds the same horses gone, voids nothing a second time
# and pushes again (a retried horse_scratched says refund_count 0: the first
# pass voided the bids).
#
# Degraded: a store that could not read its database at start (HorseStore.
# load_failed) comes up empty, so its field is the default, 1-20 with no
# scratch records, and a replacement standing in (22) would look scratched.
# sync() does not apply that: nothing is voided, nothing is pushed, the log
# says so once at ERROR, and the sync stays undone (_seen unset) so a store
# that does load is applied normally. field.current() still serves the list.

import logging
import threading
from typing import Any, Dict, Optional, Set, Tuple

from la_subasta import field, notifications
from la_subasta.config import EVENT_YEAR
from la_subasta.models import write_txn

log = logging.getLogger(__name__)

SCRATCHED = "scratched"         # voided_reason, on bids and on ownership rows

_lock = threading.Lock()
_store: Any = None              # the store we listen to (one listener per store)
_seen: Optional[Tuple[Any, int]] = None   # (store, names_rev) at the last sync
_signature: Optional[Tuple[Tuple[int, str], ...]] = None   # the field then: (number, name), by number
_degraded_logged: Any = None    # the degraded store already reported (once, not on every request)


def apply(the_field: "field.Field", event_year: int = EVENT_YEAR) -> Dict[int, Dict[str, Any]]:
    """Void whatever is still active on a horse that is not in the field:
    its bids, and its ownership row once the auction has locked. Returns
    {horse: {"bids": how many voided, "owner": {"bidder_id", "winning_bid"}
    or None}} for the horses it touched; {} when there was nothing to do.
    One transaction."""
    numbers = set(the_field.numbers())
    out: Dict[int, Dict[str, Any]] = {}
    with write_txn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT horse_id FROM bids WHERE voided = 0 AND event_year = ?",
            (event_year,),
        ).fetchall()
        for horse in sorted(r["horse_id"] for r in rows if r["horse_id"] not in numbers):
            cur = conn.execute(
                "UPDATE bids SET voided = 1, voided_reason = ? "
                "WHERE horse_id = ? AND voided = 0 AND event_year = ?",
                (SCRATCHED, horse, event_year),
            )
            out.setdefault(horse, {"bids": 0, "owner": None})["bids"] = cur.rowcount or 0
        rows = conn.execute(
            "SELECT id, horse_id, bidder_id, winning_bid FROM ownership "
            "WHERE voided = 0 AND event_year = ?",
            (event_year,),
        ).fetchall()
        for row in rows:
            if row["horse_id"] in numbers:
                continue
            conn.execute(
                "UPDATE ownership SET voided = 1, voided_reason = ?, voided_at = datetime('now') "
                "WHERE id = ?",
                (SCRATCHED, row["id"]),
            )
            out.setdefault(row["horse_id"], {"bids": 0, "owner": None})["owner"] = {
                "bidder_id": row["bidder_id"], "winning_bid": row["winning_bid"]}
    if out:
        late = _results_name(set(out), event_year)
        if late:
            log.warning("La Subasta: horse(s) %s scratched after the results were entered; "
                        "their payouts were left as they are", sorted(late))
    return out


def _results_name(horses: Set[int], event_year: int) -> Set[int]:
    from la_subasta.models import get_conn
    rows = get_conn().execute(
        "SELECT horse_id FROM payouts WHERE event_year = ?", (event_year,)).fetchall()
    return {r["horse_id"] for r in rows} & horses


def _listener() -> None:
    """The store's change listener: never raises into La Quiniela's write."""
    try:
        sync()
    except Exception:
        log.exception("La Subasta: applying La Quiniela's field failed; "
                      "the next La Subasta request tries again")


def _listen(store: Any) -> None:
    global _store, _seen
    if store is not _store:
        store.add_listener(_listener)
        _store, _seen = store, None


def sync(force: bool = False) -> Optional[Dict[str, Any]]:
    """Bring the auction in line with La Quiniela's field. Cheap when
    nothing has changed (names_rev is where it was at the last sync).
    Returns {"applied": apply()'s result, "left": horses that left the field
    since the last sync, "field": the numbers} when it looked, None when it
    did not (no La Quiniela board, nothing changed, or a store that could not
    read its database: see the header)."""
    global _seen, _signature, _degraded_logged
    store = field.lq_store()
    if store is None:
        return None             # no board: no scratch can have been recorded
    with _lock:
        _listen(store)
        rev = store.names_rev
        if not force and _seen is not None and _seen[0] is store and _seen[1] == rev:
            return None
        the_field = field.Field(store.field(), live=True)
        if the_field.degraded:
            if _degraded_logged is not store:
                _degraded_logged = store
                log.error("La Subasta: La Quiniela's store could not read its database at start, so its "
                          "field is a default (1-20, no names, no scratches) and is NOT applied: no bid "
                          "or ownership is voided until the store loads normally (restart pi5 once the "
                          "database is readable)")
            return None
        applied = apply(the_field)
        if applied:
            log.info("La Subasta: La Quiniela's field applied (names_rev %s): %s", the_field.names_rev,
                     ", ".join(f"#{h} {d['bids']} bid(s) voided" + (", ownership voided" if d["owner"] else "")
                               for h, d in sorted(applied.items())))
        signature = tuple((h["horse_id"], h["name"]) for h in the_field.horses)
        before = _signature
        numbers = the_field.numbers()
        left = sorted(({n for n, _ in before} - set(numbers) if before is not None else set()) | set(applied))
        # Pushed before the field counts as seen, still under the lock: a push
        # that raises leaves _seen and _signature as they were, so the next
        # sync() finds the same horses gone (and voids nothing twice).
        for horse in left:
            done = applied.get(horse) or {"bids": 0, "owner": None}
            notifications.horse_scratched(horse, refund_count=done["bids"],
                                          ownership_voided=done["owner"] is not None)
        if applied or (before is not None and before != signature):
            notifications.field_changed(the_field.names_rev, numbers)
        _seen, _signature = (store, the_field.names_rev), signature
    return {"applied": applied, "left": left, "field": numbers}


def follow_la_quiniela() -> bool:
    """Listen to La Quiniela's store and apply what it already holds (main.py,
    once the board exists). False when there is no board yet."""
    if field.lq_store() is None:
        return False
    sync(force=True)
    return True


def forget() -> None:
    """Drop what this process remembers (tests: a restart of pi5). The
    store's listener, if any, stays registered on that store."""
    global _store, _seen, _signature, _degraded_logged
    with _lock:
        _store, _seen, _signature, _degraded_logged = None, None, None, None

# la_subasta/reset.py - Admin testing-only state reset helpers
#
# Three scope levels for wiping La Subasta state between test runs without
# manually running SQL. NOT for production — callers must guard with the
# confirm=TESTING parameter at the HTTP layer.
#
# All three functions filter by event_year so prior years' data is never
# touched. settings_audit_log and auction_overrides are deliberately NOT
# reset — they persist across test runs (audit trail + admin tunables stay
# intact).

from typing import Dict

from la_subasta.config import EVENT_YEAR, HOUSE_BIDDER_IDENTITY
from la_subasta.models import _ensure_house_bidder, write_txn
from la_subasta.state_machine import AuctionState


# -----------------------------------------------------------------------------
# Internal helpers — operate inside an already-open write_txn
# -----------------------------------------------------------------------------

def _reset_auction_state_row(conn, event_year: int) -> None:
    """Force auction_state for this year back to a clean NOT_STARTED row."""
    conn.execute(
        "INSERT OR IGNORE INTO auction_state (state, event_year) VALUES (?, ?)",
        (AuctionState.NOT_STARTED.value, event_year),
    )
    conn.execute(
        "UPDATE auction_state SET state = ?, opens_at = NULL, "
        "closes_at = NULL, total_pot = 0, updated_at = datetime('now') "
        "WHERE event_year = ?",
        (AuctionState.NOT_STARTED.value, event_year),
    )


def _do_reset_bids(conn, event_year: int) -> Dict[str, int]:
    """Wipe bids/ownership/payouts + clear scratched flags + reset state.
    Caller must already be inside write_txn so the whole reset commits atomically."""
    bids_cur = conn.execute(
        "DELETE FROM bids WHERE event_year = ?", (event_year,),
    )
    bids_deleted = bids_cur.rowcount or 0

    own_cur = conn.execute(
        "DELETE FROM ownership WHERE event_year = ?", (event_year,),
    )
    own_deleted = own_cur.rowcount or 0

    pay_cur = conn.execute(
        "DELETE FROM payouts WHERE event_year = ?", (event_year,),
    )
    pay_deleted = pay_cur.rowcount or 0

    conn.execute(
        "UPDATE horse_state SET scratched = 0, scratched_at = NULL "
        "WHERE event_year = ?",
        (event_year,),
    )

    _reset_auction_state_row(conn, event_year)

    return {
        "bids":      bids_deleted,
        "ownership": own_deleted,
        "payouts":   pay_deleted,
        "bidders":   0,
    }


# -----------------------------------------------------------------------------
# Public scopes
# -----------------------------------------------------------------------------

def reset_bids(event_year: int = EVENT_YEAR) -> Dict[str, int]:
    """
    Wipe bids, ownership, payouts, scratched flags. Reset auction_state to
    NOT_STARTED with total_pot = 0. Bidders (including The House) are kept.
    Single atomic transaction.
    """
    with write_txn() as conn:
        return _do_reset_bids(conn, event_year)


def reset_full(event_year: int = EVENT_YEAR) -> Dict[str, int]:
    """
    Everything reset_bids does, plus delete all bidders except The House.
    Re-verifies the House row is present at the end so payouts that
    fall back to House have a valid FK target. Single atomic transaction.
    """
    with write_txn() as conn:
        result = _do_reset_bids(conn, event_year)
        bd_cur = conn.execute(
            "DELETE FROM bidders WHERE identity != ? AND event_year = ?",
            (HOUSE_BIDDER_IDENTITY, event_year),
        )
        result["bidders"] = bd_cur.rowcount or 0
        # Defensive: re-insert House if somehow missing, and refresh the
        # models-level cached id so house_bidder_id() returns the right row.
        _ensure_house_bidder(conn)
    return result


def reset_state(event_year: int = EVENT_YEAR) -> Dict[str, int]:
    """
    Flip auction_state back to NOT_STARTED only. No bids/bidders/payouts
    touched. Useful for re-running a sandbox sequence without losing the
    populated bidder roster. total_pot is reset to 0 alongside the state
    flip (a NOT_STARTED auction having a non-zero pot is incoherent).
    """
    with write_txn() as conn:
        _reset_auction_state_row(conn, event_year)
    return {"bids": 0, "ownership": 0, "payouts": 0, "bidders": 0}

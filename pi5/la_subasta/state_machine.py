# la_subasta/state_machine.py - Auction state lifecycle
#
# Parallel to (but separate from) the dashboard's race state machine.
# The admin drives this one; the race state machine drives the race LEDs.
#
# NOT_STARTED → OPEN → FINAL_HOUR → LOCKED → RACE_COMPLETE → SETTLED

from enum import Enum

from la_subasta.config import EVENT_YEAR
from la_subasta.models import get_conn, write_txn


class AuctionState(str, Enum):
    NOT_STARTED   = "NOT_STARTED"
    OPEN          = "OPEN"
    FINAL_HOUR    = "FINAL_HOUR"
    LOCKED        = "LOCKED"
    RACE_COMPLETE = "RACE_COMPLETE"
    SETTLED       = "SETTLED"


# Legal forward transitions. Admin can also force a jump backwards for
# recovery via reset(), but normal flow is strictly one direction.
_TRANSITIONS = {
    AuctionState.NOT_STARTED:   {AuctionState.OPEN},
    AuctionState.OPEN:          {AuctionState.FINAL_HOUR, AuctionState.LOCKED},
    AuctionState.FINAL_HOUR:    {AuctionState.LOCKED},
    AuctionState.LOCKED:        {AuctionState.RACE_COMPLETE},
    AuctionState.RACE_COMPLETE: {AuctionState.SETTLED},
    AuctionState.SETTLED:       set(),
}


# States in which bids are legal.
BIDDABLE_STATES = {AuctionState.OPEN, AuctionState.FINAL_HOUR}


def get_state(event_year: int = EVENT_YEAR) -> AuctionState:
    """Return the current auction state for the given event year."""
    conn = get_conn()
    row = conn.execute(
        "SELECT state FROM auction_state WHERE event_year = ?",
        (event_year,),
    ).fetchone()
    if row is None:
        return AuctionState.NOT_STARTED
    return AuctionState(row["state"])


def get_state_row(event_year: int = EVENT_YEAR) -> dict:
    """Return the full auction_state row as a dict (creating a default if missing)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT state, opens_at, closes_at, total_pot, updated_at "
        "FROM auction_state WHERE event_year = ?",
        (event_year,),
    ).fetchone()
    if row is None:
        return {
            "state": AuctionState.NOT_STARTED.value,
            "opens_at": None,
            "closes_at": None,
            "total_pot": 0.0,
            "updated_at": None,
        }
    return dict(row)


def _ensure_row(conn, event_year: int) -> None:
    """Insert a NOT_STARTED row for this event_year if none exists."""
    conn.execute(
        "INSERT OR IGNORE INTO auction_state (state, event_year) VALUES (?, ?)",
        (AuctionState.NOT_STARTED.value, event_year),
    )


def transition(new_state: AuctionState, event_year: int = EVENT_YEAR,
               force: bool = False) -> AuctionState:
    """
    Move to new_state. Raises ValueError on illegal transition unless force=True.

    Args:
        new_state: The target auction state.
        event_year: The event year this state applies to.
        force: If True, skip the legal-transitions check (admin override).

    Returns:
        The new AuctionState.
    """
    with write_txn() as conn:
        _ensure_row(conn, event_year)
        current = AuctionState(conn.execute(
            "SELECT state FROM auction_state WHERE event_year = ?",
            (event_year,),
        ).fetchone()["state"])

        if not force and new_state not in _TRANSITIONS.get(current, set()):
            raise ValueError(
                f"Illegal transition {current.value} → {new_state.value}"
            )

        conn.execute(
            "UPDATE auction_state SET state = ?, updated_at = datetime('now') "
            "WHERE event_year = ?",
            (new_state.value, event_year),
        )

    return new_state


def is_biddable(event_year: int = EVENT_YEAR) -> bool:
    """True iff bids are currently accepted."""
    return get_state(event_year) in BIDDABLE_STATES


def reset(event_year: int = EVENT_YEAR) -> None:
    """Force state back to NOT_STARTED. Used by sandbox/tests only."""
    with write_txn() as conn:
        _ensure_row(conn, event_year)
        conn.execute(
            "UPDATE auction_state SET state = ?, updated_at = datetime('now'), "
            "total_pot = 0 WHERE event_year = ?",
            (AuctionState.NOT_STARTED.value, event_year),
        )


def update_total_pot(pot: float, event_year: int = EVENT_YEAR) -> None:
    """Persist the running total pot (sum of current high bids)."""
    with write_txn() as conn:
        _ensure_row(conn, event_year)
        conn.execute(
            "UPDATE auction_state SET total_pot = ?, updated_at = datetime('now') "
            "WHERE event_year = ?",
            (pot, event_year),
        )

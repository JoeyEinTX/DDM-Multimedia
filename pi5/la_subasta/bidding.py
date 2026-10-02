# la_subasta/bidding.py - Bidder registration, bid placement, validation, undo
#
# All validation is server-authoritative per spec § Bidding Mechanics.
# Rejections return a BidError with a user-friendly `reason` string.

import time
import sqlite3
from dataclasses import dataclass
from typing import Optional, List, Dict

from la_subasta import field, settings
from la_subasta.config import (
    EMOJI_PALETTE, EVENT_YEAR, MIN_RAISE,
    BID_UNDO_WINDOW_SECONDS, MAX_HORSE,
)
from la_subasta.models import get_conn, write_txn
from la_subasta.state_machine import is_biddable


# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------

class BidError(Exception):
    """Raised when a bid/registration is rejected. `reason` is user-facing."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# -----------------------------------------------------------------------------
# Bidders
# -----------------------------------------------------------------------------

def register_bidder(name: str, emoji: str,
                    event_year: int = EVENT_YEAR) -> dict:
    """
    Create a new bidder. Enforces:
      - name+emoji identity is globally unique (per event_year)
      - emoji is from the approved palette
      - name is non-empty

    Returns the created bidder row as a dict.
    """
    name = (name or "").strip()
    emoji = (emoji or "").strip()

    if not name:
        raise BidError("Name is required")
    if emoji not in EMOJI_PALETTE:
        raise BidError("Pick an emoji from the palette")

    identity = f"{name} {emoji}"

    try:
        with write_txn() as conn:
            cursor = conn.execute(
                "INSERT INTO bidders (name, emoji, identity, event_year) "
                "VALUES (?, ?, ?, ?)",
                (name, emoji, identity, event_year),
            )
            bidder_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise BidError("That name + emoji combo is taken — pick another emoji")

    return get_bidder(bidder_id)


def get_bidder(bidder_id: int) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM bidders WHERE id = ?", (bidder_id,),
    ).fetchone()
    return dict(row) if row else None


def get_bidder_by_identity(identity: str,
                           event_year: int = EVENT_YEAR) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM bidders WHERE identity = ? AND event_year = ?",
        (identity, event_year),
    ).fetchone()
    return dict(row) if row else None


def identity_available(name: str, emoji: str,
                       event_year: int = EVENT_YEAR) -> bool:
    name = (name or "").strip()
    emoji = (emoji or "").strip()
    if not name or not emoji:
        return False
    return get_bidder_by_identity(f"{name} {emoji}", event_year) is None


def list_bidders(event_year: int = EVENT_YEAR) -> List[dict]:
    """All bidders registered for the event year, oldest first. Each row
    carries cap_exempt (the admin's exemption from the max-horses cap)."""
    rows = get_conn().execute(
        "SELECT * FROM bidders WHERE event_year = ? ORDER BY created_at, id",
        (event_year,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_bidders(event_year: int = EVENT_YEAR) -> int:
    """Number of registered bidders."""
    return len(list_bidders(event_year))


def set_cap_exempt(bidder_id: int, exempt: bool) -> dict:
    """The admin marks a bidder free of the max-horses cap, or takes the mark
    off: the host, say, who buys the horses nobody else bid on so that none
    reaches the lock unsold. The cap still applies to everyone else, and a
    bidder whose mark comes off keeps what they lead but takes no new horse
    beyond the cap. Returns the bidder row; BidError if there is no such
    bidder."""
    with write_txn() as conn:
        cur = conn.execute(
            "UPDATE bidders SET cap_exempt = ? WHERE id = ?",
            (1 if exempt else 0, bidder_id),
        )
        if not cur.rowcount:
            raise BidError("Unknown bidder")
    return get_bidder(bidder_id)


def count_bids(event_year: int = EVENT_YEAR,
               include_voided: bool = True) -> int:
    """Total bids placed for the event year (voided rows counted by default)."""
    if include_voided:
        row = get_conn().execute(
            "SELECT COUNT(*) AS c FROM bids WHERE event_year = ?",
            (event_year,),
        ).fetchone()
    else:
        row = get_conn().execute(
            "SELECT COUNT(*) AS c FROM bids WHERE event_year = ? AND voided = 0",
            (event_year,),
        ).fetchone()
    return row["c"]


# -----------------------------------------------------------------------------
# Horse / bid queries
# -----------------------------------------------------------------------------

def current_high_bid(horse_id: int,
                     event_year: int = EVENT_YEAR) -> Optional[dict]:
    """
    Return the current winning bid for a horse as a dict (bid row + bidder info),
    or None if no active bids exist.

    Ties broken by earliest bid_time (per spec — tiebreaker = earliest wins).
    """
    row = get_conn().execute(
        """
        SELECT b.id, b.bidder_id, b.horse_id, b.amount, b.bid_time,
               bd.identity, bd.name, bd.emoji
          FROM bids b
          JOIN bidders bd ON bd.id = b.bidder_id
         WHERE b.horse_id = ?
           AND b.voided = 0
           AND b.event_year = ?
         ORDER BY b.amount DESC, b.bid_time ASC, b.id ASC
         LIMIT 1
        """,
        (horse_id, event_year),
    ).fetchone()
    return dict(row) if row else None


def horses_leading_by(bidder_id: int,
                      event_year: int = EVENT_YEAR) -> List[int]:
    """Return list of horse_ids in the field this bidder is currently leading on."""
    leading = []
    for horse_id in field.current().numbers():
        hb = current_high_bid(horse_id, event_year)
        if hb and hb["bidder_id"] == bidder_id:
            leading.append(horse_id)
    return leading


def unsold_horses(event_year: int = EVENT_YEAR) -> List[int]:
    """The program numbers, in order, of the horses in the field that nobody
    has bid on. There is no House to take a horse nobody bought: the lock
    warns about these (blueprint.api_admin_lock) so a bidder, in practice the
    host, buys them first."""
    return [n for n in field.current().numbers()
            if current_high_bid(n, event_year) is None]


# Scratches are La Quiniela's: a scratched horse is simply not in the field
# (field.py), and scratches.py voids what was bid on it. There is no
# scratched flag here.


# -----------------------------------------------------------------------------
# Bid placement (with full validation per spec)
# -----------------------------------------------------------------------------

@dataclass
class PlacedBid:
    bid_id: int
    bidder_id: int
    horse_id: int
    amount: float
    bid_time: str
    previous_bidder_id: Optional[int]
    previous_bidder_identity: Optional[str]


def place_bid(bidder_id: int, horse_id: int, amount: float,
              event_year: int = EVENT_YEAR) -> PlacedBid:
    """
    Validate and insert a bid. All validation happens inside the write txn
    so a concurrent bid can't slip past the max-raise / max-horses checks.

    Raises BidError with a user-facing reason on any rejection.
    """
    # ---- Cheap pre-checks (outside txn is fine) ----------------------------
    # Read mutable rules at bid time so runtime override changes take effect
    # immediately on the next bid (no server restart required).
    min_bid = settings.get_setting("MIN_BID")
    max_raise = settings.get_setting("MAX_RAISE")
    max_horses_per_bidder = settings.get_setting("MAX_HORSES_PER_BIDDER")

    if not field.valid_number(horse_id):
        raise BidError(f"Invalid horse (must be 1-{MAX_HORSE})")

    # The field is La Quiniela's: a scratched horse, or an also-eligible
    # standing in for nobody, is not sold.
    the_field = field.current()
    if horse_id not in the_field:
        raise BidError(the_field.refusal(horse_id))

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise BidError("Amount must be a number")

    if amount < min_bid:
        raise BidError(f"Minimum bid is ${min_bid}")

    # Whole-dollar bids only — fractional dollars don't make sense in this UX
    if amount != int(amount):
        raise BidError("Bid must be a whole dollar amount")

    if not is_biddable(event_year):
        raise BidError("Auction is not accepting bids right now")

    if get_bidder(bidder_id) is None:
        raise BidError("Unknown bidder")

    # ---- Serialized validation + insert ------------------------------------
    # The field is read again under the write lock, in write_txn's prepare
    # hook so that it is read before sqlite's write lock is taken: this thread
    # never waits for La Quiniela's store with that lock in hand
    # (models.write_txn). A scratch recorded since the check above either
    # refuses this bid here or, applied after the bid commits under this same
    # lock (scratches.apply), voids it. A bid never outlives its horse.
    read = {}

    def read_field():
        read["field"] = field.current()

    with write_txn(prepare=read_field) as conn:
        the_field = read["field"]
        if horse_id not in the_field:
            raise BidError(the_field.refusal(horse_id))

        # Current high bid (re-queried inside txn)
        hb_row = conn.execute(
            """
            SELECT b.id, b.bidder_id, b.amount, bd.identity
              FROM bids b
              JOIN bidders bd ON bd.id = b.bidder_id
             WHERE b.horse_id = ?
               AND b.voided = 0
               AND b.event_year = ?
             ORDER BY b.amount DESC, b.bid_time ASC, b.id ASC
             LIMIT 1
            """,
            (horse_id, event_year),
        ).fetchone()

        current_amount = hb_row["amount"] if hb_row else 0.0
        current_bidder = hb_row["bidder_id"] if hb_row else None
        previous_identity = hb_row["identity"] if hb_row else None

        # Can't outbid yourself
        if current_bidder == bidder_id:
            raise BidError("You're already leading on this horse")

        # Opening bid vs raise
        if current_amount <= 0:
            # No existing bid — must be at least MIN_BID (already checked) and
            # at most MIN_BID + MAX_RAISE. Opening bids are effectively
            # MIN_BID..MIN_BID+MAX_RAISE per spec.
            if amount > min_bid + max_raise:
                raise BidError(f"Opening bid max is ${min_bid + max_raise}")
        else:
            min_allowed = current_amount + MIN_RAISE
            max_allowed = current_amount + max_raise
            if amount < min_allowed:
                raise BidError(f"Must bid at least ${int(min_allowed)}")
            if amount > max_allowed:
                raise BidError(f"Max raise is ${max_raise} (so ${int(max_allowed)} max)")

        # Max horses owned — checked against horses where bidder is CURRENTLY leading.
        # Outbidding someone on a horse you're already leading is impossible
        # (caught above), so this check is purely "are you about to become
        # leader on a NEW horse that would push them past the cap?"
        leading_rows = conn.execute(
            """
            SELECT b.horse_id
              FROM bids b
              JOIN (
                SELECT horse_id, MAX(amount) AS max_amt
                  FROM bids
                 WHERE voided = 0 AND event_year = ?
                 GROUP BY horse_id
              ) top ON top.horse_id = b.horse_id AND top.max_amt = b.amount
             WHERE b.bidder_id = ?
               AND b.voided = 0
               AND b.event_year = ?
            """,
            (event_year, bidder_id, event_year),
        ).fetchall()
        currently_leading = {r["horse_id"] for r in leading_rows}

        # The admin can mark a bidder exempt from the cap (the host, who buys
        # the horses nobody else bid on); the cap still applies to the rest.
        exempt_row = conn.execute(
            "SELECT cap_exempt FROM bidders WHERE id = ?", (bidder_id,),
        ).fetchone()
        cap_exempt = bool(exempt_row and exempt_row["cap_exempt"])

        # If bidder isn't currently leading this horse AND adding it would
        # push them past the cap, reject.
        if (not cap_exempt and horse_id not in currently_leading
                and len(currently_leading) >= max_horses_per_bidder):
            raise BidError(f"Max {max_horses_per_bidder} horses per bidder")

        # ---- Insert the bid ------------------------------------------------
        cursor = conn.execute(
            "INSERT INTO bids (bidder_id, horse_id, amount, event_year) "
            "VALUES (?, ?, ?, ?)",
            (bidder_id, horse_id, amount, event_year),
        )
        bid_id = cursor.lastrowid

        bid_time = conn.execute(
            "SELECT bid_time FROM bids WHERE id = ?", (bid_id,),
        ).fetchone()["bid_time"]

    return PlacedBid(
        bid_id=bid_id,
        bidder_id=bidder_id,
        horse_id=horse_id,
        amount=amount,
        bid_time=bid_time,
        previous_bidder_id=current_bidder,
        previous_bidder_identity=previous_identity,
    )


# -----------------------------------------------------------------------------
# Undo (10-second window)
# -----------------------------------------------------------------------------

def undo_bid(bid_id: int, bidder_id: int) -> dict:
    """
    Void a bid within BID_UNDO_WINDOW_SECONDS of placement.

    Only the bidder who placed the bid can undo it. Auction state is
    irrelevant — undo is allowed in any state as long as the window is open
    (matches spec intent: fat-finger protection during live bidding).

    Returns a dict with the voided bid + restored leader info.
    """
    conn = get_conn()
    row = conn.execute(
        """
        SELECT id, bidder_id, horse_id, amount, bid_time, voided,
               (strftime('%s','now') - strftime('%s', bid_time)) AS age_seconds
          FROM bids
         WHERE id = ?
        """,
        (bid_id,),
    ).fetchone()

    if row is None:
        raise BidError("Bid not found")
    if row["bidder_id"] != bidder_id:
        raise BidError("You can only undo your own bids")
    if row["voided"]:
        raise BidError("Bid already voided")

    age = row["age_seconds"]
    if age is None or age > BID_UNDO_WINDOW_SECONDS:
        raise BidError(
            f"Undo window ({BID_UNDO_WINDOW_SECONDS}s) expired"
        )

    with write_txn() as conn:
        conn.execute(
            "UPDATE bids SET voided = 1, voided_reason = 'undo' WHERE id = ?",
            (bid_id,),
        )

    new_high = current_high_bid(row["horse_id"])
    return {
        "voided_bid_id": bid_id,
        "horse_id": row["horse_id"],
        "new_high_bid": new_high,
    }


# -----------------------------------------------------------------------------
# Admin void (re-award to the runner-up, per spec § Welch / Void Policy)
# -----------------------------------------------------------------------------

def _follow_ownership(conn, horse_id: int, event_year: int) -> None:
    """After the lock a horse's ownership row is its frozen winner. When a bid
    on it is voided the row follows the bids: re-awarded to the highest bid
    still active, at that bid's amount, or removed when none is left (the
    horse is unowned: payouts.py's safety net applies if it pays). Before the
    lock there is no row and nothing to do."""
    own = conn.execute(
        "SELECT id FROM ownership WHERE horse_id = ? AND event_year = ? AND voided = 0",
        (horse_id, event_year),
    ).fetchone()
    if own is None:
        return
    top = conn.execute(
        "SELECT bidder_id, amount FROM bids "
        "WHERE horse_id = ? AND voided = 0 AND event_year = ? "
        "ORDER BY amount DESC, bid_time ASC, id ASC LIMIT 1",
        (horse_id, event_year),
    ).fetchone()
    if top is None:
        conn.execute("DELETE FROM ownership WHERE id = ?", (own["id"],))
    else:
        conn.execute(
            "UPDATE ownership SET bidder_id = ?, winning_bid = ? WHERE id = ?",
            (top["bidder_id"], top["amount"], own["id"]),
        )


def void_bid(bid_id: int, reason: str, event_year: int = EVENT_YEAR) -> dict:
    """Admin void: no time limit, records the reason. The horse goes to the
    runner-up at their bid, which is the next-highest active bid. With no
    second bidder it is unsold: there is no House to take it, so before the
    lock it is back on the list with no bid (the lock warns about it), and
    after the lock it has no owner (see _follow_ownership). `unsold` says so."""
    with write_txn() as conn:
        row = conn.execute(
            "SELECT horse_id, voided FROM bids WHERE id = ?", (bid_id,),
        ).fetchone()
        if row is None:
            raise BidError("Bid not found")
        if row["voided"]:
            raise BidError("Bid already voided")
        conn.execute(
            "UPDATE bids SET voided = 1, voided_reason = ? WHERE id = ?",
            (reason or "admin void", bid_id),
        )
        _follow_ownership(conn, row["horse_id"], event_year)

    new_high = current_high_bid(row["horse_id"], event_year)
    return {
        "voided_bid_id": bid_id,
        "horse_id": row["horse_id"],
        "new_high_bid": new_high,
        "unsold": new_high is None,
    }


# -----------------------------------------------------------------------------
# Pot / summary helpers
# -----------------------------------------------------------------------------

def total_pot(event_year: int = EVENT_YEAR) -> float:
    """Sum of current high bids across the field's horses."""
    pot = 0.0
    for horse_id in field.current().numbers():
        hb = current_high_bid(horse_id, event_year)
        if hb:
            pot += hb["amount"]
    return pot


def bidder_portfolio(bidder_id: int,
                     event_year: int = EVENT_YEAR) -> Dict:
    """Return what horses a bidder is leading + total owed. scratched: the
    horses they owned at the lock that La Quiniela then scratched, each
    with the winning bid no longer owed (ownership voided, scratches.py)."""
    leading = horses_leading_by(bidder_id, event_year)
    total = 0.0
    horses = []
    for h in leading:
        hb = current_high_bid(h, event_year)
        if hb:
            horses.append({"horse_id": h, "amount": hb["amount"]})
            total += hb["amount"]
    rows = get_conn().execute(
        "SELECT horse_id, winning_bid FROM ownership "
        "WHERE bidder_id = ? AND event_year = ? AND voided = 1 AND voided_reason = 'scratched' "
        "ORDER BY horse_id",
        (bidder_id, event_year),
    ).fetchall()
    scratched = [{"horse_id": r["horse_id"], "amount": r["winning_bid"]} for r in rows]
    return {"bidder_id": bidder_id, "horses": horses, "total": total,
            "scratched": scratched}


def ledger(bidder: dict, portfolio: Dict) -> Dict:
    """What the admin settles with a bidder: owed (the portfolio's total)
    and refund_owed, what a bidder already marked paid gets back because a
    horse was scratched after they paid (paid_amount minus what they owe
    now; 0 for an unpaid bidder, who simply owes less)."""
    owed = portfolio["total"]
    refund = 0.0
    if bidder.get("paid") and bidder.get("paid_amount") is not None:
        refund = max(0.0, float(bidder["paid_amount"]) - owed)
    return {"owed": owed, "refund_owed": refund}

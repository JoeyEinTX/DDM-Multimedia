# la_subasta/bidding.py - Bidder registration, bid placement, validation, undo
#
# All validation is server-authoritative per spec § Bidding Mechanics.
# Rejections return a BidError with a user-friendly `reason` string.

import time
import sqlite3
from dataclasses import dataclass
from typing import Optional, List, Dict

from la_subasta import settings
from la_subasta.config import (
    EMOJI_PALETTE, EVENT_YEAR, MIN_RAISE,
    BID_UNDO_WINDOW_SECONDS, NUM_HORSES,
    HOUSE_BIDDER_IDENTITY,
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


def list_bidders(event_year: int = EVENT_YEAR,
                 include_house: bool = False) -> List[dict]:
    """
    Return all real bidders for the event year. The House sentinel row is
    filtered out by default — Phase 4 spectator TV renders House wins via a
    dedicated path, and guest-facing bidder lists shouldn't surface it.
    Set include_house=True for admin/debug views.
    """
    if include_house:
        rows = get_conn().execute(
            "SELECT * FROM bidders WHERE event_year = ? ORDER BY created_at",
            (event_year,),
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM bidders WHERE event_year = ? AND identity != ? "
            "ORDER BY created_at",
            (event_year, HOUSE_BIDDER_IDENTITY),
        ).fetchall()
    return [dict(r) for r in rows]


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
    """Return list of horse_ids this bidder is currently leading on."""
    leading = []
    for horse_id in range(1, NUM_HORSES + 1):
        hb = current_high_bid(horse_id, event_year)
        if hb and hb["bidder_id"] == bidder_id:
            leading.append(horse_id)
    return leading


# Scratched state lives ONLY in La Subasta's horse_state table. The dashboard's
# DerbyHorse dataclass (services/racing_data_service.py) has no `scratched`
# field today, so there is nothing to mirror from. If the dashboard grows a
# scratch concept later, the recommended sync is one-way dashboard → La
# Subasta: RacingDataService.scratch_horse() emits a SocketIO event (e.g.
# "horse_scratched") or invokes a registered callback, and La Subasta's
# listener calls scratch_horse() here to write through. Joey scratches once
# on the dashboard; La Subasta picks it up. Until that exists, the admin
# iPad view (Phase 3) POSTs to /la-subasta/api/admin/scratch directly.
def is_horse_scratched(horse_id: int,
                       event_year: int = EVENT_YEAR) -> bool:
    row = get_conn().execute(
        "SELECT scratched FROM horse_state WHERE horse_id = ? AND event_year = ?",
        (horse_id, event_year),
    ).fetchone()
    return bool(row and row["scratched"])


def scratch_horse(horse_id: int, event_year: int = EVENT_YEAR) -> None:
    with write_txn() as conn:
        conn.execute(
            """
            INSERT INTO horse_state (horse_id, scratched, scratched_at, event_year)
            VALUES (?, 1, datetime('now'), ?)
            ON CONFLICT(horse_id, event_year) DO UPDATE SET
                scratched = 1, scratched_at = datetime('now')
            """,
            (horse_id, event_year),
        )


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

    if not isinstance(horse_id, int) or horse_id < 1 or horse_id > NUM_HORSES:
        raise BidError(f"Invalid horse (must be 1-{NUM_HORSES})")

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

    if is_horse_scratched(horse_id, event_year):
        raise BidError("That horse has been scratched")

    if get_bidder(bidder_id) is None:
        raise BidError("Unknown bidder")

    # ---- Serialized validation + insert ------------------------------------
    with write_txn() as conn:
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

        # If bidder isn't currently leading this horse AND adding it would
        # push them past the cap, reject.
        if horse_id not in currently_leading and len(currently_leading) >= max_horses_per_bidder:
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
# Admin void (re-award to 2nd-highest, per spec § Welch / Void Policy)
# -----------------------------------------------------------------------------

def void_bid(bid_id: int, reason: str) -> dict:
    """Admin void — no time limit, records reason, re-awards horse to runner-up."""
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

    return {
        "voided_bid_id": bid_id,
        "horse_id": row["horse_id"],
        "new_high_bid": current_high_bid(row["horse_id"]),
    }


# -----------------------------------------------------------------------------
# Pot / summary helpers
# -----------------------------------------------------------------------------

def total_pot(event_year: int = EVENT_YEAR) -> float:
    """Sum of current high bids across all non-scratched horses."""
    pot = 0.0
    for horse_id in range(1, NUM_HORSES + 1):
        if is_horse_scratched(horse_id, event_year):
            continue
        hb = current_high_bid(horse_id, event_year)
        if hb:
            pot += hb["amount"]
    return pot


def bidder_portfolio(bidder_id: int,
                     event_year: int = EVENT_YEAR) -> Dict:
    """Return what horses a bidder is leading + total owed."""
    leading = horses_leading_by(bidder_id, event_year)
    total = 0.0
    horses = []
    for h in leading:
        hb = current_high_bid(h, event_year)
        if hb:
            horses.append({"horse_id": h, "amount": hb["amount"]})
            total += hb["amount"]
    return {"bidder_id": bidder_id, "horses": horses, "total": total}

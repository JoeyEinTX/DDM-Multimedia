# la_subasta/payouts.py - 60/25/15 win/place/show split
#
# Called after race results are entered. Freezes ownership from current
# high bids, computes payouts against the total pot, persists them.

from typing import Dict, List, Optional

from la_subasta import settings
from la_subasta.config import EVENT_YEAR, NUM_HORSES
from la_subasta.bidding import current_high_bid, is_horse_scratched
from la_subasta.models import get_conn, house_bidder_id, write_txn


def parse_payout_preset(preset: str) -> Dict[str, float]:
    """
    Parse "W/P/S" where W+P+S = 100 into decimal percentages.

    Raises ValueError on malformed input or percentages that don't sum to 100
    (i.e. the corresponding decimal sum != 1.0).
    """
    if not isinstance(preset, str):
        raise ValueError(f"Payout preset must be a string, got {type(preset).__name__}")
    parts = preset.split("/")
    if len(parts) != 3:
        raise ValueError(f"Malformed payout preset: {preset!r} (expected W/P/S)")
    try:
        win_pct, place_pct, show_pct = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"Payout preset segments must be integers: {preset!r}")
    if win_pct < 0 or place_pct < 0 or show_pct < 0:
        raise ValueError(f"Payout preset segments must be non-negative: {preset!r}")
    result = {
        "win":   win_pct / 100.0,
        "place": place_pct / 100.0,
        "show":  show_pct / 100.0,
    }
    # Sanity check sum == 1.0 (float-safe tolerance)
    total = result["win"] + result["place"] + result["show"]
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"Payout preset percentages must sum to 1.0 (100%), got {total}"
        )
    return result


def current_payout_pcts() -> Dict[str, float]:
    """Return the active preset's win/place/show decimals from settings."""
    return parse_payout_preset(settings.get_setting("PAYOUT_PRESET"))


def compute_payout_amounts(total_pot: float) -> Dict[str, float]:
    """Return the three payout amounts for a given pot. Pure-ish (reads settings)."""
    pcts = current_payout_pcts()
    return {
        "win":   round(total_pot * pcts["win"], 2),
        "place": round(total_pot * pcts["place"], 2),
        "show":  round(total_pot * pcts["show"], 2),
    }


def freeze_ownership(event_year: int = EVENT_YEAR) -> List[dict]:
    """
    Snapshot current high bidders into the `ownership` table. Called once
    when the auction locks. Idempotent — repeated calls refresh the snapshot.

    Horses with no bids get no ownership row (they go to The House at $0
    for payout purposes, handled by compute_and_persist_payouts).
    """
    rows = []
    with write_txn() as conn:
        conn.execute(
            "DELETE FROM ownership WHERE event_year = ?", (event_year,),
        )
        for horse_id in range(1, NUM_HORSES + 1):
            if is_horse_scratched(horse_id, event_year):
                continue
            hb = current_high_bid(horse_id, event_year)
            if hb is None:
                continue
            conn.execute(
                "INSERT INTO ownership (horse_id, bidder_id, winning_bid, event_year) "
                "VALUES (?, ?, ?, ?)",
                (horse_id, hb["bidder_id"], hb["amount"], event_year),
            )
            rows.append({
                "horse_id": horse_id,
                "bidder_id": hb["bidder_id"],
                "winning_bid": hb["amount"],
            })
    return rows


def get_owner(horse_id: int, event_year: int = EVENT_YEAR) -> Optional[dict]:
    """Return the owner row for a horse, or None if it went unbid/to House."""
    row = get_conn().execute(
        """
        SELECT o.horse_id, o.bidder_id, o.winning_bid, bd.identity, bd.name, bd.emoji
          FROM ownership o
          JOIN bidders bd ON bd.id = o.bidder_id
         WHERE o.horse_id = ? AND o.event_year = ?
        """,
        (horse_id, event_year),
    ).fetchone()
    return dict(row) if row else None


def compute_and_persist_payouts(win_horse_id: int, place_horse_id: int,
                                show_horse_id: int,
                                event_year: int = EVENT_YEAR) -> dict:
    """
    Compute 60/25/15 payouts and persist to the `payouts` table.

    If a winning horse has no owner (went to The House), the payout row is
    still computed but bidder_id is set to 0 and the caller can route the
    amount to the House fund.

    Returns a dict with the payout breakdown + total pot used.
    """
    # Total pot = sum of winning bids in ownership (post-lock snapshot).
    # If freeze_ownership hasn't been called yet, call it now.
    own_count = get_conn().execute(
        "SELECT COUNT(*) AS c FROM ownership WHERE event_year = ?",
        (event_year,),
    ).fetchone()["c"]
    if own_count == 0:
        freeze_ownership(event_year)

    pot_row = get_conn().execute(
        "SELECT COALESCE(SUM(winning_bid), 0) AS pot "
        "FROM ownership WHERE event_year = ?",
        (event_year,),
    ).fetchone()
    pot = float(pot_row["pot"])

    amounts = compute_payout_amounts(pot)

    finishes = [
        ("win",   win_horse_id,   amounts["win"]),
        ("place", place_horse_id, amounts["place"]),
        ("show",  show_horse_id,  amounts["show"]),
    ]

    house_id = house_bidder_id()
    results = []
    with write_txn() as conn:
        conn.execute(
            "DELETE FROM payouts WHERE event_year = ?", (event_year,),
        )
        for finish, horse_id, amount in finishes:
            owner = get_owner(horse_id, event_year)
            bidder_id = owner["bidder_id"] if owner else house_id
            conn.execute(
                "INSERT INTO payouts (bidder_id, horse_id, finish, amount, event_year) "
                "VALUES (?, ?, ?, ?, ?)",
                (bidder_id, horse_id, finish, amount, event_year),
            )
            results.append({
                "finish": finish,
                "horse_id": horse_id,
                "bidder_id": bidder_id,
                "amount": amount,
                "is_house": owner is None,
            })

    # Update total_pot on auction_state for display convenience
    with write_txn() as conn:
        conn.execute(
            "UPDATE auction_state SET total_pot = ? WHERE event_year = ?",
            (pot, event_year),
        )

    return {
        "total_pot": pot,
        "amounts": amounts,
        "payouts": results,
    }


def list_payouts(event_year: int = EVENT_YEAR) -> List[dict]:
    rows = get_conn().execute(
        """
        SELECT p.id, p.finish, p.horse_id, p.amount, p.paid_out,
               p.bidder_id, bd.identity AS bidder_identity
          FROM payouts p
          LEFT JOIN bidders bd ON bd.id = p.bidder_id
         WHERE p.event_year = ?
         ORDER BY CASE p.finish WHEN 'win' THEN 1 WHEN 'place' THEN 2 ELSE 3 END
        """,
        (event_year,),
    ).fetchall()
    return [dict(r) for r in rows]

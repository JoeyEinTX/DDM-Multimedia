# la_subasta/payouts.py - 60/25/15 win/place/show split
#
# Called after race results are entered. Freezes ownership from current
# high bids, computes payouts against the total pot, persists them.
#
# No House. A horse nobody bought has no ownership row, and a payout slot
# whose horse has no owner is paid to nobody: it is stored with no bidder,
# flagged `unowned` in the ledger, and left to the admin, who names the horse
# that pays it (set_slot_horse). That is La Quiniela's rule for an empty cup in
# the money (architecture spec, section 6): the finisher is skipped and the
# next finisher takes its place, entered by hand. The lock warns about unsold
# horses first (bidding.unsold_horses), so this is a safety net and not the
# plan. Settlement never waits for it: the slot just shows as unresolved until
# it is set.

from typing import Dict, List, Optional

from la_subasta import field, settings
from la_subasta.config import EVENT_YEAR
from la_subasta.bidding import current_high_bid
from la_subasta.models import get_conn, write_txn

FINISHES = ("win", "place", "show")


class PayoutError(Exception):
    """A payout-slot request that cannot be done. `reason` is user-facing and
    `status` is the HTTP status the route answers with."""
    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


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

    Horses with no bids get no ownership row: they are unsold, and there is
    no House to give them to (the lock warns about them first). Should one
    finish in the money, its payout slot is left for the admin to name
    (see the header). Only the field is frozen: a horse La Quiniela does not
    have in it is not sold. A row voided by a scratch (scratches.py) is kept,
    as the record of the refund, unless its horse is in the field and owned
    again.

    The field is read in write_txn's prepare hook, before sqlite's write lock
    is taken (models.write_txn). A scratch recorded after that read voids
    its row once this commits (scratches.apply).
    """
    rows = []
    read = {}

    def read_field():
        read["numbers"] = field.current().numbers()

    with write_txn(prepare=read_field) as conn:
        conn.execute(
            "DELETE FROM ownership WHERE event_year = ? AND voided = 0", (event_year,),
        )
        for horse_id in read["numbers"]:
            hb = current_high_bid(horse_id, event_year)
            if hb is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO ownership (horse_id, bidder_id, winning_bid, event_year) "
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
    """Return the owner row for a horse, or None if it was never sold (no
    bid at the lock) or its ownership was voided by a scratch: it has no
    owner to pay."""
    row = get_conn().execute(
        """
        SELECT o.horse_id, o.bidder_id, o.winning_bid, bd.identity, bd.name, bd.emoji
          FROM ownership o
          JOIN bidders bd ON bd.id = o.bidder_id
         WHERE o.horse_id = ? AND o.event_year = ? AND o.voided = 0
        """,
        (horse_id, event_year),
    ).fetchone()
    return dict(row) if row else None


def list_payouts(event_year: int = EVENT_YEAR) -> List[dict]:
    """The payout ledger, win / place / show. Each row: horse_id is the horse
    that finished in the slot; pays_horse_id the horse the admin named to pay
    it in its place (None when the finisher pays it); bidder_id and
    bidder_identity the owner who is paid (None while nobody is: `unowned`)."""
    rows = get_conn().execute(
        """
        SELECT p.id, p.finish, p.horse_id, p.pays_horse_id, p.amount, p.paid_out,
               p.bidder_id, bd.identity AS bidder_identity
          FROM payouts p
          LEFT JOIN bidders bd ON bd.id = p.bidder_id
         WHERE p.event_year = ?
         ORDER BY CASE p.finish WHEN 'win' THEN 1 WHEN 'place' THEN 2 ELSE 3 END
        """,
        (event_year,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["unowned"] = d["bidder_id"] is None
        out.append(d)
    return out


def _ledger(pot: float, amounts: Dict[str, float], rows: List[dict]) -> dict:
    return {
        "total_pot": pot,
        "amounts": amounts,
        "payouts": rows,
        "unowned": [p["finish"] for p in rows if p["unowned"]],
    }


def ledger(event_year: int = EVENT_YEAR) -> dict:
    """The ledger as results leave it and as the admin's slot picker changes
    it: the pot and the three amounts it was settled on, the rows, and which
    slots (finish names) are still unowned. What payout_computed carries."""
    row = get_conn().execute(
        "SELECT total_pot FROM auction_state WHERE event_year = ?", (event_year,),
    ).fetchone()
    pot = float(row["total_pot"]) if row else 0.0
    return _ledger(pot, compute_payout_amounts(pot), list_payouts(event_year))


def compute_and_persist_payouts(win_horse_id: int, place_horse_id: int,
                                show_horse_id: int,
                                event_year: int = EVENT_YEAR) -> dict:
    """
    Compute 60/25/15 payouts and persist to the `payouts` table.

    A slot whose horse has no owner (never sold, or its ownership voided) is
    stored with no bidder and comes back `unowned`: nobody is paid it until
    the admin names the horse that does (set_slot_horse). There is no House
    fallback and nothing here waits for that.

    Returns the ledger: the total pot used, the amounts, the three rows and
    the finishes still unowned.
    """
    # Total pot = sum of winning bids in ownership (post-lock snapshot),
    # less any voided by a scratch. If freeze_ownership hasn't been called
    # yet (no rows at all, voided or not), call it now.
    own_count = get_conn().execute(
        "SELECT COUNT(*) AS c FROM ownership WHERE event_year = ?",
        (event_year,),
    ).fetchone()["c"]
    if own_count == 0:
        freeze_ownership(event_year)

    pot_row = get_conn().execute(
        "SELECT COALESCE(SUM(winning_bid), 0) AS pot "
        "FROM ownership WHERE event_year = ? AND voided = 0",
        (event_year,),
    ).fetchone()
    pot = float(pot_row["pot"])

    amounts = compute_payout_amounts(pot)

    finishes = [
        ("win",   win_horse_id,   amounts["win"]),
        ("place", place_horse_id, amounts["place"]),
        ("show",  show_horse_id,  amounts["show"]),
    ]

    with write_txn() as conn:
        conn.execute(
            "DELETE FROM payouts WHERE event_year = ?", (event_year,),
        )
        for finish, horse_id, amount in finishes:
            owner = get_owner(horse_id, event_year)
            conn.execute(
                "INSERT INTO payouts (bidder_id, horse_id, finish, amount, event_year) "
                "VALUES (?, ?, ?, ?, ?)",
                (owner["bidder_id"] if owner else None, horse_id, finish, amount, event_year),
            )

    # Update total_pot on auction_state for display convenience
    with write_txn() as conn:
        conn.execute(
            "UPDATE auction_state SET total_pot = ? WHERE event_year = ?",
            (pot, event_year),
        )

    return _ledger(pot, amounts, list_payouts(event_year))


def set_slot_horse(finish: str, horse_id: int,
                   event_year: int = EVENT_YEAR) -> dict:
    """
    The admin names the horse that pays a slot: the next finisher, when the
    horse that finished there had no owner (La Quiniela's rule for an empty
    cup in the money). The slot is then paid to that horse's owner, for the
    amount it already has. Naming the horse that finished there puts the slot
    back as the results had it. Any slot can be set, because skipping one
    finisher moves the next up into it (and the one after into the next).

    The horse must be in the field and have an owner. PayoutError, with the
    status the route answers with, when it cannot be done: 400 for a bad
    finish or horse, 409 when no results are in yet or the horse has no owner
    either. Returns the slot's ledger row.
    """
    if finish not in FINISHES:
        raise PayoutError("finish must be win, place or show")
    if not field.valid_number(horse_id):
        raise PayoutError(f"Invalid horse id: {horse_id}")

    read = {}

    def read_field():                  # before sqlite's write lock (models.write_txn)
        read["field"] = field.current()

    with write_txn(prepare=read_field) as conn:
        the_field = read["field"]
        if horse_id not in the_field:
            raise PayoutError(the_field.refusal(horse_id))
        slot = conn.execute(
            "SELECT id, horse_id FROM payouts WHERE finish = ? AND event_year = ?",
            (finish, event_year),
        ).fetchone()
        if slot is None:
            raise PayoutError("No results have been entered yet", status=409)
        owner = get_owner(horse_id, event_year)
        if owner is None:
            raise PayoutError(
                f"#{horse_id} has no owner either: name a horse that was sold", status=409)
        conn.execute(
            "UPDATE payouts SET pays_horse_id = ?, bidder_id = ?, paid_out = 0 WHERE id = ?",
            (None if horse_id == slot["horse_id"] else horse_id, owner["bidder_id"], slot["id"]),
        )
    return next(p for p in list_payouts(event_year) if p["finish"] == finish)

# la_subasta/payouts.py - The payout ledger, derived from SET WINNERS' result
#
# The race has one result, entered once, in the Control Center's SET WINNERS
# (gap review M5, OD4): pi5/data/results.json, read here through La
# Quiniela's board (BettingBoard.results(), the same code the cups and the TV
# are fed from), never a copy and never over HTTP. La Subasta keeps no result
# of its own and no payout rows: the ledger is worked out from the result and
# the ownership as they stand whenever it is read (ledger()), so a correction
# in SET WINNERS changes the payouts, and Reset betting, which clears the
# result, clears them. The record's next finishers (La Quiniela's skip, M6)
# are not the auction's business: the owners of the real 1st, 2nd and 3rd are
# paid. While a result stands the field is frozen (la_quiniela/board.py), so
# the ownership under it moves only by the admin's own void.
#
# What is stored is only what the admin did (payout_slots, one row per finish
# and finisher): the horse named to pay a slot whose finisher has no owner
# (set_slot_horse), and the paid-out mark (mark_paid): who was paid, how
# much, when. Both are tied to the finisher they were made for, so a
# correction that changes a slot's horse sets them aside, and one that
# changes it back brings them back. A mark that no longer matches the ledger
# (the finisher, the bidder or the amount changed under it, or there is no
# result now) is reported as paid on an earlier result, with what the slot
# pays now, until the host unmarks it or the result comes back; Reset betting
# leaves the marks, they are money facts.
#
# Amounts are whole pesos, La Quiniela's rule (prizes_for): PLACE and SHOW
# rounded half up, WIN takes the rest, so the three sum to the pot (OD5).
#
# No House. A horse nobody bought has no ownership row, and a slot whose
# finisher has no owner is paid to nobody: it comes back `unowned` and the
# admin names the horse that pays it (the next finisher: La Quiniela's rule
# for an empty cup in the money). The lock warns about unsold horses first
# (bidding.unsold_horses), so this is a safety net and not the plan.

from typing import Any, Dict, List, Optional

from la_quiniela.betting import prizes_for
from la_subasta import field, settings
from la_subasta.config import EVENT_YEAR
from la_subasta.bidding import current_high_bid
from la_subasta.models import get_conn, write_txn

FINISHES = ("win", "place", "show")
NO_RESULTS = "No results yet: they are entered in SET WINNERS on the Control Center"


class PayoutError(Exception):
    """A payout request that cannot be done. `reason` is user-facing and
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


def compute_payout_amounts(total_pot: float) -> Dict[str, int]:
    """The three amounts for a pot, whole pesos that sum to it: La Quiniela's
    own split (prizes_for), PLACE and SHOW rounded half up, WIN the rest."""
    return prizes_for(total_pot, current_payout_pcts())


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


def results_now() -> Optional[Dict[str, Any]]:
    """The result as SET WINNERS recorded it, read through La Quiniela's
    board: {"win", "place", "show", "next"}, or None while there is none (or
    no board, where the stand-in field has no result to read)."""
    board = field.lq_board()
    return board.results() if board is not None else None


def pot_now(event_year: int = EVENT_YEAR) -> float:
    """The pot the payouts are split from: the winning bids of the ownership
    rows, less any voided by a scratch."""
    row = get_conn().execute(
        "SELECT COALESCE(SUM(winning_bid), 0) AS pot FROM ownership WHERE event_year = ? AND voided = 0",
        (event_year,),
    ).fetchone()
    return float(row["pot"])


def _slots(event_year: int) -> Dict[tuple, dict]:
    """What the admin did, by (finish, finisher)."""
    rows = get_conn().execute(
        """
        SELECT s.finish, s.horse_id, s.pays_horse_id, s.paid_bidder_id, s.paid_amount, s.paid_at,
               bd.identity AS paid_identity
          FROM payout_slots s
          LEFT JOIN bidders bd ON bd.id = s.paid_bidder_id
         WHERE s.event_year = ?
        """,
        (event_year,),
    ).fetchall()
    return {(r["finish"], r["horse_id"]): dict(r) for r in rows}


def _pesos(amount: Any) -> str:
    n = int(amount)
    return f"{n} peso" if n == 1 else f"{n} pesos"


def ledger(event_year: int = EVENT_YEAR) -> dict:
    """The ledger as it stands now, worked out from the result and the
    ownership: results (the three horses, or None), total_pot, amounts, and,
    once there is a result, payouts: a row per finish with the horse that
    finished there, pays_horse_id (the horse the admin named to pay it in
    its place, or None), the amount, the bidder paid (None, and `unowned`,
    while nobody is) and `paid`, the paid-out mark while it matches; unowned,
    the finishes with nobody to pay; paid_on_old_result, every mark that no
    longer matches (the result moved, the owner or the amount changed, no
    result now), each with a line saying who was paid what for which horse
    and what the slot pays now. What GET /api/admin/payouts serves."""
    result = results_now()
    pot = pot_now(event_year)
    amounts = compute_payout_amounts(pot)
    slots = _slots(event_year)
    the_field = field.current()
    rows: List[dict] = []
    if result is not None:
        for finish in FINISHES:
            horse = result[finish]
            rec = slots.get((finish, horse))
            owner = get_owner(horse, event_year)
            pays = None
            if rec is not None and rec["pays_horse_id"] is not None and rec["pays_horse_id"] in the_field:
                named = get_owner(rec["pays_horse_id"], event_year)
                if named is not None:
                    pays, owner = rec["pays_horse_id"], named
            amount = amounts[finish]
            paid = None
            if (rec is not None and rec["paid_at"] is not None and owner is not None
                    and rec["paid_bidder_id"] == owner["bidder_id"] and rec["paid_amount"] == amount):
                paid = {"bidder_id": rec["paid_bidder_id"], "bidder_identity": rec["paid_identity"],
                        "amount": rec["paid_amount"], "paid_at": rec["paid_at"]}
            rows.append({
                "finish": finish, "horse_id": horse, "pays_horse_id": pays, "amount": amount,
                "bidder_id": owner["bidder_id"] if owner else None,
                "bidder_identity": owner["identity"] if owner else None,
                "unowned": owner is None, "paid": paid,
            })
    by_finish = {r["finish"]: r for r in rows}
    stale: List[dict] = []
    for (finish, horse), rec in sorted(slots.items(), key=lambda item: (FINISHES.index(item[0][0]), item[0][1])):
        if rec["paid_at"] is None:
            continue
        row = by_finish.get(finish)
        if row is not None and row["horse_id"] == horse and row["paid"] is not None:
            continue
        name = finish.upper()
        if row is None:
            now = "there is no result now"
        elif row["unowned"]:
            now = f"{name} is now #{row['horse_id']}, which has no owner to pay"
        else:
            now = f"{name} now pays {row['bidder_identity']} {_pesos(row['amount'])} for #{row['horse_id']}"
        stale.append({
            "finish": finish, "horse_id": horse, "bidder_id": rec["paid_bidder_id"],
            "bidder_identity": rec["paid_identity"], "amount": rec["paid_amount"], "paid_at": rec["paid_at"],
            "line": f"{rec['paid_identity']} was paid {_pesos(rec['paid_amount'])} for {name} (#{horse}); {now}",
        })
    return {
        "results": {k: result[k] for k in FINISHES} if result is not None else None,
        "total_pot": pot,
        "amounts": amounts,
        "payouts": rows,
        "unowned": [r["finish"] for r in rows if r["unowned"]],
        "paid_on_old_result": stale,
    }


def set_slot_horse(finish: str, horse_id: int,
                   event_year: int = EVENT_YEAR) -> dict:
    """
    The admin names the horse that pays a slot whose finisher has no owner:
    the next finisher (La Quiniela's rule for an empty cup in the money). The
    slot is then paid to that horse's owner, for the amount it already has.
    Naming the horse that finished there puts the slot back as the result has
    it. A slot whose finisher has an owner pays them and is not pointed at
    another horse (Joey, 2026-10-28, review S1): that would send a paid-for
    horse's payout to someone else. The owners are read under the write lock.
    The pick is kept against the finisher the result names now, so a
    correction that changes the slot's horse sets it aside.

    The horse must be in the field and have an owner. PayoutError, with the
    status the route answers with, when it cannot be done: 400 for a bad
    finish or horse, 409 when no result is in yet, the finisher has an owner
    or the horse has no owner either. Returns the slot's ledger row.
    """
    if finish not in FINISHES:
        raise PayoutError("finish must be win, place or show")
    if not field.valid_number(horse_id):
        raise PayoutError(f"Invalid horse id: {horse_id}")
    result = results_now()
    if result is None:
        raise PayoutError(NO_RESULTS, status=409)
    finisher = result[finish]

    read = {}

    def read_field():                  # before sqlite's write lock (models.write_txn)
        read["field"] = field.current()

    with write_txn(prepare=read_field) as conn:
        the_field = read["field"]
        if horse_id not in the_field:
            raise PayoutError(the_field.refusal(horse_id))
        if horse_id != finisher and get_owner(finisher, event_year) is not None:
            raise PayoutError(
                f"{finish.upper()} is #{finisher}'s: it has an owner, so it pays them. "
                f"Only a slot nobody owns can be pointed at another horse", status=409)
        owner = get_owner(horse_id, event_year)
        if owner is None:
            raise PayoutError(
                f"#{horse_id} has no owner either: name a horse that was sold", status=409)
        conn.execute(
            "INSERT INTO payout_slots (finish, horse_id, pays_horse_id, event_year) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(finish, horse_id, event_year) DO UPDATE SET pays_horse_id = excluded.pays_horse_id",
            (finish, finisher, None if horse_id == finisher else horse_id, event_year),
        )
    return next(p for p in ledger(event_year)["payouts"] if p["finish"] == finish)


def mark_paid(finish: str, paid: bool, horse_id: Optional[int] = None,
              event_year: int = EVENT_YEAR) -> dict:
    """
    The paid-out mark on a slot. paid True: the slot's finisher as the result
    names it now was paid out: who (the ledger's bidder) and how much are
    recorded, with the time; marked again for the same bidder and amount the
    mark stands as it was, otherwise it is replaced. 409 with no result, or
    while nobody is to be paid it (name the horse that pays it first).

    paid False: the mark is cleared, on the finisher given (horse_id: a mark
    paid on an earlier result names its horse) or on the slot's current
    finisher; 409 with neither. Returns the ledger.
    """
    if finish not in FINISHES:
        raise PayoutError("finish must be win, place or show")
    if not isinstance(paid, bool):
        raise PayoutError("paid must be true or false")
    if horse_id is not None and not field.valid_number(horse_id):
        raise PayoutError(f"Invalid horse id: {horse_id}")
    row = next((r for r in ledger(event_year)["payouts"] if r["finish"] == finish), None)
    if paid:
        if row is None:
            raise PayoutError(NO_RESULTS, status=409)
        if row["unowned"]:
            raise PayoutError(f"{finish.upper()} has no owner to pay yet: name the horse that pays it first", status=409)
        with write_txn() as conn:
            conn.execute(
                "INSERT INTO payout_slots (finish, horse_id, paid_bidder_id, paid_amount, paid_at, event_year) "
                "VALUES (?, ?, ?, ?, datetime('now'), ?) "
                "ON CONFLICT(finish, horse_id, event_year) DO UPDATE SET "
                "paid_at = CASE WHEN paid_at IS NOT NULL AND paid_bidder_id = excluded.paid_bidder_id "
                "                    AND paid_amount = excluded.paid_amount THEN paid_at ELSE excluded.paid_at END, "
                "paid_bidder_id = excluded.paid_bidder_id, paid_amount = excluded.paid_amount",
                (finish, row["horse_id"], row["bidder_id"], row["amount"], event_year),
            )
    else:
        target = horse_id if horse_id is not None else (row["horse_id"] if row is not None else None)
        if target is None:
            raise PayoutError("No result now: say which horse's mark to clear (horse_id)", status=409)
        with write_txn() as conn:
            conn.execute(
                "UPDATE payout_slots SET paid_bidder_id = NULL, paid_amount = NULL, paid_at = NULL "
                "WHERE finish = ? AND horse_id = ? AND event_year = ?",
                (finish, target, event_year),
            )
            conn.execute(
                "DELETE FROM payout_slots WHERE finish = ? AND horse_id = ? AND event_year = ? "
                "AND pays_horse_id IS NULL AND paid_at IS NULL",
                (finish, target, event_year),
            )
    return ledger(event_year)

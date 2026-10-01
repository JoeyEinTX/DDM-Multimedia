# la_subasta/test_smoke.py - Phase 1 smoke test
#
# Run with: python -m la_subasta.test_smoke  (from the pi5/ dir)
#
# Verifies:
#   - Blueprint routes are registered under /la-subasta
#   - /api/state returns valid JSON
#   - Bidder registration + identity uniqueness
#   - Bid validation: too-high raise, max horses, self-bid, scratched horse
#   - Valid bids accepted
#   - Bid undo within 10s, rejected after
#   - Payout math: 60/25/15 of test pot
#   - The horses are La Quiniela's: names, program numbers 1-24 and the
#     field from its store (a real board on a memory-only store per test,
#     driven through the LQ admin page's own routes)

import io
import json
import os
import sys
import tempfile
import time
import traceback

# Force UTF-8 on stdout (Windows console defaults to cp1252, which can't
# encode the emojis and arrows the test prints).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass

# Make sure pi5/ is on sys.path when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from la_subasta import config as la_config

# Redirect DB to a temp file BEFORE any module captures it at import time.
_TMP_DB = tempfile.mktemp(prefix="la_subasta_smoke_", suffix=".db")
la_config.DB_PATH = _TMP_DB

from la_subasta import models  # noqa: E402
# models.py did `from la_subasta.config import DB_PATH` which bound a local
# name — so we also need to patch models.DB_PATH (used as default arg).
models.DB_PATH = _TMP_DB

from la_subasta import bidding, payouts, settings  # noqa: E402
from la_subasta.models import init_db, reset_db_for_tests  # noqa: E402
from la_subasta.state_machine import (  # noqa: E402
    AuctionState, transition, get_state, get_state_row,
)
from la_subasta.blueprint import la_subasta_bp, init_la_subasta  # noqa: E402
from la_subasta import field as ls_field  # noqa: E402
from la_quiniela import board as lq_board  # noqa: E402
from la_quiniela.horses import HORSE_COUNT as LQ_HORSE_COUNT, HorseStore  # noqa: E402

import logging  # noqa: E402
# init_board() without a bridge says so at WARNING; La Subasta's tests never
# have one.
logging.getLogger("la_quiniela.board").setLevel(logging.ERROR)

# The 2026 Kentucky Derby field in post order and the named also-eligibles.
# The Puma (#9) scratched before the Friday deadline and Ocelli ran as #22.
DERBY_2026 = [
    "Renegade", "Albus", "Intrepido", "Litmus Test", "Right to Party",
    "Commandment", "Danon Bourbon", "So Happy", "The Puma", "Wonder Dean",
    "Incredibolt", "Chief Wallabee", "Silent Tactic", "Potente", "Emerging Market",
    "Pavlovian", "Six Speed", "Further Ado", "Golden Tempo", "Fulleffort",
]
ALSO_ELIGIBLE_2026 = {21: "Great White", 22: "Ocelli", 23: "Robusta"}
NAMES_2026 = "\n".join(f"{n}. {name}" for n, name in
                        list(enumerate(DERBY_2026, 1)) + sorted(ALSO_ELIGIBLE_2026.items()))


# -----------------------------------------------------------------------------
# Tiny test runner (no pytest dependency)
# -----------------------------------------------------------------------------

_results = []


def _check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((status, name, detail))
    marker = "[OK]" if condition else "[XX]"
    print(f"  {marker} {name}" + (f"  -- {detail}" if detail and not condition else ""))
    return condition


def _run(name, fn):
    print(f"\n=== {name} ===")
    try:
        fn()
    except Exception as exc:
        traceback.print_exc()
        _check(f"{name} (uncaught exception)", False, str(exc))


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

_LQ_TMP = tempfile.mkdtemp(prefix="la_subasta_lq_")


def _make_app():
    """A Flask app with the la_subasta blueprint and La Quiniela's board
    routes (the admin page's names and scratches), over a fresh La Quiniela
    board with a memory-only store and no bridge: La Subasta reads its
    horses from that board's store, as it does in the app."""
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    lq_board.init_board(bridge=None, log_dir=os.path.join(_LQ_TMP, "logs"),
                        results_path=os.path.join(_LQ_TMP, "results.json"))
    ls_field.set_store_source(None)          # the board's store, as in the app
    init_la_subasta(socketio=None)
    app.register_blueprint(la_subasta_bp)
    app.register_blueprint(lq_board.quiniela_board_bp)
    return app


def _lq_names(client, text=None):
    r = client.put("/api/quiniela/horses", json={"text": text if text is not None else NAMES_2026})
    assert r.status_code == 200, r.get_json()


def _lq_scratch(client, horse, number=None, name=None):
    body = {"horse": horse}
    if number is not None:
        body["replacement"] = {"number": number, "name": name or ""}
    return client.post("/api/quiniela/scratch", json=body)


def _reset():
    reset_db_for_tests(_TMP_DB)
    # After reset, re-init blueprint wiring (DB connection changed)
    init_db(_TMP_DB)


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

def test_state_endpoint():
    _reset()
    app = _make_app()
    client = app.test_client()

    resp = client.get("/la-subasta/api/state")
    _check("GET /api/state returns 200", resp.status_code == 200,
           f"status={resp.status_code}")
    data = resp.get_json()
    _check("/api/state returns JSON dict", isinstance(data, dict))
    _check("/api/state has 'state' field", data and "state" in data)
    _check("/api/state initial state is NOT_STARTED",
           data and data.get("state") == "NOT_STARTED",
           f"got {data.get('state')}")
    _check("/api/state exposes emoji_palette",
           data and isinstance(data.get("emoji_palette"), list))


def test_state_endpoint_pot_data_shapes():
    """Regression for the /api/state 500 (sqlite InterfaceError via
    total_pot()). total_pot() loops every horse calling current_high_bid();
    confirm /api/state returns 200 with a correct pot across bid shapes:
    empty DB, some horses bid + some not (gaps), and all bids voided."""
    _reset()
    app = _make_app()
    client = app.test_client()

    # (1) No bids — empty DB
    r = client.get("/la-subasta/api/state")
    _check("no bids: /api/state 200", r.status_code == 200,
           f"status={r.status_code} body={r.get_json()}")
    _check("no bids: total_pot == 0", r.get_json().get("total_pot") == 0,
           f"got {r.get_json().get('total_pot')}")

    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]

    # (2) Some horses bid, most don't — the loop must skip the no-bid gaps
    bid_ids = []
    for bidder, horse, amt in [(alice, 1, 1), (bob, 2, 3), (alice, 5, 1)]:
        resp = client.post("/la-subasta/api/bid",
                           json={"bidder_id": bidder["id"], "horse_id": horse,
                                 "amount": amt})
        bid_ids.append(resp.get_json()["bid"]["bid_id"])
    r = client.get("/la-subasta/api/state")
    _check("some bids w/ gaps: /api/state 200", r.status_code == 200,
           f"status={r.status_code} body={r.get_json()}")
    _check("some bids w/ gaps: total_pot == 1+3+1 == 5",
           r.get_json().get("total_pot") == 5,
           f"got {r.get_json().get('total_pot')}")

    # (3) Void every bid → no active high bids → pot back to 0 cleanly
    #     (the shared-connection bug also produced 'float += None' here)
    for bid_id in bid_ids:
        vr = client.post("/la-subasta/api/admin/void", json={"bid_id": bid_id})
        assert vr.status_code == 200, vr.get_json()
    r = client.get("/la-subasta/api/state")
    _check("all bids voided: /api/state 200", r.status_code == 200,
           f"status={r.status_code} body={r.get_json()}")
    _check("all bids voided: total_pot == 0",
           r.get_json().get("total_pot") == 0,
           f"got {r.get_json().get('total_pot')}")


def test_state_endpoint_concurrent_no_sqlite_misuse():
    """Regression for the actual root cause: concurrent /api/state requests
    used to raise sqlite3 InterfaceError ('bad parameter or other API misuse')
    because every thread shared ONE sqlite connection (total_pot() fires ~20-40
    execute() calls per request). With per-thread connections, concurrent
    reads must all succeed. This test reliably reproduced the 500 before the
    fix."""
    import threading
    from la_subasta.models import close_conn
    _reset()
    app = _make_app()
    client = app.test_client()
    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
    client.post("/la-subasta/api/bid",
                json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1})
    client.post("/la-subasta/api/bid",
                json={"bidder_id": bob["id"], "horse_id": 2, "amount": 1})

    errors = []
    statuses = []

    def worker():
        try:
            c2 = app.test_client()
            for _ in range(120):
                statuses.append(c2.get("/la-subasta/api/state").status_code)
        except Exception as exc:                       # pragma: no cover
            errors.append(repr(exc))
        finally:
            close_conn()   # release this worker thread's sqlite handle

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _check("concurrent /api/state: no exceptions raised",
           not errors, f"errors: {errors[:3]}")
    _check("concurrent /api/state: every response was 200",
           bool(statuses) and all(s == 200 for s in statuses),
           f"non-200: {sorted(set(s for s in statuses if s != 200))}")


def test_register_endpoint():
    _reset()
    app = _make_app()
    client = app.test_client()

    resp = client.post("/la-subasta/api/register",
                       json={"name": "Dave K", "emoji": "🌮"})
    data = resp.get_json()
    _check("register creates bidder",
           resp.status_code == 200 and data.get("success"),
           f"status={resp.status_code} body={data}")
    _check("bidder has identity 'Dave K 🌮'",
           data and data.get("bidder", {}).get("identity") == "Dave K 🌮")

    # Duplicate name+emoji rejected
    resp2 = client.post("/la-subasta/api/register",
                        json={"name": "Dave K", "emoji": "🌮"})
    _check("duplicate name+emoji rejected with 409",
           resp2.status_code == 409,
           f"status={resp2.status_code} body={resp2.get_json()}")

    # Same name different emoji OK
    resp3 = client.post("/la-subasta/api/register",
                        json={"name": "Dave K", "emoji": "🐴"})
    _check("same name + different emoji accepted",
           resp3.status_code == 200 and resp3.get_json().get("success"))

    # Emoji not in palette rejected
    resp4 = client.post("/la-subasta/api/register",
                        json={"name": "Mallory", "emoji": "💀"})
    _check("unlisted emoji rejected",
           resp4.status_code == 409)


def test_bid_validation():
    _reset()
    app = _make_app()
    client = app.test_client()

    # Open the auction
    transition(AuctionState.OPEN)

    # Register two bidders
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]

    # --- reject bid when auction closed -------------------------------------
    transition(AuctionState.LOCKED, force=True)
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1})
    _check("bid rejected in LOCKED state", resp.status_code == 400)

    # Reopen for remaining tests
    transition(AuctionState.OPEN, force=True)

    # --- scratched horse rejected (scratched on the LQ admin page) ----------
    _lq_scratch(client, 5)
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 5, "amount": 1})
    _check("bid on scratched horse rejected",
           resp.status_code == 400
           and "scratch" in resp.get_json().get("error", "").lower())

    # --- opening bid below MIN_BID rejected ---------------------------------
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 1, "amount": 0})
    _check("bid below MIN_BID rejected", resp.status_code == 400)

    # --- opening bid above MIN_BID + MAX_RAISE rejected ---------------------
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 1, "amount": 100})
    _check("opening bid above MAX_RAISE rejected", resp.status_code == 400)

    # --- valid opening bid accepted -----------------------------------------
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1})
    data = resp.get_json()
    _check("valid opening bid accepted",
           resp.status_code == 200 and data.get("success"),
           f"body={data}")

    # --- can't outbid yourself ----------------------------------------------
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 1, "amount": 2})
    _check("can't outbid yourself",
           resp.status_code == 400
           and "already leading" in resp.get_json().get("error", "").lower())

    # --- raise too low rejected ---------------------------------------------
    # Current high = $1, need at least $2
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": bob["id"], "horse_id": 1, "amount": 1})
    _check("raise below current+1 rejected", resp.status_code == 400)

    # --- raise too high (>$5 over current) rejected ------------------------
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": bob["id"], "horse_id": 1, "amount": 10})
    _check("raise above MAX_RAISE rejected",
           resp.status_code == 400
           and "max raise" in resp.get_json().get("error", "").lower())

    # --- valid raise accepted -----------------------------------------------
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": bob["id"], "horse_id": 1, "amount": 2})
    _check("valid +$1 raise accepted",
           resp.status_code == 200 and resp.get_json().get("success"))

    # --- Max horses check: put Alice on 3 horses, then try a 4th -----------
    # Alice was outbid on #1, so she's currently leading 0 horses.
    for h in (2, 3, 4):
        r = client.post("/la-subasta/api/bid",
                        json={"bidder_id": alice["id"], "horse_id": h, "amount": 1})
        assert r.status_code == 200, f"setup bid #{h} failed: {r.get_json()}"

    leading = bidding.horses_leading_by(alice["id"])
    _check("Alice leading on exactly 3 horses", len(leading) == 3,
           f"got {leading}")

    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 6, "amount": 1})
    _check("4th horse rejected (max 3)",
           resp.status_code == 400
           and "max" in resp.get_json().get("error", "").lower())


def test_bid_undo():
    _reset()
    app = _make_app()
    client = app.test_client()
    transition(AuctionState.OPEN)

    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]

    # Place a bid, undo immediately → should succeed
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1})
    bid_id = resp.get_json()["bid"]["bid_id"]

    resp = client.post("/la-subasta/api/bid/undo",
                       json={"bid_id": bid_id, "bidder_id": alice["id"]})
    _check("undo within window accepted",
           resp.status_code == 200 and resp.get_json().get("success"),
           f"body={resp.get_json()}")

    # After undo, no high bid should remain
    hb = bidding.current_high_bid(1)
    _check("no high bid after undo", hb is None)

    # Place another bid, backdate it, then try to undo
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": alice["id"], "horse_id": 2, "amount": 1})
    bid_id2 = resp.get_json()["bid"]["bid_id"]

    # Simulate the clock moving past the undo window
    from la_subasta.models import get_conn
    conn = get_conn()
    conn.execute(
        "UPDATE bids SET bid_time = datetime('now', '-60 seconds') WHERE id = ?",
        (bid_id2,),
    )

    resp = client.post("/la-subasta/api/bid/undo",
                       json={"bid_id": bid_id2, "bidder_id": alice["id"]})
    _check("undo after window rejected",
           resp.status_code == 400
           and "window" in resp.get_json().get("error", "").lower(),
           f"body={resp.get_json()}")

    # Another bidder trying to undo someone else's bid
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
    resp = client.post("/la-subasta/api/bid",
                       json={"bidder_id": bob["id"], "horse_id": 3, "amount": 1})
    bid_id3 = resp.get_json()["bid"]["bid_id"]
    resp = client.post("/la-subasta/api/bid/undo",
                       json={"bid_id": bid_id3, "bidder_id": alice["id"]})
    _check("undo someone else's bid rejected",
           resp.status_code == 400
           and "own bids" in resp.get_json().get("error", "").lower())


def test_payouts():
    _reset()
    app = _make_app()
    client = app.test_client()
    transition(AuctionState.OPEN)

    # Build a deterministic pot: 3 bidders, 3 horses, known amounts.
    # Alice leads horse 1 @ $5  (will finish 1st → win)
    # Bob   leads horse 2 @ $3  (will finish 2nd → place)
    # Carol leads horse 3 @ $2  (will finish 3rd → show)
    # Total pot = $10. Win=$6, Place=$2.50, Show=$1.50.
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
    carol = client.post("/la-subasta/api/register",
                        json={"name": "Carol", "emoji": "💃"}).get_json()["bidder"]

    def bid(bidder, horse, amount):
        r = client.post("/la-subasta/api/bid",
                        json={"bidder_id": bidder["id"], "horse_id": horse,
                              "amount": amount})
        assert r.status_code == 200, r.get_json()

    # Alice: $1 → $5 on horse 1 (she's alone)
    bid(alice, 1, 1); bid(bob, 1, 2); bid(alice, 1, 3); bid(bob, 1, 4); bid(alice, 1, 5)
    # Bob on horse 2 at $3
    bid(bob, 2, 1); bid(carol, 2, 2); bid(bob, 2, 3)
    # Carol on horse 3 at $2
    bid(carol, 3, 1); bid(alice, 3, 2); bid(carol, 3, 3)
    # Wait - bidding higher than 3 is fine since max raise is 5
    # Actually correcting: horse 3 final is Carol @ $3

    pot = bidding.total_pot()
    _check("total pot = 5 + 3 + 3 = 11", abs(pot - 11.0) < 0.001,
           f"pot={pot}")

    # Pure math check with known pot
    amounts = payouts.compute_payout_amounts(pot)
    _check("win payout = pot * 0.60",
           abs(amounts["win"] - round(pot * 0.60, 2)) < 0.001)
    _check("place payout = pot * 0.25",
           abs(amounts["place"] - round(pot * 0.25, 2)) < 0.001)
    _check("show payout = pot * 0.15",
           abs(amounts["show"] - round(pot * 0.15, 2)) < 0.001)
    _check("payouts sum = pot",
           abs(amounts["win"] + amounts["place"] + amounts["show"] - pot) < 0.01)

    # End-to-end via admin endpoint: lock + enter results
    resp = client.post("/la-subasta/api/admin/lock")
    _check("lock endpoint succeeds",
           resp.status_code == 200 and resp.get_json().get("success"),
           f"body={resp.get_json()}")

    resp = client.post("/la-subasta/api/admin/results",
                       json={"win": 1, "place": 2, "show": 3})
    data = resp.get_json()
    _check("results endpoint succeeds",
           resp.status_code == 200 and data.get("success"),
           f"body={data}")
    _check("settled state reached after results",
           data.get("state") == "SETTLED")
    _check("end-to-end total pot matches",
           abs(data["total_pot"] - pot) < 0.001,
           f"got {data.get('total_pot')}")

    # Verify recorded payouts
    ls = payouts.list_payouts()
    by_finish = {p["finish"]: p for p in ls}
    _check("win payout recorded for Alice (horse 1)",
           by_finish["win"]["bidder_id"] == alice["id"]
           and by_finish["win"]["horse_id"] == 1)
    _check("place payout recorded for Bob (horse 2)",
           by_finish["place"]["bidder_id"] == bob["id"]
           and by_finish["place"]["horse_id"] == 2)
    _check("show payout recorded for Carol (horse 3)",
           by_finish["show"]["bidder_id"] == carol["id"]
           and by_finish["show"]["horse_id"] == 3)

    _check("win amount matches 60% of pot",
           abs(by_finish["win"]["amount"] - round(pot * 0.60, 2)) < 0.01)
    _check("place amount matches 25% of pot",
           abs(by_finish["place"]["amount"] - round(pot * 0.25, 2)) < 0.01)
    _check("show amount matches 15% of pot",
           abs(by_finish["show"]["amount"] - round(pot * 0.15, 2)) < 0.01)


def test_house_payout():
    """Zero-bid horse wins → House receives the payout."""
    _reset()
    app = _make_app()
    client = app.test_client()
    transition(AuctionState.OPEN)

    # Two real bidders, two bid-on horses, one untouched horse (3) that goes
    # to The House when results are entered.
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]

    r = client.post("/la-subasta/api/bid",
                    json={"bidder_id": alice["id"], "horse_id": 1, "amount": 5})
    assert r.status_code == 200, r.get_json()
    r = client.post("/la-subasta/api/bid",
                    json={"bidder_id": bob["id"], "horse_id": 2, "amount": 3})
    assert r.status_code == 200, r.get_json()

    pot = bidding.total_pot()
    _check("total pot ignores zero-bid horse",
           abs(pot - 8.0) < 0.001, f"pot={pot}")

    # /api/bidders must hide the House row by default
    bidders_default = client.get("/la-subasta/api/bidders").get_json()["bidders"]
    identities = {b["identity"] for b in bidders_default}
    _check("/api/bidders hides House by default",
           "The House 🎩" not in identities,
           f"got {identities}")

    # ?include_house=true surfaces it for admin/debug
    bidders_with_house = client.get(
        "/la-subasta/api/bidders?include_house=true"
    ).get_json()["bidders"]
    identities_w = {b["identity"] for b in bidders_with_house}
    _check("/api/bidders?include_house=true reveals House",
           "The House 🎩" in identities_w, f"got {identities_w}")

    # Lock + enter results with the zero-bid horse winning
    r = client.post("/la-subasta/api/admin/lock")
    assert r.status_code == 200, r.get_json()

    r = client.post("/la-subasta/api/admin/results",
                    json={"win": 3, "place": 1, "show": 2})
    data = r.get_json()
    _check("House-win results endpoint succeeds",
           r.status_code == 200 and data.get("success"), f"body={data}")

    # House row exists and has the expected id
    from la_subasta.models import house_bidder_id
    hid = house_bidder_id()
    _check("House bidder id is a positive int", isinstance(hid, int) and hid > 0,
           f"got {hid}")

    by_finish = {p["finish"]: p for p in data["payouts"]}
    _check("win payout goes to the House (bidder_id matches)",
           by_finish["win"]["bidder_id"] == hid,
           f"got bidder_id={by_finish['win']['bidder_id']}, house={hid}")
    _check("win payout flagged is_house=True",
           by_finish["win"]["is_house"] is True)
    _check("place payout goes to Alice (owner of horse 1)",
           by_finish["place"]["bidder_id"] == alice["id"]
           and by_finish["place"]["is_house"] is False)
    _check("show payout goes to Bob (owner of horse 2)",
           by_finish["show"]["bidder_id"] == bob["id"]
           and by_finish["show"]["is_house"] is False)

    expected_win = round(pot * 0.60, 2)
    _check("House win amount = 60% of pot",
           abs(by_finish["win"]["amount"] - expected_win) < 0.01,
           f"got {by_finish['win']['amount']} expected {expected_win}")

    # Verify the persisted payout row (not just the response) references House
    ls = payouts.list_payouts()
    win_row = next(p for p in ls if p["finish"] == "win")
    _check("persisted win payout row has House bidder_id",
           win_row["bidder_id"] == hid)
    _check("persisted win payout row has House identity via JOIN",
           win_row["bidder_identity"] == "The House 🎩",
           f"got {win_row['bidder_identity']}")


# -----------------------------------------------------------------------------
# Phase 1.5: admin-tunable settings
# -----------------------------------------------------------------------------

def test_settings_get_defaults():
    _reset()
    _make_app()

    # Every key in DEFAULTS returns its default when no override exists
    from la_subasta import config as la_config
    for key, default in la_config.DEFAULTS.items():
        _check(f"get_setting({key}) returns default",
               settings.get_setting(key) == default,
               f"got {settings.get_setting(key)!r}, expected {default!r}")

    # get_all_settings shape
    all_s = settings.get_all_settings()
    _check("get_all_settings has all 7 keys",
           set(all_s.keys()) == set(la_config.DEFAULTS.keys()),
           f"got {set(all_s.keys())}")
    _check("settings marked is_override=False when unset",
           all(not info["is_override"] for info in all_s.values()))


def test_settings_set_and_audit():
    _reset()
    _make_app()

    # Change a setting while auction is NOT_STARTED (unlocked)
    result = settings.set_setting("MAX_RAISE", 10, changed_by="smoke")
    _check("set_setting returns new_value", result["new_value"] == 10)
    _check("set_setting returns old_value as default",
           result["old_value"] == 5)
    _check("get_setting picks up override",
           settings.get_setting("MAX_RAISE") == 10)

    # Audit entry exists
    audit = settings.get_audit_log(limit=10)
    _check("audit log has the MAX_RAISE change",
           any(e["setting_key"] == "MAX_RAISE" and e["new_value"] == "10"
               for e in audit))
    _check("audit entry records changed_by",
           audit[0].get("changed_by") == "smoke")
    _check("audit entry records auction_state_at_change",
           audit[0].get("auction_state_at_change") == "NOT_STARTED")


def test_settings_validation():
    _reset()
    _make_app()

    cases = [
        # (key, bad_value, description)
        ("MAX_RAISE",                    0,        "below range"),
        ("MAX_RAISE",                    51,       "above range"),
        ("MAX_RAISE",                    "abc",    "non-int"),
        ("MAX_HORSES_PER_BIDDER",        0,        "below range"),
        ("MAX_HORSES_PER_BIDDER",        11,       "above range"),
        ("MIN_BID",                      0,        "below range"),
        ("MIN_BID",                      21,       "above range"),
        ("LOCKDOWN_MINUTES_BEFORE_POST", 4,        "below range"),
        ("LOCKDOWN_MINUTES_BEFORE_POST", 61,       "above range"),
        ("PAYOUT_PRESET",                "80/10/10", "not in options"),
        ("PAYOUT_PRESET",                "bogus",  "malformed"),
        ("HOUSE_FUND_LABEL",             "",       "empty"),
        ("HOUSE_FUND_LABEL",             "x" * 41, "too long"),
        ("AUCTION_OPEN_TIME",            "9:00",   "missing leading zero"),
        ("AUCTION_OPEN_TIME",            "24:00",  "invalid hour"),
        ("AUCTION_OPEN_TIME",            "noon",   "non-numeric"),
    ]
    for key, value, desc in cases:
        try:
            settings.set_setting(key, value)
            _check(f"reject {key}={value!r} ({desc})", False,
                   "no error raised")
        except settings.SettingsError:
            _check(f"reject {key}={value!r} ({desc})", True)

    # Valid values from each preset succeed
    for preset in ("60/25/15", "70/20/10", "50/30/20"):
        try:
            settings.set_setting("PAYOUT_PRESET", preset)
            _check(f"accept valid preset {preset}", True)
        except settings.SettingsError as exc:
            _check(f"accept valid preset {preset}", False, str(exc))


def test_settings_lock_when_open():
    _reset()
    _make_app()

    # Unlocked settings editable in NOT_STARTED
    settings.set_setting("HOUSE_FUND_LABEL", "Test Fund")
    settings.set_setting("MAX_RAISE", 7)

    # Open the auction
    transition(AuctionState.OPEN)

    # Locked setting now rejected with a clear error
    try:
        settings.set_setting("MAX_RAISE", 8)
        _check("locked MAX_RAISE rejected while OPEN", False,
               "no error raised")
    except settings.SettingsError as exc:
        _check("locked MAX_RAISE rejected while OPEN", True)
        _check("error message names the setting",
               "MAX_RAISE" in exc.reason and "OPEN" in exc.reason,
               f"got {exc.reason!r}")

    # MIN_BID, MAX_HORSES_PER_BIDDER, PAYOUT_PRESET, AUCTION_OPEN_TIME also locked
    for key, val in [("MIN_BID", 2), ("MAX_HORSES_PER_BIDDER", 5),
                     ("PAYOUT_PRESET", "70/20/10"), ("AUCTION_OPEN_TIME", "10:00")]:
        try:
            settings.set_setting(key, val)
            _check(f"locked {key} rejected while OPEN", False)
        except settings.SettingsError:
            _check(f"locked {key} rejected while OPEN", True)

    # Unlocked settings still editable while OPEN
    try:
        settings.set_setting("HOUSE_FUND_LABEL", "Still-Editable")
        _check("HOUSE_FUND_LABEL editable while OPEN", True)
    except settings.SettingsError as exc:
        _check("HOUSE_FUND_LABEL editable while OPEN", False, str(exc))

    try:
        settings.set_setting("LOCKDOWN_MINUTES_BEFORE_POST", 20)
        _check("LOCKDOWN_MINUTES_BEFORE_POST editable while OPEN", True)
    except settings.SettingsError as exc:
        _check("LOCKDOWN_MINUTES_BEFORE_POST editable while OPEN",
               False, str(exc))

    # get_all_settings reflects locked_now state
    all_s = settings.get_all_settings()
    _check("MAX_RAISE reports locked_now=True while OPEN",
           all_s["MAX_RAISE"]["locked_now"] is True)
    _check("HOUSE_FUND_LABEL reports locked_now=False while OPEN",
           all_s["HOUSE_FUND_LABEL"]["locked_now"] is False)


def test_settings_reset():
    _reset()
    _make_app()

    settings.set_setting("MAX_RAISE", 15)
    settings.set_setting("HOUSE_FUND_LABEL", "Custom")
    _check("overrides present pre-reset",
           settings.get_setting("MAX_RAISE") == 15
           and settings.get_setting("HOUSE_FUND_LABEL") == "Custom")

    count = settings.reset_to_defaults()
    _check("reset_to_defaults returns count of reset keys", count == 2,
           f"got {count}")

    # All values back to defaults
    from la_subasta import config as la_config
    for key, default in la_config.DEFAULTS.items():
        _check(f"{key} returned to default after reset",
               settings.get_setting(key) == default)

    # Reset action appended to audit log
    audit = settings.get_audit_log(limit=20)
    reset_entries = [e for e in audit
                     if e["setting_key"] in ("MAX_RAISE", "HOUSE_FUND_LABEL")
                     and e["new_value"] in ("5", "DDM 2027 Build Fund")]
    _check("reset logged audit entries for each changed key",
           len(reset_entries) >= 2, f"got {len(reset_entries)}")


def test_bidding_picks_up_max_raise_change():
    """Change MAX_RAISE at runtime → next bid reflects the new limit."""
    _reset()
    app = _make_app()
    client = app.test_client()

    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]

    # Tighten MAX_RAISE to 2 BEFORE opening the auction (locked-when-open)
    settings.set_setting("MAX_RAISE", 2)
    transition(AuctionState.OPEN)

    # Opening bid at $1, then a +$3 raise should now be rejected
    r = client.post("/la-subasta/api/bid",
                    json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1})
    assert r.status_code == 200, r.get_json()

    r = client.post("/la-subasta/api/bid",
                    json={"bidder_id": bob["id"], "horse_id": 1, "amount": 4})
    _check("bid of +$3 rejected after MAX_RAISE lowered to 2",
           r.status_code == 400 and "max raise" in r.get_json()["error"].lower(),
           f"got {r.get_json()}")

    # +$2 still accepted
    r = client.post("/la-subasta/api/bid",
                    json={"bidder_id": bob["id"], "horse_id": 1, "amount": 3})
    _check("bid of +$2 accepted at new MAX_RAISE=2",
           r.status_code == 200, f"got {r.get_json()}")


def test_payout_preset_parser():
    # Valid presets
    for preset, expected in [
        ("60/25/15", {"win": 0.60, "place": 0.25, "show": 0.15}),
        ("70/20/10", {"win": 0.70, "place": 0.20, "show": 0.10}),
        ("50/30/20", {"win": 0.50, "place": 0.30, "show": 0.20}),
    ]:
        parsed = payouts.parse_payout_preset(preset)
        ok = all(abs(parsed[k] - expected[k]) < 1e-6 for k in expected)
        _check(f"parse_payout_preset({preset!r}) correct", ok,
               f"got {parsed}")

    # Malformed inputs
    bad = ["", "60/25", "60/25/15/0", "sixty/25/15", "-10/55/55", "60-25-15", None]
    for v in bad:
        try:
            payouts.parse_payout_preset(v)
            _check(f"reject malformed preset {v!r}", False,
                   "no error raised")
        except (ValueError, TypeError):
            _check(f"reject malformed preset {v!r}", True)

    # Sum != 100 rejected
    try:
        payouts.parse_payout_preset("70/20/20")  # sums to 110
        _check("reject preset with sum != 100", False)
    except ValueError:
        _check("reject preset with sum != 100", True)


def test_payout_uses_current_preset():
    """Each of the 3 presets yields the right split on a known pot."""
    for preset, (w_pct, p_pct, s_pct) in [
        ("60/25/15", (0.60, 0.25, 0.15)),
        ("70/20/10", (0.70, 0.20, 0.10)),
        ("50/30/20", (0.50, 0.30, 0.20)),
    ]:
        _reset()
        _make_app()

        # Set preset BEFORE opening (it's locked-when-open)
        settings.set_setting("PAYOUT_PRESET", preset)
        transition(AuctionState.OPEN)

        app_client = _make_app().test_client()
        alice = app_client.post("/la-subasta/api/register",
                                json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
        bob = app_client.post("/la-subasta/api/register",
                              json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
        carol = app_client.post("/la-subasta/api/register",
                                json={"name": "Carol", "emoji": "💃"}).get_json()["bidder"]

        def bid(b, h, a):
            r = app_client.post("/la-subasta/api/bid",
                                json={"bidder_id": b["id"], "horse_id": h, "amount": a})
            assert r.status_code == 200, r.get_json()

        # Pot of $10 = Alice $5 / Bob $3 / Carol $2 (walk the bid ladder)
        bid(alice, 1, 1); bid(bob, 1, 2); bid(alice, 1, 3); bid(bob, 1, 4); bid(alice, 1, 5)
        bid(bob, 2, 1); bid(carol, 2, 2); bid(bob, 2, 3)
        bid(carol, 3, 1); bid(alice, 3, 2)

        pot = bidding.total_pot()
        app_client.post("/la-subasta/api/admin/lock")
        r = app_client.post("/la-subasta/api/admin/results",
                            json={"win": 1, "place": 2, "show": 3})
        data = r.get_json()
        by_finish = {p["finish"]: p for p in data["payouts"]}

        _check(f"[{preset}] win = {w_pct:.0%} of pot",
               abs(by_finish["win"]["amount"] - round(pot * w_pct, 2)) < 0.01,
               f"got {by_finish['win']['amount']} expected {round(pot * w_pct, 2)}")
        _check(f"[{preset}] place = {p_pct:.0%} of pot",
               abs(by_finish["place"]["amount"] - round(pot * p_pct, 2)) < 0.01)
        _check(f"[{preset}] show = {s_pct:.0%} of pot",
               abs(by_finish["show"]["amount"] - round(pot * s_pct, 2)) < 0.01)


def test_settings_api_endpoints():
    _reset()
    app = _make_app()
    client = app.test_client()

    # GET /api/admin/settings
    r = client.get("/la-subasta/api/admin/settings")
    data = r.get_json()
    _check("GET /api/admin/settings returns 200", r.status_code == 200)
    _check("GET /api/admin/settings has 7 keys",
           len(data["settings"]) == 7)
    _check("GET /api/admin/settings includes state field",
           data.get("state") == "NOT_STARTED")

    # POST /api/admin/settings (valid)
    r = client.post("/la-subasta/api/admin/settings",
                    json={"key": "HOUSE_FUND_LABEL", "value": "ApiTest"})
    data = r.get_json()
    _check("POST /api/admin/settings (valid) returns 200",
           r.status_code == 200 and data.get("success"))
    _check("POST returns change payload",
           data["change"]["key"] == "HOUSE_FUND_LABEL"
           and data["change"]["new_value"] == "ApiTest")

    # POST /api/admin/settings (invalid validation → 400)
    r = client.post("/la-subasta/api/admin/settings",
                    json={"key": "MAX_RAISE", "value": 999})
    _check("POST /api/admin/settings (invalid) returns 400",
           r.status_code == 400)

    # POST /api/admin/settings (locked-when-open → 409)
    transition(AuctionState.OPEN)
    r = client.post("/la-subasta/api/admin/settings",
                    json={"key": "MAX_RAISE", "value": 7})
    _check("POST /api/admin/settings (locked while OPEN) returns 409",
           r.status_code == 409)

    # GET /api/admin/settings/audit
    r = client.get("/la-subasta/api/admin/settings/audit?limit=5")
    data = r.get_json()
    _check("GET /api/admin/settings/audit returns 200",
           r.status_code == 200 and data.get("success"))
    _check("audit endpoint returns a list",
           isinstance(data.get("audit"), list))
    _check("audit endpoint records recent HOUSE_FUND_LABEL change",
           any(e["setting_key"] == "HOUSE_FUND_LABEL" for e in data["audit"]))

    # POST /api/admin/settings/reset
    r = client.post("/la-subasta/api/admin/settings/reset")
    data = r.get_json()
    _check("POST /api/admin/settings/reset returns 200",
           r.status_code == 200 and data.get("success"))
    _check("reset_count >= 1", data.get("reset_count") >= 1)
    # Verify HOUSE_FUND_LABEL back to default
    r = client.get("/la-subasta/api/admin/settings")
    vals = r.get_json()["settings"]
    _check("HOUSE_FUND_LABEL restored to default after reset",
           vals["HOUSE_FUND_LABEL"]["value"] == "DDM 2027 Build Fund"
           and vals["HOUSE_FUND_LABEL"]["is_override"] is False)


def test_settings_changed_socketio_broadcast():
    _reset()

    # Stand up a capture stub that records every SocketIO emit call
    events = []

    class _StubSocketIO:
        def emit(self, event, payload, room=None):
            events.append((event, payload, room))

    from la_subasta import notifications as nots
    nots.init_notifications(_StubSocketIO())

    # Re-make the app so the blueprint wiring sees the stub
    _make_app()
    # Re-register stub since _make_app called init_la_subasta(socketio=None)
    nots.init_notifications(_StubSocketIO())
    events.clear()

    settings.set_setting("HOUSE_FUND_LABEL", "BroadcastTest")
    # Trigger a notification the same way the blueprint does
    nots.settings_changed(
        key="HOUSE_FUND_LABEL",
        old_value="DDM 2027 Build Fund",
        new_value="BroadcastTest",
        changed_at=None,
    )

    _check("settings_changed event emitted",
           any(e[0] == "settings_changed" for e in events))
    payload = next(e[1] for e in events if e[0] == "settings_changed")
    _check("settings_changed payload has key",
           payload.get("key") == "HOUSE_FUND_LABEL")
    _check("settings_changed payload has old_value",
           payload.get("old_value") == "DDM 2027 Build Fund")
    _check("settings_changed payload has new_value",
           payload.get("new_value") == "BroadcastTest")

    # Reset notifications wiring so later tests aren't affected
    nots.init_notifications(None)


# -----------------------------------------------------------------------------
# Phase 2A: guest UI
# -----------------------------------------------------------------------------

def test_guest_page_served():
    _reset()
    app = _make_app()
    client = app.test_client()

    r = client.get("/la-subasta/")
    _check("GET /la-subasta/ returns 200", r.status_code == 200,
           f"status={r.status_code}")

    html = r.get_data(as_text=True)

    # Content-Type should be HTML, not JSON
    _check("response is HTML",
           r.mimetype == "text/html",
           f"mimetype={r.mimetype}")

    # Core structural elements the JS / CSS hooks into
    required_markers = [
        ('<div id="identity-modal"',             "identity modal container"),
        ('id="ls-name-input"',                   "name input"),
        ('id="ls-emoji-grid"',                   "emoji grid"),
        ('id="ls-submit-btn"',                   "submit button"),
        ('id="ls-app"',                          "main app section"),
        ('id="ls-horse-list"',                   "horse list"),
        ('id="ls-identity-display"',             "identity badge"),
        ('id="ls-countdown"',                    "countdown strip"),
        ('id="ls-locked-banner"',                "locked banner"),
        ('id="ls-custom-bid-modal"',             "custom bid modal"),
        ('la-subasta-mobile.css',                "mobile stylesheet link"),
        ('guest.js',                             "guest script tag"),
        ('socket.io',                            "socket.io client"),
        ('Derby de Mayo', "two-line brand top"),
        ('La Subasta',    "two-line brand bottom"),
    ]
    for marker, label in required_markers:
        _check(f"HTML contains {label}", marker in html,
               f"missing marker: {marker!r}")

    # Every emoji from the palette must render in the grid
    from la_subasta.config import EMOJI_PALETTE
    for emoji in EMOJI_PALETTE:
        _check(f"emoji palette includes {emoji}",
               f'data-emoji="{emoji}"' in html)


def test_horses_endpoint_shape():
    _reset()
    app = _make_app()
    client = app.test_client()

    r = client.get("/la-subasta/api/horses")
    _check("GET /api/horses returns 200", r.status_code == 200)

    data = r.get_json()
    _check("/api/horses returns success=True", data.get("success") is True)
    horses = data.get("horses")
    _check("/api/horses returns a list",
           isinstance(horses, list) and len(horses) == 20,
           f"got {len(horses) if isinstance(horses, list) else type(horses).__name__}")

    required_fields = ["horse_id", "saddle_cloth", "name", "jockey",
                       "saddle_cloth_color", "scratched",
                       "current_high_bid",
                       "current_leader_identity",
                       "current_leader_bidder_id"]
    for idx, horse in enumerate(horses):
        missing = [f for f in required_fields if f not in horse]
        if missing:
            _check(f"horse {idx+1} has all required fields", False,
                   f"missing: {missing}")
            break
    else:
        _check("every horse has all required fields", True)

    # Unbid horses have None leader fields
    sample = horses[0]
    _check("unbid horse: current_high_bid is None",
           sample["current_high_bid"] is None)
    _check("unbid horse: current_leader_identity is None",
           sample["current_leader_identity"] is None)
    _check("unbid horse: current_leader_bidder_id is None",
           sample["current_leader_bidder_id"] is None)

    # After a real bid, the leader fields populate
    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    client.post("/la-subasta/api/bid",
                json={"bidder_id": alice["id"], "horse_id": 7, "amount": 3})

    r = client.get("/la-subasta/api/horses")
    horses = r.get_json()["horses"]
    horse7 = next(h for h in horses if h["horse_id"] == 7)
    _check("bid horse: current_high_bid.amount == 3",
           horse7["current_high_bid"]["amount"] == 3)
    _check("bid horse: current_leader_identity matches bidder",
           horse7["current_leader_identity"] == "Alice 🌮")
    _check("bid horse: current_leader_bidder_id matches bidder",
           horse7["current_leader_bidder_id"] == alice["id"])


def test_field_from_lq_store_with_replacement():
    """The horse list is La Quiniela's field: the names pasted on the LQ
    admin page, the 2026 field with The Puma (#9) scratched and Ocelli
    running as #22. 22 is listed under its own number, 9 is absent, the list
    is sorted by number, names upper-cased, colours from the board's table."""
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    r = _lq_scratch(client, 9, 22, "Ocelli")
    _check("LQ admin: 9 -> 22 recorded", r.status_code == 200, f"body={r.get_json()}")

    horses = client.get("/la-subasta/api/horses").get_json()["horses"]
    numbers = [h["horse_id"] for h in horses]
    expected = [n for n in range(1, 21) if n != 9] + [22]
    _check("field: 1-8, 10-20 and 22, sorted by number", numbers == expected, f"got {numbers}")
    _check("field: 9 (The Puma) is absent", 9 not in numbers)
    by_id = {h["horse_id"]: h for h in horses}
    h22 = by_id.get(22) or {}
    _check("field: 22 is OCELLI", h22.get("name") == "OCELLI", f"got {h22.get('name')!r}")
    _check("field: 22's saddle cloth is its own number", h22.get("saddle_cloth") == 22)
    _check("field: 22 replaces 9", h22.get("replaces") == 9, f"got {h22.get('replaces')!r}")
    _check("field: 22's cloth is the board's (#008080, white digits)",
           (h22.get("saddle_cloth_color"), h22.get("saddle_cloth_text_color")) == ("#008080", "#FFFFFF"),
           f"got {h22.get('saddle_cloth_color')}, {h22.get('saddle_cloth_text_color')}")
    _check("field: names upper-cased as the board shows them",
           by_id[1]["name"] == "RENEGADE" and by_id[19]["name"] == "GOLDEN TEMPO",
           f"got {by_id[1]['name']!r}, {by_id[19]['name']!r}")
    _check("field: 1's cloth is red with white digits",
           (by_id[1]["saddle_cloth_color"], by_id[1]["saddle_cloth_text_color"]) == ("#E31837", "#FFFFFF"))
    _check("field: a horse in its own post replaces nobody", by_id[1]["replaces"] is None)
    _check("field: the also-eligibles not drawn in (21, 23) are not listed",
           21 not in numbers and 23 not in numbers)
    state = client.get("/la-subasta/api/state").get_json()
    _check("/api/state num_horses is the field's size (20)", state.get("num_horses") == 20,
           f"got {state.get('num_horses')}")
    html = client.get("/la-subasta/").get_data(as_text=True)
    _check("guest footer counts the field", "20 horses" in html)

    # The board's model agrees, horse by horse
    model = client.get("/api/quiniela").get_json()
    in_field = sorted(int(n) for n, h in model["horses"].items() if h["in_field"])
    _check("the same field the LQ board shows", in_field == numbers, f"board {in_field}")

    # A name change on the admin page is on the next read
    client.put("/api/quiniela/horses", json={"22": {"name": "Ocelli II"}})
    horses = client.get("/la-subasta/api/horses").get_json()["horses"]
    _check("a renamed horse reads its new name",
           next(h for h in horses if h["horse_id"] == 22)["name"] == "OCELLI II")

    # Undo the replacement: 9 is back, 22 gone
    r = client.post("/api/quiniela/unscratch", json={"horse": 9})
    _check("LQ admin: undo 9 -> 22", r.status_code == 200, f"body={r.get_json()}")
    numbers = [h["horse_id"] for h in client.get("/la-subasta/api/horses").get_json()["horses"]]
    _check("after the undo the field is 1-20 again", numbers == list(range(1, 21)), f"got {numbers}")


def test_field_horse_n_fallback():
    """A horse with no name stored reads HORSE n; with no names at all the
    field is 1-20, every one HORSE n."""
    _reset()
    app = _make_app()
    client = app.test_client()
    horses = client.get("/la-subasta/api/horses").get_json()["horses"]
    _check("no names: twenty horses", len(horses) == 20, f"got {len(horses)}")
    _check("no names: every horse is HORSE n",
           all(h["name"] == f"HORSE {h['horse_id']}" for h in horses),
           f"got {[h['name'] for h in horses][:3]}")
    _lq_names(client, "1. renegade\n2. Albus")
    by_id = {h["horse_id"]: h for h in client.get("/la-subasta/api/horses").get_json()["horses"]}
    _check("a lower-case name is served upper-cased", by_id[1]["name"] == "RENEGADE")
    _check("an unnamed horse beside named ones is HORSE n", by_id[3]["name"] == "HORSE 3")
    # A replacement entered with no name and none stored
    r = _lq_scratch(client, 4, 24)
    _check("LQ admin: 4 -> 24 with no name", r.status_code == 200, f"body={r.get_json()}")
    by_id = {h["horse_id"]: h for h in client.get("/la-subasta/api/horses").get_json()["horses"]}
    _check("an unnamed replacement is HORSE 24", by_id.get(24, {}).get("name") == "HORSE 24")
    _check("24's cloth is the board's (#2F4F4F)", by_id[24]["saddle_cloth_color"] == "#2F4F4F")


def test_field_21_to_24_accepted():
    """A horse standing in under 21-24 is bid on, frozen at the lock and paid
    out like any other: no 1-20 cap anywhere."""
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    _lq_scratch(client, 9, 22, "Ocelli")
    _lq_scratch(client, 13, 23, "Robusta")
    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
    r = client.post("/la-subasta/api/bid", json={"bidder_id": alice["id"], "horse_id": 22, "amount": 4})
    _check("bid on 22 accepted", r.status_code == 200, f"body={r.get_json()}")
    r = client.post("/la-subasta/api/bid", json={"bidder_id": bob["id"], "horse_id": 23, "amount": 2})
    _check("bid on 23 accepted", r.status_code == 200, f"body={r.get_json()}")
    r = client.post("/la-subasta/api/bid", json={"bidder_id": bob["id"], "horse_id": 1, "amount": 3})
    assert r.status_code == 200, r.get_json()
    h22 = next(h for h in client.get("/la-subasta/api/horses").get_json()["horses"] if h["horse_id"] == 22)
    _check("22 shows Alice leading at 4", h22["current_leader_bidder_id"] == alice["id"]
           and h22["current_high_bid"]["amount"] == 4)
    _check("pot counts 22 and 23", bidding.total_pot() == 9, f"got {bidding.total_pot()}")
    _check("Alice leads 22", bidding.horses_leading_by(alice["id"]) == [22])
    _check("Bob leads 1 and 23", bidding.horses_leading_by(bob["id"]) == [1, 23])
    r = client.post("/la-subasta/api/admin/lock")
    assert r.status_code == 200, r.get_json()
    owned = sorted(o["horse_id"] for o in (payouts.get_owner(n) for n in (1, 22, 23)) if o)
    _check("22 and 23 frozen into ownership", owned == [1, 22, 23], f"got {owned}")
    r = client.post("/la-subasta/api/admin/results", json={"win": 22, "place": 23, "show": 1})
    data = r.get_json()
    _check("results 22 / 23 / 1 accepted", r.status_code == 200 and data.get("success"), f"body={data}")
    by_finish = {p["finish"]: p for p in payouts.list_payouts()}
    _check("22's owner (Alice) is paid the win", by_finish["win"]["bidder_id"] == alice["id"]
           and by_finish["win"]["horse_id"] == 22)
    _check("23's owner (Bob) is paid the place", by_finish["place"]["bidder_id"] == bob["id"])
    _check("MAX_HORSE is La Quiniela's 24", la_config.MAX_HORSE == LQ_HORSE_COUNT == 24)


def test_field_bid_on_absent_horse_rejected():
    """A bid on a horse not in the field is refused with a reason a guest can
    read: the scratched horse (9), an also-eligible standing in for nobody
    (21), and a number no horse can have (25)."""
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    _lq_scratch(client, 9, 22, "Ocelli")
    _lq_scratch(client, 20)                       # no replacement
    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]

    def bid(horse):
        r = client.post("/la-subasta/api/bid", json={"bidder_id": alice["id"], "horse_id": horse, "amount": 1})
        return r.status_code, (r.get_json() or {}).get("error", "")

    status, error = bid(9)
    _check("bid on 9 (replaced by 22) rejected", status == 400, f"status={status}")
    _check("...saying 9 is not in the field and 22 runs for it",
           error == "#9 is not in the field: scratched, #22 runs in its place", f"got {error!r}")
    status, error = bid(20)
    _check("bid on 20 (scratched, no replacement) rejected",
           status == 400 and error == "#20 is not in the field: scratched", f"got {status} {error!r}")
    status, error = bid(21)
    _check("bid on 21 (an also-eligible not drawn in) rejected",
           status == 400 and error == "#21 is not in the field", f"got {status} {error!r}")
    status, error = bid(25)
    _check("bid on 25 rejected as no horse at all",
           status == 400 and error == "Invalid horse (must be 1-24)", f"got {status} {error!r}")
    _check("nothing was recorded", bidding.count_bids() == 0)
    status, error = bid(22)
    _check("bid on 22 accepted", status == 200, f"got {status} {error!r}")
    # Results can only name horses in the field
    client.post("/la-subasta/api/admin/lock")
    r = client.post("/la-subasta/api/admin/results", json={"win": 9, "place": 22, "show": 1})
    _check("results naming 9 rejected", r.status_code == 400
           and "#9 is not in the field" in r.get_json().get("error", ""), f"body={r.get_json()}")
    r = client.post("/la-subasta/api/admin/results", json={"win": 21, "place": 22, "show": 1})
    _check("results naming 21 rejected", r.status_code == 400, f"body={r.get_json()}")


def test_field_no_board_standin_and_no_mock():
    """Without a La Quiniela board La Subasta sells 1-20 with no names (the
    empty store's field). The mock racing service no longer feeds it."""
    import inspect
    _reset()
    app = _make_app()
    client = app.test_client()
    ls_field.set_store_source(lambda: None)
    try:
        f = ls_field.current()
        _check("no board: the stand-in field is 1-20", f.numbers() == list(range(1, 21)))
        _check("no board: the stand-in is not live", f.live is False and f.names_rev is None)
        horses = client.get("/la-subasta/api/horses").get_json()["horses"]
        _check("no board: /api/horses lists HORSE 1..HORSE 20",
               [h["name"] for h in horses] == [f"HORSE {n}" for n in range(1, 21)])
    finally:
        ls_field.set_store_source(None)
    _check("init_la_subasta takes no racing service",
           "racing_service" not in inspect.signature(init_la_subasta).parameters)
    main_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "main.py"), encoding="utf-8").read()
    _check("main.py no longer hands La Subasta the racing service",
           "init_la_subasta(socketio=socketio)" in main_src
           and "racing_service=racing_service" not in main_src)
    store = HorseStore()
    store.set_names({1: "renegade"})
    ls_field.set_store_source(lambda: store)
    try:
        f = ls_field.current()
        _check("the field is read through HorseStore.field()",
               f.live and f.get(1)["name"] == "RENEGADE" and f.names_rev == store.names_rev)
    finally:
        ls_field.set_store_source(None)


# -----------------------------------------------------------------------------
# Scratches from La Quiniela's store
# -----------------------------------------------------------------------------

class _Events:
    """A SocketIO stand-in that records every emit."""
    def __init__(self):
        self.events = []

    def emit(self, event, payload, room=None):
        self.events.append((event, payload))

    def named(self, event):
        return [p for e, p in self.events if e == event]


def _scratch_rig(names=True):
    """A fresh app over a fresh La Quiniela board, events captured, the
    auction open, three bidders."""
    from la_subasta import notifications as nots
    _reset()
    app = _make_app()
    client = app.test_client()
    if names:
        _lq_names(client)
    ev = _Events()
    nots.init_notifications(ev)
    transition(AuctionState.OPEN)
    people = [client.post("/la-subasta/api/register", json={"name": n, "emoji": e}).get_json()["bidder"]
              for n, e in (("Alice", "🌮"), ("Bob", "🐴"), ("Carol", "💃"))]
    return app, client, ev, people


def _bid(client, bidder, horse, amount):
    r = client.post("/la-subasta/api/bid", json={"bidder_id": bidder["id"], "horse_id": horse, "amount": amount})
    assert r.status_code == 200, (horse, amount, r.get_json())
    return r.get_json()["bid"]["bid_id"]


def _bids_on(horse):
    from la_subasta.models import get_conn
    return [dict(r) for r in get_conn().execute(
        "SELECT id, bidder_id, amount, voided, voided_reason FROM bids WHERE horse_id = ? ORDER BY id",
        (horse,)).fetchall()]


def _ownership(horse):
    from la_subasta.models import get_conn
    row = get_conn().execute("SELECT * FROM ownership WHERE horse_id = ?", (horse,)).fetchone()
    return dict(row) if row else None


def _done_with_events():
    from la_subasta import notifications as nots
    nots.init_notifications(None)


def test_scratch_while_open():
    """A scratch on the LQ admin page while the auction is open: the horse
    leaves the list, every bid on it is voided 'scratched', nobody is
    charged for it, and guest phones are told without a reload."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 4, 1); _bid(client, bob, 4, 3); _bid(client, alice, 4, 5)
        _bid(client, carol, 6, 2)
        _bid(client, bob, 7, 4)
        _check("before: Alice leads 4 at 5", bidding.horses_leading_by(alice["id"]) == [4])
        _check("before: pot 5 + 2 + 4 = 11", bidding.total_pot() == 11, f"got {bidding.total_pot()}")
        ev.events.clear()

        r = _lq_scratch(client, 4)                 # Litmus Test, no replacement
        _check("LQ admin: scratch 4", r.status_code == 200, f"body={r.get_json()}")
        _check("applied and pushed by the store's listener, before any La Subasta request",
               [e for e, _ in ev.events] == ["horse_scratched", "field_changed"]
               and [b["voided"] for b in _bids_on(4)] == [1, 1, 1], f"got {ev.events}")
        bids = _bids_on(4)
        _check("every bid on 4 is voided", len(bids) == 3 and all(b["voided"] == 1 for b in bids), f"{bids}")
        _check("...with voided_reason 'scratched'",
               all(b["voided_reason"] == "scratched" for b in bids), f"{bids}")
        _check("bids on the other horses untouched",
               all(b["voided"] == 0 for b in _bids_on(6) + _bids_on(7)))
        numbers = [h["horse_id"] for h in client.get("/la-subasta/api/horses").get_json()["horses"]]
        _check("4 is gone from the list", 4 not in numbers and len(numbers) == 19, f"got {numbers}")
        _check("Alice is charged nothing", bidding.bidder_portfolio(alice["id"])["total"] == 0)
        _check("pot drops to 2 + 4 = 6", bidding.total_pot() == 6, f"got {bidding.total_pot()}")
        scratched = ev.named("horse_scratched")
        _check("horse_scratched pushed once, for 4, with 3 refunds",
               scratched == [{"horse_id": 4, "refund_count": 3, "ownership_voided": False}], f"got {scratched}")
        changed = ev.named("field_changed")
        _check("field_changed pushed with the new field",
               len(changed) == 1 and changed[0]["horses"] == numbers, f"got {changed}")
        r = client.post("/la-subasta/api/bid", json={"bidder_id": bob["id"], "horse_id": 4, "amount": 6})
        _check("a new bid on 4 is refused", r.status_code == 400
               and r.get_json()["error"] == "#4 is not in the field: scratched", f"body={r.get_json()}")
        # Undo of a voided bid is refused: it is already void
        r = client.post("/la-subasta/api/bid/undo", json={"bid_id": bids[-1]["id"], "bidder_id": alice["id"]})
        _check("undoing a bid the scratch voided says it is already voided",
               r.status_code == 400 and "already voided" in r.get_json()["error"])
    finally:
        _done_with_events()


def test_scratch_store_listener_direct():
    """The scratch applies the moment the store records it, whoever writes
    it: no La Subasta request, no timer."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 12, 2)
        ev.events.clear()
        lq_board.get_board().store.scratch_gateway(12)
        _check("store write alone voids the bid on 12",
               [b["voided_reason"] for b in _bids_on(12)] == ["scratched"])
        _check("...and pushes horse_scratched", [p["horse_id"] for p in ev.named("horse_scratched")] == [12])
        n = len(ev.events)
        client.get("/la-subasta/api/horses"); client.get("/la-subasta/api/state")
        _check("later requests find nothing to do (no second push)", len(ev.events) == n)
        lq_board.get_board().store.set_names({3: "Intrepido II"})
        _check("a name typed on the LQ admin page pushes field_changed only",
               [e for e, _ in ev.events[n:]] == ["field_changed"], f"got {ev.events[n:]}")
    finally:
        _done_with_events()


def test_scratch_after_lock():
    """A scratch after the lock: the ownership row is voided the same way,
    the owner's total owed drops by that winning bid, the horse cannot pay
    out, a paid owner shows the refund owed, and the House rule does not
    apply."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 2, 5)          # Albus
        _bid(client, alice, 3, 2)          # Intrepido
        _bid(client, bob, 5, 4)
        _bid(client, carol, 8, 3)
        r = client.post("/la-subasta/api/admin/lock")
        assert r.status_code == 200, r.get_json()
        _check("locked: Alice owns 2 and 3 and owes 7", bidding.bidder_portfolio(alice["id"])["total"] == 7)
        r = client.post("/la-subasta/api/admin/paid", json={"bidder_id": alice["id"]})
        _check("Alice marked paid 7", r.status_code == 200 and r.get_json()["amount"] == 7)
        ev.events.clear()

        r = _lq_scratch(client, 2)
        _check("LQ admin: scratch 2 after the lock", r.status_code == 200, f"body={r.get_json()}")
        own = _ownership(2)
        _check("2's ownership row is kept and voided 'scratched'",
               own is not None and own["voided"] == 1 and own["voided_reason"] == "scratched"
               and own["voided_at"], f"got {own}")
        _check("...and the bid on 2 too", [b["voided_reason"] for b in _bids_on(2)] == ["scratched"])
        _check("3, 5, 8 still owned", all((_ownership(n) or {}).get("voided") == 0 for n in (3, 5, 8)))
        _check("2 has no owner any more", payouts.get_owner(2) is None)
        port = bidding.bidder_portfolio(alice["id"])
        _check("Alice owes 2 now (7 less the 5 on Albus)", port["total"] == 2, f"got {port}")
        _check("her portfolio lists 2 as scratched at 5",
               port["scratched"] == [{"horse_id": 2, "amount": 5}], f"got {port['scratched']}")
        ledger = {b["id"]: b for b in client.get("/la-subasta/api/bidders").get_json()["bidders"]}
        _check("ledger: Alice owes 2 and is owed a refund of 5",
               ledger[alice["id"]]["owed"] == 2 and ledger[alice["id"]]["refund_owed"] == 5,
               f"got {ledger[alice['id']]}")
        _check("ledger: unpaid Bob owes 4, no refund",
               ledger[bob["id"]]["owed"] == 4 and ledger[bob["id"]]["refund_owed"] == 0)
        _check("horse_scratched says the ownership was voided",
               ev.named("horse_scratched") == [{"horse_id": 2, "refund_count": 1, "ownership_voided": True}],
               f"got {ev.named('horse_scratched')}")

        r = client.post("/la-subasta/api/admin/results", json={"win": 2, "place": 3, "show": 5})
        _check("results naming the scratched 2 are refused (it cannot pay out, not even to the House)",
               r.status_code == 400 and r.get_json()["error"] == "#2 is not in the field: scratched",
               f"body={r.get_json()}")
        r = client.post("/la-subasta/api/admin/results", json={"win": 3, "place": 5, "show": 8})
        data = r.get_json()
        _check("results 3 / 5 / 8 accepted", r.status_code == 200 and data.get("success"), f"body={data}")
        _check("the pot leaves out the scratched 5: 2 + 4 + 3 = 9", data["total_pot"] == 9,
               f"got {data.get('total_pot')}")
        _check("nobody's payout is the House's",
               not any(p["is_house"] for p in data["payouts"]))
        _check("no payout row names 2", all(p["horse_id"] != 2 for p in payouts.list_payouts()))
    finally:
        _done_with_events()


def test_scratch_replacement_adds_fresh_horse():
    """A replacement scratch (9 -> 22) during the auction: 9's bids are
    voided and 22 enters as a fresh horse with no bids."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 9, 3); _bid(client, bob, 9, 5)
        ev.events.clear()
        r = _lq_scratch(client, 9, 22, "Ocelli")
        _check("LQ admin: 9 -> 22", r.status_code == 200, f"body={r.get_json()}")
        _check("9's bids voided 'scratched'",
               [b["voided_reason"] for b in _bids_on(9)] == ["scratched", "scratched"])
        horses = {h["horse_id"]: h for h in client.get("/la-subasta/api/horses").get_json()["horses"]}
        _check("9 gone, 22 listed", 9 not in horses and 22 in horses)
        h22 = horses.get(22, {})
        _check("22 is OCELLI with no bids",
               h22.get("name") == "OCELLI" and h22.get("current_high_bid") is None
               and h22.get("current_leader_bidder_id") is None, f"got {h22}")
        _check("no bid rows on 22", _bids_on(22) == [])
        _check("pushed: horse_scratched for 9, then field_changed with 22",
               [p["horse_id"] for p in ev.named("horse_scratched")] == [9]
               and 22 in ev.named("field_changed")[-1]["horses"], f"got {ev.events}")
        _bid(client, bob, 22, 1)
        _check("22 takes its first bid at the opening price",
               bidding.current_high_bid(22)["amount"] == 1)
        _check("Bob's portfolio is just 22 at 1", bidding.bidder_portfolio(bob["id"])["total"] == 1)
    finally:
        _done_with_events()


def test_scratch_undo_restores_field_not_bids():
    """Undo on the LQ admin page puts the horse back in the field; it does
    not un-void its bids. Undoing a replacement takes the stand-in out
    again, and its bids are voided the same way."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 11, 4)
        _lq_scratch(client, 11)
        r = client.post("/api/quiniela/unscratch", json={"horse": 11})
        _check("LQ admin: undo 11", r.status_code == 200, f"body={r.get_json()}")
        horses = {h["horse_id"]: h for h in client.get("/la-subasta/api/horses").get_json()["horses"]}
        _check("11 is back in the list", 11 in horses)
        _check("...with no bids: its old bid stays voided",
               horses[11]["current_high_bid"] is None
               and [b["voided"] for b in _bids_on(11)] == [1], f"got {_bids_on(11)}")
        _check("Alice is not charged for 11", bidding.bidder_portfolio(alice["id"])["total"] == 0)
        _bid(client, bob, 11, 2)
        _check("11 can be bid on again", bidding.current_high_bid(11)["bidder_id"] == bob["id"])

        # A replacement undone: 22 leaves again and its bid goes with it
        _lq_scratch(client, 9, 22, "Ocelli")
        _bid(client, carol, 22, 3)
        client.post("/api/quiniela/unscratch", json={"horse": 9})
        numbers = [h["horse_id"] for h in client.get("/la-subasta/api/horses").get_json()["horses"]]
        _check("after undoing 9 -> 22: 9 back, 22 gone", 9 in numbers and 22 not in numbers, f"got {numbers}")
        _check("Carol's bid on 22 voided 'scratched'",
               [b["voided_reason"] for b in _bids_on(22)] == ["scratched"])
    finally:
        _done_with_events()


def test_scratch_restart_does_not_double_void():
    """pi5 restarts with La Quiniela's store on disk (the shared database):
    what was applied stays applied, nothing is voided twice, nothing is
    pushed again; a scratch recorded while La Subasta was not listening is
    applied at start."""
    from la_quiniela.models import LqDb
    from la_subasta import notifications as nots
    from la_subasta import scratches as ls_scratches
    from la_subasta.models import get_conn
    _reset()
    app = _make_app()
    client = app.test_client()
    db = LqDb(_TMP_DB)
    db.init_schema()
    try:
        store = HorseStore(db)
        store.set_names({n: name for n, name in enumerate(DERBY_2026, 1)})
        ls_field.set_store_source(lambda: store)
        ls_scratches.forget()
        ls_scratches.follow_la_quiniela()
        transition(AuctionState.OPEN)
        alice = client.post("/la-subasta/api/register",
                            json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
        bob = client.post("/la-subasta/api/register",
                          json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
        _bid(client, alice, 14, 2); _bid(client, bob, 14, 4); _bid(client, alice, 15, 1)
        store.scratch_gateway(14)

        def snapshot():
            return [tuple(r) for r in get_conn().execute(
                "SELECT id, horse_id, voided, voided_reason FROM bids ORDER BY id").fetchall()]

        before = snapshot()
        _check("14's two bids voided once", [r[2:] for r in before if r[1] == 14] == [(1, "scratched")] * 2)

        # Restart: a new store read back from the database, La Subasta's
        # memory gone, events captured from the start.
        ev = _Events()
        nots.init_notifications(ev)
        store2 = HorseStore(db)
        ls_field.set_store_source(lambda: store2)
        ls_scratches.forget()
        _check("restart: the store came back with 14 scratched", 14 not in ls_field.current())
        _check("restart: follow_la_quiniela() at start", ls_scratches.follow_la_quiniela() is True)
        client.get("/la-subasta/api/horses")
        ls_scratches.sync(force=True)
        _check("restart: no bid changed", snapshot() == before)
        _check("restart: nothing pushed", ev.events == [], f"got {ev.events}")

        # A scratch written to the database while La Subasta was not
        # listening (another process, or a listener that failed) is applied
        # at the next start, once.
        HorseStore(db).scratch_gateway(15)
        store3 = HorseStore(db)
        ls_field.set_store_source(lambda: store3)
        ls_scratches.forget()
        ls_scratches.follow_la_quiniela()
        _check("a scratch recorded while down is applied at start",
               [b["voided_reason"] for b in _bids_on(15)] == ["scratched"])
        n = sum(1 for e, _ in ev.events if e == "horse_scratched")
        ls_scratches.forget()
        ls_scratches.follow_la_quiniela()
        _check("...and a second start does nothing more",
               sum(1 for e, _ in ev.events if e == "horse_scratched") == n
               and [b["voided_reason"] for b in _bids_on(15)] == ["scratched"])
    finally:
        ls_field.set_store_source(None)
        ls_scratches.forget()
        nots.init_notifications(None)
        db.close()


def test_ownership_migration():
    """An ownership table from before this change gets the void columns,
    its rows read not voided, and nothing else is touched."""
    import sqlite3
    path = tempfile.mktemp(prefix="la_subasta_old_", suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT, horse_id INTEGER NOT NULL,
            bidder_id INTEGER NOT NULL, winning_bid REAL NOT NULL,
            locked_at TEXT NOT NULL DEFAULT (datetime('now')), event_year INTEGER NOT NULL,
            UNIQUE(horse_id, event_year));
        INSERT INTO ownership (horse_id, bidder_id, winning_bid, event_year) VALUES (3, 1, 7, 2026);
        CREATE TABLE horse_state (horse_id INTEGER NOT NULL, scratched INTEGER NOT NULL DEFAULT 0,
            scratched_at TEXT, event_year INTEGER NOT NULL, PRIMARY KEY (horse_id, event_year));
        INSERT INTO horse_state VALUES (5, 1, '2026-05-02', 2026);
    """)
    conn.commit()
    conn.close()
    try:
        c = init_db(path)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(ownership)").fetchall()]
        _check("old ownership table gains voided, voided_reason, voided_at",
               cols[-3:] == ["voided", "voided_reason", "voided_at"], f"got {cols}")
        row = c.execute("SELECT * FROM ownership").fetchone()
        _check("its row is kept and reads not voided",
               row["horse_id"] == 3 and row["winning_bid"] == 7 and row["voided"] == 0)
        c = init_db(path)
        _check("migration is idempotent", [r["name"] for r in c.execute(
            "PRAGMA table_info(ownership)").fetchall()] == cols)
        left = c.execute("SELECT * FROM horse_state").fetchall()
        _check("an old horse_state table is left as it is, unread", len(left) == 1)
    finally:
        init_db(_TMP_DB)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def test_guest_js_follows_the_field():
    """Guest phones drop a scratched horse and pick up a replacement
    without a reload: guest.js handles horse_scratched and field_changed by
    reading the list again, rebuilds the cards when the horses change (9
    out, 22 in: same count) and bids on the card's current horse."""
    _reset()
    app = _make_app()
    js = app.test_client().get("/la-subasta/static/js/guest.js").get_data(as_text=True)
    hs = js[js.index("socket.on('horse_scratched'"):]
    hs = hs[:hs.index("});")]
    _check("horse_scratched drops the horse from the list", "delete state.horses[payload.horse_id]" in hs)
    _check("horse_scratched reads the list again", "refreshHorses()" in hs)
    fc = js[js.index("socket.on('field_changed'"):]
    fc = fc[:fc.index("});")]
    _check("field_changed reads the list again", "refreshHorses()" in fc)
    _check("cards are rebuilt when the horses differ, not only their count",
           "const sameHorses" in js and "existing[i].dataset.horseId === String(h.horse_id)" in js)
    _check("a bid button bids on the card's current horse",
           "const horseId = parseInt(card.dataset.horseId, 10);" in js)


def test_horses_scratched_flag_roundtrip():
    """A client that loads /api/horses AFTER a scratch (the 'window B opened
    later' case) must not see the horse: the list is La Quiniela's field, not
    only the live SocketIO broadcast. Undo on the LQ admin page brings it
    back on the next fresh load."""
    _reset()
    app = _make_app()
    client = app.test_client()

    def listed(horse_id):
        # Fresh request each call = a client connecting now (e.g. window B)
        horses = app.test_client().get("/la-subasta/api/horses").get_json()["horses"]
        return any(h["horse_id"] == horse_id for h in horses)

    horses = client.get("/la-subasta/api/horses").get_json()["horses"]
    _check("every horse still has a 'scratched' field, false",
           all(h.get("scratched") is False for h in horses))
    _check("horse 5 listed initially", listed(5))

    r = _lq_scratch(client, 5)
    _check("LQ admin scratch of 5 returns 200", r.status_code == 200)
    _check("fresh /api/horses no longer lists horse 5", not listed(5))
    _check("only the scratched horse left (horse 6 still listed)", listed(6))

    r = client.post("/api/quiniela/unscratch", json={"horse": 5})
    _check("LQ admin undo returns 200", r.status_code == 200)
    _check("fresh /api/horses lists horse 5 again after the undo", listed(5))


def test_static_assets_served():
    _reset()
    app = _make_app()
    client = app.test_client()

    css = client.get("/la-subasta/static/css/la-subasta-mobile.css")
    _check("GET mobile CSS returns 200", css.status_code == 200,
           f"status={css.status_code}")
    css_body = css.get_data(as_text=True)
    _check("CSS contains DDM palette hex #3F8E43",
           "#3F8E43" in css_body)
    _check("CSS defines .ls-horse-card class",
           ".ls-horse-card" in css_body)
    _check("CSS defines .ls-emoji-btn class",
           ".ls-emoji-btn" in css_body)

    js = client.get("/la-subasta/static/js/guest.js")
    _check("GET guest.js returns 200", js.status_code == 200,
           f"status={js.status_code}")
    js_body = js.get_data(as_text=True)
    _check("JS references /api/register endpoint",
           "/la-subasta/api/register" in js_body)
    _check("JS references /api/bid endpoint",
           "/la-subasta/api/bid" in js_body)
    _check("JS listens for bid_placed event",
           "bid_placed" in js_body)
    _check("JS listens for settings_changed event",
           "settings_changed" in js_body)
    _check("JS listens for auction_locked event",
           "auction_locked" in js_body)
    _check("JS uses localStorage key la_subasta_identity",
           "la_subasta_identity" in js_body)


def test_api_responses_are_no_store():
    """Dynamic API responses must send Cache-Control: no-store so browsers
    can't render stale auction state (e.g. a scratched horse) from cache.
    Static assets (CSS/JS) and the guest HTML page must NOT be no-store —
    they should still cache/revalidate normally."""
    _reset()
    app = _make_app()
    client = app.test_client()

    # Every dynamic API endpoint is no-store
    for path in ("/la-subasta/api/state",
                 "/la-subasta/api/horses",
                 "/la-subasta/api/bidders"):
        resp = client.get(path)
        cc = resp.headers.get("Cache-Control", "")
        _check(f"{path} returns 200", resp.status_code == 200,
               f"status={resp.status_code}")
        _check(f"{path} Cache-Control includes no-store", "no-store" in cc,
               f"got Cache-Control={cc!r}")

    # Static assets are NOT made no-store (still cacheable / revalidate)
    css_cc = client.get(
        "/la-subasta/static/css/la-subasta-mobile.css").headers.get("Cache-Control", "")
    js_cc = client.get(
        "/la-subasta/static/js/guest.js").headers.get("Cache-Control", "")
    _check("CSS response is NOT no-store", "no-store" not in css_cc,
           f"got Cache-Control={css_cc!r}")
    _check("JS response is NOT no-store", "no-store" not in js_cc,
           f"got Cache-Control={js_cc!r}")

    # The guest HTML page is also not forced no-store by the API handler
    html_cc = client.get("/la-subasta/").headers.get("Cache-Control", "")
    _check("guest HTML page is NOT no-store", "no-store" not in html_cc,
           f"got Cache-Control={html_cc!r}")


def test_guest_js_no_dollar_currency():
    """2027 funny-money mode: guest.js must not emit any '$' currency
    symbols anywhere — neither as string literals, template-literal
    interpolation markers, nor inline copy. All amount displays go through
    the new pesos() formatter."""
    _reset()
    app = _make_app()
    client = app.test_client()
    js = client.get("/la-subasta/static/js/guest.js").get_data(as_text=True)

    _check("guest.js contains zero '$' characters",
           "$" not in js,
           f"found {js.count('$')} occurrence(s) of '$'")

    # The new pesos formatter must be defined and referenced
    _check("guest.js defines pesos() helper",
           "function pesos(" in js,
           "missing pesos() function definition")
    _check("guest.js calls pesos() at least once",
           js.count("pesos(") >= 2,  # 1 definition + >=1 call
           f"only {js.count('pesos(')} occurrences of pesos(")

    # Spot-check that previously dollar-prefixed strings now use pesos copy
    _check("guest.js uses ' pesos' in amount strings",
           " pesos" in js,
           "no ' pesos' string-literal substring found")


def test_guest_2027_pesos_button_format():
    """2027 follow-up: no user-facing '$N' patterns remain in guest.js, the
    bid buttons use the pesos-only label format, and the guest.html footer
    shows 2027 (not 2026)."""
    import re
    _reset()
    app = _make_app()
    client = app.test_client()
    js = client.get("/la-subasta/static/js/guest.js").get_data(as_text=True)

    # No user-facing dollar-amount pattern like "$5" anywhere
    m = re.search(r"\$\d", js)
    _check("guest.js has no '$N' currency pattern",
           m is None,
           f"found {m.group(0)!r} at index {m.start()}" if m else "")

    # Opening (pre-bid) buttons read "Bid N"; raise (post-bid) buttons read
    # "New Bid X" (just the resulting total — no delta math to parse).
    _check("guest.js opening buttons use 'Bid ' prefix",
           "'Bid '" in js,
           "missing \"'Bid '\" opening-button label")
    _check("guest.js raise buttons use 'New Bid X' format",
           "'New Bid ' + nextAmt" in js,
           "missing raise-button 'New Bid' label")
    _check("guest.js no longer uses the old '+N = X pesos' raise label",
           "' = ' + pesos(nextAmt)" not in js,
           "stale '+N = X pesos' raise label still present")

    # Footer year bumped 2026 -> 2027
    html = client.get("/la-subasta/").get_data(as_text=True)
    _check("guest.html footer shows 'La Subasta 2027'",
           "La Subasta 2027" in html,
           "footer not updated to 2027")
    _check("guest.html footer no longer shows 'La Subasta 2026'",
           "La Subasta 2026" not in html,
           "stale 'La Subasta 2026' still present")


# -----------------------------------------------------------------------------
# Dev panel (dev/testing-only floating panel, gated by ?dev=1)
# -----------------------------------------------------------------------------

def test_dev_panel_markup_and_gating():
    """The dev panel markup is ALWAYS rendered (so the localStorage dev flag
    can keep it visible across navigation), but ships hidden by default and is
    revealed only by guest.js when dev mode is active. We assert the markup is
    present + hidden-by-default and that guest.js carries the ?dev=1 / ?dev=0
    detection wiring — JS-driven visibility itself isn't exercised here (no JS
    engine in this harness)."""
    _reset()
    app = _make_app()
    client = app.test_client()

    html_plain = client.get("/la-subasta/").get_data(as_text=True)
    html_dev = client.get("/la-subasta/?dev=1").get_data(as_text=True)

    # Markup present regardless of the query param (JS-gated, not server-gated)
    for label, html in (("no query", html_plain), ("?dev=1", html_dev)):
        _check(f"dev panel container present ({label})",
               'id="ls-dev"' in html, "missing #ls-dev")
        _check(f"dev panel hidden by default ({label})",
               '<div id="ls-dev" class="ls-dev" hidden>' in html,
               "#ls-dev not hidden by default")

    # Required panel chrome + controls
    markers = [
        ('id="ls-dev-fab"',                 "collapsed bug fab"),
        ('id="ls-dev-panel"',               "expanded panel"),
        ('🐛 DEV PANEL',                    "panel title"),
        ('id="ls-dev-state"',               "status: state"),
        ('id="ls-dev-pot"',                 "status: pot"),
        ('id="ls-dev-bidders"',             "status: bidders"),
        ('id="ls-dev-bids"',                "status: bids placed"),
        ('data-dev-action="open"',          "open auction button"),
        ('data-dev-action="final-hour"',    "force final-hour button"),
        ('data-dev-action="lock"',          "lock button"),
        ('data-dev-action="results"',       "set results button"),
        ('data-dev-action="reset-bids"',    "reset bids button"),
        ('data-dev-action="reset-full"',    "reset full button"),
        ('data-dev-action="clear-identity"', "clear identity button"),
        ('id="ls-dev-results-modal"',       "results modal"),
        ('id="ls-dev-toast"',               "toast element"),
    ]
    for marker, lbl in markers:
        _check(f"dev panel markup has {lbl}", marker in html_dev,
               f"missing marker: {marker!r}")

    # guest.js dev-mode detection + wiring
    js = client.get("/la-subasta/static/js/guest.js").get_data(as_text=True)
    js_markers = [
        ("la_subasta_dev_mode", "localStorage dev flag"),
        ("URLSearchParams",     "URL param parse"),
        ("get('dev')",          "reads ?dev param"),
        ("function setupDevPanel(", "dev panel setup"),
        ("/la-subasta/api/admin/", "admin endpoint base"),
        ("'transition'",           "transition action wired"),
    ]
    for marker, lbl in js_markers:
        _check(f"guest.js dev wiring: {lbl}", marker in js,
               f"missing in guest.js: {marker!r}")

    # A scratch is entered once, on the LQ admin page: the dev panel has no
    # scratch or unscratch of its own any more.
    _check("dev panel has no scratch button", 'data-dev-action="scratch"' not in html_dev)
    _check("dev panel has no unscratch button", 'data-dev-action="unscratch"' not in html_dev)
    _check("dev panel has no horse input", 'id="ls-dev-horse"' not in html_dev)
    _check("guest.js has no scratch action", "'unscratch'" not in js and "'scratch'" not in js)


def test_own_scratch_route_gone():
    """La Subasta's own scratch and unscratch routes are gone, and so is its
    horse_state table: a scratch is entered on the LQ admin page."""
    _reset()
    app = _make_app()
    client = app.test_client()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    _check("/la-subasta/api/admin/scratch is not a route",
           "/la-subasta/api/admin/scratch" not in rules)
    _check("/la-subasta/api/admin/unscratch is not a route",
           "/la-subasta/api/admin/unscratch" not in rules)
    r = client.post("/la-subasta/api/admin/scratch", json={"horse_id": 5})
    _check("POST /api/admin/scratch is a 404", r.status_code == 404, f"status={r.status_code}")
    from la_subasta.models import get_conn
    tables = {r["name"] for r in get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    _check("a fresh database has no horse_state table", "horse_state" not in tables,
           f"tables: {sorted(tables)}")
    _check("bidding has no scratch functions",
           not any(hasattr(bidding, n) for n in ("scratch_horse", "unscratch_horse", "is_horse_scratched")))


def test_admin_transition_endpoint():
    _reset()
    app = _make_app()
    client = app.test_client()

    # Force OPEN from NOT_STARTED
    r = client.post("/la-subasta/api/admin/transition", json={"state": "OPEN"})
    _check("transition->OPEN returns 200", r.status_code == 200,
           f"body={r.get_json()}")
    _check("transition->OPEN sets state OPEN",
           r.get_json().get("state") == "OPEN")

    # Force FINAL_HOUR (the dev panel's "Force FINAL_HOUR")
    r = client.post("/la-subasta/api/admin/transition",
                    json={"state": "FINAL_HOUR"})
    _check("transition->FINAL_HOUR returns 200", r.status_code == 200)
    _check("state is FINAL_HOUR", get_state().value == "FINAL_HOUR")

    # force=True allows non-linear jumps (e.g. back to NOT_STARTED)
    r = client.post("/la-subasta/api/admin/transition",
                    json={"state": "NOT_STARTED"})
    _check("transition->NOT_STARTED (forced backward) returns 200",
           r.status_code == 200)
    _check("state is NOT_STARTED", get_state().value == "NOT_STARTED")

    # Invalid target rejected
    r = client.post("/la-subasta/api/admin/transition", json={"state": "BOGUS"})
    _check("invalid transition target rejected", r.status_code == 400)
    r = client.post("/la-subasta/api/admin/transition", json={})
    _check("missing transition target rejected", r.status_code == 400)


def test_state_includes_bidder_and_bid_counts():
    _reset()
    app = _make_app()
    client = app.test_client()

    data = client.get("/la-subasta/api/state").get_json()
    _check("/api/state has num_bidders field", "num_bidders" in data)
    _check("/api/state has num_bids field", "num_bids" in data)
    _check("fresh auction: num_bidders == 0", data.get("num_bidders") == 0,
           f"got {data.get('num_bidders')}")
    _check("fresh auction: num_bids == 0", data.get("num_bids") == 0,
           f"got {data.get('num_bids')}")

    # Register 2 bidders, place 3 bids, recount
    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]
    client.post("/la-subasta/api/bid",
                json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1})
    client.post("/la-subasta/api/bid",
                json={"bidder_id": bob["id"], "horse_id": 1, "amount": 2})
    client.post("/la-subasta/api/bid",
                json={"bidder_id": alice["id"], "horse_id": 2, "amount": 1})

    data = client.get("/la-subasta/api/state").get_json()
    _check("num_bidders == 2 after 2 registrations (House excluded)",
           data.get("num_bidders") == 2, f"got {data.get('num_bidders')}")
    _check("num_bids == 3 after 3 bids", data.get("num_bids") == 3,
           f"got {data.get('num_bids')}")


def test_auction_state_changed_broadcast():
    """Admin transitions must fire auction_state_changed so live guests
    see button states update without a page reload."""
    _reset()

    events = []

    class _StubSocketIO:
        def emit(self, event, payload, room=None):
            events.append((event, payload, room))

    from la_subasta import notifications as nots
    app = _make_app()
    nots.init_notifications(_StubSocketIO())
    client = app.test_client()
    events.clear()

    # NOT_STARTED → OPEN
    r = client.post("/la-subasta/api/admin/start")
    _check("admin/start returns 200", r.status_code == 200)

    state_events = [e for e in events if e[0] == "auction_state_changed"]
    _check("start fired auction_state_changed", len(state_events) == 1,
           f"got {len(state_events)}")
    payload = state_events[0][1]
    _check("auction_state_changed payload has new_state=OPEN",
           payload.get("new_state") == "OPEN",
           f"got {payload}")
    _check("auction_state_changed payload has old_state=NOT_STARTED",
           payload.get("old_state") == "NOT_STARTED")

    # OPEN → FINAL_HOUR
    events.clear()
    r = client.post("/la-subasta/api/admin/final-hour")
    _check("admin/final-hour returns 200", r.status_code == 200)
    state_events = [e for e in events if e[0] == "auction_state_changed"]
    _check("final-hour fired auction_state_changed with new_state=FINAL_HOUR",
           len(state_events) == 1
           and state_events[0][1]["new_state"] == "FINAL_HOUR")

    # FINAL_HOUR → LOCKED (also emits auction_locked, so expect both)
    events.clear()
    r = client.post("/la-subasta/api/admin/lock")
    _check("admin/lock returns 200", r.status_code == 200)
    _check("lock fired auction_state_changed with new_state=LOCKED",
           any(e[0] == "auction_state_changed"
               and e[1]["new_state"] == "LOCKED"
               for e in events))
    _check("lock still fires auction_locked event",
           any(e[0] == "auction_locked" for e in events))

    # Results endpoint does LOCKED→RACE_COMPLETE→SETTLED — expect two events
    events.clear()
    # Need at least one owned horse for payouts to work; but since the
    # admin/results requires LOCKED state and we're already there, just
    # drive it straight through with zero bids (House takes everything).
    r = client.post("/la-subasta/api/admin/results",
                    json={"win": 1, "place": 2, "show": 3})
    _check("admin/results returns 200", r.status_code == 200,
           f"body={r.get_json()}")
    transitions_fired = [e[1]["new_state"] for e in events
                         if e[0] == "auction_state_changed"]
    _check("results fires RACE_COMPLETE + SETTLED transitions in order",
           transitions_fired == ["RACE_COMPLETE", "SETTLED"],
           f"got {transitions_fired}")

    nots.init_notifications(None)


def test_guest_js_listens_for_state_changed():
    """The guest JS must subscribe to auction_state_changed, otherwise the
    backend broadcast has no client."""
    _reset()
    app = _make_app()
    client = app.test_client()
    js = client.get("/la-subasta/static/js/guest.js").get_data(as_text=True)
    _check("guest.js listens for auction_state_changed",
           "auction_state_changed" in js)


def test_initial_horse_load_after_registration():
    """Regression: the horse list showed 'Waiting for horses…' forever on a
    fresh phone until the guest manually refreshed.

    bootApp() fires /api/admin/settings, /api/horses and /api/state in parallel
    the instant registration returns. The old code let a failed /api/horses
    reject the Promise.all, which skipped the final render AND startCountdown()
    with no console trace and no retry — and nothing could repopulate the list
    afterwards, because every socket handler early-returns on an unknown
    horse_id. Only a page reload recovered.

    Server half: the exact boot sequence — a register write immediately
    followed by the three parallel boot reads — must return a full horse list.
    Client half: guest.js must not be able to swallow that failure again.
    """
    import re
    import threading
    from la_subasta.models import close_conn
    _reset()
    app = _make_app()
    client = app.test_client()

    # --- 1. Identity registration (the write that precedes the boot reads) ---
    reg = client.post("/la-subasta/api/register",
                      json={"name": "Fresh Phone", "emoji": "🌮"})
    _check("boot: registration succeeds", reg.status_code == 200,
           f"status {reg.status_code}: {reg.get_data(as_text=True)[:200]}")
    bidder = reg.get_json().get("bidder") or {}
    _check("boot: register returns the bidder fields guest.js dereferences",
           all(k in bidder for k in ("id", "name", "emoji", "identity")),
           f"bidder payload was {bidder!r}")

    # --- 2. The three boot reads, fired concurrently as bootApp() does ---
    results = {}
    errors = []

    def _get(label, url):
        try:
            c2 = app.test_client()
            resp = c2.get(url)
            results[label] = (resp.status_code, resp.get_json())
        except Exception as exc:                       # pragma: no cover
            errors.append(f"{label}: {exc!r}")
        finally:
            close_conn()   # release this worker thread's sqlite handle

    threads = [
        threading.Thread(target=_get, args=("settings",
                                            "/la-subasta/api/admin/settings")),
        threading.Thread(target=_get, args=("horses", "/la-subasta/api/horses")),
        threading.Thread(target=_get, args=("state", "/la-subasta/api/state")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _check("boot: no exception from the parallel boot reads", not errors,
           f"errors: {errors}")

    horses_status, horses_body = results.get("horses", (None, None))
    _check("boot: /api/horses returns 200 right after registration",
           horses_status == 200, f"status was {horses_status}")

    horses = (horses_body or {}).get("horses") or []
    _check("boot: /api/horses returns the full field immediately",
           len(horses) == 20,
           f"got {len(horses)} horses, expected 20")

    # renderHorseList()/updateHorseCard() dereference these on every card; a
    # missing key throws mid-render and leaves the list half-built.
    required = ("horse_id", "saddle_cloth", "scratched", "current_high_bid",
                "current_leader_identity", "current_leader_bidder_id")
    missing = [k for k in required if horses and k not in horses[0]]
    _check("boot: each horse carries every field the renderer reads",
           not missing, f"missing keys: {missing}")

    # The other two reads feed button-enabled state — they must not 500 either.
    _check("boot: /api/admin/settings returns 200",
           results.get("settings", (None,))[0] == 200,
           f"status was {results.get('settings', (None,))[0]}")
    _check("boot: /api/state returns 200",
           results.get("state", (None,))[0] == 200,
           f"status was {results.get('state', (None,))[0]}")

    # --- 3. Client contract: the failure cannot be swallowed again ---
    js = client.get("/la-subasta/static/js/guest.js").get_data(as_text=True)

    _check("guest.js: bootApp loads horses through the retrying loader",
           "loadHorsesWithRetry()" in js and
           "Promise.all([refreshSettings(), loadHorsesWithRetry(), "
           "refreshState()])" in js,
           "bootApp no longer calls loadHorsesWithRetry() in its Promise.all")
    _check("guest.js: the boot chain has a .catch() backstop",
           ".catch(function (err) {" in js,
           "boot Promise.all has no .catch — a rejection would skip the "
           "final render and startCountdown()")
    _check("guest.js: getJSON traps network-layer failures",
           "resp = await fetch(url, { credentials: 'same-origin' });" in js and
           "networkError: true" in js,
           "getJSON can still reject on a fetch() network error")
    _check("guest.js: a failed horse fetch retries with backoff",
           "HORSE_RETRY_DELAYS_MS" in js and "runHorseLoadRetries" in js,
           "no retry schedule for the initial horse load")
    _check("guest.js: exhausted retries fall back to a recovery poll",
           "startHorseRecoveryPoll" in js and "HORSE_RECOVERY_POLL_MS" in js,
           "no recovery poll — a long outage would stay stuck until reload")
    _check("guest.js: live events on an empty list trigger a re-fetch",
           "function horseFromEvent(" in js,
           "socket handlers still read state.horses directly, so an empty "
           "list can never heal from live traffic")
    _check("guest.js: horse-fetch failures are logged, not swallowed",
           "Horse list fetch failed (status " in js and
           "function logWarn(" in js,
           "no console telemetry on a failed horse fetch")
    # \r?\n — the checked-in file uses CRLF, so a bare \n never matches.
    swallow = re.search(r"getJSON\(API\.horses\);\s*\r?\n\s*if \(!resp\.ok\) return;",
                        js)
    _check("guest.js: the bare silent-swallow return is gone",
           swallow is None,
           "refreshHorses still swallows a non-OK response silently")
    _check("guest.js: the placeholder distinguishes failure from waiting",
           "Trouble loading horses" in js,
           "a stuck list still reads 'Waiting for horses…', which is "
           "indistinguishable from a slow first load")


# -----------------------------------------------------------------------------
# Phase 2A.5: onboarding flow (splash + how-it-works + help button)
# -----------------------------------------------------------------------------

def test_onboarding_markup_present():
    _reset()
    app = _make_app()
    client = app.test_client()

    r = client.get("/la-subasta/")
    _check("GET /la-subasta/ returns 200", r.status_code == 200,
           f"status={r.status_code}")
    html = r.get_data(as_text=True)

    # Splash screen markup
    splash_markers = [
        ('id="ls-splash"',       "splash container"),
        ('ls-splash-inner',      "splash inner brand block"),
    ]
    for marker, label in splash_markers:
        _check(f"HTML contains splash {label}", marker in html,
               f"missing marker: {marker!r}")

    # How It Works markup + required copy
    hiw_markers = [
        ('id="ls-howitworks"',   "how-it-works container"),
        ('id="ls-hiw-close"',    "how-it-works close button"),
        ('id="ls-hiw-vamos"',    "¡VAMOS! button id"),
        ('¡BIENVENIDOS TO LA SUBASTA!', "bienvenidos title"),
        ('RENEGADE',             "RENEGADE example horse"),
        ('COMMANDMENT',          "COMMANDMENT example horse"),
        ('FURTHER ADO',          "FURTHER ADO example horse"),
        ('¡VAMOS!',              "VAMOS button text"),
        ('60%',                  "60% win share"),
        ('25%',                  "25% place share"),
        ('15%',                  "15% show share"),
    ]
    for marker, label in hiw_markers:
        _check(f"HTML contains how-it-works {label}", marker in html,
               f"missing marker: {marker!r}")

    # Persistent "?" help button in the header
    help_markers = [
        ('id="ls-help-btn"',     "help button id"),
        ('aria-label="How it works"', "help button aria-label"),
    ]
    for marker, label in help_markers:
        _check(f"HTML contains help-button {label}", marker in html,
               f"missing marker: {marker!r}")


def test_onboarding_2027_pesos_copy():
    """DDM 2027 soft launch — funny-money copy must replace the old dollar
    framing. Asserts new pesos phrases present and old dollar/payment
    phrases absent."""
    _reset()
    app = _make_app()
    client = app.test_client()

    r = client.get("/la-subasta/")
    _check("GET /la-subasta/ returns 200", r.status_code == 200)
    html = r.get_data(as_text=True)

    # Required 2027 phrases — these are the verification markers from spec.
    # Case-insensitive: the heading "30 MIN BEFORE POST TIME" satisfies the
    # "30 min before post time" requirement; we don't care about CSS casing.
    html_lc = html.lower()
    required_phrases = [
        "100 pesos",
        "pesos pool",
        "Every owner pays their winning bid",
        "30 min before post time",
        "soft launch",
        "funny money",
        "everyone starts with 100 pesos",
        "bid on up to 3 caballos",
        "pesos pool locked in",
    ]
    for phrase in required_phrases:
        _check(f"HTML contains 2027 copy {phrase!r}",
               phrase.lower() in html_lc,
               f"missing: {phrase!r}")

    # 2027: physical-prize framing removed — no championship/trophy/tequila
    # promise until Joey finalizes the prize structure. Case-insensitive so
    # any re-introduction (CHAMPIONSHIP, Trophy, etc.) is caught.
    for phrase in ("championship", "trophy", "tequila"):
        _check(f"HTML no longer promises a physical prize {phrase!r}",
               phrase not in html_lc,
               f"still present: {phrase!r}")

    # Forbidden phrases — all the old 2026 dollar/payment framing
    forbidden_phrases = [
        "$",
        "Venmo",
        "Zelle",
        "5:42",
        "5:57",
        "pay up",
        "Owes",
        "total pot",
    ]
    for phrase in forbidden_phrases:
        _check(f"HTML no longer contains old phrase {phrase!r}",
               phrase not in html,
               f"still present: {phrase!r}")


def test_onboarding_js_state_machine():
    """Guest JS must reference the la_subasta_onboarded localStorage key
    and wire the splash + how-it-works + help-button handlers."""
    _reset()
    app = _make_app()
    client = app.test_client()
    js = client.get("/la-subasta/static/js/guest.js").get_data(as_text=True)

    _check("JS uses la_subasta_onboarded key",
           "la_subasta_onboarded" in js)
    _check("JS references the splash element",
           "ls-splash" in js)
    _check("JS references the how-it-works element",
           "ls-howitworks" in js)
    _check("JS references the help button",
           "ls-help-btn" in js)


# -----------------------------------------------------------------------------
# Phase 1.6: admin reset endpoint (testing only)
# -----------------------------------------------------------------------------

def _populate_for_reset_test(client):
    """Helper: register two bidders, place bids on 3 horses, scratch 1, lock+settle."""
    transition(AuctionState.OPEN)
    alice = client.post("/la-subasta/api/register",
                        json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    bob = client.post("/la-subasta/api/register",
                      json={"name": "Bob", "emoji": "🐴"}).get_json()["bidder"]

    # Walk the bid ladder so multiple bid rows exist per horse
    for horse_id, amount in [(1, 5), (2, 3), (3, 2)]:
        for i in range(1, amount + 1):
            actor = alice if i % 2 == 1 else bob
            r = client.post("/la-subasta/api/bid",
                            json={"bidder_id": actor["id"],
                                  "horse_id": horse_id, "amount": i})
            assert r.status_code == 200, f"setup bid failed: {r.get_json()}"

    r = _lq_scratch(client, 7)  # on the LQ admin page; La Subasta's reset never touches it
    assert r.status_code == 200, r.get_json()
    # Lock + enter results so ownership + payouts rows exist
    r = client.post("/la-subasta/api/admin/lock")
    assert r.status_code == 200, r.get_json()
    r = client.post("/la-subasta/api/admin/results",
                    json={"win": 1, "place": 2, "show": 3})
    assert r.status_code == 200, r.get_json()
    return alice, bob


def _row_count(table):
    from la_subasta.models import get_conn
    return get_conn().execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def test_reset_missing_confirm():
    _reset()
    app = _make_app()
    client = app.test_client()
    _populate_for_reset_test(client)

    r = client.post("/la-subasta/api/admin/reset?scope=bids")
    _check("POST without confirm returns 400", r.status_code == 400,
           f"status={r.status_code} body={r.get_json()}")
    body = r.get_json()
    _check("missing-confirm error mentions TESTING",
           "TESTING" in (body.get("error") or ""),
           f"got {body}")

    # And data should be untouched
    _check("bids untouched when confirm missing", _row_count("bids") > 0)


def test_reset_wrong_confirm():
    _reset()
    app = _make_app()
    client = app.test_client()
    _populate_for_reset_test(client)

    r = client.post("/la-subasta/api/admin/reset?scope=bids&confirm=WRONG")
    _check("POST with confirm=WRONG returns 400", r.status_code == 400)
    # Case sensitivity — lowercase "testing" must also fail
    r = client.post("/la-subasta/api/admin/reset?scope=bids&confirm=testing")
    _check("POST with confirm=testing (lowercase) returns 400",
           r.status_code == 400)
    _check("bids untouched when confirm wrong", _row_count("bids") > 0)


def test_reset_invalid_scope():
    _reset()
    app = _make_app()
    client = app.test_client()
    _populate_for_reset_test(client)

    r = client.post("/la-subasta/api/admin/reset?scope=bogus&confirm=TESTING")
    _check("invalid scope returns 400", r.status_code == 400)
    body = r.get_json()
    _check("invalid scope error names the allowed values",
           all(s in (body.get("error") or "") for s in ("bids", "full", "state")),
           f"got {body}")

    # Missing scope param entirely
    r = client.post("/la-subasta/api/admin/reset?confirm=TESTING")
    _check("missing scope returns 400", r.status_code == 400)
    _check("data still intact after invalid scope", _row_count("bids") > 0)


def test_reset_bids_wipes_state_data_keeps_bidders():
    _reset()
    app = _make_app()
    client = app.test_client()
    alice, bob = _populate_for_reset_test(client)

    bids_before = _row_count("bids")
    own_before = _row_count("ownership")
    pay_before = _row_count("payouts")
    bidders_before = _row_count("bidders")
    _check("setup placed >0 bids", bids_before > 0, f"got {bids_before}")
    _check("setup created ownership rows", own_before > 0)
    _check("setup created payout rows", pay_before > 0)
    _check("setup created >=2 real bidders + House",
           bidders_before >= 3, f"got {bidders_before}")

    r = client.post("/la-subasta/api/admin/reset?scope=bids&confirm=TESTING")
    _check("scope=bids returns 200", r.status_code == 200, f"body={r.get_json()}")
    body = r.get_json()
    _check("response.success = True", body.get("success") is True)
    _check("response.scope = 'bids'", body.get("scope") == "bids")
    _check("response.auction_state = NOT_STARTED",
           body.get("auction_state") == "NOT_STARTED")
    _check("response.deleted.bids matches table count",
           body["deleted"]["bids"] == bids_before,
           f"got {body['deleted']['bids']} expected {bids_before}")
    _check("response.deleted.ownership matches",
           body["deleted"]["ownership"] == own_before)
    _check("response.deleted.payouts matches",
           body["deleted"]["payouts"] == pay_before)
    _check("response.deleted.bidders = 0 for scope=bids",
           body["deleted"]["bidders"] == 0)

    # Verify SQL state
    _check("bids table empty after scope=bids", _row_count("bids") == 0)
    _check("ownership empty after scope=bids", _row_count("ownership") == 0)
    _check("payouts empty after scope=bids", _row_count("payouts") == 0)
    _check("bidders preserved after scope=bids",
           _row_count("bidders") == bidders_before)

    # State should be NOT_STARTED with total_pot=0
    state_row = get_state_row()
    _check("auction_state.state = NOT_STARTED after scope=bids",
           state_row["state"] == "NOT_STARTED")
    _check("auction_state.total_pot = 0 after scope=bids",
           state_row["total_pot"] == 0)

    # The scratch is La Quiniela's: a La Subasta reset leaves it alone
    _check("La Quiniela's scratch of 7 survives scope=bids",
           7 not in ls_field.current())


def test_reset_full_removes_everything_keeps_house():
    _reset()
    app = _make_app()
    client = app.test_client()
    alice, bob = _populate_for_reset_test(client)

    real_bidders_before = len(bidding.list_bidders(include_house=False))
    _check("real bidders >= 2 before reset", real_bidders_before >= 2,
           f"got {real_bidders_before}")

    r = client.post("/la-subasta/api/admin/reset?scope=full&confirm=TESTING")
    _check("scope=full returns 200", r.status_code == 200, f"body={r.get_json()}")
    body = r.get_json()
    _check("scope=full response.scope = 'full'", body.get("scope") == "full")
    _check("scope=full response.deleted.bidders == real bidder count",
           body["deleted"]["bidders"] == real_bidders_before,
           f"got {body['deleted']['bidders']} expected {real_bidders_before}")

    _check("no real bidders left after scope=full",
           len(bidding.list_bidders(include_house=False)) == 0)
    # House row survives
    all_bidders = bidding.list_bidders(include_house=True)
    identities = {b["identity"] for b in all_bidders}
    _check("House bidder identity present after scope=full",
           "The House 🎩" in identities, f"got {identities}")
    _check("exactly 1 bidder (House) after scope=full",
           len(all_bidders) == 1, f"got {len(all_bidders)}")

    # The cached house_bidder_id() must still resolve to a real row
    from la_subasta.models import house_bidder_id, get_conn
    hid = house_bidder_id()
    row = get_conn().execute(
        "SELECT id, identity FROM bidders WHERE id = ?", (hid,),
    ).fetchone()
    _check("house_bidder_id() resolves to surviving House row after reset_full",
           row is not None and row["identity"] == "The House 🎩",
           f"got {dict(row) if row else None}")

    # Bids/ownership/payouts also gone (reset_full calls reset_bids first)
    _check("bids empty after scope=full", _row_count("bids") == 0)
    _check("ownership empty after scope=full", _row_count("ownership") == 0)
    _check("payouts empty after scope=full", _row_count("payouts") == 0)


def test_reset_state_only_changes_auction_state():
    _reset()
    app = _make_app()
    client = app.test_client()
    alice, bob = _populate_for_reset_test(client)

    bids_before = _row_count("bids")
    own_before = _row_count("ownership")
    pay_before = _row_count("payouts")
    bidders_before = _row_count("bidders")
    state_before = get_state().value
    _check("auction not in NOT_STARTED before reset_state",
           state_before != "NOT_STARTED", f"got {state_before}")

    r = client.post("/la-subasta/api/admin/reset?scope=state&confirm=TESTING")
    _check("scope=state returns 200", r.status_code == 200, f"body={r.get_json()}")
    body = r.get_json()
    _check("scope=state response.scope = 'state'", body.get("scope") == "state")
    _check("scope=state response.auction_state = NOT_STARTED",
           body.get("auction_state") == "NOT_STARTED")
    _check("scope=state reports 0 deletions in every category",
           all(body["deleted"][k] == 0
               for k in ("bids", "ownership", "payouts", "bidders")),
           f"got {body['deleted']}")

    # Nothing else changed
    _check("bids unchanged after scope=state",
           _row_count("bids") == bids_before)
    _check("ownership unchanged after scope=state",
           _row_count("ownership") == own_before)
    _check("payouts unchanged after scope=state",
           _row_count("payouts") == pay_before)
    _check("bidders unchanged after scope=state",
           _row_count("bidders") == bidders_before)
    _check("auction state IS NOT_STARTED after scope=state",
           get_state().value == "NOT_STARTED")


def test_reset_preserves_settings_and_audit():
    """Reset should leave admin tunables + audit log alone."""
    _reset()
    app = _make_app()
    client = app.test_client()

    # Set an override and trigger an audit entry
    from la_subasta import settings as _settings
    _settings.set_setting("HOUSE_FUND_LABEL", "ResetTest Fund")
    audit_before = len(_settings.get_audit_log(limit=500))
    _check("override + audit entry present pre-reset",
           _settings.get_setting("HOUSE_FUND_LABEL") == "ResetTest Fund"
           and audit_before >= 1, f"audit_before={audit_before}")

    r = client.post("/la-subasta/api/admin/reset?scope=full&confirm=TESTING")
    _check("scope=full returns 200", r.status_code == 200)

    _check("HOUSE_FUND_LABEL override preserved after scope=full reset",
           _settings.get_setting("HOUSE_FUND_LABEL") == "ResetTest Fund")
    audit_after = len(_settings.get_audit_log(limit=500))
    _check("audit log preserved (>= entries before reset)",
           audit_after >= audit_before, f"before={audit_before} after={audit_after}")


def test_reset_broadcasts_auction_reset_event():
    """SocketIO 'auction_reset' fires with {scope, timestamp} payload."""
    _reset()

    events = []

    class _StubSocketIO:
        def emit(self, event, payload, room=None):
            events.append((event, payload, room))

    from la_subasta import notifications as nots
    app = _make_app()
    nots.init_notifications(_StubSocketIO())
    client = app.test_client()
    _populate_for_reset_test(client)
    events.clear()

    r = client.post("/la-subasta/api/admin/reset?scope=bids&confirm=TESTING")
    _check("scope=bids POST returns 200", r.status_code == 200)

    reset_events = [e for e in events if e[0] == "auction_reset"]
    _check("auction_reset event emitted exactly once for scope=bids",
           len(reset_events) == 1, f"got {len(reset_events)}")
    payload = reset_events[0][1]
    _check("auction_reset payload includes scope='bids'",
           payload.get("scope") == "bids", f"got {payload}")
    _check("auction_reset payload includes numeric timestamp",
           isinstance(payload.get("timestamp"), (int, float))
           and payload["timestamp"] > 0,
           f"got {payload}")

    # Verify scope=full and scope=state also fire with matching scope strings
    events.clear()
    r = client.post("/la-subasta/api/admin/reset?scope=full&confirm=TESTING")
    assert r.status_code == 200, r.get_json()
    reset_events = [e for e in events if e[0] == "auction_reset"]
    _check("auction_reset fires for scope=full with matching scope",
           len(reset_events) == 1 and reset_events[0][1]["scope"] == "full",
           f"got {reset_events}")

    events.clear()
    r = client.post("/la-subasta/api/admin/reset?scope=state&confirm=TESTING")
    assert r.status_code == 200, r.get_json()
    reset_events = [e for e in events if e[0] == "auction_reset"]
    _check("auction_reset fires for scope=state with matching scope",
           len(reset_events) == 1 and reset_events[0][1]["scope"] == "state",
           f"got {reset_events}")

    nots.init_notifications(None)


def test_reset_route_registered():
    """The endpoint must be addressable under the la-subasta prefix."""
    _reset()
    app = _make_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    _check("/la-subasta/api/admin/reset route registered",
           "/la-subasta/api/admin/reset" in rules,
           f"sample rules: {sorted(r for r in rules if 'admin' in r)}")


def test_existing_dashboard_still_loads():
    """Smoke-check the full pi5 app: main.py must import without error and
    the existing / route (dashboard) must still register."""
    # Save any previously imported main module to avoid test pollution
    for mod in list(sys.modules.keys()):
        if mod == "main":
            del sys.modules[mod]
    try:
        import main  # noqa: F401
    except Exception as exc:
        _check("pi5/main.py imports cleanly", False, str(exc))
        return
    _check("pi5/main.py imports cleanly", True)

    # Verify routes registered
    rules = {rule.rule for rule in main.app.url_map.iter_rules()}
    _check("dashboard / route registered", "/" in rules)
    _check("dashboard /spectator route registered", "/spectator" in rules)
    _check("/la-subasta/api/state route registered",
           "/la-subasta/api/state" in rules)
    _check("/la-subasta/api/register route registered",
           "/la-subasta/api/register" in rules)
    _check("/la-subasta/api/bid route registered",
           "/la-subasta/api/bid" in rules)
    _check("/la-subasta/api/bid/undo route registered",
           "/la-subasta/api/bid/undo" in rules)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main():
    print(f"La Subasta Phase 1 smoke test\n  DB: {_TMP_DB}")

    _run("state endpoint", test_state_endpoint)
    _run("state endpoint — total_pot across bid shapes (regression)",
         test_state_endpoint_pot_data_shapes)
    _run("state endpoint — concurrent reads, no sqlite MISUSE (regression)",
         test_state_endpoint_concurrent_no_sqlite_misuse)
    _run("registration + uniqueness", test_register_endpoint)
    _run("bid validation", test_bid_validation)
    _run("bid undo (10s window)", test_bid_undo)
    _run("payout computation", test_payouts)
    _run("House payout (zero-bid horse wins)", test_house_payout)

    # Phase 1.5
    _run("settings — defaults", test_settings_get_defaults)
    _run("settings — set + audit", test_settings_set_and_audit)
    _run("settings — validation", test_settings_validation)
    _run("settings — lock when open", test_settings_lock_when_open)
    _run("settings — reset", test_settings_reset)
    _run("settings — bidding picks up MAX_RAISE", test_bidding_picks_up_max_raise_change)
    _run("settings — payout preset parser", test_payout_preset_parser)
    _run("settings — payout uses current preset (3 presets)", test_payout_uses_current_preset)
    _run("settings — API endpoints", test_settings_api_endpoints)
    _run("settings — socketio broadcast", test_settings_changed_socketio_broadcast)

    # Phase 2A
    _run("guest UI — page served", test_guest_page_served)
    _run("guest UI — /api/horses shape", test_horses_endpoint_shape)
    _run("field — La Quiniela's, with 22 for 9", test_field_from_lq_store_with_replacement)
    _run("field — HORSE n without a name", test_field_horse_n_fallback)
    _run("field — 21-24 bid on, frozen and paid", test_field_21_to_24_accepted)
    _run("field — a bid on a horse not in it is refused", test_field_bid_on_absent_horse_rejected)
    _run("field — no board: the stand-in; no mock racing service",
         test_field_no_board_standin_and_no_mock)
    _run("scratch — while open: bids voided, horse removed, pushed", test_scratch_while_open)
    _run("scratch — applied by the store's listener", test_scratch_store_listener_direct)
    _run("scratch — after the lock: ownership voided, owed drops, refund", test_scratch_after_lock)
    _run("scratch — replacement: 22 enters with no bids", test_scratch_replacement_adds_fresh_horse)
    _run("scratch — undo restores the field, not the bids", test_scratch_undo_restores_field_not_bids)
    _run("scratch — a restart does not double-void", test_scratch_restart_does_not_double_void)
    _run("scratch — old ownership table migrated", test_ownership_migration)
    _run("scratch — guest page follows the field live", test_guest_js_follows_the_field)
    _run("guest UI — /api/horses scratched flag round-trip (regression)",
         test_horses_scratched_flag_roundtrip)
    _run("guest UI — static assets served", test_static_assets_served)
    _run("api — dynamic responses are no-store (cache hardening)",
         test_api_responses_are_no_store)
    _run("guest UI — no '$' currency in guest.js (2027 pesos mode)",
         test_guest_js_no_dollar_currency)
    _run("guest UI — 2027 pesos button format + footer year",
         test_guest_2027_pesos_button_format)
    _run("dev panel — markup present + hidden-by-default + JS gating",
         test_dev_panel_markup_and_gating)
    _run("dev panel — La Subasta's own scratch route is gone",
         test_own_scratch_route_gone)
    _run("dev panel — /api/admin/transition endpoint",
         test_admin_transition_endpoint)
    _run("dev panel — /api/state includes num_bidders + num_bids",
         test_state_includes_bidder_and_bid_counts)
    _run("guest UI — auction_state_changed broadcast", test_auction_state_changed_broadcast)
    _run("guest UI — JS listens for auction_state_changed", test_guest_js_listens_for_state_changed)
    _run("guest UI — initial horse load after registration (regression)",
         test_initial_horse_load_after_registration)

    # Phase 2A.5
    _run("onboarding — splash + how-it-works + help markup", test_onboarding_markup_present)
    _run("onboarding — 2027 pesos copy", test_onboarding_2027_pesos_copy)
    _run("onboarding — JS state machine wiring", test_onboarding_js_state_machine)

    # Phase 1.6 — admin reset (testing only)
    _run("reset — missing confirm rejected", test_reset_missing_confirm)
    _run("reset — wrong confirm rejected", test_reset_wrong_confirm)
    _run("reset — invalid scope rejected", test_reset_invalid_scope)
    _run("reset — scope=bids wipes state data, keeps bidders",
         test_reset_bids_wipes_state_data_keeps_bidders)
    _run("reset — scope=full removes everything except House",
         test_reset_full_removes_everything_keeps_house)
    _run("reset — scope=state only flips auction_state",
         test_reset_state_only_changes_auction_state)
    _run("reset — preserves settings overrides + audit log",
         test_reset_preserves_settings_and_audit)
    _run("reset — auction_reset SocketIO event payload",
         test_reset_broadcasts_auction_reset_event)
    _run("reset — endpoint route registered", test_reset_route_registered)

    _run("existing dashboard still loads", test_existing_dashboard_still_loads)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)

    # Cleanup
    try:
        os.remove(_TMP_DB)
    except OSError:
        pass

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

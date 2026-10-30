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

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
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
from la_subasta import scratches as ls_scratches  # noqa: E402
from la_quiniela import board as lq_board  # noqa: E402
from la_quiniela.horses import HORSE_COUNT as LQ_HORSE_COUNT, HorseStore  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402
from la_subasta import blueprint as ls_blueprint  # noqa: E402
from host_pin import HOST_COOKIE, host_pin_bp, pin_cookie  # noqa: E402

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

TEST_PIN = "test-pin-2027"
_SECRETS = {}                   # what each guest's phone holds: the secret register_bidder handed out, by bidder id
_register_bidder = bidding.register_bidder


def _joining(name, emoji, event_year=la_config.EVENT_YEAR):
    bidder = _register_bidder(name, emoji, event_year)
    _SECRETS[bidder["id"]] = bidder["secret"]
    return bidder


bidding.register_bidder = _joining          # the register route and the tests' direct joins both go through it


class _Phone(FlaskClient):
    """The host's iPad and a guest's phone in one client. It carries the PIN's cookie, and a bid, an undo or an identity
    check names the secret its bidder was given at join, as the page does, unless the test puts a `secret` of its own in
    the body (a wrong one, or None)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_cookie(HOST_COOKIE, pin_cookie(TEST_PIN), path="/")

    def open(self, *args, **kwargs):
        body = kwargs.get("json")
        if (isinstance(body, dict) and "secret" not in body and args and isinstance(args[0], str)
                and args[0].endswith(("/api/bid", "/api/bid/undo", "/api/check-identity"))):
            who = body.get("bidder_id")
            kwargs["json"] = {**body, "secret": _SECRETS.get(who) if isinstance(who, int) else None}
        return super().open(*args, **kwargs)


def _stranger(app):
    """A phone with no PIN cookie and no secret of its own."""
    return FlaskClient(app, app.response_class)


def _make_app(store=None):
    """A Flask app with the la_subasta blueprint and La Quiniela's board
    routes (the admin page's names and scratches), over a fresh La Quiniela
    board with a memory-only store (or `store`, to have one on a database
    file) and no bridge: La Subasta reads its horses from that board's
    store, as it does in the app."""
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["HOST_PIN"] = TEST_PIN
    app.register_blueprint(host_pin_bp)
    app.test_client_class = _Phone
    lq_board.init_board(bridge=None, store=store, log_dir=os.path.join(_LQ_TMP, "logs"),
                        results_path=os.path.join(_LQ_TMP, "results.json"))
    ls_field.set_store_source(None)          # the board's store, as in the app
    ls_scratches.forget()                    # one test's field is never diffed against the last test's
    init_la_subasta(socketio=None)
    app.register_blueprint(la_subasta_bp)
    app.register_blueprint(lq_board.quiniela_board_bp)
    return app


def _raw_bid(client, bidder, secret, horse, amount):
    """A bid whose amount is raw JSON text (NaN and Infinity are not valid JSON; Python's parser takes them)."""
    body = '{"bidder_id": %d, "horse_id": %d, "amount": %s, "secret": %s}' % (bidder["id"], horse, amount, json.dumps(secret))
    return client.post("/la-subasta/api/bid", data=body, content_type="application/json")


def _lq_names(client, text=None):
    r = client.put("/api/quiniela/horses", json={"text": text if text is not None else NAMES_2026})
    assert r.status_code == 200, r.get_json()


def _lq_scratch(client, horse, number=None, name=None):
    body = {"horse": horse}
    if number is not None:
        body["replacement"] = {"number": number, "name": name or ""}
    return client.post("/api/quiniela/scratch", json=body)


# The result the Control Center's SET WINNERS records (main.py save_results
# writes pi5/data/results.json; here the board's results_path): the real 1st,
# 2nd and 3rd, and in next the finishers after them (La Quiniela's business).
# La Subasta reads it through the board (BettingBoard.results()).
_LQ_RESULTS = os.path.join(_LQ_TMP, "results.json")


def _set_winners(win, place, show, after=(), ties=()):
    tmp = _LQ_RESULTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"win": win, "place": place, "show": show, "next": list(after), "ties": [list(t) for t in ties],
                   "timestamp": "2027-05-01T19:02:00"}, fh)
    os.replace(tmp, _LQ_RESULTS)


def _clear_winners():
    """RESET on the Control Center, or Reset betting: the result goes."""
    try:
        os.remove(_LQ_RESULTS)
    except OSError:
        pass


def _ledger(client):
    r = client.get("/la-subasta/api/admin/payouts")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _by_finish(ledger):
    return {p["finish"]: p for p in ledger["payouts"]}


def _reset():
    _SECRETS.clear()
    reset_db_for_tests(_TMP_DB)
    # After reset, re-init blueprint wiring (DB connection changed)
    init_db(_TMP_DB)
    _clear_winners()                             # no result left over from the last test


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


def test_another_years_bidder_is_unknown():
    """Gap review B7 and M4 (b): a bidder id from another event year is
    unknown everywhere. It can't bid (404, checked before anything else),
    check-identity doesn't know it, it is not listed, paid, exempted or
    given a portfolio, so it never owns a horse nobody charged it for; and
    its name and emoji are free this year (unique per year)."""
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    year = la_config.EVENT_YEAR
    old = bidding.register_bidder("Alice", "🌮", event_year=year - 1)["id"]

    def bid(bidder_id, horse, amount):
        return client.post("/la-subasta/api/bid",
                           json={"bidder_id": bidder_id, "horse_id": horse, "amount": amount})

    def check(**body):
        return client.post("/la-subasta/api/check-identity", json=body).get_json()

    r = bid(old, 1, 1)
    _check("auction not open yet: last year's id is told it is unknown (404), not that bidding is closed",
           r.status_code == 404 and r.get_json()["error"] == "Unknown bidder", f"{r.status_code} {r.get_json()}")
    transition(AuctionState.OPEN)
    r = bid(old, 1, 1)
    _check("auction open: last year's id bids, 404 Unknown bidder, and no bid is stored in either year",
           r.status_code == 404 and r.get_json()["error"] == "Unknown bidder"
           and bidding.count_bids() == 0 and bidding.count_bids(year - 1) == 0, f"{r.status_code} {r.get_json()}")
    _check("check-identity: last year's Alice is not known; the name is free this year",
           check(bidder_id=old, name="Alice", emoji="🌮") == {"success": True, "known": False, "available": True})
    _check("check-identity without a bidder_id answers availability only",
           check(name="Alice", emoji="🌮") == {"success": True, "available": True})

    r = client.post("/la-subasta/api/register", json={"name": "Alice", "emoji": "🌮"})
    new = (r.get_json() or {}).get("bidder") or {}
    _check("the same name and emoji register this year (unique per year), as a new bidder",
           r.status_code == 200 and new.get("id") not in (None, old) and new.get("event_year") == year,
           f"{r.status_code} {r.get_json()}")
    _check("check-identity: this year's Alice is known, and the name is taken",
           check(bidder_id=new["id"], name="Alice", emoji="🌮") == {"success": True, "known": True, "available": False})
    _check("...not with another emoji, nor last year's id with this year's name",
           check(bidder_id=new["id"], name="Alice", emoji="🐴")["known"] is False
           and check(bidder_id=old, name="Alice", emoji="🌮")["known"] is False)
    _check("...nor an id that is not a number", check(bidder_id="x", name="Alice", emoji="🌮")["known"] is False)
    r = client.post("/la-subasta/api/register", json={"name": "Alice", "emoji": "🌮"})
    _check("within a year the name and emoji are still unique (409)", r.status_code == 409, f"{r.status_code}")

    listed = [b["id"] for b in client.get("/la-subasta/api/bidders").get_json()["bidders"]]
    _check("the admin list has this year's Alice only", listed == [new["id"]], f"got {listed}")
    r_port = client.get(f"/la-subasta/api/bidders/{old}/portfolio")
    r_paid = client.post("/la-subasta/api/admin/paid", json={"bidder_id": old})
    r_cap = client.post("/la-subasta/api/admin/cap-exempt", json={"bidder_id": old, "exempt": True})
    _check("last year's id: no portfolio, not marked paid, not exempted (404 each)",
           (r_port.status_code, r_paid.status_code, r_cap.status_code) == (404, 404, 404),
           f"{r_port.status_code} {r_paid.status_code} {r_cap.status_code}")
    row = bidding.get_bidder(old, year - 1)
    _check("...and its own row, in its own year, is untouched",
           row is not None and row["paid"] == 0 and row["cap_exempt"] == 0
           and [b["id"] for b in bidding.list_bidders(year - 1)] == [old], f"got {row}")
    _check("this year's Alice has a portfolio", client.get(f"/la-subasta/api/bidders/{new['id']}/portfolio").status_code == 200)

    r = bid(new["id"], 1, 1)
    _check("this year's Alice bids", r.status_code == 200, f"{r.status_code} {r.get_json()}")
    client.post("/la-subasta/api/admin/lock", json={"confirm": True})
    _set_winners(1, 2, 3)
    owners = [o["bidder_id"] for o in models.get_conn().execute("SELECT bidder_id FROM ownership").fetchall()]
    win = _by_finish(_ledger(client))["win"]
    _check("the owner and the WIN payout are this year's Alice", owners == [new["id"]] and win["bidder_id"] == new["id"],
           f"owners {owners}, win {win}")


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


# -----------------------------------------------------------------------------
# Money integrity (Joey, 2026-10-28; the 2026-10-20 review's S1, S2, S3, S6 and S8)
# -----------------------------------------------------------------------------

def test_a_guest_is_a_number_and_a_secret():
    """S1: a phone proves who it is with the secret the server issued at join, not with a number every list and every
    broadcast shows. A bid and an undo carry it and get a 401 without the right one; the secret is kept as a hash and
    is in no list and no broadcast; check-identity knows a phone only by its secret too; a guest row from before the
    secret has none, so its phone is unknown and goes to the join screen."""
    from la_subasta import notifications as nots
    _reset()
    app = _make_app()
    client = app.test_client()
    stranger = _stranger(app)
    _lq_names(client)
    ev = _Events()
    nots.init_notifications(ev)
    try:
        transition(AuctionState.OPEN)
        joined = [client.post("/la-subasta/api/register", json={"name": n, "emoji": e}) for n, e in (("Alice", "🌮"), ("Bob", "🐴"))]
        (alice, alice_secret), (bob, bob_secret) = [(r.get_json()["bidder"], r.get_json()["secret"]) for r in joined]
        hashes = {r["id"]: r["secret_hash"] for r in models.get_conn().execute("SELECT id, secret_hash FROM bidders").fetchall()}
        _check("register hands out a secret beside the bidder (32 random bytes, a different one for each guest) and keeps only its hash",
               all(r.status_code == 200 for r in joined) and len(alice_secret) >= 43 and alice_secret != bob_secret
               and hashes[alice["id"]] == hashlib.sha256(alice_secret.encode()).hexdigest() and bob_secret not in hashes.values()
               and "secret" not in alice and "secret_hash" not in alice, f"{alice} {hashes}")

        def bid(who, secret, horse=1, amount=1):
            body = {"bidder_id": who["id"], "horse_id": horse, "amount": amount}
            if secret is not ...:
                body["secret"] = secret
            return stranger.post("/la-subasta/api/bid", json=body)

        wrong = {"no secret": ..., "a wrong one": "x" * 43, "another guest's secret": bob_secret, "an empty secret": "",
                 "a secret that is no string": 12345, "a list": [alice_secret]}
        answers = {name: bid(alice, secret) for name, secret in wrong.items()}
        _check("a bid with no secret, a wrong one, another guest's, an empty one or a non-string: 401, and nothing is stored",
               all(r.status_code == 401 and r.get_json() == {"success": False, "error": ls_blueprint.NOT_YOUR_GUEST_NUMBER}
                   for r in answers.values()) and bidding.count_bids() == 0, str({k: (r.status_code, r.get_json()) for k, r in answers.items()}))
        r = bid({"id": 99999}, alice_secret)
        _check("a guest number nobody has: 404 Unknown bidder, as before (the page goes to the join screen on either)",
               r.status_code == 404 and r.get_json()["error"] == "Unknown bidder", f"{r.status_code} {r.get_json()}")
        r = bid(alice, alice_secret)
        bid_id = r.get_json()["bid"]["bid_id"]
        _check("the right secret bids", r.status_code == 200 and bidding.count_bids() == 1, f"{r.status_code} {r.get_json()}")

        def undo(who, secret, bid_id=bid_id):
            body = {"bid_id": bid_id, "bidder_id": who["id"]}
            if secret is not ...:
                body["secret"] = secret
            return stranger.post("/la-subasta/api/bid/undo", json=body)

        answers = {name: undo(alice, secret) for name, secret in wrong.items()}
        _check("an undo with no secret, a wrong one or another guest's: 401, and the bid stands",
               all(r.status_code == 401 for r in answers.values()) and bidding.current_high_bid(1) is not None,
               str({k: r.status_code for k, r in answers.items()}))
        r = undo(bob, bob_secret)
        _check("Bob, with his own number and secret, cannot undo Alice's bid: 400, the bid stands",
               r.status_code == 400 and "own bids" in r.get_json()["error"] and bidding.current_high_bid(1) is not None, f"{r.status_code} {r.get_json()}")

        def check(who, secret):
            body = {"bidder_id": who["id"], "name": who["name"], "emoji": who["emoji"]}
            if secret is not ...:
                body["secret"] = secret
            return stranger.post("/la-subasta/api/check-identity", json=body).get_json()

        _check("check-identity: known with the right secret, not without, not with another's or a wrong one or a non-string",
               check(alice, alice_secret)["known"] is True
               and [check(alice, s)["known"] for s in (..., "x" * 43, bob_secret, "", 5, [1])] == [False] * 6)

        # Nowhere a phone or the host's page can read it: the lists, the broadcasts, the replies
        r = bid(bob, bob_secret, horse=2)
        listed = {
            "/api/bidders": client.get("/la-subasta/api/bidders").get_data(as_text=True),
            "/api/horses": client.get("/la-subasta/api/horses").get_data(as_text=True),
            "/api/state": client.get("/la-subasta/api/state").get_data(as_text=True),
            "a portfolio": client.get(f"/la-subasta/api/bidders/{alice['id']}/portfolio").get_data(as_text=True),
            "the bid's reply": r.get_data(as_text=True),
            "a check-identity reply": json.dumps(check(alice, alice_secret)),
            "the admin payouts": client.get("/la-subasta/api/admin/payouts").get_data(as_text=True),
            "the broadcasts": json.dumps([(name, payload) for name, payload in ev.events], default=str, ensure_ascii=False),
        }
        hidden = (alice_secret, bob_secret, *hashes.values(), "secret_hash")
        leaks = [name for name, text in listed.items() if any(s in text for s in hidden)]
        _check("the secret and its hash are in no list, no reply and no broadcast", r.status_code == 200 and leaks == [], str(leaks))

        # A guest row from before the secret: no hash, so unknown
        models.get_conn().execute("UPDATE bidders SET secret_hash = NULL WHERE id = ?", (bob["id"],))
        r = bid(bob, bob_secret, horse=3)
        _check("a guest row with no secret (from before it): its bids are 401 whatever it sends, and check-identity does not know it",
               r.status_code == 401 and check(bob, bob_secret)["known"] is False and check(bob, ...)["known"] is False,
               f"{r.status_code} {check(bob, bob_secret)}")
    finally:
        _done_with_events()


def test_the_host_pages_need_the_pin():
    """S1: /la-subasta/admin and every /api/admin/* take the host's PIN (pi5/host_pin.py, one PIN for every host page
    since 2026-10-30; the PIN, its cookie and every other write are tested whole in la_quiniela/test_dashboard.py). Without
    the cookie the page is the PIN form and the API a 401; with no PIN set a 503, never an open door; the guests' routes
    are not touched."""
    _reset()
    app = _make_app()
    host = app.test_client()
    stranger = _stranger(app)
    admin = [r for r in app.url_map.iter_rules() if r.rule == "/la-subasta/admin" or r.rule.startswith("/la-subasta/api/admin/")]
    calls = [(r.rule, m) for r in admin for m in sorted(r.methods - {"HEAD", "OPTIONS"})]
    _check("the host's routes: the page and every /api/admin/* route, a dozen at least", len(calls) >= 13 and ("/la-subasta/admin", "GET") in calls,
           str(calls))

    def knock(client, path, method):
        return client.open(path, method=method, json={} if method == "POST" else None)

    refused = {(p, m): knock(stranger, p, m) for p, m in calls}
    _check("with no cookie each of them is refused with a 401 and nothing is done (an empty body would have failed with a 400 or 409 otherwise)",
           all(r.status_code == 401 for r in refused.values()), str({k: r.status_code for k, r in refused.items() if r.status_code != 401}))
    html = refused[("/la-subasta/admin", "GET")].get_data(as_text=True)
    _check("...the page is the PIN form and none of the admin page", 'id="pin-form"' in html and "ls-admin-controls" not in html
           and "text/html" in refused[("/la-subasta/admin", "GET")].content_type)
    _check("...and no auction state moved: still NOT_STARTED, no bidder, no override", get_state() == AuctionState.NOT_STARTED
           and bidding.count_bidders() == 0)
    page = host.get("/la-subasta/admin")
    _check("with the cookie the page is the admin page, and it takes host_pin.js (a PIN that stops working sends it back to the form)",
           page.status_code == 200 and "ls-admin-controls" in page.get_data(as_text=True)
           and "/static/js/host_pin.js" in page.get_data(as_text=True) and host.get("/la-subasta/api/admin/payouts").status_code == 200)
    app.config["HOST_PIN"] = ""
    r = host.get("/la-subasta/api/admin/payouts")
    r2 = host.get("/la-subasta/admin")
    _check("no PIN set: the host pages are off (503, and it says where to set it), whatever cookie comes",
           r.status_code == r2.status_code == 503 and "DDM_HOST_PIN" in r.get_json()["error"], f"{r.status_code} {r2.status_code}")
    app.config["HOST_PIN"] = TEST_PIN
    open_to_all = {p: stranger.get(p).status_code for p in ("/la-subasta/", "/la-subasta/api/state", "/la-subasta/api/horses",
                                                            "/la-subasta/api/bidders", "/la-subasta/api/settings",
                                                            "/la-subasta/static/js/admin.js",
                                                            "/la-subasta/static/js/guest.js", "/la-subasta/static/css/la-subasta-admin.css")}
    _check("the guests' pages, lists and files stay open to everyone", all(code == 200 for code in open_to_all.values()), str(open_to_all))
    r = stranger.post("/la-subasta/api/register", json={"name": "Dave K", "emoji": "\U0001F32E"})
    _check("...and a guest can still join", r.status_code == 200 and r.get_json()["secret"])
    _check("admin.js no longer carries a PIN mechanism of its own (host_pin.js does it for every host page)",
           "askForThePin" not in _static(host, "/la-subasta/static/js/admin.js"))


def _post_on_a_thread(app, got, path, body):
    """A request on a thread of its own, the reply in got["r"]. The thread's sqlite handle is released at its end, as
    the other threaded tests do: left to the garbage collector it keeps the file open on Windows."""
    try:
        got["r"] = app.test_client().post(path, json=body)
    finally:
        models.close_conn()


def test_after_the_lock_only_the_host_voids_a_bid():
    """S2: an undo is a guest's way out of a fat-fingered bid while bidding is open. After the lock it would leave the
    horse with an owner who owes nothing, so only the host voids a bid then, through the admin route, which follows
    ownership: the horse goes to the runner-up at their bid, or is unowned with none."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        alice_bid = _bid(client, alice, 1, 3)
        bob_bid = _bid(client, bob, 1, 4)
        client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        _check("locked: Bob owns #1 at 4", (_ownership(1) or {}).get("bidder_id") == bob["id"] and _ownership(1)["winning_bid"] == 4)
        r = client.post("/la-subasta/api/bid/undo", json={"bid_id": bob_bid, "bidder_id": bob["id"]})
        _check("Bob's undo seconds after the lock: refused (400), the bid stands, Bob still owns it and owes it",
               r.status_code == 400 and r.get_json()["error"] == "Bidding is closed: only the host can void a bid now"
               and [b["voided"] for b in _bids_on(1)] == [0, 0] and (_ownership(1) or {}).get("bidder_id") == bob["id"]
               and bidding.bidder_portfolio(bob["id"])["total"] == 4, f"{r.status_code} {r.get_json()}")
        _set_winners(1, 2, 3)
        _check("...and a result that names #1 pays Bob its WIN", _by_finish(_ledger(client))["win"]["bidder_id"] == bob["id"])
        r = client.post("/la-subasta/api/admin/void", json={"bid_id": bob_bid, "reason": "fat finger"})
        _check("the host's void after the lock: 200, and the ownership follows the bids: Alice, at her 3",
               r.status_code == 200 and (_ownership(1) or {}).get("bidder_id") == alice["id"] and _ownership(1)["winning_bid"] == 3
               and bidding.bidder_portfolio(bob["id"])["total"] == 0 and _by_finish(_ledger(client))["win"]["bidder_id"] == alice["id"],
               f"{r.status_code} {r.get_json()} {_ownership(1)}")
        r = client.post("/la-subasta/api/admin/void", json={"bid_id": alice_bid, "reason": "no second bidder"})
        _check("...and with no second bidder the horse has no owner (the lock warned about it), as before",
               r.status_code == 200 and r.get_json()["unsold"] is True and _ownership(1) is None, f"{r.get_json()}")
    finally:
        _done_with_events()

    # An undo in flight at the lock: past its checks, held up on its way to the write lock, the lock lands first
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 2, 3)
        bob_bid = _bid(client, bob, 2, 4)
        got = {}
        entered, release = threading.Event(), threading.Event()
        real = bidding.write_txn

        def held_up(*args, **kwargs):
            entered.set()
            release.wait(10)
            return real(*args, **kwargs)

        bidding.write_txn = held_up
        t = threading.Thread(target=_post_on_a_thread, args=(app, got, "/la-subasta/api/bid/undo",
                                                              {"bid_id": bob_bid, "bidder_id": bob["id"]}))
        try:
            t.start()
            entered.wait(5)
            client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        finally:
            bidding.write_txn = real
            release.set()
            t.join(10)
        _check("an undo that passed its checks before the lock and reached the database after it is refused too: the bid and Bob's ownership stand",
               got["r"].status_code == 400 and bidding.current_high_bid(2)["bidder_id"] == bob["id"]
               and (_ownership(2) or {}).get("bidder_id") == bob["id"], f"{got['r'].status_code} {got['r'].get_json()}")
    finally:
        _done_with_events()


def test_a_bid_in_flight_at_the_lock_is_refused():
    """S3: bidding.py checked that the auction was open before taking the write lock and never inside it, so a bid past
    its checks and held up on its way to the database was accepted after the lock had frozen ownership: the high bid and the
    owner disagreed, and the pot was wrong. The state is read again under the lock now, as the field is. Deterministic here:
    the bid is held at the door of the write lock until the lock has landed."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 1, 1)
        got = {}
        entered, release = threading.Event(), threading.Event()
        real = bidding.write_txn

        def held_up(*args, **kwargs):
            entered.set()
            release.wait(10)
            return real(*args, **kwargs)

        bidding.write_txn = held_up
        t = threading.Thread(target=_post_on_a_thread, args=(app, got, "/la-subasta/api/bid",
                                                              {"bidder_id": bob["id"], "horse_id": 1, "amount": 2}))
        try:
            t.start()
            entered.wait(5)
            r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        finally:
            bidding.write_txn = real
            release.set()
            t.join(10)
        _check("the lock lands while Bob's bid is on its way", r.status_code == 200 and get_state() == AuctionState.LOCKED)
        high = bidding.current_high_bid(1)
        _check("Bob's bid reaches the database after the lock and is refused (400, not accepting bids); Alice's 1 is the high bid, and her ownership agrees",
               got["r"].status_code == 400 and "not accepting bids" in got["r"].get_json()["error"]
               and high["bidder_id"] == alice["id"] and high["amount"] == 1
               and (_ownership(1) or {}).get("bidder_id") == alice["id"] and _ownership(1)["winning_bid"] == 1
               and [b["bidder_id"] for b in _bids_on(1)] == [alice["id"]], f"{got['r'].status_code} {got['r'].get_json()} {high} {_ownership(1)}")
    finally:
        _done_with_events()


def test_bids_racing_the_lock_never_disagree_with_ownership():
    """S3's stress test, kept (the review's reproduction: ownership disagreed with the high bid on 7 to 12 horses in 5 of
    10 trials under 24 threads): two dozen guests bid on every horse as fast as they can while the host locks; once it has,
    every horse's owner is its high bidder at its high bid, and nothing is owned that has no bid."""
    from la_subasta import notifications as nots
    bad, placed = [], 0
    for trial in range(4):
        _reset()
        app = _make_app()
        client = app.test_client()
        _lq_names(client)
        nots.init_notifications(_Events())
        transition(AuctionState.OPEN)
        guests = [bidding.register_bidder(f"Guest {n}", "🌮") for n in range(24)]
        stop, counts = threading.Event(), []

        def bidder(guest, seed):
            n = 0
            horse = seed
            try:
                while not stop.is_set():
                    horse = horse % 20 + 1
                    high = bidding.current_high_bid(horse)
                    try:
                        bidding.place_bid(guest["id"], horse, (high["amount"] if high else 0) + 1)
                        n += 1
                    except bidding.BidError:
                        pass
            finally:
                counts.append(n)
                models.close_conn()

        threads = [threading.Thread(target=bidder, args=(g, i)) for i, g in enumerate(guests)]
        for t in threads:
            t.start()
        time.sleep(0.4)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        stop.set()
        for t in threads:
            t.join(15)
        placed += sum(counts)
        for horse in range(1, 21):
            high, own = bidding.current_high_bid(horse), _ownership(horse)
            if (high is None) != (own is None) or (high and (own["bidder_id"], own["winning_bid"]) != (high["bidder_id"], high["amount"])):
                bad.append((trial, horse, high and (high["bidder_id"], high["amount"]), own and (own["bidder_id"], own["winning_bid"])))
        _check(f"trial {trial + 1}: the lock landed", r.status_code == 200 and get_state() == AuctionState.LOCKED, f"{r.status_code}")
    _check(f"{placed} bids from 24 threads across four locks: every horse's owner is its high bidder at its high bid", bad == [] and placed > 200,
           f"{len(bad)} disagree, first {bad[:3]}, placed {placed}")


def test_settings_reset_follows_the_lock():
    """S6 (the gap review's S1 c, B8): a reset to defaults changed the payout split and the bid rules in an auction that was
    running, which a set refuses. It is refused the same way (409) while any setting it would change is locked; with
    the auction not started, or with only the always-editable settings overridden, it works."""
    for state in (AuctionState.OPEN, AuctionState.FINAL_HOUR, AuctionState.LOCKED):
        _reset()
        app = _make_app()
        client = app.test_client()
        settings.set_setting("PAYOUT_PRESET", "70/20/10")
        settings.set_setting("HOUSE_FUND_LABEL", "Custom")
        transition(state, force=True)
        audited = len(settings.get_audit_log(limit=100))
        r = client.post("/la-subasta/api/admin/settings/reset", json={})
        _check(f"reset in {state.value}: 409, 'Cannot change ...', nothing reset, nothing audited",
               r.status_code == 409 and r.get_json()["error"].startswith("Cannot change ")
               and settings.get_setting("PAYOUT_PRESET") == "70/20/10" and settings.get_setting("HOUSE_FUND_LABEL") == "Custom"
               and len(settings.get_audit_log(limit=100)) == audited, f"{r.status_code} {r.get_json()}")
    _reset()
    app = _make_app()
    client = app.test_client()
    settings.set_setting("HOUSE_FUND_LABEL", "Custom")
    transition(AuctionState.OPEN)
    r = client.post("/la-subasta/api/admin/settings/reset", json={})
    _check("running, with only an always-editable setting overridden: the reset goes through (a set of it would too)",
           r.status_code == 200 and r.get_json()["reset_count"] == 1 and settings.get_setting("HOUSE_FUND_LABEL") == "DDM 2027 Build Fund")
    _reset()
    app = _make_app()
    client = app.test_client()
    settings.set_setting("PAYOUT_PRESET", "70/20/10")
    r = client.post("/la-subasta/api/admin/settings/reset", json={})
    _check("not started: it goes through", r.status_code == 200 and settings.get_setting("PAYOUT_PRESET") == "60/25/15")


def test_bad_numbers_are_400s():
    """S8: an amount of NaN or Infinity, a number too big for the database, a guest or bid number that is no number: a 400 that
    says so, never a 500, on every route that takes one."""
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    transition(AuctionState.OPEN)
    alice = bidding.register_bidder("Alice", "🌮")
    alice_secret = alice["secret"]
    huge = 10 ** 30
    for label, amount in (("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity"), ('"nan"', '"nan"'), ('"inf"', '"inf"'),
                          ("1e999", "1e999"), ("10**400", "1" + "0" * 400), ("a list", "[1]"), ("an object", "{}"), ("null", "null")):
        r = _raw_bid(client, alice, alice_secret, 1, amount)
        _check(f"a bid of {label}: 400 'Amount must be a number'", r.status_code == 400 and r.get_json()["error"] == "Amount must be a number"
               and bidding.count_bids() == 0, f"{r.status_code} {r.get_json()}")
    bid_id = client.post("/la-subasta/api/bid", json={"bidder_id": alice["id"], "horse_id": 1, "amount": 1}).get_json()["bid"]["bid_id"]
    posts = [("/la-subasta/api/bid", {"bidder_id": huge, "horse_id": 1, "amount": 1}),
             ("/la-subasta/api/bid", {"bidder_id": alice["id"], "horse_id": huge, "amount": 1}),
             ("/la-subasta/api/bid", {"bidder_id": float("inf"), "horse_id": 1, "amount": 1}),
             ("/la-subasta/api/bid", {"bidder_id": "x", "horse_id": 1, "amount": 1}),
             ("/la-subasta/api/bid/undo", {"bid_id": huge, "bidder_id": alice["id"]}),
             ("/la-subasta/api/bid/undo", {"bid_id": bid_id, "bidder_id": huge}),
             ("/la-subasta/api/admin/void", {"bid_id": huge}),
             ("/la-subasta/api/admin/paid", {"bidder_id": huge}),
             ("/la-subasta/api/admin/cap-exempt", {"bidder_id": huge, "exempt": True}),
             ("/la-subasta/api/admin/payout-slot", {"finish": "win", "horse_id": huge}),
             ("/la-subasta/api/admin/payout-slot", {"finish": "win", "horse_id": float("nan")})]
    codes = {}
    for path, body in posts:
        if "secret" not in body and path.endswith(("/api/bid", "/api/bid/undo")):
            body = {**body, "secret": alice_secret}
        r = client.post(path, json=body)
        codes[(path[len("/la-subasta/api/"):], str(body)[:60])] = r.status_code
    _check("a number too big for the database, or no number, in a bid, an undo, a void, a paid mark, an exemption or a slot: 400 each",
           set(codes.values()) == {400}, str({k: v for k, v in codes.items() if v != 400}))
    r = client.post("/la-subasta/api/check-identity", json={"bidder_id": huge, "name": "Alice", "emoji": "🌮", "secret": alice_secret})
    _check("check-identity with a number too big: known false, not a 500", r.status_code == 200 and r.get_json()["known"] is False, f"{r.status_code}")
    _check("a portfolio for a number too big to be anyone's: 404", client.get(f"/la-subasta/api/bidders/{huge}/portfolio").status_code == 404)
    _check("none of it stored or changed anything", bidding.count_bids() == 1 and bidding.get_bidder(alice["id"])["paid"] == 0)


def test_a_guest_named_the_house_keeps_their_row():
    """S8: a guest named The House with the hat was deleted at the next start (the cleanup of the sentinel row the House had
    until 2026-10-01 took them for it). Nothing cleans up after the House any more."""
    _reset()
    app = _make_app()
    client = app.test_client()
    r = client.post("/la-subasta/api/register", json={"name": "The House", "emoji": "🎩"})
    secret = r.get_json()["secret"]
    init_db(_TMP_DB)                                            # pi5 restarts
    init_db(_TMP_DB)
    row = bidding.get_bidder_by_identity("The House 🎩")
    _check("The House 🎩 joins, pi5 restarts twice: the guest is still there, with their secret",
           r.status_code == 200 and row is not None and bidding.holds_secret(row["id"], secret), str(row))


def test_payouts():
    """The payouts come from SET WINNERS (gap review M5, OD4): the result the
    Control Center records is read through La Quiniela's board
    (BettingBoard.results(), the same file the cups and the TV are fed from),
    and the ledger is worked out from it and the ownership whenever it is
    read: whole pesos, La Quiniela's rounding (PLACE and SHOW half up, WIN
    takes the rest: OD5), the owner of each real finisher. A correction in
    SET WINNERS changes it; Reset betting (which clears the result) clears it;
    the next finishers in the record are La Quiniela's business."""
    _reset()
    app = _make_app()
    client = app.test_client()
    transition(AuctionState.OPEN)
    alice, bob, carol = (client.post("/la-subasta/api/register", json={"name": n, "emoji": e}).get_json()["bidder"]
                         for n, e in (("Alice", "🌮"), ("Bob", "🐴"), ("Carol", "💃")))

    def bid(bidder, horse, amount):
        r = client.post("/la-subasta/api/bid", json={"bidder_id": bidder["id"], "horse_id": horse, "amount": amount})
        assert r.status_code == 200, r.get_json()

    bid(alice, 1, 1); bid(bob, 1, 2); bid(alice, 1, 3); bid(bob, 1, 4); bid(alice, 1, 5)     # Alice owns 1 at 5
    bid(bob, 2, 1); bid(carol, 2, 2); bid(bob, 2, 3)                                          # Bob owns 2 at 3
    bid(carol, 3, 1); bid(alice, 3, 2); bid(carol, 3, 3)                                     # Carol owns 3 at 3
    pot = bidding.total_pot()
    _check("total pot = 5 + 3 + 3 = 11", pot == 11.0, f"pot={pot}")
    amounts = payouts.compute_payout_amounts(pot)
    _check("whole pesos, La Quiniela's rounding: PLACE 11 x 0.25 = 2.75 -> 3, SHOW 1.65 -> 2, WIN takes the rest, 6",
           amounts == {"win": 6, "place": 3, "show": 2} and all(type(v) is int for v in amounts.values()), str(amounts))
    _check("...prizes_for is the code: pot 154 -> 92 / 39 / 23, pot 1 -> 1 / 0 / 0, pot 0 -> 0 / 0 / 0",
           payouts.compute_payout_amounts(154) == {"win": 92, "place": 39, "show": 23}
           and payouts.compute_payout_amounts(1) == {"win": 1, "place": 0, "show": 0}
           and payouts.compute_payout_amounts(0) == {"win": 0, "place": 0, "show": 0})

    led = _ledger(client)
    _check("before the lock and the race: no result, no rows, nothing paid on an earlier result",
           led["results"] is None and led["payouts"] == [] and led["unowned"] == [] and led["paid_on_old_result"] == [], str(led))
    r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
    _check("lock endpoint succeeds", r.status_code == 200 and r.get_json().get("success"), f"body={r.get_json()}")
    led = _ledger(client)
    state = client.get("/la-subasta/api/state").get_json()
    _check("locked, no result yet: the pot 11 and the amounts, no rows; /api/state says LOCKED, results null, not settled",
           led["total_pot"] == 11 and led["amounts"] == {"win": 6, "place": 3, "show": 2} and led["payouts"] == []
           and state["state"] == "LOCKED" and state["results"] is None and state["settled"] is False, str((led, state)))

    _set_winners(1, 2, 3)                      # SET WINNERS, confirmed on the Control Center
    led = _ledger(client)
    by = _by_finish(led)
    _check("the result read through the board: WIN 1, PLACE 2, SHOW 3, three rows in that order",
           led["results"] == {"win": 1, "place": 2, "show": 3} and [p["finish"] for p in led["payouts"]] == ["win", "place", "show"],
           str(led))
    _set_winners(1, 2, 3, ties=[[1, 2]])            # a dead heat for 1st in the record (M6): the ledger reads slot order
    tied = _ledger(client)
    _check("a dead heat in the record: the ledger pays slot order as today, WIN 1, PLACE 2 (pool and split is a follow-up item)",
           tied["results"] == {"win": 1, "place": 2, "show": 3} and _by_finish(tied) == by, str(tied))
    _set_winners(1, 2, 3)
    _check("WIN pays Alice (horse 1) 6 pesos", by["win"]["bidder_id"] == alice["id"] and by["win"]["horse_id"] == 1
           and by["win"]["amount"] == 6 and by["win"]["bidder_identity"] == "Alice 🌮" and by["win"]["unowned"] is False
           and by["win"]["pays_horse_id"] is None and by["win"]["paid"] is None, str(by["win"]))
    _check("PLACE pays Bob (horse 2) 3, SHOW pays Carol (horse 3) 2",
           (by["place"]["bidder_id"], by["place"]["amount"], by["show"]["bidder_id"], by["show"]["amount"])
           == (bob["id"], 3, carol["id"], 2), str(by))
    _check("the three sum to the pot", by["win"]["amount"] + by["place"]["amount"] + by["show"]["amount"] == 11)
    state = client.get("/la-subasta/api/state").get_json()
    _check("/api/state: still LOCKED (no RACE_COMPLETE or SETTLED any more), the result carried, settled true",
           state["state"] == "LOCKED" and state["results"] == {"win": 1, "place": 2, "show": 3} and state["settled"] is True, str(state))
    _check("payouts.ledger() is the same thing, in the process", payouts.ledger()["payouts"] == led["payouts"])

    _set_winners(3, 1, 2)                      # a correction in SET WINNERS
    by = _by_finish(_ledger(client))
    _check("a correction in SET WINNERS: the payouts follow at the next read, nothing stored to catch up",
           (by["win"]["bidder_id"], by["place"]["bidder_id"], by["show"]["bidder_id"]) == (carol["id"], alice["id"], bob["id"])
           and (by["win"]["horse_id"], by["place"]["horse_id"], by["show"]["horse_id"]) == (3, 1, 2), str(by))
    _set_winners(3, 1, 2, after=[7, 9])
    led = _ledger(client)
    _check("the next finishers in the record (La Quiniela's skip, M6) change nothing here: the owners of the real 1st, 2nd and 3rd are paid",
           [p["horse_id"] for p in led["payouts"]] == [3, 1, 2] and _by_finish(led)["win"]["bidder_id"] == carol["id"]
           and led["results"] == {"win": 3, "place": 1, "show": 2}, str(led))

    r = client.post("/api/quiniela/reset")      # Reset betting on the LQ admin page
    led = _ledger(client)
    state = client.get("/la-subasta/api/state").get_json()
    _check("Reset betting clears the result, so the payouts go: no rows, results null, not settled; the ownership stays (LOCKED, pot 11)",
           r.status_code == 200 and led["results"] is None and led["payouts"] == [] and led["total_pot"] == 11
           and state["results"] is None and state["settled"] is False and state["state"] == "LOCKED", str((r.status_code, led, state)))


# -----------------------------------------------------------------------------
# No House (decided by Joey, 2026-10-01): unsold horses, unowned slots, the
# lock's warning, the cap exemption
# -----------------------------------------------------------------------------

def _package_sources():
    """(relative path, text) of La Subasta's own Python, JS, HTML and CSS,
    this test file apart."""
    root = os.path.dirname(os.path.abspath(__file__))
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if name == "test_smoke.py" or not name.endswith((".py", ".js", ".html", ".css")):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                out.append((os.path.relpath(path, root), fh.read()))
    return out


def test_no_house():
    """There is no House: no sentinel bidder row, no House payout, no
    is_house flag, nothing to include."""
    from la_subasta import config as _cfg
    from la_subasta.models import get_conn
    _reset()
    app = _make_app()
    client = app.test_client()
    _check("a fresh database has no bidder at all, so no House row",
           get_conn().execute("SELECT COUNT(*) AS c FROM bidders").fetchone()["c"] == 0)
    _check("config has no House bidder",
           not any(n.startswith("HOUSE_BIDDER") for n in dir(_cfg)))
    _check("models has no House helpers",
           not any(hasattr(models, n) for n in ("house_bidder_id", "_ensure_house_bidder")))
    hits = [path for path, text in _package_sources()
            if any(word in text for word in ("is_house", "house_bidder_id", "include_house"))]
    _check("no is_house, house_bidder_id or include_house anywhere in La Subasta's source",
           hits == [], f"found in {hits}")
    client.post("/la-subasta/api/register", json={"name": "Alice", "emoji": "🌮"})
    r = client.get("/la-subasta/api/bidders?include_house=true")
    _check("?include_house is not a thing: the same list, Alice alone",
           [b["identity"] for b in r.get_json()["bidders"]] == ["Alice 🌮"], f"got {r.get_json()}")


def test_unowned_win_is_a_flagged_slot():
    """A real finisher nobody owns is paid to nobody: its slot is flagged
    unowned in the ledger, nothing waits for it, and the admin names the
    horse that pays it (the next finisher: La Quiniela's rule for an empty
    cup in the money). The admin's pick is tied to the finisher it was made
    for: a correction in SET WINNERS that changes the slot's horse sets it
    aside, one that changes it back brings it back."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 1, 5)
        _bid(client, bob, 2, 3)
        _bid(client, carol, 4, 2)
        pot = bidding.total_pot()
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _set_winners(3, 1, 2)                          # 3 crossed first, and nobody bought it
        led = _ledger(client)
        by = _by_finish(led)
        _check("a result naming a horse that was never sold: the ledger settles, the win slot flagged unowned, and only it",
               led["unowned"] == ["win"] and by["win"]["unowned"] is True and by["place"]["unowned"] is False
               and by["show"]["unowned"] is False, str(led))
        _check("...with no bidder: nobody is paid it, and there is no House",
               by["win"]["bidder_id"] is None and by["win"]["bidder_identity"] is None
               and all("is_house" not in p for p in led["payouts"]))
        _check("...and it carries its amount, 60% of the pot (whole pesos: 10 -> 5 / 3 / 2)",
               by["win"]["amount"] == 5 and by["win"]["horse_id"] == 3 and led["total_pot"] == pot == 10, f"got {by['win']}")
        _check("place is Alice's (horse 1) and show Bob's (horse 2)",
               by["place"]["bidder_id"] == alice["id"] and by["show"]["bidder_id"] == bob["id"])

        # The admin names the horse that pays it: the next finisher, Carol's 4
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "win", "horse_id": 4})
        body = r.get_json()
        slot = body.get("slot") or {}
        _check("naming #4 for the win slot is accepted", r.status_code == 200 and body.get("success"), f"body={body}")
        _check("the slot is Carol's at the same amount, with #3 still the finisher and #4 paying",
               slot.get("bidder_id") == carol["id"] and slot.get("horse_id") == 3 and slot.get("pays_horse_id") == 4
               and slot.get("unowned") is False and slot.get("amount") == 5, f"got {slot}")
        _check("nothing is unowned now, and the reply carries the ledger as it stands",
               body["unowned"] == [] and body["payouts"][0]["pays_horse_id"] == 4, str(body))
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "place", "horse_id": 4})
        by = _by_finish(_ledger(client))
        _check("a slot whose finisher has an owner cannot be pointed at another horse (S1): 409, and PLACE still pays Alice",
               r.status_code == 409 and r.get_json()["error"] == "PLACE is #1's: it has an owner, so it pays them. Only a slot nobody owns can be pointed at another horse"
               and by["place"]["pays_horse_id"] is None and by["place"]["bidder_id"] == alice["id"], f"{r.status_code} {r.get_json()} {by['place']}")
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "place", "horse_id": 1})
        slot = r.get_json()["slot"]
        _check("naming the finisher's own horse is the slot as the result has it, with or without an owner's pick before",
               r.status_code == 200 and slot["pays_horse_id"] is None and slot["bidder_id"] == alice["id"], f"got {slot}")

        # The pick is tied to the finisher it was made for
        _set_winners(1, 3, 2)                          # a correction: 1 won after all, 3 was second
        by = _by_finish(_ledger(client))
        _check("a correction that moves the finisher: WIN pays its owner (Alice) and the pick for #3 is set aside; PLACE, now #3, "
               "is unowned", by["win"]["bidder_id"] == alice["id"] and by["win"]["pays_horse_id"] is None
               and by["place"]["horse_id"] == 3 and by["place"]["unowned"] is True and by["place"]["pays_horse_id"] is None, str(by))
        _set_winners(3, 1, 2)
        by = _by_finish(_ledger(client))
        _check("...and back: #3 wins again and the pick comes back with it (#4 pays WIN, Carol)",
               by["win"]["pays_horse_id"] == 4 and by["win"]["bidder_id"] == carol["id"], str(by["win"]))

        # What it refuses
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "win", "horse_id": 6})
        _check("a horse that was never sold cannot pay either: 409",
               r.status_code == 409 and "no owner either" in r.get_json()["error"], f"body={r.get_json()}")
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "win", "horse_id": 99})
        _check("a number that is no horse: 400", r.status_code == 400
               and r.get_json()["error"] == "Invalid horse id: 99")
        r = _lq_scratch(client, 20)
        _check("a scratch while the result stands is refused on the LQ admin page: the field is frozen under the payouts",
               r.status_code == 409 and "the field is frozen" in r.get_json()["error"], f"body={r.get_json()}")
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "fourth", "horse_id": 4})
        _check("a finish that is not win, place or show: 400", r.status_code == 400)
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "win", "horse_id": "x"})
        _check("a horse_id that is not a number: 400", r.status_code == 400)
        _check("the refused requests changed nothing: win is still #4's, Carol's",
               _by_finish(_ledger(client))["win"]["bidder_id"] == carol["id"])
    finally:
        _done_with_events()


def test_payout_slot_needs_results():
    """The slot picker and the paid-out mark work on a ledger: before a result
    there is none, and the refusal says where results come from."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 1, 2)
        r = client.post("/la-subasta/api/admin/payout-slot", json={"finish": "win", "horse_id": 1})
        _check("a slot before any result: 409",
               r.status_code == 409 and r.get_json()["error"] == "No results yet: they are entered in SET WINNERS on the Control Center",
               f"body={r.get_json()}")
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "paid": True})
        _check("a paid-out mark before any result: 409 too",
               r.status_code == 409 and r.get_json()["error"] == "No results yet: they are entered in SET WINNERS on the Control Center",
               f"body={r.get_json()}")
    finally:
        _done_with_events()


def test_void_with_no_second_bidder_leaves_the_horse_unsold():
    """Admin void re-awards the horse to the runner-up at their bid. With no
    second bidder it is unsold: back on the list before the lock, unowned
    after it. Never the House."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        def void(bid_id, reason="test"):
            r = client.post("/la-subasta/api/admin/void", json={"bid_id": bid_id, "reason": reason})
            assert r.status_code == 200, r.get_json()
            return r.get_json()

        only = _bid(client, alice, 4, 2)                 # alone on 4
        _bid(client, alice, 6, 1)
        _bid(client, bob, 6, 3)                          # Bob leads 6, Alice is the runner-up
        body = void(only)
        _check("before the lock: voiding the only bid on 4 leaves it unsold",
               body["unsold"] is True and body["new_high_bid"] is None, f"got {body}")
        horses = {h["horse_id"]: h for h in client.get("/la-subasta/api/horses").get_json()["horses"]}
        _check("...it is back on the list with no bid, and on the lock's unsold list",
               4 in horses and horses[4]["current_high_bid"] is None and 4 in bidding.unsold_horses())
        _bid(client, carol, 4, 1)
        _check("...and anyone can bid on it again", bidding.current_high_bid(4)["bidder_id"] == carol["id"])

        body = void(bidding.current_high_bid(6)["id"])
        _check("with a second bidder it goes to the runner-up at their bid (Alice at 1)",
               body["unsold"] is False and body["new_high_bid"]["bidder_id"] == alice["id"]
               and body["new_high_bid"]["amount"] == 1, f"got {body}")

        _bid(client, bob, 8, 4)                          # alone on 8
        _bid(client, alice, 10, 1)
        _bid(client, carol, 10, 3)                       # Carol leads 10, Alice is the runner-up
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _check("locked: Carol owns 4 (1) and 10 (3), Alice 6 (1), Bob 8 (4)",
               [(_ownership(h) or {}).get("bidder_id") for h in (4, 10, 6, 8)]
               == [carol["id"], carol["id"], alice["id"], bob["id"]])

        body = void(bidding.current_high_bid(8)["id"])
        _check("after the lock: voiding the only bid on 8 leaves it with no owner",
               body["unsold"] is True and _ownership(8) is None and payouts.get_owner(8) is None, f"got {body}")
        _check("...and Bob owes nothing for it", bidding.bidder_portfolio(bob["id"])["total"] == 0)
        body = void(bidding.current_high_bid(10)["id"])
        own = _ownership(10)
        _check("after the lock: voiding the leader on 10 re-awards the row to Alice at her bid",
               body["unsold"] is False and own["bidder_id"] == alice["id"] and own["winning_bid"] == 1,
               f"got {own}")

        _set_winners(8, 4, 10)
        led = _ledger(client)
        by = _by_finish(led)
        _check("a result naming the unowned 8 settles, its slot unowned (the pot is 1 + 1 + 1)",
               led["unowned"] == ["win"] and led["total_pot"] == 3
               and by["place"]["bidder_id"] == carol["id"] and by["show"]["bidder_id"] == alice["id"],
               f"body={led}")
    finally:
        _done_with_events()


def test_lock_warns_about_unsold_horses():
    """There is no House to take a horse nobody bought, so the lock warns:
    409 with the numbers, nothing done, unless the body confirms."""
    from la_subasta.models import get_conn
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 1, 2)
        _bid(client, bob, 2, 3)
        _bid(client, carol, 3, 1)
        ev.events.clear()
        r = client.post("/la-subasta/api/admin/lock")
        body = r.get_json()
        _check("lock with unsold horses and no confirm: 409",
               r.status_code == 409 and body["success"] is False, f"status={r.status_code} body={body}")
        _check("...listing every horse in the field nobody bid on",
               body["unsold"] == list(range(4, 21)), f"got {body.get('unsold')}")
        _check("...and it says so in words", body["error"].startswith("No bid on #4, #5"), f"got {body['error']}")
        count = lambda: get_conn().execute("SELECT COUNT(*) AS c FROM ownership").fetchone()["c"]
        _check("...and nothing happened: still OPEN, nothing frozen, nothing pushed",
               get_state() == AuctionState.OPEN and count() == 0 and ev.events == [], f"got {ev.events}")
        for body_in in ({"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {}):
            r = client.post("/la-subasta/api/admin/lock", json=body_in)
            _check(f"only a JSON true confirms: {body_in} is still 409", r.status_code == 409)

        _lq_scratch(client, 5)                          # no replacement
        _lq_scratch(client, 9, 22, "Ocelli")            # 22 stands in for 9
        unsold = client.post("/la-subasta/api/admin/lock").get_json()["unsold"]
        _check("a scratched horse is not unsold; a replacement with no bid is (22 for 9)",
               5 not in unsold and 9 not in unsold and 22 in unsold and len(unsold) == 16, f"got {unsold}")

        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        _check("with confirm it locks", r.status_code == 200 and r.get_json()["state"] == "LOCKED",
               f"body={r.get_json()}")
        _check("...and froze the three sold horses", count() == 3)

        _reset()
        app = _make_app()
        r = app.test_client().post("/la-subasta/api/admin/lock")
        _check("an auction that cannot lock yet is refused as before, with no warning",
               r.status_code == 409 and "unsold" not in r.get_json()
               and "Illegal transition" in r.get_json()["error"], f"body={r.get_json()}")
    finally:
        _done_with_events()


def test_lock_needs_no_confirm_when_every_horse_is_sold():
    """The host, exempt from the cap, buys every horse; the lock then goes
    through without a word."""
    from la_subasta.models import get_conn
    app, client, ev, (host, bob, carol) = _scratch_rig()
    try:
        r = client.post("/la-subasta/api/admin/cap-exempt", json={"bidder_id": host["id"], "exempt": True})
        assert r.status_code == 200, r.get_json()
        for n in range(1, 21):
            _bid(client, host, n, 1)
        _check("every horse in the field is sold", bidding.unsold_horses() == [])
        r = client.post("/la-subasta/api/admin/lock")
        _check("the lock with nothing unsold needs no confirm",
               r.status_code == 200 and r.get_json()["state"] == "LOCKED", f"body={r.get_json()}")
        _check("...and froze all twenty",
               get_conn().execute("SELECT COUNT(*) AS c FROM ownership").fetchone()["c"] == 20)
    finally:
        _done_with_events()


def test_cap_exempt_bidder_holds_more_than_the_cap():
    """The admin can mark one bidder (the host) exempt from the max-horses
    cap; it still applies to everyone else."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        def bid(bidder, horse):
            return client.post("/la-subasta/api/bid",
                               json={"bidder_id": bidder["id"], "horse_id": horse, "amount": 1})

        for n in (1, 2, 3):
            assert bid(alice, n).status_code == 200
        r = bid(alice, 4)
        _check("Alice, at the cap of 3, is refused a fourth",
               r.status_code == 400 and r.get_json()["error"] == "Max 3 horses per bidder", f"body={r.get_json()}")

        r = client.post("/la-subasta/api/admin/cap-exempt", json={"bidder_id": carol["id"], "exempt": True})
        _check("marking Carol exempt: 200, with the flag", r.status_code == 200
               and r.get_json() == {"success": True, "bidder_id": carol["id"], "cap_exempt": True},
               f"body={r.get_json()}")
        for n in range(5, 10):
            assert bid(carol, n).status_code == 200, n
        _check("the exempt Carol leads five horses", len(bidding.horses_leading_by(carol["id"])) == 5)
        _check("...while Alice is still capped at three", bid(alice, 4).status_code == 400)
        for n in (10, 11, 12):
            assert bid(bob, n).status_code == 200
        _check("...and so is Bob", bid(bob, 13).status_code == 400)
        flags = {b["identity"]: b["cap_exempt"] for b in client.get("/la-subasta/api/bidders").get_json()["bidders"]}
        _check("GET /api/bidders carries the mark",
               flags == {"Alice 🌮": 0, "Bob 🐴": 0, "Carol 💃": 1}, f"got {flags}")

        r = client.post("/la-subasta/api/admin/cap-exempt", json={"bidder_id": carol["id"], "exempt": False})
        _check("taking the mark off: 200, flag false", r.status_code == 200 and r.get_json()["cap_exempt"] is False)
        r = bid(carol, 13)
        _check("Carol keeps her five but takes no sixth: the cap is back",
               r.status_code == 400 and r.get_json()["error"] == "Max 3 horses per bidder"
               and len(bidding.horses_leading_by(carol["id"])) == 5, f"body={r.get_json()}")

        r = client.post("/la-subasta/api/admin/cap-exempt", json={"bidder_id": 9999, "exempt": True})
        _check("an unknown bidder: 404", r.status_code == 404 and r.get_json()["error"] == "Unknown bidder")
        r = client.post("/la-subasta/api/admin/cap-exempt", json={"bidder_id": carol["id"], "exempt": "yes"})
        _check("exempt must be true or false: 400", r.status_code == 400)
        r = client.post("/la-subasta/api/admin/cap-exempt", json={"exempt": True})
        _check("bidder_id must be there: 400", r.status_code == 400)
    finally:
        _done_with_events()


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


def test_paid_out_marks():
    """The paid-out mark: who was paid what for a slot, tied to the finisher
    it was paid for. It reads as paid while the ledger still says that
    bidder and that amount for that horse; when the result moves (a
    correction, Reset betting) or the owner or the amount changes under it,
    the ledger lists it as paid on an earlier result, with what the slot
    pays now, until the host unmarks it or the result comes back. Reset
    betting leaves the marks: they are money facts (Joey, 2026-10-20)."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 1, 5)
        _bid(client, bob, 2, 3)
        _bid(client, carol, 3, 2)
        _bid(client, carol, 4, 2)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _set_winners(1, 2, 3)                              # pot 12: 7 / 3 / 2
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "paid": True})
        body = r.get_json()
        by = _by_finish(body)
        _check("WIN marked paid out: the row says so, Alice, 7 pesos, when; the reply is the ledger; nothing stale",
               r.status_code == 200 and body["success"] is True and by["win"]["paid"]["bidder_id"] == alice["id"]
               and by["win"]["paid"]["amount"] == 7 and by["win"]["paid"]["bidder_identity"] == "Alice 🌮"
               and by["win"]["paid"]["paid_at"] and body["paid_on_old_result"] == [], str(body))
        first_at = by["win"]["paid"]["paid_at"]
        _check("the other slots are not paid", by["place"]["paid"] is None and by["show"]["paid"] is None)
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "paid": True})
        _check("marked again: the same mark, the first paid_at stands",
               r.status_code == 200 and _by_finish(r.get_json())["win"]["paid"]["paid_at"] == first_at)
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "place", "paid": True})
        _check("PLACE marked too: Bob, 3", _by_finish(r.get_json())["place"]["paid"]["bidder_id"] == bob["id"]
               and _by_finish(r.get_json())["place"]["paid"]["amount"] == 3)

        _set_winners(4, 2, 3)                              # a correction in SET WINNERS: 4 won, not 1
        led = _ledger(client)
        by = _by_finish(led)
        _check("the result corrected: WIN now pays Carol (#4) and reads unpaid; PLACE's mark still matches",
               by["win"]["bidder_id"] == carol["id"] and by["win"]["paid"] is None
               and by["place"]["paid"]["bidder_id"] == bob["id"], str(by))
        stale = led["paid_on_old_result"]
        _check("...and the ledger says Alice was paid on the earlier result and what WIN pays now, in one line",
               len(stale) == 1 and stale[0]["finish"] == "win" and stale[0]["horse_id"] == 1
               and stale[0]["bidder_id"] == alice["id"] and stale[0]["amount"] == 7 and stale[0]["paid_at"] == first_at
               and stale[0]["line"] == "Alice 🌮 was paid 7 pesos for WIN (#1); WIN now pays Carol 💃 7 pesos for #4", str(stale))
        _set_winners(1, 2, 3)                              # the result changes back
        led = _ledger(client)
        _check("the result back as it was: the mark reads as paid again and nothing is stale",
               _by_finish(led)["win"]["paid"]["bidder_id"] == alice["id"] and led["paid_on_old_result"] == [], str(led))

        # The owner goes under a mark (a void after the lock): the pot shrinks too, so PLACE's amount moves
        r = client.post("/la-subasta/api/admin/void", json={"bid_id": bidding.current_high_bid(1)["id"], "reason": "test"})
        assert r.status_code == 200, r.get_json()
        led = _ledger(client)
        by = _by_finish(led)
        _check("Alice's bid on 1 voided after the lock: WIN is unowned, the pot is 7 (4 / 2 / 1)",
               by["win"]["unowned"] is True and led["total_pot"] == 7 and led["amounts"] == {"win": 4, "place": 2, "show": 1}, str(led))
        lines = [s["line"] for s in led["paid_on_old_result"]]
        _check("...both marks are stale: Alice's (no owner to pay now), Bob's (PLACE pays 2 now, not 3)",
               lines == ["Alice 🌮 was paid 7 pesos for WIN (#1); WIN is now #1, which has no owner to pay",
                         "Bob 🐴 was paid 3 pesos for PLACE (#2); PLACE now pays Bob 🐴 2 pesos for #2"], str(lines))
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "horse_id": 1, "paid": False})
        _check("unmarking Alice's by its finish and horse: gone, Bob's stays",
               r.status_code == 200 and [s["finish"] for s in r.get_json()["paid_on_old_result"]] == ["place"], str(r.get_json()))
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "place", "paid": False})
        _check("unmarking PLACE without a horse: the current finisher's mark (#2) goes; nothing stale",
               r.status_code == 200 and r.get_json()["paid_on_old_result"] == [] and _by_finish(r.get_json())["place"]["paid"] is None)

        # What it refuses
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "paid": True})
        _check("marking an unowned slot paid: 409, name the horse that pays it first",
               r.status_code == 409 and r.get_json()["error"] == "WIN has no owner to pay yet: name the horse that pays it first",
               f"body={r.get_json()}")
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "fourth", "paid": True})
        _check("a finish that is not win, place or show: 400", r.status_code == 400)
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "show", "paid": "yes"})
        _check("paid must be true or false: 400", r.status_code == 400)

        # Reset betting leaves the marks: paid on a result that is now gone
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "show", "paid": True})
        assert r.status_code == 200 and _by_finish(r.get_json())["show"]["paid"]["amount"] == 1, r.get_json()
        r = client.post("/api/quiniela/reset")
        led = _ledger(client)
        _check("Reset betting: the result and the rows are gone; Carol's mark stays, paid on a result that is now gone",
               r.status_code == 200 and led["results"] is None and led["payouts"] == []
               and [s["line"] for s in led["paid_on_old_result"]]
               == ["Carol 💃 was paid 1 peso for SHOW (#3); there is no result now"], str(led))
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "show", "horse_id": 3, "paid": False})
        _check("...until the host unmarks it", r.status_code == 200 and r.get_json()["paid_on_old_result"] == [])
        r = client.post("/la-subasta/api/admin/payout-paid", json={"finish": "show", "paid": False})
        _check("unmarking with no result and no horse named: 409 (which mark?)", r.status_code == 409, f"body={r.get_json()}")
        _set_winners(1, 2, 3)
        led = _ledger(client)
        _check("the result entered again: WIN unowned (the void stands), PLACE and SHOW paid to nobody yet, nothing stale",
               [p["unowned"] for p in led["payouts"]] == [True, False, False] and all(p["paid"] is None for p in led["payouts"])
               and led["paid_on_old_result"] == [], str(led))
    finally:
        _done_with_events()


def test_results_entry_is_gone():
    """La Subasta's own results entry and store are gone (M5): no results
    route, no results form on the admin page, no payouts table, no
    results_entered or payout_computed event, no RACE_COMPLETE or SETTLED
    anywhere in its sources. The result is SET WINNERS'."""
    _reset()
    app = _make_app()
    client = app.test_client()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    _check("no /api/admin/results route", "/la-subasta/api/admin/results" not in rules
           and client.post("/la-subasta/api/admin/results", json={"win": 1, "place": 2, "show": 3}).status_code == 404)
    tables = {r["name"] for r in models.get_conn().execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    _check("no payouts table; payout_slots instead", "payouts" not in tables and "payout_slots" in tables, str(sorted(tables)))
    html = client.get("/la-subasta/admin").get_data(as_text=True)
    js = _static(client, "/la-subasta/static/js/admin.js")
    _check("the admin page has no results form and its script no results route",
           "ls-admin-results" not in html and "ls-admin-win" not in html and "'results'" not in js)
    from la_subasta import notifications
    _check("no results_entered or payout_computed event (nothing listened)",
           not hasattr(notifications, "results_entered") and not hasattr(notifications, "payout_computed"))
    _check("payouts.py keeps no store: no compute_and_persist_payouts, no list_payouts",
           not hasattr(payouts, "compute_and_persist_payouts") and not hasattr(payouts, "list_payouts"))
    sources = "\n".join(text for path, text in _package_sources() if path != "models.py")     # the migration names them to retire them
    _check("nothing in La Subasta's own sources (the migration apart) speaks of RACE_COMPLETE, SETTLED, results_entered or payout_computed",
           all(word not in sources for word in ("RACE_COMPLETE", "SETTLED", "results_entered", "payout_computed")))
    r = client.get("/la-subasta/api/state").get_json()
    _check("/api/state carries the result (null now) and settled (false)", r["results"] is None and r["settled"] is False, str(r))


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
        app_client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        _set_winners(1, 2, 3)
        by_finish = _by_finish(_ledger(app_client))
        want = {"60/25/15": (5, 3, 2), "70/20/10": (7, 2, 1), "50/30/20": (5, 3, 2)}[preset]
        _check(f"[{preset}] whole pesos of the pot of {pot:.0f} ({w_pct:.0%} / {p_pct:.0%} / {s_pct:.0%}, PLACE and SHOW half up, "
               f"WIN the rest): {want[0]} / {want[1]} / {want[2]}, summing to it",
               (by_finish["win"]["amount"], by_finish["place"]["amount"], by_finish["show"]["amount"]) == want and sum(want) == pot,
               f"got {[by_finish[f]['amount'] for f in ('win', 'place', 'show')]}")


def test_settings_api_endpoints():
    _reset()
    app = _make_app()
    client = app.test_client()

    # GET /api/settings: the guest page's, open to everyone
    r = _stranger(app).get("/la-subasta/api/settings")
    data = r.get_json()
    _check("GET /api/settings returns 200 to a phone with no PIN", r.status_code == 200)
    _check("GET /api/settings has 7 keys",
           len(data["settings"]) == 7)
    _check("GET /api/settings includes state field",
           data.get("state") == "NOT_STARTED")
    _check("the old admin route for it is gone (setting them is POST /api/admin/settings)",
           client.get("/la-subasta/api/admin/settings").status_code == 405)

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
    r = client.get("/la-subasta/api/settings")
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
        ("Welcome back. Join again for this year's auction.", "the join screen's welcome-back line"),
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
    r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
    assert r.status_code == 200, r.get_json()
    owned = sorted(o["horse_id"] for o in (payouts.get_owner(n) for n in (1, 22, 23)) if o)
    _check("22 and 23 frozen into ownership", owned == [1, 22, 23], f"got {owned}")
    _set_winners(22, 23, 1)
    by_finish = _by_finish(_ledger(client))
    _check("the result 22 / 23 / 1: 22's owner (Alice) is paid the win", by_finish["win"]["bidder_id"] == alice["id"]
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
    # The results are SET WINNERS' (it refuses a horse not in the field: la_quiniela/test_dashboard.py);
    # La Subasta has no results entry of its own
    client.post("/la-subasta/api/admin/lock", json={"confirm": True})
    r = client.post("/la-subasta/api/admin/results", json={"win": 9, "place": 22, "show": 1})
    _check("La Subasta has no results route", r.status_code == 404, f"{r.status_code}")

    # A replacement can be scratched in turn (9 -> 22, then 22 -> 23): the horse
    # that runs in 9's place, and in 22's, is 23, not 22.
    r = _lq_scratch(client, 22, 23, "Robusta")
    _check("LQ admin: 22 -> 23 recorded", r.status_code == 200, f"body={r.get_json()}")
    status, error = bid(9)
    _check("a bid on 9 says 23 runs in its place, 22 being out too",
           status == 400 and error == "#9 is not in the field: scratched, #23 runs in its place",
           f"got {status} {error!r}")
    status, error = bid(22)
    _check("...a bid on 22 says the same",
           status == 400 and error == "#22 is not in the field: scratched, #23 runs in its place",
           f"got {status} {error!r}")
    # ...and the chain can end in nobody
    r = _lq_scratch(client, 23)
    _check("LQ admin: 23 scratched with no replacement", r.status_code == 200, f"body={r.get_json()}")
    status, error = bid(9)
    _check("a bid on 9 no longer says anyone runs in its place",
           status == 400 and error == "#9 is not in the field: scratched", f"got {status} {error!r}")


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


class _Logs(logging.Handler):
    """Collects what the named loggers say while it is entered, so that a
    test can check an expected ERROR line and the console stays quiet."""
    def __init__(self, *names):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self._names = names

    def __enter__(self):
        for name in self._names:
            logging.getLogger(name).addHandler(self)
        return self

    def __exit__(self, *exc):
        for name in self._names:
            logging.getLogger(name).removeHandler(self)
        return False

    def emit(self, record):
        self.records.append(record)

    def errors(self, name=None):
        return [r for r in self.records
                if r.levelno >= logging.ERROR and (name is None or r.name == name)]


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
    out, and a paid owner shows the refund owed."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 2, 5)          # Albus
        _bid(client, alice, 3, 2)          # Intrepido
        _bid(client, bob, 5, 4)
        _bid(client, carol, 8, 3)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
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

        _set_winners(3, 5, 8)                    # SET WINNERS cannot name 2: it is not in the field
        led = _ledger(client)
        _check("the result 3 / 5 / 8: the pot leaves out the scratched 5: 2 + 4 + 3 = 9", led["total_pot"] == 9,
               f"got {led.get('total_pot')}")
        _check("every paying slot has an owner: nothing is unowned",
               led["unowned"] == [] and not any(p["unowned"] for p in led["payouts"]))
        _check("no payout row names 2", all(p["horse_id"] != 2 for p in led["payouts"]))
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


def test_scratch_undo_restores_everything():
    """Undo on the LQ admin page restores everything: the horse is back in
    the field and what its scratch voided comes back with it, its bids and,
    after the lock, its ownership row, so the owner owes again. Only what the
    scratch voided: a bid an admin voided stays voided."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        # --- before the lock ------------------------------------------------
        a = _bid(client, alice, 11, 4)
        b = _bid(client, bob, 11, 7)
        c = _bid(client, carol, 11, 9)
        r = client.post("/la-subasta/api/admin/void", json={"bid_id": c, "reason": "Carol will not pay"})
        assert r.status_code == 200, r.get_json()
        d = _bid(client, alice, 11, 10)                 # placed, then taken back inside the 10 s window
        r = client.post("/la-subasta/api/bid/undo", json={"bid_id": d, "bidder_id": alice["id"]})
        assert r.status_code == 200, r.get_json()
        _check("before: Bob leads 11 at 7", bidding.current_high_bid(11)["bidder_id"] == bob["id"])
        r = _lq_scratch(client, 11)
        assert r.status_code == 200, r.get_json()
        _check("scratched: Alice's and Bob's bids are voided 'scratched'",
               [(x["id"], x["voided_reason"]) for x in _bids_on(11)]
               == [(a, "scratched"), (b, "scratched"), (c, "Carol will not pay"), (d, "undo")],
               f"got {_bids_on(11)}")
        _check("...and Bob owes nothing for 11", bidding.bidder_portfolio(bob["id"])["total"] == 0)

        ev.events.clear()
        r = client.post("/api/quiniela/unscratch", json={"horse": 11})
        _check("LQ admin: undo 11", r.status_code == 200, f"body={r.get_json()}")
        _check("the bids the scratch voided are back",
               [(x["id"], x["voided"], x["voided_reason"]) for x in _bids_on(11)[:2]]
               == [(a, 0, None), (b, 0, None)], f"got {_bids_on(11)}")
        _check("...a bid an admin voided stays voided, so does one the bidder took back",
               [(x["voided"], x["voided_reason"]) for x in _bids_on(11)[2:]]
               == [(1, "Carol will not pay"), (1, "undo")], f"got {_bids_on(11)}")
        horses = {h["horse_id"]: h for h in client.get("/la-subasta/api/horses").get_json()["horses"]}
        _check("11 is back on the list with Bob leading at 7",
               11 in horses and horses[11]["current_high_bid"]["bidder_id"] == bob["id"]
               and horses[11]["current_high_bid"]["amount"] == 7, f"got {horses.get(11)}")
        _check("Bob is charged 7 for it again", bidding.bidder_portfolio(bob["id"])["total"] == 7)
        _check("the undo pushed field_changed (phones read the list again)",
               len(ev.named("field_changed")) == 1 and 11 in ev.named("field_changed")[0]["horses"],
               f"got {ev.events}")
        before = [tuple(x.values()) for x in _bids_on(11)]
        client.get("/la-subasta/api/horses"); client.get("/la-subasta/api/state")
        ls_scratches.sync(force=True)
        _check("a second look changes nothing (idempotent)",
               [tuple(x.values()) for x in _bids_on(11)] == before)
        _bid(client, carol, 11, 8)
        _check("11 can be bid on again, on top of what came back", bidding.current_high_bid(11)["amount"] == 8)

        # --- a replacement made, and undone ---------------------------------
        _bid(client, alice, 9, 3); _bid(client, bob, 9, 5)
        _lq_scratch(client, 9, 22, "Ocelli")
        _bid(client, carol, 22, 3)
        client.post("/api/quiniela/unscratch", json={"horse": 9})
        numbers = [h["horse_id"] for h in client.get("/la-subasta/api/horses").get_json()["horses"]]
        _check("undoing 9 -> 22: 9 is back and 22 is gone", 9 in numbers and 22 not in numbers, f"got {numbers}")
        _check("...9's bids are back", [x["voided"] for x in _bids_on(9)] == [0, 0]
               and bidding.current_high_bid(9)["bidder_id"] == bob["id"])
        _check("...Carol's bid on 22 is voided 'scratched' with it",
               [x["voided_reason"] for x in _bids_on(22)] == ["scratched"])
        _lq_scratch(client, 9, 22, "Ocelli")
        _check("the replacement made again: 9's bids are voided once more and 22's come back",
               [x["voided_reason"] for x in _bids_on(9)] == ["scratched", "scratched"]
               and [x["voided"] for x in _bids_on(22)] == [0]
               and bidding.current_high_bid(22)["bidder_id"] == carol["id"], f"got {_bids_on(9)} {_bids_on(22)}")
    finally:
        _done_with_events()


def test_scratch_undo_after_the_lock_restores_the_owner():
    """The owner comes back: undo after the lock un-voids the ownership row,
    so the owner owes the winning bid again, the refund owed to a paid owner
    goes, and the horse can pay out. Nothing is left ownerless."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 2, 5)          # Albus
        _bid(client, alice, 3, 2)          # Intrepido
        _bid(client, bob, 5, 4)
        _bid(client, carol, 8, 3)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        r = client.post("/la-subasta/api/admin/paid", json={"bidder_id": alice["id"]})
        assert r.status_code == 200 and r.get_json()["amount"] == 7, r.get_json()
        _lq_scratch(client, 2)
        ledger = lambda: {b["id"]: b for b in client.get("/la-subasta/api/bidders").get_json()["bidders"]}[alice["id"]]
        _check("scratched after the lock: Alice owes 2 and is owed 5 back",
               ledger()["owed"] == 2 and ledger()["refund_owed"] == 5, f"got {ledger()}")
        _check("...and 2 has no owner", payouts.get_owner(2) is None)

        ev.events.clear()
        r = client.post("/api/quiniela/unscratch", json={"horse": 2})
        _check("LQ admin: undo 2 after the lock", r.status_code == 200, f"body={r.get_json()}")
        own = _ownership(2)
        _check("2's ownership row is un-voided, the owner and the bid as they were",
               own["voided"] == 0 and own["voided_reason"] is None and own["voided_at"] is None
               and own["bidder_id"] == alice["id"] and own["winning_bid"] == 5, f"got {own}")
        _check("...and so is the bid", [(x["voided"], x["voided_reason"]) for x in _bids_on(2)] == [(0, None)])
        _check("Alice owes 7 again, and no refund is owed",
               ledger()["owed"] == 7 and ledger()["refund_owed"] == 0, f"got {ledger()}")
        _check("her portfolio no longer lists 2 as scratched",
               bidding.bidder_portfolio(alice["id"])["scratched"] == [])
        _check("2 has its owner again", (payouts.get_owner(2) or {}).get("bidder_id") == alice["id"])
        _check("the undo pushed field_changed", len(ev.named("field_changed")) == 1, f"got {ev.events}")

        _set_winners(2, 5, 8)
        led = _ledger(client)
        by = _by_finish(led)
        _check("2 can win: the result naming it pays Alice, nothing unowned, the pot 5 + 4 + 3 + 2",
               by["win"]["bidder_id"] == alice["id"] and led["unowned"] == [] and led["total_pot"] == 5 + 4 + 3 + 2, f"body={led}")
    finally:
        _done_with_events()


def test_scratch_undo_after_the_lock_of_a_scratch_before_it():
    """A horse scratched before the lock and undone after it: the lock froze
    ownership without it, so its bids come back to a horse with no owner.
    Undo freezes it from them, so that no horse is left ownerless by an undo
    (there is no House to take it)."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 12, 4)
        _bid(client, bob, 13, 3)
        _lq_scratch(client, 12)                                  # open: Alice's bid is voided
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _check("locked without 12: it has no ownership row", _ownership(12) is None
               and (_ownership(13) or {}).get("bidder_id") == bob["id"])
        client.post("/api/quiniela/unscratch", json={"horse": 12})
        own = _ownership(12)
        _check("undo after the lock: 12's bids are back and it is frozen into ownership from them",
               [x["voided"] for x in _bids_on(12)] == [0] and own is not None
               and own["voided"] == 0 and own["bidder_id"] == alice["id"] and own["winning_bid"] == 4,
               f"got {own}, {_bids_on(12)}")
        _check("...so Alice owes 4 for it and it has an owner",
               bidding.bidder_portfolio(alice["id"])["total"] == 4
               and (payouts.get_owner(12) or {}).get("bidder_id") == alice["id"])
        _set_winners(12, 13, 1)
        _check("the pot counts it: 4 + 3", payouts.ledger()["total_pot"] == 7)
    finally:
        _done_with_events()


def test_scratch_undo_after_a_lock_that_froze_nothing():
    """The lock with no ownership row at all (every horse unsold): an undo
    after it freezes the returned horse from its bids all the same, so the
    ledger, which only reads, finds its owner. (It used to leave a lone horse
    for settlement to freeze; the ledger stores nothing now.)"""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _bid(client, alice, 12, 4)
        _lq_scratch(client, 12)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _check("locked with no ownership row at all",
               _ownership(12) is None and _ownership(13) is None)
        client.post("/api/quiniela/unscratch", json={"horse": 12})
        own = _ownership(12)
        _check("undo brings the bids back and freezes 12 from them: Alice's, at 4",
               [x["voided"] for x in _bids_on(12)] == [0] and own is not None and own["voided"] == 0
               and own["bidder_id"] == alice["id"] and own["winning_bid"] == 4, f"got {own}")
        _set_winners(12, 13, 14)
        led = _ledger(client)
        by = _by_finish(led)
        _check("the result: 12 pays Alice, 13 and 14 are unowned, the pot 4",
               by["win"]["bidder_id"] == alice["id"] and led["unowned"] == ["place", "show"] and led["total_pot"] == 4,
               f"body={led}")
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


def test_scratch_undo_survives_a_restart():
    """Undo is idempotent across a restart, as apply() is. pi5 restarts over a
    store on disk: what was restored stays restored, nothing changes twice
    and nothing is pushed; an undo recorded while La Subasta was not
    listening is applied at the next start, once."""
    from la_quiniela.models import LqDb
    from la_subasta import notifications as nots
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
        _bid(client, bob, 16, 3)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        store.scratch_gateway(14)                        # after the lock: bids and ownership voided
        _check("14 scratched after the lock: its ownership is voided",
               _ownership(14)["voided"] == 1 and payouts.get_owner(14) is None)
        _check("...then undone through the store: the bids and the owner are back",
               store.unscratch_gateway(14) is True
               and [b["voided"] for b in _bids_on(14)] == [0, 0]
               and _ownership(14)["voided"] == 0 and _ownership(14)["bidder_id"] == bob["id"])

        def snapshot():
            return [[tuple(r) for r in get_conn().execute(sql).fetchall()] for sql in (
                "SELECT id, horse_id, voided, voided_reason FROM bids ORDER BY id",
                "SELECT id, horse_id, bidder_id, winning_bid, voided, voided_reason, voided_at "
                "FROM ownership ORDER BY id")]

        before = snapshot()
        ev = _Events()
        nots.init_notifications(ev)
        store2 = HorseStore(db)                          # pi5 restarts: the store read back from disk
        ls_field.set_store_source(lambda: store2)
        ls_scratches.forget()
        _check("restart: the store came back with 14 in the field", 14 in ls_field.current())
        _check("restart: follow_la_quiniela() at start", ls_scratches.follow_la_quiniela() is True)
        client.get("/la-subasta/api/horses")
        ls_scratches.sync(force=True)
        _check("restart: no bid and no ownership row changed", snapshot() == before)
        _check("restart: nothing pushed", ev.events == [], f"got {ev.events}")

        # A scratch applied, then undone while La Subasta was not listening
        # (another process, or a listener that failed): restored at the next
        # start, once.
        ls_scratches.sync(force=True)
        HorseStore(db).scratch_gateway(15)               # nobody here hears it
        store3 = HorseStore(db)
        ls_field.set_store_source(lambda: store3)
        ls_scratches.forget()
        ls_scratches.follow_la_quiniela()
        _check("a scratch recorded while down is applied at start",
               [b["voided_reason"] for b in _bids_on(15)] == ["scratched"] and _ownership(15)["voided"] == 1)
        HorseStore(db).unscratch_gateway(15)             # undone while down again
        store4 = HorseStore(db)
        ls_field.set_store_source(lambda: store4)
        ls_scratches.forget()
        ev.events.clear()
        ls_scratches.follow_la_quiniela()
        _check("an undo recorded while down is restored at the next start",
               [b["voided"] for b in _bids_on(15)] == [0] and _ownership(15)["voided"] == 0
               and _ownership(15)["bidder_id"] == alice["id"], f"got {_bids_on(15)} {_ownership(15)}")
        after = snapshot()
        ls_scratches.forget()
        ls_scratches.follow_la_quiniela()
        _check("...and a second start changes nothing more", snapshot() == after)
        _check("...nor is anything pushed for it twice", ev.named("horse_scratched") == [])
    finally:
        ls_field.set_store_source(None)
        ls_scratches.forget()
        nots.init_notifications(None)
        db.close()


def _scratch_meets(app, store, db, action, horse, scratch_first):
    """Run action() on one thread and the LQ admin page's scratch of `horse`
    (POST /api/quiniela/scratch) on another, over a store on a real file, so
    that the two meet on the locks. The action's thread parks in the store's
    field() read it makes under the write lock (the check a bid makes before
    that holds no lock, and is let through): with scratch_first it parks
    before the read and is let go once the scratch holds the store's lock and
    is about to write, so the read finds the scratch; without it it parks
    after the read and is let go once the scratch is on disk, so the read
    found the horse still in the field. Returns (what action returned or
    raised, the scratch's response or what it raised, seconds taken)."""
    import threading
    parked, go = threading.Event(), threading.Event()
    real_field, real_save = store.field, db.save_scratch
    out = {}

    def field():
        if threading.current_thread().name != "acts" or not models._write_lock.locked():
            return real_field()
        if scratch_first:
            parked.set()
            go.wait(3)
        snapshot = real_field()
        if not scratch_first:
            parked.set()
            go.wait(3)
        return snapshot

    def save_scratch(was, now):
        if scratch_first:
            go.set()                    # it holds the store's lock and is about to write
        real_save(was, now)
        if not scratch_first:
            go.set()                    # it is on disk

    def acts():
        try:
            out["action"] = action()
        except Exception as exc:        # a refused bid is an answer
            out["action"] = exc
        finally:
            models.close_conn()

    def scratches():
        try:
            out["scratch"] = _lq_scratch(app.test_client(), horse)
        except Exception as exc:        # sqlite's "database is locked", say
            out["scratch"] = exc
        finally:
            models.close_conn()

    store.field, db.save_scratch = field, save_scratch
    started = time.monotonic()
    try:
        a = threading.Thread(target=acts, name="acts")
        b = threading.Thread(target=scratches, name="scratches")
        a.start()
        parked.wait(3)
        b.start()
        a.join(20)
        b.join(20)
    finally:
        del store.field, db.save_scratch
    return out.get("action"), out.get("scratch"), time.monotonic() - started


def test_scratch_never_waits_on_a_bid_holding_sqlite():
    """Lock order. A bid holds sqlite's write lock (write_txn) and a scratch
    holds La Quiniela's store lock while it writes the same database file.
    Were the bid to wait for the store inside its transaction, the two would
    wait for each other until sqlite's 5 s busy timeout, and the admin would
    get a 500 with the scratch in memory and not on disk. So the store is read
    before sqlite's lock is taken (write_txn's prepare). Four meetings on
    threads over a store on a real file, a bid and the lock (freeze_ownership)
    each against a scratch recorded before the field is read and after it:
    the scratch always answers 200, is on disk, in well under 5 s, and the
    horse's bid or ownership does not survive it, refused or voided once the
    other side has committed."""
    from la_quiniela.models import LqDb
    _reset()
    db = LqDb(_TMP_DB)
    db.init_schema()
    try:
        store = HorseStore(db)
        store.set_names({n: name for n, name in enumerate(DERBY_2026, 1)})
        app = _make_app(store=store)
        client = app.test_client()
        transition(AuctionState.OPEN)
        alice, bob = [client.post("/la-subasta/api/register", json={"name": n, "emoji": e}).get_json()["bidder"]
                      for n, e in (("Alice", "🌮"), ("Bob", "🐴"))]

        def on_disk():
            other = LqDb(_TMP_DB)       # not the connection that wrote it
            try:
                return other.load_scratches()
            finally:
                other.close()

        def meet(label, action, horse, scratch_first):
            done, scratch, seconds = _scratch_meets(app, store, db, action, horse, scratch_first)
            _check(f"{label}: the scratch of {horse} answers 200",
                   getattr(scratch, "status_code", None) == 200, f"got {scratch!r}")
            _check(f"{label}: ...and is on disk", horse in on_disk(), f"got {on_disk()}")
            _check(f"{label}: ...both finish well under sqlite's 5 s busy timeout", seconds < 2.0,
                   f"took {seconds:.1f} s")
            return done

        done = meet("bid, scratch first", lambda: bidding.place_bid(bob["id"], 9, 2), 9, True)
        _check("bid, scratch first: the bid is refused, naming the scratch",
               isinstance(done, bidding.BidError) and done.reason == "#9 is not in the field: scratched",
               f"got {done!r}")
        _check("bid, scratch first: nothing was recorded on 9", _bids_on(9) == [])

        done = meet("bid, field read first", lambda: bidding.place_bid(bob["id"], 10, 2), 10, False)
        _check("bid, field read first: the bid was accepted (its field still had 10)",
               isinstance(done, bidding.PlacedBid), f"got {done!r}")
        _check("bid, field read first: the scratch voided it once it committed",
               [(b["voided"], b["voided_reason"]) for b in _bids_on(10)] == [(1, "scratched")],
               f"got {_bids_on(10)}")

        _bid(client, alice, 11, 3)
        _bid(client, alice, 12, 4)
        done = meet("lock, scratch first", payouts.freeze_ownership, 11, True)
        _check("lock, scratch first: 11 is not frozen into ownership, 12 is",
               isinstance(done, list) and [r["horse_id"] for r in done] == [12] and _ownership(11) is None,
               f"got {done!r}, {_ownership(11)}")
        _check("lock, scratch first: 11's bid is voided",
               [b["voided_reason"] for b in _bids_on(11)] == ["scratched"])

        done = meet("lock, field read first", payouts.freeze_ownership, 12, False)
        _check("lock, field read first: 12 was frozen (its field still had 12)",
               isinstance(done, list) and [r["horse_id"] for r in done] == [12], f"got {done!r}")
        own = _ownership(12)
        _check("lock, field read first: the scratch voided its ownership once it committed",
               own is not None and own["voided"] == 1 and own["voided_reason"] == "scratched", f"got {own}")
        _check("lock, field read first: ...and its bid",
               [b["voided_reason"] for b in _bids_on(12)] == ["scratched"])

        # And the rule itself, on this thread: wherever the field is read, no
        # sqlite write lock is held.
        held = []
        real_field = store.field

        def watching():
            held.append(models.get_conn().in_transaction)
            return real_field()

        store.field = watching
        try:
            bidding.place_bid(bob["id"], 13, 1)
            payouts.freeze_ownership()
        finally:
            del store.field
        _check("place_bid and freeze_ownership read the field only with sqlite's write lock free",
               len(held) >= 3 and not any(held), f"got {held}")
    finally:
        ls_field.set_store_source(None)
        ls_scratches.forget()
        db.close()


def test_degraded_store_voids_nothing():
    """A HorseStore whose database cannot be read at start comes up empty: no
    names and no scratch records, so the field it lists is 1-20 and #22,
    which stood in for #9, looks scratched. Applying that would void every
    bid and ownership on 22 as 'scratched'. La Subasta does not: nothing is
    voided, nothing is pushed, the log says so once at ERROR, the list is
    still served, and a store that does load is applied normally afterwards."""
    import sqlite3
    from la_subasta.models import get_conn
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        _lq_scratch(client, 9, 22, "Ocelli")                # 22 runs in 9's place
        _bid(client, alice, 22, 3)
        _bid(client, bob, 22, 5)
        _bid(client, carol, 4, 2)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _check("before: Bob owns 22 and Carol 4",
               (_ownership(22) or {}).get("bidder_id") == bob["id"] and (_ownership(4) or {}).get("voided") == 0)

        def rows():
            return [[tuple(r) for r in get_conn().execute(sql).fetchall()] for sql in (
                "SELECT id, horse_id, voided, voided_reason FROM bids ORDER BY id",
                "SELECT id, horse_id, voided, voided_reason FROM ownership ORDER BY id")]

        before = rows()

        class _UnreadableDb:
            """What an LqDb is when its tables cannot be read."""
            def load_horses(self):
                raise sqlite3.OperationalError("no such table: lq_horses")

        with _Logs("la_quiniela.horses", "la_subasta.scratches") as logs:
            store = HorseStore(db=_UnreadableDb())
            _check("the store says it could not load",
                   store.load_failed is True and store.field()["degraded"] is True)
            ls_field.set_store_source(lambda: store)
            ls_scratches.forget()                            # pi5 restarts
            ev.events.clear()
            _check("follow_la_quiniela() finds the store", ls_scratches.follow_la_quiniela() is True)
            listed = [h["horse_id"] for h in client.get("/la-subasta/api/horses").get_json()["horses"]]
            client.get("/la-subasta/api/state")
            ls_scratches.sync(force=True)
            _check("field.current() still serves the list: 1-20, flagged degraded",
                   ls_field.current().numbers() == list(range(1, 21)) and ls_field.current().degraded
                   and listed == list(range(1, 21)), f"got {listed}")
            _check("no bid and no ownership was voided", rows() == before, f"got {rows()}")
            _check("22 is still Bob's, with both bids active",
                   _ownership(22)["voided"] == 0 and [b["voided"] for b in _bids_on(22)] == [0, 0])
            _check("nothing was pushed", ev.events == [], f"got {ev.events}")
            _check("the sync stays undone (a store that loads is applied normally)",
                   ls_scratches._seen is None)
            errors = logs.errors("la_subasta.scratches")
            _check("the log says so, once, at ERROR (not on every request)",
                   len(errors) == 1 and "NOT applied" in errors[0].getMessage(),
                   f"got {[r.getMessage() for r in errors]}")

            healthy = HorseStore()                           # one that does load: the default 1-20
            ls_field.set_store_source(lambda: healthy)
            client.get("/la-subasta/api/state")              # the next La Subasta request catches up
            _check("a store that loads is applied normally: 22 is not in its field, so it is voided",
                   [b["voided_reason"] for b in _bids_on(22)] == ["scratched", "scratched"]
                   and _ownership(22)["voided"] == 1, f"got {_bids_on(22)}, {_ownership(22)}")
            _check("...and pushed", [p["horse_id"] for p in ev.named("horse_scratched")] == [22],
                   f"got {ev.events}")
    finally:
        ls_field.set_store_source(None)
        ls_scratches.forget()
        _done_with_events()


def test_failed_scratch_push_is_retried():
    """sync() counts a field as seen only once its pushes have gone out. A
    push that raises after apply() committed is sent again by the next La
    Subasta request, and nothing is voided a second time."""
    from la_subasta import notifications as nots
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    real = nots.horse_scratched
    tries = []

    def flaky(horse_id, refund_count=0, ownership_voided=False):
        tries.append(horse_id)
        if len(tries) == 1:
            raise RuntimeError("socketio.emit failed")
        return real(horse_id, refund_count=refund_count, ownership_voided=ownership_voided)

    nots.horse_scratched = flaky
    try:
        _bid(client, alice, 4, 2)
        _bid(client, bob, 4, 3)
        ev.events.clear()
        with _Logs("la_subasta.scratches") as logs:
            r = _lq_scratch(client, 4)
            _check("LQ admin: the scratch is recorded though the push raised (the listener catches it)",
                   r.status_code == 200, f"body={r.get_json()}")
            _check("...the failure is logged", len(logs.errors("la_subasta.scratches")) == 1)
        voided = _bids_on(4)
        _check("...the bids on 4 were voided, and nothing has gone out yet",
               [b["voided_reason"] for b in voided] == ["scratched", "scratched"] and ev.events == [],
               f"got {voided}, {ev.events}")

        client.get("/la-subasta/api/horses")                 # the next La Subasta request
        _check("the retry pushes horse_scratched for 4, then field_changed",
               [e for e, _ in ev.events] == ["horse_scratched", "field_changed"]
               and ev.named("horse_scratched")[0]["horse_id"] == 4 and tries == [4, 4], f"got {ev.events}")
        _check("...with the field as it stands", 4 not in ev.named("field_changed")[0]["horses"])
        _check("...and voids nothing a second time", _bids_on(4) == voided, f"got {_bids_on(4)}")
        n = len(ev.events)
        client.get("/la-subasta/api/horses")
        client.get("/la-subasta/api/state")
        _check("later requests find nothing more to push", len(ev.events) == n)
    finally:
        nots.horse_scratched = real
        _done_with_events()


def test_paid_twice_keeps_the_refund():
    """Paid is idempotent. paid_amount is what was collected and the refund
    owed after a scratch is worked out from it, so a second tap must not lower
    it to the smaller total owed now and lose the refund. It only ever goes
    up, and the first paid_at stands."""
    app, client, ev, (alice, bob, carol) = _scratch_rig()
    try:
        def tap(bidder):
            r = client.post("/la-subasta/api/admin/paid", json={"bidder_id": bidder["id"]})
            assert r.status_code == 200, r.get_json()
            return r.get_json()["amount"]

        def ledger(bidder):
            return {b["id"]: b for b in client.get("/la-subasta/api/bidders").get_json()["bidders"]}[bidder["id"]]

        # Someone who owes more by the second tap is recorded as having paid more
        _bid(client, carol, 8, 2)
        _check("Carol is marked paid 2", tap(carol) == 2)
        _bid(client, carol, 9, 3)
        _check("she bids on and is marked paid again: 5, the new total",
               tap(carol) == 5 and bidding.get_bidder(carol["id"])["paid_amount"] == 5)

        _bid(client, alice, 2, 5)
        _bid(client, alice, 3, 2)
        r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
        assert r.status_code == 200, r.get_json()
        _check("Alice is marked paid 7", tap(alice) == 7)
        first = bidding.get_bidder(alice["id"])
        _lq_scratch(client, 2)                                  # 5 of it was for a horse that is out
        _check("a scratch after the lock: Alice owes 2 and is owed 5 back",
               ledger(alice)["owed"] == 2 and ledger(alice)["refund_owed"] == 5, f"got {ledger(alice)}")
        _check("a second tap reports the 7 on record, not the 2 owed", tap(alice) == 7)
        _check("...and the refund owed is still 5", ledger(alice)["refund_owed"] == 5, f"got {ledger(alice)}")
        again = bidding.get_bidder(alice["id"])
        _check("paid_amount and paid_at stand as the first tap left them",
               again["paid"] == 1 and again["paid_amount"] == 7 and again["paid_at"] == first["paid_at"],
               f"got {again}")
        tap(alice)
        _check("a third tap changes nothing either", ledger(alice)["refund_owed"] == 5)
    finally:
        _done_with_events()


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


def test_payouts_migration():
    """A database from before: the old payouts table, La Subasta's own results
    store, is dropped and payout_slots made (M5: the ledger is derived from SET
    WINNERS' result); an auction left in RACE_COMPLETE or SETTLED reads as
    LOCKED (the two states are gone: settled is LOCKED with a result); bidders
    gains cap_exempt and the guests' secret. (The House's row, which a database
    from before 2026-10-01 had, is not cleaned up any more: every box has been
    restarted since, and a guest named so lost their row at the next start.)"""
    import sqlite3
    path = tempfile.mktemp(prefix="la_subasta_old_house_", suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE bidders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, emoji TEXT NOT NULL,
            identity TEXT UNIQUE NOT NULL, push_endpoint TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), paid INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT, paid_amount REAL, event_year INTEGER NOT NULL);
        INSERT INTO bidders (id, name, emoji, identity, event_year)
            VALUES (2, 'Alice', '\U0001F32E', 'Alice \U0001F32E', 2026);
        CREATE TABLE payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bidder_id INTEGER NOT NULL REFERENCES bidders(id),
            horse_id INTEGER NOT NULL,
            finish TEXT NOT NULL CHECK (finish IN ('win','place','show')),
            amount REAL NOT NULL, paid_out INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), event_year INTEGER NOT NULL,
            UNIQUE(finish, event_year));
        INSERT INTO payouts (bidder_id, horse_id, finish, amount, paid_out, event_year)
            VALUES (1, 3, 'win', 6, 0, 2026), (2, 1, 'place', 2.5, 1, 2026);
        CREATE TABLE auction_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, state TEXT NOT NULL, opens_at TEXT, closes_at TEXT,
            total_pot REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            event_year INTEGER NOT NULL UNIQUE);
        INSERT INTO auction_state (state, total_pot, event_year) VALUES ('SETTLED', 10, 2026), ('RACE_COMPLETE', 4, 2025);
    """)
    conn.commit()
    conn.close()
    try:
        c = init_db(path)
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        _check("the old payouts table is dropped and payout_slots is there",
               "payouts" not in tables and "payout_slots" in tables, str(sorted(tables)))
        _check("Alice stays, and bidders gained cap_exempt (0) and the secret's hash (none: a phone from before is unknown)",
               [(r["identity"], r["cap_exempt"], r["secret_hash"]) for r in c.execute("SELECT identity, cap_exempt, secret_hash FROM bidders").fetchall()]
               == [("Alice \U0001F32E", 0, None)])
        states = {r["event_year"]: r["state"] for r in c.execute("SELECT event_year, state FROM auction_state").fetchall()}
        _check("SETTLED and RACE_COMPLETE read as LOCKED, the pots kept",
               states == {2026: "LOCKED", 2025: "LOCKED"}
               and [r["total_pot"] for r in c.execute("SELECT total_pot FROM auction_state ORDER BY event_year").fetchall()] == [4, 10],
               str(states))
        c = init_db(path)
        _check("the migration is idempotent",
               {r["event_year"]: r["state"] for r in c.execute("SELECT event_year, state FROM auction_state").fetchall()} == states
               and "payouts" not in {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()})
    finally:
        init_db(_TMP_DB)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def test_bidders_unique_per_year_migration():
    """A bidders table from before (identity unique across every year) is
    rebuilt with UNIQUE(identity, event_year): every row kept with its id, so
    the bids, owners and payouts pointing at them stay right and foreign keys
    still hold; the id counter is kept (after a wipe it is above every id
    left: no new guest gets a number an old phone holds); last year's name
    registers this year; a second start changes nothing."""
    import sqlite3
    old_schema = """
        CREATE TABLE bidders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, emoji TEXT NOT NULL,
            identity TEXT UNIQUE NOT NULL, push_endpoint TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), paid INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT, paid_amount REAL, event_year INTEGER NOT NULL,
            cap_exempt INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX idx_bidders_event_year ON bidders(event_year);
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bidder_id INTEGER NOT NULL REFERENCES bidders(id),
            horse_id INTEGER NOT NULL, amount REAL NOT NULL,
            bid_time TEXT NOT NULL DEFAULT (datetime('now')), voided INTEGER NOT NULL DEFAULT 0,
            voided_reason TEXT, event_year INTEGER NOT NULL);
    """
    paths = []

    def old_db(script):
        path = tempfile.mktemp(prefix="la_subasta_old_bidders_", suffix=".db")
        paths.append(path)
        conn = sqlite3.connect(path)
        conn.executescript(old_schema + script)
        conn.commit()
        conn.close()
        return path

    def unique_indexes(c):
        return sorted(tuple(r["name"] for r in c.execute(f'PRAGMA index_info("{i["name"]}")').fetchall())
                      for i in c.execute("PRAGMA index_list(bidders)").fetchall() if i["unique"])

    def counter(c):
        row = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'bidders'").fetchone()
        return row["seq"] if row else None

    try:
        path = old_db("""
            INSERT INTO bidders (id, name, emoji, identity, paid, paid_amount, event_year, cap_exempt)
                VALUES (1, 'Alice', '\U0001F32E', 'Alice \U0001F32E', 1, 7, 2026, 0),
                       (5, 'Bob', '\U0001F434', 'Bob \U0001F434', 0, NULL, 2026, 1);
            UPDATE sqlite_sequence SET seq = 12 WHERE name = 'bidders';
            INSERT INTO bids (bidder_id, horse_id, amount, event_year) VALUES (1, 3, 7, 2026), (5, 4, 2, 2026);
        """)
        raw = sqlite3.connect(path)
        before = [tuple(r) for r in raw.execute("SELECT * FROM bidders ORDER BY id").fetchall()]
        raw.close()
        la_config.DB_PATH = path                 # register_bidder writes through get_conn(), to DB_PATH
        c = init_db(path)
        _check("bidders is rebuilt: unique on (identity, event_year), no longer on identity alone",
               unique_indexes(c) == [("identity", "event_year")], f"got {unique_indexes(c)}")
        after = [tuple(r) for r in c.execute("SELECT * FROM bidders ORDER BY id").fetchall()]
        _check("every row kept as it was, with its id (paid, paid_amount, cap_exempt too), and no secret: those phones are unknown",
               after == [row + (None,) for row in before], f"got {after}")
        _check("the column list is the same, in the same order",
               [r["name"] for r in c.execute("PRAGMA table_info(bidders)").fetchall()] == list(models._BIDDER_ALL_COLUMNS))
        _check("the id counter is kept (12, above every id left)", counter(c) == 12, f"got {counter(c)}")
        _check("foreign keys are on again, nothing points at a missing bidder, and the bids still point at bidders",
               c.execute("PRAGMA foreign_keys").fetchone()[0] == 1 and c.execute("PRAGMA foreign_key_check").fetchall() == []
               and "REFERENCES bidders(id)" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'bids'").fetchone()["sql"])
        try:
            c.execute("INSERT INTO bids (bidder_id, horse_id, amount, event_year) VALUES (99, 1, 1, 2026)")
            _check("a bid by a bidder that does not exist is still refused", False, "it went in")
        except sqlite3.IntegrityError:
            _check("a bid by a bidder that does not exist is still refused", True)
        _check("the event_year index is back, and no bidders_new is left",
               c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_bidders_event_year'").fetchone()[0] == 1
               and c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'bidders_new'").fetchone()[0] == 0)
        alice = bidding.register_bidder("Alice", "\U0001F32E", event_year=2027)
        _check("last year's Alice registers in 2027, numbered after the kept counter (13)", alice["id"] == 13, f"got {alice}")
        try:
            bidding.register_bidder("Alice", "\U0001F32E", event_year=2027)
            _check("...and only once in 2027", False, "a second 2027 Alice went in")
        except bidding.BidError:
            _check("...and only once in 2027", True)
        rows = [tuple(r) for r in c.execute("SELECT * FROM bidders ORDER BY id").fetchall()]
        c = init_db(path)
        _check("a second start changes nothing",
               [tuple(r) for r in c.execute("SELECT * FROM bidders ORDER BY id").fetchall()] == rows
               and unique_indexes(c) == [("identity", "event_year")] and counter(c) == 13)

        # Wiped (pi5/tools/wipe.py empties the rows and keeps the counter): the counter survives the rebuild
        path = old_db("""
            INSERT INTO bidders (id, name, emoji, identity, event_year) VALUES (7, 'Al', '\U0001F32E', 'Al \U0001F32E', 2026);
            DELETE FROM bidders;
        """)
        la_config.DB_PATH = path
        c = init_db(path)
        _check("a wiped table: rebuilt, still empty, its counter kept (7)",
               unique_indexes(c) == [("identity", "event_year")]
               and c.execute("SELECT COUNT(*) FROM bidders").fetchone()[0] == 0 and counter(c) == 7, f"counter {counter(c)}")
        _check("...so the next guest is 8, never a number a phone from before holds",
               bidding.register_bidder("Cy", "\U0001F32E", event_year=2027)["id"] == 8)
    finally:
        la_config.DB_PATH = _TMP_DB
        init_db(_TMP_DB)
        for path in paths:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass


def _picker_check():
    """pi5/tools/picker_check.py: its Chrome drives any page over the DevTools protocol."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "picker_check", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "picker_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve(app, fail):
    """The app on a loopback port, in threads, as a phone reaches pi5; a path in `fail` answers 503.
    Each request closes its thread's database connection when done: a keep-alive thread would hold
    the file open, and on Windows the next test's _reset() could not delete it. stop() waits for the
    requests in flight first."""
    import logging
    import threading
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    lock, busy = threading.Lock(), [0]

    def wsgi(environ, start_response):
        with lock:
            busy[0] += 1
        try:
            if environ.get("PATH_INFO") in fail:
                start_response("503 Service Unavailable", [("Content-Length", "0")])
                return [b""]
            return app(environ, start_response)
        finally:
            models.close_conn()
            with lock:
                busy[0] -= 1

    httpd = make_server("127.0.0.1", 0, wsgi, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop():
        end = time.monotonic() + 10
        while busy[0] and time.monotonic() < end:
            time.sleep(0.05)
        httpd.shutdown()
        httpd.server_close()

    httpd.stop = stop
    return httpd


# What the guest page shows: the join screen, its welcome-back line, the app, any card's error, the badge.
_GUEST_VIEW_JS = """(() => {
    const shown = (id) => { const e = document.getElementById(id); return !!e && e.getClientRects().length > 0; };
    return {join: shown('identity-modal'), again: shown('ls-join-again'), app: shown('ls-app'),
            line: (document.getElementById('ls-join-again') || {}).textContent,
            errors: [...document.querySelectorAll('[data-role="error"]')].filter((e) => !e.hidden).map((e) => e.textContent),
            badge: document.getElementById('ls-identity-display').textContent};
})()"""


def test_guest_page_rejoins_in_chrome():
    """M4 (a), on a phone: the guest page in headless Chrome at a phone's size, with touch. A kept identity
    from last year boots to the join screen with the welcome-back line, forgotten, and joining again with the
    same name works; a wipe while that page is open sends the next bid to the join screen, never a dead
    "Unknown bidder", and one tap joins again; a kept identity this year's auction knows goes straight in;
    when the check gets no answer the phone goes in with what it kept; and ?dev=1 shows no test tools
    (M4 c), not even with the old panel's flag left in storage. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("guest page browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    transition(AuctionState.OPEN)
    year = la_config.EVENT_YEAR
    old = bidding.register_bidder("Old Timer", "🎺", event_year=year - 1)
    fail = set()
    httpd = _serve(app, fail)
    base = f"http://127.0.0.1:{httpd.server_port}/la-subasta/"
    chrome = picker_check.Chrome(touch=True, size=(390, 844))
    try:
        chrome.call("Network.enable")
        chrome.call("Network.setBlockedURLs", urls=["*socket.io*"])     # no live updates: nothing here needs them

        def kept(bidder, secret=True):
            """What a phone keeps: the guest number, name, emoji and (S1) the secret it was given at join."""
            return {"bidder_id": bidder["id"], "name": bidder["name"], "emoji": bidder["emoji"],
                    "identity": bidder["identity"], **({"secret": bidder["secret"]} if secret else {})}

        def load(identity, query="", left=None):
            """The page as a phone opens it, onboarded, keeping `identity` (and `left`, other keys an old
            page left in its storage); returns once it has decided."""
            chrome.call("Page.navigate", url=base + "api/state")
            chrome.wait_for("document.readyState === 'complete'", timeout=20)
            keys = dict(left or {}, la_subasta_onboarded="true", la_subasta_identity=json.dumps(identity))
            chrome.eval("localStorage.clear();" + "".join(f" localStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
                                                           for k, v in keys.items()) + " true")
            chrome.call("Page.navigate", url=base + query)
            chrome.wait_for("document.readyState === 'complete'", timeout=20)
            return chrome.wait_for("(() => { const v = %s; return v.join || v.app ? v : null; })()" % _GUEST_VIEW_JS)

        def stored():
            raw = chrome.eval("localStorage.getItem('la_subasta_identity')")
            return json.loads(raw) if raw else None

        line = "Welcome back. Join again for this year's auction."
        view = load(kept(old))
        _check("last year's identity: the join screen with the welcome-back line, the app hidden",
               view["join"] and view["again"] and not view["app"] and view["line"] == line, str(view))
        _check("...and the phone has forgotten it", stored() is None, str(stored()))
        chrome.eval("const n = document.getElementById('ls-name-input'); n.value = 'Old Timer';"
                    " n.dispatchEvent(new Event('input')); true")
        chrome.tap('.ls-emoji-btn[data-emoji="🎺"]')
        chrome.tap("#ls-submit-btn")
        in_app = "(() => { const v = %s; return v.app ? v : null; })()" % _GUEST_VIEW_JS
        view = chrome.wait_for(in_app)
        again = stored() or {}
        _check("joining again with last year's name and emoji: in, a new guest this year",
               not view["join"] and view["badge"] == "Old Timer 🎺" and again.get("bidder_id") not in (None, old["id"])
               and bidding.get_bidder(again.get("bidder_id")) is not None, f"{view} {again}")

        bid_button = '.ls-horse-card[data-horse-id="1"] .ls-bid-btn[data-delta="1"]'
        chrome.wait_for("(() => { const b = document.querySelector(%s); return !!b && !b.disabled; })()"
                        % json.dumps(bid_button))
        with models.write_txn() as conn:              # the wipe: every guest, bid and state row gone, the counters kept
            for table in ("bids", "ownership", "payout_slots", "bidders", "auction_state"):
                conn.execute(f"DELETE FROM {table}")
        chrome.tap(bid_button)
        view = chrome.wait_for("(() => { const v = %s; return v.join ? v : null; })()" % _GUEST_VIEW_JS)
        _check("wiped while the page was open: the next bid lands on the join screen with the line",
               view["again"] and not view["app"] and view["line"] == line, str(view))
        _check("...never a dead \"Unknown bidder\" on a card, and the phone has forgotten the guest",
               view["errors"] == [] and stored() is None, f"{view['errors']} {stored()}")
        _check("...the form still holds the name and emoji joined with, Enter ready",
               chrome.eval("document.getElementById('ls-name-input').value === 'Old Timer'"
                           " && !document.getElementById('ls-submit-btn').disabled"))
        transition(AuctionState.OPEN)                 # the host opens the auction again
        chrome.tap("#ls-submit-btn")
        view = chrome.wait_for(in_app)
        _check("one tap and the guest is in again",
               view["badge"] == "Old Timer 🎺" and bidding.get_bidder((stored() or {}).get("bidder_id")) is not None,
               f"{view} {stored()}")
        again = stored() or {}
        _check("...with a secret of its own, which the phone keeps beside the number", len(again.get("secret", "")) >= 43
               and bidding.holds_secret(again.get("bidder_id"), again.get("secret")), str(again))
        chrome.wait_for("(() => { const b = document.querySelector(%s); return !!b && !b.disabled; })()" % json.dumps(bid_button))
        chrome.tap(bid_button)
        end = time.monotonic() + 5
        while bidding.current_high_bid(1) is None and time.monotonic() < end:
            time.sleep(0.05)
        _check("...and the page's bid carries it: the bid stands, the guest's own",
               bidding.current_high_bid(1) is not None and bidding.current_high_bid(1)["bidder_id"] == again["bidder_id"],
               str(bidding.current_high_bid(1)))

        alice = bidding.register_bidder("Alice", "🌮")
        view = load(kept(alice))
        _check("this year's identity: straight in, no join screen, the badge is Alice",
               view["app"] and not view["join"] and view["badge"] == "Alice 🌮", str(view))
        _check("...and the phone keeps it", stored() == kept(alice), str(stored()))
        view = load(kept(alice, secret=False))
        _check("a phone from before the secret (the number and the name, no secret): the join screen with the welcome-back line, forgotten",
               view["join"] and view["again"] and not view["app"] and view["line"] == line and stored() is None, f"{view} {stored()}")
        view = load({**kept(alice), "secret": "a-fake-secret"})
        _check("a wrong secret: the same", view["join"] and view["again"] and not view["app"] and stored() is None, f"{view} {stored()}")

        carol = bidding.register_bidder("Carol", "🍹")
        fail.add("/la-subasta/api/check-identity")
        view = load(kept(carol))
        _check("the check gets no answer (503): in with what the phone kept, nothing forgotten",
               view["app"] and not view["join"] and view["badge"] == "Carol 🍹" and stored() == kept(carol), str(view))
        fail.clear()
        dave = bidding.register_bidder("Dave", "🍀")
        load(kept(dave))
        with models.write_txn() as conn:              # Dave's secret stops being his: the server will not take it
            conn.execute("UPDATE bidders SET secret_hash = NULL WHERE id = ?", (dave["id"],))
        other_bid = '.ls-horse-card[data-horse-id="2"] .ls-bid-btn[data-delta="1"]'
        chrome.wait_for("(() => { const b = document.querySelector(%s); return !!b && !b.disabled; })()" % json.dumps(other_bid))
        chrome.tap(other_bid)
        view = chrome.wait_for("(() => { const v = %s; return v.join ? v : null; })()" % _GUEST_VIEW_JS)
        _check("a bid answered 401 (the secret is not that guest's) lands on the join screen with the line, the guest forgotten, nothing on a card",
               view["again"] and not view["app"] and view["line"] == line and view["errors"] == [] and stored() is None
               and bidding.current_high_bid(2) is None, f"{view} {stored()}")

        dev = ("({panel: document.querySelectorAll('[id^=ls-dev], [data-dev-action], .ls-dev').length,"
               " keys: Object.keys(localStorage).sort()})")
        load(kept(carol), "?dev=1")
        _check("?dev=1: no dev panel, and nothing new in the phone's storage",
               chrome.eval(dev) == {"panel": 0, "keys": ["la_subasta_identity", "la_subasta_onboarded"]},
               str(chrome.eval(dev)))
        load(kept(carol), "?dev=1", left={"la_subasta_dev_mode": "1"})
        _check("...nor with the old panel's flag left in storage", chrome.eval(dev)["panel"] == 0, str(chrome.eval(dev)))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        httpd.stop()


def test_guest_js_follows_the_field():
    """Guest phones drop a scratched horse and pick up a replacement
    without a reload: guest.js drops the horse on horse_scratched (closing a
    custom-bid dialog open on it, with the reason) and reads the list again on
    field_changed, which always follows, and on every socket connect (a phone
    that was offline catches up); it rebuilds the cards when the horses change
    (9 out, 22 in: same count), bids on the card's current horse, does not
    write a bid into a card whose horse left mid-request, and draws the
    saddle-cloth digits in the cloth's own colour (#2: black on white)."""
    _reset()
    app = _make_app()
    # A Windows checkout writes guest.js with CRLF (core.autocrlf), and the
    # checks below match source that spans lines: compare it as LF.
    js = app.test_client().get("/la-subasta/static/js/guest.js").get_data(as_text=True).replace("\r\n", "\n")
    hs = js[js.index("socket.on('horse_scratched'"):]
    hs = hs[:hs.index("});")]
    _check("horse_scratched drops the horse from the list", "delete state.horses[payload.horse_id]" in hs)
    _check("horse_scratched leaves the re-read to field_changed, which always follows",
           "refreshHorses()" not in hs)
    _check("horse_scratched closes a custom-bid dialog open on that horse, and says why",
           "state.customBid.horseId === payload.horse_id" in hs and "state.customBid.close()" in hs
           and "showNotice(" in hs and "was scratched" in hs)
    _check("the custom-bid dialog registers itself where that handler finds it, and clears itself",
           "state.customBid = { horseId: horseId, close: cleanup };" in js
           and "state.customBid.close === cleanup) state.customBid = null;" in js)
    fc = js[js.index("socket.on('field_changed'"):]
    fc = fc[:fc.index("});")]
    _check("field_changed reads the list again", "refreshHorses()" in fc)
    cn = js[js.index("socket.on('connect'"):]
    cn = cn[:cn.index("});")]
    _check("every socket connect, a reconnect included, reads the list again", "refreshHorses()" in cn)
    _check("cards are rebuilt when the horses differ, not only their count",
           "const sameHorses" in js and "existing[i].dataset.horseId === String(h.horse_id)" in js)
    _check("a bid button bids on the card's current horse",
           "const horseId = parseInt(card.dataset.horseId, 10);" in js)
    _check("a bid accepted for a horse that left mid-request is not written to a card",
           "state.horses[horseId].current_high_bid" not in js
           and "state.horses[horseId].current_leader" not in js
           and "const horse = state.horses[horseId];\n            if (horse) {" in js)
    _check("the saddle-cloth digits take the cloth's own colour",
           "saddle.style.color = horse.saddle_cloth_text_color" in js)


def test_horse_lists_run_in_program_number_order():
    """Joey, 2026-10-23: the field is shown in program-number order everywhere. #21 drawn in for #19 and #7 scratched with
    no replacement: the horses endpoint (the admin page's lists and pickers read it as it is) lists 1-6, 8-18, 20, 21, and
    so does the guest page's card list, in a phone-sized Chrome. Skipped where there is no Chrome (the endpoint is checked
    all the same)."""
    _reset()
    app = _make_app()
    client = app.test_client()
    _lq_names(client)
    transition(AuctionState.OPEN)
    _check("19 -> 21 (Great White) and 7 out: both recorded",
           _lq_scratch(client, 19, 21, "Great White").status_code == 200 and _lq_scratch(client, 7).status_code == 200)
    want = [n for n in range(1, 19) if n != 7] + [20, 21]
    ids = [h["horse_id"] for h in client.get("/la-subasta/api/horses").get_json()["horses"]]
    _check("GET /api/horses: the field by program number, 21 last", ids == want, str(ids))
    picker_check = _picker_check()
    if not picker_check.available():
        _check("guest page order check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    joined = client.post("/la-subasta/api/register", json={"name": "Alice", "emoji": "\U0001f32e"}).get_json()
    alice = joined["bidder"]
    httpd = _serve(app, set())
    base = f"http://127.0.0.1:{httpd.server_port}/la-subasta/"
    chrome = picker_check.Chrome(touch=True, size=(390, 844))
    try:
        chrome.call("Network.enable")
        chrome.call("Network.setBlockedURLs", urls=["*socket.io*"])
        chrome.call("Page.navigate", url=base + "api/state")
        chrome.wait_for("document.readyState === 'complete'", timeout=20)
        identity = {"bidder_id": alice["id"], "name": alice["name"], "emoji": alice["emoji"], "identity": alice["identity"],
                    "secret": joined["secret"]}
        chrome.eval("localStorage.clear(); localStorage.setItem('la_subasta_onboarded', 'true'); "
                    f"localStorage.setItem('la_subasta_identity', {json.dumps(json.dumps(identity))}); true")
        chrome.call("Page.navigate", url=base)
        chrome.wait_for(f"document.querySelectorAll('.ls-horse-card').length === {len(want)}", timeout=20)
        cards = chrome.eval("[...document.querySelectorAll('.ls-horse-card')].map((c) => Number(c.dataset.horseId))")
        _check("the guest page lists the cards by program number, 21 last", cards == want, str(cards))
    finally:
        chrome.close()
        httpd.stop()


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
# No test tools on guest pages (gap review M4 c)
# -----------------------------------------------------------------------------

def test_no_test_tools_on_guest_pages():
    """M4 (c): the guest page's ?dev=1 panel is gone, with its code and its
    styles; and so are the routes only a test needed, which it drove with no
    auth: the reset (all three scopes; pi5/tools/wipe.py starts fresh, with a
    backup) and the forced state change. They answer 404 with
    DDM_LQ_DEV_ENDPOINTS off or on (it gates nothing since 59e3b14), and
    wipe or move nothing."""
    _reset()
    app = _make_app()
    client = app.test_client()
    alice = client.post("/la-subasta/api/register", json={"name": "Alice", "emoji": "🌮"}).get_json()["bidder"]
    for path in ("/la-subasta/", "/la-subasta/?dev=1"):
        html = client.get(path).get_data(as_text=True)
        _check(f"{path}: no dev panel, no dev controls, no dev toast",
               "ls-dev" not in html and "data-dev-action" not in html and "DEV PANEL" not in html)
    js = _static(client, "/la-subasta/static/js/guest.js")
    css = _static(client, "/la-subasta/static/css/la-subasta-mobile.css")
    _check("guest.js reads no ?dev and keeps no dev flag",
           "get('dev')" not in js and "la_subasta_dev_mode" not in js and "setupDevPanel" not in js)
    _check("guest.js calls no admin route (the settings it reads are /api/settings, open to everyone: the admin routes take the host's PIN)",
           js.count("/api/admin/") == 0 and "settings: '/la-subasta/api/settings'," in js
           and all(f"'{a}'" not in js for a in ("start", "lock", "results", "transition")))
    _check("the stylesheet has no dev panel styles", ".ls-dev" not in css)
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    _check("no reset or forced-state route under /la-subasta",
           "/la-subasta/api/admin/reset" not in rules and "/la-subasta/api/admin/transition" not in rules)
    for flag in (None, "1"):
        if flag:
            os.environ["DDM_LQ_DEV_ENDPOINTS"] = flag
        try:
            said = "on" if flag else "off"
            codes = [client.post(f"/la-subasta/api/admin/reset?confirm=TESTING&scope={scope}").status_code
                     for scope in ("full", "bids", "state")]
            _check(f"POST /api/admin/reset (full, bids, state) is a 404, DDM_LQ_DEV_ENDPOINTS {said}",
                   codes == [404, 404, 404], str(codes))
            r = client.post("/la-subasta/api/admin/transition", json={"state": "OPEN"})
            _check(f"POST /api/admin/transition is a 404, DDM_LQ_DEV_ENDPOINTS {said}", r.status_code == 404,
                   f"{r.status_code}")
        finally:
            os.environ.pop("DDM_LQ_DEV_ENDPOINTS", None)
    _check("...and nothing was wiped or moved: Alice is there, the auction not started",
           [b["id"] for b in bidding.list_bidders()] == [alice["id"]] and get_state() == AuctionState.NOT_STARTED)
    import importlib.util
    from la_subasta import notifications
    _check("la_subasta.reset and its auction_reset event are gone",
           importlib.util.find_spec("la_subasta.reset") is None and not hasattr(notifications, "auction_reset"))


def _static(client, path):
    """A static file as the app serves it, with LF line endings: a Windows
    checkout writes these with CRLF, and the checks match source that spans
    lines."""
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True).replace("\r\n", "\n")


def test_admin_page():
    """GET /la-subasta/admin is a page now, not the Phase 3 JSON placeholder:
    the auction's controls with the lock's unsold-horse warning and its
    confirm button, the bidders (paid; the cap exemption) and the payout
    ledger (the picker for an unowned slot). No JS engine in this harness, so
    this checks the markup and the script's wiring; the page itself was
    driven in a browser."""
    _reset()
    app = _make_app()
    client = app.test_client()
    r = client.get("/la-subasta/admin")
    html = r.get_data(as_text=True)
    _check("GET /la-subasta/admin is 200 HTML", r.status_code == 200 and "text/html" in r.content_type,
           f"status={r.status_code} type={r.content_type}")
    _check("...no longer the JSON placeholder", "Phase 3 UI pending" not in html)
    for marker, label in (
            ('id="ls-admin-state"', "the state pill"),
            ('id="ls-admin-lock"', "the lock button"),
            ('data-admin-action="lock"', "the lock button's action"),
            ('id="ls-admin-unsold"', "the unsold-horse warning"),
            ('id="ls-admin-unsold-list"', "its list of horses"),
            ('id="ls-admin-unsold-confirm"', "its confirm button"),
            ('id="ls-admin-unsold-cancel"', "its cancel button"),
            ('id="ls-admin-bidders-list"', "the bidders list"),
            ('id="ls-admin-payouts"', "the payout ledger")):
        _check(f"the admin page has {label}", marker in html, f"missing {marker}")
    _check("the unsold-horse warning is hidden until the lock answers 409",
           '<div id="ls-admin-unsold" class="adm-warn" role="alert" hidden>' in html)
    _check("no results form: the results are SET WINNERS' (M5)",
           "ls-admin-results" not in html and "ls-admin-win" not in html and "Enter results" not in html)
    _check("the page links its own script and stylesheet",
           "/la-subasta/static/js/admin.js" in html and "/la-subasta/static/css/la-subasta-admin.css" in html)

    js = _static(client, "/la-subasta/static/js/admin.js")
    for marker, label in (
            ("ADMIN + 'lock'", "the lock route"),
            ("{ confirm: true }", "the lock's confirm"),
            ("Array.isArray(r.data.unsold)", "the 409's unsold list"),
            ("ADMIN + 'cap-exempt'", "the cap exemption route"),
            ("ADMIN + 'payout-slot'", "the payout slot route"),
            ("ADMIN + 'paid'", "mark paid"),
            ("ADMIN + 'payout-paid'", "the paid-out mark"),
            ("paid_on_old_result", "the lines for a payout paid on an earlier result"),
            ("ADMIN + 'payouts'", "the payout ledger"),
            ("API + 'bidders'", "the bidders"),
            ("p.unowned", "the unowned flag"),
            ("aria-pressed", "the cap toggle's state")):
        _check(f"admin.js wires {label}", marker in js, f"missing {marker!r}")
    _check("admin.js sets text with textContent and never innerHTML (names are what guests typed)",
           "textContent" in js and "innerHTML" not in js)
    css = client.get("/la-subasta/static/css/la-subasta-admin.css")
    _check("the admin stylesheet is served", css.status_code == 200 and b".adm-warn" in css.data)


def test_guest_toast_has_its_own_element():
    """A horse scratched out from under an open bid dialog is told to the
    guest in a toast of its own (#ls-toast)."""
    _reset()
    app = _make_app()
    client = app.test_client()
    html = client.get("/la-subasta/").get_data(as_text=True)
    js = _static(client, "/la-subasta/static/js/guest.js")
    css = _static(client, "/la-subasta/static/css/la-subasta-mobile.css")
    _check("guest.html has #ls-toast, hidden until used",
           '<div id="ls-toast" class="ls-toast" role="status" aria-live="polite" hidden></div>' in html)
    _check("the stylesheet styles .ls-toast, and hides it while hidden",
           ".ls-toast {" in css and ".ls-toast[hidden] { display: none; }" in css)
    body = js[js.index("function showNotice(msg) {"):]
    body = body[:body.index("\n    }\n")]
    _check("showNotice writes to #ls-toast", "getElementById('ls-toast')" in body, f"got {body}")


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
    _check("num_bidders == 2 after 2 registrations",
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
    r = client.post("/la-subasta/api/admin/lock", json={"confirm": True})
    _check("admin/lock returns 200", r.status_code == 200)
    _check("lock fired auction_state_changed with new_state=LOCKED",
           any(e[0] == "auction_state_changed"
               and e[1]["new_state"] == "LOCKED"
               for e in events))
    _check("lock still fires auction_locked event",
           any(e[0] == "auction_locked" for e in events))

    # No results entry here any more: the result is SET WINNERS' (one record), so nothing fires
    # RACE_COMPLETE or SETTLED, and LOCKED is the last state
    events.clear()
    r = client.post("/la-subasta/api/admin/results", json={"win": 1, "place": 2, "show": 3})
    _check("admin/results is gone (404), and no state event follows",
           r.status_code == 404 and not [e for e in events if e[0] == "auction_state_changed"], f"{r.status_code} {events}")
    _check("LOCKED is the last state: NOT_STARTED, OPEN, FINAL_HOUR, LOCKED and no other",
           [s.value for s in AuctionState] == ["NOT_STARTED", "OPEN", "FINAL_HOUR", "LOCKED"], str(list(AuctionState)))

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
                                            "/la-subasta/api/settings")),
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
    _check("boot: /api/settings returns 200",
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
    _check("the host's PIN reaches the app from pi5/.env (config HOST_PIN), and its login route is there",
           main.app.config.get("HOST_PIN") == main._config.HOST_PIN and "/host/login" in rules, str(main.app.config.get("HOST_PIN")))
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
    _run("identity — another year's bidder is unknown everywhere (B7); names unique per year",
         test_another_years_bidder_is_unknown)
    _run("bid validation", test_bid_validation)
    _run("bid undo (10s window)", test_bid_undo)
    _run("identity — a guest is a number and a secret: bids, undos and check-identity need it; it is in no list or broadcast (S1)",
         test_a_guest_is_a_number_and_a_secret)
    _run("host — /la-subasta/admin and /api/admin/* take the PIN; no PIN set is off, not open (S1)", test_the_host_pages_need_the_pin)
    _run("lock — after it only the host voids a bid, and a void follows ownership (S2)", test_after_the_lock_only_the_host_voids_a_bid)
    _run("lock — a bid in flight at the lock is refused (S3)", test_a_bid_in_flight_at_the_lock_is_refused)
    _run("lock — 24 threads bidding as the host locks: ownership agrees with the high bids (S3's stress test)",
         test_bids_racing_the_lock_never_disagree_with_ownership)
    _run("settings — a reset follows the lock the way a set does (S6)", test_settings_reset_follows_the_lock)
    _run("bad numbers — NaN, Infinity, numbers too big for the database: 400, never 500 (S8)", test_bad_numbers_are_400s)
    _run("a guest named The House keeps their row at the next start (S8)", test_a_guest_named_the_house_keeps_their_row)
    _run("payouts — from SET WINNERS, through the board: whole pesos, a correction follows, Reset betting clears (M5)", test_payouts)
    _run("paid out — the mark, tied to the finisher; paid on an earlier result until unmarked", test_paid_out_marks)
    _run("results — La Subasta's own entry and store are gone (M5)", test_results_entry_is_gone)
    _run("no House - no sentinel row, no House payout, no is_house", test_no_house)
    _run("no House - a paying horse nobody owns is a flagged slot; the admin names the payer",
         test_unowned_win_is_a_flagged_slot)
    _run("no House - the payout slot picker needs results", test_payout_slot_needs_results)
    _run("no House - a void with no second bidder leaves the horse unsold",
         test_void_with_no_second_bidder_leaves_the_horse_unsold)
    _run("lock - warns about unsold horses; confirm locks anyway", test_lock_warns_about_unsold_horses)
    _run("lock - no confirm needed when every horse is sold", test_lock_needs_no_confirm_when_every_horse_is_sold)
    _run("cap - an exempt bidder holds more than the cap, the rest do not",
         test_cap_exempt_bidder_holds_more_than_the_cap)

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
    _run("scratch — undo restores everything: the bids, not an admin's void",
         test_scratch_undo_restores_everything)
    _run("scratch — undo after the lock restores the owner", test_scratch_undo_after_the_lock_restores_the_owner)
    _run("scratch — undo after the lock of a scratch before it: frozen from the bids",
         test_scratch_undo_after_the_lock_of_a_scratch_before_it)
    _run("scratch — undo after a lock that froze nothing", test_scratch_undo_after_a_lock_that_froze_nothing)
    _run("scratch — a restart does not double-void", test_scratch_restart_does_not_double_void)
    _run("scratch — undo survives a restart, and an undo made while down is restored",
         test_scratch_undo_survives_a_restart)
    _run("scratch — never waits on a bid holding sqlite (lock order)",
         test_scratch_never_waits_on_a_bid_holding_sqlite)
    _run("scratch — a degraded store voids nothing", test_degraded_store_voids_nothing)
    _run("scratch — a failed push is retried, nothing voided twice", test_failed_scratch_push_is_retried)
    _run("paid — a second tap keeps the refund owed", test_paid_twice_keeps_the_refund)
    _run("scratch — old ownership table migrated", test_ownership_migration)
    _run("migration — the old payouts table, RACE_COMPLETE / SETTLED, the guests' secret column", test_payouts_migration)
    _run("migration — bidders unique per year; ids, foreign keys and the counter kept",
         test_bidders_unique_per_year_migration)
    _run("identity — a phone rejoins: last year's, this year's, a wipe, no answer (Chrome)",
         test_guest_page_rejoins_in_chrome)
    _run("scratch — guest page follows the field live", test_guest_js_follows_the_field)
    _run("field order — the horses endpoint and the guest page list the field by program number (Joey, 2026-10-23)",
         test_horse_lists_run_in_program_number_order)
    _run("guest UI — /api/horses scratched flag round-trip (regression)",
         test_horses_scratched_flag_roundtrip)
    _run("guest UI — static assets served", test_static_assets_served)
    _run("api — dynamic responses are no-store (cache hardening)",
         test_api_responses_are_no_store)
    _run("guest UI — no '$' currency in guest.js (2027 pesos mode)",
         test_guest_js_no_dollar_currency)
    _run("guest UI — 2027 pesos button format + footer year",
         test_guest_2027_pesos_button_format)
    _run("no test tools — no dev panel on the guest page; no reset or forced-state route",
         test_no_test_tools_on_guest_pages)
    _run("admin page — controls, the lock's warning, bidders, the payout ledger", test_admin_page)
    _run("guest UI — the toast for a scratched horse is its own element", test_guest_toast_has_its_own_element)
    _run("scratch — La Subasta's own scratch route is gone",
         test_own_scratch_route_gone)
    _run("state — /api/state includes num_bidders + num_bids",
         test_state_includes_bidder_and_bid_counts)
    _run("guest UI — auction_state_changed broadcast", test_auction_state_changed_broadcast)
    _run("guest UI — JS listens for auction_state_changed", test_guest_js_listens_for_state_changed)
    _run("guest UI — initial horse load after registration (regression)",
         test_initial_horse_load_after_registration)

    # Phase 2A.5
    _run("onboarding — splash + how-it-works + help markup", test_onboarding_markup_present)
    _run("onboarding — 2027 pesos copy", test_onboarding_2027_pesos_copy)
    _run("onboarding — JS state machine wiring", test_onboarding_js_state_machine)

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

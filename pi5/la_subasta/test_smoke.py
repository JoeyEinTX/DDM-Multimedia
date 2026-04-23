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
    AuctionState, transition, get_state,
)
from la_subasta.blueprint import la_subasta_bp, init_la_subasta  # noqa: E402


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

def _make_app():
    """Create a Flask app with just the la_subasta blueprint for testing."""
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True
    init_la_subasta(socketio=None, racing_service=None)
    app.register_blueprint(la_subasta_bp)
    return app


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

    # --- scratched horse rejected -------------------------------------------
    bidding.scratch_horse(5)
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
    _run("guest UI — static assets served", test_static_assets_served)
    _run("guest UI — auction_state_changed broadcast", test_auction_state_changed_broadcast)
    _run("guest UI — JS listens for auction_state_changed", test_guest_js_listens_for_state_changed)

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

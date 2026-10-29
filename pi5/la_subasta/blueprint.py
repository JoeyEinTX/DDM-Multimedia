# la_subasta/blueprint.py - Flask routes for La Subasta
#
# URL prefix: /la-subasta (hyphen, per spec § Naming & Branding Conventions)
# The guest page (/) and the admin page (/admin) are templates; the spectator
# view is still a placeholder; everything under /api is JSON. The admin page
# and every /api/admin route are the host's, behind a PIN (below).

import hashlib
import hmac
import logging
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from la_subasta import bidding, field, notifications, payouts, scratches, settings
from la_subasta.bidding import BidError, UnknownBidder
from la_subasta.config import EMOJI_PALETTE, EVENT_YEAR
from la_subasta.models import init_db
from la_subasta.payouts import PayoutError
from la_subasta.settings import SettingsError
from la_subasta.state_machine import (
    AuctionState, can_transition, get_state, get_state_row, transition,
)

logger = logging.getLogger(__name__)

la_subasta_bp = Blueprint(
    "la_subasta", __name__,
    url_prefix="/la-subasta",
    template_folder="templates",
    static_folder="static",
    # static_url_path is relative to url_prefix — giving "/la-subasta/static"
    static_url_path="/static",
)


# -----------------------------------------------------------------------------
# The host's PIN
# -----------------------------------------------------------------------------
#
# The admin page and every /api/admin route open, lock and settle the auction and
# mark who paid, and the guests' phones are on the same network. They take the
# PIN in pi5/.env (DDM_LS_ADMIN_PIN, handed to the app as config LS_ADMIN_PIN;
# Joey, 2026-10-28, review S1). The page asks for it once: the right PIN sets a
# cookie that holds a keyed hash of it, so the iPad is not asked again for 30
# days, a restart of pi5 does not log it out and a changed PIN does. With no PIN
# set the host pages are off, never open.

ADMIN_COOKIE = "ls_host"
ADMIN_COOKIE_MAX_AGE = 30 * 24 * 3600
HOST_PAGE = "/la-subasta/admin"
HOST_LOGIN = HOST_PAGE + "/login"
HOST_API = "/la-subasta/api/admin/"
SQLITE_INT_MAX = 2 ** 63 - 1


def pin_cookie(pin: str) -> str:
    return hmac.new(pin.encode("utf-8"), b"ddm la subasta host", hashlib.sha256).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _no_pin():
    if request.url_rule.rule == HOST_PAGE:
        return render_template("admin_pin.html", pin_unset=True), 503
    return _err("No host PIN is set: put DDM_LS_ADMIN_PIN=... in pi5/.env and restart pi5", 503)


@la_subasta_bp.before_request
def _the_host_pages_need_the_pin():
    rule = request.url_rule.rule if request.url_rule else ""
    if rule == HOST_LOGIN or not (rule == HOST_PAGE or rule.startswith(HOST_API)):
        return None
    pin = current_app.config.get("LS_ADMIN_PIN") or ""
    if not pin:
        return _no_pin()
    if _same(request.cookies.get(ADMIN_COOKIE, ""), pin_cookie(pin)):
        return None
    if rule == HOST_PAGE:
        return render_template("admin_pin.html", pin_unset=False), 401
    return _err("The host's PIN is needed: open the admin page and enter it", 401)


@la_subasta_bp.route("/admin/login", methods=["POST"])
def admin_login():
    """Body JSON: {"pin"}. 200 and the cookie for the right PIN; 401 "Wrong
    PIN" for anything else, the same line whatever was sent (it says nothing
    about how close it was, and there is no lockout: the PIN is behind the
    house's Wi-Fi); 503 with no PIN set."""
    pin = current_app.config.get("LS_ADMIN_PIN") or ""
    if not pin:
        return _no_pin()
    data = request.get_json(silent=True)
    given = data.get("pin") if isinstance(data, dict) else None
    if not isinstance(given, str) or not _same(given, pin):
        return _err("Wrong PIN", 401)
    response = jsonify({"success": True})
    response.set_cookie(ADMIN_COOKIE, pin_cookie(pin), max_age=ADMIN_COOKIE_MAX_AGE,
                        httponly=True, samesite="Strict", path=la_subasta_bp.url_prefix)
    return response


# -----------------------------------------------------------------------------
# Cache control
# -----------------------------------------------------------------------------

@la_subasta_bp.after_request
def _no_store_api_responses(response):
    """Stop browsers/proxies from caching dynamic API responses so a client
    can never render stale auction state (e.g. a scratched horse) served from
    cache. Scoped to /la-subasta/api/* only — static assets (CSS/JS) and the
    guest HTML page are left to cache normally."""
    if request.path.startswith("/la-subasta/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"          # HTTP/1.0 proxies
        response.headers["Expires"] = "0"
    return response


@la_subasta_bp.before_request
def _catch_up_with_la_quiniela():
    """Apply La Quiniela's field if names_rev has moved since the last sync
    (normally the store's listener already has; this catches one that
    failed). Cheap when nothing changed; never fails the request."""
    if (request.endpoint or "").endswith(".static"):
        return None
    try:
        scratches.sync()
    except Exception:
        logger.exception("La Subasta: catching up with La Quiniela's field failed")
    return None


# -----------------------------------------------------------------------------
# Init — called from main.py at app startup
# -----------------------------------------------------------------------------

def init_la_subasta(socketio=None) -> None:
    """
    Wire up DB + SocketIO. Safe to call multiple times.

    Args:
        socketio: Shared Flask-SocketIO instance for broadcasts.

    La Subasta doesn't own horse data: its horses, their names, program
    numbers and scratches are La Quiniela's (field.py, scratches.py). When
    the La Quiniela board already exists La Subasta starts following its
    store here; main.py makes the board afterwards and calls
    follow_la_quiniela() itself.
    """
    init_db()
    notifications.init_notifications(socketio)
    scratches.follow_la_quiniela()
    logger.info("La Subasta initialised (DB ready, socketio=%s)",
                "yes" if socketio else "no")


# -----------------------------------------------------------------------------
# Error helpers
# -----------------------------------------------------------------------------

def _err(reason: str, status: int = 400):
    return jsonify({"success": False, "error": reason}), status


def _bid_err(exc: BidError, status: int = 400):
    return jsonify({"success": False, "error": exc.reason}), status


NOT_YOUR_GUEST_NUMBER = "This phone is not that guest's: join again"


def _int_arg(data: dict, key: str):
    """data[key] as an int the database can hold, or None: no number, NaN,
    Infinity, or one too big for sqlite (a 400 for the route, never a 500)."""
    try:
        number = int(data.get(key))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if abs(number) <= SQLITE_INT_MAX else None


def _not_this_guest(bidder_id: int, data: dict):
    """None when the body's "secret" is the one bidder_id was given at join;
    else the refusal. A guest number nobody has is a 404, which sends the guest
    page to the join screen (a database wiped since the page opened), and a
    secret that is not theirs a 401 (the page goes there too, guest.js)."""
    if bidding.get_bidder(bidder_id) is None:
        return _bid_err(UnknownBidder(), status=404)
    if not bidding.holds_secret(bidder_id, data.get("secret")):
        return _err(NOT_YOUR_GUEST_NUMBER, status=401)
    return None


# -----------------------------------------------------------------------------
# Page routes
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/", methods=["GET"])
def guest_view():
    """Render the mobile-first guest page. Identity lives in localStorage —
    the first-visit modal collects it; a return visit asks /api/check-identity
    first and goes straight to the horse list when this year's auction knows
    it, else back to the join screen."""
    return render_template(
        "guest.html",
        emoji_palette=EMOJI_PALETTE,
        num_horses=len(field.current()),
        event_year=EVENT_YEAR,
    )


@la_subasta_bp.route("/admin", methods=["GET"])
def admin_view():
    """The admin page: the auction's state and its controls (with the lock's
    unsold-horse warning), the bidders (paid, and the cap exemption) and the
    payout ledger, derived from SET WINNERS' result (the picker for an
    unowned slot, the paid-out marks). A small page over the /api/admin
    routes; the fuller iPad view of the spec (Phase 3) is still to come. The
    PIN form until the host has entered the PIN (above)."""
    return render_template("admin.html")


@la_subasta_bp.route("/spectator", methods=["GET"])
def spectator_view():
    return jsonify({"view": "spectator", "status": "Phase 4 UI pending"})


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/state", methods=["GET"])
def api_state():
    """The auction's state, and the race's result as SET WINNERS recorded it
    (`results`: the three horses, or null) with `settled`: the auction is
    locked and a result is in, so the ledger pays."""
    row = get_state_row()
    result = payouts.results_now()
    return jsonify({
        "success": True,
        "state": row["state"],
        "results": {k: result[k] for k in payouts.FINISHES} if result is not None else None,
        "settled": row["state"] == AuctionState.LOCKED.value and result is not None,
        "opens_at": row["opens_at"],
        "closes_at": row["closes_at"],
        "total_pot": bidding.total_pot(),
        "updated_at": row["updated_at"],
        "event_year": EVENT_YEAR,
        "emoji_palette": EMOJI_PALETTE,
        "num_horses": len(field.current()),
        "num_bidders": bidding.count_bidders(),
        "num_bids": bidding.count_bids(),
    })


# -----------------------------------------------------------------------------
# Horses
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/horses", methods=["GET"])
def api_horses():
    """The field as La Quiniela has it, by program number: a replacement
    under its own number (22, not 9), names upper-cased, HORSE n without
    one, each with its current high bid."""
    horses = []
    for meta in field.current().horses:
        meta = dict(meta)
        horse_id = meta["horse_id"]
        meta["jockey"] = None           # La Quiniela keeps no jockeys
        hb = bidding.current_high_bid(horse_id)
        meta["scratched"] = False       # a scratched horse is not listed at all
        if hb:
            meta["current_high_bid"] = {
                "amount": hb["amount"],
                "bidder_id": hb["bidder_id"],
                "bidder_identity": hb["identity"],
                "bid_time": hb["bid_time"],
            }
            # Convenience fields the guest UI renders directly.
            meta["current_leader_identity"] = hb["identity"]
            meta["current_leader_bidder_id"] = hb["bidder_id"]
        else:
            meta["current_high_bid"] = None
            meta["current_leader_identity"] = None
            meta["current_leader_bidder_id"] = None
        horses.append(meta)
    return jsonify({"success": True, "horses": horses})


# -----------------------------------------------------------------------------
# Bidders / registration
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    try:
        bidder = bidding.register_bidder(
            name=data.get("name", ""),
            emoji=data.get("emoji", ""),
        )
    except BidError as exc:
        return _bid_err(exc, status=409)
    secret = bidder.pop("secret")
    return jsonify({"success": True, "bidder": bidder, "secret": secret})


@la_subasta_bp.route("/api/check-identity", methods=["POST"])
def api_check_identity():
    """Body JSON: {"name", "emoji"}, and the phone's "bidder_id" and "secret"
    when it has them. available: no bidder this event year has that name and
    emoji (what register would accept). known, when a bidder_id is sent: it is
    this year's bidder with that name and emoji, and the secret is theirs. The
    guest page asks at boot; a phone that is not known joins again."""
    data = request.get_json(silent=True) or {}
    name, emoji = data.get("name", ""), data.get("emoji", "")
    out = {"success": True, "available": bidding.identity_available(name, emoji)}
    if "bidder_id" in data:
        bidder_id = _int_arg(data, "bidder_id")
        out["known"] = bidder_id is not None and bidding.identity_known(bidder_id, name, emoji, data.get("secret"))
    return jsonify(out)


@la_subasta_bp.route("/api/bidders", methods=["GET"])
def api_bidders():
    bidders = bidding.list_bidders()
    for b in bidders:
        b["portfolio"] = bidding.bidder_portfolio(b["id"])
        # The ledger: what the bidder owes now, and what a bidder already
        # marked paid gets back for a horse scratched after the lock.
        b.update(bidding.ledger(b, b["portfolio"]))
    return jsonify({"success": True, "bidders": bidders})


@la_subasta_bp.route(f"/api/bidders/<int(max={SQLITE_INT_MAX}):bidder_id>/portfolio", methods=["GET"])
def api_bidder_portfolio(bidder_id: int):
    if bidding.get_bidder(bidder_id) is None:
        return _bid_err(UnknownBidder(), status=404)
    portfolio = bidding.bidder_portfolio(bidder_id)
    return jsonify({"success": True, "portfolio": portfolio})


# -----------------------------------------------------------------------------
# Bid placement + undo
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/bid", methods=["POST"])
def api_bid():
    data = request.get_json(silent=True) or {}
    bidder_id, horse_id = _int_arg(data, "bidder_id"), _int_arg(data, "horse_id")
    if bidder_id is None or horse_id is None:
        return _err("bidder_id and horse_id must be integers")
    refused = _not_this_guest(bidder_id, data)
    if refused:
        return refused

    try:
        placed = bidding.place_bid(bidder_id, horse_id, data.get("amount"))
    except BidError as exc:
        return _bid_err(exc)

    bidder = bidding.get_bidder(placed.bidder_id)
    payload = {
        "bid_id": placed.bid_id,
        "horse_id": placed.horse_id,
        "bidder_id": placed.bidder_id,
        "bidder_identity": bidder["identity"] if bidder else None,
        "amount": placed.amount,
        "bid_time": placed.bid_time,
        "previous_bidder_id": placed.previous_bidder_id,
        "previous_bidder_identity": placed.previous_bidder_identity,
    }

    notifications.bid_placed(
        horse_id=placed.horse_id,
        bidder_id=placed.bidder_id,
        bidder_identity=payload["bidder_identity"],
        amount=placed.amount,
        previous_bidder_id=placed.previous_bidder_id,
        previous_bidder_identity=placed.previous_bidder_identity,
    )
    if placed.previous_bidder_id is not None:
        notifications.outbid(
            horse_id=placed.horse_id,
            old_bidder_id=placed.previous_bidder_id,
            old_bidder_identity=placed.previous_bidder_identity or "",
            new_bidder_id=placed.bidder_id,
            new_bidder_identity=payload["bidder_identity"] or "",
            amount=placed.amount,
        )

    return jsonify({"success": True, "bid": payload,
                    "total_pot": bidding.total_pot()})


@la_subasta_bp.route("/api/bid/undo", methods=["POST"])
def api_bid_undo():
    data = request.get_json(silent=True) or {}
    bid_id, bidder_id = _int_arg(data, "bid_id"), _int_arg(data, "bidder_id")
    if bid_id is None or bidder_id is None:
        return _err("bid_id and bidder_id must be integers")
    refused = _not_this_guest(bidder_id, data)
    if refused:
        return refused

    try:
        result = bidding.undo_bid(bid_id, bidder_id)
    except BidError as exc:
        return _bid_err(exc)

    notifications.bid_voided(result["voided_bid_id"], result["horse_id"],
                             result["new_high_bid"])
    return jsonify({"success": True, **result,
                    "total_pot": bidding.total_pot()})


# -----------------------------------------------------------------------------
# Admin endpoints
# -----------------------------------------------------------------------------

def _transition_with_broadcast(new_state: AuctionState):
    """Transition and emit auction_state_changed. Returns (ok, old_state_value)."""
    old_state = get_state().value
    transition(new_state)
    notifications.auction_state_changed(new_state.value, old_state)
    return old_state


@la_subasta_bp.route("/api/admin/start", methods=["POST"])
def api_admin_start():
    try:
        _transition_with_broadcast(AuctionState.OPEN)
    except ValueError as exc:
        return _err(str(exc), status=409)
    return jsonify({"success": True, "state": get_state().value})


@la_subasta_bp.route("/api/admin/final-hour", methods=["POST"])
def api_admin_final_hour():
    try:
        _transition_with_broadcast(AuctionState.FINAL_HOUR)
    except ValueError as exc:
        return _err(str(exc), status=409)
    return jsonify({"success": True, "state": get_state().value})


@la_subasta_bp.route("/api/admin/lock", methods=["POST"])
def api_admin_lock():
    """Lock bidding and freeze ownership.

    There is no House to take a horse nobody bought, so before locking this
    looks for horses in the field with no bid. If there are any it answers
    409 {"unsold": [program numbers]} and does nothing, unless the body says
    {"confirm": true}: buy them first (in practice the host does, at the
    minimum bid), or lock anyway and accept that one finishing in the money
    leaves its payout slot for the admin to name (payouts.py). A lock the
    auction cannot make from where it is (not OPEN or FINAL_HOUR) is refused
    as before, without the warning.
    """
    data = request.get_json(silent=True) or {}
    if can_transition(AuctionState.LOCKED):
        unsold = bidding.unsold_horses()
        if unsold and data.get("confirm") is not True:
            return jsonify({
                "success": False,
                "error": "No bid on " + ", ".join(f"#{n}" for n in unsold)
                         + ": buy them before locking, or confirm to lock anyway",
                "unsold": unsold,
            }), 409
    try:
        _transition_with_broadcast(AuctionState.LOCKED)
    except ValueError as exc:
        return _err(str(exc), status=409)
    payouts.freeze_ownership()
    notifications.auction_locked(time.time())
    return jsonify({"success": True, "state": get_state().value})


@la_subasta_bp.route("/api/admin/void", methods=["POST"])
def api_admin_void():
    data = request.get_json(silent=True) or {}
    bid_id = _int_arg(data, "bid_id")
    if bid_id is None:
        return _err("bid_id must be an integer")
    try:
        result = bidding.void_bid(bid_id, data.get("reason", ""))
    except BidError as exc:
        return _bid_err(exc)
    notifications.bid_voided(result["voided_bid_id"], result["horse_id"],
                             result["new_high_bid"])
    return jsonify({"success": True, **result})


@la_subasta_bp.route("/api/admin/paid", methods=["POST"])
def api_admin_paid():
    data = request.get_json(silent=True) or {}
    bidder_id = _int_arg(data, "bidder_id")
    if bidder_id is None:
        return _err("bidder_id must be an integer")

    bidder = bidding.get_bidder(bidder_id)
    if bidder is None:
        return _err("Unknown bidder", status=404)

    from la_subasta.models import write_txn
    portfolio = bidding.bidder_portfolio(bidder_id)
    # Idempotent: paid_amount is what was collected, and ledger() works the
    # refund out of it (a horse scratched after the lock leaves the bidder
    # owing less than they paid). A second tap must not lower it to the new,
    # smaller total owed and lose that refund, so it only ever goes up, and
    # the first paid_at stands.
    with write_txn() as conn:
        conn.execute(
            "UPDATE bidders SET paid = 1, paid_at = COALESCE(paid_at, datetime('now')), "
            "paid_amount = MAX(COALESCE(paid_amount, 0), ?) WHERE id = ?",
            (portfolio["total"], bidder_id),
        )
        paid_amount = conn.execute(
            "SELECT paid_amount FROM bidders WHERE id = ?", (bidder_id,),
        ).fetchone()["paid_amount"]
    notifications.paid_marked(bidder_id)
    return jsonify({"success": True, "bidder_id": bidder_id,
                    "amount": paid_amount})


@la_subasta_bp.route("/api/admin/payouts", methods=["GET"])
def api_admin_payouts():
    """The payout ledger, derived from SET WINNERS' result and the ownership
    as they stand (payouts.ledger()): `results`, `total_pot`, `amounts`, a
    row per paying slot once there is a result (each with `unowned` while
    nobody is to be paid it and `paid`, the paid-out mark, while it matches),
    `unowned`, and `paid_on_old_result`, the marks that no longer match."""
    return jsonify({"success": True, **payouts.ledger()})


@la_subasta_bp.route("/api/admin/payout-slot", methods=["POST"])
def api_admin_payout_slot():
    """Name the horse that pays a slot: the safety net for a paying horse with
    no owner (La Quiniela's rule for an empty cup: the next finisher takes the
    place). Body JSON: {"finish": "win" | "place" | "show", "horse_id": 4},
    a horse in the field that has an owner. 409 before a result is in, for a
    slot whose finisher has an owner (it pays them; naming the finisher itself
    is always allowed and puts the slot back as the result has it), or if
    that horse has no owner either. Replies with the slot's row and the
    ledger."""
    data = request.get_json(silent=True) or {}
    horse_id = _int_arg(data, "horse_id")
    if horse_id is None:
        return _err("horse_id must be an integer")
    try:
        slot = payouts.set_slot_horse(data.get("finish"), horse_id)
    except PayoutError as exc:
        return _err(exc.reason, status=exc.status)
    return jsonify({"success": True, "slot": slot, **payouts.ledger()})


@la_subasta_bp.route("/api/admin/payout-paid", methods=["POST"])
def api_admin_payout_paid():
    """The paid-out mark. Body JSON: {"finish": "win" | "place" | "show",
    "paid": true} marks the slot's finisher (as the result names it now) paid
    out to the ledger's bidder for the ledger's amount; {"paid": false}
    clears the mark on that finisher, or on the horse given ("horse_id": a
    mark paid on an earlier result names its horse). 409 with no result, or
    while nobody is to be paid the slot. Replies with the ledger."""
    data = request.get_json(silent=True) or {}
    horse_id = data.get("horse_id")
    if horse_id is not None and (isinstance(horse_id, bool) or not isinstance(horse_id, int)):
        return _err("horse_id must be an integer")
    try:
        ledger = payouts.mark_paid(data.get("finish"), data.get("paid"), horse_id)
    except PayoutError as exc:
        return _err(exc.reason, status=exc.status)
    return jsonify({"success": True, **ledger})


@la_subasta_bp.route("/api/admin/cap-exempt", methods=["POST"])
def api_admin_cap_exempt():
    """Mark a bidder exempt from the max-horses cap, or take the mark off
    (the host, who buys the horses nobody else bid on). Body JSON:
    {"bidder_id": 3, "exempt": true}. The cap still applies to everyone else."""
    data = request.get_json(silent=True) or {}
    bidder_id = _int_arg(data, "bidder_id")
    if bidder_id is None:
        return _err("bidder_id must be an integer")
    exempt = data.get("exempt")
    if not isinstance(exempt, bool):
        return _err("exempt must be true or false")
    try:
        bidder = bidding.set_cap_exempt(bidder_id, exempt)
    except BidError as exc:
        return _bid_err(exc, status=404)
    return jsonify({"success": True, "bidder_id": bidder_id,
                    "cap_exempt": bool(bidder["cap_exempt"])})


# -----------------------------------------------------------------------------
# Admin-tunable settings (Phase 1.5)
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/settings", methods=["GET"])
def api_settings():
    """The settings as they stand (value, default, overridden or not) and the
    auction's state. Open to everyone: the guest page reads the bid limits from
    it. Setting them is the host's (POST /api/admin/settings)."""
    return jsonify({
        "success": True,
        "settings": settings.get_all_settings(),
        "state": get_state().value,
    })


@la_subasta_bp.route("/api/admin/settings", methods=["POST"])
def api_admin_settings_set():
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if not key:
        return _err("key is required")

    try:
        result = settings.set_setting(
            key, data.get("value"),
            changed_by=data.get("changed_by", "admin"),
        )
    except SettingsError as exc:
        # Lock-when-open rejection returns 409; validation errors return 400.
        status = 409 if "Cannot change" in exc.reason else 400
        return jsonify({"success": False, "error": exc.reason}), status

    notifications.settings_changed(
        key=result["key"],
        old_value=result["old_value"],
        new_value=result["new_value"],
        changed_at=result["changed_at"],
    )
    return jsonify({"success": True, "change": result})


@la_subasta_bp.route("/api/admin/settings/reset", methods=["POST"])
def api_admin_settings_reset():
    data = request.get_json(silent=True) or {}
    before = settings.get_all_settings()
    try:
        count = settings.reset_to_defaults(
            changed_by=data.get("changed_by", "admin"),
        )
    except SettingsError as exc:
        return jsonify({"success": False, "error": exc.reason}), 409
    # Broadcast one settings_changed per setting that actually changed, so
    # every connected client can refresh its cache.
    for key, info in before.items():
        if info["is_override"]:
            notifications.settings_changed(
                key=key, old_value=info["value"],
                new_value=info["default"], changed_at=None,
            )
    return jsonify({"success": True, "reset_count": count})


@la_subasta_bp.route("/api/admin/settings/audit", methods=["GET"])
def api_admin_settings_audit():
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({
        "success": True,
        "audit": settings.get_audit_log(limit=limit),
    })

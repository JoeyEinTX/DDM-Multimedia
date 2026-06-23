# la_subasta/blueprint.py - Flask routes for La Subasta
#
# URL prefix: /la-subasta (hyphen, per spec § Naming & Branding Conventions)
# All routes return JSON for Phase 1 — templates/UI land in Phase 2.

import logging
import time

from flask import Blueprint, jsonify, render_template, request

from la_subasta import bidding, notifications, payouts, reset, settings
from la_subasta.bidding import BidError
from la_subasta.config import EMOJI_PALETTE, EVENT_YEAR, NUM_HORSES
from la_subasta.models import init_db
from la_subasta.settings import SettingsError
from la_subasta.state_machine import (
    AuctionState, get_state, get_state_row, transition,
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


# -----------------------------------------------------------------------------
# Init — called from main.py at app startup
# -----------------------------------------------------------------------------

def init_la_subasta(socketio=None, racing_service=None) -> None:
    """
    Wire up DB + SocketIO. Safe to call multiple times.

    Args:
        socketio: Shared Flask-SocketIO instance for broadcasts.
        racing_service: Optional — the dashboard's RacingDataService, used
            to look up horse metadata (name, saddle cloth, jockey) by post
            position. La Subasta doesn't own horse data.
    """
    init_db()
    notifications.init_notifications(socketio)
    _set_racing_service(racing_service)
    logger.info("La Subasta initialised (DB ready, socketio=%s)",
                "yes" if socketio else "no")


_racing_service = None


def _set_racing_service(svc) -> None:
    global _racing_service
    _racing_service = svc


def _horse_meta(horse_id: int) -> dict:
    """Return minimal horse metadata for JSON responses (graceful if no dashboard)."""
    if _racing_service is not None:
        horse = _racing_service.get_horse(horse_id)
        if horse:
            return {
                "horse_id": horse_id,
                "saddle_cloth": horse_id,       # 1..20 — same as post position
                "name": horse.get("horse_name"),
                "jockey": horse.get("jockey"),
                "saddle_cloth_color": horse.get("saddle_cloth_color"),
            }
    # No dashboard service (headless tests). Return a stable shape so the
    # guest UI can still render a placeholder card.
    return {
        "horse_id": horse_id,
        "saddle_cloth": horse_id,
        "name": None,
        "jockey": None,
        "saddle_cloth_color": None,
    }


# -----------------------------------------------------------------------------
# Error helpers
# -----------------------------------------------------------------------------

def _err(reason: str, status: int = 400):
    return jsonify({"success": False, "error": reason}), status


def _bid_err(exc: BidError, status: int = 400):
    return jsonify({"success": False, "error": exc.reason}), status


# -----------------------------------------------------------------------------
# Page routes — return JSON placeholders until Phase 2 templates land
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/", methods=["GET"])
def guest_view():
    """Render the mobile-first guest page. Identity lives in localStorage —
    the first-visit modal collects it; return visits skip straight to the
    horse list."""
    return render_template(
        "guest.html",
        emoji_palette=EMOJI_PALETTE,
        num_horses=NUM_HORSES,
    )


@la_subasta_bp.route("/admin", methods=["GET"])
def admin_view():
    return jsonify({"view": "admin", "status": "Phase 3 UI pending"})


@la_subasta_bp.route("/spectator", methods=["GET"])
def spectator_view():
    return jsonify({"view": "spectator", "status": "Phase 4 UI pending"})


# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/state", methods=["GET"])
def api_state():
    row = get_state_row()
    return jsonify({
        "success": True,
        "state": row["state"],
        "opens_at": row["opens_at"],
        "closes_at": row["closes_at"],
        "total_pot": bidding.total_pot(),
        "updated_at": row["updated_at"],
        "event_year": EVENT_YEAR,
        "emoji_palette": EMOJI_PALETTE,
        "num_horses": NUM_HORSES,
        "num_bidders": bidding.count_bidders(),
        "num_bids": bidding.count_bids(),
    })


# -----------------------------------------------------------------------------
# Horses
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/horses", methods=["GET"])
def api_horses():
    horses = []
    for horse_id in range(1, NUM_HORSES + 1):
        meta = _horse_meta(horse_id)
        hb = bidding.current_high_bid(horse_id)
        meta["scratched"] = bidding.is_horse_scratched(horse_id)
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
    return jsonify({"success": True, "bidder": bidder})


@la_subasta_bp.route("/api/check-identity", methods=["POST"])
def api_check_identity():
    data = request.get_json(silent=True) or {}
    available = bidding.identity_available(
        data.get("name", ""), data.get("emoji", ""),
    )
    return jsonify({"success": True, "available": available})


@la_subasta_bp.route("/api/bidders", methods=["GET"])
def api_bidders():
    include_house = request.args.get("include_house", "").lower() in ("1", "true", "yes")
    bidders = bidding.list_bidders(include_house=include_house)
    for b in bidders:
        b["portfolio"] = bidding.bidder_portfolio(b["id"])
    return jsonify({"success": True, "bidders": bidders})


@la_subasta_bp.route("/api/bidders/<int:bidder_id>/portfolio", methods=["GET"])
def api_bidder_portfolio(bidder_id: int):
    portfolio = bidding.bidder_portfolio(bidder_id)
    return jsonify({"success": True, "portfolio": portfolio})


# -----------------------------------------------------------------------------
# Bid placement + undo
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/bid", methods=["POST"])
def api_bid():
    data = request.get_json(silent=True) or {}
    try:
        bidder_id = int(data.get("bidder_id"))
        horse_id = int(data.get("horse_id"))
    except (TypeError, ValueError):
        return _err("bidder_id and horse_id must be integers")

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
    try:
        bid_id = int(data.get("bid_id"))
        bidder_id = int(data.get("bidder_id"))
    except (TypeError, ValueError):
        return _err("bid_id and bidder_id must be integers")

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


@la_subasta_bp.route("/api/admin/transition", methods=["POST"])
def api_admin_transition():
    """Dev/admin: force the auction state machine to an arbitrary state.

    Body JSON: {"state": "OPEN" | "FINAL_HOUR" | "LOCKED" | "NOT_STARTED" | ...}

    Uses force=True so it can jump in any direction — this is the dev panel's
    "Force FINAL_HOUR" convenience, not the normal lifecycle path. Lock-only
    side effects (ownership freeze, payout computation) still run only through
    their dedicated /api/admin/lock and /api/admin/results endpoints; this
    endpoint changes the state row and broadcasts, nothing more.
    """
    data = request.get_json(silent=True) or {}
    target = data.get("state")
    try:
        new_state = AuctionState(target)
    except ValueError:
        valid = ", ".join(s.value for s in AuctionState)
        return _err(f"Invalid state {target!r}. Must be one of: {valid}")

    old_state = get_state().value
    transition(new_state, force=True)
    notifications.auction_state_changed(new_state.value, old_state)
    return jsonify({"success": True, "state": get_state().value,
                    "previous_state": old_state})


@la_subasta_bp.route("/api/admin/lock", methods=["POST"])
def api_admin_lock():
    try:
        _transition_with_broadcast(AuctionState.LOCKED)
    except ValueError as exc:
        return _err(str(exc), status=409)
    payouts.freeze_ownership()
    notifications.auction_locked(time.time())
    return jsonify({"success": True, "state": get_state().value})


@la_subasta_bp.route("/api/admin/results", methods=["POST"])
def api_admin_results():
    """
    Enter race results, compute payouts.

    Body JSON: {"win": 7, "place": 3, "show": 12}  (horse_id / post position)
    """
    data = request.get_json(silent=True) or {}
    try:
        win = int(data.get("win"))
        place = int(data.get("place"))
        show = int(data.get("show"))
    except (TypeError, ValueError):
        return _err("win, place, show must be integers")

    if len({win, place, show}) != 3:
        return _err("win, place, show must all differ")
    for pos in (win, place, show):
        if pos < 1 or pos > NUM_HORSES:
            return _err(f"Invalid horse id: {pos}")

    current = get_state()
    if current == AuctionState.LOCKED:
        _transition_with_broadcast(AuctionState.RACE_COMPLETE)
    elif current != AuctionState.RACE_COMPLETE:
        return _err(f"Cannot enter results from state {current.value}",
                    status=409)

    result = payouts.compute_and_persist_payouts(win, place, show)
    _transition_with_broadcast(AuctionState.SETTLED)

    notifications.results_entered(win, place, show)
    notifications.payout_computed(result)

    return jsonify({"success": True, **result, "state": get_state().value})


@la_subasta_bp.route("/api/admin/void", methods=["POST"])
def api_admin_void():
    data = request.get_json(silent=True) or {}
    try:
        bid_id = int(data.get("bid_id"))
    except (TypeError, ValueError):
        return _err("bid_id must be an integer")
    try:
        result = bidding.void_bid(bid_id, data.get("reason", ""))
    except BidError as exc:
        return _bid_err(exc)
    notifications.bid_voided(result["voided_bid_id"], result["horse_id"],
                             result["new_high_bid"])
    return jsonify({"success": True, **result})


@la_subasta_bp.route("/api/admin/scratch", methods=["POST"])
def api_admin_scratch():
    data = request.get_json(silent=True) or {}
    try:
        horse_id = int(data.get("horse_id"))
    except (TypeError, ValueError):
        return _err("horse_id must be an integer")
    if horse_id < 1 or horse_id > NUM_HORSES:
        return _err(f"Invalid horse_id: {horse_id}")
    bidding.scratch_horse(horse_id)
    notifications.horse_scratched(horse_id)
    return jsonify({"success": True, "horse_id": horse_id})


@la_subasta_bp.route("/api/admin/unscratch", methods=["POST"])
def api_admin_unscratch():
    """Clear a horse's scratched flag (dev/admin reversal of /scratch)."""
    data = request.get_json(silent=True) or {}
    try:
        horse_id = int(data.get("horse_id"))
    except (TypeError, ValueError):
        return _err("horse_id must be an integer")
    if horse_id < 1 or horse_id > NUM_HORSES:
        return _err(f"Invalid horse_id: {horse_id}")
    bidding.unscratch_horse(horse_id)
    return jsonify({"success": True, "horse_id": horse_id})


@la_subasta_bp.route("/api/admin/paid", methods=["POST"])
def api_admin_paid():
    data = request.get_json(silent=True) or {}
    try:
        bidder_id = int(data.get("bidder_id"))
    except (TypeError, ValueError):
        return _err("bidder_id must be an integer")

    bidder = bidding.get_bidder(bidder_id)
    if bidder is None:
        return _err("Unknown bidder", status=404)

    from la_subasta.models import write_txn
    portfolio = bidding.bidder_portfolio(bidder_id)
    with write_txn() as conn:
        conn.execute(
            "UPDATE bidders SET paid = 1, paid_at = datetime('now'), "
            "paid_amount = ? WHERE id = ?",
            (portfolio["total"], bidder_id),
        )
    notifications.paid_marked(bidder_id)
    return jsonify({"success": True, "bidder_id": bidder_id,
                    "amount": portfolio["total"]})


@la_subasta_bp.route("/api/admin/payouts", methods=["GET"])
def api_admin_payouts():
    return jsonify({"success": True, "payouts": payouts.list_payouts()})


# -----------------------------------------------------------------------------
# Admin-tunable settings (Phase 1.5)
# -----------------------------------------------------------------------------

@la_subasta_bp.route("/api/admin/settings", methods=["GET"])
def api_admin_settings_get():
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
    count = settings.reset_to_defaults(
        changed_by=data.get("changed_by", "admin"),
    )
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


# -----------------------------------------------------------------------------
# Admin reset (testing only — Phase 1.6)
# -----------------------------------------------------------------------------
#
# Three-scope wipe for clearing La Subasta state between test runs without
# touching SQL directly. Guarded by ?confirm=TESTING so a stray POST can't
# nuke real data. settings_audit_log and auction_overrides are preserved by
# design (test should not lose audit trail or admin tunables).
#
# TODO Phase 3: once the admin login/role system lands, wrap this endpoint
# with an `is_admin` check. For now the testing guard alone keeps the surface
# narrow; there is no production deployment to worry about yet.

_RESET_SCOPES = {"bids", "full", "state"}


@la_subasta_bp.route("/api/admin/reset", methods=["POST"])
def api_admin_reset():
    confirm = request.args.get("confirm", "")
    if confirm != "TESTING":
        return _err(
            "Missing or invalid confirm parameter (must be exactly 'TESTING')"
        )

    scope = request.args.get("scope", "")
    if scope not in _RESET_SCOPES:
        return _err(
            f"Invalid scope: {scope!r}. Must be one of: bids, full, state"
        )

    if scope == "bids":
        deleted = reset.reset_bids()
    elif scope == "full":
        deleted = reset.reset_full()
    else:  # scope == "state"
        deleted = reset.reset_state()

    notifications.auction_reset(scope=scope, timestamp=time.time())

    return jsonify({
        "success": True,
        "scope": scope,
        "deleted": deleted,
        "auction_state": AuctionState.NOT_STARTED.value,
    })

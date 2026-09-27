# la_quiniela/blueprint.py - HTTP routes and the SocketIO room for the bridge
#
# URL prefix: /api/lq. Init is called from main.py at startup, like
# la_subasta; the serial thread itself is started separately by
# start_la_quiniela() from main.py's __main__ block only, so importing the app
# never touches a serial port.

import logging
from typing import Any, Dict, Optional

from flask import Blueprint, abort, jsonify, request

from la_quiniela.bridge import LQ_ROOM, LqBridge, load_settings

logger = logging.getLogger(__name__)

la_quiniela_bp = Blueprint("la_quiniela", __name__, url_prefix="/api/lq")

_bridge: Optional[LqBridge] = None
_socketio = None


def init_la_quiniela(socketio=None, settings: Optional[Dict[str, Any]] = None,
                     db_path: Optional[str] = None, serial_factory=None,
                     bridge: Optional[LqBridge] = None) -> LqBridge:
    """Create the bridge (no thread yet) and register the SocketIO handler.

    Safe to call again: the previous bridge is stopped and replaced."""
    global _bridge, _socketio
    if _bridge is not None and _bridge is not bridge:
        try:
            _bridge.close()
        except Exception:
            pass
    _socketio = socketio
    if bridge is None:
        bridge = LqBridge(settings=load_settings(settings), db_path=db_path,
                          serial_factory=serial_factory, socketio=socketio)
    else:
        bridge.socketio = socketio
    _bridge = bridge
    if socketio is not None:
        socketio.on_event("lq_request_snapshot", _handle_request_snapshot)
    logger.info("La Quiniela bridge initialised (port=%r, enabled=%s)",
                bridge.settings.get("LQ_SERIAL_PORT"), bridge.settings.get("LQ_BRIDGE_ENABLED"))
    return bridge


def get_bridge() -> LqBridge:
    if _bridge is None:
        raise RuntimeError("La Quiniela bridge not initialised; call init_la_quiniela() first")
    return _bridge


def start_la_quiniela() -> bool:
    """Start the serial thread once. Called from main.py's __main__ only."""
    return get_bridge().start() if _bridge is not None else False


def stop_la_quiniela() -> None:
    if _bridge is not None:
        _bridge.stop()


# -----------------------------------------------------------------------------
# SocketIO: displays join the "lq" room and get a snapshot back
# -----------------------------------------------------------------------------

def _handle_request_snapshot(data=None):
    from flask_socketio import emit, join_room
    join_room(LQ_ROOM)
    emit("lq_snapshot", get_bridge().get_snapshot())


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

@la_quiniela_bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@la_quiniela_bp.route("/snapshot", methods=["GET"])
def api_snapshot():
    return jsonify(get_bridge().get_snapshot())


def _dev_only() -> None:
    if not get_bridge().settings.get("LQ_DEV_ENDPOINTS"):
        abort(404)


def _rev_or_400(fn):
    try:
        return jsonify({"success": True, "rev": fn()})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@la_quiniela_bp.route("/dev/state", methods=["POST"])
def api_dev_state():
    _dev_only()
    body = request.get_json(silent=True) or {}
    return _rev_or_400(lambda: get_bridge().set_state(
        body.get("phase"), body.get("horses"), body.get("scratched")))


@la_quiniela_bp.route("/dev/roster", methods=["POST"])
def api_dev_roster():
    _dev_only()
    body = request.get_json(silent=True) or {}
    return _rev_or_400(lambda: get_bridge().set_roster(body.get("macs")))


@la_quiniela_bp.route("/dev/roster/adopt", methods=["POST"])
def api_dev_roster_adopt():
    _dev_only()
    return _rev_or_400(lambda: get_bridge().adopt_roster())


@la_quiniela_bp.route("/dev/roster/clear", methods=["POST"])
def api_dev_roster_clear():
    """Forget the cups (bench): DevPi drops its roster and every cup's horse
    and flag and goes back to mirroring the gateway. Used to throw away what
    a simulator session left behind, or to start the adopt over. Names, the
    closing time and the scratch records belong to the board and stay (a
    scratch is about the horse). The betting itself is reset separately by
    POST /api/quiniela/reset, which keeps the roster.

    Nothing is sent to the gateway, on purpose. The protocol has no "forget"
    line, and an empty roster line would be worse than none: from its first
    roster line on the gateway hands out no cup number to a MAC that is not
    in its table (ddm_gateway.ino, handlePacket: "no slot, no ack, and the
    cup stays on its MAC screen until a roster line includes it"), so every
    cup would report -1, nothing would be left to mirror or adopt, and only a
    power-cycle of the gateway would bring the numbers back. Left alone, the
    gateway keeps its table, the cups keep their numbers, DevPi mirrors them
    again as they report and adopt_roster() copies them back."""
    _dev_only()
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "roster_clear")[:64]
    return jsonify({"success": True, **get_bridge().reset_link(reason)})


@la_quiniela_bp.route("/dev/reset", methods=["POST"])
def api_dev_reset():
    """Deprecated alias: both resets, the betting one first (PRE_RACE with
    the assignments, one state line, closing time and ticker cleared) and
    then the roster clear above (nothing sent), so the gateway hears the
    phase before DevPi forgets the cups. Kept so nothing that calls it
    breaks; new callers use POST /api/quiniela/reset and /dev/roster/clear.
    The reply is the roster clear's, plus "betting": the betting reset's
    reply (null when no board is bound to this bridge)."""
    _dev_only()
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "dev_reset")[:64]
    bridge = get_bridge()
    betting = None
    try:
        from la_quiniela.board import get_board
        board = get_board()
    except RuntimeError:
        board = None
    if board is not None and board.bridge is bridge:
        betting = board.reset_betting()
    return jsonify({"success": True, **bridge.reset_link(reason), "betting": betting})


@la_quiniela_bp.route("/dev/debug", methods=["POST"])
def api_dev_debug():
    _dev_only()
    body = request.get_json(silent=True) or {}
    on = body.get("on")
    if not isinstance(on, bool):
        return jsonify({"success": False, "error": "on must be true or false"}), 400
    sent = get_bridge().set_gateway_debug(on)
    return jsonify({"success": True, "sent": sent})

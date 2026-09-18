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


@la_quiniela_bp.route("/dev/debug", methods=["POST"])
def api_dev_debug():
    _dev_only()
    body = request.get_json(silent=True) or {}
    on = body.get("on")
    if not isinstance(on, bool):
        return jsonify({"success": False, "error": "on must be true or false"}), 400
    sent = get_bridge().set_gateway_debug(on)
    return jsonify({"success": True, "sent": sent})

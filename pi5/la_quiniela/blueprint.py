# la_quiniela/blueprint.py - HTTP routes and the SocketIO room for the bridge
#
# URL prefix: /api/lq. Init is called from main.py at startup, like
# la_subasta; the serial thread itself is started separately by
# start_la_quiniela() from main.py's __main__ block only, so importing the app
# never touches a serial port.
#
# Protocol v2: there is no roster, no adopt, no per-slot state and no dev
# flag. The snapshot is read-only, the gateway's debug text can be toggled,
# and the cup cache can be forgotten. Race state, scratches, renumbers and
# results are the betting board's (la_quiniela/board.py, /api/quiniela).

import logging
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

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
    global _bridge
    if _bridge is not None:
        _bridge.stop()


# -----------------------------------------------------------------------------
# SocketIO: a client joins the "lq" room and gets the current snapshot
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
    response.headers["Cache-Control"] = "no-store"
    return response


@la_quiniela_bp.route("/snapshot", methods=["GET"])
def api_snapshot():
    return jsonify(get_bridge().get_snapshot())


@la_quiniela_bp.route("/debug", methods=["POST"])
def api_debug():
    """{"on": true|false}: the gateway's human-readable serial output. A
    bench toggle; the JSON lines flow either way."""
    body = request.get_json(silent=True) or {}
    on = body.get("on")
    if not isinstance(on, bool):
        return jsonify({"success": False, "error": "on must be true or false"}), 400
    return jsonify({"success": True, "sent": get_bridge().set_gateway_debug(on)})


@la_quiniela_bp.route("/cups/forget", methods=["POST"])
def api_cups_forget():
    """Drop cups from the cache: body {"mac": "A0:..."} for one, {"prefix":
    "02:DD:4D:"} for a family, {} for all. The cache decides nothing (a cup
    that is still talking is back within a packet); this only stops a cup
    that went home from being listed as offline."""
    body = request.get_json(silent=True) or {}
    prefix = body.get("mac") or body.get("prefix") or None
    if prefix is not None and not isinstance(prefix, str):
        return jsonify({"success": False, "error": "mac / prefix must be a string"}), 400
    return jsonify({"success": True, "forgotten": get_bridge().forget_cups(prefix)})

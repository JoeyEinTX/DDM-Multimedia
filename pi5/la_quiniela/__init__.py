# la_quiniela package - DDM live betting system (Phase 1 POC).
#
# Spec: DDM_La_Quiniela_Spec.md
# - Raw WebSocket at /ws/la-quiniela talks to the La Quiniela ESP32.
# - Test API under /api/la-quiniela/ for status + manual horse assignment.

from flask import Blueprint, jsonify

from la_quiniela.bet_tracker import BetTracker
from la_quiniela.websocket_handler import (
    client_count,
    init_websocket,
    is_connected,
    send_display_command,
)

la_quiniela_bp = Blueprint("la_quiniela", __name__, url_prefix="/api/la-quiniela")

# Single shared tracker for the POC. Replace with DI when this grows past Phase 1.
_tracker = BetTracker()


def init_la_quiniela(app) -> None:
    """Wire the WebSocket route. Call once at app startup."""
    init_websocket(app, _tracker)


@la_quiniela_bp.route("/status", methods=["GET"])
def api_status():
    """ESP32 connection state + per-cup bet counts."""
    return jsonify({
        "connected": is_connected(),
        "client_count": client_count(),
        "total_bets": _tracker.total_bets(),
        "cups": _tracker.get_bets(),
    })


@la_quiniela_bp.route("/set-horse/<int:cup>/<string:number>", methods=["POST"])
def api_set_horse(cup: int, number: str):
    """Assign a horse number to a cup and push a display command to the ESP32.

    `number` is "1".."20" or "X" (scratch).
    """
    try:
        _tracker.set_horse(cup, number)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    horse = _tracker.get_horse(cup) or ""
    delivered = send_display_command(cup, horse)
    return jsonify({
        "success": True,
        "cup": cup,
        "horse": horse,
        "delivered_to_clients": delivered,
    })


__all__ = [
    "la_quiniela_bp",
    "init_la_quiniela",
]

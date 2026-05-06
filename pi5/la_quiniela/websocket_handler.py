# la_quiniela/websocket_handler.py - Raw WebSocket bridge to the La Quiniela ESP32.
#
# Uses flask-sock (pip install flask-sock) so the raw /ws/la-quiniela path
# coexists alongside the existing Flask-SocketIO endpoints. The ESP32 firmware
# (esp32/la_quiniela/la_quiniela_poc.ino) speaks raw WebSocket text frames.

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from flask_sock import Sock

logger = logging.getLogger("la_quiniela.ws")

sock = Sock()

# Active ESP32 client connections. In the POC there's one ESP32, but the set
# tolerates reconnects/duplicates without leaking state.
_clients: set = set()
_clients_lock = threading.Lock()

_bet_tracker = None  # set by init_websocket()


def init_websocket(app, bet_tracker) -> None:
    """Wire the /ws/la-quiniela route into the Flask app."""
    global _bet_tracker
    _bet_tracker = bet_tracker
    sock.init_app(app)

    @sock.route("/ws/la-quiniela")
    def la_quiniela_ws(ws):
        with _clients_lock:
            _clients.add(ws)
        logger.info("[la-quiniela WS] client connected (n=%d)", len(_clients))
        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                _handle_inbound(raw)
        except Exception as exc:  # connection drops surface here
            logger.info("[la-quiniela WS] client error: %s", exc)
        finally:
            with _clients_lock:
                _clients.discard(ws)
            logger.info("[la-quiniela WS] client disconnected (n=%d)", len(_clients))


def _handle_inbound(raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[la-quiniela WS] bad json: %r", raw)
        return

    if not isinstance(msg, dict):
        logger.warning("[la-quiniela WS] non-object payload: %r", msg)
        return

    if _bet_tracker is None:
        logger.warning("[la-quiniela WS] tracker not initialised, dropping %r", msg)
        return

    result = _bet_tracker.handle_weight_update(msg)
    # Verbose log so the POC bench is easy to debug from the console.
    logger.info("[la-quiniela WS] <- %s -> %s", msg, result)


def send_display_command(cup: int, show: str) -> int:
    """Push {"cup": N, "show": "<digit|X>"} to all connected ESP32 clients.

    Returns the number of clients the message was queued to.
    """
    payload = json.dumps({"cup": int(cup), "show": str(show)})
    with _clients_lock:
        targets = list(_clients)
    sent = 0
    for ws in targets:
        try:
            ws.send(payload)
            sent += 1
        except Exception as exc:
            logger.warning("[la-quiniela WS] send failed: %s", exc)
    logger.info("[la-quiniela WS] -> %s (delivered to %d)", payload, sent)
    return sent


def is_connected() -> bool:
    with _clients_lock:
        return len(_clients) > 0


def client_count() -> int:
    with _clients_lock:
        return len(_clients)

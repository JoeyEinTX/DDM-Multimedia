# la_quiniela/board.py - HTTP routes for the betting board
#
# Three routes at exactly the paths the splash display's board page expects
# (GET /api/quiniela, GET /api/quiniela/stream, POST /api/quiniela/cmd), so
# the splash can proxy them one to one. They are deliberately NOT under
# la_quiniela_bp: its after_request rewrites Cache-Control, and the stream's
# headers are part of the contract with the page.
#
# Same conventions as blueprint.py: a module-level init called from main.py,
# an accessor, and a start called from main.py's __main__ block only, so
# importing the app starts no thread.

import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

from la_quiniela import protocol as P
from la_quiniela.betting import BettingBoard, load_board_settings, sse_events, validate_cmd

logger = logging.getLogger(__name__)

quiniela_board_bp = Blueprint("quiniela_board", __name__)

_board: Optional[BettingBoard] = None

STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

DEMO_REFUSED = ("demo is not routed through pi5: the bridge speaks the JSON line protocol, "
                "and every state line turns demo off")
JSON_REFUSED = ("json is not routed through pi5: the bridge already reads the gateway's "
                "protocol, and the up state line would exceed its 1024-byte cap")
USAGE_STATE = "usage: state 0-6"
USAGE_HORSE = "usage: horse <cup 1-20> <horse 0-20>"
USAGE_SCRATCH = "usage: scratch <cup 1-20> <0|1>"


def init_board(bridge: Any = None, settings: Optional[Dict[str, Any]] = None,
               **board_kwargs: Any) -> BettingBoard:
    """Create the board (no thread yet) and hook it to the bridge. With no
    bridge given, the one init_la_quiniela() made is used; if there is none
    the board still answers, with link_ok false.

    Safe to call again: the previous board's thread is stopped and the board
    replaced. board_kwargs (clock, wall, log_dir) are for tests."""
    global _board
    if bridge is None:
        try:
            from la_quiniela.blueprint import get_bridge
            bridge = get_bridge()
        except RuntimeError:
            bridge = None
            logger.warning("La Quiniela board: no bridge initialised; the board will report "
                           "link_ok false until init_board() is called with one")
    if _board is not None:
        try:
            _board.stop()
        except Exception:
            pass
    board = BettingBoard(bridge=bridge, settings=load_board_settings(settings), **board_kwargs)
    if bridge is not None:
        bridge.add_listener(board.wake)
    _board = board
    logger.info("La Quiniela board initialised (token_value=%s, board_states=%s, log=%s)",
                board.settings.get("TOKEN_VALUE"), board.settings.get("QUINIELA_BOARD_STATES"),
                board.settings.get("QUINIELA_LOG"))
    return board


def get_board() -> BettingBoard:
    if _board is None:
        raise RuntimeError("La Quiniela board not initialised; call init_board() first")
    return _board


def start_board() -> bool:
    """Refresh once, synchronously, then start the refresh thread. Called
    from main.py's __main__ only. The thread runs even when the bridge has
    no port, so the routes answer with link_ok false rather than a stale
    picture."""
    if _board is None:
        return False
    try:
        _board.refresh()
    except Exception:
        logger.exception("La Quiniela board: first refresh failed")
    return _board.start()


def stop_board() -> None:
    if _board is not None:
        _board.stop()


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

@quiniela_board_bp.route("/api/quiniela", methods=["GET"])
def api_quiniela_model():
    resp = jsonify(get_board().model())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@quiniela_board_bp.route("/api/quiniela/stream", methods=["GET"])
def api_quiniela_stream():
    return Response(sse_events(get_board()), mimetype="text/event-stream",
                    headers=dict(STREAM_HEADERS))


def _bad(error: str, status: int = 400):
    return jsonify({"ok": False, "error": error}), status


def _int_arg(text: str, lo: int, hi: int) -> Optional[int]:
    try:
        value = int(text, 10)
    except (TypeError, ValueError):
        return None
    return value if lo <= value <= hi else None


def _parse(text: str) -> Tuple[str, Any]:
    """Split a validated command into (word, args). args is the parsed tuple,
    or an error message when the arguments are wrong."""
    parts = text.split()
    word, rest = parts[0], parts[1:]
    if word == "state":
        if len(rest) != 1:
            return word, USAGE_STATE
        phase = _int_arg(rest[0], int(P.Phase.PRE_RACE), int(P.Phase.AFTER_PARTY))
        return word, (USAGE_STATE if phase is None else (phase,))
    if word == "horse":
        if len(rest) != 2:
            return word, USAGE_HORSE
        cup = _int_arg(rest[0], 1, P.NUM_CUPS)
        horse = _int_arg(rest[1], 0, P.MAX_HORSE)
        return word, (USAGE_HORSE if cup is None or horse is None else (cup, horse))
    if word == "scratch":
        if len(rest) != 2:
            return word, USAGE_SCRATCH
        cup = _int_arg(rest[0], 1, P.NUM_CUPS)
        flag = _int_arg(rest[1], 0, 1)
        return word, (USAGE_SCRATCH if cup is None or flag is None else (cup, bool(flag)))
    return word, ()


def _state_lists(snap: Dict[str, Any]) -> Tuple[int, List[int], List[bool]]:
    """(phase, horses[20], scratched[20]) as set_state wants them, from a
    get_snapshot() dict: position 0 = cup 1, horse None -> 0."""
    cups = snap.get("cups") or []
    by_cup: Dict[int, Dict[str, Any]] = {}
    for entry in cups:
        if isinstance(entry, dict) and isinstance(entry.get("cup"), int):
            by_cup[entry["cup"]] = entry
    horses = [int(by_cup.get(cup, {}).get("horse") or 0) for cup in P.CUP_NUMBERS]
    scratched = [bool(by_cup.get(cup, {}).get("scratched")) for cup in P.CUP_NUMBERS]
    phase = int((snap.get("devpi") or {}).get("phase") or 0)
    return phase, horses, scratched


@quiniela_board_bp.route("/api/quiniela/cmd", methods=["POST"])
def api_quiniela_cmd():
    """Body {"cmd": "state 1"}. The first word must be one of the splash's
    whitelist; state / horse / scratch are translated onto the bridge's own
    set_state() (never a text line to the port: the bridge's revision model
    is the source of truth), roster answers from the snapshot, and demo /
    json have no equivalent here and are refused. Cup numbers are 1-based,
    as everywhere on pi5."""
    body = request.get_json(silent=True)
    cmd = body.get("cmd") if isinstance(body, dict) else None
    text, error = validate_cmd(cmd)
    if error:
        return _bad(error)
    word, args = _parse(text)
    if word == "demo":
        return _bad(DEMO_REFUSED)
    if word == "json":
        return _bad(JSON_REFUSED)
    if isinstance(args, str):
        return _bad(args)

    bridge = get_board().bridge
    if bridge is None:
        return _bad("bridge not initialised", 503)

    if word == "roster":
        snap = bridge.get_snapshot()
        return jsonify({
            "ok": True,
            "roster": [c.get("mac") for c in snap["cups"]],
            "roster_rev": int(snap["devpi"]["roster_rev"]),
            "has_roster": bool(snap["devpi"]["has_roster"]),
        })

    # A state change is applied even when the gateway is offline: DevPi is
    # the source of truth and re-sends on the next hello / status. The
    # response's gateway_online tells the caller which of the two happened.
    phase, horses, scratched = _state_lists(bridge.get_snapshot())
    extra: Dict[str, Any] = {}
    if word == "state":
        phase = args[0]
    elif word == "horse":
        cup, horse = args
        horses = [horse if c == cup else h for c, h in zip(P.CUP_NUMBERS, horses)]
        extra = {"cup": cup, "horse": horse}
    elif word == "scratch":
        cup, flag = args
        scratched = [flag if c == cup else s for c, s in zip(P.CUP_NUMBERS, scratched)]
        extra = {"cup": cup, "scratched": flag}
    try:
        rev = bridge.set_state(phase, horses, scratched)
    except ValueError as exc:
        return _bad(str(exc))
    online = bool(bridge.get_snapshot()["link"]["gateway_online"])
    return jsonify({"ok": True, "rev": rev, "phase": phase, "gateway_online": online, **extra})

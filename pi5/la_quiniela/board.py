# la_quiniela/board.py - HTTP routes for the betting board
#
# Three routes at exactly the paths the splash display's board page expects
# (GET /api/quiniela, GET /api/quiniela/stream, POST /api/quiniela/cmd), so
# the splash can proxy them one to one. They are deliberately NOT under
# la_quiniela_bp: its after_request rewrites Cache-Control, and the stream's
# headers are part of the contract with the page.
#
# Then the operator's side: horse names (1..24; 21..24 the also-eligibles),
# the two kinds of scratch, the closing time (GET/PUT /api/quiniela/horses,
# POST /api/quiniela/scratch and /unscratch, PUT /api/quiniela/closes_at) and
# the phone-sized admin page at GET /quiniela/admin that drives them. Race
# state stays on /api/quiniela/cmd.
#
# The renumber rule (Churchill's: an also-eligible that draws in keeps its
# own program number): a replacement scratch of horse 9 by 22 makes the cup
# that carried 9 carry 22 through the bridge's set_state(), the same path as
# the "horse <cup> <n>" command, and records {was: 9, now: 22} in the store.
# Nothing is moved on the mantle; the cup's tokens simply read under 22. The
# "horse <cup> <n>" command substitutes the record's now for a scratched n.
#
# Same conventions as blueprint.py: a module-level init called from main.py,
# an accessor, and a start called from main.py's __main__ block only, so
# importing the app starts no thread.

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from flask import Blueprint, Response, jsonify, render_template, request

from la_quiniela import protocol as P
from la_quiniela.betting import BettingBoard, load_board_settings, sse_events, validate_cmd
from la_quiniela.horses import FIELD_SIZE, NAME_MAX_LEN, in_field, parse_names_text

logger = logging.getLogger(__name__)

quiniela_board_bp = Blueprint("quiniela_board", __name__, template_folder="templates")

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
USAGE_HORSE = "usage: horse <cup 1-20> <horse 0-24>"
USAGE_SCRATCH = "usage: scratch <cup 1-20> <0|1>"
USAGE_CLOSES_AT = 'usage: {"at": <unix time>} | {"in_minutes": N} | {"at": null}'
REPLACEMENT_SHAPE = 'replacement must be {"number": N, "name": "..."}'


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
    as everywhere on pi5.

    "horse <cup> <n>" with an n that is the "was" of a replacement record
    assigns the record's "now" instead (9 is scratched, 22 stands in for it:
    "horse 1 9" puts 22 on cup 1) and says so in the reply's "note". An
    also-eligible (21-24) that is the "now" of no record is refused (400
    "23 is not in the field; scratch a horse with 23 as the replacement
    first"): a cup carries one only through a record, or its tokens would
    sit in the pot with no row on the board."""
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
        store = get_board().store
        active = store.active_number(horse) if horse else horse
        if active != horse:
            extra["note"] = f"{horse} is scratched; cup assigned {active}"
            horse = active
        if horse > FIELD_SIZE and store.replaced_by(horse) is None:
            return _bad(f"{horse} is not in the field; scratch a horse with {horse} as the replacement first")
        horses = [horse if c == cup else h for c, h in zip(P.CUP_NUMBERS, horses)]
        extra.update({"cup": cup, "horse": horse})
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


# -----------------------------------------------------------------------------
# Names, scratches, closing time, and the admin page
# -----------------------------------------------------------------------------

def _horses_as_typed() -> Dict[str, Dict[str, str]]:
    return {str(n): entry for n, entry in sorted(get_board().store.horses().items())}


def _cup_key(entry: Dict[str, Any]) -> int:
    return entry.get("cup") if isinstance(entry.get("cup"), int) else 99


def _cup_carrying(snap: Dict[str, Any], horse: int) -> Optional[Dict[str, Any]]:
    """The lowest-numbered cup entry whose horse is `horse`, or None."""
    for entry in sorted((c for c in snap.get("cups") or [] if isinstance(c, dict)), key=_cup_key):
        if entry.get("horse") == horse and isinstance(entry.get("cup"), int):
            return entry
    return None


def _field_view(board: BettingBoard) -> Tuple[Dict[str, Any], Dict[int, int], Set[int]]:
    """(snapshot, {horse: lowest cup carrying it}, horses whose cup carries
    the gateway's scratched flag). With no bridge the snapshot is empty."""
    bridge = board.bridge
    snap = bridge.get_snapshot() if bridge is not None else {"cups": []}
    on_cups: Dict[int, int] = {}
    scratched: Set[int] = set()
    for entry in sorted((c for c in snap.get("cups") or [] if isinstance(c, dict)), key=_cup_key):
        horse, cup = entry.get("horse"), entry.get("cup")
        if (isinstance(horse, int) and not isinstance(horse, bool) and 1 <= horse <= P.MAX_HORSE
                and isinstance(cup, int) and not isinstance(cup, bool)):
            if horse not in on_cups:
                on_cups[horse] = cup
                if entry.get("scratched"):
                    scratched.add(horse)
    return snap, on_cups, scratched


def _horse_arg(body: Any) -> Tuple[Optional[int], Optional[str]]:
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    value = body.get("horse")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None, f"horse must be a number 1-{P.MAX_HORSE}"
    horse = _int_arg(str(value), 1, P.MAX_HORSE)
    if horse is None:
        return None, f"horse must be a number 1-{P.MAX_HORSE}"
    return horse, None


def _replacement_arg(value: Any) -> Tuple[Optional[Tuple[int, str]], Optional[str]]:
    """((number, name), None) from a {"number": N, "name": "..."} object, or
    (None, error). name is optional and may be empty (the stored name is
    kept then); a bare string, the shape before the renumber rule, is an
    error that says what is expected."""
    if not isinstance(value, dict):
        return None, REPLACEMENT_SHAPE
    number = value.get("number")
    if isinstance(number, bool) or not isinstance(number, (int, str)):
        return None, REPLACEMENT_SHAPE
    n = _int_arg(str(number), 1, P.MAX_HORSE)
    if n is None:
        return None, f"replacement number must be 1-{P.MAX_HORSE}"
    name = value.get("name")
    if name is not None and not isinstance(name, str):
        return None, "replacement name must be a string"
    name = " ".join((name or "").split())          # as the store cleans it, so it cannot refuse it later
    if len(name) > NAME_MAX_LEN:
        return None, f"replacement name longer than {NAME_MAX_LEN} characters"
    return (n, name), None


def _renumber_cup(bridge: Any, snap: Dict[str, Any], cup: int, horse: int) -> Optional[str]:
    """set_state() with that one cup's horse changed, the same path as the
    "horse <cup> <n>" command. Returns the bridge's ValueError text, or
    None."""
    phase, horses, scratched = _state_lists(snap)
    horses = [horse if c == cup else h for c, h in zip(P.CUP_NUMBERS, horses)]
    try:
        bridge.set_state(phase, horses, scratched)
    except ValueError as exc:
        return str(exc)
    return None


def _set_scratched(bridge: Any, horse: int, flag: bool):
    """The gateway kind of scratch: flip the scratched flag on the cup that
    carries the horse, through set_state(). Returns (payload, None) or
    (None, (error, status))."""
    snap = bridge.get_snapshot()
    entry = _cup_carrying(snap, horse)
    if entry is None:
        return None, (f"horse {horse} is not on any cup", 400)
    cup = int(entry["cup"])
    phase, horses, scratched = _state_lists(snap)
    scratched = [flag if c == cup else s for c, s in zip(P.CUP_NUMBERS, scratched)]
    try:
        rev = bridge.set_state(phase, horses, scratched)
    except ValueError as exc:
        return None, (str(exc), 400)
    online = bool(bridge.get_snapshot()["link"]["gateway_online"])
    return {"ok": True, "kind": "gateway", "horse": horse, "cup": cup, "scratched": flag,
            "rev": rev, "gateway_online": online}, None


@quiniela_board_bp.route("/api/quiniela/horses", methods=["GET"])
def api_quiniela_horses():
    """{"1": {"name": "..."}, ..., "24": {...}}, names as typed (the model
    serves them upper-cased)."""
    resp = jsonify(_horses_as_typed())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@quiniela_board_bp.route("/api/quiniela/horses", methods=["PUT"])
def api_quiniela_horses_put():
    """Body {"text": "1. Fierceness\\n2. ...\\n22. Ocelli"} (parse_names_text,
    up to 24 lines) or {"1": {"name": "..."}, ...} with name required per
    entry (a "replaced" key, the shape before the renumber rule, is ignored).
    Only the horses given are touched."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    board = get_board()
    try:
        if "text" in body:
            names = parse_names_text(body["text"])
        else:
            names = {}
            for key, entry in body.items():
                if not isinstance(entry, dict) or "name" not in entry:
                    return _bad(f"horse {key}: expected {{\"name\": ...}}")
                names[key] = entry["name"]
        board.store.set_names(names)
    except ValueError as exc:
        return _bad(str(exc))
    board.refresh()
    return jsonify({"ok": True, "names_rev": board.store.names_rev, "horses": _horses_as_typed()})


@quiniela_board_bp.route("/api/quiniela/scratch", methods=["POST"])
def api_quiniela_scratch():
    """{"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}}: the
    renumber. 9 must be in the field (400 "horse 9 is not in the field") and
    22 unused: not carried by any cup, not in the field, not the was or now
    of any record (400 "22 is in use"; 22 == 9 is in use too). The cup
    carrying 9, if any, gets 22 through set_state(); with no such cup (a
    scratch before adoption) only the record is made, and "horse <cup> 9"
    later assigns 22. Then the store records {was: 9, now: 22}, names 22 if
    a name was given, and bumps names_rev. Reply {"ok": true, "kind":
    "replacement", "was": {"number", "name"}, "now": {"number", "name"},
    "cup": <cup or null>, "names_rev": N}, names as typed. A bare string
    replacement is a 400 that says the shape.

    {"horse": 9} (replacement absent or null): the gateway kind, the cup's
    scratched flag through set_state(), its tokens leave the pot; 400 when no
    cup carries 9."""
    body = request.get_json(silent=True)
    horse, error = _horse_arg(body)
    if error:
        return _bad(error)
    replacement = body.get("replacement")
    if replacement is None:
        bridge = get_board().bridge
        if bridge is None:
            return _bad("bridge not initialised", 503)
        payload, failure = _set_scratched(bridge, horse, True)
        if failure:
            return _bad(*failure)
        get_board().refresh()
        return jsonify(payload)
    parsed, error = _replacement_arg(replacement)
    if error:
        return _bad(error)
    number, name = parsed
    board = get_board()
    snap, on_cups, gateway = _field_view(board)
    records = board.store.scratches()
    if not in_field(horse, records, gateway):
        return _bad(f"horse {horse} is not in the field")
    if (number == horse or number in on_cups or in_field(number, records, gateway)
            or number in records or number in records.values()):
        return _bad(f"{number} is in use")
    cup = on_cups.get(horse)
    if cup is not None:
        error = _renumber_cup(board.bridge, snap, cup, number)
        if error:
            return _bad(error)
    try:
        done = board.store.scratch_replace(horse, number, name)
    except ValueError as exc:
        return _bad(str(exc))
    board.refresh()
    return jsonify({"ok": True, "kind": "replacement", "cup": cup,
                    "names_rev": board.store.names_rev, **done})


@quiniela_board_bp.route("/api/quiniela/unscratch", methods=["POST"])
def api_quiniela_unscratch():
    """{"horse": 9}: if 9 is the "was" of a record, the cup carrying the
    record's "now" (if any does) goes back to 9 through set_state(), the
    record is removed (22's name stays stored) and names_rev bumps; reply
    {"ok": true, "kind": "replacement", "was", "now", "cup", "names_rev"}.
    A chain (9 -> 22, then 22 -> 23) is undone last record first: while
    22 -> 23 stands, undoing 9 is a 400 "horse 9: undo 22 first" (the cup
    carries 23, so nothing could go back to 9, and 22 would be left out of
    the field with no record to bring it back). Else the gateway flag on
    the cup carrying 9 is cleared, as before; 400 "horse 9 is not
    scratched" when neither applies."""
    body = request.get_json(silent=True)
    horse, error = _horse_arg(body)
    if error:
        return _bad(error)
    board = get_board()
    now = board.store.replacement_of(horse)
    if now is not None:
        if board.store.replacement_of(now) is not None:
            return _bad(f"horse {horse}: undo {now} first")
        snap, on_cups, _ = _field_view(board)
        cup = on_cups.get(now)
        if cup is not None:
            error = _renumber_cup(board.bridge, snap, cup, horse)
            if error:
                return _bad(error)
        board.store.unscratch_replace(horse)
        board.refresh()
        return jsonify({"ok": True, "kind": "replacement", "cup": cup,
                        "names_rev": board.store.names_rev,
                        "was": {"number": horse, "name": board.store.name_of(horse)},
                        "now": {"number": now, "name": board.store.name_of(now)}})
    bridge = board.bridge
    if bridge is not None:
        entry = _cup_carrying(bridge.get_snapshot(), horse)
        if entry is not None and entry.get("scratched"):
            payload, failure = _set_scratched(bridge, horse, False)
            if failure:
                return _bad(*failure)
            board.refresh()
            return jsonify(payload)
    return _bad(f"horse {horse} is not scratched")


@quiniela_board_bp.route("/api/quiniela/closes_at", methods=["PUT"])
def api_quiniela_closes_at():
    """{"at": <unix time>} sets it, {"in_minutes": 30} counts from the
    server's clock, {"at": null} clears it."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    board = get_board()
    if "at" in body:
        when = body["at"]
        if when is not None and (isinstance(when, bool) or not isinstance(when, (int, float))):
            return _bad(USAGE_CLOSES_AT)
    elif "in_minutes" in body:
        minutes = body["in_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
            return _bad(USAGE_CLOSES_AT)
        when = board.now() + float(minutes) * 60.0
    else:
        return _bad(USAGE_CLOSES_AT)
    try:
        board.store.set_closes_at(when)
    except ValueError as exc:
        return _bad(str(exc))
    board.refresh()
    return jsonify({"ok": True, "closes_at": board.store.closes_at})


@quiniela_board_bp.route("/quiniela/admin", methods=["GET"])
def quiniela_admin_page():
    return render_template("quiniela_admin.html")

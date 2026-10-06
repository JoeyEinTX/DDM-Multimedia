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
# POST /api/quiniela/scratch and /unscratch, PUT /api/quiniela/closes_at),
# the race itself (GET/PUT /api/quiniela/race: its name and post time, the
# one store of race information), the real track's odds for the slideshow
# (GET/PUT /api/quiniela/odds, POST /api/quiniela/odds/start and /stop), the
# hand count of the cash box (PUT /api/quiniela/counted_pot: the pot and the
# prizes come from it once betting has closed), the
# between-races reset (POST /api/quiniela/reset) and the phone-sized admin
# page at GET /quiniela/admin that drives them. Race state stays on
# /api/quiniela/cmd. migrate_race_setup() copies what the old Race Setup
# file held, once, when pi5 starts.
#
# Protocol v2: the cup owns its horse number, so nothing here addresses a
# cup. A replacement scratch of horse 9 by 22 (Churchill's rule: an
# also-eligible that draws in keeps its own program number) is a record
# {was: 9, now: 22} in the store; the board's refresh() turns it into the
# renumber pair [9, 22] in the gateway's state line, and the cup that says
# it is 9 becomes 22 on its own. A no-replacement scratch is a record with
# now None and a bit in the state line. Undoing sends the pair back for a
# while. The horse and scratch commands of v1 are gone from /api/quiniela/cmd.
#
# Same conventions as blueprint.py: a module-level init called from main.py,
# an accessor, and a start called from main.py's __main__ block only, so
# importing the app starts no thread.

import json
import logging
import os
from typing import Any, Dict, Optional, Set, Tuple

from flask import Blueprint, Response, jsonify, render_template, request

from la_quiniela import protocol as P
from la_quiniela import racetime
from la_quiniela.betting import (
    COUNTED_POT_MAX, MODE_STATES, BettingBoard, CountRefused, load_board_settings, sse_events, validate_cmd,
)
from la_quiniela.horses import FIELD_SIZE, HORSE_COUNT, NAME_MAX_LEN, horse_at, in_field, parse_names_text

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
                "protocol, and the up state line is the gateway's report, not a command")
USAGE_STATE = "usage: state 0-6"
USAGE_CLOSES_AT = 'usage: {"at": <unix time>} | {"in_minutes": N} | {"at": null}'
REPLACEMENT_SHAPE = 'replacement must be {"number": N, "name": "..."}'
USAGE_RACE = ('usage: {"name": "...", "date": "YYYY-MM-DD", "time": "HH:MM"} (the race\'s clock; '
              '"" for both clears the post time) | {"post_at": <unix time> | null}')
USAGE_ODDS = 'usage: {"odds": {"1": "5-2", "22": "30-1", ...}} | {"odds": null}'
USAGE_COUNTED_POT = ('usage: {"amount": <whole dollars, 0-%d>} sets the hand count | {"amount": null} clears it'
                     % COUNTED_POT_MAX)

# The Race Setup page kept a post time as a time of day, which it treated as
# on this date and on Churchill's clock (it printed "6:57 PM ET"): what
# migrate_race_setup() makes of one.
LEGACY_RACE_DATE = "2026-05-02"
LEGACY_RACE_TZ = "America/New_York"


def init_board(bridge: Any = None, settings: Optional[Dict[str, Any]] = None,
               **board_kwargs: Any) -> BettingBoard:
    """Create the board (no thread yet) and hook it to the bridge. With no
    bridge given, the one init_la_quiniela() made is used; if there is none
    the board still answers, with link_ok false.

    Safe to call again: the previous board's thread is stopped and the board
    replaced. board_kwargs (clock, wall, log_dir, results_path) are for tests."""
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


def migrate_race_setup(path: Any, board: Optional[BettingBoard] = None) -> Dict[str, Any]:
    """The old Race Setup store (data/race_setup.json: a post time as a time
    of day, twenty names, the odds) is obsolete: race information lives in La
    Quiniela's store. Called when pi5 starts. If the file exists it is said so
    in the log, and the first time only (the store remembers) its post time
    is copied into the race info when that is empty, and its names when the
    store has no names at all. The file itself is left where it is. Returns
    what happened, for the log and the tests: {"file", "copied_post_at",
    "copied_names", "before"} ("before": done on an earlier start)."""
    board = board or get_board()
    out: Dict[str, Any] = {"file": False, "copied_post_at": None, "copied_names": 0, "before": False}
    path = str(path)
    if not os.path.exists(path):
        return out
    out["file"] = True
    logger.info("La Quiniela: %s is obsolete (race info and names are La Quiniela's: /quiniela/admin); "
                "left as it is", path)
    store = board.store
    if store.race_migrated:
        out["before"] = True
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("La Quiniela: cannot read %s (%s); nothing copied from it", path, exc)
        data = None
    if isinstance(data, dict):
        post_time = str(data.get("post_time") or "").strip()
        if post_time and store.race_empty:
            try:
                when = racetime.local_to_epoch(LEGACY_RACE_DATE, post_time, LEGACY_RACE_TZ)
                store.set_race(post_at=when, year=int(LEGACY_RACE_DATE[:4]))
                out["copied_post_at"] = when
            except ValueError as exc:
                logger.warning("La Quiniela: the old post time %r is not HH:MM (%s); not copied", post_time, exc)
        horses = data.get("horses")
        if isinstance(horses, dict) and not any(entry["name"] for entry in store.horses().values()):
            names = {}
            for key, name in horses.items():
                try:
                    n = int(str(key).strip())
                except ValueError:
                    continue
                if 1 <= n <= FIELD_SIZE and isinstance(name, str) and name.strip():
                    names[n] = name
            if names:
                try:
                    store.set_names(names)
                    out["copied_names"] = len(names)
                except ValueError as exc:
                    logger.warning("La Quiniela: the old names were not copied (%s)", exc)
    store.mark_race_migrated()
    logger.info("La Quiniela: from %s, copied %s and %d names", path,
                "the post time" if out["copied_post_at"] is not None else "no post time", out["copied_names"])
    return out


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
    return word, ()


@quiniela_board_bp.route("/api/quiniela/cmd", methods=["POST"])
def api_quiniela_cmd():
    """Body {"cmd": "state 1"}. The first word must be one of the splash's
    whitelist: state is translated onto the bridge's own set_state() (never
    a text line to the port: the bridge's revision model is the source of
    truth); demo and json have no equivalent here and are refused. The v1
    horse, scratch and roster commands no longer exist: a cup's number is
    set on the cup, scratches go through POST /api/quiniela/scratch."""
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

    board = get_board()
    if board.bridge is None:
        return _bad("bridge not initialised", 503)

    # The same path a dashboard mode takes (set_race_state). A state change
    # is applied even when the gateway is offline: DevPi is the source of
    # truth and re-sends on the next hello / status. The response's
    # gateway_online tells the caller which of the two happened.
    try:
        done = board.set_race_state(args[0], source="cmd")
    except ValueError as exc:
        return _bad(str(exc))
    return jsonify({"ok": True, "rev": done["rev"], "phase": args[0], "gateway_online": done["gateway_online"]})


@quiniela_board_bp.route("/api/quiniela/mode", methods=["GET"])
def api_quiniela_mode():
    """The one race state and the dashboard mode that set it: {"ok": true,
    "state": 1, "state_name": "BETTING_OPEN", "mode": "BETTING_60", "label":
    "60 MIN", "source": "dashboard", "modes": {...the table...}}. mode and
    label are null when the state was set directly (the admin page's
    buttons, `state N`, a reset) or not since pi5 started."""
    resp = jsonify({"ok": True, **get_board().race_mode(), "modes": dict(MODE_STATES)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@quiniela_board_bp.route("/api/quiniela/mode", methods=["POST"])
def api_quiniela_mode_set():
    """{"mode": "BETTING_60"}: a dashboard mode. La Quiniela's race state is
    the one the table gives (betting.MODE_STATES), set through the same
    path as `state N`; the reply is {"ok": true, "mode", "state",
    "state_name", "source": "dashboard", "rev", "gateway_online"}. 400 for a
    name that is not a mode, 503 without a bridge. Nothing here touches the
    LEDs: the dashboard's button does that, as it always did."""
    body = request.get_json(silent=True)
    mode = body.get("mode") if isinstance(body, dict) else None
    if not isinstance(mode, str) or mode.strip().upper() not in MODE_STATES:
        return _bad("unknown mode %r; one of %s" % (mode, " ".join(MODE_STATES)))
    board = get_board()
    if board.bridge is None:
        return _bad("bridge not initialised", 503)
    try:
        done = board.set_mode(mode)
    except ValueError as exc:
        return _bad(str(exc))
    return jsonify({"ok": True, **done})


# -----------------------------------------------------------------------------
# Names, scratches, closing time, the reset and the admin page
# -----------------------------------------------------------------------------

@quiniela_board_bp.route("/api/quiniela/reset", methods=["POST"])
def api_quiniela_reset():
    """The between-races reset. Not a dev route: it is the button on the
    admin page. Betting starts over: PRE_RACE, the results cleared (the
    dashboard's file too), the closing time cleared, the ticker cleared, the
    cups' current counts the new baseline so nothing shows as a bet. Names
    and both kinds of scratch are untouched; the cups keep their numbers,
    which are theirs. Tokens still in a cup are not an error: the pot reads
    them and "horses_with_tokens" says which horses. Reply: {"ok": true,
    "race_state": 0, "pot", "total_tokens", "horses_with_tokens",
    "cups_online", "events": 0, "closes_at": null, "rev", "gateway_online",
    "names_rev"}."""
    try:
        done = get_board().reset_betting()
    except ValueError as exc:
        return _bad(str(exc))
    return jsonify({"ok": True, **done})


def _horses_as_typed() -> Dict[str, Dict[str, str]]:
    return {str(n): entry for n, entry in sorted(get_board().store.horses().items())}


def _field_view(board: BettingBoard) -> Tuple[Dict[str, Any], Dict[int, str], Set[int]]:
    """(snapshot, {horse: MAC of a cup claiming it, online first}, the
    horses whose bit the bridge's state line carries). With no bridge the
    snapshot is empty."""
    bridge = board.bridge
    snap = bridge.get_snapshot() if bridge is not None else {"cups": [], "devpi": {}}
    on_cups: Dict[int, str] = {}
    entries = [c for c in snap.get("cups") or [] if isinstance(c, dict)]
    for entry in sorted(entries, key=lambda e: 0 if e.get("online") else 1):
        horse, mac = entry.get("horse"), entry.get("mac")
        if (isinstance(horse, int) and not isinstance(horse, bool) and 1 <= horse <= P.MAX_HORSE
                and isinstance(mac, str) and mac and horse not in on_cups):
            on_cups[horse] = mac
    devpi = snap.get("devpi") if isinstance(snap.get("devpi"), dict) else {}
    scratched = {h for h in devpi.get("scratched") or [] if isinstance(h, int)}
    return snap, on_cups, scratched


def _gateway_online(snap: Dict[str, Any]) -> bool:
    link = snap.get("link") if isinstance(snap, dict) else None
    return bool(link.get("gateway_online")) if isinstance(link, dict) else False


def _rev(board: BettingBoard) -> Optional[int]:
    bridge = board.bridge
    return int(bridge.state_rev) if bridge is not None else None


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


def field_by_post(board: BettingBoard) -> Dict[str, Any]:
    """The field as the mantle has it: {"names_rev", "posts", "names"}.

    A post is a place on the mantle, 1..20, and the LED cup there. `posts`
    has one entry per post somebody runs from, in post order: {"post": 9,
    "horse": 22, "name": "OCELLI", "label": "22 · OCELLI", "replaces": 9}.
    The horse is the post's own, or the one standing in for it (a replacement
    scratch renumbers the cup and nothing moves, so 22 runs from post 9 and
    "replaces" says so); a post whose horse was scratched with no replacement
    has no entry. Names are La Quiniela's, upper-cased, "" where none is
    stored (the label then says HORSE n). `names` carries all 24, keyed
    "1".."24": the results tote names whatever the results say."""
    store = board.store
    records = store.scratches()
    names = {n: str((entry or {}).get("name") or "").upper() for n, entry in store.horses().items()}
    posts = []
    for post in range(1, FIELD_SIZE + 1):
        horse = horse_at(post, records)
        if horse is None or not in_field(horse, records):
            continue
        name = names.get(horse, "")
        entry: Dict[str, Any] = {"post": post, "horse": horse, "name": name,
                                 "label": "%d \u00b7 %s" % (horse, name or "HORSE %d" % horse)}
        if horse != post:
            entry["replaces"] = post
        posts.append(entry)
    return {"names_rev": store.names_rev, "posts": posts,
            "names": {str(n): names.get(n, "") for n in range(1, HORSE_COUNT + 1)}}


@quiniela_board_bp.route("/api/quiniela/field", methods=["GET"])
def api_quiniela_field():
    """The field by post, for the dashboard's SET WINNERS pickers and its
    results tote (field_by_post)."""
    resp = jsonify(field_by_post(get_board()))
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    22 unused: not claimed by any cup, not in the field, not the was or now
    of any record (400 "22 is in use"; 22 == 9 is in use too). The store
    records {was: 9, now: 22}, names 22 if a name was given (keeping the
    name it had, for Undo), bumps names_rev, and the board's refresh() puts
    the pair [9, 22] in the gateway's state line: the cup that says it is 9
    becomes 22 on its own, tokens and all.
    Reply {"ok": true, "kind": "replacement", "was": {"number", "name"},
    "now": {"number", "name"}, "cup": <MAC of the cup claiming 9, or null>,
    "renum": [9, 22], "rev": R, "gateway_online": bool, "names_rev": N},
    names as typed. A bare string replacement is a 400 that says the shape.

    {"horse": 9} (replacement absent or null): the no-replacement kind. It
    is about the horse, not the cup: the store records (was 9, now None), so
    9 is out of the field and its tokens out of the pot whether or not a cup
    claims it, and refresh() sets its bit in the state line so the cup that
    is 9 draws its X (a cup set to 9 later sees the same bit). Reply {"ok":
    true, "kind": "gateway", "horse": 9, "cup": <MAC or null>, "scratched":
    true, "rev": R, "gateway_online": bool, "names_rev": N, "was": {...}}.
    400 "horse 9 is not in the field" or "horse 9 is already scratched"."""
    body = request.get_json(silent=True)
    horse, error = _horse_arg(body)
    if error:
        return _bad(error)
    board = get_board()
    snap, on_cups, gateway = _field_view(board)
    records = board.store.scratches()
    if horse in records or horse in gateway:
        return _bad(f"horse {horse} is already scratched")
    if not in_field(horse, records, gateway):
        return _bad(f"horse {horse} is not in the field")
    replacement = body.get("replacement")
    if replacement is None:
        try:
            done = board.store.scratch_gateway(horse)
        except ValueError as exc:
            return _bad(str(exc))
        board.refresh()
        return jsonify({"ok": True, "kind": "gateway", "horse": horse, "cup": on_cups.get(horse),
                        "scratched": True, "rev": _rev(board), "gateway_online": _gateway_online(snap),
                        "names_rev": board.store.names_rev, **done})
    parsed, error = _replacement_arg(replacement)
    if error:
        return _bad(error)
    number, name = parsed
    if (number == horse or number in on_cups or in_field(number, records, gateway)
            or number in records or number in records.values()):
        return _bad(f"{number} is in use")
    try:
        done = board.store.scratch_replace(horse, number, name)
    except ValueError as exc:
        return _bad(str(exc))
    board.refresh()
    return jsonify({"ok": True, "kind": "replacement", "cup": on_cups.get(horse),
                    "renum": [horse, number], "rev": _rev(board), "gateway_online": _gateway_online(snap),
                    "names_rev": board.store.names_rev, **done})


@quiniela_board_bp.route("/api/quiniela/unscratch", methods=["POST"])
def api_quiniela_unscratch():
    """{"horse": 9}: if 9 is the "was" of a record, the record is removed,
    22's name goes back to what it was before the scratch (a name the
    scratch gave is cleared, one entered ahead stays), names_rev bumps, and
    the board sends the pair [22, 9] down for a minute or until a cup
    reports 9, so the cup that became 22 goes back to 9; reply {"ok": true,
    "kind": "replacement", "was", "now", "cup": <MAC claiming 22, or null>,
    "renum": [22, 9], "names_rev"}. A chain (9 -> 22, then 22 -> 23) is
    undone last record first: while 22 -> 23 stands, undoing 9 is a 400
    "horse 9: undo 22 first" (the cup is 23, so nothing could go back to 9,
    and 22 would be left out of the field with no record to bring it back).
    Else the no-replacement record goes and its bit leaves the state line;
    400 "horse 9 is not scratched" when neither applies."""
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
        board.queue_undo_renum(now, horse)
        board.store.unscratch_replace(horse)
        board.refresh()
        return jsonify({"ok": True, "kind": "replacement", "cup": on_cups.get(now),
                        "renum": [now, horse], "rev": _rev(board), "gateway_online": _gateway_online(snap),
                        "names_rev": board.store.names_rev,
                        "was": {"number": horse, "name": board.store.name_of(horse)},
                        "now": {"number": now, "name": board.store.name_of(now)}})
    snap, on_cups, gateway = _field_view(board)
    if board.store.record(horse) != ("gateway", None) and horse not in gateway:
        return _bad(f"horse {horse} is not scratched")
    board.store.unscratch_gateway(horse)
    board.refresh()
    return jsonify({"ok": True, "kind": "gateway", "horse": horse, "cup": on_cups.get(horse),
                    "scratched": False, "rev": _rev(board), "gateway_online": _gateway_online(snap),
                    "names_rev": board.store.names_rev})


@quiniela_board_bp.route("/api/quiniela/counted_pot", methods=["PUT"])
def api_quiniela_counted_pot():
    """The hand count of the cash box's BETS compartment. {"amount": 152}
    sets it (whole dollars, 0 to COUNTED_POT_MAX; entering again overwrites),
    {"amount": null} clears it and puts the scale figures back. From then
    on the pot and all three prizes, on the TV and the admin page, come from
    the count with the same split and rounding; bets per horse stay as the
    scales read them. 400 for an amount that is not a whole number of
    dollars in range; 409 when the race is not in AT THE POST, RUNNING or
    WINNER (3-5) or there are no figures at the post yet. The count is kept
    in the figures-at-the-post record, so Reset betting and a state of 0 or 1
    clear it with them and a restart of pi5 keeps it. The model is pushed at
    once. Reply {"ok": true, "pot_counted", "pot_scale", "pot", "prizes",
    "hand_counted", "race_state", "saved"}."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "amount" not in body:
        return _bad(USAGE_COUNTED_POT)
    try:
        done = get_board().set_pot_counted(body["amount"])
    except CountRefused as exc:
        return _bad(str(exc), 409)
    except ValueError as exc:
        return _bad(str(exc))
    return jsonify({"ok": True, **done})


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


@quiniela_board_bp.route("/api/quiniela/race", methods=["GET"])
def api_quiniela_race():
    """The race as the model has it, plus the post time's date and time on
    the race's clock (what the admin page's form shows)."""
    resp = jsonify({"ok": True, **get_board().race_form()})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@quiniela_board_bp.route("/api/quiniela/race", methods=["PUT"])
def api_quiniela_race_put():
    """{"name": ..., "date": "2027-05-01", "time": "17:57"} on the race's
    clock (LQ_RACE_TZ), any part left out left alone; "date" and "time" go
    together, both "" (or null) clear the post time. Or {"post_at": <unix
    time> | null}. The year follows the post time. Reset betting never
    touches any of it."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not any(k in body for k in ("name", "date", "time", "post_at", "year")):
        return _bad(USAGE_RACE)
    board = get_board()
    tz = board.race_view()["tz"]
    change: Dict[str, Any] = {}
    try:
        if "name" in body:
            change["name"] = body["name"] if body["name"] is not None else ""
        if "date" in body or "time" in body:
            day = body.get("date") if body.get("date") is not None else ""
            hhmm = body.get("time") if body.get("time") is not None else ""
            if not isinstance(day, str) or not isinstance(hhmm, str):
                return _bad(USAGE_RACE)
            if not day.strip() and not hhmm.strip():
                change["post_at"], change["year"] = None, None          # both empty: no post time
            elif not day.strip() or not hhmm.strip():
                return _bad('date and time go together: "YYYY-MM-DD" and "HH:MM", or both "" to clear')
            else:
                change["post_at"] = racetime.local_to_epoch(day, hhmm, tz)
        elif "post_at" in body:
            when = body["post_at"]
            if when is not None and (isinstance(when, bool) or not isinstance(when, (int, float))):
                return _bad(USAGE_RACE)
            change["post_at"] = when
            if when is None:
                change["year"] = None
        if change.get("post_at") is not None:
            change["year"] = racetime.describe(change["post_at"], tz)["year"]
        if "year" in body and "post_at" not in change:
            change["year"] = body["year"]
        board.store.set_race(**change)
    except ValueError as exc:
        return _bad(str(exc))
    board.refresh()
    return jsonify({"ok": True, **board.race_form()})


def _odds_poller():
    try:
        from la_quiniela.odds import get_odds_poller
        return get_odds_poller()
    except RuntimeError:
        return None


@quiniela_board_bp.route("/api/quiniela/odds", methods=["GET"])
def api_quiniela_odds():
    """The track's odds the model carries, by program number, and the
    poller's state."""
    poller = _odds_poller()
    status = poller.status() if poller is not None else {"polling": False, "interval": None,
                                                         "last_update": None, "next_update": None}
    resp = jsonify({"ok": True, **status,
                    "odds": {str(n): v for n, v in sorted(get_board().odds().items())}})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@quiniela_board_bp.route("/api/quiniela/odds", methods=["PUT"])
def api_quiniela_odds_put():
    """By hand, when there is no poller or no internet (the morning line from
    the program): {"odds": {"1": "5-2", "22": "30-1"}} replaces them all,
    {"odds": null} clears them."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "odds" not in body or not (body["odds"] is None or isinstance(body["odds"], dict)):
        return _bad(USAGE_ODDS)
    board = get_board()
    kept = board.set_odds(body["odds"] or {})
    board.refresh()
    return jsonify({"ok": True, "odds": {str(n): v for n, v in sorted(kept.items())}})


@quiniela_board_bp.route("/api/quiniela/odds/start", methods=["POST"])
def api_quiniela_odds_start():
    """{"interval": seconds} (optional, 300 by default, 60 at least)."""
    poller = _odds_poller()
    if poller is None:
        return _bad("odds poller not initialised", 503)
    body = request.get_json(silent=True)
    result = poller.start((body or {}).get("interval") if isinstance(body, dict) else None)
    if not result.get("ok"):
        return _bad(result.get("error", "cannot start"), result.get("status", 400))
    return jsonify(result)


@quiniela_board_bp.route("/api/quiniela/odds/stop", methods=["POST"])
def api_quiniela_odds_stop():
    poller = _odds_poller()
    if poller is None:
        return _bad("odds poller not initialised", 503)
    return jsonify(poller.stop())


@quiniela_board_bp.route("/quiniela/admin", methods=["GET"])
def quiniela_admin_page():
    """The phone page: the Race section (state buttons, the figures, Reset
    betting, the Horses list), then the race's name and post time, names,
    scratches and the closing time."""
    return render_template("quiniela_admin.html")

# la_quiniela/sim/protocol.py - the gateway's serial line protocol, written
# for the simulator from firmware/quiniela/README.md ("Serial line protocol",
# line protocol v2) and the behaviour in ddm_gateway.ino.
#
# Deliberately NOT shared with the bridge's la_quiniela/protocol.py: if both
# sides shared this code, a shared mistake would pass every test. The only
# import from the bridge is the Phase enum.
#
# Protocol v2: a cup is known by its MAC and the horse number it reports.
# There are no slots, no roster line and no cup IDs; the gateway's cup table
# is its own and only its contents (MAC, horse, tokens, signal, age) are
# reported.

import json
from typing import Any, Dict, List, Optional, Tuple

from la_quiniela.protocol import Phase

LINE_PROTO_VERSION = 2          # "v" in the hello line
PROTO_VERSION = 2               # "proto" in the hello line (DDM_PROTO_VERSION)
MAX_CUPS = 24                   # DDM_MAX_CUPS: the gateway's table
MAX_LINE = 1024                 # bytes per downlink line, excluding the newline
BIG_LINE = 2560                 # the status and up state lines (they carry the table)
EXCERPT_CHARS = 40              # of a rejected line echoed in err
MAX_HORSE = 24                  # DDM_MAX_HORSE
RENUM_SLOTS = 4                 # DDM_RENUM_SLOTS
RESULT_SLOTS = 3                # DDM_RESULT_SLOTS
PHASE_MIN = min(p.value for p in Phase)
PHASE_MAX = max(p.value for p in Phase)


# -----------------------------------------------------------------------------
# Uplink lines (gateway -> DevPi), key order exactly as the README examples
# -----------------------------------------------------------------------------

def _compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


def telem_line(mac: str, horse: int, raw: int, count: int, seq: int, drop: int,
               rssi: int, up: int, hello: bool = False) -> str:
    obj: Dict[str, Any] = {"t": "telem", "mac": mac, "horse": int(horse), "raw": int(raw),
                           "count": int(count), "seq": int(seq), "drop": int(drop),
                           "rssi": int(rssi), "up": int(up)}
    if hello:
        obj["hello"] = 1
    return _compact(obj)


def hello_line(mac: str) -> str:
    return _compact({"t": "hello", "v": LINE_PROTO_VERSION, "proto": PROTO_VERSION, "mac": mac})


def cup_entry(mac: str, horse: int, tok: int, rssi: int, up: int, age_ms: int) -> Dict[str, Any]:
    """One entry of the status / up state line's cups[], in the sketch's key order."""
    return {"mac": mac, "horse": int(horse), "tok": int(tok), "rssi": int(rssi),
            "up": int(up), "age": int(age_ms)}


def status_line(gseq: int, phase: int, state_rev: int, cups: List[Dict[str, Any]],
                rejects: int, up_s: int) -> str:
    return _compact({"t": "status", "gseq": int(gseq), "phase": int(phase),
                     "state_rev": int(state_rev), "cups": list(cups),
                     "rejects": int(rejects), "up_s": int(up_s)})


def state_report_line(gseq: int, demo: bool, mac: str, phase: int, scratched: List[int],
                      renum: List[Tuple[int, int]], results: List[int],
                      cups: List[Dict[str, Any]]) -> str:
    """The up `state` line (typed `json`): the packet's contents and the table."""
    return _compact({"t": "state", "seq": int(gseq), "demo": 1 if demo else 0, "mac": mac,
                     "st": int(phase), "scr": sorted(int(h) for h in scratched),
                     "renum": [[int(f), int(t)] for f, t in renum],
                     "res": [int(h) for h in results], "cups": list(cups)})


def excerpt(line: bytes) -> str:
    """First EXCERPT_CHARS bytes of a rejected line, JSON-escaped the way the
    gateway does it: '"' and '\\' escaped, control characters dropped, bytes
    above 0x7F written as \\u00XX."""
    out = []
    for c in line[:EXCERPT_CHARS]:
        if c == 0x22:
            out.append('\\"')
        elif c == 0x5C:
            out.append("\\\\")
        elif c < 0x20 or c == 0x7F:
            continue
        elif c >= 0x80:
            out.append("\\u%04X" % c)
        else:
            out.append(chr(c))
    return "".join(out)


def err_line(msg: str, line: Optional[bytes] = None) -> str:
    """The err line is built by hand, like the gateway's snprintf, so the
    excerpt's escapes are emitted verbatim rather than escaped again."""
    if line is None:
        return '{"t":"err","msg":"%s"}' % msg
    return '{"t":"err","msg":"%s","line":"%s"}' % (msg, excerpt(line))


# -----------------------------------------------------------------------------
# Downlink lines (DevPi -> gateway): validation exactly as the README
# -----------------------------------------------------------------------------

class Rejected(Exception):
    """A downlink line the gateway answers with err. .msg is 'parse' or 'invalid'."""

    def __init__(self, msg: str, why: str = ""):
        super().__init__(why or msg)
        self.msg = msg
        self.why = why


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(value: Any, lo: int, hi: int, what: str) -> int:
    if not _is_int(value) or value < lo or value > hi:
        raise Rejected("invalid", f"{what} must be an integer {lo}..{hi}")
    return value


def _rev(obj: Dict[str, Any]) -> int:
    rev = obj.get("rev")
    if not _is_int(rev) or rev < 1:
        raise Rejected("invalid", "rev must be an integer >= 1")
    return rev


def parse_downlink(body: bytes) -> Tuple[str, Any]:
    """One line without its newline, already known to start with '{'.

    Returns ("state", {"rev","st","scr": set,"renum": [(from,to)],"res": [w,p,s]}),
    ("debug", bool) or ("ignore", None) for an unknown "t" (a v1 "roster"
    included). Unknown keys are ignored. Raises Rejected("parse") for bad
    JSON and Rejected("invalid") for a line that fails validation."""
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        raise Rejected("parse")
    if not isinstance(obj, dict):
        raise Rejected("invalid", "not an object")
    kind = obj.get("t")
    if not isinstance(kind, str):
        raise Rejected("invalid", "missing t")
    if kind == "state":
        rev = _rev(obj)
        st = _int_in(obj.get("st"), PHASE_MIN, PHASE_MAX, "st")
        scr = obj.get("scr")
        if not isinstance(scr, list):
            raise Rejected("invalid", "scr must be an array")
        scratched = {_int_in(h, 1, MAX_HORSE, "scr entry") for h in scr}
        renum = obj.get("renum")
        if not isinstance(renum, list) or len(renum) > RENUM_SLOTS:
            raise Rejected("invalid", f"renum must be an array of at most {RENUM_SLOTS} pairs")
        pairs: List[Tuple[int, int]] = []
        for pair in renum:
            if not isinstance(pair, list) or len(pair) != 2:
                raise Rejected("invalid", "renum entries must be [from, to]")
            frm = _int_in(pair[0], 1, MAX_HORSE, "renum from")
            to = _int_in(pair[1], 1, MAX_HORSE, "renum to")
            if frm == to:
                raise Rejected("invalid", "renum from and to must differ")
            if any(f == frm for f, _ in pairs):
                raise Rejected("invalid", f"renum from {frm} appears twice")
            pairs.append((frm, to))
        res = obj.get("res")
        if not isinstance(res, list) or len(res) != RESULT_SLOTS:
            raise Rejected("invalid", f"res must have exactly {RESULT_SLOTS} entries")
        results = [_int_in(h, 0, MAX_HORSE, "res entry") for h in res]
        return "state", {"rev": rev, "st": st, "scr": scratched, "renum": pairs, "res": results}
    if kind == "debug":
        on = obj.get("on")
        if not isinstance(on, bool):
            raise Rejected("invalid", "on must be a boolean")
        return "debug", on
    return "ignore", None


def phase_name(phase: int) -> str:
    try:
        return Phase(phase).name
    except ValueError:
        return str(phase)

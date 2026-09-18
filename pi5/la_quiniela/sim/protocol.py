# la_quiniela/sim/protocol.py - the gateway's serial line protocol, written
# for the simulator from firmware/quiniela/README.md ("Serial line protocol")
# and the behaviour in ddm_gateway.ino.
#
# Deliberately NOT shared with the bridge's la_quiniela/protocol.py: if both
# sides shared this code, a shared mistake would pass every test. The only
# import from the bridge is the Phase enum.
#
# Cup numbers: the person at the keyboard uses 1..20, the wire uses 0..19.
# cup_to_slot() and slot_to_cup() below are the one place the simulator
# converts; everything the simulator prints or reads from a person goes
# through them.

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from la_quiniela.protocol import Phase

LINE_PROTO_VERSION = 1          # "v" in the hello line
PROTO_VERSION = 1               # "proto" in the hello line (DDM_PROTO_VERSION)
NUM_CUPS = 20                   # DDM_MAX_CUPS
MAX_LINE = 1024                 # bytes per line, both directions, excluding the newline
EXCERPT_CHARS = 40              # of a rejected line echoed in err
MAX_HORSE = 20
BROADCAST_MAC = "FF:FF:FF:FF:FF:FF"
PHASE_MIN = min(p.value for p in Phase)
PHASE_MAX = max(p.value for p in Phase)


# -----------------------------------------------------------------------------
# The one conversion between human cup numbers and wire slots
# -----------------------------------------------------------------------------

def cup_to_slot(cup: int) -> int:
    """Human cup number 1..20 -> wire slot 0..19."""
    return cup - 1


def slot_to_cup(slot: int) -> int:
    """Wire slot 0..19 -> human cup number 1..20."""
    return slot + 1


# -----------------------------------------------------------------------------
# Uplink lines (gateway -> DevPi), key order exactly as the README examples
# -----------------------------------------------------------------------------

def _compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


def telem_line(slot: int, mac: str, raw: int, count: int, seq: int, drop: int,
               rssi: int, up: int, claim: Optional[int] = None) -> str:
    obj: Dict[str, Any] = {"t": "telem", "cup": slot, "mac": mac, "raw": int(raw),
                           "count": int(count), "seq": int(seq), "drop": int(drop),
                           "rssi": int(rssi), "up": int(up)}
    if claim is not None:
        obj["claim"] = int(claim)
    return _compact(obj)


def cup_hello_line(slot: int, mac: str) -> str:
    return _compact({"t": "cup_hello", "cup": slot, "mac": mac})


def hello_line(mac: str) -> str:
    return _compact({"t": "hello", "v": LINE_PROTO_VERSION, "proto": PROTO_VERSION, "mac": mac})


def status_line(gseq: int, phase: int, state_rev: int, roster_rev: int, cups: int,
                rejects: int, up_s: int) -> str:
    return _compact({"t": "status", "gseq": int(gseq), "phase": int(phase),
                     "state_rev": int(state_rev), "roster_rev": int(roster_rev),
                     "cups": int(cups), "rejects": int(rejects), "up_s": int(up_s)})


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

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def parse_mac(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _MAC_RE.match(value):
        return None
    return value.upper()


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

    Returns ("state", {"rev","phase","horse","scr"}), ("roster", {"rev","macs"}),
    ("debug", bool) or ("ignore", None) for an unknown "t". Unknown keys are
    ignored. Raises Rejected("parse") for bad JSON and Rejected("invalid")
    for a line that fails validation."""
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
        phase = _int_in(obj.get("phase"), PHASE_MIN, PHASE_MAX, "phase")
        horse = obj.get("horse")
        scr = obj.get("scr")
        if not isinstance(horse, list) or len(horse) != NUM_CUPS:
            raise Rejected("invalid", f"horse must have exactly {NUM_CUPS} entries")
        if not isinstance(scr, list) or len(scr) != NUM_CUPS:
            raise Rejected("invalid", f"scr must have exactly {NUM_CUPS} entries")
        horse = [_int_in(h, 0, MAX_HORSE, "horse entry") for h in horse]
        scr = [_int_in(s, 0, 1, "scr entry") for s in scr]
        return "state", {"rev": rev, "phase": phase, "horse": horse, "scr": scr}
    if kind == "roster":
        rev = _rev(obj)
        macs = obj.get("macs")
        if not isinstance(macs, list) or len(macs) != NUM_CUPS:
            raise Rejected("invalid", f"macs must have exactly {NUM_CUPS} entries")
        parsed: List[Optional[str]] = []
        for entry in macs:
            if not isinstance(entry, str):
                raise Rejected("invalid", "macs entries must be strings")
            if entry == "":
                parsed.append(None)
                continue
            mac = parse_mac(entry)
            if mac is None:
                raise Rejected("invalid", f"not a MAC: {entry!r}")
            if mac == BROADCAST_MAC:
                raise Rejected("invalid", "the broadcast address is never a cup")
            if mac in parsed:
                raise Rejected("invalid", f"{mac} appears twice")
            parsed.append(mac)
        return "roster", {"rev": rev, "macs": parsed}
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

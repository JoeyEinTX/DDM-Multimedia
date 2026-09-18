# la_quiniela/protocol.py - The gateway's serial line protocol, in Python
#
# The contract is firmware/quiniela/README.md, section "Serial line protocol".
# Everything that knows what the wire looks like lives here: the phase enum,
# the two cup-ID conversions, the downlink line builders, uplink line decoding,
# and the same validation the gateway applies to a line.
#
# THE ONE ID RULE. Cup IDs are 0-based on the wire and 1-based everywhere on
# DevPi (database, Python API, SocketIO events, HTTP JSON). wire_to_cup() and
# cup_to_wire() are the only two places in the whole system that convert.
# They are called at the serial boundary and nowhere else.

import json
import re
from enum import IntEnum
from typing import Any, Dict, Optional, Sequence, Tuple

LINE_PROTO_VERSION = 1            # the "v" the gateway reports in its hello line
NUM_CUPS = 20                     # DDM_MAX_CUPS in ddm_common.h
MAX_LINE_BYTES = 1024             # both directions, excluding the newline
MAX_HORSE = 20                    # horse numbers 1..20, 0 = unassigned
CUP_NUMBERS = tuple(range(1, NUM_CUPS + 1))   # 1..20: the DevPi side
WIRE_IDS = tuple(range(NUM_CUPS))             # 0..19: the gateway side
BROADCAST_MAC = "FF:FF:FF:FF:FF:FF"


class Phase(IntEnum):
    """DdmRaceState from firmware/quiniela/ddm_common.h, minus the DDM_ prefix.

    test_smoke parses the header and fails if a name or value drifts."""
    PRE_RACE = 0
    BETTING_OPEN = 1
    FINAL_CALL = 2
    AT_THE_POST = 3
    RUNNING = 4
    WINNER = 5
    AFTER_PARTY = 6


# -----------------------------------------------------------------------------
# The two conversions
# -----------------------------------------------------------------------------

def wire_to_cup(wire_id: Optional[int]) -> Optional[int]:
    """0-based wire cup ID -> 1-based DevPi cup number.

    Wire -1 (the gateway's "MAC not in the roster") becomes None."""
    if wire_id is None or int(wire_id) < 0:
        return None
    return int(wire_id) + 1


def cup_to_wire(cup: Optional[int]) -> int:
    """1-based DevPi cup number -> 0-based wire cup ID. None becomes -1."""
    if cup is None:
        return -1
    return int(cup) - 1


# -----------------------------------------------------------------------------
# MACs
# -----------------------------------------------------------------------------

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def normalize_mac(value: Any) -> Optional[str]:
    """Return the MAC in uppercase colon form, or None if it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _MAC_RE.match(text):
        return None
    return text.upper()


# -----------------------------------------------------------------------------
# Validation, exactly as the gateway does it
# -----------------------------------------------------------------------------

def _int_field(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def validate_state(phase: Any, horses: Sequence[Any], scratched: Sequence[Any]
                   ) -> Tuple[int, Dict[int, int], Dict[int, bool]]:
    """Validate a state the way the gateway validates a state line.

    horses and scratched are NUM_CUPS-item lists, position 0 = cup 1.
    Returns (phase, {cup: horse}, {cup: scratched}) keyed by 1-based cup.
    Raises ValueError with a message fit for a 400 response."""
    phase_i = _int_field(phase, "phase")
    if phase_i not in [p.value for p in Phase]:
        raise ValueError(f"phase must be {Phase.PRE_RACE.value}..{Phase.AFTER_PARTY.value}")
    if not isinstance(horses, (list, tuple)) or len(horses) != NUM_CUPS:
        raise ValueError(f"horses must be a list of exactly {NUM_CUPS} integers")
    if not isinstance(scratched, (list, tuple)) or len(scratched) != NUM_CUPS:
        raise ValueError(f"scratched must be a list of exactly {NUM_CUPS} values")
    horses_by_cup: Dict[int, int] = {}
    scratched_by_cup: Dict[int, bool] = {}
    for cup, value in zip(CUP_NUMBERS, horses):
        h = _int_field(value, f"horses[{cup}]")
        if h < 0 or h > MAX_HORSE:
            raise ValueError(f"horses entries must be 0..{MAX_HORSE} (0 = unassigned)")
        horses_by_cup[cup] = h
    for cup, value in zip(CUP_NUMBERS, scratched):
        if isinstance(value, bool):
            scratched_by_cup[cup] = value
        elif isinstance(value, int) and value in (0, 1):
            scratched_by_cup[cup] = bool(value)
        else:
            raise ValueError("scratched entries must be 0 or 1")
    return phase_i, horses_by_cup, scratched_by_cup


def validate_roster(macs: Sequence[Any]) -> Dict[int, str]:
    """Validate a roster the way the gateway validates a roster line.

    macs is a NUM_CUPS-item list, position 0 = cup 1, None or "" for an empty
    slot. Returns {cup: MAC} for the filled slots only, MACs uppercased.
    Raises ValueError with a message fit for a 400 response."""
    if not isinstance(macs, (list, tuple)) or len(macs) != NUM_CUPS:
        raise ValueError(f"macs must be a list of exactly {NUM_CUPS} entries")
    by_cup: Dict[int, str] = {}
    seen: Dict[str, int] = {}
    for cup, value in zip(CUP_NUMBERS, macs):
        if value is None or value == "":
            continue
        mac = normalize_mac(value)
        if mac is None:
            raise ValueError(f"macs[{cup}] is not a MAC address (AA:BB:CC:DD:EE:FF)")
        if mac == BROADCAST_MAC:
            raise ValueError("the broadcast address can never be a cup")
        if mac in seen:
            raise ValueError(f"{mac} appears twice (cups {seen[mac]} and {cup})")
        seen[mac] = cup
        by_cup[cup] = mac
    return by_cup


# -----------------------------------------------------------------------------
# Downlink lines. Key order matters: these are byte-exact against the README.
# -----------------------------------------------------------------------------

def _compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


def build_state_line(rev: int, phase: int, horses_by_cup: Dict[int, int],
                     scratched_by_cup: Dict[int, bool]) -> str:
    horse = [int(horses_by_cup.get(wire_to_cup(w), 0)) for w in WIRE_IDS]
    scr = [1 if scratched_by_cup.get(wire_to_cup(w)) else 0 for w in WIRE_IDS]
    return _compact({"t": "state", "rev": int(rev), "phase": int(phase),
                     "horse": horse, "scr": scr})


def build_roster_line(rev: int, macs_by_cup: Dict[int, str]) -> str:
    macs = [macs_by_cup.get(wire_to_cup(w)) or "" for w in WIRE_IDS]
    return _compact({"t": "roster", "rev": int(rev), "macs": macs})


def build_debug_line(on: bool) -> str:
    return _compact({"t": "debug", "on": bool(on)})


# -----------------------------------------------------------------------------
# Uplink lines
# -----------------------------------------------------------------------------

def decode_line(raw: bytes) -> Tuple[Optional[Dict[str, Any]], str]:
    """Turn one raw serial line into a dict.

    Returns (obj, "ok"), or (None, why) where why is one of "empty", "text"
    (does not start with "{", the gateway's "# " lines), "too_long" (over
    MAX_LINE_BYTES) or "bad_json"."""
    if not raw:
        return None, "empty"
    body = raw.rstrip(b"\r\n")
    if not body:
        return None, "empty"
    if body[:1] != b"{":
        return None, "text"
    if len(body) > MAX_LINE_BYTES:
        return None, "too_long"
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    return obj, "ok"

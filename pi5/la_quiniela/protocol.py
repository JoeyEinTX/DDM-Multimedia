# la_quiniela/protocol.py - The gateway's serial line protocol, in Python
#
# The contract is firmware/quiniela/README.md, section "Serial line protocol",
# line protocol v2 (the cup owns its horse number). Everything that knows
# what the wire looks like lives here: the phase enum, the horse-number
# limits, the downlink line builders, uplink line decoding, and the same
# validation the gateway applies to a state line.
#
# THE ONE KEY RULE. A cup is known by its MAC and by the horse number it
# reports. There are no cup IDs, slots or rosters on the wire or anywhere on
# DevPi; the gateway keeps a table of the cups it hears, indexed however it
# likes, and that index never leaves the gateway.

import json
import re
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LINE_PROTO_VERSION = 2            # the "v" the gateway reports in its hello line
MAX_LINE_BYTES = 4096             # the status line carries the cup table: about 2.3 KB with 24 cups
MAX_HORSE = 24                    # horse numbers 1..24, 0 = none; mirrors DDM_MAX_HORSE in ddm_common.h
                                  # (1..20 the field, 21..24 the also-eligibles; test_smoke pins the two together)
RENUM_SLOTS = 4                   # DDM_RENUM_SLOTS: renumber pairs a state line may carry
RESULT_SLOTS = 3                  # DDM_RESULT_SLOTS: win, place, show
HORSES = tuple(range(1, MAX_HORSE + 1))


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
# MACs and horse numbers
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


def parse_horse(value: Any) -> int:
    """A horse field off the wire: 1..MAX_HORSE as itself, anything else
    (0, None, out of range, not an integer) as 0 = none."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 1 <= value <= MAX_HORSE else 0


# -----------------------------------------------------------------------------
# Validation, exactly as the gateway does it
# -----------------------------------------------------------------------------

def _int_field(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def validate_phase(phase: Any) -> int:
    phase_i = _int_field(phase, "phase")
    if phase_i not in [p.value for p in Phase]:
        raise ValueError(f"phase must be {Phase.PRE_RACE.value}..{Phase.AFTER_PARTY.value}")
    return phase_i


def validate_scratched(values: Any) -> List[int]:
    """The horses scratched with no replacement: any iterable of 1..MAX_HORSE.
    Returns them sorted, without repeats."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError("scratched must be a list of horse numbers")
    out = set()
    for value in values:
        h = _int_field(value, "scratched entry")
        if h < 1 or h > MAX_HORSE:
            raise ValueError(f"scratched entries must be 1..{MAX_HORSE}")
        out.add(h)
    return sorted(out)


def validate_renum(pairs: Any) -> List[Tuple[int, int]]:
    """The renumber pairs: at most RENUM_SLOTS [from, to] pairs, both
    1..MAX_HORSE, from != to, no from twice."""
    if isinstance(pairs, (str, bytes)) or not isinstance(pairs, Iterable):
        raise ValueError("renum must be a list of [from, to] pairs")
    out: List[Tuple[int, int]] = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("renum entries must be [from, to] pairs")
        frm = _int_field(pair[0], "renum from")
        to = _int_field(pair[1], "renum to")
        if not (1 <= frm <= MAX_HORSE and 1 <= to <= MAX_HORSE):
            raise ValueError(f"renum numbers must be 1..{MAX_HORSE}")
        if frm == to:
            raise ValueError(f"renum {frm} -> {to}: from and to must differ")
        if any(f == frm for f, _ in out):
            raise ValueError(f"renum {frm} appears twice")
        out.append((frm, to))
    if len(out) > RENUM_SLOTS:
        raise ValueError(f"at most {RENUM_SLOTS} renum pairs")
    return out


def validate_results(values: Any) -> List[int]:
    """WIN, PLACE, SHOW: exactly RESULT_SLOTS integers 0..MAX_HORSE (0 = not
    yet); the horses named must differ."""
    if not isinstance(values, (list, tuple)) or len(values) != RESULT_SLOTS:
        raise ValueError(f"results must be a list of exactly {RESULT_SLOTS} horse numbers (0 = not yet)")
    out: List[int] = []
    for value in values:
        h = _int_field(value, "results entry")
        if h < 0 or h > MAX_HORSE:
            raise ValueError(f"results entries must be 0..{MAX_HORSE}")
        out.append(h)
    named = [h for h in out if h]
    if len(set(named)) != len(named):
        raise ValueError("results must name different horses")
    return out


def validate_state(phase: Any, scratched: Any, renum: Any, results: Any
                   ) -> Tuple[int, List[int], List[Tuple[int, int]], List[int]]:
    """Validate a state the way the gateway validates a state line. Raises
    ValueError with a message fit for a 400 response."""
    return (validate_phase(phase), validate_scratched(scratched),
            validate_renum(renum), validate_results(results))


# -----------------------------------------------------------------------------
# Downlink lines. Key order matters: these are byte-exact against the README.
# -----------------------------------------------------------------------------

def _compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))


def build_state_line(rev: int, phase: int, scratched: Sequence[int],
                     renum: Sequence[Sequence[int]], results: Sequence[int]) -> str:
    return _compact({"t": "state", "rev": int(rev), "st": int(phase),
                     "scr": [int(h) for h in scratched],
                     "renum": [[int(f), int(t)] for f, t in renum],
                     "res": [int(h) for h in results]})


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

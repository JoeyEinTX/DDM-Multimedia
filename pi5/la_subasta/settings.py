# la_subasta/settings.py - Admin-tunable settings with runtime overrides
#
# Phase 1.5. The 7 settings listed in config.DEFAULTS can be changed live
# by the admin UI via /api/admin/settings. Reads consult auction_overrides
# first, falling back to the code default. Writes also append to
# settings_audit_log for a full history.
#
# Writes that target a LOCKED_WHEN_OPEN setting while the auction is in a
# running state (anything other than NOT_STARTED) are rejected — Joey can't
# change MAX_RAISE mid-auction and invalidate bids already accepted under
# the old rule.

import re
from typing import Any, Dict, List, Optional, Tuple

from la_subasta import config
from la_subasta.models import get_conn, write_txn
from la_subasta.state_machine import AuctionState, get_state


# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------

class SettingsError(Exception):
    """Raised when a setting read/write fails. `reason` is user-facing."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# -----------------------------------------------------------------------------
# Validators
# -----------------------------------------------------------------------------
#
# Each validator returns (coerced_value, canonical_str). canonical_str is
# what goes into the overrides table; coerced_value is what callers get
# from get_setting(). Raises SettingsError on bad input.

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _v_int(key, value, lo, hi):
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{key} must be an integer")
    if iv < lo or iv > hi:
        raise SettingsError(f"{key} must be between {lo} and {hi}")
    return iv, str(iv)


def _v_max_raise(value):
    return _v_int("MAX_RAISE", value, 1, 50)


def _v_max_horses(value):
    return _v_int("MAX_HORSES_PER_BIDDER", value, 1, 10)


def _v_min_bid(value):
    return _v_int("MIN_BID", value, 1, 20)


def _v_lockdown(value):
    return _v_int("LOCKDOWN_MINUTES_BEFORE_POST", value, 5, 60)


def _v_preset(value):
    s = str(value).strip()
    if s not in config.PAYOUT_PRESET_OPTIONS:
        opts = ", ".join(config.PAYOUT_PRESET_OPTIONS)
        raise SettingsError(
            f"PAYOUT_PRESET must be one of: {opts}"
        )
    return s, s


def _v_fund_label(value):
    s = str(value).strip()
    if not s:
        raise SettingsError("HOUSE_FUND_LABEL cannot be empty")
    if len(s) > 40:
        raise SettingsError("HOUSE_FUND_LABEL must be 40 chars or fewer")
    return s, s


def _v_open_time(value):
    s = str(value).strip()
    if not _TIME_RE.match(s):
        raise SettingsError("AUCTION_OPEN_TIME must be HH:MM (24-hour)")
    return s, s


_VALIDATORS = {
    "MAX_RAISE":                     _v_max_raise,
    "MAX_HORSES_PER_BIDDER":         _v_max_horses,
    "MIN_BID":                       _v_min_bid,
    "LOCKDOWN_MINUTES_BEFORE_POST":  _v_lockdown,
    "PAYOUT_PRESET":                 _v_preset,
    "HOUSE_FUND_LABEL":              _v_fund_label,
    "AUCTION_OPEN_TIME":             _v_open_time,
}


def _coerce_from_str(key: str, raw: str) -> Any:
    """Inverse of the validator — turn stored TEXT back into the typed value."""
    if key in ("MAX_RAISE", "MAX_HORSES_PER_BIDDER", "MIN_BID",
              "LOCKDOWN_MINUTES_BEFORE_POST"):
        return int(raw)
    return raw


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------

def _known(key: str) -> None:
    if key not in config.DEFAULTS:
        raise SettingsError(f"Unknown setting: {key}")


def get_setting(key: str) -> Any:
    """Return the current value: override if present, else the code default."""
    _known(key)
    row = get_conn().execute(
        "SELECT value FROM auction_overrides WHERE setting_key = ?", (key,),
    ).fetchone()
    if row is None:
        return config.DEFAULTS[key]
    return _coerce_from_str(key, row["value"])


def get_all_settings() -> Dict[str, Dict[str, Any]]:
    """
    Return every tunable with its current value, default, and lock status.
    Shape:
      {
        "MAX_RAISE": {
          "value": 5, "default": 5, "is_override": False,
          "locked_when_open": True, "locked_now": False,
        }, ...
      }
    """
    conn = get_conn()
    overrides = {
        r["setting_key"]: r["value"]
        for r in conn.execute(
            "SELECT setting_key, value FROM auction_overrides"
        ).fetchall()
    }
    state = get_state()
    auction_running = state != AuctionState.NOT_STARTED

    out: Dict[str, Dict[str, Any]] = {}
    for key, default in config.DEFAULTS.items():
        if key in overrides:
            value = _coerce_from_str(key, overrides[key])
            is_override = True
        else:
            value = default
            is_override = False
        locked_when_open = key in config.LOCKED_WHEN_OPEN
        out[key] = {
            "value": value,
            "default": default,
            "is_override": is_override,
            "locked_when_open": locked_when_open,
            "locked_now": locked_when_open and auction_running,
        }
    return out


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------

def _is_locked_right_now(key: str) -> bool:
    if key not in config.LOCKED_WHEN_OPEN:
        return False
    return get_state() != AuctionState.NOT_STARTED


def set_setting(key: str, value: Any,
                changed_by: str = "admin") -> Dict[str, Any]:
    """
    Validate, persist, and audit a single setting change.

    Raises SettingsError if:
      - the key isn't a known tunable
      - the value fails validation
      - the setting is locked and the auction is running
    """
    _known(key)

    if _is_locked_right_now(key):
        current_state = get_state().value
        raise SettingsError(
            f"Cannot change {key} while auction state is {current_state}. "
            f"Lock the auction or reset to change."
        )

    coerced, canonical = _VALIDATORS[key](value)

    # Capture old value for the audit entry (default if no override yet)
    old_raw = get_conn().execute(
        "SELECT value FROM auction_overrides WHERE setting_key = ?", (key,),
    ).fetchone()
    if old_raw is None:
        old_value_str = str(config.DEFAULTS[key])
    else:
        old_value_str = old_raw["value"]

    state_at_change = get_state().value

    with write_txn() as conn:
        conn.execute(
            """
            INSERT INTO auction_overrides (setting_key, value, changed_by, changed_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(setting_key) DO UPDATE SET
                value = excluded.value,
                changed_by = excluded.changed_by,
                changed_at = datetime('now')
            """,
            (key, canonical, changed_by),
        )
        conn.execute(
            """
            INSERT INTO settings_audit_log
                (setting_key, old_value, new_value, changed_by,
                 auction_state_at_change)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, old_value_str, canonical, changed_by, state_at_change),
        )
        changed_at = conn.execute(
            "SELECT changed_at FROM auction_overrides WHERE setting_key = ?",
            (key,),
        ).fetchone()["changed_at"]

    return {
        "key": key,
        "old_value": _coerce_from_str(key, old_value_str)
                     if old_value_str is not None else None,
        "new_value": coerced,
        "changed_at": changed_at,
        "changed_by": changed_by,
        "auction_state_at_change": state_at_change,
    }


def reset_to_defaults(changed_by: str = "admin") -> int:
    """
    Wipe all overrides so every setting returns to its code default. Logs
    one audit entry per previously-overridden setting. Returns the number
    of settings that were reset.
    """
    conn = get_conn()
    existing = conn.execute(
        "SELECT setting_key, value FROM auction_overrides"
    ).fetchall()
    state_at_change = get_state().value

    with write_txn() as conn:
        for r in existing:
            key = r["setting_key"]
            conn.execute(
                """
                INSERT INTO settings_audit_log
                    (setting_key, old_value, new_value, changed_by,
                     auction_state_at_change)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, r["value"], str(config.DEFAULTS[key]),
                 changed_by, state_at_change),
            )
        conn.execute("DELETE FROM auction_overrides")

    return len(existing)


# -----------------------------------------------------------------------------
# Audit
# -----------------------------------------------------------------------------

def get_audit_log(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent setting changes, newest first."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    rows = get_conn().execute(
        """
        SELECT id, setting_key, old_value, new_value, changed_by,
               changed_at, auction_state_at_change
          FROM settings_audit_log
         ORDER BY changed_at DESC, id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]

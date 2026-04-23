# la_subasta/models.py - SQLite schema + connection helpers
#
# Owns the la_subasta.db file. Creates its own tables only — never touches
# dashboard tables or data. Uses raw sqlite3 for zero extra dependencies.

import os
import sqlite3
import threading
from contextlib import contextmanager

from la_subasta import config as _config

# Proxy that always reads the *current* config value. Tests patch
# la_subasta.config.DB_PATH before calling init_db(), so default args must
# resolve at call time — not at function definition time.
def _db_path() -> str:
    return _config.DB_PATH

# Single lock for write serialization — SQLite handles concurrent reads fine
# but the undo flow + state transitions benefit from serialized writes.
_write_lock = threading.Lock()


# -----------------------------------------------------------------------------
# Schema (Phase 1)
# -----------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bidders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    emoji        TEXT    NOT NULL,
    identity     TEXT    UNIQUE NOT NULL,
    push_endpoint TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    paid         INTEGER NOT NULL DEFAULT 0,
    paid_at      TEXT,
    paid_amount  REAL,
    event_year   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bidders_event_year ON bidders(event_year);

CREATE TABLE IF NOT EXISTS bids (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bidder_id     INTEGER NOT NULL REFERENCES bidders(id),
    horse_id      INTEGER NOT NULL,
    amount        REAL    NOT NULL,
    bid_time      TEXT    NOT NULL DEFAULT (datetime('now')),
    voided        INTEGER NOT NULL DEFAULT 0,
    voided_reason TEXT,
    event_year    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bids_horse_active
    ON bids(horse_id, voided, event_year);
CREATE INDEX IF NOT EXISTS idx_bids_bidder
    ON bids(bidder_id, voided, event_year);
CREATE INDEX IF NOT EXISTS idx_bids_time
    ON bids(bid_time);

CREATE TABLE IF NOT EXISTS ownership (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id    INTEGER NOT NULL,
    bidder_id   INTEGER NOT NULL REFERENCES bidders(id),
    winning_bid REAL    NOT NULL,
    locked_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    event_year  INTEGER NOT NULL,
    UNIQUE(horse_id, event_year)
);

CREATE TABLE IF NOT EXISTS payouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bidder_id  INTEGER NOT NULL REFERENCES bidders(id),
    horse_id   INTEGER NOT NULL,
    finish     TEXT    NOT NULL CHECK (finish IN ('win','place','show')),
    amount     REAL    NOT NULL,
    paid_out   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    event_year INTEGER NOT NULL,
    UNIQUE(finish, event_year)
);

CREATE TABLE IF NOT EXISTS auction_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    state      TEXT    NOT NULL,
    opens_at   TEXT,
    closes_at  TEXT,
    total_pot  REAL    NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    event_year INTEGER NOT NULL UNIQUE
);

-- La Subasta-local horse state (scratched flag, etc.). The canonical horse
-- metadata lives with the dashboard's RacingDataService; this table only
-- tracks auction-side flags that don't belong on dashboard horses.
CREATE TABLE IF NOT EXISTS horse_state (
    horse_id   INTEGER NOT NULL,
    scratched  INTEGER NOT NULL DEFAULT 0,
    scratched_at TEXT,
    event_year INTEGER NOT NULL,
    PRIMARY KEY (horse_id, event_year)
);

CREATE TABLE IF NOT EXISTS event_years (
    year              INTEGER PRIMARY KEY,
    derby_date        TEXT,
    total_pot         REAL,
    num_bidders       INTEGER,
    winner_horse_name TEXT,
    winner_owner      TEXT,
    biggest_spender   TEXT
);

-- Phase 1.5: admin-tunable settings.
--
-- auction_overrides holds the current value for any setting that has been
-- changed from its code default. Absence of a row = use the default.
-- Values are stored as TEXT and coerced per-setting on read (keeps the
-- schema uniform and lets us mix int/string/float presets).
CREATE TABLE IF NOT EXISTS auction_overrides (
    setting_key TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    changed_by  TEXT,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Append-only history of every setting change. Captures the auction state
-- at the time of the change so we can audit "who changed MAX_RAISE mid-auction".
CREATE TABLE IF NOT EXISTS settings_audit_log (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    setting_key              TEXT    NOT NULL,
    old_value                TEXT,
    new_value                TEXT    NOT NULL,
    changed_by               TEXT,
    changed_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    auction_state_at_change  TEXT
);

CREATE INDEX IF NOT EXISTS idx_settings_audit_changed_at
    ON settings_audit_log(changed_at DESC);
"""


# -----------------------------------------------------------------------------
# Connection / migration
# -----------------------------------------------------------------------------

def _connect(path: str) -> sqlite3.Connection:
    """Open a connection with sane defaults (rows as dicts, FK enforced)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES,
                           check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


# One shared connection per process. SQLite + WAL handles concurrent access
# well enough for the ~20 guest peak load.
_conn: sqlite3.Connection = None  # type: ignore[assignment]


def init_db(path: str = None) -> sqlite3.Connection:
    """
    Create la_subasta.db (if missing) and apply schema.

    Idempotent — safe to call on every server start. Returns the shared
    connection. Does NOT create or touch the dashboard's horses table.
    """
    global _conn, _house_bidder_id
    if path is None:
        path = _db_path()
    # Close any prior connection so file handles don't leak (matters on Windows
    # where an open SQLite handle prevents the file from being deleted).
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = _connect(path)
    _conn.executescript(SCHEMA_SQL)
    _house_bidder_id = None  # force re-lookup after schema apply
    _ensure_house_bidder(_conn)
    return _conn


# -----------------------------------------------------------------------------
# House bidder sentinel
# -----------------------------------------------------------------------------
#
# "The House" owns any horse that received zero bids at lockdown. Representing
# it as a real bidder row keeps the payouts.bidder_id foreign key intact and
# lets JOIN-based queries treat it uniformly. The id is cached after first
# lookup; filters at the API layer hide it from guest-facing bidder lists.

_house_bidder_id: int = None  # type: ignore[assignment]


def _ensure_house_bidder(conn: sqlite3.Connection) -> int:
    """Insert the House row (if missing) and return its id. Called by init_db."""
    global _house_bidder_id
    conn.execute(
        """
        INSERT OR IGNORE INTO bidders (name, emoji, identity, event_year)
        VALUES (?, ?, ?, ?)
        """,
        (_config.HOUSE_BIDDER_NAME, _config.HOUSE_BIDDER_EMOJI,
         _config.HOUSE_BIDDER_IDENTITY, _config.EVENT_YEAR),
    )
    row = conn.execute(
        "SELECT id FROM bidders WHERE identity = ?",
        (_config.HOUSE_BIDDER_IDENTITY,),
    ).fetchone()
    _house_bidder_id = int(row["id"])
    return _house_bidder_id


def house_bidder_id() -> int:
    """Return the House bidder's id. init_db() must have run first."""
    if _house_bidder_id is None:
        _ensure_house_bidder(get_conn())
    return _house_bidder_id


def get_conn() -> sqlite3.Connection:
    """Return the shared connection. init_db() must have been called first."""
    if _conn is None:
        return init_db()
    return _conn


@contextmanager
def write_txn():
    """
    Serialize writes with an explicit transaction. Use for any multi-statement
    write (bid placement, void+re-award, etc.) so readers see a consistent
    view and undo/state transitions don't interleave.
    """
    with _write_lock:
        conn = get_conn()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


# -----------------------------------------------------------------------------
# Test / reset helpers
# -----------------------------------------------------------------------------

def reset_db_for_tests(path: str = None) -> sqlite3.Connection:
    """Drop and recreate all tables. Only for smoke tests / sandbox."""
    global _conn
    if path is None:
        path = _db_path()
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    # Remove db + any WAL/SHM sidecar files so the fresh DB starts empty.
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except OSError:
                pass
    return init_db(path)

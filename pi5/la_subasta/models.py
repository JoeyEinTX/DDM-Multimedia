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

# payout_slots: what the admin did on a paying slot, per finish and the horse
# that finished in it. The ledger itself is not stored: it is derived from SET
# WINNERS' result and the ownership whenever it is read (payouts.py). A row
# holds the horse the admin named to pay the slot in the finisher's place
# (pays_horse_id, when the finisher has no owner: there is no House) and the
# paid-out mark: who was paid, how much (whole pesos), when. Both are tied to
# the finisher, so a correction that changes the slot's horse sets them aside.
_PAYOUT_SLOTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS payout_slots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    finish         TEXT    NOT NULL CHECK (finish IN ('win','place','show')),
    horse_id       INTEGER NOT NULL,
    pays_horse_id  INTEGER,
    paid_bidder_id INTEGER REFERENCES bidders(id),
    paid_amount    INTEGER,
    paid_at        TEXT,
    event_year     INTEGER NOT NULL,
    UNIQUE(finish, horse_id, event_year)
);
"""

# bidders: identity (name + emoji) is unique within an event year, so "Dave K
# 🌮" can join again the next year. cap_exempt: the admin marked this bidder
# (the host, who picks up the horses nobody bid on) as free of the max-horses
# cap (bidding.set_cap_exempt). secret_hash: the SHA-256 of the secret the
# guest's phone was given at join (bidding.register_bidder), which it sends to
# bid or undo as that guest; NULL for a guest from before it, whose phone is
# unknown and joins again. The column list, shared by SCHEMA_SQL and the
# rebuild of an older table whose identity was unique across every year
# (_migrate).
_BIDDERS_COLUMNS_SQL = """(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    emoji        TEXT    NOT NULL,
    identity     TEXT    NOT NULL,
    push_endpoint TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    paid         INTEGER NOT NULL DEFAULT 0,
    paid_at      TEXT,
    paid_amount  REAL,
    event_year   INTEGER NOT NULL,
    cap_exempt   INTEGER NOT NULL DEFAULT 0,
    secret_hash  TEXT,
    UNIQUE(identity, event_year)
)"""

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS bidders {_BIDDERS_COLUMNS_SQL};

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

-- voided: a horse scratched after the lock (La Quiniela's store, see
-- scratches.py). The row stays as the record of what was refunded; nothing
-- owes, pays out or counts in the pot from a voided row.
CREATE TABLE IF NOT EXISTS ownership (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id      INTEGER NOT NULL,
    bidder_id     INTEGER NOT NULL REFERENCES bidders(id),
    winning_bid   REAL    NOT NULL,
    locked_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    event_year    INTEGER NOT NULL,
    voided        INTEGER NOT NULL DEFAULT 0,
    voided_reason TEXT,
    voided_at     TEXT,
    UNIQUE(horse_id, event_year)
);

{_PAYOUT_SLOTS_TABLE_SQL}

CREATE TABLE IF NOT EXISTS auction_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    state      TEXT    NOT NULL,
    opens_at   TEXT,
    closes_at  TEXT,
    total_pot  REAL    NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    event_year INTEGER NOT NULL UNIQUE
);

-- No horse table and no horse_state: the horses, their names and their
-- scratches are La Quiniela's (field.py, scratches.py). A horse_state table
-- left by an older version is not read or written; it is left as it is.

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


# One sqlite3 connection PER THREAD (thread-local). A single shared connection
# is NOT safe under the concurrent access the SocketIO server produces
# (multiple guests + the admin page's polls): concurrent use of one
# connection raises sqlite3 SQLITE_MISUSE ("bad parameter or other API misuse")
# and can return torn rows. WAL mode lets each thread hold its own connection
# to the same file and still see the others' committed writes.
_local = threading.local()


def _open_for_thread(path: str) -> sqlite3.Connection:
    """(Re)open the calling thread's connection at `path`, closing any prior one."""
    old = getattr(_local, "conn", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    conn = _connect(path)
    _local.conn = conn
    _local.path = path
    return conn


def init_db(path: str = None) -> sqlite3.Connection:
    """
    Create la_subasta.db (if missing) and apply schema.

    Idempotent — safe to call on every server start. Returns the calling
    thread's connection. Schema is file-level, so connections opened later on
    other threads (via get_conn) see the same tables. Does NOT create or touch
    the dashboard's horses table.
    """
    if path is None:
        path = _db_path()
    conn = _open_for_thread(path)
    _migrate(conn)
    conn.executescript(SCHEMA_SQL)
    return conn


_OWNERSHIP_VOID_COLUMNS = (
    ("voided", "INTEGER NOT NULL DEFAULT 0"),
    ("voided_reason", "TEXT"),
    ("voided_at", "TEXT"),
)

_BIDDER_COLUMNS = (
    ("cap_exempt", "INTEGER NOT NULL DEFAULT 0"),
    ("secret_hash", "TEXT"),
)

_BIDDER_ALL_COLUMNS = (
    "id", "name", "emoji", "identity", "push_endpoint", "created_at", "paid", "paid_at",
    "paid_amount", "event_year", "cap_exempt", "secret_hash",
)

# What every list, reply and broadcast may show of a guest: all but the secret's hash.
PUBLIC_BIDDER_COLUMNS = ", ".join(c for c in _BIDDER_ALL_COLUMNS if c != "secret_hash")


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns) -> None:
    """ALTER TABLE ADD COLUMN for each column a table from an older version
    lacks (every existing row reads the column's default). A missing table is
    left to the schema."""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if not have:
        return
    for column, ddl in columns:
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _identity_unique_across_years(conn: sqlite3.Connection) -> bool:
    """bidders as it was before names were unique per year: a unique index on
    identity alone (the column's own UNIQUE)."""
    for index in conn.execute("PRAGMA index_list(bidders)").fetchall():
        columns = [r["name"] for r in conn.execute(f'PRAGMA index_info("{index["name"]}")').fetchall()]
        if index["unique"] and columns == ["identity"]:
            return True
    return False


def _rebuild_bidders(conn: sqlite3.Connection) -> None:
    """bidders again with UNIQUE(identity, event_year), every row kept with its
    id. SQLite can't drop a column's UNIQUE, so the table is made anew and put
    in the old one's place, its own way (sqlite.org, ALTER TABLE: other schema
    changes): bids, ownership and payout_slots refer to bidders by name, so with
    foreign keys off the new table is created, filled, the old one dropped and
    the new one renamed, in one transaction. Foreign keys are switched outside
    it (inside, the pragma does nothing) and back on whatever happens. The id
    counter is carried over: after a wipe it is above every id left, and no
    new guest may be given a number a phone from before still holds. The
    index on event_year goes with the old table; SCHEMA_SQL makes it again."""
    cols = ", ".join(_BIDDER_ALL_COLUMNS)
    conn.execute("PRAGMA foreign_keys = OFF;")
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            counter = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'bidders'").fetchone()
            conn.execute(f"CREATE TABLE bidders_new {_BIDDERS_COLUMNS_SQL}")
            conn.execute(f"INSERT INTO bidders_new ({cols}) SELECT {cols} FROM bidders")
            conn.execute("DROP TABLE bidders")
            conn.execute("ALTER TABLE bidders_new RENAME TO bidders")
            if counter is not None:
                conn.execute("DELETE FROM sqlite_sequence WHERE name = 'bidders'")
                conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('bidders', ?)", (counter["seq"],))
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database from an older version up to this schema, before the
    schema itself is applied. Idempotent. The one table dropped, payouts,
    held only a copy of a result entered twice.

    - ownership gains the void columns (a scratch after the lock);
    - bidders gains cap_exempt (the admin's exemption from the max-horses cap)
      and secret_hash (a guest's proof of who it is);
    - payouts, La Subasta's own results store until 2026-10-20 (gap review
      M5), is dropped: the ledger is derived from SET WINNERS' result now
      (payouts.py), and payout_slots, which the schema makes, holds what the
      admin did;
    - an auction left in RACE_COMPLETE or SETTLED, states that went with that
      store, reads as LOCKED (settled is LOCKED with a result);
    - bidders is rebuilt when its identity is unique across every year: the
      rows are copied with their ids (the bids, owners and payout slots
      pointing at them stay right), and a name is then unique within its year.
    """
    _add_missing_columns(conn, "ownership", _OWNERSHIP_VOID_COLUMNS)
    _add_missing_columns(conn, "bidders", _BIDDER_COLUMNS)
    if _identity_unique_across_years(conn):
        _rebuild_bidders(conn)
    conn.execute("DROP TABLE IF EXISTS payouts")
    if conn.execute("PRAGMA table_info(auction_state)").fetchall():
        conn.execute("UPDATE auction_state SET state = 'LOCKED' WHERE state IN ('RACE_COMPLETE', 'SETTLED')")


def get_conn() -> sqlite3.Connection:
    """Return this thread's connection, opening one if needed.

    Each thread gets its own connection (keyed on the current DB path so tests
    that repoint config.DB_PATH transparently reconnect). init_db() applies the
    schema; a fresh thread's first call here opens against the existing file.
    """
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != _db_path():
        conn = _open_for_thread(_db_path())
    return conn


def close_conn() -> None:
    """Close and drop the calling thread's connection, if any.

    Lets worker threads release their file handle deterministically — matters
    on Windows, where a lingering open SQLite handle blocks deleting the file
    in reset_db_for_tests().
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


@contextmanager
def write_txn(prepare=None):
    """
    Serialize writes with an explicit transaction. Use for any multi-statement
    write (bid placement, void+re-award, etc.) so readers see a consistent
    view and undo/state transitions don't interleave.

    prepare: an optional no-argument callable, run once _write_lock is held
    and before BEGIN IMMEDIATE, so while this thread holds no sqlite write
    lock. Read in it whatever sits behind another lock, La Quiniela's store
    above all: a scratch holds the store's lock while it writes this same
    database file, so a transaction that waits for the store with sqlite's
    write lock in hand, and a scratch that waits for sqlite's with the
    store's in hand, wait on each other until sqlite's busy timeout. The
    caller keeps what prepare read (a closure) and uses it in the
    transaction. A scratch recorded after that read is not lost:
    scratches.apply() takes _write_lock too, so it runs once this
    transaction has committed and voids what it wrote on a horse that has
    left the field.
    """
    with _write_lock:
        if prepare is not None:
            prepare()
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
    if path is None:
        path = _db_path()
    # Close the calling thread's connection so the file can be removed on
    # Windows (an open SQLite handle blocks deletion).
    close_conn()
    # Remove db + any WAL/SHM sidecar files so the fresh DB starts empty.
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except OSError:
                pass
    return init_db(path)

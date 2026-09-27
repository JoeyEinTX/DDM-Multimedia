# la_quiniela/models.py - SQLite tables for the La Quiniela bridge
#
# Lives in the app's one database (the file La Subasta uses, see
# la_subasta/config.py DB_PATH) but creates and touches only its own tables:
# lq_cups, telemetry, events, lq_link_state, and the betting board's
# lq_horses, lq_scratches, lq_board and lq_closing. Raw sqlite3, like
# la_subasta/models.
# The bridge owns one connection, shared between its thread and the Flask
# request threads behind a lock.
#
# Protocol v2 (2026-09-27): a cup is known by its MAC and the horse number it
# reports. The v1 slot tables (cups with cup_id, telemetry and events keyed
# by cup_id, the roster in lq_link_state) are migrated by init_schema(): see
# _migrate_v2().

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def default_db_path() -> str:
    """The app's main database: the one La Subasta uses. Resolved at call time
    so a test that repoints la_subasta.config.DB_PATH is honoured."""
    from la_subasta import config as _subasta_config
    return _subasta_config.DB_PATH


def utc_now_iso() -> str:
    """UTC ISO 8601 with a Z, e.g. 2027-05-01T21:14:07Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA_SQL = """
-- The cup cache: every cup ever heard, by MAC, with the horse it last
-- claimed and when it was last heard. It is what lets the admin page say
-- "offline" (seen before, silent now) rather than "no cup" (never seen), and
-- it survives restarts. Nothing decides anything from it.
CREATE TABLE IF NOT EXISTS lq_cups (
    mac        TEXT    PRIMARY KEY,
    horse      INTEGER NOT NULL DEFAULT 0,
    last_seen  TEXT,
    rssi       INTEGER,
    up_rssi    INTEGER,
    last_count INTEGER,
    last_raw   INTEGER,
    online     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    mac         TEXT    NOT NULL,
    horse       INTEGER,
    raw_weight  INTEGER,
    token_count INTEGER,
    seq         INTEGER,
    dropped     INTEGER,
    rssi        INTEGER,
    up_rssi     INTEGER,
    reason      TEXT    NOT NULL CHECK (reason IN ('change', 'heartbeat'))
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_mac_ts ON telemetry(mac, ts);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT    NOT NULL,
    type   TEXT    NOT NULL,
    horse  INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS lq_link_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    state_rev  INTEGER NOT NULL DEFAULT 0,
    state_json TEXT
);
INSERT OR IGNORE INTO lq_link_state (id) VALUES (1);

-- The betting board's own rows (la_quiniela/horses.py): horse names (1..20
-- the field, 21..24 the also-eligibles; 24 is protocol.MAX_HORSE), the
-- scratches, and when betting closes. lq_horses.replaced is a legacy column
-- from the name-swap replacement (kept NULL, never read for anything but a
-- warning); a database created with CHECK (horse BETWEEN 1 AND 20) is
-- rebuilt by init_schema() (see _migrate_lq_horses).
CREATE TABLE IF NOT EXISTS lq_horses (
    horse    INTEGER PRIMARY KEY CHECK (horse BETWEEN 1 AND 24),
    name     TEXT    NOT NULL DEFAULT '',
    replaced TEXT
);

-- One row per scratch: horse `was` left the field and the cup that was
-- `was` now reports horse `now` (a renumber pair the gateway sends until the
-- record is undone), or, with now NULL, `was` was scratched with no
-- replacement (its tokens refunded; its bit in the gateway's scratched mask
-- follows from the record). A table created with now NOT NULL (c70d894,
-- live on DevPi) is rebuilt by init_schema() (see _migrate_lq_scratches).
CREATE TABLE IF NOT EXISTS lq_scratches (
    was INTEGER PRIMARY KEY CHECK (was BETWEEN 1 AND 24),
    now INTEGER CHECK (now IS NULL OR now BETWEEN 1 AND 24)
);

CREATE TABLE IF NOT EXISTS lq_board (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    names_rev INTEGER NOT NULL DEFAULT 0,
    closes_at REAL
);
INSERT OR IGNORE INTO lq_board (id) VALUES (1);

-- The board's figures as they were when betting closed (its `closing`: the
-- pot, the prizes, total_tokens and every horse's tokens, as JSON), or NULL
-- while there are none. Taken on the way into AT_THE_POST (or RUNNING or
-- WINNER when that was skipped), dropped by Reset betting and by PRE_RACE or
-- BETTING_OPEN, so a restart during the draw comes back with them. A table
-- of its own, so the live lq_board keeps its shape.
CREATE TABLE IF NOT EXISTS lq_closing (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    closing TEXT
);
INSERT OR IGNORE INTO lq_closing (id) VALUES (1);
"""

# The shape each table must have if it already exists. A table of the same
# name with different columns is reported, never altered, unless it is the
# protocol v1 shape, which init_schema() migrates.
EXPECTED_COLUMNS: Dict[str, List[str]] = {
    "lq_cups": ["mac", "horse", "last_seen", "rssi", "up_rssi", "last_count", "last_raw", "online"],
    "telemetry": ["id", "ts", "mac", "horse", "raw_weight", "token_count",
                  "seq", "dropped", "rssi", "up_rssi", "reason"],
    "events": ["id", "ts", "type", "horse", "detail"],
    "lq_link_state": ["id", "state_rev", "state_json"],
    "lq_horses": ["horse", "name", "replaced"],
    "lq_scratches": ["was", "now"],
    "lq_board": ["id", "names_rev", "closes_at"],
    "lq_closing": ["id", "closing"],
}

# Protocol v1 (cup slots), live on DevPi until the v2 flash: accepted by
# check_shape() because init_schema() rebuilds them (see _migrate_v2).
V1_COLUMNS: Dict[str, List[str]] = {
    "telemetry": ["id", "ts", "cup_id", "mac", "raw_weight", "token_count",
                  "seq", "dropped", "rssi", "up_rssi", "reason"],
    "events": ["id", "ts", "type", "cup_id", "detail"],
    "lq_link_state": ["id", "state_rev", "state_json", "roster_rev", "roster_json"],
}

# The CHECK the first lq_horses carried (horses 1..20). A table whose CREATE
# statement still says so is rebuilt with the 1..24 CHECK.
_OLD_HORSE_CHECK = re.compile(r"BETWEEN\s+1\s+AND\s+20\b", re.I)


# lq_scratches as c70d894 created it: `now INTEGER NOT NULL ...`; a
# no-replacement scratch needs now NULL, so that shape is rebuilt.
_OLD_SCRATCH_NOW = re.compile(r"\bnow\s+INTEGER\s+NOT\s+NULL", re.I)


class LqDb:
    """One sqlite3 connection, serialised by a lock, usable from any thread."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # timeout is SQLite's busy timeout: La Subasta writes to the same file.
        self.conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    # -- schema ---------------------------------------------------------------

    def _columns(self, table: str) -> List[str]:
        return [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def check_shape(self) -> Optional[str]:
        """Return a description of the first existing table whose columns
        differ from EXPECTED_COLUMNS (or the v1 shape init_schema() knows how
        to rebuild), or None when every table is absent or matches. Nothing
        is altered."""
        with self.lock:
            for table, expected in EXPECTED_COLUMNS.items():
                have = self._columns(table)
                if not have:
                    continue
                if have != expected and have != V1_COLUMNS.get(table):
                    return (f"table {table} already exists with columns {have}, "
                            f"expected {expected}; not altering it")
        return None

    def init_schema(self) -> None:
        """Create the tables that are missing and migrate the ones that
        changed shape. Idempotent."""
        with self.lock:
            self._migrate_v2()
            self._migrate_lq_horses()
            self._migrate_lq_scratches()
            self.conn.executescript(SCHEMA_SQL)

    def _migrate_v2(self) -> List[str]:
        """Protocol v1 kept cups in slots: a cups table keyed by cup_id, a
        telemetry and an events table with a cup_id column, and a roster in
        lq_link_state. v2 knows a cup by its MAC and the horse it reports, so:
        the cups table is dropped (its rows were per slot; lq_cups fills from
        the air within seconds), telemetry and events are rebuilt with a
        horse column in place of cup_id (rows kept, the old slot numbers are
        not horses so the column starts NULL), and lq_link_state loses
        roster_rev and roster_json (state_rev and state_json kept; a v1
        state_json is read for its phase and nothing else). Each step runs
        only when the old shape is found, in its own transaction. Returns the
        steps taken."""
        done: List[str] = []
        cups_cols = self._columns("cups")
        if cups_cols and {"mac", "cup_id"} <= set(cups_cols):     # the v1 slot table, nobody else's
            with self.txn() as conn:
                conn.execute("DROP TABLE cups")
            done.append("cups dropped")
        if "cup_id" in self._columns("telemetry"):
            with self.txn() as conn:
                conn.execute("DROP TABLE IF EXISTS telemetry_new")
                conn.execute(
                    "CREATE TABLE telemetry_new ("
                    "    id          INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "    ts          TEXT    NOT NULL,"
                    "    mac         TEXT    NOT NULL,"
                    "    horse       INTEGER,"
                    "    raw_weight  INTEGER,"
                    "    token_count INTEGER,"
                    "    seq         INTEGER,"
                    "    dropped     INTEGER,"
                    "    rssi        INTEGER,"
                    "    up_rssi     INTEGER,"
                    "    reason      TEXT    NOT NULL CHECK (reason IN ('change', 'heartbeat'))"
                    ")")
                conn.execute("INSERT INTO telemetry_new (id, ts, mac, horse, raw_weight, token_count, seq, "
                             "dropped, rssi, up_rssi, reason) SELECT id, ts, mac, NULL, raw_weight, "
                             "token_count, seq, dropped, rssi, up_rssi, reason FROM telemetry")
                conn.execute("DROP TABLE telemetry")
                conn.execute("ALTER TABLE telemetry_new RENAME TO telemetry")
            done.append("telemetry rebuilt")
        if "cup_id" in self._columns("events"):
            with self.txn() as conn:
                conn.execute("DROP TABLE IF EXISTS events_new")
                conn.execute(
                    "CREATE TABLE events_new ("
                    "    id     INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "    ts     TEXT    NOT NULL,"
                    "    type   TEXT    NOT NULL,"
                    "    horse  INTEGER,"
                    "    detail TEXT"
                    ")")
                conn.execute("INSERT INTO events_new (id, ts, type, horse, detail) "
                             "SELECT id, ts, type, NULL, detail FROM events")
                conn.execute("DROP TABLE events")
                conn.execute("ALTER TABLE events_new RENAME TO events")
            done.append("events rebuilt")
        if "roster_rev" in self._columns("lq_link_state"):
            with self.txn() as conn:
                conn.execute("DROP TABLE IF EXISTS lq_link_state_new")
                conn.execute(
                    "CREATE TABLE lq_link_state_new ("
                    "    id         INTEGER PRIMARY KEY CHECK (id = 1),"
                    "    state_rev  INTEGER NOT NULL DEFAULT 0,"
                    "    state_json TEXT"
                    ")")
                conn.execute("INSERT INTO lq_link_state_new (id, state_rev, state_json) "
                             "SELECT id, state_rev, state_json FROM lq_link_state")
                conn.execute("DROP TABLE lq_link_state")
                conn.execute("ALTER TABLE lq_link_state_new RENAME TO lq_link_state")
            done.append("lq_link_state rebuilt")
        return done

    def _migrate_lq_horses(self) -> bool:
        """lq_horses was created with CHECK (horse BETWEEN 1 AND 20) and is
        live on DevPi with that CHECK. SQLite cannot alter a CHECK, so a table
        whose CREATE statement still says 1 AND 20 is rebuilt (create the new
        shape, copy the rows, drop, rename) in one transaction. Returns
        whether it did. A second call finds the 1..24 CHECK and does nothing;
        an lq_horses_new left by an interrupted run is dropped first."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lq_horses'").fetchone()
        if row is None or not _OLD_HORSE_CHECK.search(row["sql"] or ""):
            return False
        with self.txn() as conn:
            conn.execute("DROP TABLE IF EXISTS lq_horses_new")
            conn.execute(
                "CREATE TABLE lq_horses_new ("
                "    horse    INTEGER PRIMARY KEY CHECK (horse BETWEEN 1 AND 24),"
                "    name     TEXT    NOT NULL DEFAULT '',"
                "    replaced TEXT"
                ")")
            conn.execute("INSERT INTO lq_horses_new (horse, name, replaced) "
                         "SELECT horse, name, replaced FROM lq_horses")
            conn.execute("DROP TABLE lq_horses")
            conn.execute("ALTER TABLE lq_horses_new RENAME TO lq_horses")
        return True

    def _migrate_lq_scratches(self) -> bool:
        """lq_scratches was created with `now INTEGER NOT NULL` (c70d894) and
        is live on DevPi that way; a no-replacement scratch is a row with now
        NULL. SQLite cannot drop a NOT NULL, so a table whose CREATE statement
        still has it is rebuilt (new shape, copy the rows, drop, rename) in
        one transaction. Returns whether it did; idempotent."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'lq_scratches'").fetchone()
        if row is None or not _OLD_SCRATCH_NOW.search(row["sql"] or ""):
            return False
        with self.txn() as conn:
            conn.execute("DROP TABLE IF EXISTS lq_scratches_new")
            conn.execute(
                "CREATE TABLE lq_scratches_new ("
                "    was INTEGER PRIMARY KEY CHECK (was BETWEEN 1 AND 24),"
                "    now INTEGER CHECK (now IS NULL OR now BETWEEN 1 AND 24)"
                ")")
            conn.execute("INSERT INTO lq_scratches_new (was, now) SELECT was, now FROM lq_scratches")
            conn.execute("DROP TABLE lq_scratches")
            conn.execute("ALTER TABLE lq_scratches_new RENAME TO lq_scratches")
        return True

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # -- plumbing -------------------------------------------------------------

    @contextmanager
    def txn(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE;")
            try:
                yield self.conn
                self.conn.execute("COMMIT;")
            except Exception:
                self.conn.execute("ROLLBACK;")
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    # -- link state -----------------------------------------------------------

    def load_link_state(self) -> Dict[str, Any]:
        row = self.query_one("SELECT state_rev, state_json FROM lq_link_state WHERE id = 1")
        if row is None:
            return {"state_rev": 0, "state_json": None}
        return dict(row)

    def save_link_state(self, state_rev: int, state_json: Optional[str]) -> None:
        with self.txn() as conn:
            conn.execute("INSERT OR IGNORE INTO lq_link_state (id) VALUES (1)")
            conn.execute("UPDATE lq_link_state SET state_rev = ?, state_json = ? WHERE id = 1",
                         (int(state_rev), state_json))

    # -- cups -----------------------------------------------------------------

    def load_cups(self) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM lq_cups")

    def upsert_cup(self, mac: str, horse: int, last_seen: Optional[str], rssi: Optional[int],
                   up_rssi: Optional[int], last_count: Optional[int], last_raw: Optional[int],
                   online: bool) -> None:
        with self.txn() as conn:
            conn.execute(
                """INSERT INTO lq_cups (mac, horse, last_seen, rssi, up_rssi, last_count, last_raw, online)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                       horse = excluded.horse, last_seen = excluded.last_seen,
                       rssi = excluded.rssi, up_rssi = excluded.up_rssi,
                       last_count = excluded.last_count, last_raw = excluded.last_raw,
                       online = excluded.online""",
                (mac, int(horse or 0), last_seen, rssi, up_rssi, last_count, last_raw,
                 1 if online else 0))

    def set_cup_online(self, mac: str, online: bool, last_seen: Optional[str] = None) -> None:
        with self.txn() as conn:
            if last_seen is None:
                conn.execute("UPDATE lq_cups SET online = ? WHERE mac = ?", (1 if online else 0, mac))
            else:
                conn.execute("UPDATE lq_cups SET online = ?, last_seen = ? WHERE mac = ?",
                             (1 if online else 0, last_seen, mac))

    def delete_cups(self, mac_prefix: Optional[str] = None) -> int:
        """Drop cup rows, all of them or those whose MAC starts with a prefix
        (the simulator's). Returns how many went."""
        with self.txn() as conn:
            if mac_prefix:
                cur = conn.execute("DELETE FROM lq_cups WHERE mac LIKE ? || '%'", (mac_prefix,))
            else:
                cur = conn.execute("DELETE FROM lq_cups")
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # -- telemetry / events ---------------------------------------------------

    def insert_telemetry(self, ts: str, mac: str, horse: Optional[int], raw_weight: Optional[int],
                         token_count: Optional[int], seq: Optional[int], dropped: Optional[int],
                         rssi: Optional[int], up_rssi: Optional[int], reason: str) -> None:
        with self.txn() as conn:
            conn.execute(
                """INSERT INTO telemetry (ts, mac, horse, raw_weight, token_count, seq,
                                          dropped, rssi, up_rssi, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, mac, horse, raw_weight, token_count, seq, dropped, rssi, up_rssi, reason))

    def insert_event(self, ts: str, type_: str, horse: Optional[int], detail: Optional[str]) -> None:
        with self.txn() as conn:
            conn.execute("INSERT INTO events (ts, type, horse, detail) VALUES (?, ?, ?, ?)",
                         (ts, type_, horse, detail))

    # -- betting board: horse names, scratches, closing time -------------------

    def load_horses(self) -> Dict[int, Dict[str, Optional[str]]]:
        """{horse: {"name": str, "replaced": str | None}} for every row present
        (a horse never named has no row). "replaced" is the legacy name-swap
        column: the store only warns about a row that still has it set."""
        return {int(r["horse"]): {"name": r["name"] or "", "replaced": r["replaced"]}
                for r in self.query("SELECT horse, name, replaced FROM lq_horses")}

    def save_horse(self, horse: int, name: str) -> None:
        """Upsert the name. The legacy replaced column is written NULL."""
        with self.txn() as conn:
            conn.execute(
                "INSERT INTO lq_horses (horse, name, replaced) VALUES (?, ?, NULL) "
                "ON CONFLICT(horse) DO UPDATE SET name = excluded.name, replaced = NULL",
                (int(horse), name or ""))

    def load_scratches(self) -> Dict[int, Optional[int]]:
        """{was: now} for every scratch on record; now is None for a
        no-replacement scratch."""
        return {int(r["was"]): (int(r["now"]) if r["now"] is not None else None)
                for r in self.query("SELECT was, now FROM lq_scratches")}

    def save_scratch(self, was: int, now: Optional[int]) -> None:
        with self.txn() as conn:
            conn.execute(
                "INSERT INTO lq_scratches (was, now) VALUES (?, ?) "
                "ON CONFLICT(was) DO UPDATE SET now = excluded.now",
                (int(was), int(now) if now is not None else None))

    def delete_scratch(self, was: int) -> None:
        with self.txn() as conn:
            conn.execute("DELETE FROM lq_scratches WHERE was = ?", (int(was),))

    def load_board(self) -> Dict[str, Any]:
        row = self.query_one("SELECT names_rev, closes_at FROM lq_board WHERE id = 1")
        if row is None:
            return {"names_rev": 0, "closes_at": None}
        return {"names_rev": int(row["names_rev"] or 0),
                "closes_at": float(row["closes_at"]) if row["closes_at"] is not None else None}

    def save_board(self, names_rev: int, closes_at: Optional[float]) -> None:
        with self.txn() as conn:
            conn.execute("INSERT OR IGNORE INTO lq_board (id) VALUES (1)")
            conn.execute("UPDATE lq_board SET names_rev = ?, closes_at = ? WHERE id = 1",
                         (int(names_rev), float(closes_at) if closes_at is not None else None))

    def load_closing(self) -> Optional[str]:
        """The closing figures as stored (JSON text), or None."""
        row = self.query_one("SELECT closing FROM lq_closing WHERE id = 1")
        return row["closing"] if row is not None else None

    def save_closing(self, closing_json: Optional[str]) -> None:
        with self.txn() as conn:
            conn.execute("INSERT OR IGNORE INTO lq_closing (id) VALUES (1)")
            conn.execute("UPDATE lq_closing SET closing = ? WHERE id = 1", (closing_json,))

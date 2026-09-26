# la_quiniela/models.py - SQLite tables for the La Quiniela bridge
#
# Lives in the app's one database (the file La Subasta uses, see
# la_subasta/config.py DB_PATH) but creates and touches only its own tables:
# cups, telemetry, events, lq_link_state, and the betting board's lq_horses
# and lq_board. Raw sqlite3, like la_subasta/models.
# The bridge owns one connection, shared between its thread and the Flask
# request threads behind a lock.
#
# Cup numbers in these tables are 1-based, the DevPi convention.

import os
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
CREATE TABLE IF NOT EXISTS cups (
    mac        TEXT    PRIMARY KEY,
    cup_id     INTEGER UNIQUE,
    horse      INTEGER,
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
    cup_id      INTEGER,
    mac         TEXT    NOT NULL,
    raw_weight  INTEGER,
    token_count INTEGER,
    seq         INTEGER,
    dropped     INTEGER,
    rssi        INTEGER,
    up_rssi     INTEGER,
    reason      TEXT    NOT NULL CHECK (reason IN ('change', 'heartbeat'))
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_cup_ts ON telemetry(cup_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT    NOT NULL,
    type   TEXT    NOT NULL,
    cup_id INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS lq_link_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    state_rev   INTEGER NOT NULL DEFAULT 0,
    state_json  TEXT,
    roster_rev  INTEGER NOT NULL DEFAULT 0,
    roster_json TEXT
);
INSERT OR IGNORE INTO lq_link_state (id) VALUES (1);

-- The betting board's own rows (la_quiniela/horses.py): horse names, which
-- scratched horse a number now stands in for, and when betting closes.
CREATE TABLE IF NOT EXISTS lq_horses (
    horse    INTEGER PRIMARY KEY CHECK (horse BETWEEN 1 AND 20),
    name     TEXT    NOT NULL DEFAULT '',
    replaced TEXT
);

CREATE TABLE IF NOT EXISTS lq_board (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    names_rev INTEGER NOT NULL DEFAULT 0,
    closes_at REAL
);
INSERT OR IGNORE INTO lq_board (id) VALUES (1);
"""

# The shape each table must have if it already exists. A table of the same
# name with different columns is reported, never altered.
EXPECTED_COLUMNS: Dict[str, List[str]] = {
    "cups": ["mac", "cup_id", "horse", "last_seen", "rssi", "up_rssi",
             "last_count", "last_raw", "online"],
    "telemetry": ["id", "ts", "cup_id", "mac", "raw_weight", "token_count",
                  "seq", "dropped", "rssi", "up_rssi", "reason"],
    "events": ["id", "ts", "type", "cup_id", "detail"],
    "lq_link_state": ["id", "state_rev", "state_json", "roster_rev", "roster_json"],
    "lq_horses": ["horse", "name", "replaced"],
    "lq_board": ["id", "names_rev", "closes_at"],
}


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

    def check_shape(self) -> Optional[str]:
        """Return a description of the first existing table whose columns
        differ from EXPECTED_COLUMNS, or None when every table is absent or
        matches. Nothing is altered."""
        with self.lock:
            for table, expected in EXPECTED_COLUMNS.items():
                rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
                if not rows:
                    continue
                have = [r["name"] for r in rows]
                if have != expected:
                    return (f"table {table} already exists with columns {have}, "
                            f"expected {expected}; not altering it")
        return None

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA_SQL)

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
        row = self.query_one("SELECT state_rev, state_json, roster_rev, roster_json "
                             "FROM lq_link_state WHERE id = 1")
        if row is None:
            return {"state_rev": 0, "state_json": None, "roster_rev": 0, "roster_json": None}
        return dict(row)

    def save_link_state(self, state_rev: int, state_json: Optional[str],
                        roster_rev: int, roster_json: Optional[str]) -> None:
        with self.txn() as conn:
            conn.execute(
                "UPDATE lq_link_state SET state_rev = ?, state_json = ?, "
                "roster_rev = ?, roster_json = ? WHERE id = 1",
                (int(state_rev), state_json, int(roster_rev), roster_json))

    # -- cups -----------------------------------------------------------------

    def load_cups(self) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM cups")

    def upsert_cup(self, mac: str, cup_id: Optional[int], horse: Optional[int],
                   last_seen: Optional[str], rssi: Optional[int], up_rssi: Optional[int],
                   last_count: Optional[int], last_raw: Optional[int], online: bool) -> None:
        with self.txn() as conn:
            if cup_id is not None:
                # cup_id is UNIQUE when not NULL: whoever held it before loses it.
                conn.execute("UPDATE cups SET cup_id = NULL WHERE cup_id = ? AND mac != ?",
                             (cup_id, mac))
            conn.execute(
                """INSERT INTO cups (mac, cup_id, horse, last_seen, rssi, up_rssi,
                                     last_count, last_raw, online)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET
                       cup_id = excluded.cup_id, horse = excluded.horse,
                       last_seen = excluded.last_seen, rssi = excluded.rssi,
                       up_rssi = excluded.up_rssi, last_count = excluded.last_count,
                       last_raw = excluded.last_raw, online = excluded.online""",
                (mac, cup_id, horse, last_seen, rssi, up_rssi, last_count, last_raw,
                 1 if online else 0))

    def rewrite_cup_ids(self, macs_by_cup: Dict[int, str]) -> None:
        """Make the cups table's cup_id column match a roster exactly: MACs no
        longer in the roster become NULL, roster MACs get their cup, rows for
        roster MACs never seen before are created."""
        with self.txn() as conn:
            conn.execute("UPDATE cups SET cup_id = NULL")
            for cup, mac in macs_by_cup.items():
                conn.execute(
                    "INSERT INTO cups (mac, cup_id, online) VALUES (?, ?, 0) "
                    "ON CONFLICT(mac) DO UPDATE SET cup_id = excluded.cup_id",
                    (mac, cup))

    def set_cup_horses(self, horses_by_cup: Dict[int, Optional[int]]) -> None:
        with self.txn() as conn:
            for cup, horse in horses_by_cup.items():
                conn.execute("UPDATE cups SET horse = ? WHERE cup_id = ?", (horse, cup))

    def clear_cup_assignments(self, drop_mac_prefix: Optional[str] = None) -> int:
        """Forget every cup number and horse, for a link reset. Rows whose MAC
        starts with drop_mac_prefix are deleted outright rather than kept with
        a NULL cup_id: they belong to a simulator run and there is no real cup
        behind them. Returns how many rows were deleted."""
        with self.txn() as conn:
            deleted = 0
            if drop_mac_prefix:
                cur = conn.execute("DELETE FROM cups WHERE mac LIKE ? || '%'", (drop_mac_prefix,))
                deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.execute("UPDATE cups SET cup_id = NULL, horse = NULL")
            return deleted

    def set_cup_online(self, mac: str, online: bool, last_seen: Optional[str] = None) -> None:
        with self.txn() as conn:
            if last_seen is None:
                conn.execute("UPDATE cups SET online = ? WHERE mac = ?", (1 if online else 0, mac))
            else:
                conn.execute("UPDATE cups SET online = ?, last_seen = ? WHERE mac = ?",
                             (1 if online else 0, last_seen, mac))

    # -- telemetry / events ---------------------------------------------------

    def insert_telemetry(self, ts: str, cup_id: Optional[int], mac: str, raw_weight: Optional[int],
                         token_count: Optional[int], seq: Optional[int], dropped: Optional[int],
                         rssi: Optional[int], up_rssi: Optional[int], reason: str) -> None:
        with self.txn() as conn:
            conn.execute(
                """INSERT INTO telemetry (ts, cup_id, mac, raw_weight, token_count, seq,
                                          dropped, rssi, up_rssi, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, cup_id, mac, raw_weight, token_count, seq, dropped, rssi, up_rssi, reason))

    def insert_event(self, ts: str, type_: str, cup_id: Optional[int], detail: Optional[str]) -> None:
        with self.txn() as conn:
            conn.execute("INSERT INTO events (ts, type, cup_id, detail) VALUES (?, ?, ?, ?)",
                         (ts, type_, cup_id, detail))

    # -- betting board: horse names, replacements, closing time ---------------

    def load_horses(self) -> Dict[int, Dict[str, Optional[str]]]:
        """{horse: {"name": str, "replaced": str | None}} for every row present
        (a horse never named has no row)."""
        return {int(r["horse"]): {"name": r["name"] or "", "replaced": r["replaced"]}
                for r in self.query("SELECT horse, name, replaced FROM lq_horses")}

    def save_horse(self, horse: int, name: str, replaced: Optional[str]) -> None:
        with self.txn() as conn:
            conn.execute(
                "INSERT INTO lq_horses (horse, name, replaced) VALUES (?, ?, ?) "
                "ON CONFLICT(horse) DO UPDATE SET name = excluded.name, replaced = excluded.replaced",
                (int(horse), name or "", replaced))

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

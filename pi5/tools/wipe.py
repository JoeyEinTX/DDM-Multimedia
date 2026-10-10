#!/usr/bin/env python3
"""Start fresh: clear everything La Quiniela and La Subasta have entered or recorded on
this Pi, after a backup that is checked file by file, so pi5 starts like a fresh install.

On DevPi, with pi5 stopped (RACE_NIGHT.md, "Before the party: start fresh"):

    cd ~/DDM-Multimedia
    python3 pi5/tools/wipe.py --dry-run     the list: what it would clear and keep; changes nothing
    python3 pi5/tools/wipe.py               the same list, then type WIPE: the backup, checked,
                                            then the wipe, then a check that nothing is left
    python3 pi5/tools/wipe.py --restore backups/wipe-2026-10-08-201512
                                            put that backup back exactly (type RESTORE); what is
                                            there now is backed up first

What it clears:

  pi5/data/la_subasta.db     every row of every table. La Quiniela's: the names, the race
                             info, the scratches, the race state line, the figures at the post
                             and the counted pot, the closing time, the cup cache, the cups'
                             readings and the bridge's log. La Subasta's: the guests, bids,
                             owners, payouts, the auction's state, the settings changed from
                             the defaults and their history. Then VACUUM, so nothing deleted
                             stays in the file.
  pi5/data/results.json      the results (and a results.json.tmp a crash left behind)
  pi5/data/quiniela_*.jsonl  the betting log
  pi5/data/race_setup.json   the old Race Setup file (config RACE_SETUP_FILE). Left in place,
                             pi5 would copy its names and post time back in at the next start.
  splash_display/logs/       the splash's own betting log from before pi5 kept it (2026-09-22);
                             nothing reads or writes it now

It empties the tables rather than deleting the database. No table is created or altered, so
each keeps the shape check_shape() passes today (a table of another shape makes the bridge
skip init_schema() and refuse to start: b59382a). The four one-row tables come back at the
next start with what a fresh install gets (INSERT OR IGNORE in la_quiniela/models.py). And
the id counters stay (sqlite_sequence): a phone that joined La Subasta before keeps its old
guest number, and with the counters kept no new guest is ever given that number.

It keeps the code, pi5/config.py, pi5/.env and the splash's config, the LEDs' setup
(animation_registry.json, animation_assignments.json, animation_params.json), the id
counters, the backups, and anything else in pi5/data, which it lists as not recognised.

It refuses while pi5 is running: something answers on pi5's port (FLASK_PORT in
pi5/config.py), or, on Linux, a process has the database open. It says how to stop it: the
ddm-pi5 service (sudo systemctl stop ddm-pi5), or Ctrl+C for a copy started by hand. The
splash keeps no data of its own (it holds pi5's model in memory), so it can stay up.

The backup copies every file it will touch to backups/wipe-<date-time>/ (git-ignored),
checks each copy's size and SHA-256 against the original, and stops before anything is
cleared if one differs. MANIFEST.json, SHA256SUMS and RESTORE.txt (the restore command,
and the same by hand) go with it.

Only the standard library, and nothing of pi5's is imported (pi5/config.py is read on its
own, for the port and the Race Setup file): it runs under any python3, in pi5's venv or not.
Tests: python tools/test_wipe.py (from pi5/).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import runpy
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PI5_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = PI5_DIR.parent
DATA_DIR = PI5_DIR / "data"                           # main.py DATA_DIR
DB_PATH = DATA_DIR / "la_subasta.db"                  # la_subasta/config.py DB_PATH; La Quiniela's tables are in it too
DB_SIDECARS = ("-wal", "-shm", "-journal")
RESULTS_PATH = DATA_DIR / "results.json"              # main.py and la_quiniela/betting.py RESULTS_FILE
RESULTS_TMP_PATH = DATA_DIR / "results.json.tmp"      # main.py save_results writes this, then renames it
LOG_PATTERN = "quiniela_*.jsonl"                      # la_quiniela/betting.py LOG_DIR is pi5/data
SPLASH_LOG_DIR = REPO_DIR / "splash_display" / "logs"
BACKUPS_DIR = REPO_DIR / "backups"

KEPT = (
    ("Configuration", (PI5_DIR / "config.py", PI5_DIR / ".env", REPO_DIR / "splash_display" / "config.py")),
    ("The LEDs' setup", (DATA_DIR / "animation_registry.json", DATA_DIR / "animation_assignments.json",
                         PI5_DIR / "animation_params.json")),
)

CONFIRM_WIPE = "WIPE"
CONFIRM_RESTORE = "RESTORE"
PI5_SERVICE = "ddm-pi5"                   # deploy/ddm-pi5.service
STATE_NAMES = {0: "PRE-RACE", 1: "BETTING OPEN", 2: "FINAL CALL", 3: "AT THE POST",
               4: "RUNNING", 5: "WINNER", 6: "AFTER PARTY"}

# The tables the list names, line by line; any other table's rows are listed as other rows.
LQ_TABLES = ("lq_horses", "lq_race", "lq_scratches", "lq_scratch_names", "lq_board", "lq_link_state",
             "lq_closing", "lq_cups", "telemetry", "events")
LS_TABLES = ("auction_state", "bidders", "bids", "ownership", "payouts", "auction_overrides",
             "settings_audit_log")
# La Quiniela's one-row tables: emptied, each gets its row back at the next start
# (INSERT OR IGNORE), so a row holding only what that gives is no data.
ONE_ROW_TABLES = ("lq_link_state", "lq_board", "lq_closing", "lq_race")

REMINDER = """\
What this can't reach (do it before betting opens):
  - The cups keep their own count. Empty every cup: ADMIN -> Race must say Pot $0.
    A cup that still counts tokens when it is empty: hold its screen -> TARE -> YES.
  - The cups keep their own horse numbers. A cup a replacement scratch moved (9 became
    22) still says 22: hold its screen -> HORSE -> its post -> SET. Walk the mantle:
    every cup shows its post, 1 to 20.
  - The gateway keeps the last race state and the cups it heard until it loses power:
    unplug it from DevPi and plug it back in before you start pi5 again.
  - A phone that joined La Subasta before still remembers its old guest, and its bids
    are refused: on that phone delete the website data for joeydevpi.local, then join
    again.
  - The TV shows the last board until pi5 is back (about 10 s); restart the splash to
    clear it at once."""


class StopError(Exception):
    """Something went wrong before or during a step; the message says what is left."""


# -----------------------------------------------------------------------------
# Where things are
# -----------------------------------------------------------------------------

def pi5_settings() -> Tuple[Dict[str, object], Optional[str]]:
    """pi5's port and Race Setup file as main.py reads them from pi5/config.py (DevPi's
    local copy included), without importing pi5: the file is run on its own, with
    python-dotenv stubbed out if this python has none."""
    settings: Dict[str, object] = {"host": "0.0.0.0", "port": 5000,
                                   "race_setup": DATA_DIR / "race_setup.json"}
    path = PI5_DIR / "config.py"
    if not path.is_file():
        return settings, "pi5/config.py not found: port 5000 and data/race_setup.json assumed"
    stub = None
    try:
        import dotenv  # noqa: F401
    except ImportError:
        stub = types.ModuleType("dotenv")
        stub.load_dotenv = lambda *args, **kwargs: False
        sys.modules["dotenv"] = stub
    try:
        values = runpy.run_path(str(path))
    except Exception as exc:  # a broken config is pi5's problem too; say so, assume the defaults
        return settings, f"pi5/config.py could not be read ({exc}): port 5000 and data/race_setup.json assumed"
    finally:
        if stub is not None:
            sys.modules.pop("dotenv", None)
    settings["host"] = values.get("FLASK_HOST") or "0.0.0.0"
    try:
        settings["port"] = int(values.get("FLASK_PORT") or 5000)
    except (TypeError, ValueError):
        pass
    race_setup = values.get("RACE_SETUP_FILE")         # main.py: getattr(...) or DATA_DIR/race_setup.json
    if race_setup:
        settings["race_setup"] = Path(race_setup).resolve()
    return settings, None


def shown(path: Path) -> str:
    """A path as the repo sees it (pi5/data/results.json), or in full if outside it."""
    try:
        return Path(path).resolve().relative_to(REPO_DIR).as_posix()
    except ValueError:
        return str(path)


def target_of(manifest_path: str) -> Path:
    p = Path(manifest_path)
    return p if p.is_absolute() else REPO_DIR / p


def backup_location(folder: Path, manifest_path: str) -> Path:
    p = Path(manifest_path)
    if p.is_absolute():
        return folder.joinpath("outside", *[part.replace(":", "") for part in p.parts[1:]])
    return folder / p


def db_files() -> List[Path]:
    return [p for p in [DB_PATH] + [Path(str(DB_PATH) + s) for s in DB_SIDECARS] if p.is_file()]


def other_files(settings) -> Dict[str, List[Path]]:
    """Every file outside the database the wipe removes, by store, as it is on disk now."""
    logs = sorted(p for p in DATA_DIR.glob(LOG_PATTERN) if p.is_file()) if DATA_DIR.is_dir() else []
    race_setup = Path(settings["race_setup"])
    splash = sorted(p for p in SPLASH_LOG_DIR.rglob("*") if p.is_file()) if SPLASH_LOG_DIR.is_dir() else []
    return {
        "results": [p for p in (RESULTS_PATH, RESULTS_TMP_PATH) if p.is_file()],
        "logs": logs,
        "race_setup": [race_setup] if race_setup.is_file() else [],
        "splash": splash,
    }


def store_paths(settings) -> List[Path]:
    """Every file the wipe backs up: the database with its -wal, -shm and -journal, then
    the rest. The database itself is emptied; the others are removed."""
    return db_files() + [p for group in other_files(settings).values() for p in group]


def is_store_file(path: Path, settings) -> bool:
    """True for a path the wipe owns: what --restore may write."""
    path = Path(path).resolve()
    if path in {DB_PATH.resolve(), RESULTS_PATH.resolve(), RESULTS_TMP_PATH.resolve(),
                Path(settings["race_setup"]).resolve()}:
        return True
    if path in {Path(str(DB_PATH) + s).resolve() for s in DB_SIDECARS}:
        return True
    if path.parent == DATA_DIR.resolve() and fnmatch.fnmatchcase(path.name, LOG_PATTERN):
        return True
    try:
        path.relative_to(SPLASH_LOG_DIR.resolve())
        return True
    except ValueError:
        return False


# -----------------------------------------------------------------------------
# Is pi5 running?
# -----------------------------------------------------------------------------

def db_holders() -> List[Tuple[int, str, str]]:
    """(pid, command line, path) of every process with the database open. Linux only:
    elsewhere there is no /proc and the port is the only sign."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    targets = {os.path.realpath(str(DB_PATH))} | {os.path.realpath(str(DB_PATH) + s) for s in DB_SIDECARS}
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link in targets:
                try:
                    cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                except OSError:
                    cmd = ""
                found.append((int(entry.name), cmd, link))
                break
    return found


def running_reasons(settings) -> List[str]:
    reasons = []
    port = int(settings["port"])
    hosts = ["127.0.0.1"]
    host = str(settings["host"] or "")
    if host not in ("", "0.0.0.0", "::", "localhost", "127.0.0.1"):
        hosts.append(host)
    for h in hosts:
        try:
            with socket.create_connection((h, port), timeout=0.5):
                reasons.append(f"something answers on pi5's port ({h}:{port})")
                break
        except OSError:
            continue
    for pid, cmd, path in db_holders():
        reasons.append(f"{cmd or 'a process'} (pid {pid}) has {shown(Path(path))} open")
    return reasons


def service_active(service: str) -> bool:
    """True while systemd runs `service` (False where there is no systemctl)."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        state = subprocess.run([systemctl, "is-active", service], capture_output=True, text=True,
                               timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return state in ("active", "activating", "reloading")


def refuse_if_running(settings) -> bool:
    reasons = running_reasons(settings)
    if not reasons:
        return False
    if service_active(PI5_SERVICE):
        print(f"Stop pi5 first: sudo systemctl stop {PI5_SERVICE} "
              f"(start it again afterwards with sudo systemctl start {PI5_SERVICE}).")
    else:
        print("Stop pi5 first: Ctrl+C in its terminal.")
    for reason in reasons:
        print(f"  ({reason})")
    print("Nothing changed.")
    return True


# -----------------------------------------------------------------------------
# What is there
# -----------------------------------------------------------------------------

def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return None


def _one(conn, sql, default=None):
    rows = _rows(conn, sql)
    return rows[0][0] if rows and rows[0] and rows[0][0] is not None else default


def _parse(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return "unreadable"


def read_database(db: Path) -> Dict[str, object]:
    """Everything the list says about the database, read from a copy of it (with its
    -wal: committed data can still be there), so reading changes nothing on disk."""
    tmp = Path(tempfile.mkdtemp(prefix="ddm_wipe_read_"))
    try:
        copy = tmp / db.name
        shutil.copyfile(db, copy)
        for suffix in ("-wal", "-journal"):
            if Path(str(db) + suffix).is_file():
                shutil.copyfile(Path(str(db) + suffix), Path(str(copy) + suffix))
        conn = sqlite3.connect(str(copy))
        try:
            return _survey(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise StopError(f"{shown(db)} could not be read: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _survey(conn) -> Dict[str, object]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    d: Dict[str, object] = {"tables": {t: conn.execute(f'SELECT COUNT(*) FROM "{_q(t)}"').fetchone()[0]
                                       for t in names}}
    d["names"] = [r[0] for r in _rows(conn, "SELECT horse FROM lq_horses WHERE name <> '' ORDER BY horse") or []]
    race = _rows(conn, "SELECT name, year, post_at FROM lq_race WHERE id = 1")
    d["race"] = race[0] if race else None
    d["scratches"] = _rows(conn, "SELECT was, now FROM lq_scratches ORDER BY was") or []
    board = _rows(conn, "SELECT names_rev, closes_at FROM lq_board WHERE id = 1")
    d["names_rev"], d["closes_at"] = board[0] if board else (0, None)
    link = _rows(conn, "SELECT state_rev, state_json FROM lq_link_state WHERE id = 1")
    d["state_rev"], d["state"] = (link[0][0], _parse(link[0][1])) if link else (0, None)
    closing = _rows(conn, "SELECT closing FROM lq_closing WHERE id = 1")
    d["closing"] = _parse(closing[0][0]) if closing else None
    d["cups_sim"] = _one(conn, "SELECT COUNT(*) FROM lq_cups WHERE mac LIKE '02:DD:4D:%'", 0)
    d["auction"] = _rows(conn, "SELECT event_year, state, total_pot FROM auction_state ORDER BY event_year") or []
    d["paid"] = _one(conn, "SELECT COUNT(*) FROM bidders WHERE paid <> 0", 0)
    d["bids_voided"] = _one(conn, "SELECT COUNT(*) FROM bids WHERE voided <> 0", 0)
    d["owners_voided"] = _one(conn, "SELECT COUNT(*) FROM ownership WHERE voided <> 0", 0)
    d["payouts"] = [r[0] for r in _rows(conn, "SELECT finish FROM payouts ORDER BY id") or []]
    d["overrides"] = [r[0] for r in _rows(conn, "SELECT setting_key FROM auction_overrides ORDER BY setting_key") or []]
    return d


def _q(name: str) -> str:
    return name.replace('"', '""')


def one_row_fresh(table: str, d) -> bool:
    """A one-row table whose row holds no more than the next start gives it."""
    if table == "lq_link_state":
        state = d["state"]
        if state is None:
            return d["state_rev"] <= 1
        return (isinstance(state, dict) and d["state_rev"] <= 1 and state.get("phase", 0) == 0
                and not state.get("scratched") and not state.get("renum")
                and not any(state.get("results") or []))
    if table == "lq_board":
        return not d["names_rev"] and d["closes_at"] is None
    if table == "lq_closing":
        return d["closing"] is None
    if table == "lq_race":
        race = d["race"]
        return race is None or (not race[0] and race[1] is None and race[2] is None)
    return False


def db_has_data(d) -> bool:
    for table, n in d["tables"].items():
        if n == 0 or (table in ONE_ROW_TABLES and n == 1 and one_row_fresh(table, d)):
            continue
        return True
    return False


class Survey:
    """What is on disk now, store by store."""

    def __init__(self, settings):
        self.settings = settings
        self.db_files = db_files()
        self.files = other_files(settings)
        self.db = read_database(DB_PATH) if DB_PATH.is_file() else None
        # A -wal with no database beside it would be replayed into the next one pi5 creates.
        self.orphans = self.db_files if self.db is None else []
        self.has_data = (bool(self.orphans) or (self.db is not None and db_has_data(self.db))
                         or any(self.files.values()))

    def to_back_up(self) -> List[Path]:
        return self.db_files + [p for group in self.files.values() for p in group]

    def to_remove(self) -> List[Path]:
        return self.orphans + [p for group in self.files.values() for p in group]

    def unrecognised(self) -> List[Path]:
        if not DATA_DIR.is_dir():
            return []
        known = {p.resolve() for p in self.to_back_up()}
        known |= {p.resolve() for _, paths in KEPT for p in paths}
        return sorted(p for p in DATA_DIR.iterdir() if p.resolve() not in known)


# -----------------------------------------------------------------------------
# The list
# -----------------------------------------------------------------------------

def money(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"${int(x):,}" if x == int(x) else f"${x:,.2f}"


def when(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(ts)


def spans(numbers) -> str:
    """1, 2, 3, 5, 21, 22 -> 1-3, 5, 21-22"""
    out, run = [], []
    for n in sorted(numbers):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def plural(n, word, words=None) -> str:
    return f"{n:,} {word if n == 1 else (words or word + 's')}"


def _count_lines(paths) -> int:
    total = 0
    for p in paths:
        try:
            with open(p, "rb") as f:
                total += sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 16), b""))
        except OSError:
            pass
    return total


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"


def report_lines(s: Survey) -> List[Tuple[str, List[Tuple[str, str]]]]:
    d = s.db or {"tables": {}, "names": [], "race": None, "scratches": [], "names_rev": 0, "closes_at": None,
                 "state_rev": 0, "state": None, "closing": None, "cups_sim": 0, "auction": [], "paid": 0,
                 "bids_voided": 0, "owners_voided": 0, "payouts": [], "overrides": []}
    rows = d["tables"]
    none = "none"

    lq: List[Tuple[str, str]] = []
    lq.append(("Horse names", f"{len(d['names'])} named: {spans(d['names'])}" if d["names"] else none))
    race = d["race"]
    if race and (race[0] or race[1] is not None or race[2] is not None):
        title = f"{race[0] or 'KENTUCKY DERBY'}" + (f" {race[1]}" if race[1] is not None else "")
        lq.append(("Race info", f"{title}, post {when(race[2]) if race[2] is not None else 'not set'}"))
    else:
        lq.append(("Race info", none))
    if d["scratches"]:
        parts = [f"{was} -> {now}" if now is not None else f"{was} no replacement" for was, now in d["scratches"]]
        text = f"{len(d['scratches'])}: {', '.join(parts)}"
        if rows.get("lq_scratch_names"):
            text += f" (and {plural(rows['lq_scratch_names'], 'name')} for Undo)"
        lq.append(("Scratches", text))
    else:
        lq.append(("Scratches", f"none (but {plural(rows['lq_scratch_names'], 'name')} for Undo)"
                   if rows.get("lq_scratch_names") else none))
    lq.append(("Betting closes", when(d["closes_at"]) if d["closes_at"] is not None else none))
    state = d["state"] if isinstance(d["state"], dict) else {}
    phase = state.get("phase", 0) if d["state"] != "unreadable" else None
    lq.append(("Race state", "unreadable" if phase is None else f"{phase} {STATE_NAMES.get(phase, '?')}"))
    closing = d["closing"]
    if closing == "unreadable":
        lq.append(("Figures at the post", "present (unreadable)"))
        lq.append(("Counted pot", "unknown"))
    elif isinstance(closing, dict):
        lq.append(("Figures at the post", f"present: pot {money(closing.get('pot') or 0)}, "
                                          f"{plural(int(closing.get('total_tokens') or 0), 'token')}"))
        counted = closing.get("pot_counted")
        lq.append(("Counted pot", f"{money(counted)} (the hand count)" if counted is not None else none))
    else:
        lq.append(("Figures at the post", none))
        lq.append(("Counted pot", none))
    results = s.files["results"]
    if RESULTS_PATH in results:
        r = _read_json(RESULTS_PATH)
        text = (f"{r.get('win')}, {r.get('place')}, {r.get('show')} (win, place, show)"
                if isinstance(r, dict) else "present (unreadable)")
    else:
        text = none
    if RESULTS_TMP_PATH in results:
        text += " + a half-written results.json.tmp"
    lq.append(("Results", text))
    cups = rows.get("lq_cups", 0)
    lq.append(("Cups heard", (plural(cups, "cup") + (f" ({d['cups_sim']} simulated)" if d["cups_sim"] else ""))
               if cups else none))
    lq.append(("Cup readings", f"{rows.get('telemetry', 0):,}" if rows.get("telemetry") else none))
    lq.append(("Bridge log", plural(rows["events"], "entry", "entries") if rows.get("events") else none))
    logs = s.files["logs"]
    if logs:
        dates = sorted(p.name[len("quiniela_"):-len(".jsonl")] for p in logs)
        span = dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"
        lq.append(("Betting log", f"{plural(len(logs), 'file')}, {plural(_count_lines(logs), 'line')}, {span}"))
    else:
        lq.append(("Betting log", none))
    if s.files["race_setup"]:
        r = _read_json(s.files["race_setup"][0])
        if isinstance(r, dict):
            named = sum(1 for v in (r.get("horses") or {}).values() if str(v or "").strip())
            text = f"present: {plural(named, 'name')}" + (f", post {r['post_time']}" if r.get("post_time") else "")
        else:
            text = "present"
        lq.append(("Old Race Setup file", text))
    else:
        lq.append(("Old Race Setup file", none))

    ls: List[Tuple[str, str]] = []
    if d["auction"]:
        ls.append(("Auction", "; ".join(f"{state} ({year}), pot {money(pot)}" for year, state, pot in d["auction"])))
    else:
        ls.append(("Auction", "not started"))
    ls.append(("Guests", f"{rows['bidders']:,} ({d['paid']} paid)" if rows.get("bidders") else none))
    ls.append(("Bids", f"{rows['bids']:,} ({d['bids_voided']} voided)" if rows.get("bids") else none))
    ls.append(("Owners", (plural(rows["ownership"], "horse") + (f" ({d['owners_voided']} voided)"
                                                                  if d["owners_voided"] else ""))
               if rows.get("ownership") else none))
    ls.append(("Payouts", f"{len(d['payouts'])} ({', '.join(d['payouts'])})" if d["payouts"] else none))
    ls.append(("Settings changed", f"{len(d['overrides'])}: {', '.join(d['overrides'])} (back to the defaults)"
               if d["overrides"] else "none (the defaults)"))
    ls.append(("Settings history", plural(rows["settings_audit_log"], "change")
               if rows.get("settings_audit_log") else none))

    other = [(t, n) for t, n in sorted(rows.items()) if n and t not in LQ_TABLES + LS_TABLES]
    extra: List[Tuple[str, str]] = []
    if other:
        extra.append(("Other tables", ", ".join(f"{t} {plural(n, 'row')}" for t, n in other)))
    counters = []
    if d["names_rev"]:
        counters.append(f"names {d['names_rev']}")
    if d["state_rev"] > 1:
        counters.append(f"race state line {d['state_rev']}")
    if counters:
        extra.append(("Counters", ", ".join(counters) + " (start again at 0 and 1)"))
    if s.orphans:
        extra.append(("Leftovers", ", ".join(shown(p) for p in s.orphans) + " (no database beside them)"))

    splash = s.files["splash"]
    sp = [("Old betting log", f"{plural(len(splash), 'file')}, {plural(_count_lines(splash), 'line')}"
           if splash else none)]

    db_note = "" if s.db is not None else ", not there yet"
    sections = [(f"La Quiniela  ({shown(DB_PATH)}{db_note}, and the files beside it)", lq),
                (f"La Subasta  ({shown(DB_PATH)} too)", ls)]
    if extra:
        sections.append(("The database", extra))
    sections.append((f"The splash  ({shown(SPLASH_LOG_DIR)}/)", sp))
    return sections


def print_report(s: Survey) -> None:
    print("Clears:")
    for title, lines in report_lines(s):
        print(f"\n  {title}")
        for label, value in lines:
            print(f"    {label + ' ':.<22} {value}")
    print("\nKeeps:")
    for label, paths in KEPT:
        present = [shown(p) for p in paths if p.exists()]
        if present:
            print(f"    {label + ' ':.<22} {', '.join(present)}")
    print(f"    {'Id counters ':.<22} so no new guest is given a number a phone from before still holds")
    backups = sorted(p.name for p in BACKUPS_DIR.iterdir() if p.is_dir()) if BACKUPS_DIR.is_dir() else []
    print(f"    {'Backups ':.<22} {shown(BACKUPS_DIR)}/" + (f" ({plural(len(backups), 'so far', 'so far')})"
                                                           if backups else ""))
    print(f"    {'The code ':.<22} pi5's and the splash's, with the splash's slides and trivia")
    for p in s.unrecognised():
        print(f"    {'Not recognised ':.<22} {shown(p)} (left as is)")


# -----------------------------------------------------------------------------
# The backup
# -----------------------------------------------------------------------------

def file_digest(path: Path) -> Tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def copy_file(src: Path, dst: Path) -> Tuple[int, str]:
    """Copy src to dst (synced to disk, times kept); the size and SHA-256 of what was read."""
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, dst)
    return size, h.hexdigest()


def q(text) -> str:
    return shlex.quote(str(text))


def restore_command(folder: Path) -> str:
    return f"cd {q(REPO_DIR)} && python3 pi5/tools/wipe.py --restore {q(shown(folder))}"


def _restore_text(folder: Path, entries, made: str) -> str:
    sidecars = " ".join(q(shown(Path(str(DB_PATH) + s))) for s in DB_SIDECARS)
    lines = [
        f"Made by pi5/tools/wipe.py at {made}: {plural(len(entries), 'file')}, each checked against",
        "the original (size and SHA-256; MANIFEST.json, SHA256SUMS).",
        "",
        "To put them back exactly as they were, with pi5 stopped (what is there now is",
        "backed up first, to another folder in backups/):",
        "",
        f"  {restore_command(folder)}",
        "",
        "The same by hand, pi5 stopped:",
        "",
        f"  cd {q(REPO_DIR)}",
        f"  rm -f {sidecars}",
    ]
    for parent in sorted({str(Path(e["path"]).parent.as_posix()) for e in entries
                          if not Path(e["path"]).is_absolute()} - {"."}):
        lines.append(f"  mkdir -p {q(parent)}")
    for e in entries:
        lines.append(f"  cp -p {q(shown(backup_location(folder, e['path'])))} {q(e['path'])}")
    lines += [
        f"  sha256sum -c {q(shown(folder / 'SHA256SUMS'))}",
        "",
        "By hand, files pi5 wrote after the wipe stay (a new betting log, a new results.json);",
        "the command above backs them up and takes them away.",
        "",
    ]
    return "\n".join(lines)


def _write_synced(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def make_backup(paths: List[Path], kind: str = "wipe", copier=copy_file) -> Tuple[Path, List[dict]]:
    """Copy every path to backups/<kind>-<date-time>/, mirroring where it lives, and check
    each copy, read back from disk, against the original read again. Any difference or
    error removes the half-made folder and raises StopError: nothing has been cleared."""
    now = dt.datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    folder = BACKUPS_DIR / f"{kind}-{stamp}"
    n = 2
    while folder.exists():
        folder = BACKUPS_DIR / f"{kind}-{stamp}-{n}"
        n += 1
    entries: List[dict] = []
    print(f"\nBacking up {plural(len(paths), 'file')} to {shown(folder)}/")
    try:
        folder.mkdir(parents=True)
        for src in paths:
            rel = shown(src)
            dst = backup_location(folder, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            size, digest = copier(src, dst)
            again = file_digest(src)
            copied = file_digest(dst)
            if not (size, digest) == again == copied:
                raise StopError(f"{rel}: the copy is not the same as the original "
                                f"({copied[0]:,} bytes, {copied[1][:12]}... against {again[0]:,} bytes, {again[1][:12]}...)")
            print(f"  {rel}  {size:,} bytes  checked")
            entries.append({"path": rel, "size": size, "sha256": digest})
        made = now.strftime("%Y-%m-%d %H:%M:%S")
        _write_synced(folder / "MANIFEST.json", json.dumps(
            {"made": made, "by": "pi5/tools/wipe.py", "kind": kind, "repo": str(REPO_DIR), "files": entries},
            indent=2) + "\n")
        _write_synced(folder / "SHA256SUMS", "".join(f"{e['sha256']}  {e['path']}\n" for e in entries))
        _write_synced(folder / "RESTORE.txt", _restore_text(folder, entries, made))
    except (OSError, StopError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise StopError(f"The backup failed: {exc}") from exc
    print(f"Backup checked: {plural(len(entries), 'file')}, every copy the same size and SHA-256 as the original.")
    return folder, entries


# -----------------------------------------------------------------------------
# The wipe
# -----------------------------------------------------------------------------

def empty_database(db: Path) -> int:
    """Delete every row of every table in one transaction, then VACUUM. No table is
    created, dropped or altered; sqlite_sequence (the id counters) is left alone."""
    conn = sqlite3.connect(str(db), timeout=5.0, isolation_level=None)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        rows = sum(conn.execute(f'SELECT COUNT(*) FROM "{_q(t)}"').fetchone()[0] for t in tables)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tables:
                conn.execute(f'DELETE FROM "{_q(t)}"')
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        print(f"  {shown(db)}: {plural(len(tables), 'table')} emptied ({plural(rows, 'row')})")
        try:
            conn.execute("VACUUM")
            print(f"  {shown(db)}: compacted (VACUUM), nothing deleted left in the file")
        except sqlite3.Error as exc:
            print(f"  {shown(db)}: emptied, but VACUUM failed ({exc}): the deleted rows' space is still in the file")
        return rows
    finally:
        conn.close()


def remove_files(paths: List[Path]) -> None:
    for p in paths:
        p.unlink()
        print(f"  removed {shown(p)}")
    if SPLASH_LOG_DIR.is_dir():
        for sub in sorted((p for p in SPLASH_LOG_DIR.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            if not any(sub.iterdir()):
                sub.rmdir()
        if not any(SPLASH_LOG_DIR.iterdir()):
            SPLASH_LOG_DIR.rmdir()
            print(f"  removed {shown(SPLASH_LOG_DIR)}/")


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def cmd_wipe(settings, dry_run: bool) -> int:
    if refuse_if_running(settings):
        return 2
    print(f"pi5 is stopped (nothing on port {settings['port']}).\n")
    try:
        survey = Survey(settings)
    except StopError as exc:
        print(f"{exc}\nNothing changed.")
        return 2
    print_report(survey)
    if not survey.has_data:
        print("\nNothing to clear: pi5 already starts like a fresh install. Nothing changed.\n")
        print(REMINDER)
        return 0
    files = survey.to_back_up()
    size = sum(p.stat().st_size for p in files)
    print(f"\nBackup: {plural(len(files), 'file')} ({size:,} bytes) to {shown(BACKUPS_DIR)}/wipe-<date-time>/, "
          "made and checked before anything is cleared.")
    if dry_run:
        print("\nDry run: nothing backed up, nothing changed. Without --dry-run it backs this up, "
              f"asks for {CONFIRM_WIPE} and clears it.\n")
        print(REMINDER)
        return 0
    answer = ask(f"\nType {CONFIRM_WIPE} to back up and clear all of this (anything else stops): ")
    if answer != CONFIRM_WIPE:
        print("Nothing changed.")
        return 1
    if refuse_if_running(settings):                   # started while the list was being read?
        return 2
    try:
        survey = Survey(settings)
        folder, _ = make_backup(survey.to_back_up())
    except StopError as exc:
        print(f"\n{exc}\nNothing was cleared.")
        return 2
    print("\nClearing")
    try:
        if survey.db is not None:
            empty_database(DB_PATH)
        remove_files(survey.to_remove())
        after = Survey(settings)
    except (OSError, sqlite3.Error, StopError) as exc:
        print(f"\nThe wipe stopped: {exc}\nThe backup is complete. To put everything back, with pi5 stopped:\n"
              f"  {restore_command(folder)}")
        return 2
    if after.has_data:
        print("\nNot everything was cleared:")
        print_report(after)
        print(f"\nThe backup is complete. To put everything back, with pi5 stopped:\n  {restore_command(folder)}")
        return 2
    print("\nChecked: nothing left to clear. pi5 will start like a fresh install.")
    print(f"\nTo undo this (puts every file back exactly as it was), with pi5 stopped:\n"
          f"  {restore_command(folder)}\n"
          f"(the same by hand: {shown(folder / 'RESTORE.txt')})\n")
    print(REMINDER)
    return 0


# -----------------------------------------------------------------------------
# The restore
# -----------------------------------------------------------------------------

def cmd_restore(settings, where: str) -> int:
    if refuse_if_running(settings):
        return 2
    folder = Path(where)
    if not folder.is_absolute():
        folder = (Path.cwd() / folder) if (Path.cwd() / folder).exists() else (REPO_DIR / folder)
    try:
        manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
        entries = [{"path": str(e["path"]), "size": int(e["size"]), "sha256": str(e["sha256"])}
                   for e in manifest["files"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"{shown(folder)} is not a backup this tool made ({exc}). Nothing changed.")
        return 2
    problems = []
    for e in entries:
        if not is_store_file(target_of(e["path"]), settings):
            problems.append(f"{e['path']}: not a file this tool backs up")
            continue
        copy = backup_location(folder, e["path"])
        if not copy.is_file() or file_digest(copy) != (e["size"], e["sha256"]):
            problems.append(f"{e['path']}: the copy in the backup is missing or not as it was made")
    if problems:
        print(f"The backup {shown(folder)} does not check out:")
        for p in problems:
            print(f"  {p}")
        print("Nothing changed.")
        return 2
    print(f"Backup {shown(folder)}: {plural(len(entries), 'file')}, made {manifest.get('made', '?')}, "
          "every copy checked (size and SHA-256).")
    now = store_paths(settings)
    print("\nPuts back, exactly as they were:")
    for e in entries:
        print(f"  {e['path']}  {e['size']:,} bytes")
    if now:
        print("\nTakes away what is there now, after backing it up:")
        for p in now:
            print(f"  {shown(p)}")
    answer = ask(f"\nType {CONFIRM_RESTORE} to put the backup back (anything else stops): ")
    if answer != CONFIRM_RESTORE:
        print("Nothing changed.")
        return 1
    if refuse_if_running(settings):
        return 2
    now = store_paths(settings)
    try:
        before = make_backup(now, kind="before-restore")[0] if now else None
    except StopError as exc:
        print(f"\n{exc}\nNothing changed.")
        return 2
    print("\nPutting back")
    try:
        if now:
            remove_files(now)
        for e in entries:
            target = target_of(e["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_file(backup_location(folder, e["path"]), target)
            if file_digest(target) != (e["size"], e["sha256"]):
                raise StopError(f"{e['path']}: not the same as the backup after copying")
            print(f"  {e['path']}  checked")
    except (OSError, StopError) as exc:
        print(f"\nThe restore stopped: {exc}")
        if before is not None:
            print(f"What was there before it is in {shown(before)}/:\n  {restore_command(before)}")
        return 2
    print(f"\nRestored: {plural(len(entries), 'file')}, each the same size and SHA-256 as in the backup."
          + (f" What was there before is in {shown(before)}/." if before is not None else "")
          + f" Start pi5 again (sudo systemctl start {PI5_SERVICE}).")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="wipe.py", description="Back up, then clear everything La Quiniela and La Subasta have entered "
                                    "or recorded on this Pi, so pi5 starts like a fresh install.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="show what it would clear and keep; change nothing")
    group.add_argument("--restore", metavar="BACKUP", help="put a backup (backups/wipe-...) back exactly")
    args = parser.parse_args(argv)
    settings, note = pi5_settings()
    print(f"DDM start fresh: {REPO_DIR}")
    if note:
        print(f"({note})")
    if args.restore:
        return cmd_restore(settings, args.restore)
    return cmd_wipe(settings, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

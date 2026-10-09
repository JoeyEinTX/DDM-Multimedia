#!/usr/bin/env python3
"""tools/wipe.py, tested on scratch copies of pi5.

Each scratch is a copy of pi5's code and config with its own port, on loopback, with no
weather, tote, LED controller or gateway port. It is seeded with something in every
store the wipe clears: La Quiniela's and La Subasta's tables (some rows only in the -wal,
as a pi5 that was killed leaves them), results.json and its .tmp, two betting logs, the
old Race Setup file and the splash's old log. It also holds the files the wipe keeps. The
tool runs from the scratch copy exactly as it runs on DevPi, and pi5 runs there as
`python main.py`.

    cd pi5
    python tools/test_wipe.py            (--keep leaves the scratch folders for a look)
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass

PI5_SRC = Path(__file__).resolve().parent.parent
KEEP = "--keep" in sys.argv
ENV = {k: v for k, v in os.environ.items() if not k.startswith("DDM_")}
ENV.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

SCRATCH_CONFIG = """
# test_wipe.py: a scratch pi5 on loopback, its own port, nothing external
FLASK_HOST = '127.0.0.1'
FLASK_PORT = {port}
FLASK_DEBUG = False
ESP32_IP = '127.0.0.1'
ESP32_PORT = 9
WEATHER_API_KEY = ''
ANTHROPIC_API_KEY = ''
TOTE_ENABLED = False
LQ_SERIAL_PORT = ''
"""

MARK = "ZZWIPE"            # in every name and log line seeded: none may be left in the wiped database's bytes

# -----------------------------------------------------------------------------
# Tiny test runner (no pytest dependency), as la_quiniela/test_smoke.py
# -----------------------------------------------------------------------------

_results = []


def _check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((status, name, detail))
    marker = "[OK]" if condition else "[XX]"
    print(f"  {marker} {name}" + (f"  -- {detail}" if detail and not condition else ""))
    return condition


def _run(name, fn):
    print(f"\n=== {name} ===")
    try:
        fn()
    except Exception as exc:
        traceback.print_exc()
        _check(f"{name} (uncaught exception)", False, str(exc))


# -----------------------------------------------------------------------------
# Scratch trees
# -----------------------------------------------------------------------------

_BASES = []
_PORTS = {}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_tree(seed=True) -> Path:
    """<tmp>/DDM-Multimedia with pi5 (code, config, data/animation_registry.json) and
    splash_display/config.py; seeded with something in every store unless seed=False."""
    base = Path(tempfile.mkdtemp(prefix="ddm_wipe_test_"))
    _BASES.append(base)
    root = base / "DDM-Multimedia"
    pi5 = root / "pi5"
    shutil.copytree(PI5_SRC, pi5, ignore=shutil.ignore_patterns(
        "data", "__pycache__", ".env", "animation_params.json", "*.db", "*.db-wal", "*.db-shm"))
    (pi5 / "data").mkdir()
    shutil.copy2(PI5_SRC / "data" / "animation_registry.json", pi5 / "data" / "animation_registry.json")
    port = free_port()
    _PORTS[root] = port
    with open(pi5 / "config.py", "a", encoding="utf-8") as f:
        f.write(SCRATCH_CONFIG.format(port=port))
    (root / "splash_display").mkdir()
    shutil.copy2(PI5_SRC.parent / "splash_display" / "config.py", root / "splash_display" / "config.py")
    if seed:
        seed_stores(root)
    return root


# pi5's own schema first (import main), then rows. Part A is committed and checkpointed
# into the file; part B is left only in the -wal (os._exit, no close), as a killed pi5 leaves it.
SEED_SCRIPT = r'''
import json, os, sqlite3, sys
db, mark = sys.argv[1], sys.argv[2]
ts = "2026-10-01T20:00:00Z"
c = sqlite3.connect(db)
for n in range(1, 22):
    c.execute("INSERT INTO lq_horses (horse, name) VALUES (?, ?)", (n, f"{mark} HORSE {n}"))
c.execute("UPDATE lq_race SET name='TEST DERBY', year=2027, post_at=1809208620 WHERE id=1")
c.execute("INSERT INTO lq_scratches (was, now) VALUES (9, 22)")
c.execute("INSERT INTO lq_scratch_names (was, now, name_before, name_set) VALUES (9, 22, '', ?)", (f"{mark} OCELLI",))
c.execute("UPDATE lq_board SET names_rev=7, closes_at=1809205000 WHERE id=1")
c.execute("UPDATE lq_link_state SET state_rev=33, state_json=? WHERE id=1",
          (json.dumps({"phase": 5, "scratched": [9, 14], "renum": [[9, 22]], "results": [7, 3, 12]}),))
c.execute("UPDATE lq_closing SET closing=? WHERE id=1", (json.dumps(
    {"pot": 34.0, "prizes": {"win": 20.4, "place": 8.5, "show": 5.1}, "total_tokens": 34,
     "horses": {"7": {"tokens": 5}}, "at": 1809209000, "pot_counted": 36}),))
for mac, horse, count in (("AA:BB:CC:00:00:01", 7, 5), ("AA:BB:CC:00:00:02", 3, 2), ("02:DD:4D:00:00:01", 12, 0)):
    c.execute("INSERT INTO lq_cups (mac, horse, last_seen, rssi, up_rssi, last_count, last_raw, online) "
              "VALUES (?, ?, ?, -50, -55, ?, 12345, 1)", (mac, horse, ts, count))
for i in range(5):
    c.execute("INSERT INTO telemetry (ts, mac, horse, raw_weight, token_count, seq, dropped, rssi, up_rssi, reason) "
              "VALUES (?, 'AA:BB:CC:00:00:01', 7, 1000, ?, ?, 0, -50, -55, 'change')", (ts, i, i))
for i in range(4):
    c.execute("INSERT INTO events (ts, type, horse, detail) VALUES (?, 'cup_online', 7, ?)",
              (ts, json.dumps({"note": f"{mark}-EVENT-{i}"})))
for i, paid in ((1, 1), (2, 0)):
    c.execute("INSERT INTO bidders (name, emoji, identity, paid, event_year) VALUES (?, 'X', ?, ?, 2026)",
              (f"{mark} GUEST {i}", f"{mark} GUEST {i} X", paid))
for bidder, horse, amount, voided in ((1, 7, 5, 0), (2, 7, 6, 0), (1, 3, 4, 1), (2, 12, 3, 0)):
    c.execute("INSERT INTO bids (bidder_id, horse_id, amount, voided, event_year) VALUES (?, ?, ?, ?, 2026)",
              (bidder, horse, amount, voided))
c.execute("INSERT INTO ownership (horse_id, bidder_id, winning_bid, event_year) VALUES (7, 2, 6, 2026)")
c.execute("INSERT INTO ownership (horse_id, bidder_id, winning_bid, event_year) VALUES (12, 2, 3, 2026)")
for finish, horse in (("win", 7), ("place", 3), ("show", 12)):
    c.execute("INSERT INTO payouts (bidder_id, horse_id, finish, amount, event_year) VALUES (2, ?, ?, 10, 2026)",
              (horse, finish))
c.execute("INSERT INTO auction_state (state, total_pot, event_year) VALUES ('LOCKED', 57, 2026)")
c.execute("INSERT INTO auction_overrides (setting_key, value, changed_by) VALUES ('MAX_RAISE', '10', 'test')")
c.execute("INSERT INTO settings_audit_log (setting_key, old_value, new_value, changed_by) "
          "VALUES ('MAX_RAISE', '5', '10', 'test')")
c.execute("INSERT INTO event_years (year, total_pot) VALUES (2025, 99)")
c.execute("CREATE TABLE horse_state (horse_id INTEGER NOT NULL, scratched INTEGER NOT NULL DEFAULT 0, "
          "scratched_at TEXT, event_year INTEGER NOT NULL, PRIMARY KEY (horse_id, event_year))")
c.execute("INSERT INTO horse_state VALUES (4, 1, ?, 2026)", (ts,))
c.execute("INSERT INTO horse_state VALUES (5, 0, NULL, 2026)")
c.commit()
c.close()
c = sqlite3.connect(db)
c.execute("PRAGMA wal_autocheckpoint = 0")
c.execute("INSERT INTO lq_horses (horse, name) VALUES (22, ?)", (f"{mark} WAL HORSE 22",))
c.execute("INSERT INTO lq_scratches (was, now) VALUES (14, NULL)")
c.execute("INSERT INTO bidders (name, emoji, identity, event_year) VALUES (?, 'X', ?, 2026)",
          (f"{mark} WAL GUEST", f"{mark} WAL GUEST X"))
c.execute("INSERT INTO bids (bidder_id, horse_id, amount, event_year) VALUES (3, 3, 2, 2026)")
c.commit()
os._exit(0)
'''


def seed_stores(root: Path) -> None:
    pi5 = root / "pi5"
    data = pi5 / "data"
    made = subprocess.run([sys.executable, "-c", "import main"], cwd=pi5, env=ENV,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert made.returncode == 0, made.stdout + made.stderr
    seeded = subprocess.run([sys.executable, "-c", SEED_SCRIPT, str(data / "la_subasta.db"), MARK],
                            env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    assert (data / "la_subasta.db-wal").stat().st_size > 0, "the seed left nothing in the -wal"
    (data / "results.json").write_text(json.dumps(
        {"win": 7, "place": 3, "show": 12, "timestamp": "2026-10-01T21:00:00"}), encoding="utf-8")
    (data / "results.json.tmp").write_text('{"win": 7, "pla', encoding="utf-8")
    (data / "quiniela_2026-10-01.jsonl").write_text(
        "".join(json.dumps({"ts": i, "note": f"{MARK}-LOG"}) + "\n" for i in range(3)), encoding="utf-8")
    (data / "quiniela_2026-10-02.jsonl").write_text(
        "".join(json.dumps({"ts": i, "note": f"{MARK}-LOG"}) + "\n" for i in range(2)), encoding="utf-8")
    (data / "race_setup.json").write_text(json.dumps(
        {"race_name": "OLD RACE", "post_time": "18:57",
         "horses": {str(n): f"{MARK} OLD {n}" for n in range(1, 21)}, "odds": {}}), encoding="utf-8")
    logs = root / "splash_display" / "logs"
    logs.mkdir(parents=True)
    (logs / "quiniela_2026-09-22.jsonl").write_text(f'{{"a": "{MARK}"}}\n{{"b": 2}}\n', encoding="utf-8")
    # Kept
    (pi5 / ".env").write_text("# scratch .env: the wipe keeps it\n", encoding="utf-8")
    (data / "animation_assignments.json").write_text('{"welcome": "RAINBOW"}', encoding="utf-8")
    (pi5 / "animation_params.json").write_text('{"masterBrightness": 100}', encoding="utf-8")
    (data / "notes.txt").write_text("not the wipe's\n", encoding="utf-8")


STORE_FILES = ("pi5/data/la_subasta.db", "pi5/data/la_subasta.db-wal", "pi5/data/la_subasta.db-shm",
               "pi5/data/results.json", "pi5/data/results.json.tmp", "pi5/data/quiniela_2026-10-01.jsonl",
               "pi5/data/quiniela_2026-10-02.jsonl", "pi5/data/race_setup.json",
               "splash_display/logs/quiniela_2026-09-22.jsonl")
KEPT_FILES = ("pi5/config.py", "pi5/.env", "splash_display/config.py", "pi5/data/animation_registry.json",
              "pi5/data/animation_assignments.json", "pi5/animation_params.json", "pi5/data/notes.txt")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict:
    """Every file under the scratch root (bytecode aside): relative path -> SHA-256."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            out[p.relative_to(root).as_posix()] = sha(p)
    return out


def diff(a: dict, b: dict) -> str:
    changed = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
    return (f"gone {sorted(a.keys() - b.keys())} new {sorted(b.keys() - a.keys())} changed {changed}"
            if a != b else "")


def wipe(root: Path, *args, answer=None) -> subprocess.CompletedProcess:
    """The tool, run from the scratch copy as on DevPi; answer is what is typed (None: no input)."""
    return subprocess.run([sys.executable, str(root / "pi5" / "tools" / "wipe.py"), *args],
                          input=answer, stdin=None if answer is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", env=ENV, cwd=root, timeout=300)


def read_db(path: Path) -> dict:
    """Every table's rows and sqlite_sequence, read from a copy (with its -wal) so the
    files themselves are never touched."""
    tmp = Path(tempfile.mkdtemp(prefix="ddm_wipe_test_read_"))
    try:
        shutil.copyfile(path, tmp / "db")
        if Path(str(path) + "-wal").is_file():
            shutil.copyfile(Path(str(path) + "-wal"), tmp / "db-wal")
        c = sqlite3.connect(str(tmp / "db"))
        try:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            out = {t: c.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
        finally:
            c.close()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def backups(root: Path):
    d = root / "backups"
    return sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


def load_wipe(root: Path):
    """The scratch copy's wipe.py as a module: its paths are the scratch's."""
    spec = importlib.util.spec_from_file_location(f"wipe_{abs(hash(str(root)))}", root / "pi5" / "tools" / "wipe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Pi5:
    """`python main.py` in a scratch copy, until the with block ends."""

    def __init__(self, root: Path):
        self.root = root
        self.port = _PORTS[root]
        self.log = root.parent / f"pi5-{time.time_ns()}.log"

    def __enter__(self):
        self.out = open(self.log, "wb")
        self.proc = subprocess.Popen([sys.executable, "main.py"], cwd=self.root / "pi5", env=ENV,
                                     stdout=self.out, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("pi5 exited: " + self.log.read_text(encoding="utf-8", errors="replace")[-2000:])
            try:
                self.get("/api/quiniela")
                return self
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("pi5 did not come up")

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.out.close()


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

def test_paths_are_pi5s():
    """The files the tool looks at are the ones pi5 itself uses, read from pi5's own
    modules in a scratch copy; and every table pi5 creates is one the list names."""
    root = make_tree(seed=False)
    probe = r'''
import contextlib, io, json, os, sys
sys.path.insert(0, os.getcwd())
with contextlib.redirect_stdout(io.StringIO()):
    import main, config
    from la_subasta import config as la_config
    from la_quiniela import betting
print("PROBE " + json.dumps({
    "db": la_config.DB_PATH, "results": [main.RESULTS_FILE, str(betting.RESULTS_FILE)],
    "data": [main.DATA_DIR, str(betting.LOG_DIR)], "race_setup": main.RACE_SETUP_FILE,
    "port": config.FLASK_PORT, "kept": [config.PARAMS_FILE, config.ANIMATION_REGISTRY_FILE,
                                        config.ANIMATION_ASSIGNMENTS_FILE]}))
'''
    run = subprocess.run([sys.executable, "-c", probe], cwd=root / "pi5", env=ENV, capture_output=True,
                         text=True, encoding="utf-8", timeout=120)
    line = [ln for ln in run.stdout.splitlines() if ln.startswith("PROBE ")]
    if not _check("pi5's own paths read in the scratch copy", run.returncode == 0 and line, run.stderr[-500:]):
        return
    p = json.loads(line[0][len("PROBE "):])
    w = load_wipe(root)
    with contextlib.redirect_stdout(io.StringIO()):
        settings, note = w.pi5_settings()

    def same(a, b):
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))

    _check("the database is la_subasta.config.DB_PATH", same(w.DB_PATH, p["db"]), f"{w.DB_PATH} vs {p['db']}")
    _check("results.json is main's and La Quiniela's RESULTS_FILE",
           all(same(w.RESULTS_PATH, r) for r in p["results"]), str(p["results"]))
    _check("the betting log's folder is main's DATA_DIR and betting.LOG_DIR",
           all(same(w.DATA_DIR, d) for d in p["data"]), str(p["data"]))
    _check("the Race Setup file is main.RACE_SETUP_FILE", same(settings["race_setup"], p["race_setup"]),
           f"{settings['race_setup']} vs {p['race_setup']}")
    _check("the port is pi5's FLASK_PORT, read from the scratch config", settings["port"] == p["port"] and not note,
           f"{settings} {note}")
    kept = [str(x) for _, paths in w.KEPT for x in paths]
    _check("the LEDs' files pi5 names are in the kept list",
           all(any(same(k, x) for x in kept) for k in p["kept"]), str(p["kept"]))
    betting_src = (root / "pi5" / "la_quiniela" / "betting.py").read_text(encoding="utf-8")
    _check("the betting log is still named quiniela_<date>.jsonl", 'f"quiniela_{date.today().isoformat()}.jsonl"'
           in betting_src)
    tables = read_db(w.DB_PATH)
    named = set(w.LQ_TABLES) | set(w.LS_TABLES) | {"event_years", "sqlite_sequence"}
    _check("every table pi5 creates is one the list names (or event_years, never written)",
           set(tables) <= named, str(sorted(set(tables) - named)))
    _check("La Quiniela's one-row tables are exactly the ones pi5 seeds",
           {t for t, rows in tables.items() if rows} == set(w.ONE_ROW_TABLES),
           str({t: len(r) for t, r in tables.items() if r}))


def test_dry_run_changes_nothing():
    root = make_tree()
    before = snapshot(root)
    run = wipe(root, "--dry-run")
    out = run.stdout
    _check("--dry-run exits 0", run.returncode == 0, out[-800:] + run.stderr[-800:])
    _check("--dry-run changes nothing on disk (every file's SHA-256)", snapshot(root) == before,
           diff(before, snapshot(root)))
    _check("--dry-run makes no backup", not (root / "backups").exists())
    _check("--dry-run asks for nothing", "Type WIPE" not in out)
    _check("--dry-run says it changed nothing", "Dry run: nothing backed up, nothing changed." in out)
    wanted = [
        "Horse names .......... 22 named: 1-22",              # 22 is only in the -wal
        "Race info ............ TEST DERBY 2027, post 2027-05-",
        "Scratches ............ 2: 9 -> 22, 14 no replacement (and 1 name for Undo)",
        "Betting closes ....... 2027-05-0",
        "Race state ........... 5 WINNER",
        "Figures at the post .. present: pot $34, 34 tokens",
        "Counted pot .......... $36 (the hand count)",
        "Results .............. 7, 3, 12 (win, place, show) + a half-written results.json.tmp",
        "Cups heard ........... 3 cups (1 simulated)",
        "Cup readings ......... 5",
        "Bridge log ........... 4 entries",
        "Betting log .......... 2 files, 5 lines, 2026-10-01 to 2026-10-02",
        "Old Race Setup file .. present: 20 names, post 18:57",
        "Auction .............. LOCKED (2026), pot $57",
        "Guests ............... 3 (1 paid)",
        "Bids ................. 5 (1 voided)",
        "Owners ............... 2 horses",
        "Payouts .............. 3 (win, place, show)",
        "Settings changed ..... 1: MAX_RAISE (back to the defaults)",
        "Settings history ..... 1 change",
        "Other tables ......... event_years 1 row, horse_state 2 rows",
        "Counters ............. names 7, race state line 33",
        "Old betting log ...... 1 file, 2 lines",
        "Configuration ........ pi5/config.py, pi5/.env, splash_display/config.py",
        "The LEDs' setup ...... pi5/data/animation_registry.json, pi5/data/animation_assignments.json, "
        "pi5/animation_params.json",
        "Not recognised ....... pi5/data/notes.txt (left as is)",
        "Backup: 9 files",
        "The cups keep their own count",
        "unplug it from DevPi",
        "delete the website data",
    ]
    for line in wanted:
        _check(f"the list says: {line}", line in out)
    _check("the list names no seeded name or log line (counts only)", MARK not in out)


def test_wrong_answer_changes_nothing():
    root = make_tree()
    before = snapshot(root)
    for answer in ("wipe\n", "Wipe\n", "WIPE \n", " WIPE\n", "WIPE!\n", "yes\n", "\n", "", None):
        run = wipe(root, answer=answer)
        _check(f"typing {answer!r}: exit 1, 'Nothing changed.'",
               run.returncode == 1 and run.stdout.rstrip().endswith("Nothing changed."),
               f"exit {run.returncode}: {run.stdout[-300:]}{run.stderr[-300:]}")
        _check(f"typing {answer!r}: every file as it was, no backup",
               snapshot(root) == before and not (root / "backups").exists(), diff(before, snapshot(root)))


def test_wipe_backup_and_restore():
    root = make_tree()
    originals = {f: sha(root / f) for f in STORE_FILES}
    kept = {f: sha(root / f) for f in KEPT_FILES}
    seq_before = read_db(root / "pi5/data/la_subasta.db")["sqlite_sequence"]

    run = wipe(root, answer="WIPE\n")
    out = run.stdout
    _check("WIPE: exit 0", run.returncode == 0, out[-1500:] + run.stderr[-1500:])
    _check("WIPE: the backup is checked before anything is cleared",
           out.find("Backup checked: 9 files") != -1 and out.find("Backup checked") < out.find("Clearing"))
    _check("WIPE: says it checked that nothing is left", "Checked: nothing left to clear." in out)
    _check("WIPE: prints the restore command", "python3 pi5/tools/wipe.py --restore backups/wipe-" in out)
    _check("WIPE: ends with the reminder", "The cups keep their own count" in out
           and out.rstrip().endswith("clear it at once."))

    db = root / "pi5/data/la_subasta.db"
    tables = read_db(db)
    full = {t: len(r) for t, r in tables.items() if r and t != "sqlite_sequence"}
    _check("every table is empty, legacy ones too", not full and "horse_state" in tables, str(full))
    _check("the id counters are kept (sqlite_sequence)", tables["sqlite_sequence"] == seq_before,
           f"{tables['sqlite_sequence']} vs {seq_before}")
    _check("no -wal or -shm left beside the database",
           not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists())
    _check("nothing deleted is left in the database's bytes (VACUUM)", MARK.encode() not in db.read_bytes())
    gone = [f for f in STORE_FILES if f != "pi5/data/la_subasta.db" and (root / f).exists()]
    _check("results, its .tmp, the betting logs, the Race Setup file and the splash log are gone", not gone, str(gone))
    _check("splash_display/logs/ itself is gone", not (root / "splash_display/logs").exists())
    changed = [f for f in KEPT_FILES if sha(root / f) != kept[f]]
    _check("config, .env, the LEDs' files and the unrecognised file are byte for byte as they were", not changed,
           str(changed))

    made = backups(root)
    if not _check("one backup folder, backups/wipe-<date-time>", len(made) == 1 and made[0].name.startswith("wipe-"),
                  str(made)):
        return
    folder = made[0]
    manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    listed = {e["path"]: e for e in manifest["files"]}
    _check("the manifest lists every file the wipe touched", set(listed) == set(STORE_FILES),
           str(sorted(set(listed) ^ set(STORE_FILES))))
    _check("every backup copy is byte for byte the original (SHA-256)",
           all(sha(folder / f) == originals[f] == listed[f]["sha256"] for f in STORE_FILES))
    sums = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    _check("SHA256SUMS has a sha256sum -c line for every file",
           sorted(sums) == sorted(f"{originals[f]}  {f}" for f in STORE_FILES))
    restore_txt = (folder / "RESTORE.txt").read_text(encoding="utf-8")
    _check("RESTORE.txt has the command and the same by hand",
           f"--restore backups/{folder.name}" in restore_txt and "rm -f pi5/data/la_subasta.db-wal" in restore_txt
           and f"cp -p backups/{folder.name}/pi5/data/la_subasta.db pi5/data/la_subasta.db" in restore_txt
           and f"sha256sum -c backups/{folder.name}/SHA256SUMS" in restore_txt)

    again = wipe(root, answer="WIPE\n")
    _check("run again: 'Nothing to clear', exit 0, no question, no new backup",
           again.returncode == 0 and "Nothing to clear: pi5 already starts like a fresh install." in again.stdout
           and "Type WIPE" not in again.stdout and backups(root) == made, again.stdout[-600:])

    # A tampered backup is refused.
    tampered = make_tree(seed=False)
    shutil.copytree(folder, tampered / "backups" / folder.name)
    victim = tampered / "backups" / folder.name / "pi5/data/results.json"
    victim.write_bytes(victim.read_bytes().replace(b"7", b"8"))
    before = snapshot(tampered)
    run = wipe(tampered, "--restore", f"backups/{folder.name}", answer="RESTORE\n")
    _check("--restore refuses a backup whose copy changed, and changes nothing",
           run.returncode == 2 and "does not check out" in run.stdout and snapshot(tampered) == before, run.stdout[-500:])

    # pi5 writes again after the wipe (its first start re-seeds the one-row tables), then the restore.
    with Pi5(root):
        pass
    (root / "pi5/data/quiniela_2026-10-09.jsonl").write_text('{"after": "the wipe"}\n', encoding="utf-8")
    before = snapshot(root)
    run = wipe(root, "--restore", f"backups/{folder.name}", answer="no\n")
    _check("--restore with the wrong word changes nothing", run.returncode == 1 and snapshot(root) == before,
           run.stdout[-500:])
    run = wipe(root, "--restore", f"backups/{folder.name}", answer="RESTORE\n")
    _check("--restore: exit 0", run.returncode == 0, run.stdout[-1500:] + run.stderr[-800:])
    restored = [f for f in STORE_FILES if not (root / f).is_file() or sha(root / f) != originals[f]]
    _check("--restore puts every file back byte for byte (SHA-256)", not restored, str(restored))
    w = load_wipe(root)
    with contextlib.redirect_stdout(io.StringIO()):
        now = {w.shown(p) for p in w.store_paths(w.pi5_settings()[0])}
    _check("--restore leaves nothing from after the wipe (the new log, the new -wal)", now == set(STORE_FILES),
           str(sorted(now ^ set(STORE_FILES))))
    before_restore = [p for p in backups(root) if p.name.startswith("before-restore-")]
    _check("--restore backed up what was there first", len(before_restore) == 1 and
           (before_restore[0] / "pi5/data/quiniela_2026-10-09.jsonl").is_file())
    _check("the kept files are still as they were", all(sha(root / f) == kept[f] for f in KEPT_FILES))
    data = read_db(root / "pi5/data/la_subasta.db")
    _check("the restored database has its rows, the -wal's too",
           len(data["lq_horses"]) == 22 and len(data["bidders"]) == 3 and len(data["bids"]) == 5)

    # A database pi5 closed cleanly has no -wal; the one pi5 leaves after the wipe must not
    # stay beside the restored file, or SQLite would replay it into it.
    root = make_tree()
    db = root / "pi5/data/la_subasta.db"
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    _check("a cleanly closed database: no -wal beside it", not Path(str(db) + "-wal").exists())
    original = sha(db)
    run = wipe(root, answer="WIPE\n")
    folder = backups(root)[-1]
    with Pi5(root):
        pass
    _check("pi5 after the wipe leaves its own -wal", Path(str(db) + "-wal").exists())
    run = wipe(root, "--restore", f"backups/{folder.name}", answer="RESTORE\n")
    _check("--restore of a backup with no -wal: the database byte for byte, no -wal or -shm beside it",
           run.returncode == 0 and sha(db) == original and not Path(str(db) + "-wal").exists()
           and not Path(str(db) + "-shm").exists(), run.stdout[-600:])
    _check("... and it reads as it was", len(read_db(db)["lq_horses"]) == 22)


def test_pi5_starts_fresh_after_the_wipe():
    """pi5 started on the wiped data serves what pi5 serves on a fresh install, La
    Subasta included; and the tool then finds nothing to clear."""
    paths = ["/api/quiniela", "/api/quiniela/horses", "/api/quiniela/race", "/api/quiniela/field",
             "/api/results", "/api/race", "/api/spectator/state", "/la-subasta/api/state",
             "/la-subasta/api/horses", "/la-subasta/api/bidders", "/la-subasta/api/admin/payouts",
             "/la-subasta/api/admin/settings", "/la-subasta/api/admin/settings/audit"]
    volatile = {"now", "updated", "updated_at", "last_updated", "timestamp", "server_time", "time"}

    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items() if k not in volatile}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        return x

    def differs(a, b, at=""):
        if isinstance(a, dict) and isinstance(b, dict):
            return [d for k in sorted(a.keys() | b.keys()) for d in differs(a.get(k), b.get(k), f"{at}.{k}")]
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b):
            return [d for i, (x, y) in enumerate(zip(a, b)) for d in differs(x, y, f"{at}[{i}]")]
        return [] if a == b else [f"{at}: {str(a)[:80]} vs {str(b)[:80]}"]

    fresh_root = make_tree(seed=False)
    with Pi5(fresh_root) as pi5:
        fresh = {p: pi5.get(p) for p in paths}
    root = make_tree()
    run = wipe(root, answer="WIPE\n")
    if not _check("the wipe ran", run.returncode == 0, run.stdout[-800:]):
        return
    with Pi5(root) as pi5:
        wiped = {p: pi5.get(p) for p in paths}
    for p in paths:
        a, b = clean(wiped[p]), clean(fresh[p])           # [status, body]
        if p == "/api/spectator/state":        # the mock racing service draws new horses at every start
            for reply in (a, b):
                if isinstance(reply[1], dict):
                    reply[1].pop("horses", None)
        _check(f"GET {p}: the same as a fresh install", a == b, "; ".join(differs(a, b))[:600])
    status, m = wiped["/api/quiniela"]
    if _check("GET /api/quiniela answers", status == 200 and isinstance(m, dict)):
        _check("race state 0, PRE-RACE", m.get("race_state") == 0)
        _check("no names", all(not h.get("name") for h in m["horses"].values()))
        _check("no scratches", m.get("scratches") == [] and not any(h.get("scratched") for h in m["horses"].values()))
        _check("no figures at the post, no counted pot",
               m.get("closing") is None and m.get("pot_counted") is None and not m.get("hand_counted"))
        _check("no results", m.get("results") is None)
        _check("no race info", m["race"].get("post_at") is None and m["race"].get("year") is None)
        _check("no closing time, names counter 0", m.get("closes_at") is None and m.get("names_rev") == 0)
        _check("pot 0, no tokens", m.get("pot") == 0 and m.get("total_tokens") == 0)
    status, s = wiped["/la-subasta/api/state"]
    _check("La Subasta: not started, no guests, no bids, pot 0",
           status == 200 and s.get("state") == "NOT_STARTED" and s.get("num_bidders") == 0
           and s.get("num_bids") == 0 and s.get("total_pot") == 0, str(s)[:300])
    tables = read_db(root / "pi5/data/la_subasta.db")
    _check("after pi5's start: only the four one-row tables have a row",
           {t for t, r in tables.items() if r and t != "sqlite_sequence"}
           == {"lq_link_state", "lq_board", "lq_closing", "lq_race"})
    _check("the old Race Setup file was not copied back in (no names, migrated 0)",
           not tables["lq_horses"] and tables["lq_race"] == [(1, "", None, None, 0)], str(tables["lq_race"]))
    again = wipe(root, answer="WIPE\n")
    _check("after pi5's first start the tool finds nothing to clear",
           again.returncode == 0 and "Nothing to clear" in again.stdout, again.stdout[-800:])


def test_refuses_while_pi5_runs():
    root = make_tree()
    removable = [f for f in STORE_FILES if not f.startswith("pi5/data/la_subasta.db")]
    before = {f: sha(root / f) for f in removable}
    with Pi5(root):
        for args, answer in ((("--dry-run",), None), ((), "WIPE\n"), (("--restore", "backups/none"), "RESTORE\n")):
            run = wipe(root, *args, answer=answer)
            _check(f"pi5 running, {' '.join(args) or 'WIPE'}: refused, exit 2",
                   run.returncode == 2 and "Stop pi5 first: Ctrl+C in its terminal." in run.stdout
                   and f"127.0.0.1:{_PORTS[root]}" in run.stdout, run.stdout[-500:])
            _check(f"pi5 running, {' '.join(args) or 'WIPE'}: asks nothing, no backup",
                   "Type " not in run.stdout and not (root / "backups").exists())
    _check("pi5 running: the tool removed nothing (results, logs, Race Setup file, splash log as they were)",
           all((root / f).is_file() and sha(root / f) == before[f] for f in removable))
    tables = read_db(root / "pi5/data/la_subasta.db")
    _check("pi5 running: the data is all still there", len(tables["lq_horses"]) == 22 and len(tables["bidders"]) == 3)

    # Anything at all on the port is refused, pi5 or not.
    listener = socket.socket()
    listener.bind(("127.0.0.1", _PORTS[root]))
    listener.listen(1)
    try:
        before = snapshot(root)
        run = wipe(root, answer="WIPE\n")
        _check("a plain listener on pi5's port: refused, nothing changed",
               run.returncode == 2 and "Stop pi5 first" in run.stdout and snapshot(root) == before, run.stdout[-400:])
    finally:
        listener.close()

    if not Path("/proc").is_dir():
        print("  (skipped here: a process holding the database open is found through /proc, Linux only)")
        return
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sqlite3, sys, time; c = sqlite3.connect(sys.argv[1]); "
                               "c.execute('SELECT COUNT(*) FROM lq_horses').fetchall(); print('open', flush=True); "
                               "time.sleep(60)", str(root / "pi5/data/la_subasta.db")],
                              env=ENV, stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        before = snapshot(root)
        run = wipe(root, answer="WIPE\n")
        _check("a process with the database open (nothing on the port): refused, nothing changed",
               run.returncode == 2 and "has pi5/data/la_subasta.db open" in run.stdout and snapshot(root) == before,
               run.stdout[-400:])
    finally:
        holder.kill()
        holder.wait()


def test_backup_failure_clears_nothing():
    root = make_tree()
    (root / "backups").write_text("a file where the backups folder goes\n", encoding="utf-8")
    before = snapshot(root)
    run = wipe(root, answer="WIPE\n")
    _check("the backup folder can't be made: exit 2, 'The backup failed', 'Nothing was cleared.'",
           run.returncode == 2 and "The backup failed" in run.stdout and "Nothing was cleared." in run.stdout,
           run.stdout[-500:])
    _check("the backup folder can't be made: every file as it was", snapshot(root) == before,
           diff(before, snapshot(root)))
    (root / "backups").unlink()

    w = load_wipe(root)

    def bad_copier(src, dst):
        size, digest = w.copy_file(src, dst)
        if dst.name == "results.json":
            data = bytearray(dst.read_bytes())
            data[0] ^= 1
            dst.write_bytes(bytes(data))
        return size, digest

    before = snapshot(root)
    with contextlib.redirect_stdout(io.StringIO()):
        settings = w.pi5_settings()[0]
        try:
            w.make_backup(w.store_paths(settings), copier=bad_copier)
            raised = None
        except w.StopError as exc:
            raised = str(exc)
    _check("a copy that differs from its original stops the backup", raised and "results.json" in raised, str(raised))
    _check("... and leaves no half-made backup folder", not backups(root), str(backups(root)))
    _check("... and every file as it was", snapshot(root) == before, diff(before, snapshot(root)))

    # A wipe that leaves something (here the database is not emptied) says so and points at the backup.
    w.empty_database = lambda db: 0
    w.ask = lambda prompt: "WIPE"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = w.cmd_wipe(w.pi5_settings()[0], dry_run=False)
    out = out.getvalue()
    _check("a wipe that leaves data: exit 2, 'Not everything was cleared', the restore command",
           code == 2 and "Not everything was cleared" in out and "--restore backups/wipe-" in out
           and "Checked: nothing left" not in out, out[-600:])


def test_fresh_install_and_odd_cases():
    root = make_tree(seed=False)
    before = snapshot(root)
    run = wipe(root, answer="WIPE\n")
    _check("a Pi where pi5 never ran: 'Nothing to clear', exit 0, nothing changed",
           run.returncode == 0 and "Nothing to clear" in run.stdout and snapshot(root) == before
           and not (root / "backups").exists(), run.stdout[-500:])
    _check("... and its list says the database is not there yet", "la_subasta.db, not there yet" in run.stdout)

    # A -wal with no database beside it would be replayed into the next one: it goes too.
    wal = root / "pi5/data/la_subasta.db-wal"
    wal.write_bytes(b"\0" * 64)
    run = wipe(root, "--dry-run")
    _check("an orphan -wal is listed", run.returncode == 0 and "pi5/data/la_subasta.db-wal (no database beside them)"
           in run.stdout, run.stdout[-500:])
    run = wipe(root, answer="WIPE\n")
    _check("an orphan -wal is backed up and removed", run.returncode == 0 and not wal.exists()
           and (backups(root)[0] / "pi5/data/la_subasta.db-wal").is_file(), run.stdout[-500:])

    # A database with no data left but its counters moved on is not a fresh install's.
    root = make_tree(seed=False)
    with Pi5(root):
        pass
    db = root / "pi5/data/la_subasta.db"
    c = sqlite3.connect(str(db))
    c.execute("UPDATE lq_board SET names_rev = 3 WHERE id = 1")
    c.commit()
    c.close()
    run = wipe(root, answer="WIPE\n")
    _check("counters only (names 3): listed, asked, cleared",
           run.returncode == 0 and "Counters ............. names 3 " in run.stdout and "Type WIPE" in run.stdout
           and read_db(db)["lq_board"] == [], run.stdout[-600:])

    # A database that is not one: stop, change nothing.
    root = make_tree(seed=False)
    (root / "pi5/data/la_subasta.db").write_bytes(b"not a database at all, " * 100)
    before = snapshot(root)
    run = wipe(root, answer="WIPE\n")
    _check("an unreadable database: exit 2, 'could not be read', 'Nothing changed.', nothing changed",
           run.returncode == 2 and "could not be read" in run.stdout and "Nothing changed." in run.stdout
           and snapshot(root) == before, run.stdout[-500:])


def main():
    started = time.time()
    print(f"wipe.py tests, on scratch copies of {PI5_SRC}")
    _run("the paths are pi5's", test_paths_are_pi5s)
    _run("--dry-run changes nothing and lists every store", test_dry_run_changes_nothing)
    _run("anything but WIPE changes nothing", test_wrong_answer_changes_nothing)
    _run("WIPE: backup, wipe, nothing left; --restore byte for byte", test_wipe_backup_and_restore)
    _run("pi5 starts like a fresh install after the wipe", test_pi5_starts_fresh_after_the_wipe)
    _run("refuses while pi5 runs", test_refuses_while_pi5_runs)
    _run("a failed backup clears nothing", test_backup_failure_clears_nothing)
    _run("already clean, an orphan -wal, an unreadable database", test_fresh_install_and_odd_cases)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total ({time.time() - started:.0f} s)")
    print("=" * 50)
    for base in _BASES:
        if KEEP:
            print(f"kept: {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

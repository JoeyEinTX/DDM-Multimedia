#!/usr/bin/env python3
"""Move a box's own settings out of a locally edited pi5/config.py into pi5/.env.

pi5/config.py is tracked and holds only code and safe defaults; a box's own
settings (the gateway's port, the LED controller's address, the tote board) and
every secret live in pi5/.env, which is git-ignored. A box that carried a local
edit of pi5/config.py runs this once: it reads that file's values, writes the
ones pi5 now reads from .env into pi5/.env, and says how to drop the local edit.

    git stash && git pull                                    # the old edit kept in the stash
    python3 pi5/tools/env_from_config.py --stash             # dry run: what it would write
    python3 pi5/tools/env_from_config.py --stash --write     # merge them into pi5/.env
    git stash drop                                           # once pi5/.env is right

  --stash      read the old pi5/config.py and splash_display/config.py from the newest
               stash (stash@{0}); it must hold a change to pi5/config.py, or nothing is read.
  --from FILE  read the old pi5/config.py from FILE (a copy kept aside) instead.
  (neither)    read pi5/config.py as it is; one left with conflict markers by a
               `git stash pop` is read on the stash's side.
  --write      write pi5/.env (and splash_display/.env if the splash had a local
               setting); without it nothing is written.

It never changes a value already set in pi5/.env (an empty line there, copied from
pi5/.env.example, is filled in), and a second run changes nothing. It never prints
a secret: a secret line says only how long the value is. A secret whose value was
ever committed to the repo is not copied (it is public, so it was rotated): put the
new one in pi5/.env by hand. Standard library only; nothing of pi5's is imported.
"""

import argparse
import ast
import datetime
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_DEFAULT = HERE.parent.parent

# Every setting pi5 reads from pi5/.env as DDM_<NAME>, with its default there:
# config.py's _env() settings, the bridge's (la_quiniela/bridge.py DEFAULTS) and
# the betting board's (la_quiniela/betting.py DEFAULTS). A value in the old file
# that differs from the default is moved; an equal one needs no line.
PI5_SETTINGS = {
    "ESP32_IP": "", "ESP32_PORT": 5005,
    "TOTE_IP": "", "TOTE_PORT": 80, "TOTE_ENABLED": None,     # None: on when TOTE_IP is set
    "FLASK_HOST": "0.0.0.0", "FLASK_PORT": 5000,
    "WEATHER_LOCATION": "Dallas,TX",
    "LQ_BRIDGE_ENABLED": True, "LQ_SERIAL_PORT": "", "LQ_SERIAL_BAUD": 115200, "LQ_SERIAL_LINES": "leave",
    "LQ_HEARTBEAT_LOG_S": 10, "LQ_CUP_OFFLINE_S": 6, "LQ_GATEWAY_OFFLINE_S": 12,
    "LQ_DEAF_REOPEN_S": 20, "LQ_REOPEN_MIN_GAP_S": 30,
    "TOKEN_VALUE": 1.0, "QUINIELA_LOG": True, "QUINIELA_BOARD_STATES": [1, 2, 3, 4, 5],
    "LQ_SPLIT_WIN": 0.60, "LQ_SPLIT_PLACE": 0.25, "LQ_SPLIT_SHOW": 0.15,
    "LQ_CHYRON_LINES": ["TOTALS BASED ON CHEAP CHINESE ELECTRONICS - FINAL RESULTS HAND COUNTED",
                        "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS"],
    "SPLASH_BOARD_URL": "http://{host}:5001/",
}
# Secrets: moved only when found, never printed, never copied when committed before.
SECRETS = {"WEATHER_API_KEY", "ANTHROPIC_API_KEY", "FLASK_SECRET_KEY"}
# The splash's own (splash_display/.env). The old tracked PI5_URL needs no line:
# the new default, the same box, is right on DevPi.
SPLASH_SETTINGS = {"PI5_URL": "http://localhost:5000", "QUINIELA_LOOK": "dots"}
SPLASH_OLD_DEFAULTS = {"PI5_URL": "http://joeydevpi.local:5000"}
PLACEHOLDER_HINTS = ("your", "here", "changeme", "example", "xxxx")


# -----------------------------------------------------------------------------
# Reading
# -----------------------------------------------------------------------------

def stash_side(text):
    """A file a `git stash pop` left with conflict markers, on the stash's side
    (what the box had); a file with none, as it is."""
    out, side = [], None
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< "):
            side = "ours"
            continue
        if line.startswith("=======") and side == "ours":
            side = "theirs"
            continue
        if line.startswith(">>>>>>> ") and side == "theirs":
            side = None
            continue
        if side != "ours":
            out.append(line)
    return "".join(out)


def literal_settings(text):
    """NAME -> value for every top-level NAME = <literal>, and for NAME =
    _env('NAME', <literal>) / _lq_env(...) the default given there. Read with
    ast, never run."""
    values = {}
    tree = ast.parse(stash_side(text))
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name, value = node.targets[0].id, node.value
        if isinstance(value, ast.BoolOp) and value.values:       # NAME = _env(...) or something
            value = value.values[0]
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in ("_env", "_lq_env") and len(value.args) == 2):
            value = value.args[1]
        try:
            values[name] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
    return values


def read_env_file(path):
    """pi5/config.py's .env reader: NAME -> value."""
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def as_env(value):
    """A config.py value as its .env text, the way pi5 reads it back."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        if all(isinstance(v, int) for v in value):
            return ",".join(str(v) for v in value)          # DDM_QUINIELA_BOARD_STATES
        return "|".join(str(v) for v in value)              # DDM_LQ_CHYRON_LINES
    return str(value)


def git(repo, *args):
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def committed_before(repo, rel, value):
    """True when value is in any committed version of rel (so it is public), None
    when the history can't be read. Values are compared here, never passed to git."""
    commits = git(repo, "log", "--all", "--format=%H", "--", rel)
    if commits is None:
        return None
    for commit in commits.split():
        text = git(repo, "show", f"{commit}:{rel}")
        if text and value in text:
            return True
    return False


def looks_placeholder(value):
    return any(h in value.lower() for h in PLACEHOLDER_HINTS)


# -----------------------------------------------------------------------------
# Planning and writing
# -----------------------------------------------------------------------------

def plan(old, settings, env_now, secrets=(), repo=None, rel=None, old_defaults=None):
    """What to write: [(env name, text, note)], and what to say about the rest."""
    writes, notes = [], []
    for name, default in settings.items():
        if name not in old:
            continue
        value = old[name]
        env_name = "DDM_" + name
        if default is None:                                   # TOTE_ENABLED follows TOTE_IP
            default = bool(old.get("TOTE_IP"))
        if old_defaults and old_defaults.get(name) == value:
            notes.append(f"  {name}: the old default; the new one is right here, no line needed")
            continue
        if value == default or (isinstance(default, float) and isinstance(value, (int, float))
                                and float(value) == default):
            continue
        if env_now.get(env_name, "").strip():
            notes.append(f"  {env_name}: already set in .env, left as it is")
            continue
        writes.append((env_name, as_env(value), ""))
    for name in sorted(secrets):
        value = old.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        env_name = "DDM_" + name
        if env_now.get(env_name, "").strip():
            notes.append(f"  {env_name}: already set in .env, left as it is")
            continue
        if looks_placeholder(value):
            notes.append(f"  {name}: a placeholder in the old file, not copied: put the real one in .env by hand")
            continue
        public = committed_before(repo, rel, value) if repo else None
        if public is not False:
            why = "it was committed to the repo, which is public, so it was rotated" if public \
                else "the repo's history can't be read to check it was never committed"
            notes.append(f"  {name}: not copied ({why}): put the new one in .env by hand as {env_name}")
            continue
        writes.append((env_name, value, "secret"))
    return writes, notes


def shown(env_name, text, kind):
    return f"{env_name}=<secret, {len(text)} characters>" if kind == "secret" else f"{env_name}={text}"


def merge(env_path, writes, example_path=None):
    """Write the lines into the .env: an existing empty NAME= line filled in place,
    a missing one added at the end under a dated comment. A new file starts as a
    copy of the example. Returns the file's text as written."""
    if env_path.exists():
        raw = env_path.read_bytes().decode("utf-8")
    elif example_path and example_path.exists():
        raw = example_path.read_bytes().decode("utf-8")
    else:
        raw = ""
    eol = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    pending = []
    for env_name, text, _kind in writes:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].lstrip()
            name, sep, value = stripped.partition("=")
            if sep and name.strip() == env_name and not value.split(" #", 1)[0].strip():
                lines[i] = f"{env_name}={text}"
                break
        else:
            pending.append(f"{env_name}={text}")
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"# moved from config.py by pi5/tools/env_from_config.py, {datetime.date.today().isoformat()}")
        lines.extend(pending)
    text = eol.join(lines) + eol
    env_path.write_bytes(text.encode("utf-8"))
    return text


def from_stash(repo, out):
    """The old pi5/config.py and splash_display/config.py from stash@{0}: (pi5 text,
    splash text or None, what was read), or None when the newest stash holds no change
    to pi5/config.py (an older, unrelated stash is never read)."""
    names = git(repo, "stash", "show", "--name-only", "stash@{0}")
    if names is None or "pi5/config.py" not in names.split():
        out("The newest stash holds no change to pi5/config.py (or there is no stash): nothing read.\n"
            "  Run `git stash` before `git pull`, or use --from FILE.")
        return None
    label = (git(repo, "stash", "list", "-1", "--format=%gd: %gs (%cr)") or "stash@{0}").strip()
    splash = git(repo, "show", "stash@{0}:splash_display/config.py") if "splash_display/config.py" in names.split() else None
    return git(repo, "show", "stash@{0}:pi5/config.py"), splash, f"the stash, {label}"


def run(repo, source=None, write=False, out=print, stash=False):
    repo = Path(repo)
    pi5 = repo / "pi5"
    config_rel = "pi5/config.py"
    splash_cfg = repo / "splash_display" / "config.py"
    if stash:
        got = from_stash(repo, out)
        if got is None:
            return 2
        old_text, splash_text, where = got
    else:
        path = Path(source) if source else pi5 / "config.py"
        try:
            old_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            out(f"Can't read {path}: {exc}")
            return 2
        splash_text = splash_cfg.read_text(encoding="utf-8") if splash_cfg.exists() else None
        where = str(path)
    try:
        old = literal_settings(old_text)
    except SyntaxError as exc:
        out(f"Can't read {where} as Python: {exc}")
        return 2
    out(f"Reading {where}")
    env_path, env_now = pi5 / ".env", read_env_file(pi5 / ".env")
    writes, notes = plan(old, PI5_SETTINGS, env_now, SECRETS, repo, config_rel)

    splash_writes, splash_notes, splash_env = [], [], repo / "splash_display" / ".env"
    if splash_text:
        try:
            splash_old = literal_settings(splash_text)
        except SyntaxError:
            splash_old = {}
        splash_writes, splash_notes = plan(splash_old, SPLASH_SETTINGS, read_env_file(splash_env),
                                           old_defaults=SPLASH_OLD_DEFAULTS)

    verb = "Wrote" if write else "Would write"
    if writes:
        out(f"\n{verb} into pi5/.env:")
        for env_name, text, kind in writes:
            out("  " + shown(env_name, text, kind))
    if splash_writes:
        out(f"\n{verb} into splash_display/.env:")
        for env_name, text, kind in splash_writes:
            out("  " + shown(env_name, text, kind))
    if not writes and not splash_writes:
        out("\nNothing to move: every setting in it is already in .env, or is the default.")
    if notes or splash_notes:
        out("")
        for line in notes + splash_notes:
            out(line)
    if write:
        if writes:
            merge(env_path, writes, pi5 / ".env.example")
        if splash_writes:
            merge(splash_env, splash_writes, repo / "splash_display" / ".env.example")
    elif writes or splash_writes:
        out("\nDry run: nothing written. Run it again with --write to write these lines.")

    if git(repo, "rev-parse", "--git-dir") is not None:
        local = [rel for rel in (config_rel, "splash_display/config.py")
                 if (repo / rel).exists() and git(repo, "diff", "--quiet", "--", rel) is None]
        if local:
            out("\nThen drop the local edit" + ("s" if len(local) > 1 else "") + ", so a plain git pull works from now on:")
            out("  cd " + str(repo) + " && git checkout -- " + " ".join(local))
        if stash and write:
            out("\nThe old edit is still in the stash. Once pi5/.env is right (the new keys in by hand),"
                "\ndrop it: cd " + str(repo) + " && git stash drop")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--stash", action="store_true", help="read the old config files from the newest stash")
    where.add_argument("--from", dest="source", metavar="FILE", help="read the old pi5/config.py from FILE")
    ap.add_argument("--write", action="store_true", help="write the lines (default: dry run)")
    ap.add_argument("--repo", default=str(REPO_DEFAULT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    return run(args.repo, args.source, args.write, stash=args.stash)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())

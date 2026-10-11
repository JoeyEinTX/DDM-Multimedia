#!/usr/bin/env python3
"""Anything other than ddm-pi5 and ddm-splash that would start pi5 or the splash at boot,
and how to retire it. deploy/install_services.sh runs it before it installs; standard
library only.

    python3 deploy/starters.py check  [--repo DIR]             a STOP line per starter found
    python3 deploy/starters.py retire [--repo DIR] [--dry-run]  retire them, printing each step

Why: on DevPi an old, enabled /etc/systemd/system/ddm.service from an earlier session ran
the same pi5/main.py at boot, 4 s before ddm-pi5, held port 5000 and the gateway, and
ddm-pi5 restarted 107 times. The installer's port check could not see it: that unit only
runs at boot.

Where it looks, for something that runs this repo's main.py or server.py (or any
main.py / server.py under the repo's path):
  - systemd units, system and the user's, enabled or active (systemctl show: ExecStart and
    WorkingDirectory), ddm-pi5 and ddm-splash aside;
  - @reboot lines in the user's and root's crontab;
  - /etc/rc.local;
  - ~/.config/autostart/*.desktop (the kiosk's own ddm-tv.desktop aside, and anything
    already Hidden=true) and ~/.config/labwc/autostart.

Retiring is what Joey did by hand on 2026-10-11, one printed step at a time:
  - a unit: disable --now; its file (in /etc/systemd/system or ~/.config/systemd/user)
    moved to the repo's backups/ as NAME.old, or masked if it belongs to a package; then
    daemon-reload;
  - a crontab, rc.local or labwc autostart line: commented out behind a ddm-retired marker;
  - a .desktop entry: Hidden=true.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

DEPLOY_DIR = Path(__file__).resolve().parent
OURS = {"ddm-pi5.service", "ddm-splash.service"}
KIOSK_ENTRY = "ddm-tv.desktop"
ENABLED = {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "indirect", "generated", "transient"}
RUNNING = {"active", "activating", "reloading", "deactivating"}
SCRIPT = re.compile(r"(?<![\w.-])(main|server)\.py\b")


def marker() -> str:
    return f"# ddm-retired by deploy/install_services.sh --retire-old ({dt.date.today().isoformat()}): "


class Runner:
    """Runs a command (`sudo` in front when root is needed and this is not root), or in a dry
    run only prints it. Tests give their own."""

    def __init__(self, dry: bool = False):
        self.dry = dry
        self.sudo = [] if os.name != "posix" or os.geteuid() == 0 else ["sudo"]

    def query(self, cmd: Sequence[str], root: bool = False) -> Optional[str]:
        """Read-only: the command's stdout, or None if it failed or is missing."""
        args = (["sudo", "-n"] if root and self.sudo else []) + list(cmd)
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    def act(self, cmd: Sequence[str], root: bool = False, stdin: Optional[str] = None) -> bool:
        args = (self.sudo if root else []) + list(cmd)
        print(("  would run: " if self.dry else "  + ") + " ".join(args) + (" (the new contents on stdin)" if stdin else ""))
        if self.dry:
            return True
        try:
            return subprocess.run(args, input=stdin, text=True, timeout=120).returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  failed: {exc}")
            return False


class Finder:
    """Looks for the starters; `fs_root` puts /etc under a scratch folder for the tests."""

    def __init__(self, repo: Path, runner: Runner, home: Optional[Path] = None, fs_root: Path = Path("/")):
        self.repo = repo
        self.run = runner
        self.home = Path(home) if home else Path.home()
        self.fs_root = fs_root
        self.notes: List[str] = []         # what could not be checked

    def etc(self, rel: str) -> Path:
        return self.fs_root / rel.lstrip("/")

    def runs_ours(self, *texts: str) -> bool:
        """Something in these texts runs a main.py or server.py under the repo's path."""
        text = " ".join(t for t in texts if t)
        repo = str(self.repo).rstrip("/")
        spellings = {repo}
        home = str(self.home).rstrip("/")
        if repo.startswith(home) and repo[len(home):len(home) + 1] in ("/", "\\"):
            rest = repo[len(home):]
            spellings |= {"~" + rest, "$HOME" + rest, "${HOME}" + rest}
        return bool(SCRIPT.search(text)) and any(s in text for s in spellings)

    # --- systemd -------------------------------------------------------------------

    def units(self, user: bool) -> List[dict]:
        base = ["systemctl"] + (["--user"] if user else []) + ["--no-pager"]
        files = self.run.query(base + ["list-unit-files", "--type=service", "--no-legend"])
        loaded = self.run.query(base + ["list-units", "--type=service", "--all", "--no-legend", "--plain"])
        if files is None and loaded is None:
            self.notes.append(f"{'user' if user else 'system'} units not checked (systemctl{' --user' if user else ''} did not answer)")
            return []
        names = []
        for line in (files or "").splitlines() + (loaded or "").splitlines():
            parts = line.replace("●", " ").split()
            if parts and parts[0].endswith(".service") and "@." not in parts[0] and parts[0] not in names:
                names.append(parts[0])
        names = [n for n in names if n not in OURS]
        if not names:
            return []
        shown = self.run.query(base + ["show", "-p", "Id", "-p", "ExecStart", "-p", "WorkingDirectory", "-p",
                                       "UnitFileState", "-p", "ActiveState", "-p", "FragmentPath"] + names) or ""
        found = []
        for block in re.split(r"\n\s*\n", shown.strip()):
            props = {}
            for line in block.splitlines():
                key, _, value = line.partition("=")
                props[key] = value
            if not props.get("Id") or props["Id"] in OURS:
                continue
            live = props.get("UnitFileState") in ENABLED or props.get("ActiveState") in RUNNING
            if live and self.runs_ours(props.get("ExecStart", ""), props.get("WorkingDirectory", "")):
                props["user"] = user
                found.append(props)
        return found

    # --- the rest --------------------------------------------------------------------

    def crontab(self, root: bool) -> Tuple[Optional[str], List[str]]:
        cmd = ["crontab", "-l"] + (["-u", "root"] if root else [])
        text = self.run.query(cmd, root=root)
        if text is None:
            if root:
                self.notes.append("root's crontab not checked (sudo -n crontab -l -u root did not answer: no crontab, "
                                  "or sudo wants a password; sudo -v first)")
            return None, []
        hits = [ln for ln in text.splitlines()
                if ln.strip().startswith("@reboot") and self.runs_ours(ln)]
        return text, hits

    def lines_in(self, path: Path) -> List[str]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#") and self.runs_ours(ln)]

    def desktop_entries(self) -> List[Path]:
        folder = self.home / ".config" / "autostart"
        hits = []
        for p in sorted(folder.glob("*.desktop")) if folder.is_dir() else []:
            if p.name == KIOSK_ENTRY:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^\s*Hidden\s*=\s*true\s*$", text, re.M | re.I):
                continue
            execs = [ln for ln in text.splitlines() if ln.strip().startswith(("Exec=", "TryExec="))]
            if execs and self.runs_ours(*execs):
                hits.append(p)
        return hits

    def find(self) -> List[dict]:
        """Every starter found, as {kind, where, text, ...}."""
        found = []
        for user in (False, True):
            for u in self.units(user):
                found.append({"kind": "user unit" if user else "unit", "where": u["Id"], "unit": u,
                              "text": f"{u['Id']} ({u.get('UnitFileState')}, {u.get('ActiveState')}): "
                                      f"{exec_argv(u.get('ExecStart', ''))}"})
        for root in (False, True):
            text, hits = self.crontab(root)
            for ln in hits:
                found.append({"kind": "root crontab" if root else "crontab", "where": "crontab -l" + (" -u root" if root else ""),
                              "text": ln.strip(), "line": ln, "crontab": text, "root": root})
        rc_local = self.etc("/etc/rc.local")
        for ln in self.lines_in(rc_local):
            found.append({"kind": "rc.local", "where": str(rc_local), "text": ln.strip(), "line": ln, "path": rc_local})
        labwc = self.home / ".config" / "labwc" / "autostart"
        for ln in self.lines_in(labwc):
            found.append({"kind": "labwc autostart", "where": str(labwc), "text": ln.strip(), "line": ln, "path": labwc})
        for p in self.desktop_entries():
            found.append({"kind": "autostart entry", "where": str(p), "text": p.name, "path": p})
        return found


def exec_argv(exec_start: str) -> str:
    m = re.search(r"argv\[\]=([^;]*)", exec_start)
    return (m.group(1) if m else exec_start).strip()


# -----------------------------------------------------------------------------
# Retiring
# -----------------------------------------------------------------------------

def write_file(path: Path, text: str, run: Runner) -> bool:
    """Write a file in place: directly when this user may, else through sudo tee."""
    if run.dry:
        print(f"  would edit {path}")
        return True
    try:
        path.write_text(text, encoding="utf-8")
        print(f"  + edited {path}")
        return True
    except PermissionError:
        return run.act(["tee", str(path)], root=True, stdin=text)


def comment_line(text: str, line: str) -> str:
    out = []
    done = False
    for ln in text.splitlines(keepends=True):
        if not done and ln.rstrip("\r\n") == line:
            out.append(marker() + ln)
            done = True
        else:
            out.append(ln)
    return "".join(out)


def retire(found: List[dict], finder: Finder, run: Runner) -> bool:
    ok = True
    reload_system = reload_user = False
    backups = finder.repo / "backups"
    for f in found:
        print(f"  retiring the {f['kind']} {f['text']}")
        if f["kind"] in ("unit", "user unit"):
            u, user = f["unit"], f["unit"]["user"]
            base = ["systemctl"] + (["--user"] if user else [])
            ok &= run.act(base + ["disable", "--now", u["Id"]], root=not user)
            frag = u.get("FragmentPath", "")
            own_dir = (str(finder.home / ".config/systemd/user") if user else "/etc/systemd/system")
            if frag.startswith(own_dir + "/"):
                source = finder.etc(frag) if not user else Path(frag)
                target = backups / (u["Id"] + ".old")
                n = 2
                while target.exists():
                    target = backups / f"{u['Id']}.old.{n}"
                    n += 1
                if run.dry:
                    print(f"  would move {frag} to {target}")
                else:
                    backups.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(source), str(target))
                        print(f"  + moved {frag} to {target}")
                    except PermissionError:
                        ok &= run.act(["mv", frag, str(target)], root=True)
            else:
                print(f"  {frag or 'its file'} belongs to a package: masked instead of moved")
                ok &= run.act(base + ["mask", u["Id"]], root=not user)
            if user:
                reload_user = True
            else:
                reload_system = True
        elif f["kind"] in ("crontab", "root crontab"):
            new = comment_line(f["crontab"], f["line"])
            f["crontab"] = new
            for other in found:                 # a second hit in the same crontab edits this text
                if other is not f and other.get("crontab") is not None and other.get("root") == f["root"]:
                    other["crontab"] = new
            ok &= run.act(["crontab"] + (["-u", "root"] if f["root"] else []) + ["-"], root=f["root"], stdin=new)
        elif f["kind"] in ("rc.local", "labwc autostart"):
            text = f["path"].read_text(encoding="utf-8", errors="replace")
            ok &= write_file(f["path"], comment_line(text, f["line"]), run)
        elif f["kind"] == "autostart entry":
            text = f["path"].read_text(encoding="utf-8", errors="replace")
            header = "[Desktop Entry]\n"
            added = marker().rstrip() + "\nHidden=true\n"
            new = (text.replace(header, header + added, 1) if header in text
                   else text + ("" if text.endswith("\n") else "\n") + added)
            ok &= write_file(f["path"], new, run)
    if reload_system:
        ok &= run.act(["systemctl", "daemon-reload"], root=True)
    if reload_user:
        ok &= run.act(["systemctl", "--user", "daemon-reload"])
    return ok


def main(argv: Optional[List[str]] = None, runner: Optional[Runner] = None, home: Optional[Path] = None,
         fs_root: Path = Path("/")) -> int:
    parser = argparse.ArgumentParser(description="Other starters of pi5 or the splash (see the docstring).")
    parser.add_argument("action", choices=("check", "retire"))
    parser.add_argument("--repo", default=str(DEPLOY_DIR.parent))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    run = runner or Runner(dry=args.dry_run)
    finder = Finder(Path(args.repo), run, home=home, fs_root=fs_root)
    found = finder.find()
    for note in finder.notes:
        print(f"  --    {note}")
    if args.action == "check":
        if not found:
            print("  ok    nothing else starts pi5 or the splash at boot")
            return 0
        for f in found:
            print(f"  STOP  {f['kind']} {f['where']} also starts pi5 or the splash: {f['text']}")
        print("        retire it (them) with: deploy/install_services.sh --retire-old  (each step is printed; "
              "add --dry-run to see them first)")
        return 1
    if not found:
        print("  ok    nothing to retire: nothing else starts pi5 or the splash at boot")
        return 0
    return 0 if retire(found, finder, run) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The TV kiosk with the desktop session: what deploy/install_services.sh writes in the
user's home, and takes away again with --uninstall. Standard library only.

    python3 deploy/kiosk_autostart.py install   [--dry-run] [--home DIR]
    python3 deploy/kiosk_autostart.py uninstall [--dry-run] [--home DIR]
    python3 deploy/kiosk_autostart.py on | off | status [--dry-run] [--home DIR]

- ~/.config/autostart/ddm-tv.desktop, from deploy/ddm-tv.desktop with this repo's path:
  splash_display/deploy/kiosk.sh at login. Pi OS's labwc session (/etc/xdg/labwc/autostart)
  runs lxsession-xdg-autostart, which starts these, and so does LXDE. Where the system's
  labwc autostart does not run it, one marked line in ~/.config/labwc/autostart starts the
  kiosk instead (Pi OS starts labwc with -m, so that file adds to the system's, it does not
  replace it).
- With labwc: a keybind in ~/.config/labwc/rc.xml, Alt+Super+H, that hides the pointer and
  moves it to the screen's corner (labwc's HideCursor and WarpCursor; there is no setting for
  it). kiosk.sh presses it once with wtype when the browser is up. The root may be
  labwc_config or openbox_config (Pi OS's own, in the openbox namespace, a <theme> and no
  <keyboard>): a <keyboard> with <default /> and the keybind goes in just before the root's
  end tag; a <keyboard> section that had no keybind gets <default /> too, so labwc's own
  shortcuts stay. The file is read as XML and the result read back to check it, but written
  by inserting text at that one place, so everything else stays byte for byte.
- Screen blanking: only reported. Pi OS's Screen Blanking setting is a swayidle line in
  ~/.config/labwc/autostart; kiosk.sh stops swayidle for its session, and the setting itself
  (Control Centre -> Display) is Joey's.

- The switch (deploy/install_services.sh --kiosk on|off|status): off writes Hidden=true into
  ddm-tv.desktop (lxsession-xdg-autostart skips such an entry, xdg-autostart.c in lxsession)
  and closes a running kiosk (its kiosk.sh by the PID it keeps beside its lock, its browser by
  its own profile folder); on takes Hidden=true out again, and the kiosk starts at the next
  login. A ddm-tv.desktop.off (renamed by hand: the session reads only *.desktop names) is
  renamed back first, so there is one entry. The labwc-autostart line, where there is one, is
  commented out and back. install keeps the switch where it is: a pull and a re-run of the
  installer never turn a kiosk that is off back on.

Everything it adds is marked `ddm-tv`, and uninstall takes away exactly that: a file it
created goes, a file it added to comes back byte for byte. Running it twice changes nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional

DEPLOY_DIR = Path(__file__).resolve().parent
REPO = DEPLOY_DIR.parent
KIOSK = REPO / "splash_display" / "deploy" / "kiosk.sh"
DESKTOP_TEMPLATE = DEPLOY_DIR / "ddm-tv.desktop"
SYSTEM_LABWC_AUTOSTART = Path("/etc/xdg/labwc/autostart")

MARK = "ddm-tv"
KEYBIND_BEGIN = f"<!-- {MARK}: hide the TV's pointer, pressed by splash_display/deploy/kiosk.sh (deploy/install_services.sh) -->"
KEYBIND_END = f"<!-- /{MARK} -->"
KEYBIND = ('<keybind key="A-W-h"><action name="HideCursor" />'
           '<action name="WarpCursor" x="-1" y="-1" /></keybind>')
CREATED_RC = f"<!-- {MARK}: written by deploy/install_services.sh; uninstall removes it -->"
AUTOSTART_LINE_MARK = f"# {MARK}: the TV kiosk (deploy/install_services.sh)"
DESKTOP_NAME = "ddm-tv.desktop"
RENAMED_NAME = DESKTOP_NAME + ".off"              # how it was switched off by hand before --kiosk off
OFF_PREFIX = f"# {MARK}-off: "                    # the labwc-autostart line, switched off


class Plan:
    """What install or uninstall does, printed the installer's way; files written unless dry."""

    def __init__(self, dry: bool):
        self.dry = dry

    def say(self, kind: str, text: str) -> None:
        print(f"  {kind:<5} {text}")

    def write(self, path: Path, text: str, why: str) -> None:
        if path.is_file() and path.read_bytes() == text.encode("utf-8"):
            self.say("ok", f"{shown(path)} {why}: already so")
            return
        if self.dry:
            self.say("would", f"write {shown(path)}: {why}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.say("+", f"wrote {shown(path)}: {why}")

    def remove(self, path: Path, why: str) -> None:
        if self.dry:
            self.say("would", f"remove {shown(path)}: {why}")
            return
        path.unlink()
        self.say("+", f"removed {shown(path)}: {why}")


HOME = Path.home()


def shown(path: Path) -> str:
    try:
        return "~/" + Path(path).relative_to(HOME).as_posix()
    except ValueError:
        return str(path)


def read(path: Path) -> Optional[str]:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def eol_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


# -----------------------------------------------------------------------------
# rc.xml: the hide-pointer keybind
# -----------------------------------------------------------------------------

# What install adds, as exact text, so uninstall takes away exactly that. Three cases:
#   no rc.xml: the whole file (CREATED_FILE);
#   a <keyboard> section: the keybind's lines just inside it (in_keyboard), with <default />
#     when that section had no keybind (labwc loads its own shortcuts only while no keybind is
#     defined, or with <default />);
#   no <keyboard> section: one, with <default />, just before </labwc_config> (new_keyboard),
#     marked differently so it can never be taken for lines added inside a section of Joey's.
CREATED_FILE = (f"<?xml version=\"1.0\"?>\n{CREATED_RC}\n<labwc_config>\n  <keyboard>\n"
                f"    {KEYBIND_BEGIN}\n    <default />\n    {KEYBIND}\n    {KEYBIND_END}\n"
                f"  </keyboard>\n</labwc_config>\n")
SECTION_BEGIN = f"<!-- {MARK}: a keyboard section for the TV's hide-pointer keybind (deploy/install_services.sh) -->"


def in_keyboard(eol: str, default: bool) -> str:
    lines = [KEYBIND_BEGIN] + (["<default />"] if default else []) + [KEYBIND, KEYBIND_END]
    return "".join(f"{eol}    {line}" for line in lines)


def new_keyboard(eol: str) -> str:
    return (f"  <keyboard>{eol}    {SECTION_BEGIN}{eol}    <default />{eol}    {KEYBIND}{eol}"
            f"    {KEYBIND_END}{eol}  </keyboard>{eol}")


ROOTS = ("labwc_config", "openbox_config")        # Pi OS writes openbox_config, in the openbox namespace


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_rc(text: str):
    """The rc.xml as an element tree (None if it is not XML). Read only: the file is never
    written back from the tree, which would rewrite its declaration, quotes and spacing (and
    could name the openbox namespace ns0), so uninstall could not give it back byte for byte."""
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None


def has_keybind(root) -> bool:
    """Exactly one <keyboard> under the root, holding our keybind with both actions."""
    keyboards = [child for child in root if local(child.tag) == "keyboard"]
    if len(keyboards) != 1:
        return False
    binds = [kb for kb in keyboards[0] if local(kb.tag) == "keybind" and kb.get("key") == "A-W-h"]
    return len(binds) == 1 and [local(a.tag) + ":" + str(a.get("name")) for a in binds[0]] == \
        ["action:HideCursor", "action:WarpCursor"]


def rc_with_keybind(text: Optional[str]) -> Optional[str]:
    """rc.xml with the hide-pointer keybind; None when the file has a shape this can't add to.
    Read as XML (the root labwc_config or openbox_config, its <keyboard> if any), changed by
    inserting text at one place, and the result read back as XML to check it."""
    if text is None:
        return CREATED_FILE
    if f"<!-- {MARK}" in text:
        return text
    root = parse_rc(text)
    if root is None or local(root.tag) not in ROOTS:
        return None
    keyboards = [child for child in root if local(child.tag) == "keyboard"]
    if len(keyboards) > 1:
        return None                       # unusual: leave it to a person
    eol = eol_of(text)
    # Find where to insert in a copy with the comments blanked out (same length, so positions
    # hold): a commented-out example <keyboard> is not the section labwc reads.
    bare = re.sub(r"<!--.*?-->", lambda c: " " * len(c.group(0)), text, flags=re.S)
    if keyboards:
        m = re.search(r"<(?:[\w.-]+:)?keyboard(\s[^>]*)?>", bare)
        if m is None or m.group(0).endswith("/>"):
            return None                   # <keyboard /> on its own: leave it to a person
        has_any = any(local(child.tag) == "keybind" for child in keyboards[0])
        after = text[:m.end()] + in_keyboard(eol, default=not has_any) + text[m.end():]
    else:
        ends = list(re.finditer(r"</(?:[\w.-]+:)?(?:%s)\s*>" % "|".join(ROOTS), bare))
        if not ends:
            return None
        end = ends[-1].start()
        after = text[:end] + new_keyboard(eol) + text[end:]
    checked = parse_rc(after)
    if checked is None or local(checked.tag) != local(root.tag) or not has_keybind(checked):
        return None
    return after


def rc_without_keybind(text: str) -> Optional[str]:
    """rc.xml as it was before rc_with_keybind; None for a file it created (remove it).
    Unchanged if the added text was edited by hand (the caller says so)."""
    if text == CREATED_FILE:
        return None
    eol = eol_of(text)
    for added in (new_keyboard(eol), in_keyboard(eol, default=True), in_keyboard(eol, default=False)):
        if added in text:
            return text.replace(added, "", 1)
    return text


# -----------------------------------------------------------------------------
# install / uninstall
# -----------------------------------------------------------------------------

def labwc_present() -> bool:
    return shutil.which("labwc") is not None or SYSTEM_LABWC_AUTOSTART.parent.is_dir()


def system_runs_xdg_autostart() -> Optional[bool]:
    """Does the system's labwc autostart start ~/.config/autostart? None: no labwc autostart."""
    text = read(SYSTEM_LABWC_AUTOSTART)
    if text is None:
        return None
    return any("lxsession-xdg-autostart" in line and not line.lstrip().startswith("#")
               for line in text.splitlines())


def desktop_entry(on: bool = True) -> str:
    text = DESKTOP_TEMPLATE.read_text(encoding="utf-8").replace("@REPO@", REPO.as_posix())
    return text if on else text + "Hidden=true\n"


def hidden(text: str) -> bool:
    return bool(re.search(r"^\s*Hidden\s*=\s*true\s*$", text, re.M | re.I))


def switch_state(config: Path) -> Optional[bool]:
    """The kiosk at login: True on, False off, None not installed."""
    entry = read(config / "autostart" / DESKTOP_NAME)
    if entry is not None:
        return not hidden(entry)
    if (config / "autostart" / RENAMED_NAME).is_file():
        return False
    for line in (read(config / "labwc" / "autostart") or "").splitlines():
        if AUTOSTART_LINE_MARK in line:
            return not line.startswith(OFF_PREFIX)
    return None


def set_switch(plan: Plan, config: Path, on: bool) -> None:
    """ddm-tv.desktop (and the labwc-autostart line, if there is one) on or off."""
    entry, renamed = config / "autostart" / DESKTOP_NAME, config / "autostart" / RENAMED_NAME
    if renamed.is_file() and not entry.exists():
        if plan.dry:
            plan.say("would", f"rename {shown(renamed)} back to {DESKTOP_NAME} (the session reads only *.desktop names)")
        else:
            renamed.rename(entry)
            plan.say("+", f"renamed {shown(renamed)} back to {DESKTOP_NAME} (the session reads only *.desktop names)")
    elif renamed.is_file():
        plan.say("--", f"{shown(renamed)} left as it is: the session never reads it")
    plan.write(entry, desktop_entry(on), "on: the kiosk starts at login" if on
               else "off: Hidden=true, the session skips it")
    labwc_autostart = config / "labwc" / "autostart"
    text = read(labwc_autostart)
    if text and AUTOSTART_LINE_MARK in text:
        lines = []
        for line in text.splitlines(keepends=True):
            if AUTOSTART_LINE_MARK in line:
                bare = line[len(OFF_PREFIX):] if line.startswith(OFF_PREFIX) else line
                line = bare if on else OFF_PREFIX + bare
            lines.append(line)
        plan.write(labwc_autostart, "".join(lines), "its kiosk line on" if on else "its kiosk line commented out")


def install(home: Path, dry: bool) -> int:
    plan = Plan(dry)
    config = home / ".config"
    if not KIOSK.is_file():
        plan.say("STOP", f"{KIOSK} is missing")
        return 1
    on = switch_state(config) is not False          # a kiosk switched off stays off
    if not on:
        plan.say("--", "the kiosk is switched off and stays off (deploy/install_services.sh --kiosk on turns it on)")
    set_switch(plan, config, on)

    labwc_autostart = config / "labwc" / "autostart"
    runs = system_runs_xdg_autostart()
    if runs is False:
        text = read(labwc_autostart) or ""
        line = f"/bin/bash {KIOSK.as_posix()} &  {AUTOSTART_LINE_MARK}"
        if AUTOSTART_LINE_MARK in text:
            plan.say("ok", f"{shown(labwc_autostart)} has the kiosk's line: already so")
        else:
            eol = eol_of(text) if text else "\n"
            sep = "" if not text or text.endswith(("\n", "\r\n")) else eol
            plan.say("WARN", f"{SYSTEM_LABWC_AUTOSTART} does not run lxsession-xdg-autostart, so the .desktop "
                             f"entry would not start: the kiosk goes in {shown(labwc_autostart)} too")
            plan.write(labwc_autostart, text + sep + ("" if on else OFF_PREFIX) + line + eol,
                       "the kiosk starts with labwc" if on else "the kiosk's line, switched off")
    elif runs:
        plan.say("ok", f"{SYSTEM_LABWC_AUTOSTART} runs lxsession-xdg-autostart: the .desktop entry starts the kiosk")

    if labwc_present():
        rc = config / "labwc" / "rc.xml"
        before = read(rc)
        after = rc_with_keybind(before)
        if after is None:
            plan.say("WARN", f"{shown(rc)} has a shape this can't add to: the pointer stays on the TV. "
                             f"By hand, inside its root element: <keyboard><default />{KEYBIND}</keyboard> "
                             f"(or the keybind alone inside a <keyboard> that has keybinds)")
        else:
            changed = after != before
            plan.write(rc, after, "Alt+Super+H hides the pointer")
            if changed:
                plan.say("", "labwc loads it with `labwc --reconfigure` in a terminal on DevPi's desktop, "
                             "or at the next login")
        text = read(labwc_autostart) or ""
        if any("swayidle" in ln and not ln.lstrip().startswith("#") for ln in text.splitlines()):
            plan.say("WARN", f"screen blanking is on ({shown(labwc_autostart)} starts swayidle): the kiosk "
                             "stops it for its session; to turn it off for good: Control Centre -> Display -> "
                             "Screen Blanking off")
    else:
        plan.say("--", "no labwc here: nothing to hide the pointer with (X11: install unclutter)")
    return 0


# -----------------------------------------------------------------------------
# The switch: on, off, status
# -----------------------------------------------------------------------------

def profile_dir(home: Path) -> Path:
    return home / ".config" / "chromium-kiosk"          # kiosk.sh's PROFILE_DIR


def cmdline(pid: int) -> Optional[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None


def running(home: Path) -> Optional[dict]:
    """This home's kiosk now: {'script': kiosk.sh's pid or None, 'browser': [pids]}; None where
    there is no /proc and pgrep to tell (not Linux)."""
    if os.name != "posix" or not Path("/proc").is_dir() or not shutil.which("pgrep"):
        return None
    script = None
    try:
        pid = int(Path(str(profile_dir(home)) + ".pid").read_text().strip())
        if "kiosk.sh" in (cmdline(pid) or ""):
            script = pid
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", "--", f"--user-data-dir={profile_dir(home)}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    return {"script": script, "browser": [int(p) for p in out.split() if p.isdigit()]}


def close_kiosk(plan: Plan, home: Path) -> None:
    now = running(home)
    if now is None:
        plan.say("--", "can't see running processes here: close a kiosk on screen by hand")
        return
    if not now["script"] and not now["browser"]:
        plan.say("ok", "no kiosk running")
        return
    # kiosk.sh first, so a script still waiting for the splash never starts the browser.
    for what, pids in (("kiosk.sh", [now["script"]] if now["script"] else []), ("its browser", now["browser"])):
        for pid in pids:
            if plan.dry:
                plan.say("would", f"stop {what} (pid {pid})")
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                plan.say("+", f"stopped {what} (pid {pid})")
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                plan.say("WARN", f"could not stop {what} (pid {pid}): {exc}")


def switch(home: Path, action: str, dry: bool) -> int:
    plan = Plan(dry)
    config = home / ".config"
    if action == "status":
        state = switch_state(config)
        plan.say("", "the kiosk at login: " + {True: "on", False: "off", None: "not installed (run the installer)"}[state])
        now = running(home)
        if now is None:
            plan.say("", "running now: can't tell here")
        else:
            plan.say("", "running now: " + ("yes" if now["script"] or now["browser"] else "no"))
        return 0
    set_switch(plan, config, on=action == "on")
    if action == "off":
        close_kiosk(plan, home)
        return 0
    now = running(home)
    if not now or not (now["script"] or now["browser"]):
        kiosk = KIOSK.as_posix()
        print(f"  It starts at the next login. To start it now: in a terminal on DevPi's desktop,\n"
              f"      /bin/bash {kiosk} &\n"
              f"  or over SSH (the desktop's Wayland socket, usually wayland-0: ls /run/user/$(id -u)):\n"
              f"      XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 nohup /bin/bash {kiosk} >/dev/null 2>&1 &")
    return 0


def uninstall(home: Path, dry: bool) -> int:
    plan = Plan(dry)
    config = home / ".config"
    desktop = config / "autostart" / DESKTOP_NAME
    if desktop.is_file():
        plan.remove(desktop, "the kiosk no longer starts at login")
    renamed = config / "autostart" / RENAMED_NAME
    if renamed.is_file() and "deploy/install_services.sh" in (read(renamed) or ""):
        plan.remove(renamed, "the kiosk's entry, renamed off by hand")
    labwc_autostart = config / "labwc" / "autostart"
    text = read(labwc_autostart)
    if text and AUTOSTART_LINE_MARK in text:
        kept = "".join(ln for ln in text.splitlines(keepends=True) if AUTOSTART_LINE_MARK not in ln)
        if kept.strip():
            plan.write(labwc_autostart, kept, "without the kiosk's line")
        else:
            plan.remove(labwc_autostart, "it held only the kiosk's line")
    rc = config / "labwc" / "rc.xml"
    text = read(rc)
    if text and f"<!-- {MARK}" in text:
        after = rc_without_keybind(text)
        if after is None:
            plan.remove(rc, "it held only the hide-pointer keybind")
        elif after == text:
            plan.say("WARN", f"{shown(rc)}: the hide-pointer keybind was edited by hand; take it out by hand "
                             f"(the lines marked {MARK})")
        else:
            plan.write(rc, after, "without the hide-pointer keybind")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    global HOME
    parser = argparse.ArgumentParser(description="The TV kiosk with the desktop session (see the docstring).")
    parser.add_argument("action", choices=("install", "uninstall", "on", "off", "status"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--home", default=os.path.expanduser("~"))
    args = parser.parse_args(argv)
    HOME = Path(args.home)
    if args.action in ("on", "off", "status"):
        return switch(HOME, args.action, args.dry_run)
    return (install if args.action == "install" else uninstall)(HOME, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

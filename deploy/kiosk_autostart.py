#!/usr/bin/env python3
"""The TV kiosk with the desktop session: what deploy/install_services.sh writes in the
user's home, and takes away again with --uninstall. Standard library only.

    python3 deploy/kiosk_autostart.py install   [--dry-run] [--home DIR]
    python3 deploy/kiosk_autostart.py uninstall [--dry-run] [--home DIR]

- ~/.config/autostart/ddm-tv.desktop, from deploy/ddm-tv.desktop with this repo's path:
  splash_display/deploy/kiosk.sh at login. Pi OS's labwc session (/etc/xdg/labwc/autostart)
  runs lxsession-xdg-autostart, which starts these, and so does LXDE. Where the system's
  labwc autostart does not run it, one marked line in ~/.config/labwc/autostart starts the
  kiosk instead (Pi OS starts labwc with -m, so that file adds to the system's, it does not
  replace it).
- With labwc: a keybind in ~/.config/labwc/rc.xml, Alt+Super+H, that hides the pointer and
  moves it to the screen's corner (labwc's HideCursor and WarpCursor; there is no setting for
  it). kiosk.sh presses it once with wtype when the browser is up. A <keyboard> section that
  had no keybind gets <default /> too, so labwc's own shortcuts stay.
- Screen blanking: only reported. Pi OS's Screen Blanking setting is a swayidle line in
  ~/.config/labwc/autostart; kiosk.sh stops swayidle for its session, and the setting itself
  (Control Centre -> Display) is Joey's.

Everything it adds is marked `ddm-tv`, and uninstall takes away exactly that: a file it
created goes, a file it added to comes back byte for byte. Running it twice changes nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
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


def rc_with_keybind(text: Optional[str]) -> Optional[str]:
    """rc.xml with the hide-pointer keybind; None when the file has a shape this can't add to."""
    if text is None:
        return CREATED_FILE
    if f"<!-- {MARK}" in text:
        return text
    eol = eol_of(text)
    # Search a copy with the comments blanked out (same length, so positions hold): a
    # commented-out example <keyboard> is not the section labwc reads.
    bare = re.sub(r"<!--.*?-->", lambda c: " " * len(c.group(0)), text, flags=re.S)
    if re.search(r"<keyboard\s*/>", bare) or len(re.findall(r"<keyboard[\s>/]", bare)) > 1:
        return None                       # unusual: leave it to a person
    m = re.search(r"<keyboard(\s[^>]*)?>", bare)
    if m:
        close = bare.find("</keyboard>", m.end())
        if close < 0:
            return None
        has_keybind = "<keybind" in bare[m.end():close]
        return text[:m.end()] + in_keyboard(eol, default=not has_keybind) + text[m.end():]
    end = bare.rfind("</labwc_config>")
    if end < 0:
        return None
    return text[:end] + new_keyboard(eol) + text[end:]


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


def desktop_entry() -> str:
    return DESKTOP_TEMPLATE.read_text(encoding="utf-8").replace("@REPO@", REPO.as_posix())


def install(home: Path, dry: bool) -> int:
    plan = Plan(dry)
    config = home / ".config"
    if not KIOSK.is_file():
        plan.say("STOP", f"{KIOSK} is missing")
        return 1
    plan.write(config / "autostart" / "ddm-tv.desktop", desktop_entry(), "the kiosk starts at login")

    labwc_autostart = config / "labwc" / "autostart"
    runs = system_runs_xdg_autostart()
    if runs is False:
        text = read(labwc_autostart) or ""
        line = f"/bin/bash {KIOSK.as_posix()} &  {AUTOSTART_LINE_MARK}"
        if AUTOSTART_LINE_MARK in text:
            plan.say("ok", f"{shown(labwc_autostart)} starts the kiosk: already so")
        else:
            eol = eol_of(text) if text else "\n"
            sep = "" if not text or text.endswith(("\n", "\r\n")) else eol
            plan.say("WARN", f"{SYSTEM_LABWC_AUTOSTART} does not run lxsession-xdg-autostart, so the .desktop "
                             f"entry would not start: the kiosk goes in {shown(labwc_autostart)} too")
            plan.write(labwc_autostart, text + sep + line + eol, "the kiosk starts with labwc")
    elif runs:
        plan.say("ok", f"{SYSTEM_LABWC_AUTOSTART} runs lxsession-xdg-autostart: the .desktop entry starts the kiosk")

    if labwc_present():
        rc = config / "labwc" / "rc.xml"
        before = read(rc)
        after = rc_with_keybind(before)
        if after is None:
            plan.say("WARN", f"{shown(rc)} has a shape this can't add to: the pointer stays on the TV "
                             f"(add the Alt+Super+H keybind by hand: {KEYBIND})")
        else:
            plan.write(rc, after, "Alt+Super+H hides the pointer (labwc reads it at the next login)")
        text = read(labwc_autostart) or ""
        if any("swayidle" in ln and not ln.lstrip().startswith("#") for ln in text.splitlines()):
            plan.say("WARN", f"screen blanking is on ({shown(labwc_autostart)} starts swayidle): the kiosk "
                             "stops it for its session; to turn it off for good: Control Centre -> Display -> "
                             "Screen Blanking off")
    else:
        plan.say("--", "no labwc here: nothing to hide the pointer with (X11: install unclutter)")
    return 0


def uninstall(home: Path, dry: bool) -> int:
    plan = Plan(dry)
    config = home / ".config"
    desktop = config / "autostart" / "ddm-tv.desktop"
    if desktop.is_file():
        plan.remove(desktop, "the kiosk no longer starts at login")
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
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--home", default=os.path.expanduser("~"))
    args = parser.parse_args(argv)
    HOME = Path(args.home)
    return (install if args.action == "install" else uninstall)(HOME, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

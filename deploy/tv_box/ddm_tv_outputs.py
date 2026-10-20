#!/usr/bin/env python3
"""The TV box's screen: which output the board goes on, and at what mode.

Run on the box by ddm-tv-launch.sh with what `wlr-randr --json` (or, on an older
wlr-randr, plain `wlr-randr`) prints on stdin. wlr-randr lists the outputs a screen
is plugged into, enabled or not; nothing is forced on at boot (Joey's bench,
2026-10-19: the kernel's video=HDMI-A-1:...D made a phantom HDMI0 that Chromium went
full screen on while the screen sat on HDMI1, and its 1920x1080 was more than a
10.2" panel could show).

  - The output: HDMI-A-1 (HDMI0, next to the USB-C power) when a screen is on it,
    else HDMI-A-2 (HDMI1), else any other, by name. The others are switched off, so
    the desktop and the board are on the one screen.
  - The mode: 1920x1080 when the screen offers it (the refresh nearest 60 Hz), the
    size the board is designed for; else the screen's own preferred mode; else its
    current one; else its largest.

Prints, one per line:
  1. the choice, "<output> <width>x<height>@<refresh>", or "none" (no screen);
  2. "current" when the screen is already set that way (the chosen output on at that
     mode, every other output off), else "change";
  3. then the wlr-randr arguments that set it (nothing after "none").
Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

PREFERRED_OUTPUTS = ("HDMI-A-1", "HDMI-A-2")
BOARD_SIZE = (1920, 1080)


def parse(text: str) -> List[Dict[str, Any]]:
    """wlr-randr's report, --json or plain, as [{name, enabled, modes: [{width,
    height, refresh, preferred, current}]}]."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = []
        outs = []
        for o in data if isinstance(data, list) else []:
            if not isinstance(o, dict) or not o.get("name"):
                continue
            modes = []
            for m in o.get("modes") or []:
                try:
                    modes.append({"width": int(m["width"]), "height": int(m["height"]),
                                  "refresh": float(m.get("refresh") or 0.0),
                                  "preferred": bool(m.get("preferred")), "current": bool(m.get("current"))})
                except (KeyError, TypeError, ValueError):
                    continue
            outs.append({"name": str(o["name"]), "enabled": bool(o.get("enabled", True)), "modes": modes})
        return outs
    outs: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            cur = {"name": line.split()[0], "enabled": True, "modes": []}
            outs.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.lower().startswith("enabled:"):
            cur["enabled"] = s.split(":", 1)[1].strip().lower() in ("yes", "true")
            continue
        m = re.match(r"(\d+)x(\d+) px, ([\d.]+) Hz(?: \(([^)]*)\))?", s)
        if m:
            flags = (m.group(4) or "").lower()
            cur["modes"].append({"width": int(m.group(1)), "height": int(m.group(2)), "refresh": float(m.group(3)),
                                 "preferred": "preferred" in flags, "current": "current" in flags})
    return outs


def pick_output(outs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not outs:
        return None
    rank = {name: i for i, name in enumerate(PREFERRED_OUTPUTS)}
    return sorted(outs, key=lambda o: (rank.get(o["name"], len(rank)), o["name"]))[0]


def pick_mode(modes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not modes:
        return None
    board = [m for m in modes if (m["width"], m["height"]) == BOARD_SIZE]
    if board:
        return sorted(board, key=lambda m: (abs(m["refresh"] - 60.0), -m["refresh"]))[0]
    for flag in ("preferred", "current"):
        flagged = [m for m in modes if m[flag]]
        if flagged:
            return flagged[0]
    return sorted(modes, key=lambda m: (m["width"] * m["height"], m["refresh"]), reverse=True)[0]


def plan(outs: List[Dict[str, Any]]) -> Tuple[str, bool, List[str]]:
    """(the choice, already current, the wlr-randr arguments)."""
    out = pick_output(outs)
    if out is None:
        return "none", True, []
    mode = pick_mode(out["modes"])
    args = ["--output", out["name"], "--on"]
    choice = out["name"]
    current = out["enabled"]
    if mode is not None:
        size = "%dx%d" % (mode["width"], mode["height"])
        if mode["refresh"] > 0:
            args += ["--mode", "%s@%.6fHz" % (size, mode["refresh"])]
            choice += " %s@%.3f" % (size, mode["refresh"])
        else:
            args += ["--mode", size]
            choice += " " + size
        current = current and mode["current"]
    args += ["--pos", "0,0"]
    for other in outs:
        if other is not out:
            args += ["--output", other["name"], "--off"]
            current = current and not other["enabled"]
    return choice, current, args


def main() -> int:
    choice, current, args = plan(parse(sys.stdin.read()))
    print(choice)
    if choice != "none":
        print("current" if current else "change")
        for a in args:
            print(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())

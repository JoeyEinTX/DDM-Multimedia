#!/usr/bin/env python3
"""The installer's check of pi5/.env: is it there, and is every setting the night
needs set. Run by deploy/install_services.sh as the services' user, with a bare
environment, the way the service reads pi5/config.py (pi5/.env included):

    python3 deploy/env_check.py REPO

One line per check, LEVEL|text, for the installer to print: ok or warn (nothing
here blocks the install: each missing setting only turns its feature off), and
port|N with pi5's own port. A secret is reported only as set or not, never its
value. Standard library only.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def say(level, text):
    print(f"{level}|{text}", flush=True)


def load_config(pi5):
    """pi5/config.py as pi5 loads it, from a clean module name (no cached copy)."""
    spec = importlib.util.spec_from_file_location("ddm_pi5_config_check", pi5 / "config.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(pi5))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(pi5))
    return module


def check(repo):
    repo = Path(repo).resolve()
    pi5 = repo / "pi5"
    if (pi5 / ".env").is_file():
        say("ok", "pi5/.env is there")
    else:
        say("warn", "no pi5/.env: cp pi5/.env.example pi5/.env and fill in this box's settings "
                    "(the lines below say which)")
    try:
        c = load_config(pi5)
    except Exception as exc:                        # a broken config is pi5's problem too: say so
        say("warn", f"pi5/config.py could not be read ({exc}), so pi5/.env's settings are not checked")
        say("port", "5000")
        return
    say("port", str(getattr(c, "FLASK_PORT", 5000)))

    serial = getattr(c, "LQ_SERIAL_PORT", "") or ""
    if not serial:
        say("warn", "DDM_LQ_SERIAL_PORT is empty: pi5's bridge will idle, no cups "
                    "(set it in pi5/.env: the /dev/serial/by-id/... path)")
    elif os.path.exists(serial):
        say("ok", f"the gateway's port is there: {serial}")
    else:
        say("warn", f"the gateway's port is not there now ({serial}): unplugged? pi5 starts anyway "
                    "and opens it when it appears (it tries every 5 s)")

    leds = getattr(c, "ESP32_IP", "") or ""
    if leds:
        say("ok", f"the LED controller: {leds}")
    else:
        say("warn", "DDM_ESP32_IP is empty: no LED controller, the LEDs are off (set it in pi5/.env)")

    if getattr(c, "WEATHER_API_KEY", ""):
        say("ok", "the weather key is set")
    else:
        say("warn", "DDM_WEATHER_API_KEY is empty: the board's weather line is off (set it in pi5/.env)")

    if getattr(c, "ANTHROPIC_API_KEY", ""):
        say("ok", "the odds poller's key is set")
    else:
        say("warn", "DDM_ANTHROPIC_API_KEY is empty: the track's odds poller can't run (optional; set it in pi5/.env)")

    pin = getattr(c, "LS_ADMIN_PIN", "")
    if not pin:
        say("warn", "DDM_LS_ADMIN_PIN is empty: La Subasta's admin page and routes are off (set a PIN in pi5/.env)")
    elif pin == "change-me":
        say("warn", "DDM_LS_ADMIN_PIN is still the example, change-me: anyone who has read the repo knows it "
                    "(set your own in pi5/.env)")
    else:
        say("ok", "La Subasta's host PIN is set")

    # The dev routes it switched on went in 59e3b14, La Subasta's test tools with gap review M4: a line
    # left for it reads as if they were on. (pi5/config.py put pi5/.env into the environment.)
    if os.environ.get("DDM_LQ_DEV_ENDPOINTS") is not None:
        say("warn", "DDM_LQ_DEV_ENDPOINTS is set: it does nothing, pi5 has no dev or test routes; "
                    "delete its line from pi5/.env")

    # pi5/config.py is tracked: a local edit to it is what made every pull a stash dance.
    try:
        diff = subprocess.run(["git", "-C", str(repo), "diff", "--quiet", "--", "pi5/config.py"],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return
    if diff.returncode == 1:
        say("warn", "pi5/config.py has a local edit: move it into pi5/.env (python3 pi5/tools/env_from_config.py, "
                    "then --write), then git checkout -- pi5/config.py")


if __name__ == "__main__":
    check(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)

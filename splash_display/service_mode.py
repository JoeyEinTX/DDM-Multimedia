"""How this program behaves as a systemd service (deploy/: ddm-pi5, ddm-splash).

pi5 and the splash each carry an identical copy of this file (deploy/test_deploy.py
checks they stay the same).

- Never alongside the service: a copy started by hand (INVOCATION_ID unset: systemd
  sets it for the processes it starts) refuses while its service is active, before it
  opens anything. Nothing changes where there is no systemctl (the Shop PC, the tests).
- No access log in the journal: werkzeug logs every request at INFO, and the pages poll
  every few seconds. Its logger goes to WARNING, so errors still show;
  DDM_ACCESS_LOG=1 turns the request lines back on.
"""

import logging
import os
import shutil
import subprocess
import sys

RUNNING_STATES = ("active", "activating", "reloading")


def refuse_alongside_the_service(service):
    """Exit with a plain message if this copy was started by hand while `service` runs."""
    if os.environ.get("INVOCATION_ID"):          # started by systemd: this copy is the service
        return
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return
    try:
        state = subprocess.run([systemctl, "is-active", service], capture_output=True, text=True,
                               timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if state in RUNNING_STATES:
        message = (f"{service} is running under systemd. Stop it first: sudo systemctl stop {service} "
                   f"— then start it again with sudo systemctl start {service}.")
        try:
            print(message, file=sys.stderr)
        except UnicodeEncodeError:
            print(message.replace("—", "-"), file=sys.stderr)
        sys.exit(1)


def quiet_access_log():
    """werkzeug's request lines off unless DDM_ACCESS_LOG is 1/true/yes/on."""
    if os.environ.get("DDM_ACCESS_LOG", "").strip().lower() not in ("1", "true", "yes", "on"):
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

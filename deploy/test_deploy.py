#!/usr/bin/env python3
"""deploy/: the two services, the installer, and what pi5 and the splash do about them.

    python deploy/test_deploy.py        (from the repo root; --keep leaves the scratch folders)

- The unit files: the keys the services need, no home path committed, ExecStart a file in
  the repo.
- The hand-run guard: with a fake `systemctl` on PATH saying `active`, `python main.py` and
  `python3 server.py` refuse with the message and exit 1, before they open anything; saying
  `inactive`, or with INVOCATION_ID set (systemd started them), they start as before. The
  start is stubbed (socketio.run / Flask.run print and exit): no port is bound.
- The access log: werkzeug's logger at WARNING, unless DDM_ACCESS_LOG=1.
- The LED controller client: one line when it becomes unreachable, none for 5 minutes of
  simulated time, then one; one when it answers again.
- The installer: bash -n, shellcheck when there is one, and a --dry-run on a scratch copy
  (here, without systemd, it shows the units and the steps; on DevPi it runs the checks too).
- Other starters (deploy/starters.py) from faked systemctl and crontab answers: an old
  ddm.service on this repo's main.py, a packaged unit, a crontab @reboot line, rc.local, a
  labwc autostart line and an autostart entry are each a STOP; --retire-old on a scratch root
  retires them, printing each step; a clean system has none.
- The kiosk switch on scratch homes: on, off (Hidden=true), status, a ddm-tv.desktop.off
  renamed by hand, the labwc-autostart line; the installer keeps it where it is. On Linux, off
  stops a running kiosk.sh and its browser and nothing else.
- The kiosk: kiosk.sh with a fake browser (Linux), the autostart and rc.xml edits.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import io
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
KEEP = "--keep" in sys.argv
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith("DDM_") and k not in ("INVOCATION_ID", "JOURNAL_STREAM")}
ENV.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
SERVICES = {"ddm-pi5": ("pi5", "main.py"), "ddm-splash": ("splash_display", "server.py")}


def refusal(service):
    return (f"{service} is running under systemd. Stop it first: sudo systemctl stop {service} "
            f"— then start it again with sudo systemctl start {service}.")


# -----------------------------------------------------------------------------
# Tiny test runner (no pytest dependency), as pi5's suites
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


_BASES = []


def scratch_dir(prefix):
    base = Path(tempfile.mkdtemp(prefix=prefix))
    _BASES.append(base)
    return base


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# -----------------------------------------------------------------------------
# The unit files
# -----------------------------------------------------------------------------

def parse_unit(text):
    """{section: {key: [values]}}: systemd's format, a key may repeat."""
    out, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = out.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        section.setdefault(key.strip(), []).append(value.strip())
    return out


def test_unit_files():
    for service, (folder, script) in SERVICES.items():
        path = DEPLOY / f"{service}.service"
        if not _check(f"{service}.service is in deploy/", path.is_file()):
            continue
        text = path.read_text(encoding="utf-8")
        unit = parse_unit(text)
        svc = unit.get("Service", {})
        after = " ".join(unit.get("Unit", {}).get("After", [])).split()
        wants = " ".join(unit.get("Unit", {}).get("Wants", [])).split()
        _check(f"{service}: User=@USER@ (the installer fills it in)", svc.get("User") == ["@USER@"], str(svc.get("User")))
        _check(f"{service}: WorkingDirectory=@REPO@/{folder}", svc.get("WorkingDirectory") == [f"@REPO@/{folder}"])
        _check(f"{service}: ExecStart=/usr/bin/python3 @REPO@/{folder}/{script}",
               svc.get("ExecStart") == [f"/usr/bin/python3 @REPO@/{folder}/{script}"], str(svc.get("ExecStart")))
        exec_file = Path(svc["ExecStart"][0].split()[1].replace("@REPO@", str(REPO)))
        _check(f"{service}: ExecStart's script is a file in the repo", exec_file.is_file(), str(exec_file))
        _check(f"{service}: Restart=on-failure, RestartSec=3",
               svc.get("Restart") == ["on-failure"] and svc.get("RestartSec") == ["3"])
        _check(f"{service}: PYTHONUNBUFFERED=1 (prints reach the journal as they happen)",
               "PYTHONUNBUFFERED=1" in svc.get("Environment", []))
        _check(f"{service}: After= and Wants=network-online.target",
               "network-online.target" in after and "network-online.target" in wants)
        _check(f"{service}: WantedBy=multi-user.target (starts at boot)",
               unit.get("Install", {}).get("WantedBy") == ["multi-user.target"])
        _check(f"{service}: no home path or user name committed", "/home/" not in text and "joey" not in text)
    splash = parse_unit((DEPLOY / "ddm-splash.service").read_text(encoding="utf-8"))
    _check("ddm-splash: After=ddm-pi5.service, without Requires= (it finds pi5 again on its own)",
           "ddm-pi5.service" in " ".join(splash["Unit"].get("After", []))
           and "Requires" not in splash["Unit"] and "BindsTo" not in splash["Unit"])
    committed = [p for p in DEPLOY.iterdir() if p.is_file() and p.suffix in (".service", ".sh", ".desktop")]
    homes = [p.name for p in committed if re.search(r"/home/|/Users/", p.read_text(encoding="utf-8"))]
    _check("no absolute home path in any deploy/ file", not homes, str(homes))
    crlf = [p.name for p in committed if b"\r\n" in p.read_bytes()]
    _check("deploy/'s scripts and units are LF (bash and systemd on DevPi)", not crlf, str(crlf))
    _check("the splash's earlier unit (user pi) is gone", not (REPO / "splash_display/deploy/splash_display.service").exists())
    a, b = (REPO / "pi5/service_mode.py").read_bytes(), (REPO / "splash_display/service_mode.py").read_bytes()
    _check("pi5/service_mode.py and splash_display/service_mode.py are the same file",
           a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"))


# -----------------------------------------------------------------------------
# The hand-run guard and the access log, on scratch copies of pi5 and the splash
# -----------------------------------------------------------------------------

PI5_SCRATCH_CONFIG = """
# test_deploy.py: a scratch pi5 on loopback, its own port, nothing external
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
SPLASH_SCRATCH_CONFIG = """
# test_deploy.py: a scratch splash on loopback, its own port, pointed at no pi5
FLASK_HOST = '127.0.0.1'
FLASK_PORT = {port}
PI5_URL = 'http://127.0.0.1:{pi5_port}'
"""

# Run the program as __main__ with its server's start stubbed: it prints werkzeug's level and exits.
STUB_START = r'''
import logging, os, runpy, sys
sys.path.insert(0, os.getcwd())
def started(*args, **kwargs):
    print("STARTED werkzeug=%d" % logging.getLogger("werkzeug").level, flush=True)
    os._exit(0)
if sys.argv[1] == "main.py":
    import flask_socketio
    flask_socketio.SocketIO.run = started
else:
    import flask
    flask.Flask.run = started
script = sys.argv[1]
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
'''


def make_scratch():
    """A copy of pi5 and the splash with their own ports; returns (root, pi5_port, splash_port)."""
    root = scratch_dir("ddm_deploy_test_") / "DDM-Multimedia"
    shutil.copytree(REPO / "pi5", root / "pi5", ignore=shutil.ignore_patterns(
        "data", "__pycache__", ".env", "animation_params.json", "*.db", "*.db-wal", "*.db-shm"))
    (root / "pi5/data").mkdir()
    shutil.copy2(REPO / "pi5/data/animation_registry.json", root / "pi5/data/animation_registry.json")
    shutil.copytree(REPO / "splash_display", root / "splash_display",
                    ignore=shutil.ignore_patterns("__pycache__", "logs", "tests"))
    pi5_port, splash_port = free_port(), free_port()
    with open(root / "pi5/config.py", "a", encoding="utf-8") as f:
        f.write(PI5_SCRATCH_CONFIG.format(port=pi5_port))
    with open(root / "splash_display/config.py", "a", encoding="utf-8") as f:
        f.write(SPLASH_SCRATCH_CONFIG.format(port=splash_port, pi5_port=free_port()))
    return root, pi5_port, splash_port


def fake_systemctl(state):
    """A folder holding a `systemctl` that answers `is-active` with `state`."""
    folder = scratch_dir("ddm_fake_systemctl_")
    code = 0 if state == "active" else 3
    if os.name == "nt":
        (folder / "systemctl.bat").write_text(f"@echo {state}\r\n@exit /b {code}\r\n", encoding="ascii")
    else:
        script = folder / "systemctl"
        script.write_text(f"#!/bin/sh\necho {state}\nexit {code}\n", encoding="ascii")
        script.chmod(0o755)
    return folder


def run_program(root, folder, script, state=None, extra=None, stub=True):
    env = dict(ENV)
    if state is not None:
        env["PATH"] = str(fake_systemctl(state)) + os.pathsep + env.get("PATH", "")
    env.update(extra or {})
    args = [sys.executable, "-c", STUB_START, script] if stub else [sys.executable, script]
    try:
        return subprocess.run(args, cwd=root / folder, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120 if stub else 30)
    except subprocess.TimeoutExpired as exc:   # it did not refuse: it is serving (run() killed it)
        return types.SimpleNamespace(returncode="still running after 30 s",
                                     stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""))


def test_hand_run_guard():
    root, pi5_port, splash_port = make_scratch()
    ports = {"ddm-pi5": pi5_port, "ddm-splash": splash_port}
    for service, (folder, script) in SERVICES.items():
        for state in ("active", "activating"):
            run = run_program(root, folder, script, state, stub=False)
            _check(f"{service} {state}: `{script}` by hand refuses, exit 1, with the message",
                   run.returncode == 1 and run.stderr.strip() == refusal(service),
                   f"exit {run.returncode}: {run.stderr[-400:]}{run.stdout[-200:]}")
            # pi5 runs first: until its own start below, its database must not exist. The splash
            # logs its link's start at import: nothing on stderr but the refusal shows it never got there.
            _check(f"{service} {state}: refused before it opened anything (port free, no output, no database)",
                   port_free(ports[service]) and run.stdout.strip() == ""
                   and (service != "ddm-pi5" or not (root / "pi5/data/la_subasta.db").exists()))
        run = run_program(root, folder, script, "inactive")
        _check(f"{service} inactive: `{script}` starts as before",
               run.returncode == 0 and "STARTED" in run.stdout and refusal(service) not in run.stderr,
               f"exit {run.returncode}: {run.stdout[-300:]}{run.stderr[-500:]}")
        run = run_program(root, folder, script, "active", extra={"INVOCATION_ID": "0123456789abcdef"})
        _check(f"{service} active, INVOCATION_ID set (systemd started it): starts",
               run.returncode == 0 and "STARTED" in run.stdout, f"exit {run.returncode}: {run.stderr[-500:]}")
        run = run_program(root, folder, script, "inactive")
        _check(f"{service}: werkzeug's logger at WARNING (no line per request)",
               f"STARTED werkzeug={logging.WARNING}" in run.stdout, run.stdout[-200:])
        run = run_program(root, folder, script, "inactive", extra={"DDM_ACCESS_LOG": "1"})
        _check(f"{service}: DDM_ACCESS_LOG=1 leaves the request lines on",
               f"STARTED werkzeug={logging.NOTSET}" in run.stdout, run.stdout[-200:])

    # No systemctl at all (the Shop PC): nothing happens.
    sys.path.insert(0, str(REPO / "pi5"))
    try:
        mode = importlib.import_module("service_mode")
    finally:
        sys.path.pop(0)
    saved_which, saved_env = mode.shutil.which, os.environ.pop("INVOCATION_ID", None)
    mode.shutil.which = lambda name: None
    try:
        mode.refuse_alongside_the_service("ddm-pi5")
        _check("no systemctl: the guard lets it start", True)
    except SystemExit:
        _check("no systemctl: the guard lets it start", False)
    finally:
        mode.shutil.which = saved_which
        if saved_env is not None:
            os.environ["INVOCATION_ID"] = saved_env


# -----------------------------------------------------------------------------
# The LED controller client
# -----------------------------------------------------------------------------

class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeSocket:
    """socket.socket for esp32_client: `mode` decides what connect() does."""
    mode = "down"
    reply = b"STATUS:120:300:80\n"

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, t):
        pass

    def connect(self, address):
        if FakeSocket.mode == "down":
            raise OSError(113, "No route to host")
        if FakeSocket.mode == "timeout":
            raise socket.timeout("timed out")
        if FakeSocket.mode == "refused":
            raise ConnectionRefusedError(111, "Connection refused")

    def sendall(self, data):
        pass

    def recv(self, n):
        return FakeSocket.reply


def test_led_client_logs_on_change():
    sys.path.insert(0, str(REPO / "pi5"))
    try:
        esp = importlib.import_module("communication.esp32_client")
    finally:
        sys.path.pop(0)
    esp.socket = types.SimpleNamespace(socket=FakeSocket, timeout=socket.timeout,
                                       AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM)
    clock = FakeClock()
    client = esp.ESP32Client("10.0.0.42", 5005, timeout=1.0, clock=clock)

    def call(fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fn(*args)
        return result, [ln for ln in out.getvalue().splitlines() if ln.strip()]

    FakeSocket.mode = "down"
    result, lines = call(client.get_power_status)
    _check("down: one line, with the address it tried and why",
           lines == ["[ESP32] LED controller unreachable at 10.0.0.42:5005 ([Errno 113] No route to host, sending STATUS)"],
           str(lines))
    _check("down: get_power_status still answers None, connected False", result is None and not client.is_connected())
    quiet = []
    for _ in range(59):                       # the dashboard's 5 s poll, for 295 s
        clock.advance(5)
        quiet += call(client.get_power_status)[1]
    _check("down: nothing more for the next 295 s (59 polls)", quiet == [], str(quiet[:3]))
    clock.advance(5)
    _, lines = call(client.get_power_status)
    _check("down 5 minutes after the first line: one line, with how many failed in between",
           lines == ["[ESP32] LED controller still unreachable at 10.0.0.42:5005 ([Errno 113] No route to host); "
                     "59 more command(s) failed since the last line"], str(lines))
    clock.advance(5)
    _check("... then quiet again", call(client.get_power_status)[1] == [])
    for mode, error in (("timeout", "ERROR:TIMEOUT"), ("refused", "ERROR:CONNECTION_REFUSED")):
        FakeSocket.mode = mode
        result, lines = call(client.send_command, "PING")
        _check(f"{mode}: the same answer as before ({error}), and no line inside the 5 minutes",
               result == error and lines == [], f"{result} {lines}")
    FakeSocket.mode = "up"
    result, lines = call(client.get_power_status)
    _check("back, on a STATUS poll: one line saying so, and no line for the poll itself",
           lines == ["[ESP32] LED controller reachable again at 10.0.0.42:5005"], str(lines))
    _check("back: the status parsed, connected True",
           result == {"current_ma": 120, "peak_ma": 300, "min_ma": 80} and client.is_connected())
    quiet = []
    for _ in range(12):                       # a minute of the dashboard's 5 s power poll
        quiet += call(client.get_power_status)[1]
    _check("up: the STATUS poll prints nothing (it was a line every 5 s per open dashboard)", quiet == [], str(quiet[:2]))
    FakeSocket.reply = b"PONG\n"
    _, lines = call(client.send_command, "PING")
    _check("up: every other command still prints its line", lines == ["[ESP32] Sent: PING | Received: PONG"], str(lines))
    FakeSocket.reply = b"STATUS:120:300:80\n"
    saved = os.environ.get("DDM_ACCESS_LOG")
    os.environ["DDM_ACCESS_LOG"] = "1"
    try:
        _, lines = call(client.get_power_status)
    finally:
        if saved is None:
            os.environ.pop("DDM_ACCESS_LOG", None)
        else:
            os.environ["DDM_ACCESS_LOG"] = saved
    _check("DDM_ACCESS_LOG=1: the STATUS line is back", lines == ["[ESP32] Sent: STATUS | Received: STATUS:120:300:80"],
           str(lines))
    FakeSocket.mode = "down"
    clock.advance(1)
    _, lines = call(client.ping)
    _check("down again: one line at once (a new outage)",
           len(lines) == 1 and "LED controller unreachable at 10.0.0.42:5005" in lines[0], str(lines))


# -----------------------------------------------------------------------------
# The installer
# -----------------------------------------------------------------------------

def bash():
    return shutil.which("bash")


def test_installer():
    sh = bash()
    if not sh:
        print("  (skipped: no bash here)")
        return
    script = DEPLOY / "install_services.sh"
    run = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
    _check("install_services.sh: bash -n", run.returncode == 0, run.stderr)
    if shutil.which("shellcheck"):
        run = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)
        _check("install_services.sh: shellcheck", run.returncode == 0, run.stdout[-1500:])
    else:
        print("  (shellcheck is not installed here)")

    # A dry run on a copy at a plain path (the Shop PC's checkout has a space in it).
    root = scratch_dir("ddm_deploy_dry_") / "DDM-Multimedia"
    shutil.copytree(DEPLOY, root / "deploy", ignore=shutil.ignore_patterns("__pycache__", "test_*.py"))
    for rel in ("pi5/main.py", "splash_display/server.py", "splash_display/deploy/kiosk.sh"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, root / rel)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--dry-run"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", env=ENV, cwd=root, timeout=300)
    out = run.stdout
    print("\n".join("    | " + ln for ln in out.splitlines()[:6]))
    systemd_here = shutil.which("systemctl") and Path("/run/systemd/system").is_dir()
    if run.returncode != 0 and systemd_here:
        _check("--dry-run on this machine stops only on a real check (listed as STOP)",
               "STOP" in out and "Nothing installed" in out, out[-800:])
        return
    _check("--dry-run: exit 0", run.returncode == 0, out[-800:] + run.stderr[-500:])
    _check("--dry-run: says nothing is changed", "dry run: nothing is changed" in out and "Dry run: nothing changed." in out)
    rendered = [ln.split("| ", 1)[1] for ln in out.splitlines() if ln.startswith("      | ")]
    exec_lines = [ln for ln in rendered if ln.startswith("ExecStart=")]
    _check("--dry-run: shows both units with the repo's path filled in",
           len(exec_lines) == 2 and all(re.match(r"ExecStart=/usr/bin/python3 /\S*DDM-Multimedia/(pi5/main\.py|"
                                                 r"splash_display/server\.py)$", ln) for ln in exec_lines), str(exec_lines))
    _check("--dry-run: no placeholder left in the units it would write", not any("@" in ln for ln in rendered))
    user_lines = [ln for ln in rendered if ln.startswith("User=")]
    _check("--dry-run: User= is the user running it", len(user_lines) == 2 and user_lines[0] != "User=" , str(user_lines))
    for step in ("would write /etc/systemd/system/ddm-pi5.service", "would write /etc/systemd/system/ddm-splash.service",
                 "systemctl daemon-reload", "systemctl enable ddm-pi5 ddm-splash", "systemctl restart ddm-pi5 ddm-splash"):
        _check(f"--dry-run: says it would: {step}", step in out)
    _check("--dry-run: and the TV kiosk's autostart (deploy/kiosk_autostart.py, dry)",
           "TV kiosk" in out and "would write ~/.config/autostart/ddm-tv.desktop" in out, out[-600:])
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    _check("--dry-run: changed nothing in the repo", before == after)
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--uninstall", "--dry-run"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, timeout=120)
    _check("--uninstall --dry-run: exit 0, changes nothing", run.returncode == 0 and before == {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}, run.stdout[-500:])
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--bogus"], capture_output=True, text=True,
                         env=ENV, timeout=60)
    _check("an unknown option: exit 2", run.returncode == 2, run.stderr)
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--kiosk", "sometimes"], capture_output=True,
                         text=True, env=ENV, timeout=60)
    _check("--kiosk with something other than on, off or status: exit 2", run.returncode == 2, run.stderr)
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--kiosk", "off", "--dry-run"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, timeout=120)
    _check("--kiosk off --dry-run: only the kiosk's switch, nothing about the services",
           run.returncode == 0 and "TV kiosk: off" in run.stdout and "Checks" not in run.stdout
           and "would" in run.stdout, run.stdout[-600:])
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--retire-old", "--dry-run"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, cwd=root, timeout=300)
    _check("--retire-old --dry-run: runs (here, without systemd, it has nothing to look at)",
           run.returncode in (0, 1) and ("Dry run" in run.stdout or "STOP" in run.stdout), run.stdout[-600:])


# -----------------------------------------------------------------------------
# The TV kiosk: kiosk.sh, deploy/ddm-tv.desktop, deploy/kiosk_autostart.py
# -----------------------------------------------------------------------------

KIOSK = REPO / "splash_display/deploy/kiosk.sh"


def load_kiosk_autostart():
    spec = importlib.util.spec_from_file_location("kiosk_autostart", DEPLOY / "kiosk_autostart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kiosk_files():
    sh = bash()
    text = KIOSK.read_text(encoding="utf-8")
    if sh:
        run = subprocess.run([sh, "-n", str(KIOSK)], capture_output=True, text=True)
        _check("kiosk.sh: bash -n", run.returncode == 0, run.stderr)
    if shutil.which("shellcheck"):
        run = subprocess.run(["shellcheck", str(KIOSK)], capture_output=True, text=True)
        _check("kiosk.sh: shellcheck", run.returncode == 0, run.stdout[-1500:])
    _check("kiosk.sh: LF, no home path", b"\r\n" not in KIOSK.read_bytes() and "/home/" not in text)
    _check("kiosk.sh: the board's page by default, http://localhost:5001/display",
           'URL="${SPLASH_URL:-http://localhost:5001/display}"' in text)
    _check("kiosk.sh: waits for http://localhost:5001/ up to 2 minutes, then starts the browser anyway",
           'WAIT_URL="${SPLASH_WAIT_URL:-http://localhost:5001/}"' in text and 'WAIT_S="${SPLASH_WAIT_S:-120}"' in text
           and "starting the browser anyway" in text)
    ka = load_kiosk_autostart()
    _check("kiosk.sh presses the key kiosk_autostart.py binds (Alt+Super+H)",
           'key="A-W-h"' in ka.KEYBIND and "HIDE_POINTER=(-M alt -M logo -k h -m logo -m alt)" in text)
    desktop = (DEPLOY / "ddm-tv.desktop").read_text(encoding="utf-8")
    entry = parse_unit(desktop).get("Desktop Entry", {})
    _check("ddm-tv.desktop: an Application whose Exec is kiosk.sh, the path filled in by the installer",
           entry.get("Type") == ["Application"] and entry.get("Exec") == ["/bin/bash @REPO@/splash_display/deploy/kiosk.sh"])
    _check("ddm-tv.desktop's Exec is a file in the repo",
           Path(entry["Exec"][0].split()[1].replace("@REPO@", str(REPO))).is_file())


KIOSK_FAKES = {
    "chromium": '#!/bin/bash\necho "$*" >> "$FAKE_LOG/chromium.args"\nexec -a "chromium $*" sleep 6\n',
    "curl": ('#!/bin/bash\nn=$(cat "$FAKE_LOG/curl.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$FAKE_LOG/curl.n"\n'
             '[ $n -ge ${CURL_OK_AT:-3} ]\n'),
    "wtype": '#!/bin/bash\necho "$*" >> "$FAKE_LOG/wtype.args"\n',
}


def test_kiosk_script_runs():
    """kiosk.sh with a fake browser, curl and wtype: it needs flock and pgrep (Linux)."""
    sh = bash()
    if not (sh and shutil.which("flock") and shutil.which("pgrep")):
        print("  (skipped here: kiosk.sh's run needs Linux's flock and pgrep; it runs on DevPi)")
        return
    base = scratch_dir("ddm_kiosk_test_")
    fakes, home, logs = base / "bin", base / "home", base / "log"
    for d in (fakes, home, logs):
        d.mkdir()
    for name, body in KIOSK_FAKES.items():
        (fakes / name).write_text(body, encoding="utf-8")
        (fakes / name).chmod(0o755)
    env = dict(ENV, PATH=f"{fakes}{os.pathsep}{ENV.get('PATH', '')}", HOME=str(home), FAKE_LOG=str(logs),
               WAYLAND_DISPLAY="wayland-test", SPLASH_WAIT_S="20")
    first = subprocess.Popen([sh, str(KIOSK)], env=env, stderr=subprocess.PIPE, text=True)
    time.sleep(1)
    second = subprocess.run([sh, str(KIOSK)], env=env, capture_output=True, text=True, timeout=60)
    _check("a second kiosk.sh while the first runs: does nothing, says so",
           second.returncode == 0 and "another kiosk.sh is running" in second.stderr, second.stderr)
    err = first.communicate(timeout=60)[1]
    _check("the first: exit 0 when its browser closes", first.returncode == 0, err)
    _check("it waited for the splash (curl until it answered, the third time)",
           (logs / "curl.n").read_text().strip() == "3")
    args = (logs / "chromium.args").read_text().split()
    _check("one browser, kiosk mode, on http://localhost:5001/display",
           len((logs / "chromium.args").read_text().splitlines()) == 1 and "--kiosk" in args
           and args[-1] == "http://localhost:5001/display", str(args[-3:]))
    _check("then Alt+Super+H, once (the pointer off)",
           (logs / "wtype.args").read_text().splitlines() == ["-M alt -M logo -k h -m logo -m alt"])
    for f in ("curl.n", "chromium.args"):
        (logs / f).unlink()
    started = time.time()
    run = subprocess.run([sh, str(KIOSK)], env=dict(env, CURL_OK_AT="999", SPLASH_WAIT_S="4"),
                         capture_output=True, text=True, timeout=60)
    _check("the splash never answers: the browser starts anyway after the wait",
           run.returncode == 0 and "starting the browser anyway" in run.stderr
           and (logs / "chromium.args").is_file() and time.time() - started < 30, run.stderr)


RC_WITH_KEYBINDS = ('<?xml version="1.0"?>\r\n<labwc_config>\r\n  <theme><name>PiXnoir</name></theme>\r\n'
                    '  <keyboard>\r\n    <keybind key="W-Return"><action name="Execute" command="lxterminal" />'
                    '</keybind>\r\n  </keyboard>\r\n</labwc_config>\r\n')
RC_NO_KEYBOARD = '<?xml version="1.0"?>\n<labwc_config>\n  <theme><name>PiXflat</name></theme>\n</labwc_config>\n'
RC_EMPTY_KEYBOARD = ('<?xml version="1.0"?>\n<labwc_config>\n  <keyboard>\n    <repeatRate>25</repeatRate>\n'
                     '  </keyboard>\n</labwc_config>\n')
RC_COMMENTED = ('<?xml version="1.0"?>\n<labwc_config>\n  <!-- an example:\n  <keyboard>\n    <keybind key="W-x" />\n'
                '  </keyboard>\n  -->\n</labwc_config>\n')
RC_SELF_CLOSING = '<?xml version="1.0"?>\n<labwc_config>\n  <keyboard />\n</labwc_config>\n'


def test_kiosk_autostart():
    import xml.etree.ElementTree as ET
    ka = load_kiosk_autostart()
    base = scratch_dir("ddm_kiosk_home_")
    system_autostart = base / "etc-xdg-labwc-autostart"
    ka.SYSTEM_LABWC_AUTOSTART = system_autostart
    ka.labwc_present = lambda: True

    def run(action, home, dry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ka.main([action, "--home", str(home)] + (["--dry-run"] if dry else []))
        return code, out.getvalue()

    def files(home):
        return {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*") if p.is_file()}

    def keybind_ok(text, default):
        root = ET.fromstring(text.split("?>", 1)[1] if text.startswith("<?xml") else text)
        keyboards = root.findall("keyboard")
        binds = [k for kb in keyboards for k in kb.findall("keybind") if k.get("key") == "A-W-h"]
        actions = [a.get("name") for a in binds[0].findall("action")] if len(binds) == 1 else []
        defaults = [d for kb in keyboards for d in kb.findall("default")]
        return actions == ["HideCursor", "WarpCursor"] and (len(defaults) == 1) == default

    system_autostart.write_text("/usr/bin/lwrespawn /usr/bin/wf-panel-pi &\n/usr/bin/kanshi &\n"
                                "/usr/bin/lxsession-xdg-autostart\n", encoding="utf-8")
    home = base / "home1"
    home.mkdir()
    code, out = run("install", home, dry=True)
    _check("dry run: says what it would write, writes nothing",
           code == 0 and "would" in out and files(home) == {}, out)
    code, out = run("install", home)
    desktop = (home / ".config/autostart/ddm-tv.desktop").read_text(encoding="utf-8")
    _check("install: ~/.config/autostart/ddm-tv.desktop runs this repo's kiosk.sh",
           f"Exec=/bin/bash {REPO.as_posix()}/splash_display/deploy/kiosk.sh" in desktop and "@REPO@" not in desktop, desktop)
    rc = (home / ".config/labwc/rc.xml").read_text(encoding="utf-8")
    _check("install, no rc.xml: one written, Alt+Super+H -> HideCursor + WarpCursor, with <default />",
           keybind_ok(rc, default=True), rc)
    _check("install: the system autostart runs lxsession-xdg-autostart, so no labwc autostart line",
           not (home / ".config/labwc/autostart").exists() and "runs lxsession-xdg-autostart" in out)
    snapshot = files(home)
    code, out = run("install", home)
    _check("install twice: nothing changes", files(home) == snapshot and "already so" in out, out)
    code, out = run("uninstall", home)
    _check("uninstall: everything it wrote is gone", code == 0 and files(home) == {}, str(files(home)))

    for name, original, default in (("keybinds in <keyboard> (CRLF)", RC_WITH_KEYBINDS, False),
                                    ("no <keyboard>", RC_NO_KEYBOARD, True),
                                    ("a <keyboard> with no keybind", RC_EMPTY_KEYBOARD, True),
                                    ("only a commented-out <keyboard>", RC_COMMENTED, True)):
        home = base / f"home-{len(name)}-{abs(hash(name)) % 1000}"
        (home / ".config/labwc").mkdir(parents=True)
        rc_path = home / ".config/labwc/rc.xml"
        rc_path.write_bytes(original.encode("utf-8"))
        run("install", home)
        after = rc_path.read_bytes().decode("utf-8")
        _check(f"rc.xml with {name}: the keybind added, <default /> {'added' if default else 'not added'}, still XML",
               keybind_ok(after, default) and after != original, after)
        _check(f"rc.xml with {name}: line endings kept",
               ("\r\n" in after) == ("\r\n" in original) and after.replace("\r\n", "").count("\n") == 0
               if "\r\n" in original else "\r\n" not in after)
        run("uninstall", home)
        _check(f"rc.xml with {name}: uninstall puts it back byte for byte",
               rc_path.read_bytes() == original.encode("utf-8"), rc_path.read_text(encoding="utf-8"))

    home = base / "home-selfclosing"
    (home / ".config/labwc").mkdir(parents=True)
    (home / ".config/labwc/rc.xml").write_text(RC_SELF_CLOSING, encoding="utf-8")
    code, out = run("install", home)
    _check("rc.xml with <keyboard />: left alone, with a WARN saying what to add by hand",
           (home / ".config/labwc/rc.xml").read_text(encoding="utf-8") == RC_SELF_CLOSING and "WARN" in out, out)

    # A system autostart that does not run lxsession-xdg-autostart: the kiosk goes in the labwc one.
    system_autostart.write_text("/usr/bin/kanshi &\n", encoding="utf-8")
    home = base / "home-labwc-autostart"
    (home / ".config/labwc").mkdir(parents=True)
    user_autostart = home / ".config/labwc/autostart"
    original = "swayidle -w timeout 600 'wlopm --off \\*' resume 'wlopm --on \\*' &\n"
    user_autostart.write_text(original, encoding="utf-8")
    code, out = run("install", home)
    text = user_autostart.read_text(encoding="utf-8")
    _check("no lxsession-xdg-autostart: one marked line starts kiosk.sh from ~/.config/labwc/autostart",
           text.startswith(original) and f"/bin/bash {REPO.as_posix()}/splash_display/deploy/kiosk.sh &" in text
           and text.count("ddm-tv") == 1 and "WARN" in out, text)
    _check("swayidle there: reported as screen blanking on, the file's own line untouched",
           "screen blanking is on" in out and original in text)
    run("uninstall", home)
    _check("uninstall: the labwc autostart back byte for byte", user_autostart.read_text(encoding="utf-8") == original)
    home = base / "home-labwc-new"
    home.mkdir()
    run("install", home)
    created = (home / ".config/labwc/autostart").is_file()
    run("uninstall", home)
    _check("a labwc autostart it created: uninstall removes it", created and not (home / ".config/labwc/autostart").exists())

    # The keybind edited by hand: uninstall leaves it and says so.
    home = base / "home-edited"
    (home / ".config/labwc").mkdir(parents=True)
    (home / ".config/labwc/rc.xml").write_text(RC_NO_KEYBOARD, encoding="utf-8")
    run("install", home)
    rc_path = home / ".config/labwc/rc.xml"
    edited = rc_path.read_text(encoding="utf-8").replace('x="-1"', 'x="-2"')
    rc_path.write_text(edited, encoding="utf-8")
    code, out = run("uninstall", home)
    _check("uninstall after a hand edit of the keybind: the file left as it is, a WARN",
           rc_path.read_text(encoding="utf-8") == edited and "edited by hand" in out, out)


# -----------------------------------------------------------------------------
# Other starters (deploy/starters.py) and the kiosk switch
# -----------------------------------------------------------------------------

def load_module(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_starters():
    st = load_module("starters")
    base = scratch_dir("ddm_starters_")
    home = base / "home" / "joey"
    repo = home / "DDM-Multimedia"
    fs_root = base / "root"
    (repo / "pi5").mkdir(parents=True)
    (fs_root / "etc/systemd/system").mkdir(parents=True)
    main_py, server_py = str(repo / "pi5" / "main.py"), str(repo / "splash_display" / "server.py")

    class FakeRunner(st.Runner):
        """systemctl and crontab as a fixture: queries answered from `answers`, actions recorded."""

        def __init__(self, answers, dry=False):
            self.dry, self.sudo, self.answers, self.acts = dry, ["sudo"], answers, []

        def query(self, cmd, root=False):
            key = " ".join(cmd)
            for prefix, answer in self.answers.items():
                if key.startswith(prefix):
                    return answer
            return None

        def act(self, cmd, root=False, stdin=None):
            args = (self.sudo if root else []) + list(cmd)
            print(("  would run: " if self.dry else "  + ") + " ".join(args))
            self.acts.append((args, stdin))
            return True

    def unit_block(unit, argv, wd, state, active, fragment):
        return (f"Id={unit}\nExecStart={{ path=/usr/bin/python3 ; argv[]={argv} ; ignore_errors=no ; start_time=[n/a] }}\n"
                f"WorkingDirectory={wd}\nUnitFileState={state}\nActiveState={active}\nFragmentPath={fragment}\n")

    clean = {
        "systemctl --no-pager list-unit-files": "ddm-pi5.service enabled enabled\nddm-splash.service enabled enabled\n"
                                                "ssh.service enabled enabled\n",
        "systemctl --no-pager list-units": "ddm-pi5.service loaded active running DDM pi5\nssh.service loaded active running\n",
        "systemctl --no-pager show": unit_block("ssh.service", "/usr/sbin/sshd -D", "/", "enabled", "active",
                                                "/usr/lib/systemd/system/ssh.service"),
        "systemctl --user --no-pager list-unit-files": "",
        "systemctl --user --no-pager list-units": "",
        "crontab -l -u root": "",
        "crontab -l": "# m h dom mon dow command\n",
    }

    def run(action, answers, dry=False):
        runner = FakeRunner(answers, dry)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = st.main([action, "--repo", str(repo)] + (["--dry-run"] if dry else []), runner=runner,
                           home=home, fs_root=fs_root)
        return code, out.getvalue(), runner

    code, out, _ = run("check", clean)
    _check("a clean system: nothing else starts pi5 or the splash, exit 0",
           code == 0 and "nothing else starts pi5 or the splash at boot" in out, out)

    # DevPi on 2026-10-11, and every other kind of starter there is.
    ddm = unit_block("ddm.service", f"/usr/bin/python3 {main_py}", str(repo / "pi5"), "enabled", "inactive",
                     "/etc/systemd/system/ddm.service")
    packaged = unit_block("dashboard.service", f"/usr/bin/python3 {server_py}", "/", "enabled", "active",
                          "/usr/lib/systemd/system/dashboard.service")
    rogue = dict(clean)
    rogue["systemctl --no-pager list-unit-files"] += "ddm.service enabled enabled\ndashboard.service enabled enabled\n"
    rogue["systemctl --no-pager show"] = clean["systemctl --no-pager show"] + "\n" + ddm + "\n" + packaged
    tilde = "~" + str(repo)[len(str(home)):]
    cron = f"# m h dom mon dow command\n@reboot cd {tilde}/splash_display && python3 server.py\n0 3 * * * /usr/bin/true\n"
    rogue["crontab -l"] = cron
    (fs_root / "etc/systemd/system/ddm.service").write_text("[Service]\nExecStart=/usr/bin/python3 main.py\n",
                                                            encoding="utf-8")
    rc_local = fs_root / "etc/rc.local"
    rc_local.write_text(f"#!/bin/sh -e\npython3 {main_py} &\nexit 0\n", encoding="utf-8")
    autostart = home / ".config/autostart"
    autostart.mkdir(parents=True)
    (autostart / "dashboard.desktop").write_text(f"[Desktop Entry]\nType=Application\nExec=python3 {main_py}\n",
                                                 encoding="utf-8")
    kiosk_entry = "[Desktop Entry]\nType=Application\nExec=/bin/bash kiosk.sh\n"
    (autostart / "ddm-tv.desktop").write_text(kiosk_entry, encoding="utf-8")
    labwc = home / ".config/labwc/autostart"
    labwc.parent.mkdir(parents=True)
    labwc.write_text(f"/usr/bin/kanshi &\npython3 {server_py} &\n", encoding="utf-8")

    code, out, _ = run("check", rogue)
    for what in ("unit ddm.service", "unit dashboard.service", "crontab", "rc.local", "labwc autostart",
                 "autostart entry"):
        _check(f"check: STOP names the {what}", f"STOP  {what}" in out, out)
    _check("check: the kiosk's own ddm-tv.desktop is not a starter", "ddm-tv.desktop" not in out)
    _check("check: exit 1 and says how to retire them", code == 1 and "--retire-old" in out)
    _check("check: ssh.service and the cron line at 3 a.m. are left out", "ssh" not in out and "/usr/bin/true" not in out)

    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    code, out, runner = run("retire", rogue, dry=True)
    _check("retire --dry-run: says each step, changes nothing",
           code == 0 and "would run: sudo systemctl disable --now ddm.service" in out
           and before == {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}, out)

    code, out, runner = run("retire", rogue)
    acts = [" ".join(a) for a, _ in runner.acts]
    _check("retire: ddm.service disabled and stopped, then daemon-reload",
           "sudo systemctl disable --now ddm.service" in acts and acts[-1] == "sudo systemctl daemon-reload", str(acts))
    _check("retire: its file moved to the repo's backups/ddm.service.old",
           (repo / "backups/ddm.service.old").is_file() and not (fs_root / "etc/systemd/system/ddm.service").exists())
    _check("retire: a package's unit is masked, not moved", "sudo systemctl mask dashboard.service" in acts, str(acts))
    new_cron = [stdin for a, stdin in runner.acts if a[:2] == ["crontab", "-"]]
    _check("retire: the crontab's @reboot line commented out behind a marker, the rest kept",
           len(new_cron) == 1 and "# ddm-retired" in new_cron[0] and "0 3 * * * /usr/bin/true" in new_cron[0]
           and not any(ln.startswith("@reboot") for ln in new_cron[0].splitlines()), str(new_cron))
    rc = rc_local.read_text(encoding="utf-8")
    _check("retire: rc.local's line commented out, exit 0 kept",
           f"# ddm-retired" in rc and "exit 0\n" in rc and not any(ln.startswith("python3") for ln in rc.splitlines()), rc)
    desktop = (autostart / "dashboard.desktop").read_text(encoding="utf-8")
    _check("retire: the autostart entry gets Hidden=true", "Hidden=true" in desktop, desktop)
    _check("retire: the kiosk's own entry untouched", (autostart / "ddm-tv.desktop").read_text(encoding="utf-8") == kiosk_entry)
    _check("retire: the labwc autostart line commented out, kanshi kept",
           labwc.read_text(encoding="utf-8").startswith("/usr/bin/kanshi &\n# ddm-retired"))
    _check("retire: every step printed", all(f"+ {a}" in out for a in acts) and "+ moved" in out and "+ edited" in out, out)
    after = dict(clean)
    after["crontab -l"] = new_cron[0]
    code, out, _ = run("check", after)
    _check("check after retiring: nothing left", code == 0, out)


JOEY_RC = '''<?xml version="1.0"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <theme>
    <font place="ActiveWindow">
      <name>Nunito Sans</name>
      <size>12</size>
      <weight>Light</weight>
      <slant>Normal</slant>
    </font>
    <font place="InactiveWindow">
      <name>Nunito Sans</name>
      <size>12</size>
      <weight>Light</weight>
      <slant>Normal</slant>
    </font>
    <font place="OnScreenDisplay">
      <name>Nunito Sans</name>
      <size>12</size>
      <weight>Light</weight>
      <slant>Normal</slant>
    </font>
    <font place="MenuItem">
      <name>Nunito Sans</name>
      <size>12</size>
      <weight>Light</weight>
      <slant>Normal</slant>
    </font>
    <name>PiXonyx</name>
  </theme>
</openbox_config>
'''


def test_rc_xml_on_pi_os():
    """Joey's ~/.config/labwc/rc.xml on DevPi, verbatim: Pi OS's shape (openbox_config in the
    openbox namespace, a <theme>, no <keyboard>), which the first version could not add to.
    Whether labwc accepts the result is for DevPi (labwc --reconfigure)."""
    import xml.etree.ElementTree as ET
    ka = load_kiosk_autostart()
    ka.labwc_present = lambda: True
    ka.SYSTEM_LABWC_AUTOSTART = scratch_dir("ddm_rc_sys_") / "autostart"
    ka.SYSTEM_LABWC_AUTOSTART.write_text("/usr/bin/lxsession-xdg-autostart\n", encoding="utf-8")
    ns = "{http://openbox.org/3.4/rc}"
    for name, original in (("Joey's rc.xml (LF)", JOEY_RC), ("Joey's rc.xml with a blank line at the end", JOEY_RC + "\n"),
                           ("Joey's rc.xml, CRLF", JOEY_RC.replace("\n", "\r\n"))):
        home = scratch_dir("ddm_rc_home_")
        rc = home / ".config/labwc/rc.xml"
        rc.parent.mkdir(parents=True)
        rc.write_bytes(original.encode("utf-8"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ka.main(["install", "--home", str(home)])
        after = rc.read_bytes().decode("utf-8")
        root = ET.fromstring(after.encode("utf-8"))
        keyboard = root.find(f"{ns}keyboard")
        bind = keyboard.find(f"{ns}keybind") if keyboard is not None else None
        _check(f"{name}: the keybind lands, in a <keyboard> with <default />, in the openbox namespace",
               root.tag == f"{ns}openbox_config" and bind is not None and bind.get("key") == "A-W-h"
               and [a.get("name") for a in bind.findall(f"{ns}action")] == ["HideCursor", "WarpCursor"]
               and keyboard.find(f"{ns}default") is not None, after)
        _check(f"{name}: the declaration and the namespace survive, no ns0", after.startswith(
            original.split("\n")[0]) and '<openbox_config xmlns="http://openbox.org/3.4/rc">' in after and "ns0" not in after)
        theme = original[original.index("  <theme>"):original.index("</theme>") + len("</theme>")]
        added = ka.new_keyboard(ka.eol_of(original))
        _check(f"{name}: the theme block byte for byte, and only the keyboard added, just before the end tag",
               theme in after and after.replace(added, "", 1) == original
               and after.index(added) == original.rindex("</openbox_config>"))
        _check(f"{name}: install says how to load it (labwc --reconfigure)", "labwc --reconfigure" in out.getvalue(),
               out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            ka.main(["install", "--home", str(home)])
        _check(f"{name}: a second run changes nothing", rc.read_bytes().decode("utf-8") == after)
        with contextlib.redirect_stdout(io.StringIO()):
            ka.main(["uninstall", "--home", str(home)])
        _check(f"{name}: uninstall gives it back byte for byte", rc.read_bytes() == original.encode("utf-8"))
    # A root it does not know, or a file that is not XML: left alone, with the lines to add by hand.
    # The last case: the root's <keyboard> comes after another element's <keyboard>, so the text
    # insertion lands in the wrong one; reading the result back as XML catches it.
    for name, text in (("another root element", '<?xml version="1.0"?>\n<config><theme/></config>\n'),
                       ("not XML", "<openbox_config><theme></openbox_config>\n"),
                       ("a <keyboard> inside another element before the root's own",
                        '<?xml version="1.0"?>\n<labwc_config>\n  <mouse><keyboard>x</keyboard></mouse>\n'
                        '  <keyboard>\n    <keybind key="W-x" />\n  </keyboard>\n</labwc_config>\n')):
        home = scratch_dir("ddm_rc_home_")
        rc = home / ".config/labwc/rc.xml"
        rc.parent.mkdir(parents=True)
        rc.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ka.main(["install", "--home", str(home)])
        _check(f"rc.xml with {name}: untouched, a WARN with the lines to add by hand",
               rc.read_text(encoding="utf-8") == text and "WARN" in out.getvalue()
               and "<keyboard><default />" in out.getvalue(), out.getvalue())


def test_kiosk_switch():
    ka = load_kiosk_autostart()
    base = scratch_dir("ddm_kiosk_switch_")
    ka.SYSTEM_LABWC_AUTOSTART = base / "etc-xdg-labwc-autostart"
    ka.SYSTEM_LABWC_AUTOSTART.write_text("/usr/bin/kanshi &\n/usr/bin/lxsession-xdg-autostart\n", encoding="utf-8")
    ka.labwc_present = lambda: False
    home = base / "home"
    home.mkdir()
    entry = home / ".config/autostart/ddm-tv.desktop"

    def run(action, dry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ka.main([action, "--home", str(home)] + (["--dry-run"] if dry else []))
        return code, out.getvalue()

    _check("status before anything: not installed", "not installed" in run("status")[1])
    run("install")
    code, out = run("status")
    _check("installed: the kiosk at login is on", "the kiosk at login: on" in out, out)
    code, out = run("off", dry=True)
    _check("off --dry-run: says it, changes nothing", "would" in out and "Hidden" not in entry.read_text(encoding="utf-8"))
    code, out = run("off")
    _check("off: Hidden=true in ddm-tv.desktop (lxsession-xdg-autostart skips it)",
           code == 0 and "Hidden=true" in entry.read_text(encoding="utf-8"), out)
    _check("off: it says whether a kiosk was running (or that it can't tell here)",
           "no kiosk running" in out or "can't see running processes" in out, out)
    snapshot = entry.read_bytes()
    run("off")
    _check("off twice: nothing changes", entry.read_bytes() == snapshot)
    _check("status: off", "the kiosk at login: off" in run("status")[1])
    run("install")
    _check("the installer run again keeps it off", "Hidden=true" in entry.read_text(encoding="utf-8"))
    code, out = run("on")
    _check("on: Hidden=true gone, and how to start it now without logging out",
           "Hidden" not in entry.read_text(encoding="utf-8") and "To start it now" in out
           and "splash_display/deploy/kiosk.sh &" in out, out)
    _check("on twice: nothing changes", "already so" in run("on")[1])

    # Switched off by hand, as Joey did: ddm-tv.desktop renamed to ddm-tv.desktop.off.
    entry.rename(entry.with_name("ddm-tv.desktop.off"))
    _check("renamed .off by hand: status says off", "the kiosk at login: off" in run("status")[1])
    run("install")
    _check("the installer on a renamed entry: renamed back, kept off, one entry",
           entry.is_file() and "Hidden=true" in entry.read_text(encoding="utf-8")
           and not entry.with_name("ddm-tv.desktop.off").exists())
    entry.rename(entry.with_name("ddm-tv.desktop.off"))
    run("on")
    _check("on from a renamed entry: renamed back and on",
           entry.is_file() and "Hidden" not in entry.read_text(encoding="utf-8")
           and not entry.with_name("ddm-tv.desktop.off").exists())

    # Where the system's labwc autostart does not run lxsession-xdg-autostart: the labwc line too.
    ka.SYSTEM_LABWC_AUTOSTART.write_text("/usr/bin/kanshi &\n", encoding="utf-8")
    run("install")
    labwc = home / ".config/labwc/autostart"
    run("off")
    off_text = labwc.read_text(encoding="utf-8")
    run("on")
    on_text = labwc.read_text(encoding="utf-8")
    _check("the labwc autostart line: commented out by off, back by on",
           off_text.startswith("# ddm-tv-off: /bin/bash ") and on_text.startswith("/bin/bash ")
           and "kiosk.sh" in on_text, f"{off_text!r} {on_text!r}")
    entry.rename(entry.with_name("ddm-tv.desktop.off"))
    run("uninstall")
    _check("uninstall: the entry (even renamed .off) and the labwc line gone",
           not entry.exists() and not entry.with_name("ddm-tv.desktop.off").exists() and not labwc.exists(),
           str([p.name for p in home.rglob("*")]))
    kiosk = (REPO / "splash_display/deploy/kiosk.sh").read_text(encoding="utf-8")
    _check("kiosk.sh keeps its PID where off looks for it",
           'echo "$$" > "$PROFILE_DIR.pid"' in kiosk and 'PROFILE_DIR="${HOME}/.config/chromium-kiosk"' in kiosk
           and ka.profile_dir(Path("/h")).as_posix() == "/h/.config/chromium-kiosk")


def test_kiosk_off_stops_a_running_kiosk():
    """Linux: a fake kiosk.sh (its PID in the file) and a fake browser on the scratch profile:
    off stops both, and nothing else."""
    if os.name != "posix" or not shutil.which("pgrep") or not Path("/proc").is_dir():
        print("  (skipped here: stopping a running kiosk needs Linux's /proc and pgrep; it runs on DevPi)")
        return
    ka = load_kiosk_autostart()
    ka.labwc_present = lambda: False
    base = scratch_dir("ddm_kiosk_stop_")
    home = base / "home"
    (home / ".config").mkdir(parents=True)
    fake_kiosk = base / "kiosk.sh"
    fake_kiosk.write_text("#!/bin/bash\nsleep 60\n", encoding="utf-8")
    script = subprocess.Popen(["bash", str(fake_kiosk)])
    browser = subprocess.Popen(["bash", "-c", f'exec -a "chromium --user-data-dir={home}/.config/chromium-kiosk" sleep 60'])
    bystander = subprocess.Popen(["sleep", "60"])
    try:
        Path(f"{home}/.config/chromium-kiosk.pid").write_text(f"{script.pid}\n")
        time.sleep(0.5)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ka.main(["off", "--home", str(home)])
        time.sleep(0.5)
        _check("off: the kiosk's kiosk.sh and its browser stopped",
               script.poll() is not None and browser.poll() is not None, out.getvalue())
        _check("off: nothing else stopped", bystander.poll() is None)
    finally:
        for p in (script, browser, bystander):
            if p.poll() is None:
                p.kill()


def main():
    started = time.time()
    print(f"deploy/ tests, repo {REPO}")
    _run("the unit files", test_unit_files)
    _run("the hand-run guard and the access log", test_hand_run_guard)
    _run("the LED controller client logs on change", test_led_client_logs_on_change)
    _run("the installer", test_installer)
    _run("the kiosk script and its desktop entry", test_kiosk_files)
    _run("kiosk.sh with a fake browser", test_kiosk_script_runs)
    _run("the kiosk's autostart and the hide-pointer keybind", test_kiosk_autostart)
    _run("other starters of pi5 or the splash, and --retire-old", test_starters)
    _run("the pointer keybind on Pi OS's rc.xml", test_rc_xml_on_pi_os)
    _run("the kiosk switch: on, off, status", test_kiosk_switch)
    _run("--kiosk off stops a running kiosk", test_kiosk_off_stops_a_running_kiosk)
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

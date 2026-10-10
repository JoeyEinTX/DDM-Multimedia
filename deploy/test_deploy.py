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
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
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
    _check("back: one line saying so, then the command's own line as before",
           lines == ["[ESP32] LED controller reachable again at 10.0.0.42:5005",
                     "[ESP32] Sent: STATUS | Received: STATUS:120:300:80"], str(lines))
    _check("back: the status parsed, connected True",
           result == {"current_ma": 120, "peak_ma": 300, "min_ma": 80} and client.is_connected())
    _, lines = call(client.get_power_status)
    _check("still up: no reachability line", lines == ["[ESP32] Sent: STATUS | Received: STATUS:120:300:80"], str(lines))
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
    for rel in ("pi5/main.py", "splash_display/server.py"):
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
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    _check("--dry-run: changed nothing in the repo", before == after)
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--uninstall", "--dry-run"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, timeout=120)
    _check("--uninstall --dry-run: exit 0, changes nothing", run.returncode == 0 and before == {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}, run.stdout[-500:])
    run = subprocess.run([sh, str(root / "deploy/install_services.sh"), "--bogus"], capture_output=True, text=True,
                         env=ENV, timeout=60)
    _check("an unknown option: exit 2", run.returncode == 2, run.stderr)


def main():
    started = time.time()
    print(f"deploy/ tests, repo {REPO}")
    _run("the unit files", test_unit_files)
    _run("the hand-run guard and the access log", test_hand_run_guard)
    _run("the LED controller client logs on change", test_led_client_logs_on_change)
    _run("the installer", test_installer)
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

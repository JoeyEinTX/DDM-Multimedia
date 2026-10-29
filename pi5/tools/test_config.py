# pi5/tools/test_config.py - secrets and a box's own settings live in .env, never in a tracked file
#
# Run with: python pi5/tools/test_config.py  (from the repo root, or anywhere)
#
# pi5/config.py and splash_display/config.py on scratch copies with and without a
# .env (each run in its own python, so nothing leaks between cases); the real
# main.py started on a scratch copy with no .env; pi5/tools/env_from_config.py on a
# scratch git repo carrying a DevPi-style local edit; and a scan of every tracked
# file for secrets, so one can't be committed again without this going red. Same
# hand-rolled runner as the other suites. Standard library only.

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PI5 = REPO / "pi5"
TOOL = HERE / "env_from_config.py"
ENV = {k: v for k, v in os.environ.items() if not k.startswith("DDM_") and k != "ANTHROPIC_API_KEY"}
ENV["PYTHONIOENCODING"] = "utf-8"
ENV["PYTHONDONTWRITEBYTECODE"] = "1"

_results = []
_scratch = []


def _check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((status, name, detail))
    print(f"  {'[OK]' if condition else '[XX]'} {name}" + (f"  -- {detail}" if detail and not condition else ""))
    return condition


def _run(name, fn):
    print(f"\n=== {name} ===")
    try:
        fn()
    except Exception as exc:
        traceback.print_exc()
        _check(f"{name} (uncaught exception)", False, str(exc))


def scratch(prefix):
    d = Path(tempfile.mkdtemp(prefix=prefix))
    _scratch.append(d)
    return d


def fake_secret(n=31):
    """A secret-shaped value made at run time, so this file holds no such literal."""
    return "".join("0123456789abcdef"[(i * 7 + 3) % 16] for i in range(n))


def py(cwd, code, env=None):
    run = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(ENV, **(env or {})), capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)
    return run


SETTINGS_JSON = ("import json, config as c; print(json.dumps({k: getattr(c, k) for k in dir(c) "
                 "if k.isupper() and isinstance(getattr(c, k), (str, int, float, bool, list))}))")


def config_values(env_text=None, env=None, folder="pi5"):
    """The config module's settings in a fresh python: a copy of <folder>/config.py, with
    env_text as its .env when given."""
    d = scratch("ddm_cfg_")
    shutil.copy2(REPO / folder / "config.py", d / "config.py")
    if env_text is not None:
        (d / ".env").write_bytes(env_text.encode("utf-8"))
    run = py(d, SETTINGS_JSON, env)
    if run.returncode != 0:
        raise RuntimeError(run.stderr[-800:])
    return json.loads(run.stdout.strip().splitlines()[-1]), run


# -----------------------------------------------------------------------------
# pi5/config.py
# -----------------------------------------------------------------------------

def test_defaults_with_no_env():
    v, run = config_values()
    _check("no .env: the weather key is empty (the weather is off)", v["WEATHER_API_KEY"] == "")
    _check("no .env: the odds poller's key is empty", v["ANTHROPIC_API_KEY"] == "")
    _check("no .env: no LED controller (ESP32_IP empty), port 5005", v["ESP32_IP"] == "" and v["ESP32_PORT"] == 5005)
    _check("no .env: no tote board (TOTE_IP empty, TOTE_ENABLED false)", v["TOTE_IP"] == "" and v["TOTE_ENABLED"] is False)
    _check("no .env: the bridge idles (LQ_SERIAL_PORT empty)", v["LQ_SERIAL_PORT"] == "")
    _check("no .env: no host PIN (LS_ADMIN_PIN empty): La Subasta's admin page is off", v["LS_ADMIN_PIN"] == "")
    _check("no .env: pi5 on 0.0.0.0:5000, the weather for Dallas,TX",
           v["FLASK_HOST"] == "0.0.0.0" and v["FLASK_PORT"] == 5000 and v["WEATHER_LOCATION"] == "Dallas,TX")
    v2, _ = config_values()
    _check("no .env: Flask's secret key is random, 48 hex characters, a new one each start",
           re.fullmatch(r"[0-9a-f]{48}", v["FLASK_SECRET_KEY"]) is not None and v["FLASK_SECRET_KEY"] != v2["FLASK_SECRET_KEY"])
    _check("no .env: importing config prints nothing", run.stdout.strip().count("\n") == 0 and not run.stderr.strip(),
           run.stderr[-300:])
    _check("no .env: the money's defaults are as they were (TOKEN_VALUE 1.00, the 60/25/15 split)",
           v["TOKEN_VALUE"] == 1.0 and (v["LQ_SPLIT_WIN"], v["LQ_SPLIT_PLACE"], v["LQ_SPLIT_SHOW"]) == (0.60, 0.25, 0.15))


def test_env_file_lands_by_name():
    key, odds, flask_key, pin = fake_secret(31), fake_secret(40), fake_secret(32), fake_secret(8)
    env_text = "\r\n".join([
        "# a comment",
        f"DDM_WEATHER_API_KEY={key}",
        f"export DDM_ANTHROPIC_API_KEY=\"{odds}\"",
        f"DDM_FLASK_SECRET_KEY='{flask_key}'",
        f"DDM_LS_ADMIN_PIN={pin}",
        "DDM_LQ_SERIAL_PORT=/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
        "DDM_ESP32_IP=10.0.0.42   # the mantle",
        "DDM_ESP32_PORT=5006",
        "DDM_TOTE_IP=10.0.0.124",
        "DDM_TOTE_PORT=81",
        "DDM_FLASK_HOST=127.0.0.1",
        "DDM_FLASK_PORT=5050",
        "DDM_WEATHER_LOCATION=Louisville,KY",
        "DDM_LQ_SERIAL_BAUD=57600",
        "DDM_LQ_BRIDGE_ENABLED=0",
        "DDM_LQ_CUP_OFFLINE_S=9",
        "", ""])
    v, _ = config_values(env_text)
    expect = {
        "WEATHER_API_KEY": key, "ANTHROPIC_API_KEY": odds, "FLASK_SECRET_KEY": flask_key, "LS_ADMIN_PIN": pin,
        "LQ_SERIAL_PORT": "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
        "ESP32_IP": "10.0.0.42", "ESP32_PORT": 5006, "TOTE_IP": "10.0.0.124", "TOTE_PORT": 81, "TOTE_ENABLED": True,
        "FLASK_HOST": "127.0.0.1", "FLASK_PORT": 5050, "WEATHER_LOCATION": "Louisville,KY",
        "LQ_SERIAL_BAUD": 57600, "LQ_BRIDGE_ENABLED": False, "LQ_CUP_OFFLINE_S": 9,
    }
    for name, want in expect.items():
        shown = "<secret>" if name in ("WEATHER_API_KEY", "ANTHROPIC_API_KEY", "FLASK_SECRET_KEY", "LS_ADMIN_PIN") else repr(want)
        _check(f".env: DDM_{name} lands in config.{name} ({shown})", v.get(name) == want and type(v.get(name)) is type(want))
    v, _ = config_values("DDM_TOTE_IP=10.0.0.124\nDDM_TOTE_ENABLED=0\n")
    _check(".env: DDM_TOTE_ENABLED=0 turns a set tote board off", v["TOTE_IP"] == "10.0.0.124" and v["TOTE_ENABLED"] is False)
    v, _ = config_values(f"ANTHROPIC_API_KEY={odds}\n")
    _check(".env: an unprefixed ANTHROPIC_API_KEY works too (the Shop PC's .env has one)", v["ANTHROPIC_API_KEY"] == odds)
    v, _ = config_values("DDM_ESP32_IP=10.0.0.42\n", env={"DDM_ESP32_IP": "10.9.9.9"})
    _check("a variable already set in the environment wins over .env", v["ESP32_IP"] == "10.9.9.9")


def test_splash_env():
    v, _ = config_values(folder="splash_display")
    _check("splash, no .env: pi5 on the same box (http://localhost:5000), the dots look",
           v["PI5_URL"] == "http://localhost:5000" and v["QUINIELA_LOOK"] == "dots", str((v["PI5_URL"], v["QUINIELA_LOOK"])))
    v, _ = config_values("DDM_PI5_URL=http://joeydevpi.local:5000\nDDM_QUINIELA_LOOK=impact\n", folder="splash_display")
    _check("splash .env: DDM_PI5_URL and DDM_QUINIELA_LOOK land",
           v["PI5_URL"] == "http://joeydevpi.local:5000" and v["QUINIELA_LOOK"] == "impact")
    _check("splash: its port and the rest as before (5001)", v["FLASK_PORT"] == 5001)


def test_led_client_off_without_an_address():
    d = scratch("ddm_led_")
    shutil.copytree(PI5 / "communication", d / "communication", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(PI5 / "config.py", d / "config.py")
    run = py(d, "import socket\n"
                "def refuse(*a, **k): raise AssertionError('a socket was opened')\n"
                "socket.socket = refuse\n"
                "from communication.esp32_client import ESP32Client\n"
                "c = ESP32Client(ip='')\n"
                "print(c.send('ANIM:BETTING_60'), c.ask('STATUS'), c.ping(), c.health()['enabled'])")
    _check("no LED address: a send is dropped (and returns what was asked), an ask answers ERROR:NO_LED_CONTROLLER at once, no socket opened",
           run.stdout.strip() == "ANIM:BETTING_60 ERROR:NO_LED_CONTROLLER False False", run.stdout + run.stderr[-400:])
    _check("no LED address: and logs nothing per command", run.stdout.strip().count("\n") == 0, run.stdout)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_main_starts_with_no_env():
    """The real main.py on a scratch copy with no .env (only its own port set):
    one line says the LEDs are off, one that the weather is off, the bridge idles;
    the device panel's LED address goes into pi5/.env, never config.py."""
    root = scratch("ddm_main_") / "pi5"
    shutil.copytree(PI5, root, ignore=shutil.ignore_patterns("data", "__pycache__", ".env", "animation_params.json",
                                                            "*.db", "*.db-wal", "*.db-shm"))
    (root / "data").mkdir()
    shutil.copy2(PI5 / "data/animation_registry.json", root / "data/animation_registry.json")
    port = free_port()
    config_before = (root / "config.py").read_bytes()
    env = dict(ENV, DDM_FLASK_HOST="127.0.0.1", DDM_FLASK_PORT=str(port))
    env.pop("INVOCATION_ID", None)
    out_path, err_path = root.parent / "out.txt", root.parent / "err.txt"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen([sys.executable, "main.py"], cwd=root, env=env, stdout=out, stderr=err)
    try:
        up = False
        for _ in range(240):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/quiniela", timeout=2).read(10)
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        _check("main.py with no .env starts and serves", up, err_path.read_text(encoding="utf-8", errors="replace")[-600:])
        time.sleep(1.0)
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
        _check("one line: no LED controller, the LEDs are off", stdout.count("no LED controller set (DDM_ESP32_IP") == 1, stdout[-800:])
        _check("one line: no weather key, the board's weather line is off",
               stdout.count("no weather key (DDM_WEATHER_API_KEY in pi5/.env): the board's weather line is off") == 1, stdout[-800:])
        _check("one line: no host PIN, La Subasta's admin page and routes are off",
               stdout.count("no host PIN (DDM_LS_ADMIN_PIN in pi5/.env): its admin page and routes are off") == 1, stdout[-800:])
        _check("the bridge idles: LQ_SERIAL_PORT is empty", "La Quiniela bridge idle: LQ_SERIAL_PORT is empty" in stdout + stderr,
               (stdout + stderr)[-800:])
        _check("no LED controller is tried: no unreachable line, no probe", "LED wall unreachable" not in stdout)
        _check("no tote board", "Tote Board:    none (DDM_TOTE_IP in pi5/.env)" in stdout)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/esp32/config", method="POST",
                                     data=json.dumps({"ip": "127.0.0.9"}).encode(), headers={"Content-Type": "application/json"})
        answer = json.loads(urllib.request.urlopen(req, timeout=30).read())
        env_text = (root / ".env").read_text(encoding="utf-8") if (root / ".env").exists() else ""
        _check("the device panel's address: persisted, into pi5/.env as DDM_ESP32_IP",
               answer.get("persisted") is True and "DDM_ESP32_IP=127.0.0.9" in env_text.splitlines(), str(answer) + env_text)
        _check("and pi5/config.py is untouched", (root / "config.py").read_bytes() == config_before)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/esp32/config", method="POST",
                                     data=json.dumps({"ip": "127.0.0.10"}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        lines = (root / ".env").read_text(encoding="utf-8").splitlines()
        _check("a second address replaces the line, one DDM_ESP32_IP line left",
               [ln for ln in lines if ln.startswith("DDM_ESP32_IP=")] == ["DDM_ESP32_IP=127.0.0.10"], str(lines))
    finally:
        proc.kill()
        proc.wait(10)


# -----------------------------------------------------------------------------
# pi5/tools/env_from_config.py
# -----------------------------------------------------------------------------

OLD_CONFIG = '''# config.py - the old tracked file, a box's values in it
ESP32_IP = "10.0.0.44"  # DDM ESP32 controller
ESP32_PORT = 5005
SOCKET_TIMEOUT = 5.0
TOTE_IP = "10.0.0.124"
TOTE_PORT = 80
TOTE_ENABLED = True
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5000
import os
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
WEATHER_API_KEY = "{key}"
WEATHER_LOCATION = "Dallas,TX"
def _lq_env(name, default):
    return os.environ.get('DDM_' + name, default)
LQ_SERIAL_PORT = _lq_env('LQ_SERIAL_PORT', '')
LQ_SERIAL_BAUD = _lq_env('LQ_SERIAL_BAUD', 115200)
TOKEN_VALUE = 1.00
QUINIELA_BOARD_STATES = [1, 2, 3, 4, 5]
'''
OLD_SPLASH = 'FLASK_PORT = 5001\nPI5_URL = "http://joeydevpi.local:5000"\nQUINIELA_LOOK = "dots"\n'
BY_ID = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=dict(ENV, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                                     GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def devpi_like_repo(committed_key):
    """A scratch git repo whose committed pi5/config.py is the old format (with a
    key in it), then a DevPi-style local edit on top: the LED address, the gateway's port."""
    repo = scratch("ddm_env_repo_")
    (repo / "pi5").mkdir()
    (repo / "splash_display").mkdir()
    shutil.copy2(PI5 / ".env.example", repo / "pi5/.env.example")
    shutil.copy2(REPO / "splash_display/.env.example", repo / "splash_display/.env.example")
    (repo / "pi5/config.py").write_text(OLD_CONFIG.format(key=committed_key), encoding="utf-8")
    (repo / "splash_display/config.py").write_text(OLD_SPLASH, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "old")
    text = (repo / "pi5/config.py").read_text(encoding="utf-8")
    text = text.replace('ESP32_IP = "10.0.0.44"', 'ESP32_IP = "10.0.0.42"')
    text = text.replace("_lq_env('LQ_SERIAL_PORT', '')", f"_lq_env('LQ_SERIAL_PORT', '{BY_ID}')")
    (repo / "pi5/config.py").write_text(text, encoding="utf-8")
    (repo / "splash_display/config.py").write_text(OLD_SPLASH.replace('"dots"', '"impact"'), encoding="utf-8")
    return repo


def tool(repo, *args):
    run = subprocess.run([sys.executable, str(TOOL), "--repo", str(repo), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=ENV, timeout=120)
    return run.returncode, run.stdout + run.stderr


def test_env_from_config():
    key = fake_secret(31)
    repo = devpi_like_repo(key)
    code, out = tool(repo)
    print("\n".join("    | " + ln for ln in out.splitlines()[:14]))
    _check("dry run: exit 0, writes nothing", code == 0 and not (repo / "pi5/.env").exists() and not (repo / "splash_display/.env").exists(), out)
    for line in ("DDM_ESP32_IP=10.0.0.42", f"DDM_LQ_SERIAL_PORT={BY_ID}", "DDM_TOTE_IP=10.0.0.124", "DDM_QUINIELA_LOOK=impact"):
        _check(f"dry run: says it would write {line.split('=')[0]}", line in out, out)
    _check("dry run: the settings equal to the defaults need no line (FLASK_PORT, TOTE_PORT, LQ_SERIAL_BAUD, TOKEN_VALUE...)",
           not re.search(r"DDM_(FLASK_PORT|FLASK_HOST|TOTE_PORT|TOTE_ENABLED|LQ_SERIAL_BAUD|TOKEN_VALUE|ESP32_PORT|"
                         r"WEATHER_LOCATION|QUINIELA_BOARD_STATES)=", out), out)
    _check("dry run: the splash's old PI5_URL needs no line", "DDM_PI5_URL=" not in out, out)
    _check("a key committed before is not copied, and says so", "WEATHER_API_KEY: not copied (it was committed" in out, out)
    _check("the key's value is never printed", key not in out and key[:12] not in out)
    _check("it says how to drop the local edits",
           "git checkout -- pi5/config.py splash_display/config.py" in out, out)

    (repo / "pi5/.env").write_text("DDM_ESP32_IP=1.2.3.4\n# mine\nDDM_LQ_SERIAL_PORT=\n", encoding="utf-8")
    code, out = tool(repo, "--write")
    env_text = (repo / "pi5/.env").read_text(encoding="utf-8")
    _check("--write: a value already set is left as it is (DDM_ESP32_IP=1.2.3.4)",
           "DDM_ESP32_IP=1.2.3.4" in env_text.splitlines() and "DDM_ESP32_IP=10.0.0.42" not in env_text, env_text)
    _check("--write: says it left it", "DDM_ESP32_IP: already set in .env, left as it is" in out, out)
    _check("--write: an empty line is filled in place", env_text.splitlines()[2] == f"DDM_LQ_SERIAL_PORT={BY_ID}", env_text)
    _check("--write: a missing one is added at the end, under a dated comment",
           env_text.splitlines()[-1] == "DDM_TOTE_IP=10.0.0.124" and "# moved from config.py" in env_text, env_text)
    _check("--write: the other lines kept (# mine)", "# mine" in env_text.splitlines())
    splash_env = (repo / "splash_display/.env").read_text(encoding="utf-8")
    _check("--write: the splash's look into splash_display/.env, started from its example",
           "DDM_QUINIELA_LOOK=impact" in splash_env.splitlines() and "splash_display/.env.example" in splash_env, splash_env)
    before = (repo / "pi5/.env").read_bytes(), (repo / "splash_display/.env").read_bytes()
    code, out = tool(repo, "--write")
    _check("a second run changes nothing (both files byte for byte)",
           ((repo / "pi5/.env").read_bytes(), (repo / "splash_display/.env").read_bytes()) == before, out)
    _check("a second run says there is nothing to move", "Nothing to move" in out, out)

    # A key the box had that was never committed: copied, never shown.
    repo = devpi_like_repo(key)
    local_key = fake_secret(29)[::-1]
    text = (repo / "pi5/config.py").read_text(encoding="utf-8").replace(key, local_key)
    (repo / "pi5/config.py").write_text(text, encoding="utf-8")
    code, out = tool(repo, "--write")
    env_values = dict(ln.split("=", 1) for ln in (repo / "pi5/.env").read_text(encoding="utf-8").splitlines()
                      if "=" in ln and not ln.startswith("#"))
    _check("a key never committed: copied into pi5/.env", env_values.get("DDM_WEATHER_API_KEY") == local_key)
    _check("and shown only as its length", f"DDM_WEATHER_API_KEY=<secret, {len(local_key)} characters>" in out
           and local_key not in out, out)
    _check("--write on a new .env starts from pi5/.env.example", "# pi5/.env.example" in (repo / "pi5/.env").read_text(encoding="utf-8"))

    # A config.py left with conflict markers by `git stash pop`: read on the stash's side.
    repo = devpi_like_repo(key)
    new_side = "ESP32_IP = _env('ESP32_IP', '')\n"
    conflicted = (repo / "pi5/config.py").read_text(encoding="utf-8").replace(
        'ESP32_IP = "10.0.0.42"  # DDM ESP32 controller\n',
        "<<<<<<< Updated upstream\n" + new_side + "=======\n" + 'ESP32_IP = "10.0.0.42"\n' + ">>>>>>> Stashed changes\n")
    (repo / "pi5/config.py").write_text(conflicted, encoding="utf-8")
    code, out = tool(repo)
    _check("a conflicted config.py is read on the stash's side (DDM_ESP32_IP=10.0.0.42)", "DDM_ESP32_IP=10.0.0.42" in out, out)

    # --from: the old file kept aside, the tree already clean.
    aside = scratch("ddm_aside_") / "config.py.devpi"
    aside.write_text((repo / "pi5/config.py").read_text(encoding="utf-8"), encoding="utf-8")
    git(repo, "checkout", "--", "pi5/config.py", "splash_display/config.py")
    code, out = tool(repo, "--from", str(aside))
    _check("--from FILE reads the copy kept aside", "DDM_ESP32_IP=10.0.0.42" in out and "Reading " + str(aside) in out, out)
    _check("with the tree clean, no checkout to suggest", "git checkout" not in out, out)

    # --stash: the DevPi steps. `git stash`, the pull (here: the new files committed), then
    # the old values read from the stash, never popped.
    repo = devpi_like_repo(key)
    git(repo, "stash")
    shutil.copy2(PI5 / "config.py", repo / "pi5/config.py")
    shutil.copy2(REPO / "splash_display/config.py", repo / "splash_display/config.py")
    git(repo, "commit", "-q", "-am", "the pull")
    code, out = tool(repo, "--stash")
    _check("--stash: reads the stash", "Reading the stash, stash@{0}" in out, out)
    _check("--stash: the box's values from it (DDM_ESP32_IP, DDM_LQ_SERIAL_PORT, the splash's look)",
           "DDM_ESP32_IP=10.0.0.42" in out and f"DDM_LQ_SERIAL_PORT={BY_ID}" in out and "DDM_QUINIELA_LOOK=impact" in out, out)
    _check("--stash: the tree is clean, no checkout to suggest", "git checkout" not in out, out)
    code, out = tool(repo, "--stash", "--write")
    _check("--stash --write: written, and says to drop the stash once .env is right",
           "DDM_ESP32_IP=10.0.0.42" in (repo / "pi5/.env").read_text(encoding="utf-8").splitlines()
           and "git stash drop" in out, out)
    _check("--stash --write: the stash is left for Joey to drop", "stash@{0}" in git(repo, "stash", "list").stdout)
    git(repo, "stash", "drop")
    (repo / "notes.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "notes")
    (repo / "notes.txt").write_text("y", encoding="utf-8")
    git(repo, "stash")
    code, out = tool(repo, "--stash")
    _check("--stash with a newest stash that doesn't touch pi5/config.py: reads nothing, says so",
           code == 2 and "holds no change to pi5/config.py" in out, out)

    # The tool's defaults are pi5's: config.py's, the bridge's and the betting board's.
    sys.path.insert(0, str(HERE))
    try:
        import importlib
        efc = importlib.import_module("env_from_config")
    finally:
        sys.path.remove(str(HERE))
    v, _ = config_values()
    mismatched = [k for k, d in efc.PI5_SETTINGS.items() if k in v and d is not None and v[k] != d]
    _check("its table of defaults matches pi5/config.py's", not mismatched, str(mismatched))
    sys.path.insert(0, str(PI5))
    try:
        from la_quiniela.bridge import DEFAULTS as BRIDGE
        from la_quiniela.betting import DEFAULTS as BOARD
    finally:
        sys.path.remove(str(PI5))
    wrong = [k for k, d in list(BRIDGE.items()) + list(BOARD.items()) if k in efc.PI5_SETTINGS and efc.PI5_SETTINGS[k] != d]
    _check("and the bridge's and the betting board's", not wrong, str(wrong))


# -----------------------------------------------------------------------------
# No secret in a tracked file
# -----------------------------------------------------------------------------

WORDS = re.compile(r"(?i)(key|token|secret|passw(?:or)?d|pwd)")
RUN = re.compile(r"(?<![A-Za-z0-9+/=_\-])(?:[0-9a-fA-F]{20,}|[A-Za-z0-9+/_\-]{24,}={0,2})(?![A-Za-z0-9+/=_\-])")
NAMED = re.compile(r"""(?i)(?P<name>[A-Z0-9_]*(?:API_?KEY|SECRET|PASSW(?:OR)?D|PASSWD|PWD|SSID|AUTH_?TOKEN|ACCESS_?TOKEN|BEARER)[A-Z0-9_]*)"""
                   r"""["']?\s*(?:=|:|\s)\s*(?P<q>["'])(?P<val>[^"'\n]*)(?P=q)""")
PLACEHOLDER = re.compile(r"(?i)^$|your|here|xxx|changeme|example|placeholder|<|\{|\$|fake|dummy|test")
# Fixtures that need such a line, file -> why. Each entry is a deliberate exception.
ALLOW = {
}


def is_secret_run(run):
    """A long run that could be a key, as against what this repo is full of: paths
    and routes (pi5/data/quiniela_YYYY-MM-DD), snake_case names (test functions,
    constants) and words joined by dashes. A key mixes upper and lower case with
    digits inside one piece, or is a long hex string."""
    if re.fullmatch(r"[0-9a-fA-F]{20,}", run):
        return True
    pieces = re.split(r"[/_\-.]", run)
    return any(re.search(r"[a-z]", p) and re.search(r"[A-Z]", p) and re.search(r"[0-9]", p) for p in pieces)


def scan_text(text):
    """(line number, what) for every secret-looking thing in text; values masked."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in NAMED.finditer(line):
            if not PLACEHOLDER.search(m.group("val")):
                hits.append((i, f"{m.group('name')} = <{len(m.group('val'))} characters>"))
        if WORDS.search(line):
            for m in RUN.finditer(line):
                if is_secret_run(m.group(0)):
                    hits.append((i, f"a {len(m.group(0))}-character run beside '{WORDS.search(line).group(0)}'"))
    return hits


def tracked_files():
    run = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True)
    return [p for p in run.stdout.decode("utf-8", "replace").split("\0") if p]


def test_no_secret_in_a_tracked_file():
    samples = {
        "a weather key in Python": f'WEATHER_API_KEY = "{fake_secret(31)}"\n',
        "a Wi-Fi password in a sketch": '#define WIFI_PASSWORD "' + "hun" + "ter22" + '"\n',
        "a hex token beside its name": f"auth token: {fake_secret(36)}\n",
        "a mixed-case key beside its name": "api key " + "aB3dE9" + "fG2hJ7" + "kL5mN8" + "pQ4rS6" + "tU1vW" + "\n",
    }
    for name, text in samples.items():
        _check(f"the scan catches {name}", bool(scan_text(text)))
    _check("the scan leaves placeholders alone",
           not scan_text('WEATHER_API_KEY = ""\n#define WIFI_SSID "YOUR_WIFI_SSID"\nDDM_WEATHER_API_KEY=\n'))
    files = tracked_files()
    _check("git ls-files works here", len(files) > 100, str(len(files)))
    found = []
    for rel in files:
        path = REPO / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if rel.endswith((".pyc", ".pyo")):
            found.append(f"{rel}: a compiled Python file is tracked (it carries config.py's values)")
            continue
        if b"\0" in data[:4096]:
            continue
        for line_no, what in scan_text(data.decode("utf-8", "replace")):
            if rel not in ALLOW:
                found.append(f"{rel}:{line_no}: {what}")
    _check("no tracked file holds a secret (and no compiled .pyc is tracked)", not found, "\n      ".join([""] + found[:20]))


# -----------------------------------------------------------------------------

def main():
    print(f"DDM config smoke test\n  repo: {REPO}")
    _run("pi5/config.py with no .env: safe defaults", test_defaults_with_no_env)
    _run("pi5/config.py with a .env: each setting lands by name", test_env_file_lands_by_name)
    _run("splash_display/config.py and its .env", test_splash_env)
    _run("the LED client with no address", test_led_client_off_without_an_address)
    _run("main.py with no .env", test_main_starts_with_no_env)
    _run("pi5/tools/env_from_config.py", test_env_from_config)
    _run("no secret in a tracked file", test_no_secret_in_a_tracked_file)
    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}\nRESULTS: {passed} passed, {failed} failed, {len(_results)} total\n{'=' * 50}")
    for d in _scratch:
        shutil.rmtree(d, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

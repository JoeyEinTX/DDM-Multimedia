# la_quiniela/test_dashboard.py - the dashboard's side of La Quiniela
#
# Run with: python -m la_quiniela.test_dashboard  (from the pi5/ dir)
#
# The real app (pi5/main.py: every route, the real templates and static
# files) over a fresh bridge on test_smoke's fake serial port and temp
# database. No hardware and no network: the LED wall's client has its
# exchange stubbed (every command is recorded and answered OK, on its worker's
# thread), the tote board is off, and the results file lives in a temp directory. Same tiny runner as
# test_smoke.py.
#
# What is checked here is what the dashboard and La Quiniela share: the
# menu, the horses' names on the results tote and in the SET WINNERS
# pickers, the race roster /api/race builds from La Quiniela (Race Setup is
# gone), the page's versioned CSS and JS, and the one race state (the
# dashboard's modes set La Quiniela's state; the results make it 5, RESET
# makes it 6), and the SET WINNERS picker: a tap fills the slot the host meant,
# however slowly the LED controller answers, and a horse whose cup held no bets
# at the post is marked NO BETS from La Quiniela's figures at the post (the
# source, and a headless Chrome in pi5/tools/picker_check.py). And the iPad on
# race night, held in landscape: everything the host presses on the dashboard
# and the LQ admin page is 44 px or more each way, both pages carry the Home
# Screen metas, and full screen the links between them open in place.

import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass

_PI5_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PI5_DIR)

from la_quiniela import test_smoke as S  # noqa: E402  (repoints the DB before the bridge loads)
from la_quiniela import protocol as P  # noqa: E402
from la_quiniela.blueprint import get_bridge, init_la_quiniela  # noqa: E402
from la_quiniela.board import get_board, init_board  # noqa: E402
from la_subasta import blueprint as ls_blueprint  # noqa: E402
from la_quiniela.test_smoke import FakeClock, MAC_A, MAC_B, _fresh_bridge, telem  # noqa: E402
from communication.esp32_client import ESP32Client  # noqa: E402
from communication.sender import Unreachable  # noqa: E402

_real_stdout = sys.stdout
sys.stdout = io.StringIO()          # main.py prints its banner lines at import
try:
    import main  # noqa: E402
finally:
    sys.stdout = _real_stdout

# main.py made a bridge of its own at import, on the same temp database. It
# is not the one under test, and while its connection is open Windows will
# not let a test replace the file: close it.
try:
    get_bridge().close()
except Exception:
    pass

DERBY = ["Dornoch", "Sierra Leone", "Mystik Dan", "Catching Freedom", "Catalytic", "Just Steel",
         "Honor Marie", "Just a Touch", "Encino", "T O Password", "Forever Young", "Track Phantom",
         "West Saratoga", "Endlessly", "Domestic Product", "Grand Mo the First", "Fierceness",
         "Stronghold", "Resilience", "Society Man", "Mugatu", "Ocelli", "Epic Ride", "Society Girl"]
NAMES_TEXT = "\n".join(f"{n}. {name}" for n, name in enumerate(DERBY, 1))

_results = []
_tmpdirs = []


def _check(name, condition, detail=""):
    status_ = "PASS" if condition else "FAIL"
    _results.append((status_, name, detail))
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


def wait_for(pred, timeout=3.0, every=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(every)
    return bool(pred())


def tmpdir():
    d = tempfile.mkdtemp(prefix="lq_dash_")
    _tmpdirs.append(d)
    return Path(d)


RIG_PIN = "rig-host-pin"            # the host's PIN for La Subasta's admin routes; every rig's client is the host's iPad


class Rig:
    """main's app over a fresh bridge and board; the hardware stubbed."""

    def __init__(self, led_ok=True, real_time=False):
        # A database of its own per rig. main.py's import left connections
        # open on test_smoke's temp file (La Subasta shares it), and Windows
        # does not let an open file be replaced, so that one cannot be made
        # fresh again; a new path always is.
        self.dir = tmpdir()
        S._TMP_DB = str(self.dir / "la_quiniela_dash.db")
        # real_time: the bridge and the board on the real clocks (a test that runs the board's own thread).
        self.bridge, self.port, self.sio, self.clk = _fresh_bridge(clock=not real_time)
        self.bridge._open_port()
        init_la_quiniela(socketio=None, bridge=self.bridge)
        self.results_file = self.dir / "results.json"
        main.RESULTS_FILE = str(self.results_file)
        self.wall = None if real_time else FakeClock(1_700_000_000.0)
        self.board = init_board(bridge=self.bridge, log_dir=self.dir / "logs", results_path=self.results_file,
                                **({} if real_time else {"wall": self.wall}))
        # A LED client of its own (nothing of another rig's wall, what it missed or whether it was up, carries
        # over), its exchange stubbed: the worker records every command but its own STATUS probes in self.led.
        self.led = []
        self.led_ok = led_ok
        main.esp32 = ESP32Client("10.0.0.42", 5005, timeout=0.3)
        main.racing_service.esp32_client = main.esp32

        def exchange(command):
            if command != "STATUS":
                self.led.append(command)
            if not self.led_ok:
                raise Unreachable("TIMEOUT", "timed out")
            return "OK:" + command
        main.esp32.exchange = exchange
        main.tote = None                # tote_send() does nothing
        main.app.config["TESTING"] = True
        main.app.config["LS_ADMIN_PIN"] = RIG_PIN
        self.client = main.app.test_client()
        self.client.set_cookie(ls_blueprint.ADMIN_COOKIE, ls_blueprint.pin_cookie(RIG_PIN), path="/la-subasta")

    def post(self, path, body=None, **kw):
        return self.client.post(path, json=body, **kw) if body is not None else self.client.post(path, **kw)

    def real_wall(self, port, timeout):
        """The real LED client against a socket on this port (a DeadWall's)."""
        main.esp32 = ESP32Client("127.0.0.1", port, timeout=timeout)
        main.racing_service.esp32_client = main.esp32

    def lines(self):
        return self.port.lines()

    def model(self):
        return self.client.get("/api/quiniela").get_json()


def state_line(rev, phase, scratched=(), renum=(), results=(0, 0, 0)):
    return P.build_state_line(rev, phase, scratched, renum, results)


# -----------------------------------------------------------------------------
# Commit 1: the menu and the names
# -----------------------------------------------------------------------------

def test_menu_and_page():
    rig = Rig()
    r = rig.client.get("/", base_url="http://joeydevpi.local:5000")
    html = r.get_data(as_text=True)
    _check("GET / 200", r.status_code == 200)
    _check("the menu links to La Quiniela's admin page", 'href="/quiniela/admin"' in html and "La Quiniela Admin" in html)
    _check("the menu links to the board on the splash's port, on the host the dashboard was opened with",
           'href="http://joeydevpi.local:5001/"' in html and "La Quiniela Board" in html, html[html.find("drawer-nav"):][:600])
    r = rig.client.get("/", base_url="http://10.0.0.37:5000")
    _check("...whatever that host is", 'href="http://10.0.0.37:5001/"' in r.get_data(as_text=True))
    saved = main.SPLASH_BOARD_URL
    try:
        main.SPLASH_BOARD_URL = "http://splashpi.local:5001/?look=dots"
        r = rig.client.get("/", base_url="http://joeydevpi.local:5000")
        _check("SPLASH_BOARD_URL without {host} is used as it is",
               'href="http://splashpi.local:5001/?look=dots"' in r.get_data(as_text=True))
    finally:
        main.SPLASH_BOARD_URL = saved
    _check("splash_board_url drops the port and brackets an IPv6 host",
           main.splash_board_url("localhost:5000") == "http://localhost:5001/"
           and main.splash_board_url("[::1]:5000") == "http://[::1]:5001/"
           and main.splash_board_url("") == "http://localhost:5001/")
    _check("Race Setup is gone from the menu and the page",
           "Race Setup" not in html and "race-setup-modal" not in html and "openRaceSetupModal" not in html)
    _check("the other three menu items are still there",
           all(s in html for s in ("openAnimLibModal()", "openTuningModal()", "openAnimationsModal()")))
    _check("the pickers' grid and the named modal", 'id="saddle-cloth-grid"' in html and "winner-pick-grid" in html
           and "results-modal-named" in html)
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    _check("the dashboard's JS reads the field from La Quiniela", "/api/quiniela/field" in js and "horseDisplayName" in js)
    _check("the 5x7 table carries what the TV board prints besides names (its face is built from this table)",
           all(("'%s': [0x" % ch) in js for ch in "$+#%()?*=;<>@_") and "'\"': [0x" in js
           and all(("'\\u%s': [0x" % code) in js for code in ("00B7", "25C6", "25B6", "00B0")))
    _check("...and has nothing of Race Setup left",
           not any(s in js for s in ("raceSetup", "race-setup", "toggleOddsPolling", "initPostTimeCountdown")))
    _check("the tote prints the name, HORSE n without one, never cut short",
           "createToteName(horseDisplayName(horse))" in js and "`HORSE ${n}`" in js and "results-name-fit" in js)
    _check("a pick carries the post (the LED cup) and the horse (the results)",
           "selectCup(post, horse)" in js and "cup: post" in js and "win: winHorse" in js)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    _check("the new CSS is there", all(s in css for s in (".winner-pick-btn", ".drawer-link", ".results-name-fit", ".slot-horse-name")))
    _check("...and nothing of Race Setup's modal or its odds toggle", "race-setup" not in css and "odds-polling" not in css)
    rules = {rule.rule for rule in main.app.url_map.iter_rules()}
    _check("the AI search route is gone", "/api/race-setup/ai-search" not in rules
           and rig.post("/api/race-setup/ai-search", {}).status_code == 404)
    _check("Race Setup is gone: its store's routes and its three odds routes are no routes",
           not any(r.startswith("/api/race-setup") for r in rules)
           and rig.client.get("/api/race-setup").status_code == 404 and rig.post("/api/race-setup", {"post_time": "18:57"}).status_code == 404
           and rig.post("/api/race-setup/start-odds-polling").status_code == 404, str(sorted(r for r in rules if "race" in r)))
    _check("race info and the odds are La Quiniela's routes now; /api/race stays",
           {"/api/quiniela/race", "/api/quiniela/odds", "/api/quiniela/odds/start", "/api/quiniela/odds/stop", "/api/race"} <= rules)
    _check("main.py keeps nothing of the old store", not any(hasattr(main, f) for f in ("load_race_setup", "save_race_setup", "poll_odds")))
    _check("/api/quiniela/field is registered", "/api/quiniela/field" in rules)
    # A pull and a restart must serve the new CSS and JS: every link carries pi5's page revision (page_rev.py).
    import re
    import page_rev
    for path in ("css/ddm_style.css", "js/ddm_control.js"):
        _check(f"the page asks for /static/{path}?v=<pi5's page revision>", f'/static/{path}?v={page_rev.PAGE_REV}"' in html,
               str(re.findall(r'/static/[^"]*' + re.escape(path.split("/")[-1]) + r'[^"]*', html)))
        _check(f"...and the versioned URL serves it", rig.client.get(f"/static/{path}?v={page_rev.PAGE_REV}").status_code == 200)


def test_race_roster_is_la_quinielas():
    """/api/race, in the shape it always had, built from La Quiniela: its
    field and names, its race info, the odds by program number, its race
    state and results."""
    rig = Rig()
    r = rig.client.get("/api/race")
    body = r.get_json()
    _check("no names in La Quiniela: no horses, state unknown, no post time, CORS header kept",
           r.status_code == 200 and body["horses"] == [] and body["race_state"] == "unknown"
           and body["post_time"] == "" and body["post_time_iso"] == "" and body["winner"] is None
           and r.headers.get("Access-Control-Allow-Origin") == "*", str(body))
    _check("the same keys as ever", set(body) == {"race_state", "post_time", "post_time_iso", "last_updated", "horses", "winner"})
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.client.put("/api/quiniela/odds", json={"odds": {"1": "5-2", "9": "8-1", "20": "30-1", "22": "12-1"}})
    rig.client.put("/api/quiniela/race", json={"date": "2027-05-01", "time": "17:57"})
    body = rig.client.get("/api/race").get_json()
    _check("20 horses, La Quiniela's names as typed, the odds by program number",
           [h["number"] for h in body["horses"]] == list(range(1, 21))
           and body["horses"][0] == {"number": 1, "name": "Dornoch", "odds": "5-2", "finish": None}
           and body["horses"][1]["odds"] is None and body["horses"][8] == {"number": 9, "name": "Encino", "odds": "8-1", "finish": None},
           str(body["horses"][:2]))
    _check("the post time from La Quiniela's race info, on the race's clock",
           body["post_time"] == "5:57 PM CDT" and body["post_time_iso"] == "2027-05-01T17:57:00-05:00", str(body))
    _check("race state from La Quiniela's: PRE_RACE is pre-race", body["race_state"] == "pre-race")
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.post("/api/quiniela/scratch", {"horse": 20})
    body = rig.client.get("/api/race").get_json()
    numbers = [h["number"] for h in body["horses"]]
    _check("9 replaced by 22 and 20 scratched: the field in numeric order, 22 last, neither 9 nor 20",
           numbers == [1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 22], str(numbers))
    _check("22 under its own number and name, with 22's odds", body["horses"][-1] == {"number": 22, "name": "Ocelli", "odds": "12-1",
                                                                                      "finish": None}, str(body["horses"][-1]))
    rig.client.put("/api/quiniela/horses", json={"3": {"name": ""}})
    body = rig.client.get("/api/race").get_json()
    _check("a horse with no name is not listed", 3 not in [h["number"] for h in body["horses"]])
    for mode, word in (("BETTING_60", "pre-race"), ("AT_THE_GATE", "pre-race"), ("GATES_BURST", "running"),
                       ("HEARTBEAT_COOLDOWN", "post-race")):
        rig.post("/api/quiniela/mode", {"mode": mode})
        _check(f"{mode}: {word}", rig.client.get("/api/race").get_json()["race_state"] == word)
    rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    body = rig.client.get("/api/race").get_json()
    finish = {h["number"]: h["finish"] for h in body["horses"] if h["finish"]}
    _check("the results: post-race, the winner, finish 1 / 2 / 3", body["race_state"] == "post-race" and body["winner"] == 19
           and finish == {19: 1, 1: 2, 22: 3}, str(finish))
    rig.post("/api/results/clear")
    _check("RESET: post-race still (AFTER_PARTY), no winner", rig.client.get("/api/race").get_json()["winner"] is None)


def test_field_route_on_the_real_app():
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    r = rig.client.get("/api/quiniela/field")
    body = r.get_json()
    _check("GET /api/quiniela/field 200, no-store", r.status_code == 200 and r.headers.get("Cache-Control") == "no-store")
    _check("post 9 offers 22 OCELLI", body["posts"][8] == {"post": 9, "horse": 22, "name": "OCELLI",
                                                         "label": "22 · OCELLI", "replaces": 9}, str(body["posts"][8]))
    _check("the label of a named horse is '19 · RESILIENCE'", body["posts"][18]["label"] == "19 · RESILIENCE")


# -----------------------------------------------------------------------------
# Commit 2: one race state
# -----------------------------------------------------------------------------

BUTTONS = [   # (the button's text, its mode, what it calls)
    ("Welcome", "WELCOME", "sendAnimation('WELCOME', this)"),
    ("Test", "TEST", "openTestModal()"),
    ("Standby", "STANDBY", "sendStandby()"),
    ("60 Min", "BETTING_60", "sendAnimation('BETTING_60', this)"),
    ("30 Min", "BETTING_30", "sendAnimation('BETTING_30', this)"),
    ("Final Call", "FINAL_CALL", "sendAnimation('FINAL_CALL', this)"),
    ("AT THE GATE", "AT_THE_GATE", "sendAnimation('AT_THE_GATE', this)"),
    ("THEY'RE OFF!", "GATES_BURST", "sendAnimation('GATES_BURST', this)"),
    ("Chaos", "CHAOS", "sendAnimation('CHAOS', this)"),
    ("Finish", "FINISH", "sendAnimation('FINISH', this)"),
    ("Set Winners", "RESULTS", "showResultsModal()"),
    ("Heartbeat", "HEARTBEAT_COOLDOWN", "sendAnimation('HEARTBEAT_COOLDOWN', this)"),
    ("Reset", "RESET", "sendReset()"),
]


def test_buttons_carry_their_mode():
    from la_quiniela.betting import MODE_STATES
    rig = Rig()
    html = rig.client.get("/").get_data(as_text=True)
    panels = html[html.index('<div class="panels">'):html.index('<!-- Spectator Panel')]
    import re
    found = re.findall(r'<button class="btn"[^>]*?data-mode="([A-Z_0-9]+)"[^>]*?onclick="([^"]+)"[^>]*>([^<]+)</button>', panels)
    _check("thirteen buttons on the four panels, each with a mode", len(found) == 13 and panels.count("<button") == 13, str(len(found)))
    _check("every button's mode and call are the expected ones",
           [(text, mode, call) for mode, call, text in found] == BUTTONS, str(found))
    _check("every button's mode but TEST is in pi5's table, and the table has no mode without a button",
           {mode for mode, _, _ in found} - {"TEST"} == set(MODE_STATES) and "TEST" not in MODE_STATES,
           str(set(MODE_STATES) ^ {m for m, _, _ in found}))
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    _check("a mode button names its mode to pi5 before the LEDs answer",
           "const raceSet = mode ? setRaceMode(mode, asked) : null;" in js and "'/api/quiniela/mode'" in js
           and js.index("const raceSet = mode ? setRaceMode(mode, asked) : null;") < js.index("showNotification(withRaceState(`Animation: ${animName}`, race), 'success');"))
    _check("Standby names its mode; TEST names none (an LED test only, Joey 2026-10-13)",
           "const raceSet = setRaceMode('STANDBY', asked);" in js and "setRaceMode('TEST')" not in js
           and "setRaceMode" not in _js_function(js, "function openTestModal()")
           and "/api/quiniela/mode" not in _js_function(js, "function openTestModal()"))
    _check("pi5 refuses TEST as a mode: 400, the race state where it was",
           rig.post("/api/quiniela/mode", {"mode": "TEST"}).status_code == 400 and rig.bridge.phase == 0)
    _check("the table is pi5's, not repeated in the page", "MODE_STATES" not in js.replace("la_quiniela/betting.py MODE_STATES", "")
           and "BETTING_60: 1" not in js)
    _check("the race state is read back every 5 s, and fresh as a tap that could reopen betting is made",
           "setInterval(pollRaceMode, 5000);" in js and "const info = await freshRaceMode();" in _js_function(js, "async function reopenAllowed(mode)"))
    _check("the host's yes is what carries `reopen`: only Reopen betting sends it, a Cancel sends nothing, and the page asks whenever "
           "pi5 holds the figures (at_post), not by state",
           "? { reopen: true } : null" in _js_function(js, "async function reopenAllowed(mode)")
           and "info.at_post" in _js_function(js, "async function reopenAllowed(mode)")
           and "body: JSON.stringify({ mode: mode, ...confirmed })" in js
           and re.findall(r"reopen: true", re.sub(r"//[^\n]*", "", js)) == ["reopen: true"])


def test_dashboard_modes_move_la_quiniela():
    """What a dashboard button does, as the page does it: the LED route it
    always called, and the mode to pi5."""
    from la_quiniela.betting import MODE_STATES
    rig = Rig()
    rig.bridge.handle_raw_line(telem(MAC_A, horse=19, count=5))
    led_routes = {   # mode -> (the LED route the button calls, the command the LED controller must get)
        "WELCOME": ("/api/animation/WELCOME", "ANIM:WELCOME"),
        "BETTING_60": ("/api/animation/BETTING_60", "ANIM:BETTING_60"),
        "BETTING_30": ("/api/animation/BETTING_30", "ANIM:BETTING_30"),
        "FINAL_CALL": ("/api/animation/FINAL_CALL", "ANIM:FINAL_CALL"),
        "AT_THE_GATE": ("/api/animation/AT_THE_GATE", "ANIM:AT_THE_GATE"),
        "GATES_BURST": ("/api/animation/GATES_BURST", "ANIM:GATES_BURST"),
        "CHAOS": ("/api/animation/CHAOS", "ANIM:CHAOS"),
        "FINISH": ("/api/animation/FINISH", "ANIM:FINISH"),
        "HEARTBEAT_COOLDOWN": ("/api/animation/HEARTBEAT_COOLDOWN", "ANIM:HEARTBEAT_COOLDOWN"),
        "STANDBY": ("/api/led/all_off", "LED:ALL_OFF"),
    }
    for mode in ("BETTING_60", "WELCOME", "BETTING_30", "STANDBY", "FINAL_CALL", "AT_THE_GATE", "GATES_BURST",
                 "AT_THE_GATE", "CHAOS", "FINAL_CALL", "FINISH", "HEARTBEAT_COOLDOWN"):
        route, command = led_routes[mode]
        rig.led.clear()
        r_mode = rig.post("/api/quiniela/mode", {"mode": mode, "reopen": True})        # the walk goes back to 2 from 4
        r_led = rig.post(route)
        _check(f"{mode}: the LEDs get {command} as before, La Quiniela state {MODE_STATES[mode]}",
               r_led.status_code == 200 and r_led.get_json()["success"] is True and wait_for(lambda: rig.led == [command], 2.0)
               and r_mode.get_json()["state"] == MODE_STATES[mode] and rig.bridge.phase == MODE_STATES[mode]
               and rig.model()["race_state"] == MODE_STATES[mode], f"{rig.led} {r_mode.get_json()}")
    _check("the LED routes alone never move the race state (a preview, the Animations list)",
           rig.post("/api/animation/BETTING_60").status_code == 200 and rig.bridge.phase == 5
           and rig.post("/api/led/all_off").status_code == 200 and rig.bridge.phase == 5)
    # The LED controller unreachable: the race state moves all the same.
    dead = Rig(led_ok=False)
    r_mode = dead.post("/api/quiniela/mode", {"mode": "BETTING_60"})
    r_led = dead.post("/api/animation/BETTING_60")
    _check("LED wall unreachable: the LED route still answers at once (the command queued), the race state is set",
           r_led.get_json()["success"] is True and r_led.get_json()["queued"] == "ANIM:BETTING_60"
           and r_mode.get_json()["ok"] is True and dead.bridge.phase == 1
           and wait_for(lambda: main.esp32.health()["online"] is False, 2.0), str(r_led.get_json()))


def test_results_and_reset_are_modes():
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    rev = rig.bridge.state_rev
    rig.port.written.clear()
    rig.led.clear()
    # SET WINNERS opens its modal on RESULTS_ENTRY: the LEDs only, the state stays RUNNING
    rig.post("/api/animation/RESULTS_ENTRY")
    _check("opening SET WINNERS moves nothing", rig.bridge.phase == 4 and rig.port.written == []
           and wait_for(lambda: rig.led == ["ANIM:RESULTS_ENTRY"], 2.0))
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    body = r.get_json()
    _check("POST /api/results: success, the results as sent (and no next finisher)", r.status_code == 200 and body["success"] is True
           and body["results"] == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []}, str(body))
    _check("...the LED wall told after them (RESULTS:FINALIZE, which the page used to send), its health online in the reply",
           wait_for(lambda: rig.led == ["ANIM:RESULTS_ENTRY", "RESULTS:FINALIZE"], 2.0) and body["leds"] == "online",
           f"{rig.led} {body.get('leds')}")
    _check("...and the race state it set: WINNER, mode RESULTS, no error",
           body["race"] == {"rev": rev + 1, "state": 5, "state_name": "WINNER", "mode": "RESULTS", "source": "dashboard",
                            "gateway_online": False} and body["race_error"] is None, str(body.get("race")))
    _check("one state line: WINNER with the results, byte-exact (no renumber pair: no cup says 9)",
           rig.lines() == [state_line(rev + 1, 5, [], [], [19, 1, 22])], str(rig.lines()))
    _check("the file holds horse numbers", json.loads(rig.results_file.read_text())["show"] == 22)
    m = rig.model()
    _check("the model: state 5, the results", m["race_state"] == 5 and m["results"] == {"win": 19, "place": 1, "show": 22}, str(m["results"]))
    info = rig.client.get("/api/quiniela/mode").get_json()
    _check("GET mode: SET WINNERS", (info["state"], info["mode"], info["label"]) == (5, "RESULTS", "SET WINNERS"))
    r = rig.post("/api/results", {"win": 19, "place": 19, "show": 22})
    _check("results naming a horse twice: 400, nothing moved", r.status_code == 400 and rig.bridge.state_rev == rev + 1)
    # RESET
    rig.port.written.clear()
    r = rig.post("/api/results/clear")
    body = r.get_json()
    _check("POST /api/results/clear: success, the race ended: AFTER_PARTY, mode RESET",
           r.status_code == 200 and body["success"] is True and body["race"]["state"] == 6
           and body["race"]["state_name"] == "AFTER_PARTY" and body["race"]["mode"] == "RESET", str(body))
    _check("one state line: AFTER_PARTY, the results cleared",
           rig.lines() == [state_line(rev + 2, 6, [], [])], str(rig.lines()))
    _check("the file is gone, the model's results are null", not rig.results_file.exists()
           and rig.model()["results"] is None)
    _check("the board keeps the TV in WINNER and hands it back in AFTER_PARTY",
           rig.model()["board_states"] == [1, 2, 3, 4, 5] and rig.model()["race_state"] == 6)
    _check("the LEDs went off as they always did on a reset", wait_for(lambda: "LED:ALL_OFF" in rig.led, 2.0))
    # The next race: WELCOME is PRE_RACE again (the host's yes: the figures at the post are still held after RESET)
    rig.post("/api/quiniela/mode", {"mode": "WELCOME", "reopen": True})
    _check("WELCOME after the reset: PRE_RACE", rig.bridge.phase == 0)
    # Without a board the results are refused: test_results_refused_while_the_field_cannot_be_read


# -----------------------------------------------------------------------------
# The results are facts about the race, not about the LEDs
# -----------------------------------------------------------------------------

def test_results_stand_without_the_leds():
    """SET WINNERS with the LED controller down: the results are saved,
    La Quiniela goes to WINNER with them in one state line, and the reply
    says the wall is unreachable (found down by the FINISH animation before)."""
    rig = Rig(led_ok=False)
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    rig.post("/api/animation/FINISH")
    _check("the wall found down by the animation before", wait_for(lambda: main.esp32.health()["online"] is False, 2.0))
    rev = rig.bridge.state_rev
    rig.port.written.clear()
    rig.led.clear()
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    body = r.get_json()
    _check("LED wall down: 200, success, the results as sent, leds unreachable",
           r.status_code == 200 and body["success"] is True and body["results"] == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []}
           and body["leds"] == "unreachable", str(body))
    _check("...saved all the same, horse numbers in the file",
           json.loads(rig.results_file.read_text()) | {"timestamp": None}
           == {"win": 19, "place": 1, "show": 22, "next": [], "ties": [], "timestamp": None})
    _check("...WINNER with the results in one state line, byte-exact",
           body["race"]["state"] == 5 and rig.lines() == [state_line(rev + 1, 5, [], [], [19, 1, 22])], str(rig.lines()))
    _check("...the TV's model has them", rig.model()["results"] == {"win": 19, "place": 1, "show": 22}
           and rig.model()["race_state"] == 5)
    _check("...the LED wall was tried once, after the results were saved", wait_for(lambda: rig.led == ["RESULTS:FINALIZE"], 2.0), str(rig.led))
    r = rig.client.get("/api/results")
    _check("GET /api/results has them (the dashboard's banner after a reload)", r.get_json()["success"] is True
           and (r.get_json()["results"]["win"], r.get_json()["results"]["show"]) == (19, 22))
    # A file that cannot be written: nothing moves, and the reply says why
    saved = main.RESULTS_FILE
    main.RESULTS_FILE = str(rig.dir / "no such dir" / "results.json")
    try:
        rig.port.written.clear()
        rig.led.clear()
        rev = rig.bridge.state_rev
        r = rig.post("/api/results", {"win": 7, "place": 3, "show": 10})
        body = r.get_json()
        _check("results that cannot be saved: 500, success false, the reason, race null",
               r.status_code == 500 and body["success"] is False and body["error"].startswith("results not saved")
               and body["race"] is None, str(body))
        _check("...no state line, no LED command, the race state and the saved results as they were",
               rig.port.written == [] and rig.led == [] and rig.bridge.state_rev == rev
               and rig.model()["results"] == {"win": 19, "place": 1, "show": 22})
    finally:
        main.RESULTS_FILE = saved
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    _check("the page no longer says when the LEDs missed them (the pill does); the results line is the race state's",
           "data.leds" not in js and "LEDs unreachable" not in js)
    _check("the page no longer sends RESULTS:FINALIZE itself (the route does, once)",
           "fetch('/api/results/finalize'" not in js)
    _check("the page follows the server's results: Reset betting takes the tote down within 5 s",
           "async function followServerResults()" in js and "await followServerResults();" in js
           and "setRaceComplete(false);" in js[js.index("async function followServerResults()"):])


def test_results_are_kept_when_pi5_starts():
    """pi5 used to delete results.json when it started, so a restart in
    WINNER lost the results. Nothing that runs at start may remove it: not
    main.py's top level, not its __main__ block (the routes, which run only
    when asked, are another matter: /api/results/clear is the dashboard's
    RESET)."""
    import ast
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    removes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("remove", "unlink", "rmtree")):
                removes.append((sub.lineno, ast.unparse(sub)))
    _check("nothing at main.py's top level or in its __main__ block removes a file", removes == [], str(removes))
    rig = Rig()
    rig.results_file.write_text(json.dumps({"win": 19, "place": 1, "show": 22, "timestamp": "2027-05-01T19:02:00"}))
    r = rig.client.get("/api/results")
    _check("a results file from before a restart is served as it is",
           r.get_json()["success"] is True and r.get_json()["results"]["place"] == 1)


def test_results_need_all_three():
    """B9 of the gap review: POST /api/results is a 400 unless win, place and show are all there, three
    different horse numbers, each in the field as La Quiniela has it (the SET WINNERS pickers' own list: 22
    standing in for 9, not 9; an also-eligible only while it stands in). A missing key is never filled in.
    A 400 saves nothing, sends no state line, no LED command and no results event."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.post("/api/quiniela/scratch", {"horse": 20})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    events = []
    saved_broadcast = main.broadcast_sse
    main.broadcast_sse = lambda event, data: events.append((event, data))
    try:
        cases = [
            ("WIN missing", {"place": 1, "show": 22}, "win is missing"),
            ("PLACE missing (the review's {win: 19, show: 22})", {"win": 19, "show": 22}, "place is missing"),
            ("SHOW missing (the review's {win: 19, place: 1})", {"win": 19, "place": 1}, "show is missing"),
            ("all missing", {}, "win is missing"),
            ("a null", {"win": 19, "place": None, "show": 22}, "place is missing"),
            ("a horse twice", {"win": 19, "place": 19, "show": 22}, "three different horses"),
            ("9, replaced by 22", {"win": 9, "place": 1, "show": 22}, "horse 9 is not in the field"),
            ("20, scratched with no replacement", {"win": 19, "place": 20, "show": 22}, "horse 20 is not in the field"),
            ("21, an also-eligible standing in for nobody", {"win": 19, "place": 1, "show": 21}, "horse 21 is not in the field"),
            ("25", {"win": 25, "place": 1, "show": 22}, "horse 25 is not in the field"),
            ("0", {"win": 0, "place": 1, "show": 22}, "horse 0 is not in the field"),
            ("a string", {"win": "19", "place": 1, "show": 22}, "win must be a horse number"),
            ("a bool", {"win": True, "place": 1, "show": 22}, "win must be a horse number"),
        ]
        for name, body, words in cases:
            rig.port.written.clear()
            rig.led.clear()
            rev = rig.bridge.state_rev
            r = rig.post("/api/results", body)
            reply = r.get_json() or {}
            _check(f"/api/results {name}: 400 that says so, nothing saved, no line, no LEDs, no event",
                   r.status_code == 400 and reply.get("success") is False and words in reply.get("error", "")
                   and not rig.results_file.exists() and rig.port.written == [] and rig.led == [] and events == []
                   and rig.bridge.state_rev == rev and rig.bridge.phase == 4, f"{r.status_code} {reply}")
        r = rig.client.post("/api/results", data="not json", content_type="application/json")
        _check("/api/results with a body that is not JSON: 400, nothing saved",
               r.status_code == 400 and not rig.results_file.exists(), r.get_data(as_text=True)[:200])
        r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
        _check("all three, distinct, in the field (22 standing in for 9): 200, saved, WINNER, one results event",
               r.status_code == 200 and r.get_json()["success"] is True and rig.results_file.exists()
               and rig.bridge.phase == 5 and [e for e, _ in events] == ["results"], str(r.get_json()))
    finally:
        main.broadcast_sse = saved_broadcast


# -----------------------------------------------------------------------------
# SET WINNERS records the real finish (Joey, 2026-10-20, gap review M5; OD3 revised)
# -----------------------------------------------------------------------------
# The host enters the horses that crossed 1st, 2nd and 3rd, never a substitute. When one of
# them had no bets at the post La Quiniela will skip it (its payout computation, M6), so the
# record goes on: the dialog asks for the next finisher(s), 4th, 5th, as needed, until three
# horses with bets are named, and the server refuses a record that stops short. The state
# line, the cups and the TV carry the three; `next` lives in the file for M6 and in
# BettingBoard.results(), the one reader (La Subasta pays the owners of the real 1st, 2nd and
# 3rd from it). While a result stands the field is frozen: a scratch or an undo is refused with
# a line that says to clear the result first. And the field is never guessed: a store that
# could not read its database, or no La Quiniela at all, refuses the results instead of
# checking them against posts 1-20.

FROZEN_LINE = ("The results are in, so the field is frozen: clear them in SET WINNERS first (RESET in the dialog), "
               "then scratch, then enter them again.")


def _cups_at_post(rig, counts):
    """Cups reporting the given {horse: tokens} with betting open, then AT THE GATE takes the figures at the
    post from them; a horse not in `counts` has no cup, so no bets. Ends in RUNNING. Any figures from before go
    (betting starts over)."""
    rig.post("/api/quiniela/mode", {"mode": "BETTING_60", "reopen": True})
    for horse, count in counts.items():
        rig.bridge.handle_raw_line(telem("A0:B7:65:55:00:%02X" % horse, horse=horse, count=count))
    rig.board.refresh()
    rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})


def test_results_record_the_real_finish():
    from la_quiniela.betting import bets_at_post, finishers_needed, read_results
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    counts = {n: (0 if n == 4 else 2) for n in range(1, 21) if n not in (7, 9)}      # 7 has no cup, 4's is empty
    counts[22] = 3
    _cups_at_post(rig, counts)
    closing = rig.model()["closing"]
    _check("the figures at the post: 4 and 7 held nothing, 22 (for 9) 3, the rest 2",
           closing["horses"]["4"]["tokens"] == 0 and closing["horses"]["7"]["tokens"] == 0
           and closing["horses"]["22"]["tokens"] == 3 and closing["horses"]["1"]["tokens"] == 2, str(closing)[:200])
    # the rule, on its own: three horses with bets among those named, or every horse with bets when fewer had any
    bets = {1: 2, 2: 0, 3: 5, 4: 0, 5: 1, 6: 4}
    field = {1, 2, 3, 4, 5, 6, 7}
    cases = [([1, 3, 5], 0), ([2, 3, 5], 1), ([2, 4, 5], 2), ([2, 4, 5, 1], 1), ([2, 4, 5, 1, 3], 0), ([2, 4, 7], 3),
             ([1, 3, 5, 6], 0)]
    got = [finishers_needed(named, bets, field) for named, _ in cases]
    _check("finishers_needed: how many more the finish must name so three with bets are among them",
           got == [want for _, want in cases], str(got))
    _check("...fewer than three horses with bets in the field: every one of them, no more",
           finishers_needed([2, 4, 5], {1: 2, 3: 5}, field) == 2 and finishers_needed([1, 3, 2], {1: 2, 3: 5}, field) == 0
           and finishers_needed([2, 4, 5], {}, field) == 0)
    _check("...a horse with bets that is out of the field does not count (a cup scratched after the post)",
           finishers_needed([2, 4, 5], bets, {2, 4, 5, 6}) == 1)
    _check("...no figures at the post: nothing is needed (unknown is not zero)", finishers_needed([2, 4, 5], None, field) == 0)
    _check("bets_at_post reads the model's closing by horse number, 0 for a horse with no cup, None without figures",
           bets_at_post(closing)[22] == 3 and bets_at_post(closing)[7] == 0 and bets_at_post(closing)[4] == 0
           and bets_at_post(None) is None and bets_at_post({"pot": 3}) is None
           and bets_at_post({"horses": {"1": {"tokens": 2}, "2": {"tokens": "x"}, "3": {"tokens": -1}}})
           == {n: (2 if n == 1 else 0) for n in range(1, 25)})
    events = []
    saved_broadcast = main.broadcast_sse
    main.broadcast_sse = lambda event, data: events.append((event, data))

    def refused(name, body, words):
        rig.port.written.clear()
        rig.led.clear()
        events.clear()
        rev = rig.bridge.state_rev
        before = rig.results_file.read_bytes() if rig.results_file.exists() else None
        r = rig.post("/api/results", body)
        reply = r.get_json() or {}
        after = rig.results_file.read_bytes() if rig.results_file.exists() else None
        _check(f"/api/results {name}: 400 that says so, the file as it was, no line, no LEDs, no event",
               r.status_code == 400 and reply.get("success") is False and words in reply.get("error", "")
               and after == before and rig.port.written == [] and rig.led == [] and events == []
               and rig.bridge.state_rev == rev, f"{r.status_code} {reply}")

    try:
        r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
        body = r.get_json()
        saved = json.loads(rig.results_file.read_text())
        _check("three horses with bets: 200 as before, and the reply and the file carry next: []",
               r.status_code == 200 and body["results"] == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []}
               and (saved["win"], saved["place"], saved["show"], saved["next"]) == (19, 1, 22, []), str(body))
        _check("GET /api/results carries next too", rig.client.get("/api/results").get_json()["results"]["next"] == [])
        _check("BettingBoard.results(): the record, the one reader",
               rig.board.results() == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []}, str(rig.board.results()))
        # the real finish: 7 crossed first with no bets at the post
        refused("7 (no bets) for WIN with no next finisher", {"win": 7, "place": 1, "show": 22},
                "#7 (WIN) had no bets at the post: La Quiniela skips it, so enter the next finisher too (4th)")
        refused("...next: [] is the same", {"win": 7, "place": 1, "show": 22, "next": []}, "(4th)")
        refused("...a 4th with no bets either (4) needs a 5th", {"win": 7, "place": 1, "show": 22, "next": [4]},
                "#7 (WIN) and #4 (4TH) had no bets at the post: La Quiniela skips them, so enter the next finisher too (5th)")
        refused("...two of the three with no bets need two more", {"win": 7, "place": 4, "show": 22},
                "#7 (WIN) and #4 (PLACE) had no bets at the post: La Quiniela skips them, so enter the next finishers too (4th and 5th)")
        refused("next must be a list", {"win": 7, "place": 1, "show": 22, "next": 4}, "next must be a list of horse numbers")
        refused("next: a bool", {"win": 7, "place": 1, "show": 22, "next": [True]}, "next: True is not a horse number")
        refused("next: a string", {"win": 7, "place": 1, "show": 22, "next": ["4"]}, "next: '4' is not a horse number")
        refused("next: a horse named twice (1 is PLACE)", {"win": 7, "place": 1, "show": 22, "next": [1]}, "next: horse 1 is named twice")
        refused("next: 12 twice", {"win": 7, "place": 1, "show": 22, "next": [12, 12]}, "next: horse 12 is named twice")
        refused("next: 9, replaced by 22", {"win": 7, "place": 1, "show": 22, "next": [9]}, "next: horse 9 is not in the field")
        refused("next: 25", {"win": 7, "place": 1, "show": 22, "next": [25]}, "next: horse 25 is not in the field")
        # enough next finishers: recorded
        rig.port.written.clear()
        events.clear()
        rev = rig.bridge.state_rev
        r = rig.post("/api/results", {"win": 7, "place": 1, "show": 22, "next": [4, 12]})
        body = r.get_json()
        saved = json.loads(rig.results_file.read_text())
        _check("7, 1, 22 and the next finishers 4 (no bets either) and 12: 200, the record saved with next [4, 12]",
               r.status_code == 200 and body["success"] is True
               and body["results"] == {"win": 7, "place": 1, "show": 22, "next": [4, 12], "ties": []} and saved["next"] == [4, 12], str(body))
        _check("the state line carries the three real finishers only: the cups and the TV show the real finish until M6",
               rig.lines() == [state_line(rev + 1, 5, [], [], [7, 1, 22])], str(rig.lines()))
        finish = {h["number"]: h["finish"] for h in rig.client.get("/api/race").get_json()["horses"] if h["finish"]}
        _check("the model's results are the three, /api/race's finish 1 / 2 / 3 the same",
               rig.model()["results"] == {"win": 7, "place": 1, "show": 22} and finish == {7: 1, 1: 2, 22: 3}, str(finish))
        _check("the results event carries the three", events == [("results", {"win": 7, "place": 1, "show": 22})], str(events))
        _check("BettingBoard.results() carries next; GET /api/results too",
               rig.board.results() == {"win": 7, "place": 1, "show": 22, "next": [4, 12], "ties": []}
               and rig.client.get("/api/results").get_json()["results"]["next"] == [4, 12])
        r = rig.post("/api/results", {"win": 7, "place": 1, "show": 22, "next": [4, 12, 15]})
        _check("next finishers beyond the need are kept as given (the host may know the whole order)",
               r.status_code == 200 and rig.board.results()["next"] == [4, 12, 15])
        r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
        _check("a correction overwrites the record whole: next is [] again",
               r.status_code == 200 and rig.board.results() == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []})
        rig.post("/api/results/clear")
        # no figures at the post (a rig whose cups never reported, betting open): nothing to pay from, so no result (M6)
        fresh = Rig()
        fresh.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
        fresh.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
        fresh.post("/api/quiniela/mode", {"mode": "BETTING_60"})
        _check("betting open, no cup heard: no figures at the post", fresh.model()["closing"] is None)
        r = fresh.post("/api/results", {"win": 7, "place": 1, "show": 22})
        _check("with no figures at the post there is nothing to pay from: a result is refused until AT THE GATE takes them (M6)",
               r.status_code == 409 and "before the post" in r.get_json()["error"] and fresh.board.results() is None, str(r.get_json()))
        # what the file reads as (the routes read through the board, the fresh rig's now)
        fresh.results_file.write_text(json.dumps({"win": 19, "place": 1, "show": 22, "timestamp": "2027-05-01T19:02:00"}))
        _check("a results file from before `next`: the record reads next [] and ties [], GET /api/results too",
               fresh.board.results() == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []}
               and fresh.client.get("/api/results").get_json()["results"] == {"win": 19, "place": 1, "show": 22, "next": [], "ties": []})
        fresh.results_file.write_text(json.dumps({"win": 19, "place": 1, "show": 22, "next": [1, 30, "x", 12, 12, 4]}))
        _check("junk in next is dropped (a repeat, a number no horse has, a string): the record reads next [12, 4]",
               fresh.board.results()["next"] == [12, 4], str(fresh.board.results()))
        fresh.results_file.write_text(json.dumps({"win": 19, "place": 1}))
        _check("a hand-made file naming two: no record, and the state line's view is [19, 1, 0] as before",
               fresh.board.results() is None and read_results(fresh.results_file) == [19, 1, 0])
        fresh.results_file.write_text(json.dumps({"win": 19, "place": 19, "show": 1}))
        _check("a horse twice: no record, no results for the line", fresh.board.results() is None and read_results(fresh.results_file) == [0, 0, 0])
        fresh.results_file.write_text("not json")
        _check("a broken file: no record", fresh.board.results() is None and read_results(fresh.results_file) == [0, 0, 0])
        fresh.results_file.unlink()
        _check("no file: no record", fresh.board.results() is None)
    finally:
        main.broadcast_sse = saved_broadcast


def test_results_refused_while_the_field_cannot_be_read():
    """The field is never guessed. A store that could not read its database at start serves the default field
    (1-20, no scratches: la_quiniela/horses.py load_failed), so the results are refused until pi5 is restarted on a
    readable database; with no La Quiniela at all they are refused too (they used to be checked against posts 1-20
    then, so a scratched horse went through). Clearing still works without it."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 20})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    rig.board.store.load_failed = True
    try:
        rig.port.written.clear()
        rig.led.clear()
        rev = rig.bridge.state_rev
        r = rig.post("/api/results", {"win": 20, "place": 1, "show": 2})
        reply = r.get_json() or {}
        _check("a store that could not read its database: 20 (scratched) is refused, with why, nothing saved, no line, no LEDs",
               r.status_code == 400 and reply.get("success") is False and "could not read its database" in reply.get("error", "")
               and "restart pi5" in reply.get("error", "") and not rig.results_file.exists() and rig.port.written == []
               and rig.led == [] and rig.bridge.state_rev == rev, f"{r.status_code} {reply}")
        r = rig.post("/api/results", {"win": 19, "place": 1, "show": 2})
        _check("...horses that are in the field are refused the same: the field is unknown",
               r.status_code == 400 and not rig.results_file.exists())
    finally:
        rig.board.store.load_failed = False
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 2})
    _check("the store readable again: saved", r.status_code == 200 and rig.results_file.exists())
    rig.post("/api/results/clear")
    saved = main.get_board

    def no_board():
        raise RuntimeError("La Quiniela board not initialised")
    main.get_board = no_board
    try:
        r = rig.post("/api/results", {"win": 1, "place": 2, "show": 3})
        reply = r.get_json() or {}
        _check("La Quiniela not running: refused, no longer checked against posts 1-20",
               r.status_code == 400 and reply.get("success") is False and reply.get("error", "").startswith("La Quiniela is not running")
               and not rig.results_file.exists(), f"{r.status_code} {reply}")
        r = rig.post("/api/results/clear")
        _check("...clearing still works without it", r.status_code == 200 and r.get_json()["race"] is None
               and not rig.results_file.exists())
    finally:
        main.get_board = saved


def test_field_frozen_while_a_result_stands():
    """While a result stands a scratch or an undo is refused on the LQ admin page (409, FROZEN_LINE), a name is not
    (names are not the field). The dialog's RESET and the panel's RESET clear the result (/api/results/clear);
    Reset betting clears it too where it can run (0 or 6)."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    assert r.status_code == 200, r.get_json()
    rev = rig.board.store.names_rev
    records = rig.model()["scratches"]
    r = rig.post("/api/quiniela/scratch", {"horse": 20})
    _check("a result stands: a scratch is refused, 409, with the line", r.status_code == 409 and r.get_json()["error"] == FROZEN_LINE,
           str(r.get_json()))
    r = rig.post("/api/quiniela/scratch", {"horse": 3, "replacement": {"number": 23, "name": "Epic Ride"}})
    _check("...a replacement scratch too", r.status_code == 409 and r.get_json()["error"] == FROZEN_LINE)
    r = rig.post("/api/quiniela/unscratch", {"horse": 9})
    _check("...and an undo", r.status_code == 409 and r.get_json()["error"] == FROZEN_LINE)
    _check("nothing moved: names_rev as it was, 9 -> 22 still the one record, 20 in the field",
           rig.board.store.names_rev == rev and rig.model()["scratches"] == records and rig.model()["horses"]["20"]["in_field"] is True)
    r = rig.client.put("/api/quiniela/horses", json={"3": {"name": "Mystik Dan II"}})
    _check("a name is not the field: still editable with the results in", r.status_code == 200
           and rig.model()["horses"]["3"]["name"] == "MYSTIK DAN II")
    r = rig.post("/api/quiniela/reset")
    _check("Reset betting in WINNER is refused as ever (OD13), so it does not unfreeze the field from there", r.status_code == 409)
    rig.post("/api/results/clear")
    _check("RESET (the dialog's or the panel's): the result is gone, AFTER PARTY", rig.board.results() is None and rig.bridge.phase == 6)
    r = rig.post("/api/quiniela/scratch", {"horse": 20})
    _check("...and a scratch goes through", r.status_code == 200 and rig.model()["horses"]["20"]["in_field"] is False, str(r.get_json()))
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    _check("the results entered again", r.status_code == 200)
    r = rig.post("/api/quiniela/unscratch", {"horse": 20})
    _check("...frozen again: the undo is refused", r.status_code == 409 and r.get_json()["error"] == FROZEN_LINE)
    rig.post("/api/quiniela/cmd", {"cmd": "state 6"})            # AFTER PARTY with the results kept (a curl, not RESET)
    _check("AFTER PARTY keeps the results: still frozen", rig.board.results() is not None
           and rig.post("/api/quiniela/unscratch", {"horse": 20}).status_code == 409)
    r = rig.post("/api/quiniela/reset")
    _check("Reset betting (0 or 6) clears the result too, and the field is free: the undo goes through",
           r.status_code == 200 and rig.board.results() is None and rig.post("/api/quiniela/unscratch", {"horse": 20}).status_code == 200,
           str(r.get_json()))


def test_la_subasta_pays_on_set_winners():
    """End to end on the real app (gap review M5): guests join and bid, the auction locks, SET WINNERS records the
    result through POST /api/results, and La Subasta's ledger (GET /la-subasta/api/admin/payouts) pays the owners
    of the real 1st, 2nd and 3rd from that record, whole pesos; a correction through SET WINNERS moves the payouts;
    a paid-out mark the result moved away from is said; RESET (/api/results/clear) clears the payouts, the mark
    staying as paid on an earlier result; Reset betting clears them the same."""
    from la_subasta import config as ls_config, models as ls_models, scratches as ls_scratches
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    ls_models.reset_db_for_tests(ls_config.DB_PATH)       # La Subasta's own temp database, fresh
    ls_scratches.forget()
    ls_scratches.follow_la_quiniela()                       # this rig's board
    c = rig.client
    r = c.post("/la-subasta/api/admin/start")
    assert r.status_code == 200, r.get_json()
    joined = {name: c.post("/la-subasta/api/register", json={"name": name, "emoji": emoji}).get_json()
              for name, emoji in (("Alice", "🌮"), ("Bob", "🐴"))}
    guests = {name: j["bidder"] for name, j in joined.items()}

    def bid(who, horse, amount):
        r = c.post("/la-subasta/api/bid", json={"bidder_id": guests[who]["id"], "secret": joined[who]["secret"],
                                                "horse_id": horse, "amount": amount})
        assert r.status_code == 200, r.get_json()

    bid("Alice", 19, 4)
    bid("Alice", 1, 2)
    bid("Bob", 3, 3)
    bid("Bob", 7, 1)                                        # the pot: 10
    r = c.post("/la-subasta/api/admin/lock", json={"confirm": True})
    assert r.status_code == 200, r.get_json()
    _cups_at_post(rig, {n: 2 for n in range(1, 21)})        # every cup with bets: no next finisher needed
    alice, bob = guests["Alice"]["id"], guests["Bob"]["id"]

    def ledger():
        return c.get("/la-subasta/api/admin/payouts").get_json()

    led = ledger()
    _check("locked, no result: La Subasta's ledger has the pot (10) and no rows",
           led["results"] is None and led["payouts"] == [] and led["total_pot"] == 10, str(led))
    r = rig.post("/api/results", {"win": 19, "place": 3, "show": 7})
    assert r.status_code == 200, r.get_json()
    led = ledger()
    by = {p["finish"]: p for p in led["payouts"]}
    _check("SET WINNERS confirmed: the ledger pays Alice (19) 5, Bob (3) 3, Bob (7) 2 pesos, from the same record",
           led["results"] == {"win": 19, "place": 3, "show": 7}
           and (by["win"]["bidder_id"], by["win"]["amount"]) == (alice, 5)
           and (by["place"]["bidder_id"], by["place"]["amount"]) == (bob, 3)
           and (by["show"]["bidder_id"], by["show"]["amount"]) == (bob, 2), str(led))
    state = c.get("/la-subasta/api/state").get_json()
    _check("/la-subasta/api/state: LOCKED, the result, settled", state["state"] == "LOCKED"
           and state["results"] == {"win": 19, "place": 3, "show": 7} and state["settled"] is True, str(state))
    r = c.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "paid": True})
    _check("WIN marked paid out to Alice, 5", r.status_code == 200 and r.get_json()["payouts"][0]["paid"]["amount"] == 5
           and r.get_json()["payouts"][0]["paid"]["bidder_id"] == alice, str(r.get_json()))
    r = rig.post("/api/results", {"win": 1, "place": 3, "show": 7})      # a correction in SET WINNERS
    assert r.status_code == 200, r.get_json()
    led = ledger()
    _check("a correction in SET WINNERS: WIN is #1 now (Alice's too, 5 pesos) and reads unpaid; the mark is paid on an earlier result",
           led["payouts"][0]["horse_id"] == 1 and led["payouts"][0]["bidder_id"] == alice and led["payouts"][0]["paid"] is None
           and [s["line"] for s in led["paid_on_old_result"]]
           == ["Alice 🌮 was paid 5 pesos for WIN (#19); WIN now pays Alice 🌮 5 pesos for #1"], str(led))
    r = rig.post("/api/results/clear")                                   # RESET on the Control Center
    assert r.status_code == 200, r.get_json()
    led = ledger()
    _check("RESET clears the result: no payouts; the mark stays, paid on a result that is gone",
           led["results"] is None and led["payouts"] == [] and c.get("/la-subasta/api/state").get_json()["settled"] is False
           and [s["line"] for s in led["paid_on_old_result"]] == ["Alice 🌮 was paid 5 pesos for WIN (#19); there is no result now"],
           str(led))
    r = c.post("/la-subasta/api/admin/payout-paid", json={"finish": "win", "horse_id": 19, "paid": False})
    _check("unmarked: nothing stale", r.status_code == 200 and r.get_json()["paid_on_old_result"] == [], str(r.get_json()))
    r = rig.post("/api/results", {"win": 19, "place": 3, "show": 7})
    assert r.status_code == 200, r.get_json()
    _check("the result again: the payouts are back", ledger()["payouts"][0]["bidder_id"] == alice)
    r = rig.post("/api/quiniela/reset")
    _check("Reset betting is refused in WINNER (OD13): the result stands", r.status_code == 409 and ledger()["results"] is not None)
    rig.post("/api/quiniela/cmd", {"cmd": "state 6"})
    r = rig.post("/api/quiniela/reset")
    led = ledger()
    _check("Reset betting (in AFTER PARTY) clears the result and with it the payouts",
           r.status_code == 200 and led["results"] is None and led["payouts"] == [] and led["paid_on_old_result"] == [], str(led))


def test_picker_records_the_real_finish():
    """The page's side, read from the source (the picker itself is driven in test_picker_in_a_browser and
    test_picker_follows_the_field_in_a_browser): the confirm sends the next finishers; the rule is the server's,
    computed from the frozen figures and the field; the field is polled only while the picker is open; a server
    refusal stands beside the button; RESET asks before it clears a saved result, in the page's own ask
    modal (no browser confirm), through the panel's own clearResults; the note's words."""
    rig = Rig()
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True).replace("\r\n", "\n")
    html = rig.client.get("/", base_url="http://joeydevpi.local:5000").get_data(as_text=True)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    confirm = _js_function(js, "async function resultsConfirm()")
    _check("CONFIRM RESULTS sends the three and the next finishers",
           "const after = resultsState.next.slice();" in confirm and "next: after" in confirm, confirm[:400])
    _check("...and a refusal from pi5 stands beside the button (the state's refusal, not the note line) and reads the field again, the picker left open",
           "resultsState.refusal = {" in confirm and "resultsNote(" not in confirm and "loadQuinielaField()" in confirm
           and "closeResultsModal(true)" in confirm)
    needed = _js_function(js, "function finishersNeeded(named)")
    _check("the rule is one: three horses with bets among those named, the field's horses only, nothing while the figures are unknown",
           "resultsBets === null" in needed and "Math.min(3" in needed and "quinielaField" in needed)
    _check("the finish goes on in cards of their own: next:0 is 4TH", "'next:'" in js and "ordinalLabel(" in js
           and "function renderResultCards()" in js)
    _check("no card in the markup: the places' cards are all made by script into #results-cards, ahead of the confirm section",
           "4TH" not in html and 'data-slot="win"' not in html and html.index('id="results-cards"') < html.index('id="results-confirm-section"'))
    _check("the field is read every 2 s while the picker is open and only then",
           "function startResultsFieldPoll()" in js and "function stopResultsFieldPoll()" in js
           and "startResultsFieldPoll();" in _js_function(js, "async function showResultsModal()")
           and "stopResultsFieldPoll();" in _js_function(js, "async function closeResultsModal(keepAnimation = false)")
           and "RESULTS_FIELD_POLL_MS = 2000" in js)
    follows = _js_function(js, "function pickerFollowsField()")
    _check("a horse that left the field leaves its slot, its cup unlocked, the grid rebuilt, the note saying so",
           "was scratched" in follows and "generateSaddleClothGrid()" in follows and "ledCall('/api/cup/unlock'" in follows)
    reset = _js_function(js, "async function resultsReset()")
    _check("RESET asks pi5 for a saved result, then the host (askFirst, the page's own modal), then clears it through clearResults",
           "askFirst('Clear the saved result?'" in reset and "await clearResults()" in reset and "await savedResultsOnPi5()" in reset
           and "confirm(" not in reset)
    ui = _js_function(js, "function updateResultsModalUI()")
    _check("a pick nobody bet is said once over CONFIRM RESULTS: no bets at the post, La Quiniela skips it, La Subasta still pays",
           "no bets at the post. La Quiniela skips " in ui and "La Subasta still pays " in ui and "can't pay" not in js
           and "Enter the next finisher instead" not in js)
    _check("the next cards have a colour of their own", '.result-slot[data-slot^="next:"]' in css)


# -----------------------------------------------------------------------------
# La Quiniela's payout rule (gap review M6, OD3; Joey's examples and answers, 2026-10-22)
# -----------------------------------------------------------------------------

def _pays(payout):
    return [(tuple(p["slots"]), [(h["horse"], h["prize"]) for h in p["horses"]], tuple(p["skipped"])) for p in payout["pays"]]


def _at_the_post(rig, counts):
    """The field named and the given cups at the post ({horse: tokens}); the race RUNNING."""
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    _cups_at_post(rig, counts)


def test_results_with_a_dead_heat():
    rig = Rig()
    _at_the_post(rig, {7: 12, 3: 9, 12: 5, 5: 4, 19: 12})                 # $42: WIN 25, PLACE 11, SHOW 6
    r = rig.post("/api/results", {"win": 7, "place": 12, "show": 3, "ties": [[7, 12]]})
    body = r.get_json()
    _check("7 and 12 dead heat for 1st, 3 third: saved with the tie, WINNER", r.status_code == 200 and body["success"] is True
           and body["results"] == {"win": 7, "place": 12, "show": 3, "next": [], "ties": [[7, 12]]} and rig.bridge.phase == 5, str(body))
    _check("the record carries it; the cups' state line is the three slots as before",
           rig.board.results() == {"win": 7, "place": 12, "show": 3, "next": [], "ties": [[7, 12]]}
           and rig.lines()[-1] == state_line(rig.bridge.state_rev, 5, [], [], [7, 12, 3]), str(rig.lines()[-1:]))
    p = rig.model()["payout"]
    _check("the model's payout: 7 and 12 share WIN+PLACE ($36 of $42, $18 each), 3 SHOW $6",
           p["pot"] == 42 and _pays(p) == [(("win", "place"), [(7, 18), (12, 18)], ()), (("show",), [(3, 6)], ())], str(p))
    cases = (("a tie that is not consecutive in the finish", {"win": 7, "place": 12, "show": 3, "ties": [[7, 3]]}, "consecutive"),
             ("a tie four deep", {"win": 7, "place": 12, "show": 3, "next": [5], "ties": [[7, 12, 3, 5]]}, "two or three"),
             ("a tie of one", {"win": 7, "place": 12, "show": 3, "ties": [[7]]}, "two or three"),
             ("a horse in two ties", {"win": 7, "place": 12, "show": 3, "ties": [[7, 12], [12, 3]]}, "twice"),
             ("a tie naming a horse not in the finish", {"win": 7, "place": 12, "show": 3, "ties": [[7, 8]]}, "not in the finish"),
             ("ties that are not a list of lists", {"win": 7, "place": 12, "show": 3, "ties": "7,12"}, "ties must be"))
    for name, body, words in cases:
        r = rig.post("/api/results", body)
        reply = r.get_json() or {}
        _check(f"{name}: 400 that says so, the record as it was", r.status_code == 400 and words in reply.get("error", "")
               and rig.board.results()["ties"] == [[7, 12]], f"{r.status_code} {reply}")
    r = rig.post("/api/results", {"win": 7, "place": 4, "show": 3, "next": [5], "ties": [[7, 4]]})
    p = rig.model()["payout"]
    _check("7 and 4 dead heat, 4 no bets (example 9): recorded as the tie it was; La Quiniela drops 4 and pays 7 WIN, 3 PLACE, 5 SHOW",
           r.status_code == 200 and rig.board.results()["ties"] == [[7, 4]]
           and _pays(p) == [(("win",), [(7, 25)], (4,)), (("place",), [(3, 11)], ()), (("show",), [(5, 6)], ())], str(p))


def test_results_refused_before_the_post():
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    for mode, state in (("WELCOME", 0), ("BETTING_60", 1), ("FINAL_CALL", 2)):
        rig.post("/api/quiniela/mode", {"mode": mode})
        r = rig.post("/api/results", {"win": 7, "place": 3, "show": 12})
        reply = r.get_json() or {}
        _check(f"a result in {mode} (state {state}): 409, before the post, nothing saved, the state as it was",
               r.status_code == 409 and "before the post" in reply.get("error", "") and not rig.results_file.exists()
               and rig.bridge.phase == state, f"{r.status_code} {reply}")
    _cups_at_post(rig, {7: 12, 3: 9, 12: 5})
    for mode in ("AT_THE_GATE", "GATES_BURST", "HEARTBEAT_COOLDOWN"):
        rig.post("/api/quiniela/mode", {"mode": mode})
        r = rig.post("/api/results", {"win": 7, "place": 3, "show": 12})
        _check(f"...in {mode}: accepted", r.status_code == 200 and rig.bridge.phase == 5, str(r.get_json()))
    rig.post("/api/results/clear")
    r = rig.post("/api/results", {"win": 7, "place": 3, "show": 12})
    _check("in AFTER PARTY, the figures at the post still held: accepted, WINNER again", r.status_code == 200 and rig.bridge.phase == 5)


def test_results_have_one_reader():
    rig = Rig()
    _at_the_post(rig, {7: 12, 3: 9, 12: 5, 5: 4})
    _check("no record: GET /api/results says so", rig.client.get("/api/results").get_json() == {"success": False, "message": "No results available"})
    rig.post("/api/results", {"win": 7, "place": 3, "show": 12, "next": [5], "ties": [[3, 12]]})
    got = rig.client.get("/api/results").get_json()
    _check("GET /api/results is the board's record: the finish, the next finishers and the ties, nothing else",
           got == {"success": True, "results": {"win": 7, "place": 3, "show": 12, "next": [5], "ties": [[3, 12]]}}, str(got))
    _check("the spectator page reads the same record", rig.client.get("/api/spectator/state").get_json()["results"] == got["results"])
    _check("main.py has no reader of its own any more", not hasattr(main, "load_results"))


def test_payout_on_the_pages_source():
    rig = Rig()
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True).replace("\r\n", "\n")
    admin = rig.client.get("/quiniela/admin").get_data(as_text=True).replace("\r\n", "\n")
    opened = _js_function(js, "async function showResultsModal()")
    _check("the dialog opens on the saved result (CHECK THE RESULTS with the picks: a correction is one tap)",
           "savedResultsOnPi5()" in opened and "preloadResults(" in opened and "function preloadResults(saved)" in js)
    keys = _js_function(js, "function nextSlotKeys()")
    _check("the next cards appear only when a named horse had no bets and a horse with bets is left to name: the empty WIN, PLACE and SHOW count first",
           "finishersNeeded(namedFinish())" in keys and "empty" in keys)
    _check("a dead heat is made on the link on the seam under a card, undone by UNTIE, and sent as ties",
           "function tieAfter(key)" in js and "function untie(key)" in js and "ties: ties" in _js_function(js, "async function resultsConfirm()")
           and "link.onclick = () => tieAfter(last);" in _js_function(js, "function renderResultCards()"))
    _check("the admin page has a RESULTS section: the paying horses, the skips, the draw instruction, the refunds, which pot",
           'id="results"' in admin and 'id="results-pays"' in admin and 'id="results-refunds"' in admin and 'id="results-pot"' in admin
           and "function renderResults()" in admin and "draw one token from cup" in admin)


# CONFIRM RESULTS and the reason pi5 gave: the words, the button's, the type sizes, how near the reason stands, the outline
REFUSAL_JS = """JSON.stringify((() => { const b = document.getElementById('results-confirm-btn'), r = document.getElementById('results-confirm-refusal');
    const bb = b.getBoundingClientRect(), rb = r.getBoundingClientRect(), bs = getComputedStyle(b), rs = getComputedStyle(r);
    return {button: b.innerText.trim(), reason: r.innerText.trim(), refused: b.classList.contains('is-refused'),
            outline: [bs.outlineStyle, bs.outlineWidth, bs.outlineColor], buttonSize: bs.fontSize, reasonSize: rs.fontSize,
            gap: bb.top - rb.bottom, within: rb.left >= bb.left - 1 && rb.right <= bb.right + 1, onScreen: bb.top >= 0 && bb.bottom <= innerHeight}; })())"""


def _picker_payout(picker_check, requests, slow):
    """The bench: one cup with bets (19), the rest empty. The cards only when needed, the dialog reopening on the
    saved result, a dead heat entered, the admin page's RESULTS section, and a result refused before the post."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    _cups_at_post(rig, {19: 12})                                   # only 19 held bets at the post ($12); RUNNING
    server = picker_check.Server(rig)
    picker = picker_check.Picker(server, touch=True)
    chrome = picker.chrome
    modal_up = "document.getElementById('results-modal').classList.contains('active')"
    cards = "document.querySelectorAll('.result-slot[data-slot^=\"next:\"]').length"

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def picks():
        st = picker.state()
        return (st["win"], st["place"], st["show"])

    try:
        picker.open()
        st = picker.state()
        _check("an empty dialog, one horse with bets in the field: CHOOSE WINNER and no 4TH card (the empty slots come first)",
               st["header"] == "CHOOSE WINNER" and chrome.eval(cards) == 0, f"{st['header']} cards {chrome.eval(cards)}")
        picker.tap_horse(7)
        settle(0.3)
        picker.tap_horse(3)
        settle(0.3)
        _check("7 and 3 (no bets) as WIN and PLACE: still no card, SHOW can take the horse with bets",
               chrome.eval(cards) == 0 and picks() == (7, 3, None), str(picks()))
        picker.tap_horse(19)
        settle(0.3)
        st = picker.state()
        _check("19 as SHOW: the one horse with bets is named, no card, CHECK THE RESULTS, CONFIRM RESULTS up",
               chrome.eval(cards) == 0 and st["confirm"] is True and st["header"] == "CHECK THE RESULTS", str(st))
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("!" + modal_up, timeout=10)
        saved = rig.board.results()
        _check("confirmed: 7 / 3 / 19, no next finisher, no tie", saved == {"win": 7, "place": 3, "show": 19, "next": [], "ties": []}, str(saved))
        p = rig.model()["payout"]
        _check("La Quiniela pays 19 the whole $12: 7 and 3 skipped, PLACE and SHOW unpaid, both to WIN",
               _pays(p) == [(("win",), [(19, 12)], (7, 3))] and p["unpaid_to_win"] == 5, str(p))
        # the dialog reopened while the result stands: CHECK THE RESULTS with the saved picks, a correction one tap
        chrome.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=15)
        server.led_log.clear()
        picker.open()
        st = picker.state()
        _check("reopened while the result stands: CHECK THE RESULTS with the saved picks in the slots, the three cups lit again",
               st["header"] == "CHECK THE RESULTS" and picks() == (7, 3, 19) and st["confirm"] is True
               and wait_for(lambda: any(c.startswith("CUP:LOCK:19:") for c in server.led("CUP:LOCK")), 3.0), str(st))
        chrome.tap(".result-slot[data-slot='show']")
        settle(0.3)
        picker.tap_horse(12)
        settle(0.3)
        _check("a correction in one tap: SHOW is 12 now, WIN and PLACE as they were; 12 had no bets, so a 4TH card asks for 19",
               picks() == (7, 3, 12) and chrome.eval(cards) == 1, f"{picks()} cards {chrome.eval(cards)}")
        picker.tap_horse(19)
        settle(0.3)
        _check("19 as 4TH: complete again", picker.state()["next"] == [19] and picker.state()["confirm"] is True)
        # a dead heat: the link on the seam under WIN ties PLACE's horse to it
        chrome.tap(".seam button[data-after='win']")
        settle(0.3)
        tied = chrome.eval("JSON.stringify(resultsState.tied)")
        title = chrome.eval("document.querySelector('.is-dead-heat .group-title').innerText")
        _check("the seam under WIN: PLACE is tied with it and one card says so", json.loads(tied) == ["place"]
               and title == "WIN · PLACE — DEAD HEAT", f"{tied} {title!r}")
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("!" + modal_up, timeout=10)
        saved = rig.board.results()
        _check("confirmed with the tie: 7 and 3 dead heat for 1st, 12 third, 19 fourth",
               saved == {"win": 7, "place": 3, "show": 12, "next": [19], "ties": [[7, 3]]}, str(saved))
        p = rig.model()["payout"]
        _check("...both tied horses had no bets, 12 too: 19 pays WIN, the whole pot", _pays(p) == [(("win",), [(19, 12)], (7, 3, 12))], str(p))
        chrome.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=15)
        picker.open()
        tied = chrome.eval("JSON.stringify(resultsState.tied)")
        title = chrome.eval("(document.querySelector('.is-dead-heat .group-title') || {}).innerText")
        _check("reopened on the tie: the saved dead heat is in the dialog, as one card", json.loads(tied) == ["place"] and picks() == (7, 3, 12)
               and picker.state()["next"] == [19] and title == "WIN · PLACE — DEAD HEAT", f"{tied} {picks()} {title!r}")
        chrome.tap("#results-cancel-btn")
        chrome.wait_for("!" + modal_up, timeout=10)
        _check("CANCEL with a result standing puts the mantle back on it (RESULTS_ACTIVE), not off",
               wait_for(lambda: any(c.startswith("ANIM:RESULTS_ACTIVE:7:3:12") for c in server.led("ANIM:")), 3.0), str(server.led("ANIM:")[-3:]))
        # the admin page's RESULTS section reads the same payout
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.getElementById('results') && !document.getElementById('results').hidden"
                        " && document.getElementById('results-pays').innerText.length > 0", timeout=15)
        text = chrome.eval("document.getElementById('results-pays').innerText + ' | ' + document.getElementById('results-pot').innerText")
        _check("the admin page's RESULTS section: WIN pays 19 $12, the skipped horses named, the draw instruction, the pot and its source",
               "WIN" in text and "#7, #3, #12 no bets" in text and "$12" in text and "draw one token from cup 19" in text
               and "the scales at the post" in text and "added to the WIN pool" in text, text[:400])
        # before the post: RESET ends the race (AFTER PARTY), Reset betting puts it in PRE-RACE with the result and the
        # figures at the post gone; a result is refused there
        rig.post("/api/results/clear")
        reset = rig.post("/api/quiniela/reset")
        assert reset.status_code == 200, reset.get_json()
        picker.load()
        picker.open()
        for horse in (7, 3, 12):
            picker.tap_horse(horse)
            settle(0.3)
        chrome.wait_for("getComputedStyle(document.getElementById('results-confirm-section')).display !== 'none'", timeout=5)
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("document.getElementById('results-confirm-refusal').innerText.includes('before the post')", timeout=8)
        settle(0.5)                                                  # the button's colours ease over 0.2 s
        shown = json.loads(chrome.eval(REFUSAL_JS))
        _check("before the post: pi5 refuses, nothing saved, the picker open, no line under the heading",
               rig.board.results() is None and chrome.eval(modal_up) and rig.bridge.phase == 0 and picker.note() == "", repr(picker.note()))
        _check("...its reason stands right over the button in the button's own type size, the button says NOT SAVED in a red outline",
               "AT THE GATE" in shown["reason"] and shown["button"] == "NOT SAVED" and shown["refused"]
               and shown["outline"] == ["solid", "3px", "rgb(211, 47, 47)"] and shown["reasonSize"] == shown["buttonSize"]
               and 0 <= shown["gap"] <= 12 and shown["within"] and shown["onScreen"], str(shown))
        chrome.tap(".result-slot[data-slot='show']")
        settle(0.3)
        picker.tap_horse(1)
        settle(0.3)
        shown = json.loads(chrome.eval(REFUSAL_JS))
        _check("a pick changed: the button is CONFIRM RESULTS again, the reason gone",
               shown["button"] == "CONFIRM RESULTS" and not shown["refused"] and shown["reason"] == "", str(shown))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        picker.close()
        server.close()


def test_picker_payout_in_a_browser():
    """SET WINNERS in headless Chrome on the bench's field (one cup with bets): no 4TH card on an empty dialog, the
    dialog reopening on the saved result with a correction one tap, a dead heat entered and saved, CANCEL putting the
    mantle back, the admin page's RESULTS section, and a result refused before the post. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("payout picker checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _picker_payout(picker_check, requests, slow))


# -----------------------------------------------------------------------------
# The LED wall and the tote board never block the race (Joey, DevPi, 2026-10-20 and 21)
# -----------------------------------------------------------------------------
# With the wall off every LED-bound request waited 5 s on pi5, and the page issues them all
# the time (the STATUS poll, a PING after each action, each tap's animation), so the browser's
# few connections per host queued the race state behind them: a minute's lag, FINISH stuck.
# Now a send is handed to the device's worker (communication/sender.py) and the request
# returns; the state, the gateway's line and the pages' event never wait on a device.

class DeadWall:
    """A socket nothing answers on: accepts, reads, and never replies (the wall off)."""

    def __init__(self):
        import socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import socket
        self.sock.settimeout(0.1)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn):
        with conn:
            conn.settimeout(2.0)
            try:
                self.received.append(conn.recv(1024).decode().strip())
            except OSError:
                return
            time.sleep(1.0)

    def close(self):
        self._stop = True
        self.sock.close()


def test_state_change_never_waits_on_the_wall():
    """The real LED client against a socket nothing answers on (0.3 s timeout instead of the night's
    5 s): a mode's two requests, SET WINNERS and RESET all return at once, the race state, the gateway's
    line and the results event happen as they would, and the wall's health is said, not waited for."""
    wall = DeadWall()
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.real_wall(wall.port, timeout=0.3)
    events = []
    saved_broadcast = main.broadcast_sse
    main.broadcast_sse = lambda event, data: events.append((event, data))
    try:
        rig.port.written.clear()
        t0 = time.monotonic()
        r_mode = rig.post("/api/quiniela/mode", {"mode": "GATES_BURST"})
        r_led = rig.post("/api/animation/GATES_BURST")
        took = time.monotonic() - t0
        _check("THEY'RE OFF with the wall dead: both requests back within 0.15 s, the race state set, the line written",
               r_mode.status_code == 200 and r_led.status_code == 200 and took < 0.15 and rig.bridge.phase == 4
               and len(rig.lines()) == 1, f"{took:.3f} s, phase {rig.bridge.phase}, {r_led.get_json()}")
        _check("the LED route's reply: queued, success, and the wall's health as known so far",
               r_led.get_json()["success"] is True and r_led.get_json()["queued"] == "ANIM:GATES_BURST"
               and r_led.get_json()["leds"] in ("unknown", "unreachable"), str(r_led.get_json()))
        _check("the wall was tried once", wait_for(lambda: wall.received == ["ANIM:GATES_BURST"], 2.0), str(wall.received))
        t0 = time.monotonic()
        for mode in ("CHAOS", "FINISH"):
            rig.post("/api/quiniela/mode", {"mode": mode})
            rig.post("/api/animation/" + mode)
        took = time.monotonic() - t0
        _check("CHAOS and FINISH tapped straight after: at once, RUNNING still, and the wall holds at most the newest animation",
               took < 0.2 and rig.bridge.phase == 4 and main.esp32.health()["queued"] <= 1, f"{took:.3f} s {main.esp32.health()}")
        _check("the wall's health: unreachable, since when", wait_for(lambda: main.esp32.health()["online"] is False, 2.0)
               and main.esp32.health()["unreachable_since"] is not None, str(main.esp32.health()))
        status = rig.client.get("/api/status").get_json()
        _check("GET /api/status says so for the wall, and that the tote is off here",
               status["leds"]["online"] is False and status["leds"]["enabled"] is True and status["tote"]["enabled"] is False, str(status))
        rig.port.written.clear()
        t0 = time.monotonic()
        r = rig.post("/api/results", {"win": 19, "place": 1, "show": 2})
        took = time.monotonic() - t0
        body = r.get_json()
        _check("SET WINNERS with the wall dead: back at once, saved, WINNER in one line, the results event sent, leds unreachable",
               r.status_code == 200 and took < 0.15 and body["success"] is True and body["leds"] == "unreachable"
               and rig.results_file.exists() and rig.bridge.phase == 5 and rig.lines() == [state_line(rig.bridge.state_rev, 5, [], [], [19, 1, 2])]
               and [e for e, _ in events] == ["results"], f"{took:.3f} s {body}")
        _check("RESULTS:FINALIZE was handed to the worker", wait_for(lambda: "RESULTS:FINALIZE" in wall.received, 3.0), str(wall.received))
        t0 = time.monotonic()
        r = rig.post("/api/results/clear")
        took = time.monotonic() - t0
        _check("RESET: at once, AFTER PARTY, the file gone", r.status_code == 200 and took < 0.15 and rig.bridge.phase == 6
               and not rig.results_file.exists(), f"{took:.3f} s")
    finally:
        main.broadcast_sse = saved_broadcast
        wall.close()


def test_race_goes_on_without_the_wall():
    """The racing service's own progression (AUTO, the demo) is driven by its timers alone: with every LED
    send timing out it walks its states on schedule. Durations shortened to the loop's own half-second step."""
    from services import racing_data_service as rds
    wall = DeadWall()
    rig = Rig()
    rig.real_wall(wall.port, timeout=0.3)
    saved = dict(rds.STATE_DURATIONS)
    svc = main.racing_service
    try:
        for state in rds.STATE_DURATIONS:
            rds.STATE_DURATIONS[state] = 0.1
        svc.reset()
        t0 = time.monotonic()
        svc.set_mode("auto")
        reached = wait_for(lambda: svc.current_state == rds.RaceState.OFFICIAL, 8.0)
        took = time.monotonic() - t0
        _check("AUTO from DORMANT reaches OFFICIAL on its timers with the wall dead: 7 steps of half a second, not 7 x 5 s",
               reached and took < 6.0, f"{svc.current_state} after {took:.1f} s")
        _check("the LED sends went to the worker: at most one animation waits, the rest were dropped",
               main.esp32.health()["queued"] <= 1, str(main.esp32.health()))
    finally:
        svc.set_mode("manual")
        svc.reset()
        rds.STATE_DURATIONS.clear()
        rds.STATE_DURATIONS.update(saved)
        wall.close()


def test_device_health_on_the_page_source():
    """The page's side, read from the source (the browser test drives it): one poll of /api/status every 5 s
    feeds two pills and the device modal; a toast once per transition per device; no PING after an action,
    no error toast per tap for a device that is down, no clearing of the active button."""
    rig = Rig()
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True).replace("\r\n", "\n")
    html = rig.client.get("/", base_url="http://joeydevpi.local:5000").get_data(as_text=True)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    _check("the two pills in the top bar, in place of the icon and the badge",
           'id="device-leds"' in html and 'id="device-tote"' in html and 'id="device-count-badge"' not in html and ".device-pill" in css)
    _check("one poll, /api/status, every 5 s; /api/power and /api/ping are not asked by the page",
           "async function pollDevices()" in js and "setInterval(pollDevices, 5000)" in js and "fetch('/api/status'" in js
           and "/api/power" not in js and "/api/ping" not in js)
    _check("no PING after every action: checkESP32Status is gone", "checkESP32Status" not in js and "pollPowerStatus" not in js
           and "handleEsp32Offline" not in js)
    render = _js_function(js, "function renderDevices(status)")
    _check("a toast once per transition, in plain words, and none while nothing changes",
           "unreachable" in render and "back online" in render and "showNotification(" in render
           and "the race goes on" in render)
    _check("the wall going away no longer clears the active button or says the animation stopped",
           "animation stopped" not in js and "DISCONNECTED" not in js)
    _check("no per-tap error toast for the wall: the animation route's success is the tap's own",
           "LEDs unreachable" not in js and "Error: ${data.response}" not in _js_function(js, "async function animate(animName, buttonElement)"))
    rules = {rule.rule for rule in main.app.url_map.iter_rules()}
    _check("/api/power is gone; /api/status carries both devices", "/api/power" not in rules and "/api/status" in rules)


def _device_indicators(picker_check, requests, slow):
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True)
    toasts = []

    def pill(which):
        return chrome.eval(f"(document.getElementById('device-{which}') || {{}}).innerText || ''")

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    try:
        chrome.call("Page.navigate", url=server.url)
        chrome.wait_for("document.readyState === 'complete' && typeof pollDevices === 'function'", timeout=30)
        chrome.wait_for("(() => { const s = document.getElementById('splash-screen'); return !s || s.style.display === 'none'; })()", timeout=20)
        chrome.eval("window.__toasts = []; const _sn = showNotification; showNotification = (m, t) => { window.__toasts.push([m, t || 'success']); return _sn(m, t); }; 1")
        chrome.wait_for("document.getElementById('device-leds').innerText.includes('online')", timeout=12)
        _check("the wall answering: the pill reads LED wall · online, the tote's reads off (none set here)",
               pill("leds") == "LED wall \u00b7 online" and pill("tote") == "Tote board \u00b7 off", f"{pill('leds')!r} {pill('tote')!r}")
        _check("...and no toast for a device that was never away", chrome.eval("window.__toasts.length") == 0, str(chrome.eval("window.__toasts")))
        server.led_timeout = True                                   # the wall off from now on: every exchange times out
        chrome.wait_for("document.getElementById('device-leds').innerText.includes('unreachable')", timeout=15)
        since = pill("leds")
        _check("the wall off: within the poll the pill reads unreachable since hh:mm",
               re.fullmatch(r"LED wall \u00b7 unreachable since \d\d:\d\d", since) is not None, repr(since))
        settle(6.0)
        toasts = chrome.eval("window.__toasts")
        _check("...one toast, red, saying the race goes on, and no more as the polls go by",
               len(toasts) == 1 and toasts[0][1] == "error" and "LED wall unreachable" in toasts[0][0] and "race goes on" in toasts[0][0],
               str(toasts))
        chrome.eval("window.__toasts = []; 1")
        t = time.monotonic()
        chrome.tap("button[data-mode='BETTING_60']")
        chrome.wait_for("raceStateSending === null && !document.querySelector(\"button[data-mode='BETTING_60'].is-sending\")", timeout=12)
        settle(0.5)
        toasts = chrome.eval("window.__toasts")
        _check("a tap with the wall off: at once, the race state set, the tap's own green toast and no red one",
               rig.model()["race_state"] == 1 and time.monotonic() - t < 4.0 and len(toasts) == 1 and toasts[0][1] == "success"
               and toasts[0][0] == "Animation: BETTING_60 \u00b7 BETTING OPEN", str(toasts))
        _check("...the button stays lit", chrome.eval("document.querySelector(\"button[data-mode='BETTING_60']\").classList.contains('active')"))
        chrome.eval("window.__toasts = []; 1")
        server.led_timeout = False
        chrome.wait_for("document.getElementById('device-leds').innerText === 'LED wall \u00b7 online'", timeout=15)
        settle(1.0)
        toasts = chrome.eval("window.__toasts")
        _check("the wall back: the pill reads online, one green toast", len(toasts) == 1 and toasts[0][1] == "success"
               and "LED wall back online" in toasts[0][0], str(toasts))
        _check("...and the wall was given the current animation again",
               wait_for(lambda: server.led("ANIM:BETTING_60") != [], 3.0), str(server.led_log[-4:]))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        server.close()


def test_device_indicators_in_a_browser():
    """The Control Center in headless Chrome: the LED wall pill follows the wall (online; unreachable since
    hh:mm; online again), with one toast each way and none per tap; a tap with the wall off goes at once and
    keeps its button; the wall is given the current animation when it returns. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("device indicator browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _device_indicators(picker_check, requests, slow))


# -----------------------------------------------------------------------------
# The field in program-number order, the SET WINNERS cards, a scratch after the post (Joey, 2026-10-23)
# -----------------------------------------------------------------------------

TILES_JS = """JSON.stringify([...document.querySelectorAll('.winner-pick-btn')].map((b) => {
    const r = b.getBoundingClientRect();
    return {post: Number(b.dataset.post), horse: Number(b.dataset.horse), shown: b.querySelector('.winner-pick-num').textContent.trim(),
            title: b.title, top: Math.round(r.top), left: Math.round(r.left)};
}))"""


def _grid_rows(tiles):
    """The horses of the grid as laid out: one list per visual row, left to right."""
    rows = {}
    for tile in tiles:
        rows.setdefault(tile["top"], []).append(tile)
    return [[t["horse"] for t in sorted(row, key=lambda t: t["left"])] for _, row in sorted(rows.items())]


def _picker_field_order(picker_check, requests, slow):
    """#21 draws in for #19 (post 19 is its LED cup); later #7 is scratched with no replacement. The grid lists the field in
    program-number order, #21 last, no blank where #7 was, and a tap on a tile records that tile's own program number, never
    its position in the grid: tiles are tapped by position and compared with the field as the store has it."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 19, "replacement": {"number": 21, "name": "Late Entry"}})
    _cups_at_post(rig, {**{n: 2 for n in range(1, 21) if n != 19}, 21: 3})
    server = picker_check.Server(rig)
    picker = picker_check.Picker(server, touch=True)
    chrome = picker.chrome
    modal_up = "document.getElementById('results-modal').classList.contains('active')"

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def tiles():
        return json.loads(chrome.eval(TILES_JS))

    def field():                                     # the store's, not the page's
        return [h["number"] for h in rig.board.store.field()["horses"]]

    def tap_position(k):                             # the k-th tile of the grid, 1-based, as a finger finds it
        chrome.tap(f".winner-pick-btn:nth-child({k})")
        settle(0.3)

    try:
        picker.open()
        t = tiles()
        _check("#19 replaced by #21: the grid lists the field by program number, 21 last, the last row 17, 18, 20, 21",
               [x["horse"] for x in t] == field() == [n for n in range(1, 19)] + [20, 21]
               and _grid_rows(t)[-1] == [17, 18, 20, 21], str(_grid_rows(t)))
        _check("...each tile shows its own number and carries its post: 21 runs from post 19, the LED cup",
               all(x["shown"] == str(x["horse"]) and x["title"].startswith(f"{x['horse']} \u00b7") for x in t)
               and next(x for x in t if x["horse"] == 21)["post"] == 19 and all(x["post"] == x["horse"] for x in t if x["horse"] != 21),
               str([(x["post"], x["horse"]) for x in t if x["post"] != x["horse"]]))
        tap_position(20)
        st = picker.state()
        _check("the last tile (21): WIN is horse 21 and its cup is post 19, its colour locked on that cup",
               st["win"] == 21 and st["posts"]["win"] == 19 and wait_for(lambda: "CUP:LOCK:19:255:215:0" in server.led("CUP:LOCK"), 3.0),
               f"{st['win']} {st['posts']} {server.led('CUP:LOCK')}")
        _check("...and its slot card names it", "LATE ENTRY" in st["slots"]["win"].upper(), repr(st["slots"]["win"]))
        r = rig.post("/api/quiniela/scratch", {"horse": 7})
        assert r.status_code == 200, r.get_json()
        chrome.wait_for("!document.querySelector(\".winner-pick-btn[data-horse='7']\")", timeout=8)
        settle(0.3)
        t = tiles()
        _check("#7 scratched with no replacement while the picker is open: the grid closes up (no blank), the last row 18, 20, 21",
               [x["horse"] for x in t] == field() == [n for n in range(1, 19) if n != 7] + [20, 21]
               and _grid_rows(t)[-1] == [18, 20, 21] and not chrome.eval("document.querySelectorAll('.winner-pick-gap').length"),
               str(_grid_rows(t)))
        _check("...the pick of 21 stays, on post 19", picker.state()["win"] == 21 and picker.state()["posts"]["win"] == 19)
        tap_position(7)                              # where 7 was: now 8
        tap_position(1)
        st = picker.state()
        _check("the 7th tile is now horse 8, the 1st horse 1: PLACE 8, SHOW 1, each the tile's own number",
               (st["win"], st["place"], st["show"]) == (21, 8, 1) and st["posts"]["place"] == 8 and st["posts"]["show"] == 1, str(st))
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("!" + modal_up, timeout=10)
        saved = rig.board.results()
        _check("CONFIRM RESULTS records the numbers of the tiles that were tapped: 21, 8, 1",
               saved == {"win": 21, "place": 8, "show": 1, "next": [], "ties": []}, str(saved))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        picker.close()
        server.close()


def test_picker_grid_in_program_number_order_in_a_browser():
    """Joey, 2026-10-23: SET WINNERS lists the field by program number (a replacement where its number falls, no blanks),
    and a tap records the tile's own number. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("SET WINNERS order checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _picker_field_order(picker_check, requests, slow))


CARDS_BOXES_JS = """JSON.stringify([...document.querySelectorAll('.result-group')].map((group) => {
    const rect = (el) => { const r = el.getBoundingClientRect(); return {left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                                                                          width: r.width, height: r.height}; };
    const shown = (el) => { if (!el) return null; const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return null; const r = rect(el); return r.width && r.height ? r : null; };
    const cell = (card) => {
        const name = card.querySelector('.slot-horse-name');
        let text = null;
        if (name) { const range = document.createRange(); range.selectNodeContents(name);
                    text = Object.assign(rect(range), {clipped: name.scrollWidth > name.clientWidth + 1, font: parseFloat(getComputedStyle(name).fontSize),
                                                       words: name.textContent}); }
        const part = (sel) => shown(card.querySelector(sel));
        return {slot: card.dataset.slot, filled: card.classList.contains('is-filled'), card: rect(card),
                buttons: [...card.querySelectorAll('button')].filter(shown).map((b) => b.className),
                parts: {label: part('.slot-label'), clear: part('.slot-clear'), saddle: part('.slot-saddle'), name: text, nobets: part('.slot-nobets')}};
    };
    return {dead: group.classList.contains('is-dead-heat'), box: rect(group),
            title: (group.querySelector('.group-title') || {}).innerText || '', untie: shown(group.querySelector('.group-untie')),
            cells: [...group.querySelectorAll('.result-slot')].map(cell)};
}))"""

SEAMS_JS = """JSON.stringify([...document.querySelectorAll('.seam button')].map((b) => { const r = b.getBoundingClientRect();
    return {after: b.dataset.after, text: b.textContent, left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height}; }))"""

# The column against the viewport (the modal's scroll, the sidebar, CONFIRM RESULTS), the note over the button with its colours,
# and any dashed line or "with WIN" caption left over from the old dead heat
FIT_JS = """JSON.stringify((() => { const m = document.getElementById('results-modal'), s = document.querySelector('.results-sidebar').getBoundingClientRect(),
    b = document.getElementById('results-confirm-btn').getBoundingClientRect(), n = document.getElementById('results-confirm-notes');
    return {viewport: [innerWidth, innerHeight], scroll: [m.scrollHeight, m.clientHeight], sidebar: [s.top, s.bottom], button: [b.top, b.bottom],
            note: n.innerText, noteColour: getComputedStyle(n).color, noteBorder: getComputedStyle(n).borderTopColor,
            boxes: document.querySelectorAll('.confirm-notes, .confirm-warning').length,
            dashed: [...document.querySelectorAll('.result-group, .result-slot')].some((el) => getComputedStyle(el).borderTopStyle === 'dashed'),
            caption: m.innerText.includes('with WIN')}; })())"""


def _overlap(a, b):
    return a["left"] < b["right"] - 0.5 and b["left"] < a["right"] - 0.5 and a["top"] < b["bottom"] - 0.5 and b["top"] < a["bottom"] - 0.5


def _grey(css_colour):
    channels = [int(n) for n in re.findall(r"\d+", css_colour)[:3]]
    return max(channels) - min(channels) <= 24


def _picker_cards(picker_check, requests, slow):
    """The picks column of SET WINNERS (Joey, 2026-10-26: laid out for a room that is waiting), the longest names of the 2026
    field on the cards, 15 and 12 with no bets: a card shows the pick and nothing else, the x alone at its top corner above
    the cloth; a dead heat is one card, two horses side by side or three, with one UNTIE and no caption or dashed line; a
    dead heat for third merges SHOW with 4TH; the link on every seam; the note; and 1180 x 820 holds a three-way dead heat, a
    4TH and a 5TH card and the note with the whole column on screen. What is pressed is 44 px or more each way."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": "3. Grand Mo the First\n5. Right to Party\n12. Chief Wallabee\n15. Emerging Market\n19. Golden Tempo"})
    _cups_at_post(rig, {n: 2 for n in range(1, 21) if n not in (15, 12)})                  # 15 and 12 held nothing at the post
    server = picker_check.Server(rig)
    picker = picker_check.Picker(server, touch=True)
    chrome = picker.chrome

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def groups():
        return json.loads(chrome.eval(CARDS_BOXES_JS))

    def seams():
        return {s["after"]: s for s in json.loads(chrome.eval(SEAMS_JS))}

    def order(gs):
        return [[c["slot"] for c in g["cells"]] for g in gs]

    def problems(gs):
        """What is wrong with the cards as laid out: a part over another or outside its card, a name clipped or small, a
        button that is not the x (or any in a dead heat's cell), the x not at the top corner above the cloth, a target under 44 px."""
        out = []
        for g in gs:
            if g["dead"] and (not g["untie"] or g["untie"]["width"] < 44 or g["untie"]["height"] < 44):
                out.append(f"{g['title']}: UNTIE is missing or under 44 px")
            for c in g["cells"]:
                if not c["filled"]:
                    continue
                parts = {k: v for k, v in c["parts"].items() if v}
                names = sorted(parts)
                for i, a in enumerate(names):
                    for b in names[i + 1:]:
                        if _overlap(parts[a], parts[b]):
                            out.append(f"{c['slot']}: {a} overlaps {b}")
                for k, v in parts.items():
                    if v["left"] < c["card"]["left"] - 1 or v["right"] > c["card"]["right"] + 1 or v["bottom"] > c["card"]["bottom"] + 1:
                        out.append(f"{c['slot']}: {k} sticks out of its card")
                if parts["name"]["clipped"]:
                    out.append(f"{c['slot']}: the name is clipped")
                if parts["name"]["font"] < (14 if g["dead"] else 18):
                    out.append(f"{c['slot']}: the name is down to {parts['name']['font']}px")
                if c["buttons"] != ([] if g["dead"] else ["slot-clear"]) or (g["dead"] and "label" in parts):
                    out.append(f"{c['slot']}: buttons {c['buttons']}, label {'label' in parts}")
                clear = parts.get("clear")
                if clear and (clear["width"] < 44 or clear["height"] < 44 or clear["bottom"] > parts["saddle"]["top"] + 0.5
                              or clear["right"] < c["card"]["right"] - 14):
                    out.append(f"{c['slot']}: the x is {clear['width']:.0f} x {clear['height']:.0f}, not alone at the top corner above the cloth")
        return out

    def seam_problems(gs, links):
        """A link under 44 px each way, over a cloth, a name, a badge, an x or an UNTIE, or reaching more than 11 px into the card
        above or 19 into the card below (its padding, and the middle of the head row)."""
        out = []
        pressed = [(c["slot"], k, v) for g in gs for c in g["cells"] for k, v in c["parts"].items() if v and k != "label"]
        pressed += [("the dead heat", "UNTIE", g["untie"]) for g in gs if g["untie"]]
        for after, s in links.items():
            if s["width"] < 44 or s["height"] < 44:
                out.append(f"the link after {after} is {s['width']:.0f} x {s['height']:.0f}")
            out += [f"the link after {after} covers {owner}'s {k}" for owner, k, v in pressed if _overlap(s, v)]
        for i, g in enumerate(gs):
            s = links.get(g["cells"][-1]["slot"])
            below = gs[i + 1]["box"]["top"] if i + 1 < len(gs) else None
            if s and (s["top"] < g["box"]["bottom"] - 11.5 or s["top"] > g["box"]["bottom"] + 0.5
                      or (below is not None and (s["bottom"] < below - 0.5 or s["bottom"] > below + 19.5))):
                out.append(f"the link after {g['cells'][-1]['slot']} is at {s['top']:.0f}-{s['bottom']:.0f} between the cards at {g['box']['bottom']:.0f} and {below}")
        return out

    try:
        picker.open()
        for horse in (15, 19, 5, 3):                 # 15 had no bets: the 4TH card asks for the next finisher
            picker.tap_horse(horse)
            settle(0.3)
        gs, links = groups(), seams()
        _check("WIN 15 EMERGING MARKET (NO BETS), PLACE 19 GOLDEN TEMPO, SHOW 5 RIGHT TO PARTY, 4TH 3 GRAND MO THE FIRST: four cards, each the "
               "place's name, the cloth, the name whole and the badge where it applies, the x alone at its top corner above the cloth",
               order(gs) == [["win"], ["place"], ["show"], ["next:0"]] and not problems(gs), str(problems(gs)))
        _check("a link on every seam, between the cards and under the last: a 44 px tap area in the middle of the gap, over no cloth, name, "
               "badge or x", sorted(links) == ["next:0", "place", "show", "win"] and not seam_problems(gs, links), str(seam_problems(gs, links)))
        fit = json.loads(chrome.eval(FIT_JS))
        _check("one quiet box over CONFIRM RESULTS says it once: #15, no bets at the post, La Quiniela skips it, La Subasta still pays its owner",
               fit["note"] == "#15: no bets at the post. La Quiniela skips it; La Subasta still pays its owner." and fit["boxes"] == 1
               and _grey(fit["noteColour"]) and _grey(fit["noteBorder"]), str(fit))
        chrome.eval("(() => { const link = document.querySelector(\".seam button[data-after='win']\"); link.click(); link.click(); return 1; })()")
        settle(0.3)
        _check("a click that fires twice on the link under WIN ties once", chrome.eval("resultsState.tied.length") == 1, chrome.eval("JSON.stringify(resultsState.tied)"))
        gs, links = groups(), seams()
        g = gs[0]
        _check("a dead heat between WIN and PLACE: one card, taller than a single one, headed WIN · PLACE — DEAD HEAT, the two horses side by "
               "side inside it, one UNTIE, no x or button in the cells",
               order(gs) == [["win", "place"], ["show"], ["next:0"]] and g["dead"] and g["title"] == "WIN · PLACE — DEAD HEAT"
               and abs(g["cells"][0]["card"]["top"] - g["cells"][1]["card"]["top"]) < 1
               and g["cells"][0]["card"]["right"] <= g["cells"][1]["card"]["left"] + 1 and g["box"]["height"] > gs[1]["box"]["height"]
               and not problems(gs), f"{g['title']!r} {problems(gs)}")
        fit = json.loads(chrome.eval(FIT_JS))
        _check("...no dashed line and no 'DEAD HEAT · with WIN' caption; its link ties SHOW in as the third, SHOW's and 4TH's stay",
               not fit["dashed"] and not fit["caption"] and sorted(links) == ["next:0", "place", "show"] and not seam_problems(gs, links),
               f"{fit['dashed']} {fit['caption']} {sorted(links)} {seam_problems(gs, links)}")
        chrome.tap(".seam button[data-after='place']")
        settle(0.3)
        gs, links = groups(), seams()
        g = gs[0]
        _check("a dead heat of three: one card headed WIN · PLACE · SHOW — DEAD HEAT, two horses side by side and the third under them, one "
               "UNTIE, no link under it (a dead heat is two or three horses), the 4TH card's link stays",
               order(gs) == [["win", "place", "show"], ["next:0"]] and g["title"] == "WIN · PLACE · SHOW — DEAD HEAT"
               and abs(g["cells"][0]["card"]["top"] - g["cells"][1]["card"]["top"]) < 1
               and g["cells"][2]["card"]["top"] >= max(g["cells"][0]["card"]["bottom"], g["cells"][1]["card"]["bottom"]) - 1
               and sorted(links) == ["next:0"] and not problems(gs) and not seam_problems(gs, links), f"{g['title']!r} {problems(gs)} {sorted(links)}")
        chrome.tap(".group-untie")
        settle(0.3)
        _check("UNTIE: the whole dead heat is undone, four cards again",
               order(groups()) == [["win"], ["place"], ["show"], ["next:0"]] and chrome.eval("resultsState.tied.length") == 0)
        chrome.tap(".seam button[data-after='show']")
        settle(0.3)
        gs = groups()
        _check("a dead heat for third: SHOW and the 4TH card are one card, headed SHOW · 4TH — DEAD HEAT",
               order(gs) == [["win"], ["place"], ["show", "next:0"]] and gs[2]["title"] == "SHOW · 4TH — DEAD HEAT" and not problems(gs),
               f"{gs[2]['title']!r} {problems(gs)}")
        # a dead heat for third with no 4TH card yet: the link under SHOW makes the card, merged, and the next tap fills it
        chrome.tap("#results-reset-btn")
        settle(0.4)
        for horse in (19, 5, 3):
            picker.tap_horse(horse)
            settle(0.3)
        _check("three horses with bets: no 4TH card, a link under SHOW all the same",
               order(groups()) == [["win"], ["place"], ["show"]] and "show" in seams(), str(order(groups())))
        chrome.tap(".seam button[data-after='show']")
        settle(0.3)
        gs = groups()
        _check("the link under SHOW makes a 4TH card tied to it, empty and lit, CONFIRM RESULTS waiting for it",
               order(gs) == [["win"], ["place"], ["show", "next:0"]] and not gs[2]["cells"][1]["filled"]
               and chrome.eval("activeSlot()") == "next:0" and chrome.eval("getComputedStyle(document.getElementById('results-confirm-btn')).display") == "none",
               f"{order(gs)} {chrome.eval('activeSlot()')}")
        picker.tap_horse(1)
        settle(0.3)
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("!document.getElementById('results-modal').classList.contains('active')", timeout=10)
        saved = rig.board.results()
        _check("1 fills it and the record carries the tie: 3 and 1 dead heat for third", saved == {"win": 19, "place": 5, "show": 3, "next": [1], "ties": [[3, 1]]}, str(saved))
        rig.post("/api/results/clear")
        chrome.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=15)
        picker.open()
        # the fit: 15 and 12 nobody bet, so a 4TH and a 5TH card ask for the next two finishers; WIN, PLACE and SHOW one card
        chrome.tap("#results-reset-btn")
        settle(0.4)
        for horse in (15, 12, 19, 5, 3):
            picker.tap_horse(horse)
            settle(0.3)
        chrome.tap(".seam button[data-after='win']")
        settle(0.3)
        chrome.tap(".seam button[data-after='place']")
        settle(0.4)
        gs, links = groups(), seams()
        fit = json.loads(chrome.eval(FIT_JS))
        _check("a three-way dead heat of 15 EMERGING MARKET (NO BETS), 12 CHIEF WALLABEE (NO BETS) and 19 GOLDEN TEMPO, 5 RIGHT TO PARTY 4TH, "
               "3 GRAND MO THE FIRST 5TH: one card and two, every name whole, the links clear of them",
               order(gs) == [["win", "place", "show"], ["next:0"], ["next:1"]] and not problems(gs) and not seam_problems(gs, links),
               f"{order(gs)} {problems(gs)} {seam_problems(gs, links)}")
        _check("1180 x 820 with all of it showing (the dead heat, the 4TH and 5TH cards, the note for the two, CONFIRM RESULTS): the modal does not "
               "scroll, the column and the button are inside the screen",
               fit["viewport"] == [1180, 820] and fit["scroll"][0] <= fit["scroll"][1] and fit["sidebar"][0] >= 0 and fit["sidebar"][1] <= 820
               and 0 <= fit["button"][0] and fit["button"][1] <= 820
               and fit["note"] == "#15, #12: no bets at the post. La Quiniela skips them; La Subasta still pays their owners.", str(fit))
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("!document.getElementById('results-modal').classList.contains('active')", timeout=10)
        saved = rig.board.results()
        _check("confirmed: the three-way dead heat is one tie in the record",
               saved == {"win": 15, "place": 12, "show": 19, "next": [5, 3], "ties": [[15, 12, 19]]}, str(saved))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        picker.close()
        server.close()


def test_slot_cards_have_a_place_for_everything_in_a_browser():
    """Joey, 2026-10-23 and 2026-10-26: what a card shows and where, a dead heat as one card, the seam's link, the note, and the
    whole column at 1180 x 820. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("SET WINNERS card checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _picker_cards(picker_check, requests, slow))


def _admin_after_the_post(picker_check, requests, slow):
    """The LQ admin page with #21 drawn in for #19: its lists in program-number order; the Scratches row with the number list
    and name field until AT THE GATE, and from AT THE GATE on only the No replacement box, ticked; its step and its notes
    say the tokens are refunded; Undo as ever; back to FINAL CALL the number list and name field return."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 19, "replacement": {"number": 21, "name": "Late Entry"}})
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True, size=(1366, 1024))
    row = lambda n: f'#scratch-list .horse[data-horse="{n}"] '            # noqa: E731
    who = "#9 " + DERBY[8].upper()

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def controls(n):                                  # what row n offers, as the page shows it
        return json.loads(chrome.eval("""(() => { const row = document.querySelector('#scratch-list .horse[data-horse="%d"]');
            const shown = (el) => !!el && getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
            const box = row.querySelector('[data-norepl]');
            return JSON.stringify({num: shown(row.querySelector('[data-repl-num]')), name: shown(row.querySelector('[data-repl-name]')),
                                   scratch: shown(row.querySelector('[data-scratch]')), box: shown(box),
                                   ticked: !!box && box.checked, locked: !!box && box.disabled,
                                   label: box ? box.parentElement.innerText.trim() : ''}); })()""" % n))

    def step():
        return chrome.eval("(() => { const el = document.querySelector('" + row(9) + "[data-rowstatus]'); return el ? el.innerText.trim() : ''; })()")

    def wait_state(n):
        rig.post("/api/quiniela/cmd", {"cmd": f"state {n}", "reopen": True})
        chrome.wait_for(f"document.getElementById('state-now').textContent.trim().startsWith('{n} ')", timeout=12)
        settle(0.3)

    try:
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && !!document.querySelector('" + row(9) + "[data-scratch]')", timeout=20)
        settle(0.4)
        horses = chrome.eval("[...document.querySelectorAll('#horses .hrow')].map((r) => Number(r.dataset.horse))")
        field_rows = chrome.eval("[...document.querySelectorAll('#scratch-list .horse')].map((r) => Number(r.dataset.horse))")
        scratched_rows = chrome.eval("[...document.querySelectorAll('#scratched-list .horse')].map((r) => Number(r.dataset.horse))")
        _check("#21 drawn in for #19: Horses lists 1 to 24 in order, the Scratches field rows 1-18, 20, 21, the Scratched row 19",
               horses == list(range(1, 25)) and field_rows == list(range(1, 19)) + [20, 21] and scratched_rows == [19],
               f"{horses} {field_rows} {scratched_rows}")
        c = controls(9)
        _check("before the gate (PRE-RACE): the number list, the name field, Scratch and the unticked box",
               c["num"] and c["name"] and c["scratch"] and c["box"] and not c["ticked"] and not c["locked"], str(c))
        wait_state(3)
        c = controls(9)
        _check("AT THE POST: no number list, no name field; Scratch and the No replacement box, ticked, nothing to untick, "
               "saying the tokens are refunded in cash",
               not c["num"] and not c["name"] and c["scratch"] and c["box"] and c["ticked"] and c["locked"]
               and "refunded in cash" in c["label"] and "re-bet" not in c["label"], str(c))
        chrome.tap(row(9) + "[data-scratch]")
        settle(0.4)
        _check(f"Scratch asks at once, no question about a name: Scratch {who}, no replacement, its tokens refunded in cash",
               step().startswith(f"Scratch {who}, no replacement: its tokens are refunded in cash at token value.")
               and "re-bet" not in step(), repr(step()))
        t = time.monotonic()
        chrome.tap(row(9) + "[data-confirm]")
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"9\"]')", timeout=10)
        settle(0.4)
        note = chrome.eval("document.querySelector('#scratched-list .horse[data-horse=\"9\"] .note').innerText")
        _check("Confirm: 9 scratched with no replacement, the reply line and the Scratched row both say the tokens are refunded",
               rig.board.store.scratches().get(9, 0) is None
               and next(s for s in rig.model()["scratches"] if s["was"]["number"] == 9)["refund"] is True
               and "tokens refunded in cash" in note
               and chrome.eval("document.querySelector('#scratched-list .horse[data-horse=\"9\"] [data-rowstatus]').innerText.trim()")
               .endswith("\u00b7 tokens refunded"), repr(note))
        chrome.tap('#scratched-list .horse[data-horse="9"] [data-undo]')
        chrome.wait_for("!!document.querySelector('" + row(9) + "[data-scratch]')", timeout=10)
        _check("Undo as ever", 9 not in rig.board.store.scratches())
        wait_state(2)
        c = controls(9)
        _check("back to FINAL CALL (betting reopened): the number list and the name field return, the box is the host's again",
               c["num"] and c["name"] and c["box"] and not c["ticked"] and not c["locked"] and "re-bet" in c["label"], str(c))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        server.close()


def _reset_confirm(picker_check, requests, slow):
    """The admin page's Reset betting asks before it clears, and says what: the results, the figures at the post and the
    hand count while they are held (Joey, 2026-10-28, review Q1); with nothing held it names neither."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    _cups_at_post(rig, {n: 5 for n in range(1, 18)})                              # 85 tokens: pot $85 at the post
    rig.client.put("/api/quiniela/counted_pot", json={"amount": 80})
    rig.post("/api/quiniela/cmd", {"cmd": "state 6"})
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True, size=(1366, 1024))
    try:
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && document.getElementById('state-now').textContent.trim().startsWith('6 ')", timeout=20)
        chrome.eval("window.__confirms = []; window.confirm = (m) => { window.__confirms.push(m); return false; }; 1")
        chrome.tap("#reset-betting")
        time.sleep(0.5)
        asked = chrome.eval("window.__confirms")
        _check("AFTER PARTY with the figures and a count held: Reset betting asks, naming the results, the figures at the post and the hand count",
               len(asked) == 1 and "This clears the results, the figures at the post (pot $85), the hand count ($80) and the ticker."
               in asked[0], str(asked))
        _check("...and Cancel clears nothing", rig.model()["closing"] is not None and rig.model()["pot_counted"] == 80 and rig.bridge.phase == 6)
        chrome.eval("window.confirm = (m) => { window.__confirms.push(m); return true; }; 1")
        chrome.tap("#reset-betting")
        chrome.wait_for("document.getElementById('state-now').textContent.trim().startsWith('0 ')", timeout=15)
        _check("OK clears them: PRE-RACE, no figures, no count", rig.model()["closing"] is None and rig.model()["pot_counted"] is None)
        chrome.eval("window.__confirms = []; 1")
        chrome.tap("#reset-betting")
        time.sleep(0.5)
        asked = chrome.eval("window.__confirms")
        _check("with nothing held it names no figures and no count",
               len(asked) == 1 and "This clears the results and the ticker." in asked[0], str(asked))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        server.close()


def test_reset_betting_names_what_it_clears_in_a_browser():
    """Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("Reset betting confirm checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _reset_confirm(picker_check, requests, slow))


def test_admin_scratch_after_the_post_in_a_browser():
    """Joey, 2026-10-23: from AT THE GATE on a scratch has no replacement; the LQ admin page's Scratches row offers only
    No replacement, ticked, and says the tokens are refunded; its field lists run in program-number order. Skipped where
    there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("admin page after-the-post checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _admin_after_the_post(picker_check, requests, slow))


# -----------------------------------------------------------------------------
# The weather, for the TV's crawl
# -----------------------------------------------------------------------------

def test_weather_reaches_the_board():
    """main.py feeds pi5's weather (the dashboard's source) into La
    Quiniela's model for the TV's crawl; without a key or with nothing
    fetched there is none, and nothing errors."""
    rig = Rig()
    payload = {"success": True, "current": {"temp_f": 88.2, "condition": {"text": "Sunny"}}, "location": "Dallas",
               "hourly": [], "cached": False}
    _check("the payload as the crawl wants it", main.weather_for_board(payload)
           == {"location": "Dallas", "temp_f": 88.2, "condition": "Sunny"})
    _check("no key, a failed fetch, no current conditions: none",
           main.weather_for_board({"success": False, "error": "Weather API key not configured"}) is None
           and main.weather_for_board({"success": True, "current": {}}) is None and main.weather_for_board(None) is None)
    import threading as _t
    import time as _time
    saved = main.weather_data
    main.weather_data = lambda: (payload, 200)
    stop = _t.Event()
    try:
        feed = _t.Thread(target=main.feed_weather_to_board, args=(stop,), daemon=True)
        feed.start()
        deadline = _time.time() + 5
        while rig.board.model().get("weather") is None and _time.time() < deadline:
            rig.board.refresh()
            _time.sleep(0.05)
        _check("the feed puts it in the model", rig.board.model()["weather"] == {"location": "Dallas", "temp_f": 88,
                                                                                  "condition": "Sunny"},
               str(rig.board.model().get("weather")))
    finally:
        stop.set()
        main.weather_data = saved
    r = rig.client.get("/api/weather")
    _check("GET /api/weather still answers (503 without a key, as before)", r.status_code in (200, 503)
           and "success" in r.get_json())


# -----------------------------------------------------------------------------
# The SET WINNERS picker: a tap fills the slot the host meant
# -----------------------------------------------------------------------------
# Joey, 2026-10-02: he picked WIN, tapped a horse for PLACE and it landed in WIN,
# overwriting the first. selectCup used to read "which slot is next" from
# resultsState.step, wrote the pick and moved step on only after awaiting the LED
# lock for it, so a second tap inside that answer (a slow or unreachable LED
# controller: 5 s) read the old step and filled WIN again. The picker is now a state
# machine only taps move (activeSlot()), and the LED calls follow it.

def _js_function(js, signature):
    """The body of the JS function that starts with `signature`, braces matched."""
    start = js.index(signature)
    open_at = js.index("{", start)
    depth = 0
    for k in range(open_at, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[start:k + 1]
    raise AssertionError("unbalanced braces after " + signature)


def test_picker_is_a_state_machine_only_taps_move():
    rig = Rig()
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    html = rig.client.get("/", base_url="http://joeydevpi.local:5000").get_data(as_text=True)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    region = js[js.index("// Results modal state"):js.index("// Saddle cloth colors and text colors")]
    pick = _js_function(js, "function selectCup(post, horse)")
    _check("a pick is decided and written in one turn: selectCup is not async, awaits nothing and does no network call",
           "async function selectCup" not in js and "await" not in pick and "fetch(" not in pick)
    for name in ("function chooseSlot(slot)", "function clearSlot(slot)", "function updateResultsModalUI()", "function activeSlot()"):
        body = _js_function(js, name)
        _check(f"{name.split('(')[0].replace('function ', '')} reads and writes the picker's state without waiting on anything",
               "await" not in body and "fetch(" not in body)
    active = _js_function(js, "function activeSlot()")
    _check("the slot a tap fills is the chosen one, else the first empty in order WIN, PLACE, SHOW, else none",
           "resultsState.chosen" in active and "keys.find" in active and "allSlotKeys()" in active and "|| null" in active
           and "const RESULT_SLOTS = ['win', 'place', 'show'];" in js)
    _check("the state has no 'next step' left to go stale", "resultsState.step" not in js and "step: 'win'" not in js)
    _check("the LEDs follow the picks, one call after another, never ahead of them",
           "function ledCall(" in js and "ledChain.then(" in js and region.count("fetch(") == 2
           and "ledCall('/api/cup/lock'" in pick and "ledCall('/api/cup/unlock'" in pick)
    _check("a tap is one click: no touch, pointer or mouse-down handler in the picker",
           re.search(r"touchstart|touchend|pointerdown|pointerup|mousedown|mouseup", region) is None
           and "btn.onclick = () => selectCup(post, horse);" in js)
    _check("a horse is in one slot: refused with a message, not moved",
           "is already ${slotName(where)}" in pick and "resultsNote(" in pick)
    _check("the old go-back, which only unlocked a cup and stepped back, is gone", "resultsGoBack" not in js)
    _check("a stray tap beside the picker does not throw picks away; another device's results event waits while it is up",
           "event.target === resultsModal && !resultsPicksMade()" in js
           and "if (resultsModalOpen()) {" in _js_function(js, "function connectResultsStream()"))
    _check("the confirm waits briefly for the LED calls in flight, then sets the results whatever they did",
           "await ledDrain(3000);" in js and "win: winHorse" in js and "cup: post" in js)
    # the page: the cards are made by one function, each tappable with its x; the note line, and over the button the note and the refusal
    card = _js_function(js, "function slotCard(key)")
    _check("a card per place, tappable, with its x, made by one function into #results-cards; the note line, the notes box and the refusal line are in the page",
           "card.onclick = () => chooseSlot(key);" in card and "clearSlot(key)" in card and 'id="results-cards"' in html
           and all(f'id="{i}"' in html for i in ("results-pick-note", "results-confirm-notes", "results-confirm-refusal")))
    _check("the active slot is lit, the slots' type is big, and taps are plain (touch-action: manipulation)",
           ".result-slot.is-active" in css and ".slot-clear" in css and ".winner-pick-tag" in css
           and css.count("touch-action: manipulation") >= 4)


# -----------------------------------------------------------------------------
# SET WINNERS: NO BETS on a horse whose cup held no bets at the post
# -----------------------------------------------------------------------------
# The host enters the three horses that pay, not strictly the first three: a
# finisher whose cup had no bets at the post cannot win a prize (an empty cup has
# no token to draw), so the next finisher takes its place. The picker marks such a
# horse from La Quiniela's figures at the post (the model's closing), by horse
# number, so the host does not have to spot it; unknown figures mark nothing.

def test_bets_at_the_post_for_the_picker():
    """What the picker reads: GET /api/quiniela's closing. Null before the post; from AT THE GATE every
    horse "1".."24" with the tokens its cup held then, keyed by horse number (22 running for 9 is "22"),
    0 for a horse no cup claims; frozen, so a token dropped later does not move it; gone when betting
    reopens."""
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.post("/api/quiniela/mode", {"mode": "BETTING_60"})
    rig.bridge.handle_raw_line(telem(MAC_A, horse=22, count=3))           # the cup that was 9 reports 22
    rig.bridge.handle_raw_line(telem(MAC_B, horse=4, count=0))            # a cup with nothing in it
    rig.bridge.handle_raw_line(telem("A0:B7:65:12:34:58", horse=12, count=5))
    rig.board.refresh()
    m = rig.model()
    _check("betting open: no figures at the post (the picker marks nothing then)", m["closing"] is None
           and m["horses"]["22"]["tokens"] == 3, str(m["closing"]))
    rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})
    closing = rig.model()["closing"]
    horses = (closing or {}).get("horses") or {}
    _check("AT THE GATE: the figures at the post carry every horse 1-24 by its number",
           sorted(horses, key=int) == [str(n) for n in range(1, 25)], str(sorted(horses)))
    _check("...22, running from post 9, under 22 with its 3; 12 with 5", horses["22"]["tokens"] == 3 and horses["12"]["tokens"] == 5)
    _check("...the empty cup's horse 0, a horse with no cup 0, and 9 (no cup claims it now) 0",
           (horses["4"]["tokens"], horses["7"]["tokens"], horses["9"]["tokens"]) == (0, 0, 0))
    post9 = rig.client.get("/api/quiniela/field").get_json()["posts"][8]
    _check("...and the picker's post 9 is horse 22, the key it reads", (post9["post"], post9["horse"]) == (9, 22), str(post9))
    rig.bridge.handle_raw_line(telem(MAC_B, horse=4, count=2))            # a token after the post
    rig.board.refresh()
    m = rig.model()
    _check("a token dropped after the post: the live count moves, the figures at the post do not",
           m["horses"]["4"]["tokens"] == 2 and m["closing"]["horses"]["4"]["tokens"] == 0)
    r = rig.post("/api/quiniela/mode", {"mode": "BETTING_60"})
    _check("betting reopened without the host's yes: refused, the figures at the post stay", r.status_code == 409 and rig.model()["closing"] is not None)
    rig.post("/api/quiniela/mode", {"mode": "BETTING_60", "reopen": True})
    _check("betting reopened: the figures at the post are gone", rig.model()["closing"] is None)


def test_picker_marks_horses_nobody_bet():
    rig = Rig()
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    load = _js_function(js, "async function loadBetsAtPost()")
    _check("the picker reads La Quiniela's figures at the post from its model (closing), beside the field",
           "fetch('/api/quiniela', { cache: 'no-store' })" in load and "model.closing" in load
           and js.index("async function loadQuinielaField()") < js.index("async function loadBetsAtPost()") < js.index("// Results modal state"))
    bets = _js_function(js, "function betsAtPost(closing)")
    _check("...by horse number; a horse that is missing or held nothing counts 0; no figures at all is null",
           "closing && closing.horses" in bets and "horses[String(n)]" in bets and "return null;" in bets
           and "tokens > 0 ? tokens : 0" in bets)
    _check("...and a failed read is no figures too, said nowhere but the console",
           "return null;" in load[load.index("catch"):] and "showNotification" not in load)
    _check("unknown is not zero: with no figures at the post no horse is marked",
           "resultsBets !== null &&" in _js_function(js, "function hadNoBets(horse)"))
    open_ = _js_function(js, "async function showResultsModal()")
    _check("the figures are read as the picker opens, with the field and the saved result, and only there",
           "Promise.all([loadQuinielaField(), loadBetsAtPost(), savedResultsOnPi5()])" in open_ and "resultsBets = bets;" in open_)
    at = js.index("async function showResultsModal()")
    writes = [w.start() for w in re.finditer(r"\bresultsBets\s*=[^=]", js)]
    _check("...nothing else writes them (the declaration aside), so the marks cannot change while the picker is open",
           len(writes) == 2 and "let resultsBets = null;" in js and sum(1 for w in writes if at <= w < at + len(open_)) == 1,
           str(writes))
    for name in ("async function loadQuinielaField()", "async function pollRaceMode()", "async function followServerResults()",
                 "function connectResultsStream()", "function selectCup(post, horse)", "async function resultsConfirm()"):
        _check(f"{name.split('(')[0].split()[-1]} does not touch the figures at the post", "resultsBets" not in _js_function(js, name))
    ui = _js_function(js, "function updateResultsModalUI()")
    _check("every picker is marked from them by its horse: dimmed (no-bets) and tagged NO BETS",
           "const noBets = hadNoBets(horse);" in ui and "btn.classList.toggle('no-bets', noBets);" in ui
           and "noBets ? 'NO BETS' : ''" in ui and "none.className = 'winner-pick-nobets';" in js)
    _check("a pick nobody bet is said once over CONFIRM RESULTS, word for word: the real finish, La Quiniela skips it, La Subasta still pays",
           "`${skipped.join(', ')}: no bets at the post. La Quiniela skips ${many ? 'them' : 'it'}; La Subasta still pays "
           "${many ? 'their owners' : 'its owner'}.`" in ui and "getElementById('results-confirm-notes').textContent" in ui)
    _check("...and its slot card shows the tag", "if (hadNoBets(horseNum))" in _js_function(js, "function updateSlot(slot, horseNum)")
           and "none.className = 'slot-nobets';" in js)
    _check("picking it is allowed and CONFIRM RESULTS stays enabled: nothing refuses or disables on NO BETS",
           "hadNoBets" not in _js_function(js, "function selectCup(post, horse)") and "hadNoBets" not in _js_function(js, "async function resultsConfirm()")
           and ".disabled" not in ui and "results-confirm-btn').disabled" not in js)
    _check("the confirm step appearing brings CONFIRM RESULTS into view (a short screen scrolls the modal)",
           "complete && !wasShown" in ui and "confirmBtn.scrollIntoView({ block: 'nearest' });" in ui)
    tag = re.search(r"\n\.winner-pick-nobets \{(.*?)\}", css, re.S)
    _check("the tag: red, 14 px, its own corner (top left; a slot's tag is top right)",
           bool(tag) and all(s in tag.group(1) for s in ("background: #D32F2F;", "font-size: 14px;", "left: 6px;")))
    _check("the picker dimmed but not its tag, the slot card's tag, the warning, a modal that scrolls when taller than the screen",
           ".winner-pick-btn.no-bets .winner-pick-name" in css and ".winner-pick-btn.no-bets .winner-pick-nobets" in css
           and ".slot-nobets {" in css and ".confirm-notes {" in css
           and re.search(r"#results-modal\.active \{[^}]*overflow-y: auto;", css) is not None
           and "#results-modal.active .results-modal-content {\n    margin: auto;" in css.replace("\r\n", "\n"))


# Joey, 2026-10-05 (DevPi): with all three slots filled the modal showed the picks three times (the grid, the slot
# cards, and a small list over CONFIRM RESULTS whose names broke mid-word, COMMANDMEN / T), and the right column was
# crowded. The slot cards are the check; the list is gone, the NO BETS warnings and CONFIRM RESULTS stay.

def test_confirm_step_lists_no_picks():
    rig = Rig()
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True).replace("\r\n", "\n")
    html = rig.client.get("/").get_data(as_text=True)
    _check("nothing builds a list of the picks over CONFIRM RESULTS any more (script, page, stylesheet)",
           not any(s in text for s in ("confirm-row", "confirm-summary") for text in (js, css, html)))
    section = html[html.index('id="results-confirm-section"'):]
    section = section[:section.index("</button>")]
    _check("the confirm step holds the note, the refusal line and CONFIRM RESULTS, nothing else",
           re.findall(r'id="([^"]+)"', section) == ["results-confirm-section", "results-confirm-notes", "results-confirm-refusal", "results-confirm-btn"],
           str(re.findall(r'id="([^"]+)"', section)))
    ui = _js_function(js, "function updateResultsModalUI()")
    _check("...the note filled with its one sentence, the refusal line with pi5's reason, nothing built into them",
           "getElementById('results-confirm-notes')" in ui and "getElementById('results-confirm-refusal')" in ui and "appendChild(" not in ui)
    _check("an empty note or refusal line takes no room; CONFIRM RESULTS sits under the last card, no divider",
           ".confirm-notes:empty,\n.confirm-refusal:empty {\n    display: none;\n}" in css
           and ".results-modal-named .confirm-section-sidebar {\n    margin-top: 0;\n    padding-top: 0;\n    border-top: 0;\n}" in css)
    name_rule = re.search(r"\n\.slot-horse-name \{(.*?)\}", css, re.S).group(1)
    note_rule = re.search(r"\n\.confirm-notes \{(.*?)\}", css, re.S).group(1)
    _check("a card's name and the note wrap only between words",
           "overflow-wrap: normal;" in name_rule and "anywhere" not in name_rule and "overflow-wrap: normal;" in note_rule
           and "word-break" not in name_rule + note_rule)
    fit = _js_function(js, "function fitSlotName(el)")
    _check("...and a word too wide for the card shrinks, a pixel at a time, never below 12 px",
           ".forEach(fitSlotName)" in _js_function(js, "function renderResultCards()")
           and "el.scrollWidth > room + 0.5" in fit and "el.parentElement.clientWidth" in fit and "size -= 1;" in fit
           and "const SLOT_NAME_MIN_PX = 12;" in js)


def _picker_check():
    """pi5/tools/picker_check.py, the headless Chrome harness (its Server and Chrome serve any page of the app)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("picker_check", os.path.join(_PI5_DIR, "tools", "picker_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -----------------------------------------------------------------------------
# The LQ admin page shows the race state, read-only (build-order item 5)
# -----------------------------------------------------------------------------
# The race state changes in one place, the Control Center, whose mode buttons run the LEDs with it; the
# admin page's seven state buttons set the state alone, so the TV and the cups could disagree with the
# mantle. They are gone; the page shows the state from the model it polls, with a link to the Control
# Center. The state-only route stays (tests, the simulator, a curl).

ADMIN_STATE_NAMES = ["PRE-RACE", "BETTING OPEN", "FINAL CALL", "AT THE POST", "RUNNING", "WINNER", "AFTER PARTY"]


def test_admin_state_line_in_a_browser():
    """The admin page in headless Chrome at an iPad's size: the state line follows the model for every state
    0-6 (set through the state-only route, which still works), the link goes to the Control Center, there are
    no state buttons, and a model that cannot be read is said, never shown as the last state. Skipped where
    there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("admin page browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    rig = Rig()
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True)
    live = ("(() => { const e = document.getElementById('state-now');"
            " return e && !e.classList.contains('unknown') ? e.textContent : ''; })()")
    try:
        seen, answers = [], []
        for n in range(7):
            r = rig.post("/api/quiniela/cmd", {"cmd": f"state {n}"})
            answers.append(r.status_code == 200 and r.get_json()["phase"] == n)
            chrome.call("Page.navigate", url=server.url + "quiniela/admin")
            chrome.wait_for("document.readyState === 'complete'", timeout=20)
            seen.append(chrome.wait_for(live, timeout=15))
        _check("the state-only route still sets each state 0-6", all(answers), str(answers))
        _check("the state line shows the model's state, its number and name, for each of 0-6",
               seen == [f"{n} · {name}" for n, name in enumerate(ADMIN_STATE_NAMES)], str(seen))
        page = chrome.eval("({buttons: document.querySelectorAll('[data-state], #states').length,"
                           " href: document.getElementById('state-link').getAttribute('href'),"
                           " text: document.getElementById('state-link').textContent})")
        _check("no state buttons; a link to the Control Center (/)",
               page == {"buttons": 0, "href": "/", "text": "Change the race state on the Control Center"}, str(page))
        rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})
        _check("a Control Center button's state reaches the line on the page's own poll",
               chrome.wait_for(live + " === '3 · AT THE POST'", timeout=12) is True)
        stale = ("(() => { const e = document.getElementById('state-now');"
                 " return e.classList.contains('unknown') ? e.textContent : ''; })()")
        server.fail.add("/api/quiniela")
        why = chrome.wait_for(stale, timeout=12)
        _check("pi5 answering an error: the line says so on the next poll, not the last state",
               why.startswith("pi5: ") and "AT THE POST" not in why, why)
        server.fail.discard("/api/quiniela")
        chrome.wait_for(live, timeout=12)
        server.close()
        server = None
        _check("pi5 gone: the line says it cannot reach pi5", chrome.wait_for(stale, timeout=12) == "cannot reach pi5")
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        if server is not None:
            server.close()


def test_picker_in_a_browser():
    """The picker in headless Chrome, over the DevTools protocol (pi5/tools/picker_check.py): the real
    dashboard over loopback, the LED controller slow on purpose, a mouse session and a touch session
    (an iPad's 1180 x 820, touch emulation), and the NO BETS marks from figures at the post set up on the
    server. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("picker browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    rig = Rig()
    for name, passed, detail in picker_check.run_checks(rig, quick=True):
        _check("picker: " + name, passed, detail)


# -----------------------------------------------------------------------------
# Race night on an iPad in landscape: taps big enough, and a Home Screen shortcut
# -----------------------------------------------------------------------------
# Joey runs the night from an iPad held in landscape, the two pages open as two Safari tabs. Everything the host
# presses takes a finger, 44 px or more each way (AT THE GATE and THEY'RE OFF! were 38 px tall and 6 px apart), at
# an iPad Air's 1180 x 820 and the floor's 1024 x 768. Added to the Home Screen, either page opens full screen (the
# web-app metas, no manifest), and there the links between the two open in place: full screen has no tabs.
# Headless Chrome is not iPadOS: it forces navigator.standalone (or the display-mode query) to see the links do
# that; what the iPad makes of the shortcut is checked on the iPad.

HOME_SCREEN_TITLES = (("/", "DDM"), ("/quiniela/admin", "LQ Admin"))


def test_home_screen_metas():
    """Both pages carry the metas a Home Screen shortcut reads: full screen, a black status bar, and each its own
    title so the two icons are told apart; and the viewport still lets a pinch zoom."""
    rig = Rig()
    for path, title in HOME_SCREEN_TITLES:
        head = rig.client.get(path).get_data(as_text=True).split("</head>")[0]
        metas = dict(re.findall(r'<meta name="([^"]+)" content="([^"]*)"\s*/?>', head))
        want = {"apple-mobile-web-app-capable": "yes", "mobile-web-app-capable": "yes",
                "apple-mobile-web-app-status-bar-style": "black", "apple-mobile-web-app-title": title}
        got = {k: metas.get(k) for k in want}
        _check(f"{path}: full screen from the Home Screen, a black status bar, titled {title}", got == want, str(got))
        viewport = metas.get("viewport", "")
        _check(f"{path}: the viewport still lets a pinch zoom",
               viewport.startswith("width=device-width") and "user-scalable" not in viewport
               and "maximum-scale" not in viewport, viewport)


def test_tap_targets_in_a_browser():
    """Everything the host presses on race night, in headless Chrome at 1180 x 820 and 1024 x 768 with touch
    emulation (pi5/tools/picker_check.py, tap_checks): the Control Center's main view, its drawer, SET WINNERS
    with three horses picked and REVEAL WINNERS; the admin page with the counted pot showing and a scratch to
    undo. Each control 44 x 44 px or more under a finger, touch-action manipulation; AT THE GATE and THEY'RE OFF!
    8 px or more from any other mode button; nothing scrolls sideways. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("tap target browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    rig = Rig()
    for name, passed, detail in picker_check.tap_checks(rig):
        _check("taps: " + name, passed, detail)


def test_full_screen_links_in_a_browser():
    """The links between the two pages (pi5/tools/picker_check.py, link_checks): in a browser tab as built, the
    Control Center's opening a new tab and the admin page's its named tab, reused by the next tap; with
    navigator.standalone forced true, or the display-mode query answering yes, no target, and a tap opens the
    other page in place. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("full screen link browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    rig = Rig()
    for name, passed, detail in picker_check.link_checks(rig):
        _check("links: " + name, passed, detail)


# -----------------------------------------------------------------------------
# The LQ admin page: one tap, one request (Joey on DevPi, 2026-10-06)
# -----------------------------------------------------------------------------
# A scratch drew its reply twice (into the horse's row and the section's line, one under the other once the
# row had moved to the Scratched list), which looked like a tap sent twice. Every action's buttons are now held
# while its request is in flight, the scratch rows are not rebuilt under a Scratch or Undo in flight, and after
# one that went through every row's replacement fields are back to their defaults.

ADMIN_ROWS_JS = """JSON.stringify([...document.querySelectorAll('#scratch-list .horse')].map((row) => {
    const s = row.querySelector('[data-repl-num]'), r = row.querySelector('[data-repl-name]'), c = row.querySelector('[data-norepl]');
    return [Number(row.dataset.horse), s.value, s.options.length ? s.options[0].value : '', r.value, c.checked];
}))"""
ADMIN_LINES_JS = "[...document.querySelectorAll('.status')].filter((e) => e.textContent.trim() === %s).length"


def _admin_one_tap(picker_check, touch, requests, slow):
    """One session of test_admin_one_tap_one_request_in_a_browser."""
    mode = "[touch]" if touch else "[mouse]"
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={str(n): {"name": DERBY[n - 1]} for n in range(1, 21)})
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=touch, size=(1366, 1024))

    def count(since, method, path):
        return sum(1 for t, m, p in requests if t >= since and m == method and p == path)

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def double_tap(sel, gap=0.0):
        x, y = chrome.center(sel)
        chrome.tap_at(x, y)
        if gap:
            settle(gap)
        chrome.tap_at(x, y)                          # the same spot, as a finger's second tap

    def load():
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && "
                        "!!document.querySelector('#scratch-list .horse[data-horse=\"1\"] [data-scratch]')", timeout=20)
        chrome.eval("window.__confirms = 0; window.confirm = () => { window.__confirms++; return true; }; 1")
        settle(0.4)

    def rows_at_defaults():
        names = rig.client.get("/api/quiniela/horses").get_json()
        rows = json.loads(chrome.eval(ADMIN_ROWS_JS))
        bad = [r for r in rows if r[1] != r[2] or r[3] != (names[r[2]]["name"] if r[2] else "") or r[4]]
        return not bad and len(rows) > 0, str(bad[:3] or rows[:2])

    try:
        load()
        # Scratch #1 with #21 "Test", a double tap while pi5 takes its time
        chrome.eval("""(() => { const row = document.querySelector('#scratch-list .horse[data-horse="1"]');
            const s = row.querySelector('[data-repl-num]'); s.value = '21'; s.dispatchEvent(new Event('change', {bubbles: true}));
            row.querySelector('[data-repl-name]').value = 'Test'; return 1; })()""")
        slow[("POST", "/api/quiniela/scratch")] = 0.8
        chrome.tap('#scratch-list .horse[data-horse="1"] [data-scratch]')          # the confirm step (it sends nothing)
        chrome.wait_for("!!document.querySelector('#scratch-list .horse[data-horse=\"1\"] [data-confirm]')", timeout=5)
        t = time.monotonic()
        double_tap('#scratch-list .horse[data-horse="1"] [data-confirm]')
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"1\"]')", timeout=10)
        settle(0.6)
        _check(f"admin page {mode}: a double tap on Confirm scratch sends one request", count(t, "POST", "/api/quiniela/scratch") == 1,
               str(count(t, "POST", "/api/quiniela/scratch")))
        _check(f"admin page {mode}: ...one record in the model", len(rig.model()["scratches"]) == 1, str(rig.model()["scratches"]))
        reply = "Scratched 1 \u2192 #21 TEST"
        lines = chrome.eval(ADMIN_LINES_JS % json.dumps(reply))
        _check(f"admin page {mode}: ...one reply line, {reply!r}", lines == 1, f"{lines} lines")
        ok, detail = rows_at_defaults()
        _check(f"admin page {mode}: ...every row in the field back to its defaults (the first free number, its stored name)", ok, detail)
        # Undo #1, a double tap
        slow[("POST", "/api/quiniela/unscratch")] = 0.8
        t = time.monotonic()
        double_tap('#scratched-list .horse[data-horse="1"] [data-undo]')
        chrome.wait_for("!!document.querySelector('#scratch-list .horse[data-horse=\"1\"]')", timeout=10)
        settle(0.6)
        _check(f"admin page {mode}: a double tap on Undo sends one request (a second could undo another scratch)",
               count(t, "POST", "/api/quiniela/unscratch") == 1, str(count(t, "POST", "/api/quiniela/unscratch")))
        _check(f"admin page {mode}: ...no record left", rig.model()["scratches"] == [], str(rig.model()["scratches"]))
        lines = chrome.eval(ADMIN_LINES_JS % json.dumps("Undone 1"))
        _check(f"admin page {mode}: ...one reply line, 'Undone 1'", lines == 1, f"{lines} lines")
        ok, detail = rows_at_defaults()
        _check(f"admin page {mode}: ...every row back to its defaults, #1's and #2's the same (no pick left from before)", ok, detail)
        row1 = json.loads(chrome.eval(ADMIN_ROWS_JS))[0]
        line21 = chrome.eval("document.getElementById('names-text').value.split(String.fromCharCode(10))[20]")
        _check(f"admin page {mode}: ...and 21 has no name again: none stored, #1's row offers #21 with an empty name box, "
               "Horse names reads '21.'", rig.client.get("/api/quiniela/horses").get_json()["21"] == {"name": ""}
               and row1[:2] == [1, "21"] and row1[3] == "" and line21.strip() == "21.", f"{row1} {line21!r}")
        # A Scratch in flight across the 5 s refresh: pi5 has made the record, the refresh sees it, and the rows it
        # would draw put another horse's Scratch under the finger; the second tap must send nothing.
        polls = lambda: [t for t, m, p in requests if m == "GET" and p == "/api/quiniela"]     # noqa: E731
        seen = len(polls())
        chrome.wait_for("true", timeout=1)
        end = time.monotonic() + 7
        while len(polls()) == seen and time.monotonic() < end:
            settle(0.05)
        settle(4.0)                                       # the next refresh is a second away
        chrome.eval("""(() => { document.querySelector('#scratch-list .horse[data-horse="3"] [data-norepl]').checked = true; return 1; })()""")
        slow[("POST", "/api/quiniela/scratch")] = 2.6
        chrome.tap('#scratch-list .horse[data-horse="3"] [data-scratch]')
        chrome.wait_for("!!document.querySelector('#scratch-list .horse[data-horse=\"3\"] [data-confirm]')", timeout=5)
        t = time.monotonic()
        double_tap('#scratch-list .horse[data-horse="3"] [data-confirm]', gap=1.8)
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"3\"]')", timeout=10)
        settle(0.6)
        refreshed = [p for p in polls() if t < p < t + 2.6]
        _check(f"admin page {mode}: a Scratch in flight across the 5 s refresh is sent once, and nobody else is scratched",
               refreshed and count(t, "POST", "/api/quiniela/scratch") == 1 and len(rig.model()["scratches"]) == 1,
               f"{len(refreshed)} refreshes in flight, {count(t, 'POST', '/api/quiniela/scratch')} requests, {rig.model()['scratches']}")
        slow.clear()
        rig.post("/api/quiniela/unscratch", {"horse": 3})
        # Every other action, a double tap each while pi5 takes its time
        for key in (("GET", "/api/quiniela/horses"), ("PUT", "/api/quiniela/horses"), ("PUT", "/api/quiniela/race"),
                    ("PUT", "/api/quiniela/counted_pot"), ("POST", "/api/quiniela/reset")):
            slow[key] = 0.6
        load()
        actions = [("Names: Reload", "#names-reload", "GET", "/api/quiniela/horses"),
                   ("Names: Save names", "#names-save", "PUT", "/api/quiniela/horses"),
                   ("Race info: Save race info", "#race-save", "PUT", "/api/quiniela/race")]
        for name, sel, method, path in actions:
            t = time.monotonic()
            double_tap(sel)
            settle(1.0)
            _check(f"admin page {mode}: a double tap on {name} sends one request", count(t, method, path) == 1,
                   str(count(t, method, path)))
        rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})              # the counted pot shows from state 3
        load()
        chrome.wait_for("!document.getElementById('counted').hidden", timeout=15)
        chrome.eval("document.getElementById('counted-input').value = '150'; 1")
        for name, sel, method, path in (("Counted pot: Save count", "#counted-save", "PUT", "/api/quiniela/counted_pot"),
                                         ("Counted pot: Clear", "#counted-clear", "PUT", "/api/quiniela/counted_pot")):
            t = time.monotonic()
            double_tap(sel)
            settle(1.0)
            _check(f"admin page {mode}: a double tap on {name} sends one request", count(t, method, path) == 1,
                   str(count(t, method, path)))
        # Reset betting at the post (3): pi5 refuses it (OD13); the page asks nothing first and says why.
        t = time.monotonic()
        double_tap("#reset-betting")
        settle(1.0)
        said = chrome.eval("(() => { const e = document.getElementById('reset-status'); return [e.textContent, e.className]; })()")
        _check(f"admin page {mode}: Reset betting at the post: one request, no question, refused in red with the reason, the race where it was",
               count(t, "POST", "/api/quiniela/reset") == 1 and chrome.eval("window.__confirms") == 0
               and said == ["Reset betting works only before the race (0) or after it (6). Change the race state on the Control Center.",
                            "status err"] and rig.model()["race_state"] == 3 and rig.model()["closing"] is not None,
               str((count(t, "POST", "/api/quiniela/reset"), chrome.eval("window.__confirms"), said, rig.model()["race_state"])))
        # After the race (RESET: 6) it asks once and goes, a double tap one request.
        rig.post("/api/results/clear")
        load()
        chrome.wait_for("document.getElementById('state-now').textContent.indexOf('AFTER PARTY') >= 0", timeout=15)
        t = time.monotonic()
        double_tap("#reset-betting")
        settle(1.0)
        _check(f"admin page {mode}: Reset betting after the race: asked once, a double tap sends one request, back to 0",
               count(t, "POST", "/api/quiniela/reset") == 1 and chrome.eval("window.__confirms") == 1
               and rig.model()["race_state"] == 0,
               str((count(t, "POST", "/api/quiniela/reset"), chrome.eval("window.__confirms"), rig.model()["race_state"])))
        _check(f"admin page {mode}: no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        slow.clear()
        chrome.close()
        server.close()


def test_admin_one_tap_one_request_in_a_browser():
    """The LQ admin page in headless Chrome at a 12.9-inch iPad Pro's 1366 x 1024 (pi5/tools/picker_check.py's
    Server and Chrome), with touch and with a mouse, every request counted where pi5 receives it and the
    actions' replies held back a moment after the work is done, as a busy pi5's are: a double tap on Confirm
    scratch, Undo and every other action sends one request; a scratch makes one record and one reply line; a Scratch in
    flight across the 5 s refresh is sent once and scratches nobody else; after a Scratch and after an Undo
    every row's replacement fields are back to their defaults. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("admin page browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: [_admin_one_tap(picker_check, touch, requests, slow) for touch in (True, False)])


# -----------------------------------------------------------------------------
# The admin page's Scratch check (Joey, 2026-10-07)
# -----------------------------------------------------------------------------

ADMIN_ROW9 = '#scratch-list .horse[data-horse="9"] '
ADMIN_STEP_JS = """(() => { const el = document.querySelector(`#scratch-list .horse[data-horse="9"] [data-rowstatus]`);
    return el ? el.innerText.trim() : null; })()"""
ADMIN_STEP_BUTTONS_JS = """JSON.stringify([...document.querySelectorAll(`#scratch-list .horse[data-horse="9"] [data-rowstatus] button`)]
    .map((b) => { const r = b.getBoundingClientRect(); return [b.textContent.trim(), r.width, r.height]; }))"""
ADMIN_NOREPL_JS = "document.querySelector('" + ADMIN_ROW9 + "[data-norepl]').checked"
ADMIN_NAME_JS = "document.querySelector('" + ADMIN_ROW9 + "[data-repl-name]').value"


def _admin_scratch_check(picker_check, touch, requests, slow):
    """One session of test_admin_scratch_asks_first_in_a_browser."""
    mode = "[touch]" if touch else "[mouse]"
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={str(n): {"name": DERBY[n - 1]} for n in range(1, 21)})
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=touch, size=(1366, 1024))
    who = "#9 " + DERBY[8].upper()
    # Joey's rule (2026-10-14): with betting open (the model's betting_open: 60 MIN to FINAL CALL) a replacement's step
    # says to empty the cup and hand the tokens back. From AT THE GATE on (2026-10-23) there is no replacement at all:
    # test_admin_scratch_after_the_post_in_a_browser.
    new_line = "Betting is open: empty this cup and hand the tokens back to re-bet; #21 starts empty."

    def posts(since, path="/api/quiniela/scratch"):
        return sum(1 for t, m, p in requests if t >= since and m == "POST" and p == path)

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def step():
        return chrome.eval(ADMIN_STEP_JS)

    def tap(what):                                   # a control in #9's row
        chrome.tap(ADMIN_ROW9 + what)
        settle(0.4)

    def scratch():                                   # a tap on #9's Scratch, and what the row says then
        tap("[data-scratch]")
        return step() or ""

    def state(n):                                    # the race state, once the page's 5 s refresh has read it
        rig.post("/api/quiniela/cmd", {"cmd": f"state {n}", "reopen": True})
        chrome.wait_for(f"document.getElementById('state-now').textContent.trim().startsWith('{n} ')", timeout=12)

    try:
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && "
                        "!!document.querySelector('" + ADMIN_ROW9 + "[data-scratch]')", timeout=20)
        chrome.eval("window.__dialogs = 0; window.confirm = () => { window.__dialogs++; return true; }; "
                    "window.alert = () => { window.__dialogs++; }; 1")
        settle(0.4)
        t = time.monotonic()
        said = scratch()
        _check(f"admin page {mode}: Scratch with nothing set (No replacement unticked, #21 with no name) sends nothing and asks, in the row",
               posts(t) == 0 and said.startswith("No replacement? Tick No replacement, or enter the replacement's name."), repr(said))
        buttons = json.loads(chrome.eval(ADMIN_STEP_BUTTONS_JS))
        _check(f"admin page {mode}: ...with a No replacement button, 44 px or more each way; the box still unticked",
               [b[0] for b in buttons] == ["No replacement"] and min(buttons[0][1:]) >= 44 and chrome.eval(ADMIN_NOREPL_JS) is False,
               str(buttons))
        tap("[data-ask-norepl]")
        said = step() or ""
        buttons = json.loads(chrome.eval(ADMIN_STEP_BUTTONS_JS))
        _check(f"admin page {mode}: ...which ticks the box and goes on to the confirm step: Scratch {who}, no replacement",
               chrome.eval(ADMIN_NOREPL_JS) is True and said.startswith(f"Scratch {who}, no replacement: its tokens are handed back to re-bet.")
               and posts(t) == 0, repr(said))
        _check(f"admin page {mode}: ...Confirm scratch and Cancel, 44 px or more each way",
               [b[0] for b in buttons] == ["Confirm scratch", "Cancel"] and all(min(b[1:]) >= 44 for b in buttons), str(buttons))
        tap("[data-cancel]")
        _check(f"admin page {mode}: Cancel sends nothing, takes the step away and leaves the box ticked",
               posts(t) == 0 and step() == "" and chrome.eval(ADMIN_NOREPL_JS) is True, repr(step()))
        tap("[data-norepl]")                         # unticked again, and a name for #21
        tap("[data-repl-name]")
        chrome.call("Input.insertText", text="Test")
        settle(0.3)
        said = scratch()
        _check(f"admin page {mode}: with a name, the confirm step: Scratch {who}: #21 TEST draws in (no betting-open line before betting opens)",
               said.startswith(f"Scratch {who}: #21 TEST draws in.") and new_line not in said
               and posts(t) == 0, repr(said))
        tap("[data-repl-name]")
        chrome.call("Input.insertText", text="y")
        settle(0.3)
        _check(f"admin page {mode}: ...a change to the row takes the step away (it would no longer say what is sent)",
               step() == "" and chrome.eval(ADMIN_NAME_JS) == "Testy", repr(step()))
        chrome.eval("(() => { const r = document.querySelector('" + ADMIN_ROW9 + "[data-repl-name]'); r.value = 'Test'; "
                    "r.dispatchEvent(new Event('input', { bubbles: true })); return 1; })()")
        scratch()
        tap("[data-cancel]")
        _check(f"admin page {mode}: Cancel keeps the typed name and sends nothing",
               chrome.eval(ADMIN_NAME_JS) == "Test" and step() == "" and posts(t) == 0, chrome.eval(ADMIN_NAME_JS))
        lines = {}
        for n in (1, 2, 0):
            state(n)
            said = scratch()
            lines[n] = new_line in said
            tap("[data-cancel]")
        _check(f"admin page {mode}: with a replacement, the empty-the-cup line while betting is open (1, 2), not before it (0)",
               lines == {0: False, 1: True, 2: True}, str(lines))
        state(1)
        tap("[data-norepl]")
        said = scratch()
        _check(f"admin page {mode}: ...and with No replacement the no-replacement line as before, neither of the others",
               said.startswith(f"Scratch {who}, no replacement: its tokens are handed back to re-bet.")
               and new_line not in said, repr(said))
        tap("[data-cancel]")
        tap("[data-norepl]")
        said = scratch()
        _check(f"admin page {mode}: betting open, the replacement's confirm step: Scratch {who}: #21 TEST draws in. {new_line}",
               said.startswith(f"Scratch {who}: #21 TEST draws in. {new_line}") and posts(t) == 0, repr(said))
        slow[("POST", "/api/quiniela/scratch")] = 0.8
        t = time.monotonic()
        x, y = chrome.center(ADMIN_ROW9 + "[data-confirm]")
        chrome.tap_at(x, y)
        chrome.tap_at(x, y)                          # the same spot, as a finger's second tap
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"9\"]')", timeout=10)
        settle(0.6)
        slow.clear()
        reply = "Scratched 9 " + chr(0x2192) + " #21 TEST \u00b7 re-bet"
        _check(f"admin page {mode}: a double tap on Confirm scratch sends one request: one record, 9 -> 21 TEST, re-bet, one reply line",
               posts(t) == 1 and rig.model()["scratches"] == [{"was": {"number": 9, "name": DERBY[8].upper()}, "now": {"number": 21, "name": "TEST"},
                                                              "rebet": True, "refund": False}]
               and chrome.eval(ADMIN_LINES_JS % json.dumps(reply)) == 1, f"{posts(t)} requests, {rig.model()['scratches']}")
        row_note = chrome.eval("document.querySelector('#scratched-list .horse[data-horse=\"9\"] .note').innerText")
        _check(f"admin page {mode}: ...and its Scratched row says re-bet, so the host knows to empty that cup",
               row_note == "replaced by #21 TEST \u00b7 re-bet", repr(row_note))
        t = time.monotonic()
        chrome.tap('#scratched-list .horse[data-horse="9"] [data-undo]')
        chrome.wait_for("!!document.querySelector('" + ADMIN_ROW9 + "[data-scratch]')", timeout=10)
        settle(0.4)
        _check(f"admin page {mode}: Undo is unchanged: one tap, one request, no step",
               posts(t, "/api/quiniela/unscratch") == 1 and rig.model()["scratches"] == [] and step() == "Undone 9", repr(step()))
        tap("[data-norepl]")
        scratch()
        t = time.monotonic()
        tap("[data-confirm]")
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"9\"]')", timeout=10)
        _check(f"admin page {mode}: Confirm sends what the step said: 9 scratched with no replacement",
               posts(t) == 1 and rig.model()["scratches"] == [{"was": {"number": 9, "name": DERBY[8].upper()}, "now": None, "rebet": True, "refund": False}],
               str(rig.model()["scratches"]))
        _check(f"admin page {mode}: no confirm() or alert() dialog anywhere, no page errors",
               chrome.eval("window.__dialogs") == 0 and not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        slow.clear()
        chrome.close()
        server.close()


def _admin_gremlins_check(picker_check, touch, requests, slow):
    """One session of test_admin_number_list_and_typed_number_in_a_browser."""
    mode = "[touch]" if touch else "[mouse]"
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={str(n): {"name": DERBY[n - 1]} for n in range(1, 21)})
    rig.bridge.handle_raw_line(telem(MAC_A, horse=19, count=3))       # the bench cup, on 19
    rig.board.refresh()
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=touch, size=(1366, 1024))
    row = '#scratch-list .horse[data-horse="19"] '
    step_js = "(() => { const el = document.querySelector(`" + row + "[data-rowstatus]`); return el ? el.innerText.trim() : null; })()"

    def posts(since):
        return sum(1 for t, m, p in requests if t >= since and m == "POST" and p == "/api/quiniela/scratch")

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def picks():                                     # the numbers the in-field rows' lists show, as a set
        return json.loads(chrome.eval("JSON.stringify([...new Set([...document.querySelectorAll('#scratch-list .horse "
                                      "[data-repl-num]')].map((s) => s.value))])"))

    def wait_picks(value, timeout=13.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and picks() != [value]:
            time.sleep(0.2)
        return picks()

    def type_name(text):
        chrome.eval("(() => { document.querySelector('" + row + "[data-repl-name]').value = ''; return 1; })()")
        chrome.tap(row + "[data-repl-name]")
        chrome.call("Input.insertText", text=text)
        settle(0.3)

    def scratch():
        chrome.tap(row + "[data-scratch]")
        settle(0.4)
        return chrome.eval(step_js) or ""

    try:
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && !!document.querySelector('" + row + "[data-scratch]')", timeout=20)
        _check(f"admin lists {mode}: every in-field row offers #21, the first free number", picks() == ["21"], str(picks()))
        type_name("Test")
        scratch()
        chrome.tap(row + "[data-confirm]")
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"19\"] [data-undo]')", timeout=10)
        rig.bridge.handle_raw_line(telem(MAC_A, horse=21, count=3))   # the cup followed 19 -> 21
        rig.board.refresh()
        _check(f"admin lists {mode}: after Scratch 19 -> #21, every row offers #22", wait_picks("22") == ["22"], str(picks()))
        chrome.tap('#scratched-list .horse[data-horse="19"] [data-undo]')
        chrome.wait_for("!!document.querySelector('" + row + "[data-scratch]')", timeout=10)
        settle(0.5)                                  # the rows as the Undo left them: the cup still says 21
        rig.bridge.handle_raw_line(telem(MAC_A, horse=19, count=3))   # a report later it is back on 19
        rig.board.refresh()
        _check(f"admin lists {mode}: after the Undo, once the cup is back on 19, every row offers #21 again (it stayed on #22)",
               wait_picks("21") == ["21"], str(picks()))
        _check(f"admin lists {mode}: ...and 21's name went back to blank", rig.board.store.name_of(21) == "", rig.board.store.name_of(21))
        for typed in ("#21 Test", "21 Test", "21. Test", "21 - Test"):
            type_name(typed)
            said = scratch()
            _check(f"admin name {mode}: {typed!r} with #21 selected: the confirm step names it TEST",
                   said.startswith("Scratch #19 ") and ": #21 TEST draws in." in said, repr(said))
            chrome.tap(row + "[data-cancel]")
            settle(0.3)
        type_name("#22 Test")
        t = time.monotonic()
        said = scratch()
        _check(f"admin name {mode}: '#22 Test' with #21 selected is refused in the row, nothing sent",
               said == "You typed #22 but #21 is selected. Pick #22 in the list, or take it out of the name." and posts(t) == 0,
               repr(said))
        type_name("50 Cent")
        said = scratch()
        _check(f"admin name {mode}: a name that starts with a number no horse has is left as typed",
               ": #21 50 CENT draws in." in said, repr(said))
        chrome.tap(row + "[data-cancel]")
        type_name("#21 Test")
        scratch()
        t = time.monotonic()
        chrome.tap(row + "[data-confirm]")
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"19\"] [data-undo]')", timeout=10)
        _check(f"admin name {mode}: Confirm sends and stores Test, not #21 Test",
               posts(t) == 1 and rig.board.store.name_of(21) == "Test"
               and rig.model()["scratches"][0]["now"] == {"number": 21, "name": "TEST"}, str(rig.model()["scratches"]))
        _check(f"admin lists {mode}: no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        server.close()


def test_admin_number_list_and_typed_number_in_a_browser():
    """Two gremlins from Joey's DevPi check (2026-10-14, 4e66ab9), on the LQ admin page in headless Chrome at
    1366 x 1024, touch and mouse, requests counted. (1) After Scratch 19 -> #21 and Undo, every row's number list
    must come back to #21 once the cup is back on 19 (it stayed on #22: the rows were rebuilt while the cup still
    said 21, which makes 21 in use, and the next rebuild kept that default as if the host had picked it). (2) A
    replacement name typed as "#21 Test" (or 21 Test, 21. Test, 21 - Test) with #21 selected is Test, in the
    confirm step and the record; "#22 Test" with #21 selected is refused in the row and nothing is sent; a plain
    name, or one starting with a number no horse has, is untouched. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("admin page gremlins check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: [_admin_gremlins_check(picker_check, touch, requests, slow) for touch in (True, False)])


def test_pages_come_up_fresh():
    """Pages come up fresh after a pull (Joey, 2026-10-14; page_rev.py). Every HTML page pi5 serves (the Control
    Center, the LQ admin page, La Subasta's, the rest) answers Cache-Control: no-store, while the API keeps its own;
    every static URL on the Control Center and the admin page carries pi5's page revision; every response says it
    (X-Page-Rev). In headless Chrome over the DevTools protocol, the Control Center reloads itself off its 5 s poll
    when the revision changes, with the admin page's mechanism (static/js/page_rev.js), and not otherwise: not while
    the revision is the same, and not while a modal is open."""
    import page_rev
    rig = Rig()
    rev = page_rev.PAGE_REV
    _check("pi5's page revision: twelve hex digits over its templates and static JS and CSS, as they are",
           re.fullmatch(r"[0-9a-f]{12}", rev) is not None and page_rev.compute() == rev, rev)
    pages, not_fresh = [], []
    for rule in main.app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or rule.rule.startswith(("/api/", "/static", "/la-subasta/api/")):
            continue
        r = rig.client.get(rule.rule)
        if r.mimetype == "text/html":
            pages.append(rule.rule)
            if r.headers.get("Cache-Control") != "no-store" or r.headers.get("X-Page-Rev") != rev:
                not_fresh.append((rule.rule, r.headers.get("Cache-Control")))
    _check("every HTML page pi5 serves answers no-store and says the revision (the Control Center, the admin page, "
           "La Subasta's included)", not not_fresh and {"/", "/quiniela/admin", "/la-subasta/", "/la-subasta/admin"} <= set(pages),
           str((sorted(pages), not_fresh)))
    spectator, guest = rig.client.get("/spectator"), rig.client.get("/guest/spectator")
    _check("/spectator is the spectator TV page, the same as /guest/spectator (it rendered without its horses: a 500)",
           spectator.status_code == 200 and guest.status_code == 200 and "/spectator" in pages
           and spectator.get_data() == guest.get_data(), str((spectator.status_code, guest.status_code)))
    api = {path: rig.client.get(path) for path in ("/api/quiniela", "/api/quiniela/mode", "/api/results", "/api/quiniela/race")}
    _check("the API keeps its own caching: JSON is not made no-store (GET /api/quiniela was no-store already)",
           api["/api/quiniela"].headers.get("Cache-Control") == "no-store"
           and all(r.mimetype == "application/json" for r in api.values())
           and all(r.headers.get("Cache-Control") in (None, "no-store") for r in api.values())
           and api["/api/results"].headers.get("Cache-Control") is None,
           str({p: r.headers.get("Cache-Control") for p, r in api.items()}))
    for path in ("/", "/quiniela/admin"):
        page = rig.client.get(path).get_data(as_text=True)
        statics = re.findall(r'(?:src|href)="(/static/[^"?]+[.](?:js|css)[^"]*)"', page)
        _check(f"{path}: every static JS and CSS URL carries the revision (?v={rev}), and the page its meta",
               bool(statics) and all(("v=" + rev) in u for u in statics) and ('name="ddm-page-rev" content="%s"' % rev) in page,
               str(statics))
    picker_check = _picker_check()
    if not picker_check.available():
        _check("Control Center reload browser check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True, size=(1366, 1024))
    meta = "(document.querySelector('meta[name=\"ddm-page-rev\"]') || {}).content"
    try:
        chrome.call("Page.navigate", url=server.url)
        chrome.wait_for("document.readyState === 'complete' && !!window.DDMPageRev", timeout=20)
        chrome.eval("window.__before = 1; 1")
        time.sleep(7)
        _check("Control Center: the same revision, more than a poll: no reload", chrome.eval("window.__before") == 1)
        chrome.eval("document.getElementById('device-modal').classList.add('active'); 1")
        page_rev.PAGE_REV = "updated-cc01"
        time.sleep(7)
        _check("Control Center: another revision while a modal is open: it waits", chrome.eval("window.__before") == 1)
        chrome.eval("document.getElementById('device-modal').classList.remove('active'); 1")
        try:
            reloaded = chrome.wait_for("window.__before === undefined && document.readyState === 'complete' && (%s) === 'updated-cc01'"
                                       % meta, timeout=12)
        except TimeoutError:
            reloaded = False
        _check("Control Center: the modal closed, the next poll reloads it, with the new revision", reloaded is True)
    finally:
        page_rev.PAGE_REV = rev
        chrome.close()
        server.close()


def _with_counted_requests(run):
    """run(requests, slow) with every request pi5 receives counted, as (time, method, path), and the replies of the
    (method, path) keys in `slow` held back that many seconds after the work is done, as a busy pi5's are."""
    requests, slow = [], {}
    inner = main.app.wsgi_app

    def counting(environ, start_response):
        key = (environ["REQUEST_METHOD"], environ["PATH_INFO"])
        requests.append((time.monotonic(), key[0], key[1]))
        response = inner(environ, start_response)              # the work is done now, the reply waits
        if key in slow:
            time.sleep(slow[key])
        return response

    main.app.wsgi_app = counting
    try:
        run(requests, slow)
    finally:
        main.app.wsgi_app = inner


def test_admin_scratch_asks_first_in_a_browser():
    """Joey, 2026-10-07: Scratch asks before it scratches. On the LQ admin page in headless Chrome at 1366 x 1024,
    touch and mouse, every request counted: Scratch with nothing set sends nothing and asks, in the row; its No
    replacement button ticks the box and goes on to the confirm step; the step says what the scratch will do,
    for both kinds, with the betting-open line in race states 1-5 when there is a replacement; Cancel or a change
    to the row sends nothing and keeps what was typed; only Confirm sends, one request on a double tap, what the
    step said; Undo is unchanged; no browser dialog. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("admin page Scratch check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: [_admin_scratch_check(picker_check, touch, requests, slow) for touch in (True, False)])


# -----------------------------------------------------------------------------
# The Control Center sends once a tap (Joey, 2026-10-07)
# -----------------------------------------------------------------------------

CC_MODES = (("WELCOME", "/api/animation/WELCOME"), ("TEST", None), ("STANDBY", "/api/led/all_off"),
            ("BETTING_60", "/api/animation/BETTING_60"), ("BETTING_30", "/api/animation/BETTING_30"),
            ("FINAL_CALL", "/api/animation/FINAL_CALL"), ("AT_THE_GATE", "/api/animation/AT_THE_GATE"),
            ("GATES_BURST", "/api/animation/GATES_BURST"), ("CHAOS", "/api/animation/CHAOS"),
            ("FINISH", "/api/animation/FINISH"), ("HEARTBEAT_COOLDOWN", "/api/animation/HEARTBEAT_COOLDOWN"))


def _control_center_taps(picker_check, touch, requests, slow):
    """One session of test_control_center_one_tap_one_send_in_a_browser."""
    mode = "[touch]" if touch else "[mouse]"
    rig = Rig()
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=touch, size=(1366, 1024))

    def posts(since, path):
        return sum(1 for t, m, p in requests if t >= since and m == "POST" and p == path)

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def double_tap(sel, gap=0.0):
        x, y = chrome.center(sel)
        chrome.tap_at(x, y)
        if gap:
            settle(gap)
        chrome.tap_at(x, y)                          # the same spot, as a finger's second tap

    def button(m):
        return f"button[data-mode='{m}']"

    def wait_posts(since, paths, seconds):         # each path's POSTs since, once each has one or the time is up
        end = time.monotonic() + seconds
        while time.monotonic() < end and not all(posts(since, p) for p in paths):
            settle(0.05)
        return tuple(posts(since, p) for p in paths)

    try:
        chrome.call("Page.navigate", url=server.url)
        chrome.wait_for("document.readyState === 'complete' && typeof showResultsModal === 'function'", timeout=30)
        chrome.wait_for("(() => { const s = document.getElementById('splash-screen'); return !s || s.style.display === 'none'; })()",
                        timeout=20)
        chrome.wait_for("typeof quinielaField !== 'undefined' && quinielaField !== null", timeout=20)
        chrome.eval("window.__confirms = 0; window.confirm = () => { window.__confirms++; return true; }; 1")
        # Every mode button, a double tap each: one race state, one LED request (TEST opens its modal: no LEDs,
        # and no race state, an LED test only).
        server.led_latency = 0.3
        sent = {}
        for m, led in CC_MODES:
            chrome.wait_for("(typeof raceStateSending === 'undefined' || raceStateSending === null) && "
                            "!document.getElementById('loader').classList.contains('show')", timeout=10)
            t = time.monotonic()
            double_tap(button(m))
            end = time.monotonic() + (1.0 if m == "TEST" else 5.0)       # until this button's requests have reached pi5
            while time.monotonic() < end and not (posts(t, "/api/quiniela/mode") and (posts(t, led) if led else True)):
                settle(0.05)
            settle(0.6)
            sent[m] = (posts(t, "/api/quiniela/mode"), posts(t, led) if led else 1)
            if m == "TEST":
                chrome.eval("closeTestModal(); 1")
                settle(0.3)
        _check(f"Control Center {mode}: a double tap on each mode button sends one race state and one LED request; TEST no race state",
               all(v == ((0, 1) if m == "TEST" else (1, 1)) for m, v in sent.items()), str(sent))
        # A slower second tap, after the LEDs answered: still held, so it neither sends nor turns the animation off.
        settle(2.2)
        t = time.monotonic()
        double_tap(button("AT_THE_GATE"), gap=0.7)
        settle(0.5)
        _check(f"Control Center {mode}: a second tap 0.7 s on (the LEDs answered at 0.3 s) sends nothing and does not turn the animation off",
               (posts(t, "/api/quiniela/mode"), posts(t, "/api/animation/AT_THE_GATE"), posts(t, "/api/command")) == (1, 1, 0)
               and chrome.eval("document.querySelector(\"button[data-mode='AT_THE_GATE']\").classList.contains('active')"),
               str((posts(t, "/api/quiniela/mode"), posts(t, "/api/animation/AT_THE_GATE"), posts(t, "/api/command"))))
        # While a race state is on its way no mode button takes a tap; once pi5 has answered, another mode goes.
        # (From PRE-RACE: 60 MIN in WINNER, where the walk above ended, would ask first, gap review OD2.)
        settle(2.2)
        rig.post("/api/quiniela/mode", {"mode": "WELCOME", "reopen": True})
        slow[("POST", "/api/quiniela/mode")] = 3.0
        t = time.monotonic()
        chrome.tap(button("BETTING_60"))
        wait_posts(t, ("/api/quiniela/mode",), 3.0)  # pi5 has 60 MIN's race state and answers 3 s later
        chrome.tap(button("FINAL_CALL"))             # meanwhile
        settle(0.8)
        early = (posts(t, "/api/quiniela/mode"), posts(t, "/api/animation/FINAL_CALL"))
        slow.clear()                                 # pi5 answers at once again
        chrome.wait_for("typeof raceStateSending === 'undefined' || raceStateSending === null", timeout=10)
        t = time.monotonic()
        chrome.tap(button("FINAL_CALL"))
        later = wait_posts(t, ("/api/quiniela/mode", "/api/animation/FINAL_CALL"), 3.0)
        _check(f"Control Center {mode}: a tap on another mode while a race state is on its way sends nothing; once pi5 has answered it goes",
               early == (1, 0) and later == (1, 1) and rig.model()["race_state"] == 2, str((early, later, rig.model()["race_state"])))
        # The LED controller hangs for its whole 5 s: the race state and every other mode still go at once.
        settle(2.2)
        chrome.wait_for("typeof raceStateSending === 'undefined' || raceStateSending === null", timeout=10)
        server.led_latency = 5.0
        answered = len(server.led("ANIM:AT_THE_GATE"))
        t = time.monotonic()
        chrome.tap(button("AT_THE_GATE"))
        wait_posts(t, ("/api/animation/AT_THE_GATE",), 3.0)        # its LED call is hanging from now on
        settle(1.0)
        t1 = time.monotonic()
        chrome.tap(button("AT_THE_GATE"))            # its LEDs still on their way: held
        chrome.tap(button("GATES_BURST"))
        wait_posts(t1, ("/api/animation/GATES_BURST",), 2.5)
        took = round(time.monotonic() - t1, 2)
        went = (posts(t, "/api/quiniela/mode"), posts(t, "/api/animation/AT_THE_GATE"), posts(t, "/api/animation/GATES_BURST"))
        state = rig.model()["race_state"]
        hanging = len(server.led("ANIM:AT_THE_GATE")) == answered      # its LED call has not answered yet
        _check(f"Control Center {mode}: with the LEDs answering in 5 s, THEY'RE OFF! tapped 1 s after AT THE GATE goes at once "
               "(its race state and its LEDs, while AT THE GATE's LED call still hangs), and AT THE GATE tapped again does not",
               went == (2, 1, 1) and state == 4 and took < 2.5 and hanging, str((went, state, took, hanging)))
        settle(5.5)
        server.led_latency = 0.0
        # The panel's Reset asks once (its confirm) and sends once.
        settle(2.2)
        t = time.monotonic()
        double_tap(button("RESET"))
        settle(1.5)
        _check(f"Control Center {mode}: a double tap on Reset asks once and sends /api/results/clear once",
               chrome.eval("window.__confirms") == 1 and posts(t, "/api/results/clear") == 1,
               str((chrome.eval("window.__confirms"), posts(t, "/api/results/clear"))))
        chrome.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=15)
        # SET WINNERS: RESET and CONFIRM RESULTS, a double tap each.
        chrome.tap(button("RESULTS"))
        chrome.wait_for("document.getElementById('results-modal').classList.contains('active') && "
                        "document.querySelectorAll('.winner-pick-btn').length > 0", timeout=15)
        settle(0.3)
        picks = chrome.eval("[...document.querySelectorAll('.winner-pick-btn')].slice(0, 3).map((b) => b.getAttribute('data-horse'))")

        def pick_three():
            for h in picks:
                chrome.tap(f".winner-pick-btn[data-horse='{h}']")
                settle(0.3)

        chrome.tap(f".winner-pick-btn[data-horse='{picks[0]}']")      # one pick: RESET stays where the finger is
        settle(0.3)
        t = time.monotonic()
        double_tap("#results-reset-btn")
        settle(1.0)
        reset_once = posts(t, "/api/cup/unlock")
        pick_three()
        t = time.monotonic()
        chrome.tap("#results-reset-btn")
        settle(0.8)
        _check(f"Control Center {mode}: SET WINNERS, a double tap on RESET sends its unlock once; a RESET after new picks unlocks again",
               reset_once == 1 and posts(t, "/api/cup/unlock") == 1
               and chrome.eval("JSON.stringify(resultsState)") == json.dumps({"chosen": None, "win": None, "place": None, "show": None,
                                                                              "next": [], "tied": [], "refusal": None}, separators=(",", ":")),
               str((reset_once, posts(t, "/api/cup/unlock"), chrome.eval("JSON.stringify(resultsState)"))))
        pick_three()
        chrome.wait_for("getComputedStyle(document.getElementById('results-confirm-section')).display !== 'none'", timeout=10)
        t = time.monotonic()
        double_tap("#results-confirm-btn")
        settle(3.0)
        _check(f"Control Center {mode}: SET WINNERS, a double tap on CONFIRM RESULTS sends /api/results once",
               posts(t, "/api/results") == 1, str(posts(t, "/api/results")))
        _check(f"Control Center {mode}: no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        slow.clear()
        chrome.close()
        server.close()


def test_control_center_one_tap_one_send_in_a_browser():
    """Joey, 2026-10-07: the Control Center's buttons send once a tap, and a slow LED controller holds only the
    button tapped. In headless Chrome at 1366 x 1024, touch and mouse, every request counted where pi5 receives it,
    the LED controller answering through picker_check's stub: a double tap on each mode button sends one race state
    and one LED request, and a second tap after the LEDs answered neither sends nor turns the animation off; while a
    race state is on its way no mode button takes a tap; with the LEDs taking 5 s a different mode is sent within a
    second (its race state and its LEDs) and the same one is not; the panel's Reset asks once and sends once; in
    SET WINNERS a double tap on RESET or CONFIRM RESULTS sends once. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("Control Center tap checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: [_control_center_taps(picker_check, touch, requests, slow) for touch in (True, False)])


# -----------------------------------------------------------------------------
# Race-night guard: the figures at the post survive every tap (Joey, 2026-10-13)
# -----------------------------------------------------------------------------
# Gap review M2, B1, B2, B9; decisions OD1 (FINAL CALL after the post drops the figures and the count, behind a
# confirm), OD2 (TEST is an LED test only; WELCOME, STANDBY, 60 MIN and 30 MIN in 3-5 ask first). All of it on
# the Control Center in headless Chrome, every request counted where pi5 receives it.

REOPENERS = (("WELCOME", "/api/animation/WELCOME"), ("STANDBY", "/api/led/all_off"),
             ("BETTING_60", "/api/animation/BETTING_60"), ("BETTING_30", "/api/animation/BETTING_30"),
             ("FINAL_CALL", "/api/animation/FINAL_CALL"))
GUARD_MAC = "A0:B7:65:66:00:%02X"
GUARD_COUNTS = {12: 1, 18: 0, **{n: (5 if i < 13 else 4) for i, n in enumerate(n for n in range(1, 21) if n not in (12, 18))}}
GUARD_IDLE_JS = ("raceStateSending === null && modesHeld.size === 0 && askPending === null && askHold === null"
                 " && !document.getElementById('loader').classList.contains('show')"
                 " && !document.getElementById('ask-modal').classList.contains('active')")
ASK_UP_JS = "document.getElementById('ask-modal').classList.contains('active') && askPending !== null"


def reopen_text(count=None):
    return ("This reopens betting. The figures at the post (pot $86)"
            + (f" and the hand count (${count}) will be cleared." if count is not None else " will be cleared."))


def _race_night_guards(picker_check, touch, requests, slow, full):
    """One session of test_race_night_guards_in_a_browser. full: every reopening button in every state; else
    one in each."""
    from la_quiniela.betting import MODE_STATES
    mode = "[touch]" if touch else "[mouse]"
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=touch, size=(1366, 1024))

    def posts(since, path=None):
        return sum(1 for t, m, p in requests if t >= since and m == "POST" and (path is None or p == path))

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def double_tap(sel):
        x, y = chrome.center(sel)
        chrome.tap_at(x, y)
        chrome.tap_at(x, y)                          # the same spot, as a finger's second tap

    def wait_posts(since, paths, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not all(posts(since, p) for p in paths):
            settle(0.05)
        return tuple(posts(since, p) for p in paths)

    def button(m):
        return f".panels button[data-mode='{m}']"

    def ready():                                     # nothing on its way, nothing held, no question up; no button active
        chrome.wait_for(GUARD_IDLE_JS, timeout=15)
        chrome.eval("clearAllActiveButtons(); window.__asks = 0; 1")

    def cups(**changes):
        for n, count in {**GUARD_COUNTS, **{int(k[1:]): v for k, v in changes.items()}}.items():
            rig.bridge.handle_raw_line(telem(GUARD_MAC % n, horse=n, count=count))
        rig.board.refresh()

    def race_at(state, count=None):                  # pi5 in `state`; from 3 on (6 included) with figures at the post (pot $86)
        rig.post("/api/quiniela/mode", {"mode": "BETTING_60", "reopen": True})  # any figures from before go
        cups()
        if state >= 3:
            rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})
            if count is not None:
                rig.client.put("/api/quiniela/counted_pot", json={"amount": count})
            rig.post("/api/quiniela/mode", {"mode": {3: "AT_THE_GATE", 4: "GATES_BURST", 5: "HEARTBEAT_COOLDOWN", 6: "RESET"}[state]})
        else:
            rig.post("/api/quiniela/mode", {"mode": {0: "WELCOME", 1: "BETTING_60", 2: "FINAL_CALL"}[state]})
        m = rig.model()
        assert m["race_state"] == state and (m["pot_scale"] == 86.0) == (state >= 3), (state, m["race_state"], m["pot_scale"])

    def ask():
        return chrome.eval("[document.getElementById('ask-title').textContent, document.getElementById('ask-text').textContent,"
                           " document.getElementById('ask-ok').textContent]")

    try:
        chrome.call("Page.navigate", url=server.url)
        chrome.wait_for("document.readyState === 'complete' && typeof showResultsModal === 'function'", timeout=30)
        chrome.wait_for("(() => { const s = document.getElementById('splash-screen'); return !s || s.style.display === 'none'; })()",
                        timeout=20)
        chrome.wait_for("raceModes !== null && typeof quinielaField !== 'undefined' && quinielaField !== null", timeout=20)
        chrome.eval("window.__confirms = 0; window.confirm = () => { window.__confirms++; return true; };"
                    " window.__asks = 0; const realAsk = askFirst; askFirst = (...a) => { window.__asks++; return realAsk(...a); }; 1")

        # B2: at the post with a count, TEST opened, run, closed: state 3, the figures and the count as they were.
        race_at(3, count=80)
        ready()
        t = time.monotonic()
        chrome.tap(button("TEST"))
        chrome.wait_for("document.getElementById('test-modal').classList.contains('active')", timeout=10)
        settle(0.4)
        if chrome.eval("typeof iro !== 'undefined'"):
            chrome.tap(".preset-btn[data-name='ROSE']")
        else:                                        # no internet for the colour wheel's script: the same command
            chrome.eval("sendTestColor(225, 149, 171, currentBrightness); 1")
        settle(0.4)
        chrome.eval("(() => { const s = document.getElementById('brightness-slider'); s.value = '40';"
                    " s.dispatchEvent(new Event('input')); return 1; })()")
        settle(0.4)
        chrome.tap("#test-modal .modal-buttons .btn")
        settle(0.8)
        m = rig.model()
        _check(f"guards {mode}: TEST at the post (B2): the LED test runs and closes, no race state sent; state 3, the figures and the count intact",
               posts(t, "/api/quiniela/mode") == 0 and posts(t, "/api/command") >= 2
               and not chrome.eval("document.getElementById('test-modal').classList.contains('active')")
               and (m["race_state"], m["pot_scale"], m["pot_counted"], m["closing"]["horses"]["12"]) == (3, 86.0, 80, {"tokens": 1}),
               str((posts(t, "/api/quiniela/mode"), posts(t, "/api/command"), m["race_state"], m["pot_scale"], m["pot_counted"])))

        # Each reopening button in 3, 4, 5 and 6 (AFTER PARTY after a RESET: Joey, 2026-10-28, review Q1): the confirm with the
        # real figures; Cancel sends nothing; Reopen betting, a double tap, sends once, and the figures and the count are cleared.
        cases = [(m, s) for s in (3, 4, 5, 6) for m, _ in REOPENERS] if full else [("FINAL_CALL", 3), ("BETTING_60", 4), ("WELCOME", 5), ("STANDBY", 6)]
        led_of = dict(REOPENERS)
        out = {}
        for m, s in cases:
            count = None if s == 4 else 80                          # 4 without a count: the text says only the pot
            race_at(s, count)
            ready()
            t = time.monotonic()
            chrome.tap(button(m))
            chrome.wait_for(ASK_UP_JS, timeout=10)
            settle(0.3)
            asked = (ask(), posts(t))
            chrome.tap("#ask-cancel")
            settle(0.9)
            mm = rig.model()
            cancelled = (posts(t), mm["race_state"], mm["pot_scale"], mm["pot_counted"],
                         chrome.eval("document.getElementById('ask-modal').classList.contains('active')"))
            ready()
            t2 = time.monotonic()
            chrome.tap(button(m))
            chrome.wait_for(ASK_UP_JS, timeout=10)
            double_tap("#ask-ok")
            wait_posts(t2, ("/api/quiniela/mode", led_of[m]), 5.0)
            settle(1.0)
            mm = rig.model()
            went = (posts(t2, "/api/quiniela/mode"), posts(t2, led_of[m]), posts(t2), mm["race_state"], mm["closing"],
                    mm["pot_counted"], chrome.eval("window.__asks"))
            out[(m, s)] = (asked == (["Reopen betting?", reopen_text(count), "Reopen betting"], 0)
                           and cancelled == (0, s, 86.0, count, False) and went == (1, 1, 2, MODE_STATES[m], None, None, 1),
                           str((asked, cancelled, went)))
        bad = {k: v[1] for k, v in out.items() if not v[0]}
        _check(f"guards {mode}: {len(out)} reopening taps in states 3-6: the confirm names the real figures, Cancel sends nothing, "
               "Reopen betting (a double tap) sends once and clears the figures and the count", not bad, str(bad)[:900])

        # A stale page: its fresh read says nothing is held, pi5 is RUNNING with the figures and a count. The tap goes with no
        # question and no `reopen`; pi5 refuses it, nothing is cleared, and the page says why.
        race_at(4, 80)
        ready()
        chrome.eval("window.__realFresh = freshRaceMode; freshRaceMode = async () => ({state: 2, state_name: 'FINAL_CALL', at_post: null}); 1")
        t = time.monotonic()
        chrome.tap(button("WELCOME"))
        wait_posts(t, ("/api/quiniela/mode",), 5.0)
        settle(0.8)
        mm = rig.model()
        said = chrome.eval("document.getElementById('notification').textContent")
        chrome.eval("freshRaceMode = window.__realFresh; 1")
        _check(f"guards {mode}: a stale page that did not ask is refused by pi5: no question, the figures and the count stay, "
               "the page says what it would have cleared",
               chrome.eval("window.__asks") == 0 and (mm["race_state"], mm["pot_scale"], mm["pot_counted"]) == (4, 86.0, 80)
               and "race state not set (This reopens betting: the figures at the post (pot $86) and the hand count ($80)" in said, said)

        # In 0-2 the same buttons send at once: no question.
        cases = [(m, s) for s in (0, 1, 2) for m, _ in REOPENERS] if full else [("STANDBY", 2), ("BETTING_30", 0)]
        out = {}
        for m, s in cases:
            race_at(s)
            ready()
            t = time.monotonic()
            chrome.tap(button(m))
            got = wait_posts(t, ("/api/quiniela/mode", led_of[m]), 5.0)
            settle(0.4)
            out[(m, s)] = (got == (1, 1) and chrome.eval("window.__asks") == 0 and rig.model()["race_state"] == MODE_STATES[m]
                           and not chrome.eval("document.getElementById('ask-modal').classList.contains('active')"),
                           str((got, chrome.eval("window.__asks"), rig.model()["race_state"])))
        bad = {k: v[1] for k, v in out.items() if not v[0]}
        _check(f"guards {mode}: {len(out)} taps of the same buttons in states 0-2 send at once, no question", not bad, str(bad)[:600])

        # B1: close, reopen (the confirm), more tokens, close again: the second close's figures, on the TV's model, and
        # the log has both closes and the reopen.
        race_at(3, count=80)
        ready()
        t = time.monotonic()
        chrome.tap(button("FINAL_CALL"))
        chrome.wait_for(ASK_UP_JS, timeout=10)
        text = ask()[1]
        chrome.tap("#ask-ok")
        wait_posts(t, ("/api/quiniela/mode",), 5.0)
        settle(0.5)
        reopened = rig.model()
        cups(h12=11)                                                # ten more on #12: the live pot $96
        ready()
        t = time.monotonic()
        chrome.tap(button("AT_THE_GATE"))
        wait_posts(t, ("/api/quiniela/mode",), 5.0)
        settle(0.5)
        tv = json.loads(rig.board.model_json())
        log = [json.loads(line) for f in sorted((rig.dir / "logs").glob("quiniela_*.jsonl"))
               for line in f.read_text(encoding="utf-8").splitlines() if line]
        marks = [(r["betting"], (r.get("dropped") or {}).get("pot_counted"),
                  next((c["closing"]["pot"] for c in r["changes"] if c.get("closing")), None)) for r in log if "betting" in r][-3:]
        _check(f"guards {mode}: B1 through the page: FINAL CALL asks with $86 / $80, clears both; AT THE GATE takes $96 (#12 at 11), "
               "which the TV's model shows; the log has the close, the reopen and the second close",
               text == reopen_text(80) and (reopened["race_state"], reopened["closing"], reopened["pot_counted"]) == (2, None, None)
               and tv["race_state"] == 3 and tv["closing"]["pot"] == 96.0 and tv["closing"]["horses"]["12"] == {"tokens": 11}
               and tv["pot"] == 96.0 and marks == [("closed", None, 86.0), ("reopened", 80, None), ("closed", None, 96.0)],
               str((text, reopened["race_state"], tv["closing"]["pot"], marks)))

        # With the results set, the reopening buttons stay disabled, as before.
        race_at(5)
        rig.post("/api/results", {"win": 19, "place": 1, "show": 3})
        chrome.wait_for("document.getElementById('results-reveal-modal').classList.contains('active')", timeout=10)
        chrome.eval("revealWinners(); 1")
        settle(0.3)
        locked = chrome.eval("JSON.stringify(%s.map((m) => document.querySelector(`.panels button[data-mode='${m}']`).disabled))"
                             % json.dumps([m for m, _ in REOPENERS]))
        _check(f"guards {mode}: with the results set the reopening buttons are disabled, as before",
               json.loads(locked) == [True] * len(REOPENERS), locked)

        # CONFIRM RESULTS: a success names the race state it set; a La Quiniela refusal is red with its reason.
        def confirm_results(horses):
            rig.post("/api/results/clear")               # the dialog opens on a saved result: start it empty
            ready()
            chrome.eval("(() => { const n = document.getElementById('notification'); n.classList.remove('show');"
                        " n.textContent = ''; return 1; })()")
            chrome.tap(button("RESULTS"))
            chrome.wait_for("document.getElementById('results-modal').classList.contains('active') && "
                            "document.querySelectorAll('.winner-pick-btn').length > 0", timeout=15)
            settle(0.3)
            for h in horses:
                chrome.tap(f".winner-pick-btn[data-horse='{h}']")
                settle(0.3)
            chrome.wait_for("getComputedStyle(document.getElementById('results-confirm-section')).display !== 'none'", timeout=10)
            chrome.tap("#results-confirm-btn")
            return chrome.wait_for("(() => { const n = document.getElementById('notification');"
                                   " return n.classList.contains('show') && n.textContent.startsWith('Results set')"
                                   " ? [n.textContent, n.classList.contains('error') ? 'red' : 'green'] : null; })()", timeout=15)

        race_at(4)
        good = confirm_results((7, 3, 12))
        saved_mode = main.quiniela_mode
        main.quiniela_mode = lambda m: (None, "La Quiniela: refused for the test")
        try:
            refused = confirm_results((5, 14, 2))
        finally:
            main.quiniela_mode = saved_mode
        _check(f"guards {mode}: CONFIRM RESULTS says the race state it set, green: Results set ... · WINNER",
               good == ["Results set: Win=7, Place=3, Show=12 · WINNER", "green"], str(good))
        _check(f"guards {mode}: ...and when La Quiniela refuses WINNER, red with its reason",
               refused == ["Results set: Win=5, Place=14, Show=2 · race state not set (La Quiniela: refused for the test)", "red"],
               str(refused))
        rig.post("/api/results/clear")
        chrome.wait_for("bannerResults === null", timeout=15)
        settle(1.0)

        # AUTO: switched on at the post it asks first; while on, SET WINNERS, STANDBY and RESET (every mode button) are
        # disabled and say "switch to MANUAL", and a tap on them sends nothing; MANUAL again needs no question.
        switch = "label.race-control-switch:has(#race-control-toggle)"
        race_at(3)
        ready()
        t = time.monotonic()
        chrome.tap(switch)
        chrome.wait_for(ASK_UP_JS, timeout=10)
        asked = ask()
        chrome.tap("#ask-cancel")
        settle(0.9)
        cancelled = (posts(t, "/api/racing/mode"), chrome.eval("raceControlMode"), chrome.eval("document.getElementById('race-control-toggle').checked"))
        ready()
        t = time.monotonic()
        chrome.tap(switch)
        chrome.wait_for(ASK_UP_JS, timeout=10)
        chrome.tap("#ask-ok")
        wait_posts(t, ("/api/racing/mode",), 5.0)
        settle(0.6)
        went = (posts(t, "/api/racing/mode"), chrome.eval("raceControlMode"), chrome.eval("document.getElementById('race-control-toggle').checked"))
        held = json.loads(chrome.eval("JSON.stringify([...document.querySelectorAll('.panels button.btn')].map((b) =>"
                                      " [b.dataset.mode, b.disabled, b.innerText.includes('switch to MANUAL')]))"))
        _check(f"guards {mode}: AUTO switched on at the post asks first; Cancel leaves MANUAL and sends nothing; Switch to AUTO sends once",
               asked[0] == "Switch to AUTO?" and "AT THE POST" in asked[1] and asked[2] == "Switch to AUTO"
               and cancelled == (0, "manual", False) and went == (1, "auto", True), str((asked, cancelled, went)))
        _check(f"guards {mode}: in AUTO every mode button is disabled and says 'switch to MANUAL' (SET WINNERS, STANDBY, RESET among them)",
               len(held) == 13 and all(d and r for _, d, r in held)
               and {m for m, d, r in held if d and r} >= {"RESULTS", "STANDBY", "RESET"}, str(held))
        t = time.monotonic()
        confirms = chrome.eval("window.__confirms")
        for m in ("RESULTS", "STANDBY", "RESET"):
            chrome.tap(button(m))
            settle(0.6)
        _check(f"guards {mode}: ...and a tap on SET WINNERS, STANDBY or RESET in AUTO does nothing at all",
               posts(t) == 0 and chrome.eval("window.__confirms") == confirms
               and not chrome.eval("document.getElementById('results-modal').classList.contains('active')"), str(posts(t)))
        chrome.eval("window.__asks = 0; 1")
        t = time.monotonic()
        chrome.tap(switch)
        wait_posts(t, ("/api/racing/mode",), 5.0)
        settle(0.6)
        back = json.loads(chrome.eval("JSON.stringify([...document.querySelectorAll('.panels button.btn')].map((b) =>"
                                      " [b.disabled, !!b.querySelector('.btn-reason')]))"))
        _check(f"guards {mode}: MANUAL again without a question: every mode button enabled, the reasons gone",
               chrome.eval("window.__asks") == 0 and chrome.eval("raceControlMode") == "manual"
               and all(not d and not r for d, r in back), str((chrome.eval("window.__asks"), back[:3])))
        race_at(1)
        ready()
        t = time.monotonic()
        chrome.tap(switch)
        wait_posts(t, ("/api/racing/mode",), 5.0)
        settle(0.6)
        _check(f"guards {mode}: with betting open AUTO goes without a question",
               chrome.eval("window.__asks") == 0 and chrome.eval("raceControlMode") == "auto", str(chrome.eval("raceControlMode")))
        chrome.tap(switch)
        wait_posts(time.monotonic() - 0.01, ("/api/racing/mode",), 3.0)
        settle(0.6)
        _check(f"guards {mode}: no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        slow.clear()
        try:
            main.racing_service.set_mode("manual")
        except Exception:
            pass
        chrome.close()
        server.close()


def test_race_night_guards_in_a_browser():
    """Joey, 2026-10-13 (gap review M2, B1, B2, B9; OD1, OD2): nothing drops the figures at the post by accident. In
    headless Chrome at 1366 x 1024, touch (every reopening button in every state) and mouse (one in each), every
    request counted: TEST at the post sends no race state and the figures and count stay; WELCOME, STANDBY, 60 MIN,
    30 MIN and FINAL CALL in 3-5 show the in-page confirm with the real figures, Cancel sends nothing, Reopen
    betting sends once (double tap) and clears them; in 0-2 they send at once; B1 through the page; with results the
    buttons stay disabled; CONFIRM RESULTS says WINNER, or La Quiniela's reason in red; AUTO asks first at the post,
    holds every mode button with the reason, and MANUAL again needs no question. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("race-night guard browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: [_race_night_guards(picker_check, touch, requests, slow, full=touch)
                                                   for touch in (True, False)])


# -----------------------------------------------------------------------------
# The picker follows the field while it is open; a stale dialog cannot save a scratched horse (gap review M5)
# -----------------------------------------------------------------------------

def _picker_follows_the_field(picker_check, requests, slow):
    rig = Rig()
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    server = picker_check.Server(rig)
    picker = picker_check.Picker(server, touch=True)
    chrome = picker.chrome
    modal_up = "document.getElementById('results-modal').classList.contains('active')"

    def posts(since, path, method="POST"):
        return sum(1 for t, m, p in requests if t >= since and m == method and p == path)

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chrome.eval("1")
            time.sleep(0.05)

    def tile(horse):
        return chrome.eval(f"!!document.querySelector(\".winner-pick-btn[data-horse='{horse}']\")")

    def wait_gone(horse, timeout=6):
        chrome.wait_for(f"!document.querySelector(\".winner-pick-btn[data-horse='{horse}']\")", timeout=timeout)

    try:
        picker.open()
        picker.tap_horse(7)
        settle(0.3)
        picker.tap_horse(3)
        settle(0.3)
        st = picker.state()
        _check("WIN 7, PLACE 3 picked", (st["win"], st["place"]) == (7, 3), str(st))
        t = time.monotonic()
        r = rig.post("/api/quiniela/scratch", {"horse": 3})
        assert r.status_code == 200, r.get_json()
        wait_gone(3)
        took = time.monotonic() - t
        settle(0.3)
        st = picker.state()
        _check("3 scratched on the LQ admin page while the picker is open: within 3 s its tile is gone, PLACE is empty, WIN still 7",
               took < 3.5 and st["win"] == 7 and st["place"] is None and st["show"] is None and not tile(3), f"{took:.1f} s {st}")
        _check("...the note says so", picker.note() == "#3 MYSTIK DAN was scratched: PLACE is empty, tap the horse that finished there.",
               repr(picker.note()))
        settle(0.5)
        _check("...its cup unlocked", "CUP:UNLOCK:3" in server.led("CUP:UNLOCK"), str(server.led("CUP:")))
        _check("...the next tap fills PLACE", chrome.eval("activeSlot()") == "place")
        # a replacement is refused from the gate on (the race is RUNNING here), so this one is a record entered at BETTING OPEN
        # (state 1) and read by the open picker: what the page does with a replacement is what is tested
        rig.board.store.scratch_replace(9, 22, "Ocelli", state=1, at=time.time())
        rig.board.refresh()
        chrome.wait_for("!!document.querySelector(\".winner-pick-btn[data-horse='22']\")", timeout=6)
        at9 = chrome.eval("(() => { const b = document.querySelector(\".winner-pick-btn[data-post='9']\");"
                          " return b ? [b.dataset.horse, b.title] : null; })()")
        _check("9 -> 22 recorded meanwhile: post 9 offers 22 · OCELLI, the picks untouched, the note names 9",
               at9 == ["22", "22 · OCELLI"] and picker.state()["win"] == 7 and picker.note() == "#9 ENCINO was scratched.",
               f"{at9} {picker.note()!r}")
        # a stale dialog: the page's own poll held back, 12 scratched, CONFIRM RESULTS pressed: pi5 refuses and the dialog says so
        picker.tap_horse(12)
        settle(0.3)
        picker.tap_horse(19)
        settle(0.3)
        chrome.wait_for("getComputedStyle(document.getElementById('results-confirm-section')).display !== 'none'", timeout=5)
        chrome.eval("stopResultsFieldPoll(); 1")
        r = rig.post("/api/quiniela/scratch", {"horse": 12})
        assert r.status_code == 200, r.get_json()
        t = time.monotonic()
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("document.getElementById('results-pick-note').innerText.includes('was scratched')", timeout=8)
        note = picker.note()
        _check("CONFIRM RESULTS from a stale dialog: pi5 refuses (12 is not in the field), nothing saved, the picker open; the field read "
               "again takes 12 out of PLACE, which says so under the heading, and CONFIRM RESULTS is gone until PLACE is set again",
               note == "#12 TRACK PHANTOM was scratched: PLACE is empty, tap the horse that finished there."
               and not rig.results_file.exists() and posts(t, "/api/results") == 1 and chrome.eval(modal_up) and rig.bridge.phase == 4
               and chrome.eval("getComputedStyle(document.getElementById('results-confirm-btn')).display") == "none", repr(note))
        wait_gone(12)
        settle(0.3)
        st = picker.state()
        _check("...and the field is read again at once: 12's tile gone and PLACE empty, WIN 7 and SHOW 19 as they were",
               st["win"] == 7 and st["place"] is None and st["show"] == 19 and not tile(12), str(st))
        chrome.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=10)
        chrome.eval("startResultsFieldPoll(); 1")
        picker.tap_horse(1)
        settle(0.3)
        chrome.wait_for("getComputedStyle(document.getElementById('results-confirm-section')).display !== 'none'", timeout=5)
        chrome.tap("#results-confirm-btn")
        chrome.wait_for("!" + modal_up, timeout=10)
        saved = rig.board.results()
        _check("1 for PLACE, confirmed: saved 7 / 1 / 19, WINNER", saved == {"win": 7, "place": 1, "show": 19, "next": [], "ties": []}
               and rig.bridge.phase == 5, str(saved))
        # RESET in the dialog with a result saved: it asks, then clears the result through the panel's own route
        chrome.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=15)
        picker.open()
        t = time.monotonic()
        chrome.tap("#results-reset-btn")
        chrome.wait_for(ASK_UP_JS, timeout=6)
        asked = chrome.eval("[document.getElementById('ask-title').innerText, document.getElementById('ask-text').innerText,"
                            " document.getElementById('ask-ok').innerText]")
        _check("RESET with a result saved asks first: the three by name, what clearing does, the button",
               asked == ["Clear the saved result?",
                         "WIN 7 · HONOR MARIE, PLACE 1 · DORNOCH, SHOW 19 · RESILIENCE. Clearing ends the race (AFTER PARTY: "
                         "the TV back to the slideshow, the LEDs off) until you confirm new results; scratch on the LQ admin page in "
                         "between if you need to.", "Clear the result"], str(asked))
        chrome.tap("#ask-cancel")
        settle(0.9)
        _check("Cancel: nothing sent, the result stands, the picks as they were (the saved result's: the dialog opened on it)",
               posts(t, "/api/results/clear") == 0 and rig.board.results() is not None and picker.state()["win"] == 7)
        chrome.wait_for("askPending === null && askHold === null", timeout=5)
        chrome.tap("#results-reset-btn")
        chrome.wait_for(ASK_UP_JS, timeout=6)
        chrome.tap("#ask-ok")
        chrome.wait_for("document.getElementById('results-banner').style.display === 'none'", timeout=10)
        settle(0.6)
        _check("Clear the result: /api/results/clear sent once, the result gone, AFTER PARTY, the tote down, the picker still open and empty",
               posts(t, "/api/results/clear") == 1 and rig.board.results() is None and rig.bridge.phase == 6 and chrome.eval(modal_up)
               and picker.state()["win"] is None, str((posts(t, "/api/results/clear"), rig.bridge.phase)))
        r = rig.post("/api/quiniela/scratch", {"horse": 19})               # the scratch the host cleared the result for
        assert r.status_code == 200, r.get_json()
        wait_gone(19)
        _check("...and a scratch made now reaches the open picker", not tile(19) and picker.note() == "#19 RESILIENCE was scratched.",
               repr(picker.note()))
        t = time.monotonic()
        chrome.tap("#results-reset-btn")
        settle(0.9)
        _check("RESET with no result saved asks pi5, then nobody, and clears nothing",
               not chrome.eval(ASK_UP_JS) and posts(t, "/api/results/clear") == 0 and posts(t, "/api/results", "GET") >= 1
               and posts(t, "/api/results") == 0, str((posts(t, "/api/results/clear"), posts(t, "/api/results", "GET"))))
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        picker.close()
        server.close()


def test_picker_follows_the_field_in_a_browser():
    """The picker in headless Chrome (touch, an iPad's size), every request counted where pi5 receives it: a
    scratch made on the LQ admin page while the picker is open takes the horse out of the grid within the page's
    2 s poll, empties its slot and says so; a replacement shows under its post; a stale dialog (the poll held
    back) cannot save a scratched horse: pi5 refuses and the dialog shows pi5's line and reads the field again;
    RESET with a result saved asks first and clears it through the panel's own route, the picker staying open for
    the scratch and the new picks. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("picker-follows-the-field checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _picker_follows_the_field(picker_check, requests, slow))


# -----------------------------------------------------------------------------
# Undo always brings the cup back: the admin page's renumbering badge (gap review M7)
# -----------------------------------------------------------------------------

def test_admin_renumbering_badge_in_a_browser():
    """A cup that has not followed its renumber pair a minute on (here: off when the scratch was undone) is shown on
    the LQ admin page as ⏳ renumbering beside the horse it should be (9), from the model's renumbering list; when
    the cup is back and has followed, the badge goes. Skipped where there is no Chrome (the source check is not)."""
    rig = Rig()
    html = rig.client.get("/quiniela/admin").get_data(as_text=True)
    _check("the admin page reads the model's renumbering list and badges the horse it names",
           "model.renumbering" in html and "r.to === n" in html and (chr(0x23F3) + " renumbering") in html and 'class="tag ren"' in html)
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.bridge.handle_raw_line(telem(MAC_A, horse=9, count=4))
    rig.board.refresh()
    rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
    rig.clk.advance(1)
    rig.bridge.handle_raw_line(telem(MAC_A, horse=22, count=4))      # the cup followed the scratch
    rig.board.refresh()
    rig.clk.advance(8)
    rig.bridge.tick(rig.clk())                                         # then it went off
    rig.post("/api/quiniela/unscratch", {"horse": 9})
    rig.clk.advance(61)
    rig.bridge.tick(rig.clk())
    rig.board.refresh()
    late = rig.model()["renumbering"]
    _check("a minute on with the cup off: the model lists it, 22 -> 9, offline",
           [(r["from"], r["to"], r["mac"], r["online"]) for r in late] == [(22, 9, MAC_A, False)], str(late))
    picker_check = _picker_check()
    if not picker_check.available():
        _check("renumbering badge browser check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True, size=(1366, 1024))
    badge = ("(() => { const t = document.querySelector('#horses .hrow[data-horse=\"%d\"] .tag.ren');"
             " return t ? [t.textContent, t.getAttribute('title')] : null; })()")
    try:
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && !!document.querySelector('#horses .hrow')", timeout=20)
        shown = chrome.wait_for(badge % 9, timeout=15)
        _check("the badge on horse 9's row: ⏳ renumbering, the cup and what it still says in its title; none on 22",
               shown == [chr(0x23F3) + " renumbering", "cup %s still says 22 (off)" % MAC_A] and chrome.eval(badge % 22) is None,
               str(shown))
        rig.bridge.handle_raw_line(telem(MAC_A, horse=22, count=4))  # the cup is back, still 22: the pair goes down
        rig.board.refresh()
        rig.clk.advance(2)
        rig.bridge.handle_raw_line(telem(MAC_A, horse=9, count=4))   # it followed
        rig.board.refresh()
        gone = chrome.wait_for("(%s) === null" % (badge % 9), timeout=12)
        _check("the cup back and on 9: the badge goes on the page's own refresh", gone is True and rig.model()["renumbering"] == [])
        _check("no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        server.close()


def test_admin_late_tag_in_real_time():
    """Item (z) on DevPi: the renum_late event for horse 19 stood for three minutes and the admin page never showed
    the tag. Here the real admin page is open from the start and does it all itself, with the board's own thread on
    the real clock and a cup reporting every 2 s from a thread of its own (RENUM_LATE_S cut to 6 s), and a ghost on
    19 (a cup in the cache from an old database, offline, heard on 2026-09-20). Scratch 19 -> 21 with the page's
    Scratch and Confirm scratch: the cup follows; the ghost is never late (no tag on 21), and row 19 says when it was
    last heard. The cup is switched off and the page's Undo pressed: the tag is on 19 within one 5 s poll of the
    model listing the cup, and gone within one poll of the cup coming back to 19. Then the page left open across an
    update: when GET /api/quiniela names another page, it reloads itself while idle, and while something is being
    typed it says so and waits. Skipped where there is no Chrome."""
    import threading
    from la_quiniela import betting as betting_mod
    import page_rev
    from la_quiniela.bridge import CupLive
    picker_check = _picker_check()
    if not picker_check.available():
        _check("real-time late tag browser check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    saved = (betting_mod.RENUM_LATE_S, page_rev.PAGE_REV)
    betting_mod.RENUM_LATE_S = 6.0
    rig = Rig(real_time=True)
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    ghost = CupLive(mac="A4:F0:0F:5E:0B:08")
    ghost.horse, ghost.count, ghost.last_seen_ts = 19, 0, "2026-09-20T18:00:00Z"
    rig.bridge.cups[ghost.mac] = ghost                 # as _load_cups seeds it from an old lq_cups row
    cup = {"horse": 19, "on": True, "next": 0.0}
    stop = threading.Event()

    def the_cup():                                     # hears every state line at once, reports every 2 s
        while not stop.is_set():
            now = time.monotonic()
            if cup["on"]:
                lines = [l for l in rig.port.lines() if l.startswith('{"t":"state"')]
                if lines:
                    for f, t in json.loads(lines[-1])["renum"]:
                        if f == cup["horse"]:
                            cup["horse"] = t
                if now >= cup["next"]:
                    rig.bridge.handle_raw_line(telem(MAC_A, horse=cup["horse"], count=4))
                    cup["next"] = now + 2.0
            rig.bridge.tick(now)
            stop.wait(0.1)

    threading.Thread(target=the_cup, name="test-cup", daemon=True).start()
    rig.board.start()
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True, size=(1366, 1024))
    badge = ("(() => { const t = document.querySelector('#horses .hrow[data-horse=\"%d\"] .tag.ren');"
             " return t ? [t.textContent, t.getAttribute('title')] : null; })()")
    status = "document.querySelector('#horses .hrow[data-horse=\"%d\"] .hstat').innerText"

    def late():
        return rig.board.model()["renumbering"]

    try:
        chrome.call("Page.navigate", url=server.url + "quiniela/admin")
        chrome.wait_for("document.readyState === 'complete' && "
                        "!!document.querySelector('#scratch-list .horse[data-horse=\"19\"] [data-scratch]')", timeout=20)
        chrome.wait_for(("(%s).indexOf('online') >= 0" % (status % 19)), timeout=12)
        chrome.tap('#scratch-list .horse[data-horse="19"] [data-repl-name]')
        chrome.call("Input.insertText", text="TEST")
        chrome.tap('#scratch-list .horse[data-horse="19"] [data-scratch]')
        chrome.wait_for("!!document.querySelector('#scratch-list .horse[data-horse=\"19\"] [data-confirm]')", timeout=5)
        chrome.tap('#scratch-list .horse[data-horse="19"] [data-confirm]')
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"19\"] [data-undo]')", timeout=10)
        end = time.monotonic() + 10
        while time.monotonic() < end and cup["horse"] != 21:
            time.sleep(0.1)
        _check("real time: the page's Scratch and Confirm made 19 -> 21 and the cup followed", cup["horse"] == 21, str(cup))
        heard = chrome.wait_for("(() => { const s = %s; return s.indexOf('heard Sep 20') >= 0 ? s : null; })()" % (status % 19),
                                timeout=12)
        tags_on_21 = []
        end = time.monotonic() + betting_mod.RENUM_LATE_S + 6
        while time.monotonic() < end:
            tags_on_21.append(chrome.eval(badge % 21))
            time.sleep(0.25)
        _check("...the ghost left alone on 19: its row says when it was last heard (heard Sep 20), and it is never "
               "late: no tag on 21, nothing in the model", "offline" in heard and not any(tags_on_21) and late() == [],
               str((heard, [t for t in tags_on_21 if t][:1], late())))
        cup["on"] = False                               # unplugged
        chrome.wait_for(("(%s).indexOf('offline') >= 0" % (status % 21)), timeout=15)
        chrome.tap('#scratched-list .horse[data-horse="19"] [data-undo]')
        chrome.wait_for("!!document.querySelector('#scratch-list .horse[data-horse=\"19\"] [data-scratch]')", timeout=10)
        end = time.monotonic() + betting_mod.RENUM_LATE_S + 10
        while time.monotonic() < end and not late():
            time.sleep(0.05)
        listed, t_model = late(), time.monotonic()
        try:
            shown = chrome.wait_for(badge % 19, timeout=10)
        except TimeoutError:
            shown = None
        took = time.monotonic() - t_model
        _check("the cup off at the Undo: the model lists it (21 -> 19, off) and the tag is on horse 19's row within one "
               "5 s poll, naming the cup; none on 21",
               [(r["from"], r["to"], r["online"]) for r in listed] == [(21, 19, False)] and took <= 6.5
               and shown == [chr(0x23F3) + " renumbering", "cup %s still says 21 (off)" % MAC_A] and chrome.eval(badge % 21) is None,
               "%s after %.1f s: %s" % (listed, took, shown))
        cup["on"], cup["next"] = True, 0.0              # plugged back in
        end = time.monotonic() + 10
        while time.monotonic() < end and (late() or cup["horse"] != 19):
            time.sleep(0.05)
        t_ack = time.monotonic()
        try:
            gone = chrome.wait_for("(%s) === null" % (badge % 19), timeout=10)
        except TimeoutError:
            gone = False
        took = time.monotonic() - t_ack
        _check("...plugged back in: the cup is 19 again, and the tag goes within one poll",
               cup["horse"] == 19 and late() == [] and gone is True and took <= 6.5, "%s after %.1f s" % (cup, took))
        _check("...no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])

        # The page left open across an update (the iPad's Home Screen app keeps it for days).
        meta = "document.querySelector('meta[name=\"ddm-page-rev\"]').content"
        _check("the page carries the revision pi5 serves, and so does GET /api/quiniela",
               chrome.eval(meta) == page_rev.PAGE_REV != "" and
               rig.client.get("/api/quiniela").headers.get("X-Page-Rev") == page_rev.PAGE_REV)
        chrome.eval("window.__before = 1; 1")
        page_rev.PAGE_REV = "updated-0001"
        try:
            reloaded = chrome.wait_for("window.__before === undefined && document.readyState === 'complete' && (%s) === 'updated-0001'"
                                       % meta, timeout=12)
        except TimeoutError:
            reloaded = False
        _check("pi5 serves another page: the open page reloads itself within a poll, idle", reloaded is True)
        chrome.wait_for("!!document.querySelector('#names-text')", timeout=10)
        chrome.eval("window.__before = 1; 1")
        chrome.tap("#names-text")
        chrome.call("Input.insertText", text="x")
        page_rev.PAGE_REV = "updated-0002"
        try:
            noted = chrome.wait_for("document.getElementById('page-note').textContent", timeout=12)
        except TimeoutError:
            noted = ""
        _check("...while something is being typed it does not reload, and says why at the top",
               chrome.eval("window.__before") == 1 and "newer version of this page" in noted, repr(noted))
    finally:
        stop.set()
        rig.board.stop()
        chrome.close()
        server.close()
        betting_mod.RENUM_LATE_S, page_rev.PAGE_REV = saved


# -----------------------------------------------------------------------------
# The Control Center's ticker is gone (Joey, 2026-10-13, gap review OD9)
# -----------------------------------------------------------------------------
# The TV's crawl is the only crawl. The ticker went with its markup, CSS, JS, its two timers (the step every
# 220 ms and the message every second) and the 30-minute /api/weather fetch, which fed only it (the forecast
# modal it also filled had nothing left that opened it). pi5's chyron, its weather feed for the TV and the
# 5x7 table the TV's face is built from stay.

TICKER_WORDS = ("ticker-strip", "ticker-track", "buildTicker", "initTicker", "tickerTick", "renderTickerFrame",
                "tickerMessage", "getWeather", "weatherData", "openWeatherModal", "weather-modal", "/api/weather")


def test_no_ticker():
    rig = Rig()
    html = rig.client.get("/").get_data(as_text=True)
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    left = {name: [w for w in TICKER_WORDS if w in text] for name, text in (("page", html), ("script", js), ("stylesheet", css))}
    _check("the ticker is gone from the page, the script and the stylesheet, with its weather fetch and the forecast modal",
           not any(left.values()), str(left))
    _check("...the 5x7 table stays (the TV's face is built from it), and so does the results tote that uses it",
           "const dotPatterns = {" in js and "function createDotDigit(char)" in js and "createDotMatrixText(" in js)
    _check("pi5 keeps the TV crawl's line and its weather: the model's chyron, the weather feed, the GET /api/weather route",
           bool(rig.model()["chyron"]) and callable(getattr(main, "feed_weather_to_board", None))
           and "/api/weather" in {rule.rule for rule in main.app.url_map.iter_rules()})


def _no_ticker_in_a_browser(picker_check, requests):
    rig = Rig()
    server = picker_check.Server(rig)
    chrome = picker_check.Chrome(touch=True, size=(1366, 1024))
    try:
        chrome.call("Page.addScriptToEvaluateOnNewDocument", source=(
            "window.__intervals = []; const __si = window.setInterval;"
            " window.setInterval = function (fn, ms, ...a) { window.__intervals.push([(fn && fn.name) || '', ms]);"
            " return __si.call(this, fn, ms, ...a); };"))
        t = time.monotonic()
        chrome.call("Page.navigate", url=server.url)
        chrome.wait_for("document.readyState === 'complete' && typeof showResultsModal === 'function'", timeout=30)
        chrome.wait_for("(() => { const s = document.getElementById('splash-screen'); return !s || s.style.display === 'none'; })()",
                        timeout=20)
        time.sleep(4.0)
        weather = sum(1 for at, m, p in requests if at >= t and p == "/api/weather")
        page = chrome.eval("JSON.stringify({ticker: document.querySelectorAll('.ticker-strip, #ticker-track').length,"
                           " intervals: window.__intervals, gap: (() => {"
                           " const h = document.querySelector('.hero-header').getBoundingClientRect();"
                           " const s = document.querySelector('.serape-band').getBoundingClientRect();"
                           " return Math.round((s.top - h.bottom) * 10) / 10; })()})")
        page = json.loads(page)
        names = [n for n, _ in page["intervals"]]
        _check("in a browser: no ticker element, no request for the weather in the first seconds",
               page["ticker"] == 0 and weather == 0, str((page["ticker"], weather)))
        _check("...no ticker timer: no 220 ms step, no second 1 s timer (the clock's alone), no 30-minute weather timer",
               not any(ms in (220, 1800000) for _, ms in page["intervals"])
               and sum(1 for _, ms in page["intervals"] if ms == 1000) == 1
               and not any(n in ("tickerTick", "buildTicker", "getWeather") for n in names), str(page["intervals"]))
        _check("...and the serape band sits right under the logo: the page closed up by the ticker's space, nothing else moved",
               page["gap"] == 0, str(page["gap"]))
        _check("...no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        chrome.close()
        server.close()


def test_no_ticker_in_a_browser():
    """The Control Center in headless Chrome: no ticker element, no ticker timer, no weather request; the serape
    band directly under the logo. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("no-ticker browser check skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
    _with_counted_requests(lambda requests, slow: _no_ticker_in_a_browser(picker_check, requests))


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main_():
    print(f"La Quiniela dashboard test\n  DB: {S._TMP_DB}")
    _run("menu — Race Setup out, the two La Quiniela links in", test_menu_and_page)
    _run("race info — /api/race is built from La Quiniela", test_race_roster_is_la_quinielas)
    _run("names — the field route on the real app", test_field_route_on_the_real_app)
    _run("one race state — the thirteen buttons carry their mode", test_buttons_carry_their_mode)
    _run("one race state — a dashboard mode moves La Quiniela, the LEDs as before", test_dashboard_modes_move_la_quiniela)
    _run("one race state — the results make it WINNER, RESET makes it AFTER_PARTY", test_results_and_reset_are_modes)
    _run("results — saved and WINNER with the LED controller down", test_results_stand_without_the_leds)
    _run("results — kept when pi5 starts", test_results_are_kept_when_pi5_starts)
    _run("results — all three, distinct and in the field, or a 400 and nothing saved (B9)", test_results_need_all_three)
    _run("results — the real finish: the next finishers when a horse had no bets at the post; the record (M5)",
         test_results_record_the_real_finish)
    _run("results — refused while the field cannot be read (a degraded store, no La Quiniela)",
         test_results_refused_while_the_field_cannot_be_read)
    _run("results — the field is frozen while a result stands: a scratch or an undo is refused", test_field_frozen_while_a_result_stands)
    _run("payout — a dead heat in the record, checked and paid; a tied horse with no bets drops out (M6)", test_results_with_a_dead_heat)
    _run("payout — a result before the post is refused; from AT THE POST on it is taken", test_results_refused_before_the_post)
    _run("payout — one reader of the record: GET /api/results and the spectator page read the board's", test_results_have_one_reader)
    _run("payout — the dialog reopens on the saved result, the cards only when needed, a dead heat; the admin page's RESULTS section",
         test_payout_on_the_pages_source)
    _run("La Subasta — pays the owners of the real 1st, 2nd and 3rd from SET WINNERS' record; a correction, RESET, Reset betting",
         test_la_subasta_pays_on_set_winners)
    _run("the wall — a state change never waits on it: the real client against a socket nothing answers on",
         test_state_change_never_waits_on_the_wall)
    _run("the wall — the race goes on without it: AUTO on its timers", test_race_goes_on_without_the_wall)
    _run("the wall — the page: two pills, one poll, a toast per transition (the source)", test_device_health_on_the_page_source)
    _run("the wall — the pills and the once-only toasts, in a browser", test_device_indicators_in_a_browser)
    _run("SET WINNERS — the page records the real finish; the field followed; RESET asks (the source)", test_picker_records_the_real_finish)
    _run("crawl — the weather reaches La Quiniela's model", test_weather_reaches_the_board)
    _run("SET WINNERS — the picker is a state machine only taps move", test_picker_is_a_state_machine_only_taps_move)
    _run("SET WINNERS — the figures at the post the picker reads, by horse number", test_bets_at_the_post_for_the_picker)
    _run("SET WINNERS — NO BETS marks from the frozen figures, a warning, CONFIRM still works", test_picker_marks_horses_nobody_bet)
    _run("SET WINNERS — the slot cards are the check: no list of the picks over CONFIRM", test_confirm_step_lists_no_picks)
    _run("admin page — the race state read-only, in a browser", test_admin_state_line_in_a_browser)
    _run("SET WINNERS — a tap lands in the slot meant, in a browser, mouse and touch", test_picker_in_a_browser)
    _run("iPad — the Home Screen metas on both pages, pinch zoom kept", test_home_screen_metas)
    _run("iPad — everything the host presses takes a finger, 44 px, in a browser", test_tap_targets_in_a_browser)
    _run("iPad — full screen, the links between the two pages open in place, in a browser", test_full_screen_links_in_a_browser)
    _run("admin page — one tap, one request; one scratch, one record, one reply; the rows reset", test_admin_one_tap_one_request_in_a_browser)
    _run("admin page — Scratch asks first: the question, the confirm step, only Confirm sends", test_admin_scratch_asks_first_in_a_browser)
    _run("Control Center — one tap, one send; a slow LED controller holds only the button tapped", test_control_center_one_tap_one_send_in_a_browser)
    _run("payout — SET WINNERS in a browser: the cards only when needed, the reopen on the saved result, a dead heat, the admin page's RESULTS, "
         "refused before the post", test_picker_payout_in_a_browser)
    _run("field order — SET WINNERS lists the field by program number and a tap records the tile's own number, in a browser",
         test_picker_grid_in_program_number_order_in_a_browser)
    _run("SET WINNERS cards — the name, the badge, the tie line and the button each have a place, in a browser",
         test_slot_cards_have_a_place_for_everything_in_a_browser)
    _run("scratch after the post — no replacement, only the ticked box, the tokens refunded; the admin lists in order, in a browser",
         test_admin_scratch_after_the_post_in_a_browser)
    _run("Reset betting — the admin page's question names the results, the figures at the post and the hand count, in a browser",
         test_reset_betting_names_what_it_clears_in_a_browser)
    _run("race-night guard — TEST, the reopen confirm, B1, CONFIRM RESULTS's message, AUTO, in a browser", test_race_night_guards_in_a_browser)
    _run("SET WINNERS — the picker follows a scratch while open; a stale dialog cannot save it; RESET clears a saved result, in a browser",
         test_picker_follows_the_field_in_a_browser)
    _run("renumber — a cup late on its pair: the admin page's ⏳ renumbering badge, in a browser", test_admin_renumbering_badge_in_a_browser)
    _run("renumber — item (z) in real time: the page open first, a ghost, the tag within a poll; a page left open across an update",
         test_admin_late_tag_in_real_time)
    _run("admin page — the number lists after Scratch and Undo; a typed #21 in the replacement name (Joey, DevPi, 2026-10-14)",
         test_admin_number_list_and_typed_number_in_a_browser)
    _run("pages come up fresh — no-store HTML, versioned static URLs, the Control Center reloads on a new revision",
         test_pages_come_up_fresh)
    _run("no ticker — gone from the page, the script and the stylesheet, with its weather fetch", test_no_ticker)
    _run("no ticker — no element, no timer, no request, in a browser", test_no_ticker_in_a_browser)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    S._drop_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_())

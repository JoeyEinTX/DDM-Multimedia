# la_quiniela/test_dashboard.py - the dashboard's side of La Quiniela
#
# Run with: python -m la_quiniela.test_dashboard  (from the pi5/ dir)
#
# The real app (pi5/main.py: every route, the real templates and static
# files) over a fresh bridge on test_smoke's fake serial port and temp
# database. No hardware and no network: the LED controller's client is
# stubbed (every command is recorded and answered OK), the tote board is
# off, and the results file lives in a temp directory. Same tiny runner as
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
from la_quiniela.test_smoke import FakeClock, MAC_A, MAC_B, _fresh_bridge, telem  # noqa: E402

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


def tmpdir():
    d = tempfile.mkdtemp(prefix="lq_dash_")
    _tmpdirs.append(d)
    return Path(d)


class Rig:
    """main's app over a fresh bridge and board; the hardware stubbed."""

    def __init__(self, led_ok=True):
        # A database of its own per rig. main.py's import left connections
        # open on test_smoke's temp file (La Subasta shares it), and Windows
        # does not let an open file be replaced, so that one cannot be made
        # fresh again; a new path always is.
        self.dir = tmpdir()
        S._TMP_DB = str(self.dir / "la_quiniela_dash.db")
        self.bridge, self.port, self.sio, self.clk = _fresh_bridge()
        self.bridge._open_port()
        init_la_quiniela(socketio=None, bridge=self.bridge)
        self.results_file = self.dir / "results.json"
        main.RESULTS_FILE = str(self.results_file)
        self.wall = FakeClock(1_700_000_000.0)
        self.board = init_board(bridge=self.bridge, log_dir=self.dir / "logs",
                                results_path=self.results_file, wall=self.wall)
        self.led = []                   # every command the LED controller was sent
        self.led_ok = led_ok

        def send_command(command):
            self.led.append(command)
            return ("OK:" + command) if self.led_ok else "ERROR:TIMEOUT"
        main.esp32.send_command = send_command
        main.tote = None                # tote_send() does nothing
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def post(self, path, body=None, **kw):
        return self.client.post(path, json=body, **kw) if body is not None else self.client.post(path, **kw)

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
    # A pull and a restart must serve the new CSS and JS: every link carries its file's modification time.
    import re
    for path in ("css/ddm_style.css", "js/ddm_control.js"):
        mtime = int(os.path.getmtime(os.path.join(main.app.static_folder, path)))
        _check(f"the page asks for /static/{path}?v=<its mtime>", f'/static/{path}?v={mtime}"' in html,
               str(re.findall(r'/static/[^"]*' + re.escape(path.split("/")[-1]) + r'[^"]*', html)))
        _check(f"...and the versioned URL serves it", rig.client.get(f"/static/{path}?v={mtime}").status_code == 200)


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
    _check("every button's mode is in pi5's table, and the table has no mode without a button",
           {mode for mode, _, _ in found} == set(MODE_STATES), str(set(MODE_STATES) ^ {m for m, _, _ in found}))
    js = rig.client.get("/static/js/ddm_control.js").get_data(as_text=True)
    _check("a mode button names its mode to pi5 before the LEDs answer",
           "const raceSet = mode ? setRaceMode(mode) : null;" in js and "'/api/quiniela/mode'" in js
           and js.index("const raceSet = mode ? setRaceMode(mode) : null;") < js.index("showNotification(withRaceState(`Animation: ${animName}`, race), 'success');"))
    _check("Standby and Test name theirs", "const raceSet = setRaceMode('STANDBY');" in js and "setRaceMode('TEST');" in js)
    _check("the table is pi5's, not repeated in the page", "MODE_STATES" not in js.replace("la_quiniela/betting.py MODE_STATES", "")
           and "BETTING_60: 1" not in js)
    _check("the race state is read back every 5 s and the ticker shows it",
           "setInterval(pollRaceMode, 5000);" in js and "raceStateLabel(raceModeInfo)" in js)


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
        r_mode = rig.post("/api/quiniela/mode", {"mode": mode})
        r_led = rig.post(route)
        _check(f"{mode}: the LEDs get {command} as before, La Quiniela state {MODE_STATES[mode]}",
               r_led.status_code == 200 and r_led.get_json()["success"] is True and rig.led == [command]
               and r_mode.get_json()["state"] == MODE_STATES[mode] and rig.bridge.phase == MODE_STATES[mode]
               and rig.model()["race_state"] == MODE_STATES[mode], f"{rig.led} {r_mode.get_json()}")
    _check("the LED routes alone never move the race state (a preview, the Animations list)",
           rig.post("/api/animation/BETTING_60").status_code == 200 and rig.bridge.phase == 5
           and rig.post("/api/led/all_off").status_code == 200 and rig.bridge.phase == 5)
    # The LED controller unreachable: the race state moves all the same.
    dead = Rig(led_ok=False)
    r_mode = dead.post("/api/quiniela/mode", {"mode": "BETTING_60"})
    r_led = dead.post("/api/animation/BETTING_60")
    _check("LED controller unreachable: the LED route says so, the race state is set",
           r_led.get_json()["success"] is False and r_mode.get_json()["ok"] is True and dead.bridge.phase == 1)


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
    _check("opening SET WINNERS moves nothing", rig.bridge.phase == 4 and rig.port.written == [] and rig.led == ["ANIM:RESULTS_ENTRY"])
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    body = r.get_json()
    _check("POST /api/results: success, the results as sent", r.status_code == 200 and body["success"] is True
           and body["results"] == {"win": 19, "place": 1, "show": 22}, str(body))
    _check("...the LED controller told after them (RESULTS:FINALIZE, which the page used to send) and it answered",
           rig.led == ["ANIM:RESULTS_ENTRY", "RESULTS:FINALIZE"] and body["leds"] == "ok"
           and body["response"] == "OK:RESULTS:FINALIZE", f"{rig.led} {body.get('leds')}")
    _check("...and the race state it set: WINNER, mode RESULTS",
           body["race"] == {"rev": rev + 1, "state": 5, "state_name": "WINNER", "mode": "RESULTS", "source": "dashboard",
                            "gateway_online": False}, str(body.get("race")))
    _check("one state line: WINNER with the results and the renumber pair, byte-exact",
           rig.lines() == [state_line(rev + 1, 5, [], [(9, 22)], [19, 1, 22])], str(rig.lines()))
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
    _check("one state line: AFTER_PARTY, the results cleared, the pair kept",
           rig.lines() == [state_line(rev + 2, 6, [], [(9, 22)])], str(rig.lines()))
    _check("the file is gone, the model's results are null", not rig.results_file.exists()
           and rig.model()["results"] is None)
    _check("the board keeps the TV in WINNER and hands it back in AFTER_PARTY",
           rig.model()["board_states"] == [1, 2, 3, 4, 5] and rig.model()["race_state"] == 6)
    _check("the LEDs went off as they always did on a reset", "LED:ALL_OFF" in rig.led)
    # The next race: WELCOME is PRE_RACE again
    rig.post("/api/quiniela/mode", {"mode": "WELCOME"})
    _check("WELCOME after the reset: PRE_RACE", rig.bridge.phase == 0)
    # Without a board the dashboard's routes still do their own work
    saved = main.get_board

    def no_board():
        raise RuntimeError("La Quiniela board not initialised")
    main.get_board = no_board
    try:
        r = rig.post("/api/results", {"win": 1, "place": 2, "show": 3})
        _check("La Quiniela unavailable: the results are still saved, race null",
               r.status_code == 200 and r.get_json()["success"] is True and r.get_json()["race"] is None
               and rig.results_file.exists())
        r = rig.post("/api/results/clear")
        _check("...and still cleared", r.status_code == 200 and r.get_json()["race"] is None and not rig.results_file.exists())
    finally:
        main.get_board = saved


# -----------------------------------------------------------------------------
# The results are facts about the race, not about the LEDs
# -----------------------------------------------------------------------------

def test_results_stand_without_the_leds():
    """SET WINNERS with the LED controller down: the results are saved,
    La Quiniela goes to WINNER with them in one state line, and the reply
    says the LEDs were unreachable."""
    rig = Rig(led_ok=False)
    rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
    rig.post("/api/quiniela/mode", {"mode": "FINISH"})
    rev = rig.bridge.state_rev
    rig.port.written.clear()
    rig.led.clear()
    r = rig.post("/api/results", {"win": 19, "place": 1, "show": 22})
    body = r.get_json()
    _check("LED controller down: 200, success, the results as sent, leds unreachable",
           r.status_code == 200 and body["success"] is True and body["results"] == {"win": 19, "place": 1, "show": 22}
           and body["leds"] == "unreachable" and body["response"].startswith("ERROR"), str(body))
    _check("...saved all the same, horse numbers in the file",
           json.loads(rig.results_file.read_text()) | {"timestamp": None} == {"win": 19, "place": 1, "show": 22, "timestamp": None})
    _check("...WINNER with the results in one state line, byte-exact",
           body["race"]["state"] == 5 and rig.lines() == [state_line(rev + 1, 5, [], [], [19, 1, 22])], str(rig.lines()))
    _check("...the TV's model has them", rig.model()["results"] == {"win": 19, "place": 1, "show": 22}
           and rig.model()["race_state"] == 5)
    _check("...the LED controller was tried once, after the results were saved", rig.led == ["RESULTS:FINALIZE"], str(rig.led))
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
    _check("the page says when the LEDs missed them, and the results stand",
           "data.leds === 'unreachable'" in js and "LEDs unreachable" in js)
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
           "resultsState.chosen" in active and "RESULT_SLOTS.find" in active and "|| null" in active
           and "const RESULT_SLOTS = ['win', 'place', 'show'];" in js)
    _check("the state has no 'next step' left to go stale", "resultsState.step" not in js and "step: 'win'" not in js)
    _check("the LEDs follow the picks, one call after another, never ahead of them",
           "function ledCall(" in js and "ledChain.then(" in js and region.count("fetch(") == 2
           and "ledCall('/api/cup/lock'" in pick and "ledCall('/api/cup/unlock'" in pick)
    _check("a tap is one click: no touch, pointer or mouse-down handler in the picker",
           re.search(r"touchstart|touchend|pointerdown|pointerup|mousedown|mouseup", region) is None
           and "btn.onclick = () => selectCup(post, horse);" in js)
    _check("a horse is in one slot: refused with a message, not moved",
           "is already ${RESULT_SLOT_NAMES[where]}" in pick and "resultsNote(" in pick)
    _check("the old go-back, which only unlocked a cup and stepped back, is gone", "resultsGoBack" not in js)
    _check("a stray tap beside the picker does not throw picks away; another device's results event waits while it is up",
           "event.target === resultsModal && !resultsPicksMade()" in js
           and "if (resultsModalOpen()) {" in _js_function(js, "function connectResultsStream()"))
    _check("the confirm waits briefly for the LED calls in flight, then sets the results whatever they did",
           "await ledDrain(3000);" in js and "win: winHorse" in js and "cup: post" in js)
    # the page: three tappable slots, each with its x, the note line, the warnings box over the confirm button
    _check("three slots you can tap, each with an x, and the note and warning elements",
           all(f'data-slot="{s}"' in html and f"chooseSlot('{s}')" in html and f"clearSlot('{s}')" in html for s in ("win", "place", "show"))
           and 'id="results-pick-note"' in html and 'id="results-confirm-warnings"' in html)
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
    rig.post("/api/quiniela/mode", {"mode": "BETTING_60"})
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
    _check("the figures are read as the picker opens, with the field, and only there",
           "Promise.all([loadQuinielaField(), loadBetsAtPost()])" in open_ and "resultsBets = bets;" in open_)
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
    _check("a pick nobody bet is warned about over CONFIRM RESULTS, word for word",
           "`#${horse} ${horseDisplayName(horse)}: nobody bet this horse, so it can't pay. `" in ui
           and "'Enter the next finisher instead.'" in ui and "warning.className = 'confirm-warning';" in ui
           and "warnings.appendChild(warning);" in ui)
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
           and ".slot-nobets {" in css and ".confirm-warning {" in css
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
    _check("the confirm step holds the warnings box and CONFIRM RESULTS, nothing else",
           re.findall(r'id="([^"]+)"', section) == ["results-confirm-section", "results-confirm-warnings", "results-confirm-btn"],
           str(re.findall(r'id="([^"]+)"', section)))
    ui = _js_function(js, "function updateResultsModalUI()")
    _check("...which is filled with the warnings only", "getElementById('results-confirm-warnings')" in ui
           and ui.count("appendChild(") == 1 and "warnings.appendChild(warning);" in ui)
    _check("an empty warnings box takes no room; CONFIRM RESULTS sits one card gap under SHOW, no divider",
           ".confirm-warnings:empty {\n    display: none;\n}" in css
           and ".results-modal-named .confirm-section-sidebar {\n    margin-top: 0;\n    padding-top: 0;\n    border-top: 0;\n}" in css
           and "margin-bottom: 14px;" in css[css.index(".confirm-warnings {"):][:200])
    name_rule = re.search(r"\n\.slot-horse-name \{(.*?)\}", css, re.S).group(1)
    warning_rule = re.search(r"\n\.confirm-warning \{(.*?)\}", css, re.S).group(1)
    _check("a slot card's name and a warning line wrap only between words",
           "overflow-wrap: normal;" in name_rule and "anywhere" not in name_rule and "overflow-wrap: normal;" in warning_rule
           and "word-break" not in name_rule + warning_rule)
    fit = _js_function(js, "function fitSlotName(el)")
    _check("...and a word too wide for the card shrinks, a pixel at a time, never below 12 px",
           "fitSlotName(name);" in _js_function(js, "function updateSlot(slot, horseNum)")
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
        t = time.monotonic()
        double_tap('#scratch-list .horse[data-horse="1"] [data-scratch]')
        chrome.wait_for("!!document.querySelector('#scratched-list .horse[data-horse=\"1\"]')", timeout=10)
        settle(0.6)
        _check(f"admin page {mode}: a double tap on Scratch sends one request", count(t, "POST", "/api/quiniela/scratch") == 1,
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
        t = time.monotonic()
        double_tap('#scratch-list .horse[data-horse="3"] [data-scratch]', gap=1.8)
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
                    ("PUT", "/api/quiniela/closes_at"), ("PUT", "/api/quiniela/counted_pot"), ("POST", "/api/quiniela/reset")):
            slow[key] = 0.6
        load()
        chrome.eval("(() => { const d = new Date(Date.now() + 3600e3); const p = (x) => String(x).padStart(2, '0');"
                    " document.getElementById('closes-input').value = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())"
                    " + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()); return 1; })()")
        actions = [("Names: Reload", "#names-reload", "GET", "/api/quiniela/horses"),
                   ("Names: Save names", "#names-save", "PUT", "/api/quiniela/horses"),
                   ("Race info: Save race info", "#race-save", "PUT", "/api/quiniela/race"),
                   ("Betting closes: Set", "#closes-set", "PUT", "/api/quiniela/closes_at"),
                   ("Betting closes: +15 min", '#closes button[data-min="15"]', "PUT", "/api/quiniela/closes_at"),
                   ("Betting closes: +30 min", '#closes button[data-min="30"]', "PUT", "/api/quiniela/closes_at"),
                   ("Betting closes: +60 min", '#closes button[data-min="60"]', "PUT", "/api/quiniela/closes_at"),
                   ("Betting closes: Clear", "#closes-clear", "PUT", "/api/quiniela/closes_at")]
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
                                         ("Counted pot: Clear", "#counted-clear", "PUT", "/api/quiniela/counted_pot"),
                                         ("Reset betting (its confirm accepted)", "#reset-betting", "POST", "/api/quiniela/reset")):
            t = time.monotonic()
            double_tap(sel)
            settle(1.0)
            _check(f"admin page {mode}: a double tap on {name} sends one request", count(t, method, path) == 1,
                   str(count(t, method, path)))
        _check(f"admin page {mode}: Reset betting asked once", chrome.eval("window.__confirms") == 1, str(chrome.eval("window.__confirms")))
        _check(f"admin page {mode}: no page errors", not chrome.page_errors(), "; ".join(chrome.page_errors())[:300])
    finally:
        slow.clear()
        chrome.close()
        server.close()


def test_admin_one_tap_one_request_in_a_browser():
    """The LQ admin page in headless Chrome at a 12.9-inch iPad Pro's 1366 x 1024 (pi5/tools/picker_check.py's
    Server and Chrome), with touch and with a mouse, every request counted where pi5 receives it and the
    actions' replies held back a moment after the work is done, as a busy pi5's are: a double tap on Scratch,
    Undo and every other action sends one request; a scratch makes one record and one reply line; a Scratch in
    flight across the 5 s refresh is sent once and scratches nobody else; after a Scratch and after an Undo
    every row's replacement fields are back to their defaults. Skipped where there is no Chrome."""
    picker_check = _picker_check()
    if not picker_check.available():
        _check("admin page browser checks skipped (no Chrome or Chromium: DDM_CHROME; or no simple_websocket)", True)
        return
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
        for touch in (True, False):
            _admin_one_tap(picker_check, touch, requests, slow)
    finally:
        main.app.wsgi_app = inner


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

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    S._drop_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_())

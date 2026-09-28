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
# makes it 6).

import io
import json
import os
import sys
import tempfile
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

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    S._drop_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_())

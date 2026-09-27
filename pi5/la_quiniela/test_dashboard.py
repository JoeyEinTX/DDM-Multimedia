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
# pickers, the race roster the splash display reads (/api/race), and the one
# race state (the dashboard's modes set La Quiniela's state; the results
# make it 5, RESET makes it 6).

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
from la_quiniela.blueprint import init_la_quiniela  # noqa: E402
from la_quiniela.board import get_board, init_board  # noqa: E402
from la_quiniela.test_smoke import FakeClock, MAC_A, MAC_B, _fresh_bridge, telem  # noqa: E402

_real_stdout = sys.stdout
sys.stdout = io.StringIO()          # main.py prints its banner lines at import
try:
    import main  # noqa: E402
finally:
    sys.stdout = _real_stdout

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
        self.bridge, self.port, self.sio, self.clk = _fresh_bridge()
        self.bridge._open_port()
        init_la_quiniela(socketio=None, bridge=self.bridge)
        self.dir = tmpdir()
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
    _check("...and has nothing of Race Setup left",
           not any(s in js for s in ("raceSetup", "race-setup", "toggleOddsPolling", "initPostTimeCountdown")))
    _check("the tote prints the name, HORSE n without one, never cut short",
           "createToteName(horseDisplayName(horse))" in js and "`HORSE ${n}`" in js and "results-name-fit" in js)
    _check("a pick carries the post (the LED cup) and the horse (the results)",
           "selectCup(post, horse)" in js and "cup: post" in js and "win: winHorse" in js)
    css = rig.client.get("/static/css/ddm_style.css").get_data(as_text=True)
    _check("the new CSS is there", all(s in css for s in (".winner-pick-btn", ".drawer-link", ".results-name-fit", ".slot-horse-name")))
    rules = {rule.rule for rule in main.app.url_map.iter_rules()}
    _check("the AI search route is gone", "/api/race-setup/ai-search" not in rules
           and rig.post("/api/race-setup/ai-search", {}).status_code == 404)
    _check("the store of post time and odds and its routes stay (the splash's roster slide and the spectator page read them)",
           {"/api/race-setup", "/api/race-setup/start-odds-polling", "/api/race-setup/stop-odds-polling",
            "/api/race-setup/odds-status", "/api/race"} <= rules)
    _check("/api/quiniela/field is registered", "/api/quiniela/field" in rules)


def test_race_roster_uses_la_quiniela_names():
    rig = Rig()
    saved = main.load_race_setup
    main.load_race_setup = lambda: {"race_name": "x", "post_time": "18:57",
                                    "horses": {"1": "An Old Name", "9": "Another"},
                                    "odds": {"1": "5-2", "9": "8-1", "20": "30-1", "22": "99-1"}}
    try:
        r = rig.client.get("/api/race")
        body = r.get_json()
        _check("no names in La Quiniela: no horses, state unknown, CORS header kept",
               r.status_code == 200 and body["horses"] == [] and body["race_state"] == "unknown"
               and r.headers.get("Access-Control-Allow-Origin") == "*", str(body))
        rig.client.put("/api/quiniela/horses", json={"text": NAMES_TEXT})
        body = rig.client.get("/api/race").get_json()
        _check("20 horses, La Quiniela's names as typed, the old Race Setup names ignored",
               [h["number"] for h in body["horses"]] == list(range(1, 21))
               and body["horses"][0] == {"number": 1, "name": "Dornoch", "odds": "5-2", "finish": None}
               and body["horses"][8]["name"] == "Encino", str(body["horses"][:2]))
        _check("the stored post time still comes through", body["post_time"] == "6:57 PM ET" and body["post_time_iso"].startswith("2026-05-02T18:57"))
        rig.post("/api/quiniela/scratch", {"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}})
        rig.post("/api/quiniela/scratch", {"horse": 20})
        body = rig.client.get("/api/race").get_json()
        numbers = [h["number"] for h in body["horses"]]
        _check("9 replaced by 22 and 20 scratched: the field in numeric order, 22 last, neither 9 nor 20",
               numbers == [1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 22], str(numbers))
        h22 = body["horses"][-1]
        _check("22 under its own number and name, without the odds of the post it took",
               h22 == {"number": 22, "name": "Ocelli", "odds": None, "finish": None}, str(h22))
        rig.client.put("/api/quiniela/horses", json={"3": {"name": ""}})
        body = rig.client.get("/api/race").get_json()
        _check("a horse with no name is not listed", 3 not in [h["number"] for h in body["horses"]])
    finally:
        main.load_race_setup = saved


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
# Entry point
# -----------------------------------------------------------------------------

def main_():
    print(f"La Quiniela dashboard test\n  DB: {S._TMP_DB}")
    _run("menu — Race Setup out, the two La Quiniela links in", test_menu_and_page)
    _run("names — /api/race lists La Quiniela's field", test_race_roster_uses_la_quiniela_names)
    _run("names — the field route on the real app", test_field_route_on_the_real_app)

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    S._drop_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_())

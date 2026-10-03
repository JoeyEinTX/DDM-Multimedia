#!/usr/bin/env python3
"""The Control Center's SET WINNERS picker, driven in headless Chrome.

The real dashboard (pi5/main.py: every route, the real template, CSS and JS) is
served over loopback, the LED controller stubbed with a latency you choose, and
Chrome is driven over the DevTools protocol with real input events: a mouse
session, and a touch session (a 1180 x 820 iPad-sized viewport with touch
emulation, where a tap is a touchstart, a touchend and then a click). The page's
own state is read back (`resultsState`, the three slots in the DOM).

Why it exists: a tap on a horse picks the slot the host meant, however slowly the
LED controller answers, whatever the page is doing in the background, and
whether a touch device fires a touch and a click or just one. The picker used to
decide "which slot does the next tap fill" only after the LED lock had come back,
so a second tap inside that window filled the first slot again (see
la_quiniela/test_dashboard.py and RACE_NIGHT.md section 8).

    cd pi5
    python tools/picker_check.py                  every scenario, mouse and touch
    python tools/picker_check.py --reproduce      the slot a second tap lands in, by
                                                  LED latency and gap between the taps
    python tools/picker_check.py --js OLD.js      run against another copy of ddm_control.js
                                                  (git show HEAD~1:pi5/static/js/ddm_control.js)
    python tools/picker_check.py --shots DIR      screenshots of the picker, for looking at

Needs Chrome or Chromium (DDM_CHROME names one); the DevTools socket is
simple_websocket's client, which python-engineio already pulls in for pi5's
Socket.IO. la_quiniela/test_dashboard.py runs `run_checks` when there is a Chrome.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

TRACE = bool(os.environ.get("PICKER_TRACE"))              # print each check as it is made
PI5_DIR = Path(__file__).resolve().parent.parent
if str(PI5_DIR) not in sys.path:
    sys.path.insert(0, str(PI5_DIR))

IPAD = (1180, 820)                        # an iPad's CSS pixels, landscape
HORSES = {"win": 7, "place": 3, "show": 12}          # the three the scenarios tap, in the order they should fill
OTHERS = (5, 9, 14)                       # horses for changes of mind (9 is also offered as post 9)


def find_chrome() -> Optional[str]:
    """DDM_CHROME if set, else the usual names on PATH (DevPi's chromium), else the usual
    Windows and macOS places. None when there is none."""
    env = os.environ.get("DDM_CHROME")
    if env:
        return env if (Path(env).exists() or shutil.which(env)) else None
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(path).exists():
            return path
    return None


def available() -> bool:
    try:
        import simple_websocket  # noqa: F401
    except ImportError:
        return False
    return find_chrome() is not None


# -----------------------------------------------------------------------------
# The page's server: main.app over loopback, the LED controller slow on purpose
# -----------------------------------------------------------------------------

class Server:
    """main.app (a Rig's: the temp database, the board, the LED client stubbed) on a
    loopback port. `led_latency` is how long every LED command takes to answer, in
    seconds, and `led_log` every command it was sent, with the time it arrived and the
    time it was answered."""

    def __init__(self, rig: Any, js: Optional[str] = None) -> None:
        import main                                       # the Rig has imported it, stubbed
        from werkzeug.serving import make_server
        self.main = main
        self.rig = rig
        self.led_latency = 0.0
        self.led_log: List[Tuple[float, str, float]] = []
        self.t0 = time.monotonic()
        self._saved = (main.esp32.send_command, main.WEATHER_API_KEY)
        main.WEATHER_API_KEY = ""                          # the page polls /api/weather: never call out
        main.esp32.send_command = self._send_command
        override = Path(js).read_bytes() if js else None

        def app(environ, start_response):
            if override is not None and environ.get("PATH_INFO") == "/static/js/ddm_control.js":
                start_response("200 OK", [("Content-Type", "application/javascript"),
                                          ("Content-Length", str(len(override))), ("Cache-Control", "no-store")])
                return [override]
            return main.app(environ, start_response)

        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self.httpd = make_server("127.0.0.1", 0, app, threaded=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/"

    def _send_command(self, command: str) -> str:
        arrived = time.monotonic() - self.t0
        time.sleep(self.led_latency)
        self.led_log.append((round(arrived, 3), command, round(time.monotonic() - self.t0, 3)))
        return "OK:" + command

    def led(self, prefix: str) -> List[str]:
        return [c for _, c, _ in self.led_log if c.startswith(prefix)]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.main.esp32.send_command, self.main.WEATHER_API_KEY = self._saved


# -----------------------------------------------------------------------------
# A Chrome of our own, over the DevTools protocol
# -----------------------------------------------------------------------------

class Chrome:
    def __init__(self, touch: bool = False, size: Tuple[int, int] = IPAD) -> None:
        import simple_websocket
        self._ws_module = simple_websocket
        self.touch = touch
        self.size = size
        self.tmp = Path(tempfile.mkdtemp(prefix="picker_chrome_"))
        port = self._free_port()
        cmd = [find_chrome(), "--headless=new", "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
               f"--window-size={size[0]},{size[1]}", f"--remote-debugging-port={port}", "--remote-allow-origins=*",
               "--user-data-dir=" + str((self.tmp / "profile").resolve()), "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.n = 0
        self.events: List[dict] = []
        ws_url = None
        for _ in range(80):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read().decode())
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.25)
        if ws_url is None:
            self.close()
            raise RuntimeError("Chrome did not come up")
        self.ws = simple_websocket.Client.connect(ws_url)
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=size[0], height=size[1], deviceScaleFactor=1, mobile=touch)
        if touch:
            self.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)

    @staticmethod
    def _free_port() -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def call(self, method: str, timeout: float = 30.0, **params: Any) -> dict:
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.ws.receive(timeout=max(0.05, deadline - time.monotonic()))
            if raw is None:
                continue
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)
        raise TimeoutError(method)

    def eval(self, expression: str, timeout: float = 30.0) -> Any:
        res = self.call("Runtime.evaluate", timeout=timeout, expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in res:
            raise RuntimeError("page error: " + json.dumps(res["exceptionDetails"])[:500])
        return res["result"].get("value")

    def wait_for(self, expression: str, timeout: float = 15.0, every: float = 0.05) -> Any:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = self.eval(expression)
            if value:
                return value
            time.sleep(every)
        raise TimeoutError("waiting for " + expression)

    def page_errors(self) -> List[str]:
        out = []
        for e in self.events:
            if e.get("method") == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append((d.get("exception") or {}).get("description") or d.get("text", "?"))
        return out

    # ---- real input ----
    def center(self, selector: str) -> Tuple[float, float]:
        x, y = self.eval("(() => { const el = document.querySelector(%s); if (!el) return null; el.scrollIntoView({block: 'center'});"
                         " const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()"
                         % json.dumps(selector)) or (None, None)
        if x is None:
            raise LookupError("no element " + selector)
        return x, y

    def tap_at(self, x: float, y: float) -> None:
        """One tap as the session's device makes it: a mouse click, or a touch that Chrome follows with its click."""
        if self.touch:
            self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y, "id": 0}])
            self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        else:
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
            self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    def tap(self, selector: str) -> None:
        self.tap_at(*self.center(selector))

    def shot(self, path: Path, clip: Optional[dict] = None) -> None:
        import base64
        args: Dict[str, Any] = {"format": "png"}
        if clip:
            args["clip"] = clip
        path.write_bytes(base64.b64decode(self.call("Page.captureScreenshot", **args)["data"]))

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)


# -----------------------------------------------------------------------------
# The picker, from the outside
# -----------------------------------------------------------------------------

STATE_JS = """JSON.stringify({
    win: resultsState.win, place: resultsState.place, show: resultsState.show,
    posts: resultsPosts,
    slots: {win: document.getElementById('slot-win').innerText, place: document.getElementById('slot-place').innerText,
            show: document.getElementById('slot-show').innerText},
    header: document.getElementById('results-modal-header').innerText,
    confirm: document.getElementById('results-confirm-section').style.display !== 'none'
})"""


class Picker:
    """A dashboard page in a Chrome session, with its SET WINNERS picker."""

    def __init__(self, server: Server, touch: bool) -> None:
        self.server = server
        self.chrome = Chrome(touch=touch)
        self.touch = touch
        self.loaded = False

    def close(self) -> None:
        self.chrome.close()

    def load(self) -> None:
        c = self.chrome
        c.call("Page.navigate", url=self.server.url)
        c.wait_for("document.readyState === 'complete' && typeof showResultsModal === 'function'", timeout=30)
        c.wait_for("(() => { const s = document.getElementById('splash-screen'); return !s || s.style.display === 'none'; })()", timeout=20)
        c.wait_for("typeof quinielaField !== 'undefined' && quinielaField !== null", timeout=20)
        self.loaded = True

    def open(self) -> None:
        """SET WINNERS, tapped like the host does it, with the LED controller quick for the moment."""
        if not self.loaded:
            self.load()
        c = self.chrome
        latency, self.server.led_latency = self.server.led_latency, 0.0
        try:
            # whatever the last scenario left: another device's reveal popup, the picker itself (closing it
            # shows a reveal that was waiting)
            reveal = "document.getElementById('results-reveal-modal').classList.contains('active')"
            if c.eval(reveal):
                c.eval("revealWinners(); 1")
            if c.eval("document.getElementById('results-modal').classList.contains('active')"):
                c.tap("#results-cancel-btn")
                c.wait_for("!document.getElementById('results-modal').classList.contains('active')", timeout=10)
                time.sleep(0.3)
            if c.eval(reveal):
                c.eval("revealWinners(); 1")
            c.tap("button[data-mode='RESULTS']")
            c.wait_for("document.getElementById('results-modal').classList.contains('active') && "
                       "document.querySelectorAll('.winner-pick-btn').length > 0", timeout=20)
        finally:
            self.server.led_latency = latency
        time.sleep(0.2)

    def horse(self, n: int) -> str:
        return f".winner-pick-btn[data-horse='{n}']"

    def tap_horse(self, n: int) -> None:
        self.chrome.tap(self.horse(n))

    def state(self) -> dict:
        return json.loads(self.chrome.eval(STATE_JS))

    def settle(self, seconds: float) -> None:
        time.sleep(seconds)


# -----------------------------------------------------------------------------
# --reproduce: where does the second tap land?
# -----------------------------------------------------------------------------

def reproduce(rig: Any, js: Optional[str] = None, latencies=(0.0, 0.15, 1.5, 5.0), gaps=(0.0, 0.1, 1.0, 5.0),
              modes=("mouse", "touch")) -> List[dict]:
    """Tap a WIN horse, wait `gap`, tap a PLACE horse, with every LED command taking `latency` to
    answer. A second tap that lands in WIN overwrites the first pick: the bug."""
    server = Server(rig, js)
    rows: List[dict] = []
    try:
        for mode in modes:
            picker = Picker(server, touch=(mode == "touch"))
            try:
                for latency in latencies:
                    for gap in gaps:
                        picker.open()
                        server.led_latency = latency
                        t0 = time.monotonic()
                        picker.tap_horse(HORSES["win"])
                        time.sleep(gap)
                        picker.tap_horse(HORSES["place"])
                        waited = time.monotonic() - t0
                        picker.settle(max(0.4, latency * 2 + 0.4 - waited))
                        state = picker.state()
                        ok = (state["win"], state["place"]) == (HORSES["win"], HORSES["place"])
                        rows.append({"mode": mode, "latency": latency, "gap": gap, "ok": ok, "win": state["win"],
                                     "place": state["place"], "show": state["show"], "slots": state["slots"],
                                     "locks": server.led("CUP:LOCK")})
                        server.led_latency = 0.0
                        server.led_log.clear()
            finally:
                picker.close()
    finally:
        server.close()
    return rows


def print_reproduction(rows: List[dict]) -> None:
    gaps = sorted({r["gap"] for r in rows})
    for mode in sorted({r["mode"] for r in rows}):
        print(f"\n{mode}: tap WIN horse {HORSES['win']}, wait the gap, tap PLACE horse {HORSES['place']}; "
              f"'ok' = WIN {HORSES['win']} / PLACE {HORSES['place']}, otherwise what WIN and PLACE hold")
        print("  LED answers in   " + "".join(f"gap {g * 1000:>5.0f} ms      " for g in gaps))
        for latency in sorted({r["latency"] for r in rows if r["mode"] == mode}):
            cells = []
            for g in gaps:
                r = next(x for x in rows if x["mode"] == mode and x["latency"] == latency and x["gap"] == g)
                cells.append(("ok" if r["ok"] else f"WIN {r['win']}, PLACE {r['place']}").ljust(17))
            print(f"  {latency * 1000:>7.0f} ms       " + "".join(cells))


# -----------------------------------------------------------------------------
# The scenarios: what the picker must do (run_checks)
# -----------------------------------------------------------------------------

Result = Tuple[str, bool, str]


class Ctx:
    """What a scenario works with: the picker, the server, and the results it adds to."""

    def __init__(self, picker: Picker, server: Server, slow: float, gaps: Tuple[float, ...]) -> None:
        self.p = picker
        self.server = server
        self.slow = slow                    # how long the LED controller takes when it is slow
        self.gaps = gaps                    # the gaps between taps the ordering scenario tries
        self.out: List[Result] = []

    def fresh(self, latency: float = 0.0) -> None:
        """A newly opened picker, and from now on every LED command takes `latency` seconds."""
        if TRACE:
            print("  ... opening the picker, LED answers in %.2f s" % latency, flush=True)
        self.p.open()
        self.server.led_log.clear()
        self.server.led_latency = latency

    def check(self, name: str, got: Any, want: Any) -> None:
        self.add(name, got == want, f"got {got!r}, wanted {want!r}")

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.out.append((name, bool(passed), detail))
        if TRACE:
            print(("  [OK] " if passed else "  [XX] ") + name, flush=True)

    def picks(self) -> Tuple[Any, Any, Any]:
        s = self.p.state()
        return s["win"], s["place"], s["show"]

    def locks(self) -> List[str]:
        return self.server.led("CUP:LOCK")


def s_order(c: Ctx) -> None:
    """WIN, PLACE, SHOW fill in that order, at any speed, however slowly the LEDs answer."""
    w, pl, sh = HORSES["win"], HORSES["place"], HORSES["show"]
    for gap in c.gaps:
        c.fresh(c.slow)
        c.p.tap_horse(w)
        time.sleep(gap)
        c.p.tap_horse(pl)
        time.sleep(gap)
        c.p.tap_horse(sh)
        c.p.settle(c.slow * 3 + 0.5)
        c.check(f"in order with {c.slow * 1000:.0f} ms LEDs and {gap * 1000:.0f} ms between taps: WIN, PLACE, SHOW",
                c.picks(), (w, pl, sh))
        c.check(f"  ... the LEDs lock exactly those three cups, gold, silver, bronze ({gap * 1000:.0f} ms)",
                c.locks(), [f"CUP:LOCK:{w}:255:215:0", f"CUP:LOCK:{pl}:192:192:192", f"CUP:LOCK:{sh}:205:127:50"])
    c.server.led_latency = 0.0


def s_refresh(c: Ctx) -> None:
    """Taps across a forced background refresh and a results event from another device."""
    w, pl, sh = HORSES["win"], HORSES["place"], HORSES["show"]
    c.fresh(c.slow)
    c.p.tap_horse(w)
    c.p.chrome.eval("pollRaceMode(); loadQuinielaField(); followServerResults(); 1")           # the page's own refreshes, now
    c.server.main.broadcast_sse("results", {"win": 1, "place": 2, "show": 4})                  # another device sets results
    time.sleep(0.25)
    c.p.tap_horse(pl)
    c.p.chrome.eval("pollRaceMode(); loadQuinielaField(); 1")
    time.sleep(0.25)
    c.p.tap_horse(sh)
    c.p.settle(c.slow * 3 + 0.5)
    c.check("taps across a forced refresh and another device's results event land in WIN, PLACE, SHOW", c.picks(), (w, pl, sh))
    reveal_js = "document.getElementById('results-reveal-modal').classList.contains('active')"
    c.check("  ... and the other device's reveal waits until the picker is closed", c.p.chrome.eval(reveal_js), False)
    c.p.chrome.tap("#results-cancel-btn")
    time.sleep(0.5)
    c.check("  ... and shows when it is", c.p.chrome.eval(reveal_js), True)
    c.server.led_latency = 0.0


def s_together(c: Ctx) -> None:
    """Touch and click firing together: one tap, one slot."""
    w, pl = HORSES["win"], HORSES["place"]
    c.fresh(c.slow)
    c.p.chrome.eval("""(() => { const el = document.querySelector(%s);
        const fire = (type, Ctor, init) => el.dispatchEvent(new Ctor(type, Object.assign({bubbles: true, cancelable: true}, init || {})));
        fire('pointerdown', PointerEvent, {pointerType: 'touch'}); fire('mousedown', MouseEvent);
        fire('pointerup', PointerEvent, {pointerType: 'touch'}); fire('mouseup', MouseEvent); fire('click', MouseEvent);
        fire('pointerdown', PointerEvent, {pointerType: 'mouse'}); fire('pointerup', PointerEvent, {pointerType: 'mouse'}); fire('click', MouseEvent);
        return 1; })()""" % json.dumps(c.p.horse(w)))
    c.p.settle(c.slow * 2 + 0.3)
    c.check("a tap that fires pointer, mouse and click events, twice over, fills WIN once and moves on to PLACE",
            c.picks(), (w, None, None))
    c.check("  ... one lock", c.locks(), [f"CUP:LOCK:{w}:255:215:0"])
    c.p.tap_horse(pl)
    c.p.settle(c.slow * 2 + 0.3)
    c.check("  ... and the next real tap is PLACE", c.picks(), (w, pl, None))
    c.server.led_latency = 0.0


def s_twice(c: Ctx) -> None:
    """A horse tapped twice, or already in another slot, is refused and the picker is as it was."""
    w, pl = HORSES["win"], HORSES["place"]
    c.fresh(c.slow)
    c.p.tap_horse(w)
    c.p.tap_horse(w)                                                  # a double tap, inside the LED's answer
    c.p.settle(c.slow + 0.4)
    c.check("a horse tapped twice at once fills WIN only", c.picks(), (w, None, None))
    c.p.tap_horse(w)                                                  # tapped again, later
    time.sleep(0.2)
    note = c.p.chrome.eval("(document.getElementById('results-pick-note') || {}).innerText || ''")
    c.check("a horse already in WIN, tapped for PLACE, is refused: WIN and PLACE as they were", c.picks(), (w, None, None))
    c.add("  ... with a plain message saying which slot has it", "WIN" in note.upper() and str(w) in note, f"note: {note!r}")
    c.p.tap_horse(pl)
    time.sleep(0.2)
    c.check("  ... and PLACE is still free for another horse", c.picks(), (w, pl, None))
    c.server.led_latency = 0.0


def s_change(c: Ctx) -> None:
    """Changing a filled slot on purpose, and nothing else being touched."""
    w, pl, sh = HORSES["win"], HORSES["place"], HORSES["show"]
    other = OTHERS[0]
    c.fresh(0.0)
    for n in (w, pl, sh):
        c.p.tap_horse(n)
        time.sleep(0.25)
    c.check("three picks, and nothing else is left to fill", c.picks(), (w, pl, sh))
    c.p.tap_horse(other)
    time.sleep(0.25)
    c.check("  ... a tap on a horse with all three set and no slot chosen is refused", c.picks(), (w, pl, sh))
    c.server.led_log.clear()
    c.p.chrome.tap(".result-slot[data-slot='place']")
    time.sleep(0.2)
    active = c.p.chrome.eval("[...document.querySelectorAll('.result-slot.is-active')].map(e => e.dataset.slot)")
    c.check("tapping PLACE makes it the active slot, and only it", active, ["place"])
    c.p.tap_horse(other)
    time.sleep(0.4)
    c.check("  ... the next horse replaces PLACE, WIN and SHOW untouched", c.picks(), (w, other, sh))
    c.check("  ... the replaced horse's cup unlocked, the new one's locked silver",
            [x for x in c.server.led("CUP:") if x != "CUP:UNLOCK:ALL"], [f"CUP:UNLOCK:{pl}", f"CUP:LOCK:{other}:192:192:192"])
    c.p.tap_horse(pl)                                                 # the old PLACE horse is free again: but all three are set
    time.sleep(0.2)
    c.check("  ... and the slot goes back to being unchosen: a stray tap does not overwrite another", c.picks(), (w, other, sh))
    shown = c.p.chrome.eval("document.getElementById('results-confirm-section').style.display")
    c.check("  ... with all three set the confirm step shows", shown != "none", True)
    summary = c.p.chrome.eval("(document.getElementById('results-confirm-summary') || {}).innerText || ''")
    flat = " ".join(line for line in summary.replace("\r", "").split("\n") if line.strip()).upper()
    c.add("  ... listing WIN, PLACE and SHOW with each horse's number and name",
          all(f in flat for f in ("WIN", "PLACE", "SHOW", str(w), str(other), str(sh))), f"summary: {summary!r}")


def s_clear(c: Ctx) -> None:
    """Clear one slot, then all three."""
    w, other, sh = HORSES["win"], OTHERS[0], HORSES["show"]
    c.fresh(0.0)
    for n in (w, other, sh):
        c.p.tap_horse(n)
        time.sleep(0.2)
    c.p.chrome.tap(".slot-clear[data-slot='win']")
    time.sleep(0.3)
    c.check("clearing WIN empties WIN only", c.picks(), (None, other, sh))
    c.p.tap_horse(w)
    time.sleep(0.3)
    c.check("  ... and the next horse fills it", c.picks(), (w, other, sh))
    c.p.chrome.tap("#results-reset-btn")
    time.sleep(0.3)
    c.check("RESET clears all three", c.picks(), (None, None, None))
    c.p.tap_horse(w)
    time.sleep(0.2)
    c.check("  ... and WIN is next", c.picks(), (w, None, None))


def s_backdrop(c: Ctx) -> None:
    """A tap beside the picker does not throw the picks away."""
    c.fresh(0.0)
    c.p.tap_horse(HORSES["win"])
    time.sleep(0.2)
    c.p.chrome.tap_at(20, 20)
    time.sleep(0.4)
    still = c.p.chrome.eval("document.getElementById('results-modal').classList.contains('active')")
    c.check("a tap on the dark backdrop with a pick made does not close the picker", still, True)


SCENARIOS: List[Tuple[str, Callable[[Ctx], None]]] = [
    ("order", s_order), ("refresh", s_refresh), ("together", s_together), ("twice", s_twice),
    ("change", s_change), ("clear", s_clear), ("backdrop", s_backdrop),
]


def run_checks(rig: Any, js: Optional[str] = None, modes=("mouse", "touch"), quick: bool = False) -> List[Result]:
    """Every scenario in each session; (name, passed, detail) for each. A scenario that cannot even run
    (an element it needs is not there) is one failure and the rest go on. quick: the shorter run the test
    suite makes: the LED controller's slow answer 0.4 s, two gaps, and in the touch session only the
    ordering and the touch-and-click scenarios."""
    results: List[Result] = []
    server = Server(rig, js)
    try:
        for mode in modes:
            picker = Picker(server, touch=(mode == "touch"))
            ctx = Ctx(picker, server, slow=0.4 if quick else 0.6, gaps=(0.0, 0.3) if quick else (0.0, 0.1, 0.3, 0.9))
            if quick and mode == "touch":
                ctx.gaps = (0.0,)
            try:
                for key, scenario in SCENARIOS:
                    if quick and mode == "touch" and key not in ("order", "together"):
                        continue
                    try:
                        scenario(ctx)
                    except Exception as exc:                          # noqa: BLE001 - a scenario that cannot run is a failure
                        ctx.add(f"scenario '{key}' ran", False, f"{type(exc).__name__}: {exc}"[:400])
                    finally:
                        server.led_latency = 0.0
                errors = picker.chrome.page_errors()
                ctx.add("no page errors", not errors, "; ".join(errors)[:300])
            finally:
                picker.close()
            results.extend((f"[{mode}] " + name, passed, detail) for name, passed, detail in ctx.out)
    finally:
        server.close()
    return results


# -----------------------------------------------------------------------------
# --shots: the picker, for looking at
# -----------------------------------------------------------------------------

def shots(rig: Any, directory: Path, js: Optional[str] = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    server = Server(rig, js)
    picker = Picker(server, touch=True)
    try:
        picker.open()
        picker.chrome.shot(directory / "1_open.png")
        picker.tap_horse(HORSES["win"])
        time.sleep(0.4)
        picker.chrome.shot(directory / "2_win_picked.png")
        picker.tap_horse(HORSES["place"])
        time.sleep(0.3)
        picker.tap_horse(HORSES["show"])
        time.sleep(0.5)
        picker.chrome.shot(directory / "3_all_three.png")
        try:
            picker.chrome.tap(".result-slot[data-slot='win']")
            time.sleep(0.4)
            picker.chrome.shot(directory / "4_changing_win.png")
        except LookupError:
            pass
        print("screenshots in", directory)
    finally:
        picker.close()
        server.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reproduce", action="store_true", help="the slot a second tap lands in, by LED latency and gap")
    ap.add_argument("--mode", choices=("mouse", "touch", "both"), default="both")
    ap.add_argument("--js", help="serve this file as ddm_control.js (another version of it)")
    ap.add_argument("--shots", help="write screenshots of the picker to this directory")
    args = ap.parse_args(argv)
    if not available():
        print("needs Chrome or Chromium (DDM_CHROME) and simple_websocket")
        return 2
    from la_quiniela import test_dashboard as D          # its Rig: the real app over a temp database, hardware stubbed
    rig = D.Rig()
    modes = ("mouse", "touch") if args.mode == "both" else (args.mode,)
    if args.shots:
        shots(rig, Path(args.shots), args.js)
        return 0
    if args.reproduce:
        print_reproduction(reproduce(rig, args.js, modes=modes))
        return 0
    results = run_checks(rig, args.js, modes=modes)
    failed = 0
    for name, passed, detail in results:
        print(("  [OK] " if passed else "  [XX] ") + name + ("" if passed else f"   -- {detail}"))
        failed += not passed
    print(f"\n{len(results) - failed} of {len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

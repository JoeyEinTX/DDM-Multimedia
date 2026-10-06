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
la_quiniela/test_dashboard.py and RACE_NIGHT.md section 8). It also checks the
NO BETS marks: a horse whose cup held no bets at the post (La Quiniela's figures
at the post, set up on the server through the rig) is dimmed and tagged, a pick of
it is warned about over CONFIRM RESULTS, and nothing is marked while the figures
are unknown.

It also has the iPad on race night, held in landscape: everything the host presses on
the Control Center and the LQ admin page is 44 x 44 px or more under a finger, AT THE
GATE and THEY'RE OFF! stand 8 px or more from any other mode button, and neither page
scrolls sideways (`tap_checks`); and the links between the two pages open in place
when a page runs full screen from the Home Screen (`link_checks`).

    cd pi5
    python tools/picker_check.py                  every scenario, mouse and touch
    python tools/picker_check.py --reproduce      the slot a second tap lands in, by
                                                  LED latency and gap between the taps
    python tools/picker_check.py --js OLD.js      run against another copy of ddm_control.js
                                                  (git show HEAD~1:pi5/static/js/ddm_control.js)
    python tools/picker_check.py --shots DIR      screenshots of the picker, for looking at
    python tools/picker_check.py --taps           the size of everything the host presses, in four
                                                  iPad landscape sizes, and the scroll each page needs

Needs Chrome or Chromium (DDM_CHROME names one); the DevTools socket is
simple_websocket's client, which python-engineio already pulls in for pi5's
Socket.IO. la_quiniela/test_dashboard.py runs `run_checks`, `tap_checks` and
`link_checks` when there is a Chrome.
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
EMPTY_CUP, NO_CUP = 4, 7                  # the NO BETS scenarios' figures at the post: 4's cup held nothing, 7 had no cup
NO_BETS = (EMPTY_CUP, NO_CUP)
REPLACEMENT = (9, 22, 3)                  # 22 runs for 9 (from post 9), and its cup held 3 bets


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
        self.fail: set = set()                            # paths answered 503, as if pi5 could not
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
            if environ.get("PATH_INFO") in self.fail:
                start_response("503 Service Unavailable", [("Content-Type", "text/plain"), ("Content-Length", "0")])
                return [b""]
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
        """The element's centre in the page's CSS pixels, which is where input events land, also when a mobile
        view has zoomed the page out (the dashboard at 820 px wide, iPad portrait, is drawn at 0.8)."""
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

# Every picker in the grid as the host sees it: its post and horse, whether it carries the NO BETS class and
# shows the tag (displayed, with its text), the tag's size and colour, and how bright its name is drawn.
MARKS_JS = """JSON.stringify([...document.querySelectorAll('.winner-pick-btn')].map((b) => {
    const tag = b.querySelector('.winner-pick-nobets');
    const cs = tag ? getComputedStyle(tag) : null;
    return {post: +b.dataset.post, horse: +b.dataset.horse, cls: b.classList.contains('no-bets'),
            shown: !!cs && cs.display !== 'none' && tag.innerText.trim() === 'NO BETS',
            font: cs ? parseFloat(cs.fontSize) : 0, bg: cs ? cs.backgroundColor : '',
            dim: parseFloat(getComputedStyle(b.querySelector('.winner-pick-name')).opacity)};
}))"""

# What the picker says about a pick nobody bet: the warning lines over CONFIRM RESULTS, and the slot cards that
# show the tag.
WARNINGS_JS = """({
    lines: [...document.querySelectorAll('#results-confirm-warnings .confirm-warning')].map((w) => w.innerText.trim()),
    slots: [...document.querySelectorAll('.result-slot')].filter((s) => s.querySelector('.slot-nobets')).map((s) => s.dataset.slot)
})"""

# The right column as laid out: the boxes of the modal, the three slot cards, the warning lines and CONFIRM RESULTS
# (the page's CSS pixels), what the confirm section says, any old list of picks, and every word of a slot card's name
# or a warning line that is broken across two lines.
COLUMN_JS = r"""JSON.stringify((() => {
    const box = (el) => { if (!el) return null; const b = el.getBoundingClientRect();
                          return {top: b.top, bottom: b.bottom, left: b.left, right: b.right, height: b.height}; };
    const q = (s) => document.querySelector(s);
    const broken = [];
    document.querySelectorAll('.slot-horse-name, .confirm-warning').forEach((el) => {
        const t = el.firstChild && el.firstChild.nodeType === 3 ? el.firstChild : null;
        if (!t) return;
        let i = 0;
        for (const w of t.data.split(' ')) {
            if (w) {
                const r = document.createRange(); r.setStart(t, i); r.setEnd(t, i + w.length);
                if (new Set([...r.getClientRects()].map((x) => Math.round(x.top))).size > 1) broken.push(w);
            }
            i += w.length + 1;
        }
    });
    const button = q('#results-confirm-btn');
    return {vh: innerHeight, modal: box(q('#results-modal .results-modal-content')), sidebar: box(q('.results-sidebar')),
            win: box(q('.result-slot[data-slot="win"]')), place: box(q('.result-slot[data-slot="place"]')),
            show: box(q('.result-slot[data-slot="show"]')),
            lines: [...document.querySelectorAll('#results-confirm-warnings .confirm-warning')].map(box),
            button: box(button), enabled: !button.disabled,
            section: q('#results-confirm-section').innerText.replace(/\s+/g, ' ').trim(),
            rows: document.querySelectorAll('.confirm-row, .confirm-summary').length, broken: broken};
})())"""


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
            # CONFIRM RESULTS shows the loader for a minimum time, and while it is up it takes the next tap
            c.wait_for("!document.getElementById('loader').classList.contains('show')", timeout=10)
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

    def marks(self) -> List[dict]:
        return json.loads(self.chrome.eval(MARKS_JS))

    def marked(self) -> List[int]:
        """The horses the grid tags NO BETS (the tag shown, not just the class), in post order."""
        return [m["horse"] for m in self.marks() if m["shown"]]

    def warnings(self) -> dict:
        return self.chrome.eval(WARNINGS_JS)

    def note(self) -> str:
        return self.chrome.eval("(document.getElementById('results-pick-note') || {}).innerText || ''")

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
    slots = c.p.state()["slots"]
    c.add("  ... the slot cards are the check: each shows its horse's number",
          all(str(n).zfill(2) in slots[s] for s, n in (("win", w), ("place", other), ("show", sh))), f"slots: {slots!r}")
    column = json.loads(c.p.chrome.eval(COLUMN_JS))
    c.check("  ... and the picks are not listed a second time over CONFIRM RESULTS (nobody's NO BETS here)",
            (column["rows"], column["section"]), (0, "CONFIRM RESULTS"))


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


# NO BETS: a horse whose cup held no bets at the post cannot pay (the empty-cup rule: the host enters the
# next finisher instead), so the picker marks it, from La Quiniela's figures at the post. The scenarios set
# those figures up on the server through the rig, as the night would: cups that report their horses and
# counts, then AT THE GATE.

def figures_at_post(server: Server) -> dict:
    """La Quiniela as the NO BETS scenarios want it: the 2024 field's names, 9 scratched with 22 Ocelli
    running for it from post 9, a cup with bets for every horse in the field but two (4's cup is empty, 7
    has no cup at all), then betting reopened (any older figures at the post go) and AT THE GATE, which
    takes the figures at the post from those cups. Returns the model's closing."""
    rig = server.rig
    # The rig's own module (test_dashboard, imported or run as __main__) for its names and its telem lines:
    # importing test_dashboard again would run its top level, which closes the bridge the rig is using.
    D = sys.modules[type(rig).__module__]
    telem = D.telem
    was, now, bets = REPLACEMENT
    rig.client.put("/api/quiniela/horses", json={"text": D.NAMES_TEXT})
    if not rig.model()["horses"][str(now)]["in_field"]:
        rig.post("/api/quiniela/scratch", {"horse": was, "replacement": {"number": now, "name": "Ocelli"}})
    rig.post("/api/quiniela/mode", {"mode": "BETTING_60"})
    for horse in [n for n in range(1, 21) if n not in (was, NO_CUP)] + [now]:
        count = 0 if horse == EMPTY_CUP else (bets if horse == now else 1 + horse % 4)
        rig.bridge.handle_raw_line(telem("A0:B7:65:77:00:%02X" % horse, horse=horse, count=count))
    rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})
    return rig.model()["closing"]


def no_figures(server: Server) -> None:
    """Betting open, so no figures at the post: they are taken at AT THE GATE and dropped when betting reopens."""
    server.rig.post("/api/quiniela/mode", {"mode": "BETTING_60"})


def warning_for(horse: int, name: str) -> str:
    return f"#{horse} {name}: nobody bet this horse, so it can't pay. Enter the next finisher instead."


def s_unknown(c: Ctx) -> None:
    """Unknown is not zero: with no figures at the post, or none to be had, nothing is marked and nothing says
    anything is wrong."""
    w, pl, sh = HORSES["win"], HORSES["place"], HORSES["show"]
    no_figures(c.server)
    c.fresh(0.0)
    c.check("no figures at the post (betting open): the picker holds none and tags no horse NO BETS",
            (c.p.chrome.eval("resultsBets"), c.p.marked()), (None, []))
    for n in (w, pl, sh):
        c.p.tap_horse(n)
        time.sleep(0.2)
    c.check("  ... three picks as ever", c.picks(), (w, pl, sh))
    c.check("  ... no slot card shows the tag, no warning over CONFIRM RESULTS, the note line is empty",
            (c.p.warnings(), c.p.note()), ({"lines": [], "slots": []}, ""))
    # Figures at the post on the server that the page cannot read (pi5 not answering the model): the same
    figures_at_post(c.server)
    c.server.fail.add("/api/quiniela")
    try:
        c.fresh(0.0)
        c.check("figures at the post the page cannot read: no marks", (c.p.chrome.eval("resultsBets"), c.p.marked()), (None, []))
        for n in (NO_CUP, pl, sh):
            c.p.tap_horse(n)
            time.sleep(0.2)
        c.check("  ... the horse nobody bet is picked like any other, with no warning",
                (c.picks(), c.p.warnings(), c.p.note()), ((NO_CUP, pl, sh), {"lines": [], "slots": []}, ""))
        shown = c.p.chrome.eval("(() => { const n = document.getElementById('notification');"
                                " return n.classList.contains('show') && n.classList.contains('error') ? n.textContent : ''; })()")
        c.check("  ... and no error notice", shown, "")
    finally:
        c.server.fail.discard("/api/quiniela")
        no_figures(c.server)


def s_marks(c: Ctx) -> None:
    """Figures at the post where 4's cup held nothing and 7 had no cup: exactly those two are tagged and dimmed."""
    closing = figures_at_post(c.server)
    bets = {int(k): v["tokens"] for k, v in closing["horses"].items()}
    c.check("the figures at the post: 4 and 7 held nothing, 22 (running for 9) held 3, a horse with bets more",
            (bets[EMPTY_CUP], bets[NO_CUP], bets[REPLACEMENT[1]], bets[HORSES["place"]] > 0), (0, 0, REPLACEMENT[2], True))
    c.fresh(0.0)
    marks = c.p.marks()
    c.check("exactly 4 and 7 are tagged NO BETS", [m["horse"] for m in marks if m["shown"]], list(NO_BETS))
    c.check("  ... and only they carry the class", [m["horse"] for m in marks if m["cls"]], list(NO_BETS))
    dim = {m["horse"]: m["dim"] for m in marks}
    c.add("  ... drawn dimmer than the rest", all(dim[n] <= 0.5 for n in NO_BETS)
          and all(v == 1 for h, v in dim.items() if h not in NO_BETS), str(dim))
    post9 = next(m for m in marks if m["post"] == REPLACEMENT[0])
    c.check("22 runs from post 9 and its cup held bets: not tagged", (post9["horse"], post9["shown"], post9["cls"]),
            (REPLACEMENT[1], False, False))
    tag = next(m for m in marks if m["horse"] == NO_CUP)
    c.add("a horse with no cup at all counts as no bets (7)", tag["shown"] and tag["cls"], str(tag))
    c.add("  ... the tag is red and big enough to read at arm's length (14 px or more)",
          tag["bg"] == "rgb(211, 47, 47)" and tag["font"] >= 14, str(tag))
    c.check("  ... nothing is picked and the note line is empty", (c.picks(), c.p.note()), ((None, None, None), ""))
    read = c.p.chrome.eval("JSON.stringify([betsAtPost(null), betsAtPost({pot: 3}), "
                           "betsAtPost({horses: {'1': {tokens: 2}, '2': {tokens: 'x'}, '3': {tokens: -1}, '4': {}}})])")
    none, no_horses, some = json.loads(read)
    c.check("the page's reading of closing: none, or no horses in it, is no figures (null); a horse missing, without a "
            "count or with a junk one held 0", (none, no_horses, some),
            (None, None, {str(n): (2 if n == 1 else 0) for n in range(1, 25)}))


def s_marks_frozen(c: Ctx) -> None:
    """The marks are the figures as the picker opened: a forced refresh, another device's results event and the
    figures going on the server move neither them nor the picks; the picker opened again reads them afresh."""
    first, second, third = HORSES["place"], EMPTY_CUP, HORSES["show"]
    figures_at_post(c.server)
    c.fresh(c.slow)
    c.p.tap_horse(first)
    c.p.tap_horse(second)                         # nobody bet 4: picked for PLACE all the same
    c.p.settle(c.slow * 2 + 0.3)
    c.check("a horse nobody bet can be picked: WIN 3, PLACE 4", c.picks(), (first, second, None))
    c.check("  ... and its slot card shows the tag", c.p.warnings()["slots"], ["place"])
    no_figures(c.server)                          # betting reopened from another device: the figures at the post go
    c.add("the figures at the post dropped on the server", c.server.rig.model()["closing"] is None)
    c.p.chrome.eval("pollRaceMode(); loadQuinielaField(); followServerResults(); 1")           # the page's own refreshes, now
    c.server.main.broadcast_sse("results", {"win": 1, "place": 2, "show": 5})                  # another device sets results
    time.sleep(0.4)
    c.p.chrome.eval("pollRaceMode(); loadQuinielaField(); 1")
    time.sleep(0.3)
    c.check("  ... after a forced refresh and another device's results event: still exactly 4 and 7 tagged",
            c.p.marked(), list(NO_BETS))
    c.check("  ... the picks and the slot card's tag as they were", (c.picks(), c.p.warnings()["slots"]),
            ((first, second, None), ["place"]))
    c.p.tap_horse(third)
    c.p.settle(c.slow * 2 + 0.3)
    c.check("  ... the next tap fills SHOW, and the marks stay", (c.picks(), c.p.marked()), ((first, second, third), list(NO_BETS)))
    c.check("  ... the warning over CONFIRM RESULTS names the horse nobody bet", c.p.warnings()["lines"],
            [warning_for(second, "CATCHING FREEDOM")])
    c.fresh(0.0)                                  # closed (another device's reveal dismissed) and opened again
    c.check("opened again, with no figures at the post now: no marks", c.p.marked(), [])
    c.server.led_latency = 0.0


def s_nobets_confirm(c: Ctx) -> None:
    """Horses nobody bet, picked anyway (7 for WIN, 4 for PLACE): their slot cards and the warnings say so,
    CONFIRM RESULTS stays on screen and enabled, and pressing it sets the results as picked."""
    sh = HORSES["show"]
    figures_at_post(c.server)
    c.fresh(0.0)
    try:
        for n in (NO_CUP, EMPTY_CUP, sh):
            c.p.tap_horse(n)
            time.sleep(0.25)
        c.check("7 (no cup) picked for WIN, 4 (an empty cup) for PLACE, then SHOW", c.picks(), (NO_CUP, EMPTY_CUP, sh))
        seen = c.p.warnings()
        c.check("  ... the WIN and PLACE slot cards show the tag", seen["slots"], ["win", "place"])
        c.check("  ... the warnings over CONFIRM RESULTS name both, word for word, in slot order", seen["lines"],
                [warning_for(NO_CUP, "HONOR MARIE"), warning_for(EMPTY_CUP, "CATCHING FREEDOM")])
        button = c.p.chrome.eval("(() => { const b = document.getElementById('results-confirm-btn'); const r = b.getBoundingClientRect();"
                                 " return {disabled: b.disabled, on_screen: r.height > 0 && r.top >= 0 && r.bottom <= innerHeight}; })()")
        c.check("  ... CONFIRM RESULTS is enabled and on screen without scrolling (the warnings make it taller)",
                button, {"disabled": False, "on_screen": True})
        c.p.chrome.tap("#results-confirm-btn")
        c.p.chrome.wait_for("!document.getElementById('results-modal').classList.contains('active')", timeout=10)
        saved = (c.server.rig.client.get("/api/results").get_json() or {}).get("results") or {}
        c.check("  ... and pressing it sets the results as picked", [saved.get(k) for k in ("win", "place", "show")],
                [NO_CUP, EMPTY_CUP, sh])
    finally:
        c.server.rig.post("/api/results/clear")   # as the other scenarios expect the server: no results...
        no_figures(c.server)                      # ...and no figures at the post


# The right column, all three slots filled, at the sizes it is seen at: Joey's report of 2026-10-05 (1554 x 1116: the
# picks listed a second time over CONFIRM RESULTS, COMMANDMEN / T broken there, the column crowded), the iPad both
# ways round and a short Safari view. The 2026 field's longest names on the horses picked, and the longest real name
# in the horse data (GRAND MO THE FIRST) on a horse nobody bet, so it is in a warning line too; once more in a wide
# font (a Pi's fallback sans is wider than Segoe UI).
COLUMN_SIZES = ((1554, 1116), (1180, 820), (820, 1180), (1180, 740))
COLUMN_NAMES = {4: "Grand Mo the First", 6: "Commandment", 7: "Danon Bourbon", 11: "Incredibolt",
                12: "Chief Wallabee", 15: "Emerging Market"}
COLUMN_FILLS = (((6, 15, 12), 0), ((7, 11, 6), 1), ((4, 7, 15), 2))      # the picks, and how many nobody bet
WIDE_FONT_CSS = "body, body * { font-family: Verdana, 'DejaVu Sans', sans-serif !important; }"
OVERSIZED_NAME = "Supercalifragilistic"                                   # one word, wider than a card at full size


def s_column(c: Ctx) -> None:
    """The slot cards are the check: no list of the picks over CONFIRM RESULTS; the cards, any warning lines and
    CONFIRM RESULTS stacked in order, evenly, none over another and none past the modal; CONFIRM on screen and
    enabled; no name broken inside a word."""
    figures_at_post(c.server)                                     # 4's cup empty, 7 with no cup
    c.server.rig.client.put("/api/quiniela/horses", json={str(n): {"name": v} for n, v in COLUMN_NAMES.items()})
    cases = [(size, picks, nobets, False) for size in COLUMN_SIZES for picks, nobets in COLUMN_FILLS]
    cases += [(size, COLUMN_FILLS[2][0], COLUMN_FILLS[2][1], True) for size in COLUMN_SIZES[:2]]
    try:
        for (width, height), picks, nobets, wide in cases:
            c.p.chrome.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1,
                            mobile=width <= 1180)
            c.fresh(0.0)
            if wide:
                c.p.chrome.eval("(() => { const s = document.createElement('style'); s.id = 'wide-font'; s.textContent = %s;"
                                " document.head.appendChild(s); return 1; })()" % json.dumps(WIDE_FONT_CSS))
            for n in picks:
                c.p.tap_horse(n)
                time.sleep(0.25)
            time.sleep(0.3)
            col = json.loads(c.p.chrome.eval(COLUMN_JS))
            lines = c.p.warnings()["lines"]
            at = f"{width}x{height}, {nobets} nobody bet" + (", a wide font" if wide else "")
            c.check(f"[{at}] picks {picks} in WIN, PLACE, SHOW; over CONFIRM RESULTS only the warnings, no list of the picks",
                    (c.picks(), col["rows"], len(lines), col["section"]), (picks, 0, nobets, " ".join(lines + ["CONFIRM RESULTS"])))
            stack = [col["win"], col["place"], col["show"]] + col["lines"] + [col["button"]]
            gaps = [round(b["top"] - a["bottom"], 1) for a, b in zip(stack, stack[1:])]
            c.add(f"  ... stacked in order, none over another, the gaps even (14 px between cards, CONFIRM and the warnings)",
                  all(g >= -0.5 for g in gaps) and all(abs(g - 14) <= 1 for g in gaps[:2] + [gaps[2], gaps[-1]])
                  and all(abs(g - 8) <= 1 for g in gaps[3:-1]), f"gaps {gaps}")
            m = col["modal"]
            c.add("  ... nothing past the modal's edge", all(b["left"] >= m["left"] - 0.5 and b["right"] <= m["right"] + 0.5
                                                            and b["top"] >= m["top"] - 0.5 and b["bottom"] <= m["bottom"] + 0.5
                                                            for b in stack + [col["sidebar"]]), f"modal {m}")
            b = col["button"]
            c.check("  ... CONFIRM RESULTS on screen and enabled", (b["top"] >= -0.5 and b["bottom"] <= col["vh"] + 0.5, col["enabled"]),
                    (True, True))
            if not nobets:
                c.add("  ... and with no warning the whole column is on screen, WIN's top to CONFIRM's bottom",
                      col["win"]["top"] >= -0.5 and b["bottom"] <= col["vh"] + 0.5, f"win {col['win']}, button {b}, vh {col['vh']}")
            c.check("  ... no name broken inside a word, in a slot card or a warning line", col["broken"], [])
            if wide:
                c.p.chrome.eval("document.getElementById('wide-font').remove(); 1")
        # A single word wider than a card (longer than any real name; the store takes up to 80 characters): it takes
        # a smaller size until it fits, whole.
        c.server.rig.client.put("/api/quiniela/horses", json={"11": {"name": OVERSIZED_NAME}})
        c.p.chrome.call("Emulation.setDeviceMetricsOverride", width=IPAD[0], height=IPAD[1], deviceScaleFactor=1, mobile=True)
        c.fresh(0.0)
        for n in (11, 6, 12):
            c.p.tap_horse(n)
            time.sleep(0.25)
        fit = json.loads(c.p.chrome.eval(
            "JSON.stringify((() => { const e = document.querySelector('#slot-win .slot-horse-name');"
            " const other = document.querySelector('#slot-place .slot-horse-name');"
            " return {text: e.textContent, size: parseFloat(getComputedStyle(e).fontSize),"
            " normal: parseFloat(getComputedStyle(other).fontSize), fits: e.scrollWidth <= e.parentElement.clientWidth + 0.5}; })())"))
        col = json.loads(c.p.chrome.eval(COLUMN_JS))
        c.check("a word wider than a slot card (longer than any real name) takes a smaller size until it fits, never broken",
                (fit["text"], fit["fits"], 12 <= fit["size"] < fit["normal"], col["broken"]), (OVERSIZED_NAME.upper(), True, True, []))
    finally:
        c.p.chrome.call("Emulation.setDeviceMetricsOverride", width=IPAD[0], height=IPAD[1], deviceScaleFactor=1, mobile=c.p.touch)
        no_figures(c.server)


SCENARIOS: List[Tuple[str, Callable[[Ctx], None]]] = [
    ("order", s_order), ("refresh", s_refresh), ("together", s_together), ("twice", s_twice),
    ("change", s_change), ("clear", s_clear), ("backdrop", s_backdrop),
    ("unknown", s_unknown), ("marks", s_marks), ("frozen", s_marks_frozen), ("confirm", s_nobets_confirm),
    ("column", s_column),
]
QUICK_TOUCH = ("order", "together", "confirm")     # what the suite's quick run does in the touch session


def run_checks(rig: Any, js: Optional[str] = None, modes=("mouse", "touch"), quick: bool = False) -> List[Result]:
    """Every scenario in each session; (name, passed, detail) for each. A scenario that cannot even run
    (an element it needs is not there) is one failure and the rest go on. quick: the shorter run the test
    suite makes: the LED controller's slow answer 0.4 s, two gaps, and in the touch session only the
    ordering, the touch-and-click and the NO BETS confirm scenarios (QUICK_TOUCH)."""
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
                    if quick and mode == "touch" and key not in QUICK_TOUCH:
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
# --taps: everything the host presses on race night, 44 px each way
# -----------------------------------------------------------------------------
# Joey runs the night from an iPad held in landscape, the Control Center and the LQ admin page open as two
# Safari tabs. A finger needs 44 x 44 px; AT THE GATE and THEY'RE OFF!, the two buttons that matter most, were
# 38 px tall and 6 px apart. A control passes when it is 44 px or more each way, its box with the padding (for a
# small round x, the invisible square around it, its ::after), and a finger lands on it: its centre and the
# middle of each side of a 44 px square centred on it hit the control (document.elementFromPoint, a pixel short
# of the edge: Chrome hit-tests on whole pixels), so one something covers fails too.

TAP_MIN = 44                                   # px each way, the least a finger needs
GAP_MIN = 8                                    # px from AT THE GATE and THEY'RE OFF! to any other mode button
TAP_SIZES = ((1180, 820), (1024, 768))         # the suite's: an iPad Air in landscape, and the floor
LANDSCAPE = ((1180, 820), (1080, 810), (1024, 768), (1180, 720))   # --taps: and a 9th gen, and Safari's bars showing
MODE_BUTTONS = (("WELCOME", "WELCOME"), ("TEST", "TEST"), ("STANDBY", "STANDBY"), ("BETTING_60", "60 MIN"),
                ("BETTING_30", "30 MIN"), ("FINAL_CALL", "FINAL CALL"), ("AT_THE_GATE", "AT THE GATE"),
                ("GATES_BURST", "THEY'RE OFF!"), ("CHAOS", "CHAOS"), ("FINISH", "FINISH"), ("RESULTS", "SET WINNERS"),
                ("HEARTBEAT_COOLDOWN", "HEARTBEAT"), ("RESET", "RESET"))
RACE_PAIR = ("AT_THE_GATE", "GATES_BURST")
# (where, control, selector, several): with `several` every match is measured and the smallest reported
TAPS: List[Tuple[str, str, str, bool]] = [
    ("Control Center", "menu", "#hamburger-btn", False),
    ("Control Center", "connected devices (the status icon)", "#footer-device", False),
    ("Control Center", "AUTO / MANUAL switch", "label.race-control-switch:has(#race-control-toggle)", False),
    ("Control Center", "RACE INFO switch", "label.race-control-switch:has(#race-info-toggle)", False),
    ("Control Center", "full screen (the corner button)", "#fullscreen-toggle", False),
] + [("Control Center", label, f".panels button[data-mode='{mode}']", False) for mode, label in MODE_BUTTONS] + [
    ("drawer", "close x", ".drawer-close", False),
    ("drawer", "items (La Quiniela Admin and 4 more)", ".drawer-item", True),
    ("SET WINNERS", "horse buttons (the grid)", ".winner-pick-btn", True),
    ("SET WINNERS", "slot cards", ".result-slot", True),
    ("SET WINNERS", "slot clear x", ".slot-clear", True),
    ("SET WINNERS", "CONFIRM RESULTS", "#results-confirm-btn", False),
    ("SET WINNERS", "RESET", "#results-reset-btn", False),
    ("SET WINNERS", "CANCEL", "#results-cancel-btn", False),
    ("SET WINNERS", "close x", "#results-modal .modal-close-tab", False),
    ("reveal", "REVEAL WINNERS (results set on another device)", ".btn-reveal-winners", False),
    ("admin page", "link to the Control Center", "#state-link", False),
    ("admin page", "Reset betting", "#reset-betting", False),
    ("admin page", "Counted pot field", "#counted-input", False),
    ("admin page", "Counted pot Save count", "#counted-save", False),
    ("admin page", "Counted pot Clear", "#counted-clear", False),
    ("admin page", "scratch: replacement number", "#scratch-list select", True),
    ("admin page", "scratch: replacement name", "#scratch-list input[data-repl-name]", True),
    ("admin page", "scratch: No replacement (its label)", "#scratch-list label:has(input[data-norepl])", True),
    ("admin page", "scratch: Scratch", "#scratch-list button[data-scratch]", True),
    ("admin page", "scratch: Undo", "#scratched-list button[data-undo]", True),
    ("admin page", "names Reload", "#names-reload", False),
    ("admin page", "names Save names", "#names-save", False),
    ("admin page", "race info Save race info", "#race-save", False),
    ("admin page", "close time field", "#closes-input", False),
    ("admin page", "close time Set", "#closes-set", False),
    ("admin page", "close time +15 / +30 / +60", "#closes button[data-min]", True),
    ("admin page", "close time Clear", "#closes-clear", False),
]
WHERE = ("Control Center", "drawer", "SET WINNERS", "reveal", "admin page")

TAP_JS = r"""((sel, several, min) => {
  const els = (several ? [...document.querySelectorAll(sel)] : [document.querySelector(sel)])
    .filter((e) => e && e.getClientRects().length);
  return els.map((el) => {
    el.scrollIntoView({block: 'center', inline: 'center'});
    const r = el.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const at = (x, y) => document.elementFromPoint(x, y);
    const on = (x, y) => { const h = at(x, y); return !!h && el.contains(h); };
    const d = min / 2 - 1;
    const probes = [[0, 0], [-d, 0], [d, 0], [0, -d], [0, d]];
    const miss = probes.find(([x, y]) => !on(cx + x, cy + y));
    // a small round x takes its taps on an invisible square, its ::after (absolute, inset around it)
    const a = getComputedStyle(el, '::after');
    const square = a.content !== 'none' && a.position === 'absolute' ? [parseFloat(a.width) || 0, parseFloat(a.height) || 0] : null;
    const what = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '')
      + (typeof e.className === 'string' && e.className ? '.' + e.className.split(' ')[0] : '');
    const top = miss && at(cx + miss[0], cy + miss[1]);
    return {w: r.width, h: r.height, tw: Math.max(r.width, square ? square[0] : 0), th: Math.max(r.height, square ? square[1] : 0),
            square: !!square, lands: !miss, covered: !miss ? '' : (top ? what(top) : 'off screen'),
            field: ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName), touch: getComputedStyle(el).touchAction};
  });
})"""
GAP_JS = r"""((pair) => {
  const btns = [...document.querySelectorAll('.panels button[data-mode]')];
  return pair.map((mode) => {
    const a = document.querySelector(`.panels button[data-mode='${mode}']`).getBoundingClientRect();
    return btns.filter((b) => b.dataset.mode !== mode).map((b) => {
      const r = b.getBoundingClientRect();
      const dx = Math.max(0, r.left - a.right, a.left - r.right), dy = Math.max(0, r.top - a.bottom, a.top - r.bottom);
      return [mode, b.dataset.mode, Math.round(Math.hypot(dx, dy) * 10) / 10];
    }).sort((x, y) => x[2] - y[2]).slice(0, 3);
  }).flat();
})"""
PAGE_JS = r"""(() => { const d = document.documentElement;
  const modes = [...document.querySelectorAll('.panels button[data-mode]')].map((b) => b.getBoundingClientRect());
  return {sw: d.scrollWidth, cw: d.clientWidth, sh: d.scrollHeight, vh: innerHeight,
          modes_bottom: modes.length ? Math.round(Math.max(...modes.map((r) => r.bottom + scrollY))) : null}; })()"""
MODAL_JS = r"""(() => { const m = document.getElementById('results-modal');
  return {sw: m.scrollWidth, cw: m.clientWidth, sh: m.scrollHeight, vh: m.clientHeight}; })()"""


def _measure(chrome: Chrome, where: str) -> List[dict]:
    rows = []
    for w, name, sel, several in TAPS:
        if w != where:
            continue
        found = chrome.eval("(%s)(%s, %s, %d)" % (TAP_JS, json.dumps(sel), json.dumps(several), TAP_MIN)) or []
        rows.append({"where": where, "name": name, "n": len(found),
                     "w": min((f["w"] for f in found), default=0.0), "h": min((f["h"] for f in found), default=0.0),
                     "tw": min((f["tw"] for f in found), default=0.0), "th": min((f["th"] for f in found), default=0.0),
                     "square": any(f["square"] for f in found),
                     "ok": bool(found) and all(f["tw"] >= TAP_MIN and f["th"] >= TAP_MIN and f["lands"] for f in found),
                     "covered": sorted({f["covered"] for f in found if f["covered"]}),
                     # a double tap on a button doesn't zoom the page (a field keeps its own double tap)
                     "zooms": bool(found) and any(not f["field"] and f["touch"] != "manipulation" for f in found)})
    return rows


def tap_targets(rig: Any, sizes=TAP_SIZES) -> List[dict]:
    """Every control the host presses on race night, at each size (landscape, touch): its size and whether a
    finger lands on it; the gaps around AT THE GATE and THEY'RE OFF!; and the scroll each page needs. The
    Control Center's main view, its drawer, SET WINNERS with three horses picked, the REVEAL WINNERS popup,
    and the LQ admin page with the betting closed (the counted pot shows) and a horse scratched (for Undo). A
    Chrome of its own for each size: one that has left a few Control Centers behind (kept for Back, their result
    streams open) has no connection to pi5 left (see link_checks)."""
    server = Server(rig)
    out = []
    try:
        for size in sizes:
            chrome = Chrome(touch=True, size=size)
            try:
                chrome.call("Page.navigate", url=server.url)
                chrome.wait_for("document.readyState === 'complete' && typeof showResultsModal === 'function'", timeout=30)
                chrome.wait_for("(() => { const s = document.getElementById('splash-screen');"
                                " return !s || s.style.display === 'none'; })()", timeout=20)
                chrome.wait_for("typeof quinielaField !== 'undefined' && quinielaField !== null", timeout=20)
                chrome.eval("window.scrollTo(0, 0); 1")
                page = chrome.eval(PAGE_JS)
                gaps = chrome.eval("(%s)(%s)" % (GAP_JS, json.dumps(RACE_PAIR)))
                rows = _measure(chrome, "Control Center")
                # each view opened and closed by script: whether a finger lands on its buttons is what is measured
                chrome.eval("openDrawer(); 1")
                chrome.wait_for("document.getElementById('drawer').classList.contains('open')", timeout=10)
                time.sleep(0.45)                                          # the drawer slides in for 0.3 s
                rows += _measure(chrome, "drawer")
                chrome.eval("closeDrawer(); 1")
                time.sleep(0.45)
                chrome.eval("showResultsModal(); 1")
                chrome.wait_for("document.getElementById('results-modal').classList.contains('active') && "
                                "document.querySelectorAll('.winner-pick-btn').length > 0", timeout=20)
                time.sleep(0.2)
                for n in HORSES.values():
                    chrome.eval("document.querySelector(\".winner-pick-btn[data-horse='%d']\").click(); 1" % n)
                    time.sleep(0.2)
                chrome.wait_for("[...document.querySelectorAll('.slot-clear')].filter((b) => b.getClientRects().length).length === 3",
                                timeout=10)
                modal = chrome.eval(MODAL_JS)
                rows += _measure(chrome, "SET WINNERS")
                chrome.eval("closeResultsModal(); 1")
                chrome.wait_for("!document.getElementById('results-modal').classList.contains('active')", timeout=10)
                time.sleep(0.3)
                chrome.eval("pendingResults = {win: %d, place: %d, show: %d}; showResultsRevealModal(); 1"
                            % (HORSES["win"], HORSES["place"], HORSES["show"]))
                chrome.wait_for("document.getElementById('results-reveal-modal').classList.contains('active')", timeout=10)
                time.sleep(0.5)
                rows += _measure(chrome, "reveal")
                chrome.eval("revealWinners(); 1")
                # the admin page: betting closed (AT THE GATE), so the counted pot shows; a horse scratched, for Undo
                rig.post("/api/quiniela/mode", {"mode": "AT_THE_GATE"})
                if not rig.model().get("scratches"):
                    rig.post("/api/quiniela/scratch", {"horse": 20})
                chrome.call("Page.navigate", url=server.url + "quiniela/admin")
                chrome.wait_for("document.readyState === 'complete' && !!document.querySelector('#scratch-list button[data-scratch]')"
                                " && !!document.querySelector('#scratched-list button[data-undo]')"
                                " && !document.getElementById('counted').hidden", timeout=20)
                time.sleep(0.3)
                admin = chrome.eval(PAGE_JS)
                rows += _measure(chrome, "admin page")
                out.append({"size": size, "rows": rows, "gaps": gaps, "page": page, "modal": modal, "admin": admin,
                            "errors": chrome.page_errors()})
            finally:
                chrome.close()
    finally:
        server.close()
    return out


def _px(v: float) -> str:
    return ("%.1f" % v).rstrip("0").rstrip(".")


def tap_checks(rig: Any, sizes=TAP_SIZES) -> List[Result]:
    """tap_targets as checks: at each size, everything the host presses on each view takes a finger (44 px
    each way), AT THE GATE and THEY'RE OFF! are 8 px or more from any other mode button, and neither page nor
    SET WINNERS scrolls sideways."""
    out: List[Result] = []
    for m in tap_targets(rig, sizes):
        tag = "[%dx%d] " % m["size"]
        for where in WHERE:
            rows = [r for r in m["rows"] if r["where"] == where]
            missing = [r["name"] for r in rows if not r["n"]]
            small = ["%s %s x %s%s" % (r["name"], _px(r["tw"]), _px(r["th"]),
                                       ", a finger lands on " + ", ".join(r["covered"]) if r["covered"] else "")
                     for r in rows if r["n"] and not r["ok"]]
            out.append((tag + f"{where}: every control the host presses there ({len(rows)}) found, {TAP_MIN} x {TAP_MIN} px or more under a finger",
                        not missing and not small, "; ".join((["not found: " + ", ".join(missing)] if missing else []) + small)))
            zooms = [r["name"] for r in rows if r["zooms"]]
            out.append((tag + f"{where}: touch-action manipulation on each (a double tap doesn't zoom, a pinch does)",
                        not zooms, ", ".join(zooms)))
        short = [f"{a} to {b} {g} px" for a, b, g in m["gaps"] if g < GAP_MIN]
        out.append((tag + f"AT THE GATE and THEY'RE OFF! {GAP_MIN} px or more from any other mode button",
                    len(m["gaps"]) == 2 * 3 and not short, "; ".join(short) or str(m["gaps"])))
        sideways = [name for name, p in (("Control Center", m["page"]), ("SET WINNERS", m["modal"]), ("admin page", m["admin"]))
                    if p["sw"] > p["cw"]]
        out.append((tag + "no sideways scroll: the Control Center, SET WINNERS, the admin page", not sideways,
                    ", ".join(sideways)))
        out.append((tag + "no page errors", not m["errors"], "; ".join(m["errors"])[:300]))
    return out


def print_taps(measured: List[dict]) -> None:
    for m in measured:
        p, a, mo = m["page"], m["admin"], m["modal"]
        print("\n=== %d x %d, landscape, touch ===" % m["size"])
        print("  %-15s %-50s %-24s %s" % ("where", "control", "w x h (px)", "44 px each way"))
        for r in m["rows"]:
            size = "%s x %s" % (_px(r["w"]), _px(r["h"])) if r["n"] else "-"
            if r["square"]:                                               # drawn smaller, taps on a square
                size = "%s x %s (drawn %s)" % (_px(r["tw"]), _px(r["th"]), size)
            several = " (%d)" % r["n"] if r["n"] > 1 else ""
            verdict = ("yes" if r["ok"] else "NOT FOUND" if not r["n"]
                       else "NO" + (" (a finger lands on %s)" % ", ".join(r["covered"]) if r["covered"] else ""))
            print("  %-15s %-50s %-24s %s" % (r["where"], r["name"] + several, size, verdict))
        print("  gaps: " + ", ".join(f"{x} to {y} {g} px" for x, y, g in m["gaps"]))
        print("  Control Center: %d px wide in %d (%s), %d px tall in %d; the lowest mode button ends at %d (%s)"
              % (p["sw"], p["cw"], "no sideways scroll" if p["sw"] <= p["cw"] else "SCROLLS SIDEWAYS", p["sh"], p["vh"],
                 p["modes_bottom"], "on the first screen" if p["modes_bottom"] <= p["vh"] else "scroll to reach it"))
        print("  SET WINNERS: %d px wide in %d (%s), %d px tall in %d (%s)"
              % (mo["sw"], mo["cw"], "no sideways scroll" if mo["sw"] <= mo["cw"] else "SCROLLS SIDEWAYS", mo["sh"], mo["vh"],
                 "fits" if mo["sh"] <= mo["vh"] else "scrolls"))
        print("  admin page: %d px wide in %d (%s), %d px tall in %d"
              % (a["sw"], a["cw"], "no sideways scroll" if a["sw"] <= a["cw"] else "SCROLLS SIDEWAYS", a["sh"], a["vh"]))
        if m["errors"]:
            print("  page errors: " + "; ".join(m["errors"]))


# -----------------------------------------------------------------------------
# Full screen from the Home Screen: the links between the two pages open in place
# -----------------------------------------------------------------------------
# A Home Screen shortcut runs a page full screen (the web-app metas on both pages): no tabs, no second window,
# so the links between the Control Center and the admin page (data-in-place-standalone) drop their target there,
# when navigator.standalone or the display-mode: standalone media query says so, and a tap replaces the page in
# place. Replaces, not stacks: a Control Center kept for Back holds its result streams open (its own and its
# spectator preview's), and in Chrome the fourth trip back to it found no connection to pi5 left. Headless Chrome
# is not an iPad: here navigator.standalone is forced true, or the media query answered yes, before the page's
# scripts run. What iPadOS does with the shortcut is checked on the iPad.

STANDALONE_JS = "Object.defineProperty(Navigator.prototype, 'standalone', {configurable: true, get: () => true});"
DISPLAY_MODE_JS = r"""(() => { const real = window.matchMedia.bind(window);
  window.matchMedia = (q) => /display-mode:\s*standalone/.test(q) ? {matches: true, media: q, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; }}
    : real(q); })();"""
# (the link, the page it is on, its selector, its target and rel as built, the path it goes to)
CROSS_LINKS = (("the Control Center's link to the admin page (the drawer)", "/", "a.drawer-link[href='/quiniela/admin']",
                "_blank", "noopener", "/quiniela/admin"),
               ("the admin page's link to the Control Center", "/quiniela/admin", "#state-link",
                "ddm-control-center", None, "/"))
ROUND_TRIPS = 4                                # the fourth trip back stalled when the pages stacked
LINK_JS = "(() => { const a = document.querySelector(%s); return a && [a.getAttribute('target'), a.getAttribute('rel')]; })()"
ANSWERS_JS = """(async () => { const c = new AbortController(); const k = setTimeout(() => c.abort(), 6000);
  try { const r = await fetch('/api/quiniela', {signal: c.signal, cache: 'no-store'}); await r.text(); return r.ok; }
  catch (e) { return false; } finally { clearTimeout(k); } })()"""


def _tabs(chrome: Chrome) -> int:
    return sum(t["type"] == "page" for t in chrome.call("Target.getTargets")["targetInfos"])


def _wait_tabs(chrome: Chrome, n: int, timeout: float) -> int:
    end = time.monotonic() + timeout
    while _tabs(chrome) != n and time.monotonic() < end:
        time.sleep(0.1)
    time.sleep(0.3)
    return _tabs(chrome)


def _arrive(chrome: Chrome, path: str, timeout: float = 20.0) -> bool:
    """The page at `path` loaded and ready to tap (the Control Center's splash gone); False after `timeout` s."""
    ready = ("location.pathname === %s && document.readyState === 'complete' && (() => {"
             " const s = document.getElementById('splash-screen'); return !s || s.style.display === 'none'; })()"
             % json.dumps(path))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if chrome.eval(ready, timeout=5.0):
                return True
        except (RuntimeError, TimeoutError):                  # between two documents, or a page that hangs
            pass
        time.sleep(0.05)
    return False


def _tap_link(chrome: Chrome, page: str, sel: str) -> None:
    chrome.call("Page.bringToFront")                          # a tab the last tap opened went in front
    if page == "/":                                           # the Control Center's is in the drawer
        chrome.tap("#hamburger-btn")
        chrome.wait_for("document.getElementById('drawer').classList.contains('open')", timeout=10)
        time.sleep(0.45)
    chrome.tap(sel)


def link_checks(rig: Any) -> List[Result]:
    """The links between the two pages, each case in a Chrome of its own. In a browser tab: the target as built,
    and a tap opens a second tab while the page stays (a named one is reused by the next tap). Full screen by the
    display-mode query: no target, and a tap replaces the page in place. Full screen by navigator.standalone:
    ROUND_TRIPS round trips by taps, each page in place without a second tab or a history entry, pi5 answering
    to the end. A case that cannot run is one failure and the rest go on."""
    out: List[Result] = []
    errors: List[str] = []
    server = Server(rig)

    def case(label: str, script: Optional[str], body: Callable[[Chrome], None]) -> None:
        chrome = Chrome(touch=True)
        try:
            if script:
                chrome.call("Page.addScriptToEvaluateOnNewDocument", source=script)
            body(chrome)
            errors.extend(chrome.page_errors())
        except Exception as exc:                              # noqa: BLE001 - a case that cannot run is a failure
            out.append((f"{label}: ran", False, f"{type(exc).__name__}: {exc}"[:300]))
        finally:
            chrome.close()

    def open_page(chrome: Chrome, page: str) -> None:
        chrome.call("Page.navigate", url=server.url + page.lstrip("/"))
        if not _arrive(chrome, page):
            raise TimeoutError("the page did not load: " + page)

    try:
        for name, page, sel, target, rel, dest in CROSS_LINKS:
            def in_a_tab(chrome: Chrome, name=name, page=page, sel=sel, target=target, rel=rel) -> None:
                how = "in a browser tab"
                open_page(chrome, page)
                got = chrome.eval(LINK_JS % json.dumps(sel))
                out.append((f"{name}, {how}: target {target}" + (f" rel {rel}" if rel else "") + " as built",
                            got == [target, rel], str(got)))
                _tap_link(chrome, page, sel)
                n = _wait_tabs(chrome, 2, 10.0)
                stayed = chrome.eval("location.pathname")
                out.append((f"{name}, {how}: a tap opens a second tab and this page stays",
                            n == 2 and stayed == page, f"{n} tabs, this page at {stayed}"))
                if target != "_blank":
                    _tap_link(chrome, page, sel)
                    out.append((f"{name}, {how}: a second tap goes back to that tab ({target}), no third",
                                _wait_tabs(chrome, 2, 2.0) == 2, f"{_tabs(chrome)} tabs"))

            def full_screen(chrome: Chrome, name=name, page=page, sel=sel, dest=dest) -> None:
                how = "full screen (display-mode: standalone)"
                open_page(chrome, page)
                got = chrome.eval(LINK_JS % json.dumps(sel))
                history = chrome.eval("history.length")
                out.append((f"{name}, {how}: no target", got == [None, None], str(got)))
                _tap_link(chrome, page, sel)
                went = _arrive(chrome, dest)
                after = (_wait_tabs(chrome, 1, 2.0), chrome.eval("history.length", timeout=5.0) if went else None)
                out.append((f"{name}, {how}: a tap replaces this page with {dest}, no second tab",
                            went and after == (1, history), f"at {dest}: {went}; tabs, history {after} (was {history})"))

            case(f"{name}, in a browser tab", None, in_a_tab)
            case(f"{name}, full screen (display-mode: standalone)", DISPLAY_MODE_JS, full_screen)

        # navigator.standalone: back and forth by taps, as the host would all night
        def round_trips(chrome: Chrome) -> None:
            open_page(chrome, "/")
            history = chrome.eval("history.length")
            trips: List[tuple] = []
            for trip in range(ROUND_TRIPS):
                for name, page, sel, target, rel, dest in CROSS_LINKS:
                    try:
                        got = chrome.eval(LINK_JS % json.dumps(sel), timeout=5.0)
                        _tap_link(chrome, page, sel)
                        went = _arrive(chrome, dest)
                        trips.append((trip + 1, dest, got == [None, None], went,
                                      went and chrome.eval(ANSWERS_JS, timeout=10.0), _tabs(chrome),
                                      chrome.eval("history.length", timeout=5.0) if went else None))
                    except (RuntimeError, TimeoutError, LookupError) as exc:
                        trips.append((trip + 1, dest, None, False, False, None, f"{type(exc).__name__}: {exc}"[:120]))
                    if not trips[-1][3]:
                        break
                if not trips[-1][3]:
                    break
            bad = [t for t in trips if not (t[2] and t[3] and t[4] and t[5] == 1 and t[6] == history)]
            out.append((f"full screen (navigator.standalone): {ROUND_TRIPS} round trips by taps, Control Center to admin page "
                        "and back: each page in place, its link without a target, no second tab, no history entry, pi5 answering",
                        len(trips) == 2 * ROUND_TRIPS and not bad,
                        "trip, at, no target, arrived, pi5 answers, tabs, history (was %s): %s" % (history, bad or trips[-1:])))

        case("full screen (navigator.standalone), round trips", STANDALONE_JS, round_trips)
        out.append(("no page errors", not errors, "; ".join(errors)[:300]))
    finally:
        server.close()
    return out


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
        figures_at_post(server)                   # 4's cup empty, 7 with no cup: NO BETS on both
        picker.open()
        picker.chrome.shot(directory / "5_no_bets.png")
        for n in (NO_CUP, HORSES["place"], HORSES["show"]):
            picker.tap_horse(n)
            time.sleep(0.3)
        time.sleep(0.3)
        picker.chrome.shot(directory / "6_no_bets_picked.png")
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
    ap.add_argument("--taps", action="store_true", help="the size of everything the host presses, iPad landscape")
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
    if args.taps:
        print_taps(tap_targets(rig, LANDSCAPE))
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

#!/usr/bin/env python3
"""
Fake pi5 — run the real splash server against a synthetic pi5.

A dev aid for the La Quiniela board. It serves a fake of pi5's three
``/api/quiniela`` routes on ``--pi5-port`` (default 5078) and, unless
``--no-splash``, points ``config.PI5_URL`` at it, starts the splash's real
``Pi5Link`` and serves the REAL splash app (``server.py``: every route,
template and the slideshow) on ``--host:--port`` (default 127.0.0.1:5077).
Nothing leaves loopback: no serial port, no dashboard poller.

    cd splash_display
    python tools/fake_pi5.py --phase open          # BETTING OPEN, static rich data
    python tools/fake_pi5.py --phase final         # FINAL CALL (banner pulses)
    python tools/fake_pi5.py --phase closed        # AT_THE_POST: BETTING CLOSED, frozen
    python tools/fake_pi5.py --phase running       # RUNNING: BETTING CLOSED, still frozen
    python tools/fake_pi5.py --phase winner        # state 5: the board yields to the playlist
    python tools/fake_pi5.py --phase idle          # state 0: playlist, link up
    python tools/fake_pi5.py --phase cycle         # idle -> open -> final -> closed -> running -> winner, forever
    python tools/fake_pi5.py --phase open --stop-feed-after 3   # board up, then pi5 gone: NO LINK mark

Flags:
    --phase {idle,open,final,closed,running,winner,cycle}   (default: open)
    --period SECONDS       seconds per state in --phase cycle (default: 15)
    --stop-feed-after N    after N s the fake pi5 stops answering: its stream
                           closes and GET /api/quiniela (and POST .../cmd)
                           return 503, so the splash's link_ok drops within
                           ~7.5 s and the board shows the NO LINK mark. Give
                           the splash a few seconds to connect first: with 0
                           the fake is already gone before the splash's first
                           request, the splash keeps its empty model
                           (board_states []) and no board ever appears
    --pi5-port PORT        the fake pi5 (default: 5078)
    --port PORT            the real splash app (default: 5077)
    --host HOST            bind address for both servers (default: 127.0.0.1)
    --no-splash            serve only the fake pi5; point a splash at it with
                           config.PI5_URL = "http://127.0.0.1:5078"

Both URLs are printed; open http://127.0.0.1:5077/display. The fake's
``POST /api/quiniela/cmd`` answers ``{"ok": true, "echo": "<cmd>"}`` and
``state N`` also switches the scenario's phase, so through the real relay

    curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'

puts the board up on the TV and ``state 5`` takes it down again.

Static phases carry 20 cups (cup n on horse n), tokens spread with a clear
leader (horse 7), one scratched horse (13), one offline cup (horse 11) and a
handful of recent events already in the model. ``cycle`` walks the race
states about every 15 s with tokens climbing while betting is open, and
keeps a slow trickle of bets going in AT_THE_POST so the freeze rule is
visible.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

HERE = Path(__file__).resolve().parent.parent      # splash_display/
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402

from flask import Flask, Response, jsonify, request  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

PHASES = {
    "idle":    0,   # PRE_RACE
    "open":    1,   # BETTING_OPEN
    "final":   2,   # FINAL_CALL
    "closed":  3,   # AT_THE_POST
    "running": 4,   # RUNNING
    "winner":  5,   # WINNER
}

RACE_STATE_NAMES = {
    0: "PRE_RACE", 1: "BETTING_OPEN", 2: "FINAL_CALL", 3: "AT_THE_POST",
    4: "RUNNING", 5: "WINNER", 6: "AFTER_PARTY",
}
BOARD_STATES = [1, 2, 3, 4]
TOKEN_VALUE = 1.0
HEARTBEAT_S = 5.0        # pi5's SSE ping cadence
MAX_EVENTS = 8

# Tokens per horse for the static phases: sums to 147 (POT $147), horse 7
# a clear leader. Horse 13 is scratched (and had no bets), horse 11's cup
# has gone quiet.
BASE_TOKENS = {
    1: 9,  2: 4,  3: 14, 4: 2,  5: 7,
    6: 11, 7: 31, 8: 6,  9: 3,  10: 12,
    11: 1, 12: 8, 13: 0, 14: 5, 15: 6,
    16: 2, 17: 6, 18: 9, 19: 4, 20: 7,
}
SCRATCHED = {13}
OFFLINE = {11}
# The last few bets, oldest first; they become the model's events.
SEED_EVENTS = [7, 3, 15, 7, 10, 7, 18]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def build_model(
    phase: int,
    tokens: Dict[int, int],
    scratched: Iterable[int] = SCRATCHED,
    offline: Iterable[int] = OFFLINE,
    events: Iterable[Dict[str, Any]] = (),
    updated: Optional[float] = None,
    link_ok: bool = True,
) -> Dict[str, Any]:
    """The contract's model: share 4 dp, pot 2 dp, leader = strictly most
    tokens (lowest horse on a tie, None when nothing is bet), cup numbers
    1-based (cup n on horse n), events newest first, at most 8."""
    scratched, offline = set(scratched), set(offline)
    counts = {n: max(0, int(tokens.get(n, 0))) for n in range(1, 21)}
    total = sum(counts.values())
    horses: Dict[str, Dict[str, Any]] = {}
    leader: Optional[int] = None
    best = 0
    for n in range(1, 21):
        t = counts[n]
        horses[str(n)] = {
            "tokens": t,
            "share": round(t / total, 4) if total else 0.0,
            "scratched": n in scratched,
            "online": n not in offline,
            "cup": n,
        }
        if t > best:
            leader, best = n, t
    return {
        "link_ok": bool(link_ok),
        "race_state": int(phase),
        "race_state_name": RACE_STATE_NAMES.get(int(phase), f"STATE_{int(phase)}"),
        "token_value": float(TOKEN_VALUE),
        "pot": round(total * TOKEN_VALUE, 2),
        "total_tokens": total,
        "horses": horses,
        "leader": leader,
        "events": list(events)[:MAX_EVENTS],
        "updated": float(updated if updated is not None else time.time()),
        "board_states": list(BOARD_STATES),
    }


def seed_events(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """SEED_EVENTS as model events, newest first, half a second apart."""
    now = time.time() if now is None else now
    n = len(SEED_EVENTS)
    return [
        {"horse": h, "delta": 1, "ts": round(now - 0.5 * (n - i), 3)}
        for i, h in reversed(list(enumerate(SEED_EVENTS)))
    ]


class FakePi5:
    """The scenario (phase, tokens, scratched, offline cups, events) and the
    three routes on a Flask app of its own."""

    def __init__(self, phase: int) -> None:
        self._lock = threading.Lock()
        self._subs: List["queue.Queue[Optional[str]]"] = []
        self.phase = phase
        self.tokens: Dict[int, int] = dict(BASE_TOKENS)
        self.scratched: Set[int] = set(SCRATCHED)
        self.offline: Set[int] = set(OFFLINE)
        self.events: List[Dict[str, Any]] = seed_events()
        self.stopped = False
        self._json = _dumps(self._build())
        self.app = self._make_app()

    # -- scenario ------------------------------------------------------------
    def _build(self) -> Dict[str, Any]:
        return build_model(self.phase, self.tokens, self.scratched, self.offline, self.events)

    def _publish_locked(self) -> None:
        self._json = _dumps(self._build())
        for q in self._subs:
            try:
                q.put_nowait(self._json)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(self._json)
                except queue.Full:
                    pass

    def set_phase(self, phase: int) -> None:
        with self._lock:
            if phase != self.phase:
                self.phase = int(phase)
                self._publish_locked()

    def reset(self, tokens: Dict[int, int], phase: Optional[int] = None) -> None:
        """A fresh token table with no events (the start of a cycle)."""
        with self._lock:
            self.tokens = dict(tokens)
            self.events = []
            if phase is not None:
                self.phase = int(phase)
            self._publish_locked()

    def bump(self, horse: int, delta: int = 1) -> None:
        """One bet: tokens and an event, newest first."""
        with self._lock:
            self.tokens[horse] = max(0, self.tokens.get(horse, 0) + delta)
            self.events = ([{"horse": horse, "delta": delta, "ts": round(time.time(), 3)}]
                           + self.events)[:MAX_EVENTS]
            self._publish_locked()

    def stop_feed(self) -> None:
        """Stop answering: every open stream ends, GET/POST answer 503."""
        with self._lock:
            self.stopped = True
            for q in self._subs:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def model_json(self) -> str:
        with self._lock:
            return self._json

    # -- routes --------------------------------------------------------------
    def _sse(self) -> Iterator[str]:
        q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=32)
        with self._lock:
            self._subs.append(q)
            first = self._json
        try:
            yield "data: " + first + "\n\n"
            while True:
                try:
                    item = q.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    if self.stopped:
                        return
                    yield ": heartbeat\n\nevent: ping\ndata: " + _dumps({"ts": round(time.time(), 3)}) + "\n\n"
                    continue
                if item is None:          # stop_feed(): the stream closes
                    return
                yield "data: " + item + "\n\n"
        finally:
            with self._lock:
                if q in self._subs:
                    self._subs.remove(q)

    def _make_app(self) -> Flask:
        app = Flask("fake_pi5")
        fake = self

        def gone():
            return jsonify({"ok": False, "error": "fake pi5 stopped (--stop-feed-after)"}), 503

        @app.route("/api/quiniela")
        def api_quiniela():
            if fake.stopped:
                return gone()
            return Response(fake.model_json(), mimetype="application/json")

        @app.route("/api/quiniela/stream")
        def api_quiniela_stream():
            if fake.stopped:
                return gone()
            return Response(
                fake._sse(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        @app.route("/api/quiniela/cmd", methods=["POST"])
        def api_quiniela_cmd():
            if fake.stopped:
                return gone()
            body = request.get_json(silent=True)
            cmd = body.get("cmd") if isinstance(body, dict) else None
            if not isinstance(cmd, str):
                return jsonify({"ok": False, "error": "cmd must be a string"}), 400
            text = cmd.strip()
            if not text:
                return jsonify({"ok": False, "error": "empty command"}), 400
            words = text.split()
            if words[0] == "state" and len(words) == 2 and words[1].isdigit():
                fake.set_phase(int(words[1]))
            print(f"fake pi5: cmd {text!r}", flush=True)
            return jsonify({"ok": True, "echo": text})

        return app


# ---------------------------------------------------------------------------
# Feeders
# ---------------------------------------------------------------------------
def run_cycle(fake: FakePi5, period: float) -> None:
    rng = random.Random(7)
    bettable = [n for n in BASE_TOKENS if n not in SCRATCHED]
    while True:
        tokens = {n: (0 if n in SCRATCHED else max(0, t // 3)) for n, t in BASE_TOKENS.items()}
        fake.reset(tokens, PHASES["idle"])
        plan = [
            # (phase, every-Nth-second a bet lands; 0 = none)
            ("idle",    0),
            ("open",    1),
            ("final",   2),
            ("closed",  4),   # a trickle in AT_THE_POST exercises the freeze rule
            ("running", 0),
            ("winner",  0),
        ]
        for name, every in plan:
            st = PHASES[name]
            print(f"-> {name} (state {st})", flush=True)
            fake.set_phase(st)
            for i in range(int(period)):
                if fake.stopped:
                    return
                if every and i % every == 0:
                    fake.bump(rng.choice(bettable))
                time.sleep(1.0)


def stop_feed_later(fake: FakePi5, after: float) -> None:
    time.sleep(max(0.0, after))
    fake.stop_feed()
    print("feed stopped: streams closed, GET answers 503; the splash's link_ok drops in ~7.5 s", flush=True)


def _client_host(host: str) -> str:
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=sorted(PHASES) + ["cycle"], default="open")
    ap.add_argument("--period", type=float, default=15.0, help="seconds per state in --phase cycle")
    ap.add_argument("--stop-feed-after", type=float, default=None, metavar="SECONDS",
                    help="stop answering after this long (the splash's link_ok then drops); "
                         "use a few seconds, e.g. 3: with 0 the splash never receives a model")
    ap.add_argument("--pi5-port", type=int, default=5078, help="the fake pi5 (default 5078)")
    ap.add_argument("--port", type=int, default=5077, help="the real splash app (default 5077)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-splash", action="store_true", help="serve only the fake pi5")
    args = ap.parse_args()

    phase = PHASES["idle"] if args.phase == "cycle" else PHASES[args.phase]
    fake = FakePi5(phase)
    pi5_httpd = make_server(args.host, args.pi5_port, fake.app, threaded=True)
    threading.Thread(target=pi5_httpd.serve_forever, name="fake-pi5", daemon=True).start()
    pi5_url = f"http://{_client_host(args.host)}:{args.pi5_port}"
    print(f"fake pi5: phase={args.phase} pid={os.getpid()} {pi5_url}/api/quiniela", flush=True)

    if args.stop_feed_after is not None:
        threading.Thread(target=stop_feed_later, args=(fake, args.stop_feed_after), daemon=True).start()
    if args.phase == "cycle":
        threading.Thread(target=run_cycle, args=(fake, args.period), daemon=True).start()

    if args.no_splash:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            pi5_httpd.shutdown()
            pi5_httpd.server_close()
        return

    # The real splash, pointed at the fake BEFORE quiniela/server are imported
    # (quiniela's module-level link reads config.PI5_URL when it is built).
    config.PI5_URL = pi5_url
    import quiniela  # noqa: E402
    import race_poller  # noqa: E402
    race_poller._started = True      # never poll joeydevpi.local from here
    import server  # noqa: E402
    quiniela.start_link()            # idempotent: server's import already did it

    httpd = make_server(args.host, args.port, server.app, threaded=True)
    splash_url = f"http://{_client_host(args.host)}:{args.port}"
    print(f"splash: {splash_url}/api/quiniela (Pi5Link -> {pi5_url})", flush=True)
    print(f"open {splash_url}/display", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        pi5_httpd.shutdown()
        pi5_httpd.server_close()


if __name__ == "__main__":
    main()

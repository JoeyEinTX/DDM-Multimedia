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
    python tools/fake_pi5.py --phase bench         # the 2026-09-25 bench picture: 50/42/8/3 tokens
    python tools/fake_pi5.py --phase bench-reset   # bench, then a reset (counts 0, events cleared, state 0), then state 1 again; repeats
    python tools/fake_pi5.py --phase redesign      # the board_reference.html picture: Derby 2024 field, two replacement
                                                   # scratches, POT $154, closes in 15 min, a bet on horse 7 every 4 s (toast)
    python tools/fake_pi5.py --phase redesign-static   # the same picture, nothing moving (screenshots)

Flags:
    --phase {idle,open,final,closed,running,winner,cycle,bench,bench-reset,redesign,redesign-static}   (default: open)
    --period SECONDS       seconds per state in --phase cycle, and per step in
                           --phase bench-reset (default: 15)
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
``POST /api/quiniela/cmd`` answers ``{"ok": true, "echo": "<cmd>"}``;
``state N`` switches the scenario's phase and ``reset`` plays pi5's reset
(counts 0, events cleared, state 0), so through the real relay

    curl -s -X POST localhost:5077/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'

puts the board up on the TV and ``state 5`` takes it down again; ``scratch C 1``
/ ``scratch C 0`` flips the kind-2 scratch on cup C (its tokens leave the pot);
``name N Some Long Name`` renames horse N (names_rev bumps, no event; ``name N``
alone clears it), which is how to watch a long name shrink to fit its row.

Static phases carry 20 cups (cup n on horse n), tokens spread with a clear
leader (horse 7), one scratched horse (13), one offline cup (horse 11) and a
handful of recent events already in the model; they carry no names, so the
board shows HORSE n. ``cycle`` walks the race states about every 15 s with
tokens climbing while betting is open, and keeps a slow trickle of bets
going in AT_THE_POST so the freeze rule is visible.

The model carries every key of pi5's contract, including the additive ones
the redesigned board reads: ``now`` (stamped when the JSON is built),
``closes_at``, ``prizes`` (whole dollars, place and show rounded half up,
win the remainder), ``split``, ``chyron``, ``names_rev``, ``scratches`` and
``horses[n].name`` / ``replaced``. A kind-2 scratch (no replacement) keeps
its tokens in ``total_tokens`` but out of ``pot`` and the prizes.
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
from decimal import ROUND_HALF_UP, Decimal
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
# pi5's LQ_SPLIT_WIN / _PLACE / _SHOW and LQ_CHYRON_LINES, as the model serves them.
SPLIT = {"win": 0.60, "place": 0.25, "show": 0.15}
CHYRON_LINES = [
    "TOTALS BASED ON CHEAP CHINESE ELECTRONICS · FINAL RESULTS HAND COUNTED",
    "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS",
]

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
# The picture from the first DevPi run (2026-09-25): a few horses in play,
# nothing scratched, every cup online. 50 fills its bar, 42 sits at 84 %,
# 8 at 16 %, 3 at 6 % (bars are relative to the leader, not to the pot).
BENCH_TOKENS = {3: 50, 2: 42, 7: 8, 1: 3}
BENCH_EVENTS = [3, 2, 3, 7, 1]
# The last few bets, oldest first; they become the model's events.
SEED_EVENTS = [7, 3, 15, 7, 10, 7, 18]

# --phase redesign: tools/board_reference.html's picture. The 2024 Derby
# field in post order, two replacement scratches (the number stays live, the
# old name goes to `replaced`), the reference's counts (POT $154 -> WIN $92,
# PLACE $39, SHOW $23), betting closes in 15 minutes.
DERBY_2024 = [
    "Dornoch", "Sierra Leone", "Mystik Dan", "Catching Freedom", "Catalytic",
    "Just Steel", "Honor Marie", "Just a Touch", "Encino", "T O Password",
    "Forever Young", "Track Phantom", "West Saratoga", "Endlessly", "Domestic Product",
    "Grand Mo the First", "Fierceness", "Stronghold", "Resilience", "Society Man",
]
REDESIGN_REPLACED = {9: "Encino", 14: "Endlessly"}
REDESIGN_NAMES = {n: name for n, name in enumerate(DERBY_2024, 1)}
REDESIGN_NAMES.update({9: "Epic Ride", 14: "Mugatu"})
REDESIGN_TOKENS = {
    1: 11, 2: 18, 3: 14, 4: 6,  5: 0,
    6: 3,  7: 23, 8: 9,  9: 7,  10: 2,
    11: 16, 12: 12, 13: 4, 14: 0, 15: 1,
    16: 0, 17: 19, 18: 5, 19: 0, 20: 4,
}
REDESIGN_EVENTS = [2, 17, 7]          # oldest first
REDESIGN_CLOSES_IN_S = 15 * 60
REDESIGN_BUMP_EVERY_S = 4.0
REDESIGN_BUMP_HORSE = 7


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def round_half_up(x: float) -> int:
    """pi5's rounding for the prizes: Decimal ROUND_HALF_UP, never round()
    (banker's rounding gives 38 for 38.5; the prize is $39)."""
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def prizes_for(pot: float) -> Dict[str, int]:
    """Whole dollars that always sum to the pot: place and show rounded
    half up, win the remainder. 154 -> 92/39/23, 1 -> 1/0/0, 0 -> 0/0/0."""
    place = round_half_up(pot * SPLIT["place"])
    show = round_half_up(pot * SPLIT["show"])
    win = round_half_up(pot - place - show)
    return {"win": win, "place": place, "show": show}


def build_model(
    phase: int,
    tokens: Dict[int, int],
    scratched: Iterable[int] = SCRATCHED,
    offline: Iterable[int] = OFFLINE,
    events: Iterable[Dict[str, Any]] = (),
    updated: Optional[float] = None,
    link_ok: bool = True,
    names: Optional[Dict[int, str]] = None,
    replaced: Optional[Dict[int, str]] = None,
    closes_at: Optional[float] = None,
    names_rev: int = 0,
    chyron: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The contract's model: share 4 dp, pot 2 dp, leader = strictly most
    tokens (lowest horse on a tie, None when nothing is bet), cup numbers
    1-based (cup n on horse n), events newest first, at most 8. A kind-2
    scratch (scratched, no replacement) keeps its tokens in total_tokens
    and share's denominator but out of the pot and the prizes. Names are
    served upper-cased ("" when unset); `replaced` is the upper-cased name
    of the horse this number replaced, or None. `now` is stamped here, when
    the model is built."""
    scratched, offline = set(scratched), set(offline)
    names = names or {}
    replaced = replaced or {}
    counts = {n: max(0, int(tokens.get(n, 0))) for n in range(1, 21)}
    total = sum(counts.values())
    in_pot = sum(t for n, t in counts.items() if n not in scratched)
    pot = round(in_pot * TOKEN_VALUE, 2)
    horses: Dict[str, Dict[str, Any]] = {}
    scratches: List[Dict[str, Any]] = []
    leader: Optional[int] = None
    best = 0
    for n in range(1, 21):
        t = counts[n]
        was = replaced.get(n)
        horses[str(n)] = {
            "tokens": t,
            "share": round(t / total, 4) if total else 0.0,
            "scratched": n in scratched,
            "online": n not in offline,
            "cup": n,
            "name": str(names.get(n) or "").upper(),
            "replaced": str(was).upper() if was else None,
        }
        if was:
            scratches.append({"horse": n, "was": str(was).upper(), "now": str(names.get(n) or "").upper()})
        if t > best:
            leader, best = n, t
    return {
        "link_ok": bool(link_ok),
        "race_state": int(phase),
        "race_state_name": RACE_STATE_NAMES.get(int(phase), f"STATE_{int(phase)}"),
        "token_value": float(TOKEN_VALUE),
        "pot": pot,
        "total_tokens": total,
        "horses": horses,
        "leader": leader,
        "events": list(events)[:MAX_EVENTS],
        "updated": float(updated if updated is not None else time.time()),
        "board_states": list(BOARD_STATES),
        "now": time.time(),
        "closes_at": float(closes_at) if closes_at is not None else None,
        "prizes": prizes_for(pot),
        "split": dict(SPLIT),
        "chyron": list(CHYRON_LINES if chyron is None else chyron),
        "names_rev": int(names_rev),
        "scratches": scratches,
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

    def __init__(self, phase: int, tokens: Optional[Dict[int, int]] = None,
                 scratched: Optional[Iterable[int]] = None, offline: Optional[Iterable[int]] = None,
                 events: Optional[List[Dict[str, Any]]] = None,
                 names: Optional[Dict[int, str]] = None, replaced: Optional[Dict[int, str]] = None,
                 closes_at: Optional[float] = None, names_rev: int = 0) -> None:
        self._lock = threading.Lock()
        self._subs: List["queue.Queue[Optional[str]]"] = []
        self.phase = phase
        self.tokens: Dict[int, int] = dict(BASE_TOKENS if tokens is None else tokens)
        self.scratched: Set[int] = set(SCRATCHED if scratched is None else scratched)
        self.offline: Set[int] = set(OFFLINE if offline is None else offline)
        self.events: List[Dict[str, Any]] = seed_events() if events is None else list(events)
        self.names: Dict[int, str] = dict(names or {})
        self.replaced: Dict[int, str] = dict(replaced or {})
        self.closes_at: Optional[float] = closes_at
        self.names_rev = int(names_rev)
        self.stopped = False
        self._json = _dumps(self._build())
        self.app = self._make_app()

    # -- scenario ------------------------------------------------------------
    def _build(self) -> Dict[str, Any]:
        return build_model(self.phase, self.tokens, self.scratched, self.offline, self.events,
                           names=self.names, replaced=self.replaced, closes_at=self.closes_at,
                           names_rev=self.names_rev)

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

    def reset(self, tokens: Dict[int, int], phase: Optional[int] = None,
              events: Optional[List[Dict[str, Any]]] = None) -> None:
        """A fresh token table and, unless given, no events: what pi5's
        model serves after reset_link() (a reset is a baseline, never a
        list of removals) or at the start of a cycle."""
        with self._lock:
            self.tokens = dict(tokens)
            self.events = [] if events is None else list(events)
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

    def set_scratched(self, horse: int, on: bool) -> None:
        """A kind-2 scratch (pi5's `scratch C 1` / `scratch C 0`; cup n is
        horse n here): the flag flips, no event, the horse's tokens leave
        (or rejoin) the pot. names_rev is the names store's revision on pi5
        and a gateway scratch does not touch it (the bridge's state rev is
        what moves), so it stays put here too."""
        with self._lock:
            if on == (horse in self.scratched):
                return
            if on:
                self.scratched.add(horse)
            else:
                self.scratched.discard(horse)
            self._publish_locked()

    def set_name(self, horse: int, name: str) -> None:
        """An operator's name change (pi5's PUT /api/quiniela/horses): the
        name and names_rev move, no event, nothing else does. An empty name
        clears it (the board shows HORSE n again)."""
        with self._lock:
            if self.names.get(horse, "") == name:
                return
            self.names[horse] = name
            self.names_rev += 1
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
        """A fresh serialisation (so `now` is the moment of the request);
        the published stream items are stamped when they are published."""
        with self._lock:
            return _dumps(self._build())

    # -- routes --------------------------------------------------------------
    def _sse(self) -> Iterator[str]:
        q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=32)
        with self._lock:
            self._subs.append(q)
            first = _dumps(self._build())
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
            elif words[0] == "reset":
                # Not in pi5's whitelist (there it is POST /api/lq/dev/reset);
                # here it plays that reset: counts 0, events cleared, PRE_RACE.
                fake.reset({}, PHASES["idle"])
            elif words[0] == "scratch" and len(words) == 3 and words[1].isdigit() and words[2] in ("0", "1"):
                fake.set_scratched(int(words[1]), words[2] == "1")
            elif words[0] == "name" and len(words) >= 2 and words[1].isdigit() and 1 <= int(words[1]) <= 20:
                # Not a pi5 command (there it is PUT /api/quiniela/horses); here
                # it renames horse N (the rest of the line, empty clears) so a
                # long name's shrink-to-fit can be watched on the board.
                fake.set_name(int(words[1]), " ".join(words[2:]))
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


def bench_events(now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    n = len(BENCH_EVENTS)
    return [
        {"horse": h, "delta": 1, "ts": round(now - 0.5 * (n - i), 3)}
        for i, h in reversed(list(enumerate(BENCH_EVENTS)))
    ]


def run_bench_reset(fake: FakePi5, period: float) -> None:
    """The 2026-09-25 sequence, forever: the bench picture with betting open
    for `period` s; then the reset (counts 0, events cleared, state 0: the
    board yields); 4 s later state 1 again with nothing bet (board up, empty
    events, every row NO BETS) for `period` s; then the picture comes back."""
    while True:
        print("-> bench (state 1): 50/42/8/3", flush=True)
        fake.reset(BENCH_TOKENS, PHASES["open"], events=bench_events())
        for _ in range(int(period)):
            if fake.stopped:
                return
            time.sleep(1.0)
        print("-> reset (state 0): counts 0, events cleared", flush=True)
        fake.reset({}, PHASES["idle"])
        time.sleep(4.0)
        print("-> open again (state 1): nothing bet, no events, no toast", flush=True)
        fake.set_phase(PHASES["open"])
        for _ in range(int(period)):
            if fake.stopped:
                return
            time.sleep(1.0)


def redesign_events(now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    n = len(REDESIGN_EVENTS)
    return [
        {"horse": h, "delta": 1, "ts": round(now - 4.0 * (n - i), 3)}
        for i, h in reversed(list(enumerate(REDESIGN_EVENTS)))
    ]


def run_redesign(fake: FakePi5) -> None:
    """A token lands on horse 7 every 4 s: the count ticks, the pot and the
    prizes follow, and the board's toast fires each time."""
    while not fake.stopped:
        time.sleep(REDESIGN_BUMP_EVERY_S)
        if fake.stopped:
            return
        fake.bump(REDESIGN_BUMP_HORSE)


def stop_feed_later(fake: FakePi5, after: float) -> None:
    time.sleep(max(0.0, after))
    fake.stop_feed()
    print("feed stopped: streams closed, GET answers 503; the splash's link_ok drops in ~7.5 s", flush=True)


def _client_host(host: str) -> str:
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=sorted(PHASES) + ["cycle", "bench", "bench-reset", "redesign", "redesign-static"],
                    default="open")
    ap.add_argument("--period", type=float, default=15.0,
                    help="seconds per state in --phase cycle, per step in --phase bench-reset")
    ap.add_argument("--stop-feed-after", type=float, default=None, metavar="SECONDS",
                    help="stop answering after this long (the splash's link_ok then drops); "
                         "use a few seconds, e.g. 3: with 0 the splash never receives a model")
    ap.add_argument("--pi5-port", type=int, default=5078, help="the fake pi5 (default 5078)")
    ap.add_argument("--port", type=int, default=5077, help="the real splash app (default 5077)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-splash", action="store_true", help="serve only the fake pi5")
    args = ap.parse_args()

    if args.phase == "cycle":
        fake = FakePi5(PHASES["idle"])
    elif args.phase in ("bench", "bench-reset"):
        fake = FakePi5(PHASES["open"], tokens=BENCH_TOKENS, scratched=(), offline=(),
                       events=bench_events())
    elif args.phase in ("redesign", "redesign-static"):
        fake = FakePi5(PHASES["open"], tokens=REDESIGN_TOKENS, scratched=(), offline=(),
                       events=redesign_events(), names=REDESIGN_NAMES, replaced=REDESIGN_REPLACED,
                       closes_at=time.time() + REDESIGN_CLOSES_IN_S, names_rev=2)
    else:
        fake = FakePi5(PHASES[args.phase])
    pi5_httpd = make_server(args.host, args.pi5_port, fake.app, threaded=True)
    threading.Thread(target=pi5_httpd.serve_forever, name="fake-pi5", daemon=True).start()
    pi5_url = f"http://{_client_host(args.host)}:{args.pi5_port}"
    print(f"fake pi5: phase={args.phase} pid={os.getpid()} {pi5_url}/api/quiniela", flush=True)

    if args.stop_feed_after is not None:
        threading.Thread(target=stop_feed_later, args=(fake, args.stop_feed_after), daemon=True).start()
    if args.phase == "cycle":
        threading.Thread(target=run_cycle, args=(fake, args.period), daemon=True).start()
    elif args.phase == "bench-reset":
        threading.Thread(target=run_bench_reset, args=(fake, args.period), daemon=True).start()
    elif args.phase == "redesign":
        threading.Thread(target=run_redesign, args=(fake,), daemon=True).start()

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

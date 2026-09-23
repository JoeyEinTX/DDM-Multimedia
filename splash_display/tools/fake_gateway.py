#!/usr/bin/env python3
"""
Fake gateway — run the real splash server with a synthetic cup gateway.

A dev aid for the La Quiniela board: it imports ``server`` (so every route,
template and the slideshow are the real ones), serves it on 127.0.0.1, and
feeds synthetic ``{"t":"state",...}`` lines into ``quiniela.link.feed_line()``
from a thread, exactly as the serial thread would. No serial port is ever
opened (``quiniela._started`` is set before ``server`` is imported, so
``start_link()`` is a no-op); only the 1 Hz link_ok ticker is started, so
``link_ok`` is derived from the fed lines just like in production. The
dashboard poller is kept out too, and the JSONL event log is disabled so
fake bets never land in ``logs/``.

    cd splash_display
    python tools/fake_gateway.py --phase open          # BETTING OPEN, static rich data
    python tools/fake_gateway.py --phase final         # FINAL CALL (banner pulses)
    python tools/fake_gateway.py --phase closed        # AT_THE_POST: BETTING CLOSED, frozen
    python tools/fake_gateway.py --phase running       # RUNNING: BETTING CLOSED, still frozen
    python tools/fake_gateway.py --phase winner        # state 5: the board yields to the playlist
    python tools/fake_gateway.py --phase idle          # state 0: playlist, link up
    python tools/fake_gateway.py --phase cycle         # idle -> open -> final -> closed -> running -> winner, forever
    python tools/fake_gateway.py --phase open --stop-feed-after 0   # board up, link lost: NO LINK mark

Then open http://127.0.0.1:5077/display (the URL is printed).

Static phases carry 20 cups, tokens spread with a clear leader (horse 7),
one scratched horse (13), one offline cup (horse 11, age 9000 ms) and a
handful of events already in the model (fed as several lines with rising
token counts). ``cycle`` walks the race states about every 15 s with
tokens climbing while betting is open, and keeps a slow trickle of bets
going in AT_THE_POST so the freeze rule is visible.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent      # splash_display/
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
import quiniela  # noqa: E402
import race_poller  # noqa: E402

# Never open a serial port, never poll the dashboard, never write the real
# event log. start_*() are idempotent guards, so marking them started makes
# server's module-load calls no-ops.
quiniela._started = True
race_poller._started = True
config.QUINIELA_LOG = False

import server  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

PHASES = {
    "idle":    0,   # PRE_RACE
    "open":    1,   # BETTING_OPEN
    "final":   2,   # FINAL_CALL
    "closed":  3,   # AT_THE_POST
    "running": 4,   # RUNNING
    "winner":  5,   # WINNER
}

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
# The last few bets, oldest first: fed one line each so they become events.
SEED_EVENTS = [7, 3, 15, 7, 10, 7, 18]


def state_line(st: int, tokens: dict, scratched=SCRATCHED, offline=OFFLINE) -> str:
    """One gateway state line: 20 cups, cup id n-1 on horse n."""
    cups = []
    for n in range(1, 21):
        cups.append({
            "id": n - 1,
            "mac": "20:50:0D:11:D9:%02X" % (n - 1),
            "h": n,
            "scr": 1 if n in scratched else 0,
            "tok": int(tokens.get(n, 0)),
            "rssi": -60 - n,
            "age": 9000 if n in offline else 120 + 37 * n,
        })
    return json.dumps({"t": "state", "st": st, "cups": cups}, separators=(",", ":"))


def feed(line: str) -> None:
    quiniela.link.feed_line(line)


def seed_history(st: int) -> dict:
    """Feed a baseline plus SEED_EVENTS so the model already carries events.
    Returns the resulting token table (== BASE_TOKENS)."""
    tokens = dict(BASE_TOKENS)
    for h in SEED_EVENTS:
        tokens[h] -= 1
    feed(state_line(st, tokens))          # baseline: no events yet
    for h in SEED_EVENTS:
        time.sleep(0.03)                  # distinct event timestamps
        tokens[h] += 1
        feed(state_line(st, tokens))
    return tokens


def run_static(st: int, stop_feed_after: float | None) -> None:
    tokens = seed_history(st)
    started = time.monotonic()
    while True:
        if stop_feed_after is not None and time.monotonic() - started >= stop_feed_after:
            print("feed stopped; link_ok drops in ~5 s", flush=True)
            return
        feed(state_line(st, tokens))       # 1 Hz like the real gateway; unchanged = not published
        time.sleep(1.0)


def run_cycle(period: float) -> None:
    rng = random.Random(7)
    bettable = [n for n in BASE_TOKENS if n not in SCRATCHED]

    def bump(tokens: dict) -> None:
        tokens[rng.choice(bettable)] += 1

    while True:
        tokens = {n: (0 if n in SCRATCHED else max(0, t // 3)) for n, t in BASE_TOKENS.items()}
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
            for i in range(int(period)):
                if every and i % every == 0:
                    bump(tokens)
                feed(state_line(st, tokens))
                time.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", choices=sorted(PHASES) + ["cycle"], default="open")
    ap.add_argument("--port", type=int, default=5077)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--period", type=float, default=15.0, help="seconds per state in --phase cycle")
    ap.add_argument("--stop-feed-after", type=float, default=None, metavar="SECONDS",
                    help="static phases: stop feeding after this long (link_ok then drops)")
    args = ap.parse_args()

    # The 1 Hz ticker that lets the board notice the (fake) gateway going
    # silent — the only part of start_link() we want.
    threading.Thread(target=quiniela._tick_loop, name="quiniela-tick", daemon=True).start()

    if args.phase == "cycle":
        feeder = threading.Thread(target=run_cycle, args=(args.period,), daemon=True)
    else:
        feeder = threading.Thread(
            target=run_static, args=(PHASES[args.phase], args.stop_feed_after), daemon=True
        )
    feeder.start()

    httpd = make_server(args.host, args.port, server.app, threaded=True)
    print(f"fake gateway: phase={args.phase} pid={os.getpid()}", flush=True)
    print(f"http://{args.host}:{args.port}/display", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

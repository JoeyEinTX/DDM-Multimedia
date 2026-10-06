"""
Unit tests for splash_display/quiniela.py (the pi5 relay), the page that
carries the board and its looks, the tote look's face (tools/make_tote_font.py)
and the dev harness (tools/fake_pi5.py).

Run from splash_display/ (stdlib unittest, no pytest needed):

    python -m unittest -v tests.test_quiniela

No network beyond loopback is used: pi5 is simulated by an injected opener
(the link's HTTP seam), a fake sleeper and a fake clock drive the reconnect
loop without real time, and the one in-process fake pi5 (the cmd relay test)
listens on an ephemeral loopback port. The harness is exercised through
Flask's test client; none of its servers is started.
"""

from __future__ import annotations

import ast
import contextlib
import html
import importlib.util
import io
import json
import logging
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
import quiniela  # noqa: E402

# The dev harness, by path (tools/ is not a package). Importing it starts
# nothing: its servers and the splash's own modules only come with main().
_spec = importlib.util.spec_from_file_location("fake_pi5", HERE / "tools" / "fake_pi5.py")
fake_pi5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_pi5)

# ... and the tool that builds the tote look's face.
_spec = importlib.util.spec_from_file_location("make_tote_font", HERE / "tools" / "make_tote_font.py")
make_tote_font = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_tote_font)

# Keep the link thread out of the test process: it would try pi5 every few
# seconds. start_link() is an idempotent guard, so marking it started makes
# server's module-load call a no-op.
quiniela._started = True

import server  # noqa: E402

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from quiniela import BoardRelay, Pi5Link, sse_events  # noqa: E402

LOGGER = "splash_display.quiniela"
BASE = "http://pi5.test:5000"

CONTRACT_KEYS = {
    "link_ok", "race_state", "race_state_name", "token_value", "pot",
    "total_tokens", "horses", "leader", "events", "updated", "board_states",
}
# What pi5 serves (pi5/la_quiniela/test_betting.py MODEL_KEYS): the contract,
# the keys added with the payout model, those added with protocol v2, and the
# figures at the post (closing).
PI5_MODEL_KEYS = CONTRACT_KEYS | {
    "now", "closes_at", "prizes", "split", "chyron", "names_rev", "scratches",
    "cups_online", "cups_no_horse", "results", "closing",
    "race", "weather",                      # race info lives in La Quiniela; pi5's weather for the crawl
    "pot_scale", "pot_counted", "hand_counted",     # the host's hand count of the cash box (the counted pot)
}
PI5_HORSE_KEYS = {"tokens", "share", "scratched", "online", "cup", "conflict", "cups",
                  "name", "replaced", "in_field", "odds"}
UNASSIGNED = {"tokens": 0, "share": 0.0, "scratched": False, "online": False, "cup": None}


def mac_of(n: int) -> str:
    """A cup as pi5 names it since protocol v2: by its MAC, never a number."""
    return "A0:B7:65:12:34:%02X" % n


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pi5_model(race_state: int = 1, tokens: Optional[Dict[int, int]] = None, link_ok: bool = True,
              updated: float = 1_700_000_000.0, events=None, board_states=(1, 2, 3, 4, 5), **extra):
    """A model as pi5 serves it: a horse's cup is the MAC of the cup claiming
    it (null when none does), share 4 dp, leader = strictly most tokens
    (lowest horse on a tie), the board states 1..5."""
    tokens = dict(tokens or {})
    total = sum(tokens.values())
    horses = {}
    for n in range(1, 21):
        t = tokens.get(n, 0)
        horses[str(n)] = {
            "tokens": t,
            "share": round(t / total, 4) if total else 0.0,
            "scratched": False,
            "online": n in tokens,
            "cup": mac_of(n) if n in tokens else None,
        }
    leader = max(tokens, key=lambda n: (tokens[n], -n)) if total else None
    model = {
        "link_ok": link_ok,
        "race_state": race_state,
        "race_state_name": quiniela.race_state_name(race_state),
        "token_value": 1.0,
        "pot": round(total * 1.0, 2),
        "total_tokens": total,
        "horses": horses,
        "leader": leader,
        "events": list(events or []),
        "updated": updated,
        "board_states": list(board_states),
    }
    model.update(extra)
    return model


def sse_bytes(model: dict) -> List[bytes]:
    return [b"data: " + json.dumps(model).encode("utf-8") + b"\n", b"\n"]


PING = [b": heartbeat\n", b"\n", b"event: ping\n", b'data: {"ts":1700000000.0}\n', b"\n"]


class Recorder:
    """on_model / on_alive sinks."""

    def __init__(self) -> None:
        self.models: List[dict] = []
        self.alive = 0

    def on_model(self, m: dict) -> None:
        self.models.append(m)

    def on_alive(self) -> None:
        self.alive += 1


class FakeResponse:
    """What the injected opener returns: a context manager that iterates
    lines (the stream) and has .read() (a poll / cmd reply)."""

    def __init__(self, body: bytes = b"", lines: Any = (), status: int = 200) -> None:
        self.body = body
        self.lines = lines
        self.status = status
        self.closed = False

    def read(self) -> bytes:
        return self.body

    def __iter__(self):
        return iter(self.lines() if callable(self.lines) else self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True


def json_response(obj: Any, status: int = 200) -> FakeResponse:
    return FakeResponse(body=json.dumps(obj).encode("utf-8"), status=status)


def refused() -> urllib.error.URLError:
    return urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def http_error(code: int, body: Any) -> urllib.error.HTTPError:
    raw = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
    return urllib.error.HTTPError(BASE + "/api/quiniela/cmd", code, "nope", {}, io.BytesIO(raw))


class ScriptedOpener:
    """opener(request, timeout): per path, a list of outcomes (a FakeResponse
    to return or an exception to raise); the last outcome repeats forever."""

    def __init__(self, **scripts: List[Any]) -> None:
        self.scripts = {"/api/quiniela/stream": scripts.pop("stream", [refused()]),
                        "/api/quiniela": scripts.pop("poll", [refused()]),
                        "/api/quiniela/cmd": scripts.pop("cmd", [refused()])}
        self.calls: List[Any] = []       # (path, timeout, request)

    def __call__(self, req: Any, timeout: float) -> Any:
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        assert url.startswith(BASE), url
        path = url[len(BASE):]
        self.calls.append((path, timeout, req))
        script = self.scripts[path]
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def paths(self) -> List[str]:
        return [c[0] for c in self.calls]


class FakeSleeper:
    """Advances the fake clock instead of sleeping; stops the link after
    stop_after calls so _run() returns."""

    def __init__(self, clock: FakeClock, stop_after: Optional[int] = None) -> None:
        self.clock = clock
        self.stop_after = stop_after
        self.sleeps: List[float] = []
        self.link: Optional[Pi5Link] = None

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        if self.stop_after is not None and len(self.sleeps) >= self.stop_after and self.link:
            self.link.stop()


def make_link(rec: Recorder, clock: FakeClock, opener: Any, stop_after: Optional[int] = None):
    sleeper = FakeSleeper(clock, stop_after)
    link = Pi5Link(on_model=rec.on_model, on_alive=rec.on_alive, base_url=BASE + "/",
                   clock=clock, opener=opener, sleeper=sleeper)
    sleeper.link = link
    return link, sleeper


# ---------------------------------------------------------------------------
# BoardRelay
# ---------------------------------------------------------------------------
class RelayCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.wall = FakeClock(1_700_000_000.0)
        self.board = BoardRelay(clock=self.clock, wall=self.wall)


class RelayTests(RelayCase):
    def test_empty_model_shape(self) -> None:
        m = self.board.model()
        self.assertEqual(set(m), CONTRACT_KEYS)
        self.assertIs(m["link_ok"], False)
        self.assertEqual((m["race_state"], m["race_state_name"]), (0, "PRE_RACE"))
        self.assertEqual(m["token_value"], 1.0)
        self.assertEqual(m["pot"], 0.0)
        self.assertEqual(m["total_tokens"], 0)
        self.assertEqual(sorted(m["horses"], key=int), [str(n) for n in range(1, 21)])
        for entry in m["horses"].values():
            self.assertEqual(entry, UNASSIGNED)
        self.assertIsNone(m["leader"])
        self.assertEqual(m["events"], [])
        self.assertEqual(m["updated"], self.wall.now)
        self.assertEqual(m["board_states"], [], "until pi5 is heard the board stays hidden")
        self.assertFalse(self.board.pi5_ok())
        self.assertEqual(self.board.model_json(), json.dumps(m, separators=(",", ":")))

    def test_apply_model_passes_every_key_through_and_serves_link_ok_as_received(self) -> None:
        m = pi5_model(race_state=2, tokens={7: 23, 3: 2}, updated=1_600_000_000.0,
                      events=[{"horse": 7, "delta": 1, "ts": 1_600_000_000.0}], extra_key="kept")
        self.assertTrue(self.board.apply_model(m))
        served = self.board.model()
        self.assertEqual(served, m, "pi5's model is served untouched, extra keys included")
        self.assertEqual(served["horses"]["7"]["cup"], mac_of(7), "the cup is pi5's: a MAC string, never a number")
        self.assertIsNone(served["horses"]["1"]["cup"], "null when no cup claims the horse")
        self.assertEqual(served["updated"], 1_600_000_000.0, "updated is kept as received")
        self.assertIs(served["link_ok"], True)
        self.assertTrue(self.board.pi5_ok())
        # pi5's own link_ok false (its gateway is quiet) is served as false.
        self.assertTrue(self.board.apply_model(pi5_model(link_ok=False)))
        self.assertIs(self.board.model()["link_ok"], False)
        # ...and a truthy non-bool becomes a bool.
        self.assertTrue(self.board.apply_model(pi5_model(link_ok="yes")))
        self.assertIs(self.board.model()["link_ok"], True)

    def test_results_pass_through_as_received(self) -> None:
        results = {"win": 19, "place": 1, "show": 22}
        self.assertTrue(self.board.apply_model(pi5_model(race_state=5, results=None)))
        served = self.board.model()
        self.assertIn("results", served)
        self.assertIsNone(served["results"], "null until the dashboard has them")
        self.assertIn('"results":null', self.board.model_json())
        self.assertTrue(self.board.apply_model(pi5_model(race_state=5, results=results)),
                        "the results arriving is a change: it is published")
        self.assertEqual(self.board.model()["results"], results)
        self.assertEqual(self.board.model()["board_states"], [1, 2, 3, 4, 5])
        self.assertFalse(self.board.apply_model(pi5_model(race_state=5, results=dict(results))))
        self.assertTrue(self.board.apply_model(pi5_model(race_state=6, results=None)), "pi5's reset clears them")
        self.assertIsNone(self.board.model()["results"])

    def test_the_figures_at_the_post_pass_through_as_received(self) -> None:
        closing = {"pot": 154.0, "prizes": {"win": 92, "place": 39, "show": 23}, "total_tokens": 158,
                   "horses": {str(n): {"tokens": 4 if n == 19 else 0} for n in range(1, 25)}, "at": 1_700_000_000.0}
        self.assertTrue(self.board.apply_model(pi5_model(race_state=5, tokens={19: 0}, closing=closing)))
        served = self.board.model()
        self.assertEqual(served["closing"], closing, "the relay serves pi5's closing untouched")
        self.assertEqual((served["pot"], served["horses"]["19"]["tokens"]), (0.0, 0), "...beside the live fields")
        self.assertIn('"closing":{"pot":154.0', self.board.model_json())
        self.assertTrue(self.board.apply_model(pi5_model(race_state=1, closing=None)), "dropped when betting reopens")
        self.assertIsNone(self.board.model()["closing"])

    def test_apply_model_ignores_junk(self) -> None:
        before = self.board.model_json()
        for junk in (None, "x", 5, [], {}, {"link_ok": True}, {"horses": []}, {"horses": None},
                     {"horses": "1"}):
            self.assertFalse(self.board.apply_model(junk), junk)
        self.assertEqual(self.board.model_json(), before)
        self.assertFalse(self.board.pi5_ok(), "junk is not contact")

    def test_unchanged_model_is_not_republished(self) -> None:
        q = self.board.subscribe()
        m = pi5_model(tokens={7: 1})
        self.assertTrue(self.board.apply_model(m))
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(q.get_nowait(), json.dumps(m, separators=(",", ":")))
        self.assertFalse(self.board.apply_model(json.loads(json.dumps(m))))
        self.assertFalse(self.board.apply_model(m))
        self.assertEqual(q.qsize(), 0)
        self.assertTrue(self.board.apply_model(pi5_model(tokens={7: 2})))
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(json.loads(q.get_nowait())["horses"]["7"]["tokens"], 2)

    def test_tick_flips_link_ok_after_timeout_and_publishes_once(self) -> None:
        q = self.board.subscribe()
        self.board.apply_model(pi5_model(race_state=1, tokens={7: 5}))
        self.assertTrue(self.board.model()["link_ok"])
        self.assertEqual(q.qsize(), 1)
        self.clock.advance(quiniela.LINK_TIMEOUT_S - 0.1)
        self.assertFalse(self.board.tick())
        self.assertTrue(self.board.model()["link_ok"])
        self.assertTrue(self.board.pi5_ok())
        self.assertEqual(q.qsize(), 1, "nothing published while link_ok is unchanged")
        self.clock.advance(0.2)
        self.wall.advance(5.1)
        self.assertTrue(self.board.tick())
        m = self.board.model()
        self.assertIs(m["link_ok"], False)
        self.assertEqual(m["race_state"], 1, "other fields keep their last values")
        self.assertEqual(m["horses"]["7"]["tokens"], 5)
        self.assertEqual(m["updated"], self.wall.now, "a flip bumps updated")
        self.assertFalse(self.board.pi5_ok())
        self.assertEqual(q.qsize(), 2)
        q.get_nowait()
        self.assertIs(json.loads(q.get_nowait())["link_ok"], False)
        self.assertFalse(self.board.tick(), "already published")
        self.assertTrue(self.board.apply_model(pi5_model(race_state=1, tokens={7: 5})),
                        "the same model again differs from the served one (link_ok) and restores it")
        self.assertTrue(self.board.model()["link_ok"])
        # pi5 reporting link_ok false itself: nothing to flip when it goes quiet.
        self.board.apply_model(pi5_model(link_ok=False))
        self.clock.advance(quiniela.LINK_TIMEOUT_S + 1)
        self.assertFalse(self.board.tick())
        self.assertIs(self.board.model()["link_ok"], False)

    def test_link_timeout_clears_one_ping_period(self) -> None:
        """pi5 pings after 5 s of silence measured from its previous chunk,
        so two contacts are never less than 5 s apart at the relay; a window
        of exactly 5 s flickered link_ok on a healthy pi5. It must clear one
        ping period with margin and stay under the page's 10 s STALE_MS."""
        self.assertGreater(quiniela.LINK_TIMEOUT_S, quiniela.SSE_HEARTBEAT_S + 1.0)
        self.assertLess(quiniela.LINK_TIMEOUT_S, 10.0)
        self.board.apply_model(pi5_model(tokens={7: 5}))
        for _ in range(3):                      # pings 5.02 s apart, one tick lands 5.01 s after the last
            for _ in range(4):
                self.clock.advance(1.0)
                self.assertFalse(self.board.tick(), "a tick between two pings must not flip")
            self.clock.advance(1.01)            # inside the old 5.0 s tripwire
            self.assertFalse(self.board.tick(), "a tick just past 5 s must not flip")
            self.clock.advance(0.01)
            self.board.touch()
        self.assertTrue(self.board.model()["link_ok"])

    def test_now_is_stamped_at_serve_time_never_stored(self) -> None:
        # pi5 stamps "now" when it serialises; the relay must do the same, or a
        # TV loading on a quiet board would anchor CLOSES IN to a clock as old
        # as the last bet. Found on the 2026-09-25 verification run.
        self.assertNotIn("now", self.board.model(), "no stamp before pi5 has sent one")
        m = pi5_model(tokens={7: 1}, closes_at=1_600_001_000.0, now=1_600_000_000.0)
        self.assertTrue(self.board.apply_model(m))
        self.wall.advance(218)
        served = self.board.model()
        self.assertEqual(served["now"], self.wall.now, "fresh, not pi5's 218 s old stamp")
        self.assertEqual(served["closes_at"], 1_600_001_000.0)
        self.assertEqual(json.loads(self.board.model_json())["now"], self.wall.now)
        # The same picture with a newer stamp is not a change.
        self.assertFalse(self.board.apply_model(pi5_model(tokens={7: 1}, closes_at=1_600_001_000.0,
                                                          now=1_600_000_100.0)))
        # A publish carries a fresh stamp too.
        q = self.board.subscribe()
        self.wall.advance(1)
        self.assertTrue(self.board.apply_model(pi5_model(tokens={7: 2}, closes_at=1_600_001_000.0,
                                                         now=1_600_000_200.0)))
        self.assertEqual(json.loads(q.get_nowait())["now"], self.wall.now)
        # ...and so does the link_ok flip a tick publishes.
        self.clock.advance(quiniela.LINK_TIMEOUT_S + 1)
        self.wall.advance(quiniela.LINK_TIMEOUT_S + 1)
        self.assertTrue(self.board.tick())
        flipped = json.loads(q.get_nowait())
        self.assertIs(flipped["link_ok"], False)
        self.assertEqual(flipped["now"], self.wall.now)
        self.assertEqual(flipped["horses"]["7"]["tokens"], 2)

    def test_key_order_does_not_count_as_a_change(self) -> None:
        """The stream carries pi5's key order, the poll fallback (jsonify)
        sorted keys: the same model in another order is not republished."""
        q = self.board.subscribe()
        m = pi5_model(tokens={7: 5})
        self.assertTrue(self.board.apply_model(m))
        sorted_m = json.loads(json.dumps(m, sort_keys=True))
        self.assertNotEqual(list(sorted_m), list(m), "the fixture really differs in order")
        self.assertFalse(self.board.apply_model(sorted_m))
        self.assertFalse(self.board.apply_model(m))
        self.assertEqual(q.qsize(), 1)

    def test_touch_alone_keeps_link_ok_and_restores_it_after_a_flip(self) -> None:
        q = self.board.subscribe()
        self.board.apply_model(pi5_model(tokens={7: 5}))
        self.clock.advance(quiniela.LINK_TIMEOUT_S - 1.5)
        self.board.touch()                      # a ping
        self.clock.advance(2.0)                 # past the window since the model, 2 s since the ping
        self.assertFalse(self.board.tick())
        self.assertTrue(self.board.model()["link_ok"])
        self.assertTrue(self.board.pi5_ok())
        self.assertEqual(q.qsize(), 1)
        self.clock.advance(quiniela.LINK_TIMEOUT_S + 0.1)
        self.assertTrue(self.board.tick())
        self.assertIs(self.board.model()["link_ok"], False)
        self.board.touch()                      # pi5 is back: the next ping restores it
        self.assertIs(self.board.model()["link_ok"], True)
        self.assertEqual(q.qsize(), 3)
        self.assertFalse(self.board.tick())

    def test_subscriber_queue_drops_oldest_when_full(self) -> None:
        q = self.board.subscribe()
        for i in range(quiniela.SSE_QUEUE_SIZE + 8):
            self.assertTrue(self.board.apply_model(pi5_model(tokens={7: i + 1})))
        self.assertEqual(q.qsize(), quiniela.SSE_QUEUE_SIZE)
        newest = None
        while not q.empty():
            newest = json.loads(q.get_nowait())
        self.assertEqual(newest["horses"]["7"]["tokens"], quiniela.SSE_QUEUE_SIZE + 8)
        self.assertEqual(self.board.subscriber_count(), 1)
        self.board.unsubscribe(q)
        self.assertEqual(self.board.subscriber_count(), 0)


# ---------------------------------------------------------------------------
# Pi5Link: the SSE parser seam
# ---------------------------------------------------------------------------
class FeedSseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.rec = Recorder()
        self.link = Pi5Link(on_model=self.rec.on_model, on_alive=self.rec.on_alive,
                            base_url=BASE, clock=self.clock)

    def test_data_events_are_models_and_pings_are_contact(self) -> None:
        self.assertFalse(self.link.connected)
        m = pi5_model(tokens={7: 3})
        self.link.feed_sse(sse_bytes(m) + PING + sse_bytes(pi5_model(tokens={7: 4})))
        self.assertEqual([x["horses"]["7"]["tokens"] for x in self.rec.models], [3, 4])
        self.assertEqual(self.rec.models[0], m)
        self.assertEqual(self.rec.alive, 3, "each model and each ping is contact")
        self.assertTrue(self.link.connected)
        self.clock.advance(quiniela.LINK_TIMEOUT_S + 0.1)
        self.assertFalse(self.link.connected)

    def test_multiline_data_joins_with_newline_and_str_lines_are_fine(self) -> None:
        m = pi5_model()
        text = json.dumps(m, indent=1)              # several lines
        lines = ["data: " + part for part in text.split("\n")] + [""]
        lines += ["data:" + json.dumps(pi5_model(race_state=3)), ""]   # no space after the colon
        self.link.feed_sse(lines)
        self.assertEqual(len(self.rec.models), 2)
        self.assertEqual(self.rec.models[0], m)
        self.assertEqual(self.rec.models[1]["race_state"], 3)

    def test_comments_bad_json_non_dicts_and_other_events_are_ignored_at_debug(self) -> None:
        with self.assertLogs(LOGGER, level="DEBUG") as captured:
            self.link.feed_sse([
                b": just a comment\n", b"\n",
                b"data: {not json\n", b"\n",
                b"data: [1,2,3]\n", b"\n",
                b"data: 5\n", b"\n",
                b"event: roster\n", b"data: {\"horses\":{}}\n", b"\n",
                b"event: ping\n", b"\n",             # no data: not an event
                b"id: 7\n", b"retry: 1000\n", b"\n",
            ])
        self.assertTrue(all(r.levelname == "DEBUG" for r in captured.records))
        self.assertEqual(self.rec.models, [])
        self.assertEqual(self.rec.alive, 0)
        self.assertFalse(self.link.connected)

    def test_unnamed_and_message_events_are_both_models(self) -> None:
        m = pi5_model(tokens={1: 1})
        self.link.feed_sse([b"event: message\n"] + sse_bytes(m) + sse_bytes(m))
        self.assertEqual(self.rec.models, [m, m])
        self.assertEqual(self.rec.alive, 2)

    def test_handler_failure_does_not_stop_the_feed(self) -> None:
        def boom(_m):
            raise RuntimeError("relay bug")
        link = Pi5Link(on_model=boom, on_alive=self.rec.on_alive, base_url=BASE, clock=self.clock)
        with self.assertLogs(LOGGER, level="ERROR"):
            link.feed_sse(sse_bytes(pi5_model()) + PING)
        self.assertEqual(self.rec.alive, 2)
        self.assertTrue(link.connected)


# ---------------------------------------------------------------------------
# Pi5Link: the reconnect loop (injected opener + sleeper, fake clock)
# ---------------------------------------------------------------------------
class RunLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.rec = Recorder()

    def test_stream_failure_polls_every_second_and_retries_the_stream(self) -> None:
        opener = ScriptedOpener(stream=[refused()], poll=[json_response(pi5_model(tokens={7: 9}))])
        link, sleeper = make_link(self.rec, self.clock, opener, stop_after=12)
        with self.assertLogs(LOGGER, level="DEBUG"):
            link._run()
        self.assertEqual(opener.paths(),
                         ["/api/quiniela/stream"] + ["/api/quiniela"] * 10
                         + ["/api/quiniela/stream"] + ["/api/quiniela"] * 2)
        self.assertEqual(sleeper.sleeps, [quiniela.POLL_INTERVAL_S] * 12)
        self.assertEqual(len(self.rec.models), 12)
        self.assertEqual(self.rec.models[0]["horses"]["7"]["tokens"], 9)
        self.assertEqual(self.rec.alive, 12)
        self.assertTrue(link.connected)
        stream_call, poll_call = opener.calls[0], opener.calls[1]
        self.assertEqual(stream_call[1], quiniela.STREAM_READ_TIMEOUT_S)
        self.assertEqual(stream_call[2].get_header("Accept"), "text/event-stream")
        self.assertEqual(poll_call[1], quiniela.FETCH_TIMEOUT_S)
        self.assertEqual(poll_call[2].get_header("Accept"), "application/json")

    def test_poll_failures_back_off_and_a_success_resets(self) -> None:
        fail = refused()
        opener = ScriptedOpener(
            stream=[refused()],
            poll=[fail] * 6 + [json_response(pi5_model())] + [fail],
        )
        link, sleeper = make_link(self.rec, self.clock, opener, stop_after=9)
        with self.assertLogs(LOGGER, level="DEBUG"):
            link._run()
        self.assertEqual(sleeper.sleeps, [1, 2, 4, 8, 10, 10, 1, 1, 2])
        # The stream is retried once every STREAM_RETRY_S of polling, not more.
        stream_at = [i for i, p in enumerate(opener.paths()) if p.endswith("/stream")]
        self.assertEqual(stream_at, [0, 5, 7, 9])
        self.assertEqual(len(self.rec.models), 1)
        self.assertTrue(link.connected, "the one good poll was 4 s of fake time ago")
        self.clock.advance(quiniela.LINK_TIMEOUT_S - 4.0 + 0.1)
        self.assertFalse(link.connected)

    def test_stream_models_feed_the_sinks_and_stop_ends_the_loop(self) -> None:
        holder: Dict[str, Pi5Link] = {}

        def lines():
            yield from sse_bytes(pi5_model(tokens={7: 1}))
            yield from PING
            holder["link"].stop()
            yield b"data: {\"horses\":{}}\n"       # never dispatched: stop() was called
            yield b"\n"

        stream = FakeResponse(lines=lines)
        opener = ScriptedOpener(stream=[stream])
        link, sleeper = make_link(self.rec, self.clock, opener)
        holder["link"] = link
        with self.assertLogs(LOGGER, level="INFO") as captured:
            link._run()
        self.assertEqual(len(self.rec.models), 1)
        self.assertEqual(self.rec.alive, 2)
        self.assertEqual(sleeper.sleeps, [])
        self.assertTrue(stream.closed)
        self.assertTrue(link.connected)
        self.assertTrue(any("pi5 link up (stream)" in r.getMessage() for r in captured.records))
        self.assertFalse(any(r.levelname == "WARNING" for r in captured.records))
        link.stop()                                   # idempotent

    def test_stream_lost_warns_once_then_debug_until_it_is_back(self) -> None:
        saved = quiniela.STREAM_RETRY_S
        quiniela.STREAM_RETRY_S = 2.0
        try:
            opener = ScriptedOpener(stream=[refused(), refused(), FakeResponse(lines=[]), refused()])
            link, sleeper = make_link(self.rec, self.clock, opener, stop_after=8)
            with self.assertLogs(LOGGER, level="DEBUG") as captured:
                link._run()
        finally:
            quiniela.STREAM_RETRY_S = saved
        lost = [r for r in captured.records if "pi5 stream lost" in r.getMessage()]
        self.assertEqual([r.levelname for r in lost], ["WARNING", "WARNING"],
                         "once when first lost, once again after it had come back")
        still = [r for r in captured.records if "pi5 stream still down" in r.getMessage()]
        self.assertTrue(still and all(r.levelname == "DEBUG" for r in still))
        self.assertEqual(sum("pi5 link up (stream)" in r.getMessage() for r in captured.records), 1)
        polls = [r.levelname for r in captured.records if "pi5 poll failed" in r.getMessage()]
        self.assertEqual(polls[:2], ["WARNING", "DEBUG"])
        self.assertEqual(polls.count("WARNING"), 2,
                         "one per failing episode: the stream coming up ends the first")

    def test_an_opener_that_raises_forever_never_kills_the_thread(self) -> None:
        def opener(_req, _timeout):
            raise RuntimeError("boom")

        link, sleeper = make_link(self.rec, self.clock, opener, stop_after=3)
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            self.assertTrue(link.start())
            self.assertTrue(link.start(), "idempotent")
            link._thread.join(2.0)
        self.assertFalse(link._thread.is_alive())
        self.assertEqual(link._thread.name, "quiniela-link")
        self.assertEqual(sleeper.sleeps, [quiniela.BACKOFF_MIN_S] * 3)
        self.assertGreaterEqual(
            sum("pi5 link loop failed" in r.getMessage() for r in captured.records), 3)
        self.assertFalse(link.connected)


# ---------------------------------------------------------------------------
# Pi5Link.forward_cmd
# ---------------------------------------------------------------------------
class ForwardCmdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()

    def _link(self, opener: Any) -> Pi5Link:
        return Pi5Link(on_model=self.rec.on_model, on_alive=self.rec.on_alive, base_url=BASE, opener=opener)

    def test_200_is_relayed_with_the_body_as_received(self) -> None:
        opener = ScriptedOpener(cmd=[json_response({"ok": True, "echo": "state 1"})])
        link = self._link(opener)
        self.assertEqual(link.forward_cmd({"cmd": "state 1"}), (200, {"ok": True, "echo": "state 1"}))
        path, timeout, req = opener.calls[0]
        self.assertEqual(path, "/api/quiniela/cmd")
        self.assertEqual(timeout, quiniela.FETCH_TIMEOUT_S)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"cmd": "state 1"})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        # Not a dict (no body, a list): pi5 gets {} and decides.
        link.forward_cmd(None)
        link.forward_cmd(["state 1"])
        self.assertEqual([json.loads(c[2].data) for c in opener.calls[1:]], [{}, {}])

    def test_http_error_with_json_body_is_relayed(self) -> None:
        opener = ScriptedOpener(cmd=[http_error(400, {"ok": False, "error": "empty command"}),
                                     http_error(503, {"ok": False, "error": "gateway not connected"}),
                                     http_error(500, b"<html>boom</html>")])
        link = self._link(opener)
        self.assertEqual(link.forward_cmd({"cmd": ""}), (400, {"ok": False, "error": "empty command"}))
        self.assertEqual(link.forward_cmd({"cmd": "state 1"}),
                         (503, {"ok": False, "error": "gateway not connected"}))
        status, payload = link.forward_cmd({"cmd": "state 1"})
        self.assertEqual(status, 500)
        self.assertIs(payload["ok"], False)
        self.assertIn("non-JSON reply", payload["error"])

    def test_unreachable_is_503_and_non_json_2xx_is_502(self) -> None:
        opener = ScriptedOpener(cmd=[refused(), TimeoutError("timed out"), FakeResponse(body=b"<html>"),
                                     FakeResponse(body=b"[1, 2]", status=201)])
        link = self._link(opener)
        status, payload = link.forward_cmd({"cmd": "state 1"})
        self.assertEqual(status, 503)
        self.assertIs(payload["ok"], False)
        self.assertTrue(payload["error"].startswith("pi5 not reachable: "), payload)
        self.assertIn("refused", payload["error"])
        status, payload = link.forward_cmd({"cmd": "state 1"})
        self.assertEqual(status, 503)
        self.assertIn("timed out", payload["error"])
        # A 2xx without a JSON object is a bad gateway, never a 200 saying ok:false.
        status, payload = link.forward_cmd({"cmd": "state 1"})
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"ok": False, "error": "non-JSON reply from pi5 (HTTP 200)"})
        status, payload = link.forward_cmd({"cmd": "state 1"})
        self.assertEqual((status, payload), (502, {"ok": False, "error": "non-JSON reply from pi5 (HTTP 201)"}))


class StopTests(unittest.TestCase):
    """stop() against a real socket. A stream that sent one event and then
    went silent leaves the link thread blocked in a read; stop() must return
    at once (closing the response from here would wait on the reader's
    buffer lock for the whole read) and the thread must end: on Linux the
    socket shutdown wakes it, on Windows the read timeout does."""

    def test_stop_returns_at_once_and_the_thread_ends(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        conns: List[socket.socket] = []

        def serve() -> None:
            conn, _ = srv.accept()
            conns.append(conn)
            conn.recv(4096)                                     # the GET
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\n\r\n"
                         b"data: " + json.dumps(pi5_model(tokens={7: 1})).encode("utf-8") + b"\n\n")
            # ...and then silence: the reader blocks in readline().

        threading.Thread(target=serve, name="silent-pi5", daemon=True).start()
        rec = Recorder()
        got = threading.Event()
        link = Pi5Link(on_model=lambda m: (rec.on_model(m), got.set()), on_alive=rec.on_alive,
                       base_url=f"http://127.0.0.1:{port}")
        saved = quiniela.STREAM_READ_TIMEOUT_S
        quiniela.STREAM_READ_TIMEOUT_S = 2.0                    # bounds the Windows case
        try:
            link.start()
            self.assertTrue(got.wait(5), "the first event never arrived")
            self.assertEqual(rec.models[0]["horses"]["7"]["tokens"], 1)
            t0 = time.monotonic()
            link.stop()
            self.assertLess(time.monotonic() - t0, 0.5, "stop() blocked on the reader")
            link._thread.join(quiniela.STREAM_READ_TIMEOUT_S + 3)
            self.assertFalse(link._thread.is_alive(), "the link thread did not end")
            self.assertEqual(len(rec.models), 1)
            link.stop()                                          # idempotent, socket already gone
        finally:
            quiniela.STREAM_READ_TIMEOUT_S = saved
            for c in conns:
                c.close()
            srv.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
class RouteCase(unittest.TestCase):
    """Swap in a fresh relay + link so the module singletons stay untouched."""

    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.wall = FakeClock(1_700_000_000.0)
        self.board = BoardRelay(clock=self.clock, wall=self.wall)
        self.link = Pi5Link(on_model=self.board.apply_model, on_alive=self.board.touch,
                            base_url="http://127.0.0.1:9", clock=self.clock)
        self._saved = (quiniela.board, quiniela.link, quiniela.SSE_HEARTBEAT_S)
        quiniela.board, quiniela.link = self.board, self.link
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        quiniela.board, quiniela.link, quiniela.SSE_HEARTBEAT_S = self._saved


def fake_pi5_app() -> Flask:
    """Just pi5's cmd route: 400 for an empty / missing cmd, else an echo."""
    logging.getLogger("werkzeug").setLevel(logging.ERROR)     # no access log in the test output
    app = Flask("test_fake_pi5")

    @app.route("/api/quiniela/cmd", methods=["POST"])
    def cmd():
        body = request.get_json(silent=True)
        c = body.get("cmd") if isinstance(body, dict) else None
        if not isinstance(c, str) or not c.strip():
            return jsonify({"ok": False, "error": "empty command"}), 400
        return jsonify({"ok": True, "echo": c.strip()})

    return app


class ApiTests(RouteCase):
    def test_import_started_nothing(self) -> None:
        self.assertTrue(quiniela._started)
        self.assertIsNone(self._saved[1]._thread, "the module link thread was never started")
        self.assertFalse(self._saved[1].connected)

    def test_get_relays_the_last_model(self) -> None:
        resp = self.client.get("/api/quiniela")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        m = resp.get_json()
        self.assertEqual(set(m), CONTRACT_KEYS)
        self.assertIs(m["link_ok"], False)
        self.assertEqual(m["board_states"], [])
        self.assertEqual((m["race_state"], m["race_state_name"]), (0, "PRE_RACE"))
        self.assertEqual(len(m["horses"]), 20)
        self.assertIsNone(m["leader"])
        pi5 = pi5_model(race_state=2, tokens={7: 23, 3: 2})
        self.assertTrue(self.board.apply_model(pi5))
        m = self.client.get("/api/quiniela").get_json()
        self.assertEqual(m, pi5)
        self.assertEqual(m["horses"]["7"]["cup"], mac_of(7))
        self.assertEqual(m["board_states"], [1, 2, 3, 4, 5])
        self.assertEqual(m["leader"], 7)

    def test_cmd_is_relayed_to_pi5_and_503_when_it_is_gone(self) -> None:
        srv = make_server("127.0.0.1", 0, fake_pi5_app(), threaded=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        quiniela.link = Pi5Link(on_model=self.board.apply_model, on_alive=self.board.touch,
                                base_url=f"http://127.0.0.1:{srv.server_port}")
        try:
            resp = self.client.post("/api/quiniela/cmd", json={"cmd": "  state 1 "})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json(), {"ok": True, "echo": "state 1"})
            for bad in ({"cmd": ""}, {"nope": 1}, None):
                resp = self.client.post("/api/quiniela/cmd", json=bad)
                self.assertEqual(resp.status_code, 400, bad)
                self.assertEqual(resp.get_json(), {"ok": False, "error": "empty command"})
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(2.0)
        resp = self.client.post("/api/quiniela/cmd", json={"cmd": "state 1"})
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertIs(body["ok"], False)
        self.assertTrue(body["error"].startswith("pi5 not reachable: "), body)

    def test_existing_routes_still_there(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/display").status_code, 200)

    def test_the_page_carries_the_board_and_its_results_screen(self) -> None:
        html = self.client.get("/display").get_data(as_text=True)
        for needle in ('id="quiniela-board"', 'class="qb-stage"', 'id="qb-col-left"', 'id="qb-col-right"',
                       'id="qb-results"', 'id="qb-result-win"', 'id="qb-result-place"', 'id="qb-result-show"',
                       "One token drawn from each cup", "Drawn token takes the prize",
                       "js/quiniela_board.js", "css/quiniela_board.css"):
            self.assertIn(needle, html)
        self.assertEqual(html.count('class="qb-result-prize"'), 3)
        js = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
        for needle in ("'Official results coming'", "'Official results'", "m.results", "board_states"):
            self.assertIn(needle, js)
        self.assertNotIn("[1, 2, 3, 4", js, "the page never hard-codes pi5's board states")

    def test_the_page_asks_for_the_css_and_js_it_has(self) -> None:
        # A pull and a restart serve the new code without a hard reload: every
        # stylesheet and script URL carries its file's modification time.
        html = self.client.get("/display").get_data(as_text=True)
        for path in ("css/ddm_style.css", "css/quiniela_board.css", "js/quiniela_board.js"):
            mtime = int((HERE / "static" / path).stat().st_mtime)
            self.assertIn(f'/static/{path}?v={mtime}"', html, path)
            r = self.client.get(f"/static/{path}?v={mtime}")
            self.assertEqual(r.status_code, 200, path)
            r.close()
        self.assertNotIn("url_for('static'", (HERE / "templates" / "base.html").read_text(encoding="utf-8"))


class StreamTests(RouteCase):
    def test_generator_initial_then_ping_then_update(self) -> None:
        gen = sse_events(self.board, heartbeat_s=0.05, wall=self.wall)
        first = next(gen)
        self.assertTrue(first.startswith("data: "))
        self.assertTrue(first.endswith("\n\n"))
        model = json.loads(first[len("data: "):])
        self.assertFalse(model["link_ok"])
        self.assertEqual(model["board_states"], [])
        self.assertEqual(self.board.subscriber_count(), 1)

        ping = next(gen)
        self.assertEqual(ping, ": heartbeat\n\nevent: ping\ndata: {\"ts\":%s}\n\n"
                         % json.dumps(round(self.wall.now, 3)))

        self.board.apply_model(pi5_model(tokens={7: 5}))
        update = next(gen)
        self.assertTrue(update.startswith("data: "))
        self.assertEqual(update, "data: " + self.board.model_json() + "\n\n")
        self.assertEqual(json.loads(update[6:])["total_tokens"], 5)

        gen.close()
        self.assertEqual(self.board.subscriber_count(), 0, "unsubscribed on close")

    def test_route_headers_first_event_and_ping(self) -> None:
        quiniela.SSE_HEARTBEAT_S = 0.1
        self.board.apply_model(pi5_model(tokens={7: 3}))
        resp = self.client.get("/api/quiniela/stream", buffered=False)
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, "text/event-stream")
            self.assertEqual(resp.headers["Cache-Control"], "no-cache")
            self.assertEqual(resp.headers["X-Accel-Buffering"], "no")
            self.assertEqual(resp.headers["Connection"], "keep-alive")
            chunks = iter(resp.response)
            first = next(chunks).decode("utf-8")
            self.assertEqual(first, "data: " + self.board.model_json() + "\n\n")
            self.assertEqual(json.loads(first[6:])["horses"]["7"]["tokens"], 3)
            second = next(chunks).decode("utf-8")
            self.assertTrue(second.startswith(": heartbeat\n\nevent: ping\ndata: {\"ts\":"), second)
            self.assertTrue(second.endswith("}\n\n"))
            self.assertEqual(self.board.subscriber_count(), 1)
        finally:
            resp.close()
        self.assertEqual(self.board.subscriber_count(), 0, "closing the response unsubscribes")


class RaceSlideTests(RouteCase):
    """The countdown and the roster ("field and odds") read La Quiniela's
    model as the splash relays it: in the playlist only with something to
    show, shells the page fills from the live model, no copy of the race or
    of the glyphs of their own."""

    def redesign(self, **kw) -> "fake_pi5.FakePi5":
        return fake_pi5.FakePi5(fake_pi5.PHASES["open"], tokens=fake_pi5.REDESIGN_TOKENS,
                                scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                                names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS,
                                names_rev=2, **kw)

    def relay(self, fake) -> Dict[str, Any]:
        self.assertTrue(self.board.apply_model(json.loads(fake.model_json())))
        return self.board.model()

    def slides(self) -> Dict[str, Any]:
        pages = {p["id"]: p for p in server.load_splash_pages()}
        return {sid: server._splash_page_to_slide(pages[sid]) for sid in ("countdown", "horse_roster")}

    def test_nothing_heard_from_pi5_no_race_slides(self) -> None:
        self.assertIsNone(server.race_post_at())
        self.assertFalse(server.roster_ready())
        self.assertEqual(self.slides(), {"countdown": None, "horse_roster": None})

    def test_a_post_time_and_a_named_field_put_both_in(self) -> None:
        fake = self.redesign(post_at=1_809_212_220.0)
        m = self.relay(fake)
        self.assertEqual(server.race_post_at(), 1_809_212_220.0)
        self.assertEqual(m["race"]["post_local"] is not None, True)
        slides = self.slides()
        for sid in ("countdown", "horse_roster"):
            self.assertEqual(slides[sid]["splash_id"], sid)
            self.assertNotIn("html", slides[sid], "a shell from the template cache; the page fills it")
        fake.set_post(None)
        self.relay(fake)
        self.assertIsNone(self.slides()["countdown"], "no post time: the countdown leaves the playlist")
        self.assertIsNotNone(self.slides()["horse_roster"])
        for _ in range(3):
            self.assertFalse(any(s["splash_id"] == "countdown" for s in server.build_playlist() if s.get("type") == "splash"))

    def test_a_field_without_names_is_no_roster(self) -> None:
        fake = self.redesign(post_at=None)
        for n in list(fake.names):
            fake.set_name(n, "")
        self.relay(fake)
        self.assertFalse(server.roster_ready())
        self.assertIsNone(self.slides()["horse_roster"])

    def test_the_roster_is_a_shell_of_the_boards_rows(self) -> None:
        with server.app.test_request_context("/"):
            html = server.render_template("splash/horse_roster.html")
        for needle in ('class="qb qb--embed qb-roster"', 'data-look="dots"', 'data-roster="board"',
                       'data-roster-col="0"', 'data-roster-col="1"', 'data-roster="post"', ">Odds<", ">Horse<"):
            self.assertIn(needle, html)
        for gone in ("splash-tote-digit", "splash-tote-cell", "_DOT_PATTERNS", "splash-saddle--pos", "6:57 PM ET"):
            self.assertNotIn(gone, html, "the roster's own dots and cloths are gone")
        css = (HERE / "static" / "css" / "ddm_style.css").read_text(encoding="utf-8")
        for gone in (".splash-tote-digit", ".splash-tote-cell", ".splash-saddle--pos-1 ", ".splash-tote-staleness"):
            self.assertNotIn(gone, css)
        board_css = (HERE / "static" / "css" / "quiniela_board.css").read_text(encoding="utf-8")
        self.assertIn(".qb.qb--embed {", board_css)
        js = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
        for needle in ("window.ddmQuiniela", "function fillRoster", "makeRow(n)", "h.odds"):
            self.assertIn(needle, js)

    def test_the_countdown_reads_the_race(self) -> None:
        with server.app.test_request_context("/"):
            html = server.render_template("splash/countdown.html")
        for needle in ('data-race="event"', 'data-race="when"', "countdown-off", "And they're off",
                       'data-countdown="days"', 'data-countdown="seconds"'):
            self.assertIn(needle, html)
        for gone in ("Kentucky Derby 2026", "May 2", "6:57 PM ET"):
            self.assertNotIn(gone, html, "no race of its own")
        page = (HERE / "templates" / "slideshow.html").read_text(encoding="utf-8")
        self.assertNotIn("DDM_POST_TIME_ISO", page)
        self.assertIn("('horse_roster',      'splash/horse_roster.html')", page)
        for needle in ("liveRace()", "race.post_at", "'is-off'", "postLine(race)", "q.fillRoster(board)"):
            self.assertIn(needle, page)
        self.assertFalse((HERE / "race_poller.py").exists(), "the race poller is gone")


class LookTests(RouteCase):
    """?look= on the board's URL, config.QUINIELA_LOOK for the default."""

    def setUp(self) -> None:
        super().setUp()
        self._look = getattr(config, "QUINIELA_LOOK", None)
        config.QUINIELA_LOOK = "impact"
        server._bad_looks.clear()

    def tearDown(self) -> None:
        config.QUINIELA_LOOK = self._look
        super().tearDown()

    def look_of(self, path: str) -> str:
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, path)
        html = resp.get_data(as_text=True)
        start = html.index('id="quiniela-board"')
        tag = html[start:html.index(">", start)]
        self.assertEqual(tag.count("data-look="), 1)
        return tag.split('data-look="')[1].split('"')[0]

    def test_the_repo_ships_the_tote_look(self) -> None:
        self.assertEqual(self._look, "dots", "the TV's board is the tote look; ?look=impact and ?look=numbers stay")
        self.assertEqual(server.DEFAULT_LOOK, "dots", "and so is the code's default")
        with server.app.test_request_context("/"):
            html = server.render_template("splash/quiniela_live.html")
        self.assertIn('data-look="dots"', html, "the template's default too")
        js = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
        self.assertIn("LOOKS.includes(board.dataset.look) ? board.dataset.look : 'dots'", js, "and the script's")

    def test_the_url_names_the_look(self) -> None:
        self.assertEqual(self.look_of("/display"), "impact")
        self.assertEqual(self.look_of("/display?look=dots"), "dots")
        self.assertEqual(self.look_of("/display?look=impact"), "impact")
        self.assertEqual(self.look_of("/display?look=numbers"), "numbers")
        self.assertEqual(self.look_of("/display?look=DOTS"), "dots", "case does not matter")
        self.assertEqual(self.look_of("/display?look=neon"), "impact", "a look nobody knows is the default")
        self.assertEqual(self.look_of("/display?look="), "impact")

    def test_config_is_the_default_and_the_url_wins(self) -> None:
        config.QUINIELA_LOOK = "dots"
        self.assertEqual(self.look_of("/display"), "dots")
        self.assertEqual(self.look_of("/display?look=impact"), "impact")
        self.assertEqual(self.look_of("/display?look=neon"), "dots")
        config.QUINIELA_LOOK = " Numbers "
        self.assertEqual(self.look_of("/display"), "numbers")

    def test_a_config_value_that_names_no_look_is_the_default_and_logged_once(self) -> None:
        config.QUINIELA_LOOK = "dot"
        with self.assertLogs("splash_display", level="WARNING") as cm:
            self.assertEqual(self.look_of("/display"), "dots")
            self.assertEqual(self.look_of("/display"), "dots")
            self.assertEqual(self.look_of("/display?look=impact"), "impact")
        self.assertEqual(len(cm.output), 1, cm.output)
        self.assertIn("QUINIELA_LOOK", cm.output[0])
        del config.QUINIELA_LOOK
        self.assertEqual(self.look_of("/display"), "dots", "a config without the key is the tote look")
        self.assertEqual(server.board_look(None), "dots")
        self.assertEqual(server.board_look(7), "dots")

    def test_the_root_redirect_keeps_the_look(self) -> None:
        resp = self.client.get("/?look=dots")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/display?look=dots"), resp.headers["Location"])
        self.assertTrue(self.client.get("/").headers["Location"].endswith("/display"))

    def test_the_page_marks_the_totes_fields(self) -> None:
        html = self.client.get("/display?look=dots").get_data(as_text=True)
        # figures: the pot, three prizes, a row's bets (the template), the results' three counts and three prizes
        self.assertEqual(html.count('data-tote="num"'), 1 + 3 + 1 + 3 + 3)
        # names: a row's (the template), the results' three, and the countdown's AND THEY'RE OFF
        self.assertEqual(html.count('data-tote="name"'), 1 + 3 + 1)
        for sign in ('class="qb-saddle"', 'class="qb-result-saddle"', 'id="qb-banner-text"', 'id="qb-toast-name"',
                     'class="qb-result-place"', 'class="qb-prize-k"'):
            tag = html[html.index(sign) - 40:html.index(">", html.index(sign))]
            self.assertNotIn("data-tote", tag, f"{sign} is a cloth or a sign, not a tote field")

    def test_the_stylesheet_and_the_script_know_the_looks(self) -> None:
        css = (HERE / "static" / "css" / "quiniela_board.css").read_text(encoding="utf-8")
        self.assertIn('font-family: "DDM Tote";', css)
        self.assertIn('url("../fonts/DDMTote.ttf?v=2")', css)      # the face's version (v2: the degree sign)
        self.assertIn('.qb[data-look="dots"] [data-tote="name"]', css)
        self.assertIn('.qb:is([data-look="dots"], [data-look="numbers"]) [data-tote="num"]', css)
        self.assertIn('.qb:is([data-look="dots"], [data-look="numbers"]) .qb-crawl-text', css)
        # Both looks are one stylesheet: above the tote look's section nothing
        # asks which look it is; inside it every rule does, so the Impact look
        # cannot be touched by it (the face and the custom properties aside).
        before, marker, tote = css.partition("The tote look: data-look")
        self.assertTrue(marker)
        self.assertNotIn("data-look", before)
        tote = re.sub(r"/\*.*?\*/", "", tote.split("*/", 1)[1], flags=re.S)
        selectors = [rule.split("{")[0].strip() for rule in tote.split("}") if "{" in rule]
        self.assertGreater(len(selectors), 10)
        for selector in selectors:
            if selector in ("@font-face", ".qb"):
                continue
            for one in selector.split(","):
                self.assertIn("data-look", one, f"a tote-look rule must name its look: {one.strip()!r}")
        js = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
        for needle in ("board.dataset.look", "'impact', 'dots', 'numbers'", "function fitTiles", "qb-crawl-text", "DDM Tote",
                       "function layoutStrips", "function updateScroll", "STRIP_TILE_MIN", "--qb-strip-tile-w",
                       "function placeCounted", "m.hand_counted === true", "'HAND COUNTED'", "'COUNTED'"):
            self.assertIn(needle, js)
        # the hand count's tag: one element in the template, no tote field of its own (the count of those above stands),
        # lettered in Impact in every look: no rule hands it a face, a tile or a glow, the tote look only colours it
        with server.app.test_request_context("/"):
            html = server.render_template("splash/quiniela_live.html")
        self.assertEqual(html.count('id="qb-counted"'), 1)
        self.assertRegex(html, r'<div class="qb-counted" id="qb-counted" aria-hidden="true">HAND COUNTED</div>')
        self.assertIn('.qb:is([data-look="dots"], [data-look="numbers"]) .qb-counted', css)
        self.assertIn(".qb-counted.is-on { display: block; }", css)
        bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        tag_rules = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", bare) if ".qb-counted" in sel]
        self.assertEqual(len(tag_rules), 3, "the base rule, .is-on and the tote looks' colour: " + str([r[0] for r in tag_rules]))
        for selector, body in tag_rules:
            for prop in ("font-family", "background-image", "text-shadow"):
                self.assertNotIn(prop, body, f"{selector}: the tag takes the board's own face, as every label does")
        self.assertNotRegex(js, r"\bNAME_PITCH\b", "the rows' pitch-shrinking fit is gone (the results screen keeps its own)")
        # impact and numbers: the strip's wrappers are no boxes, outside the tote section
        self.assertIn(".qb-strip-cell,\n.qb-strip,\n.qb-namebox { display: contents; }", css.replace("\r\n", "\n"))


def find_chrome() -> Optional[str]:
    """A Chrome or Chromium to run the board's script in, headless: DDM_CHROME
    if set, else the usual names on PATH (DevPi's chromium), else the usual
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


CHROME = find_chrome()

# Stands in for the page's network (the board fetches /api/quiniela once and
# listens to /api/quiniela/stream): every model arrives as a stream message,
# one after another, and after each the board's figures are read off the
# DOM, past the 500 ms count tween. The readings end up in #probe as JSON.
# strips: each row's strip, measured in tiles (served with the stylesheet,
# the tote look's rows); pulses: the rows that pulsed since the last reading.
# A "model" {"__stage": [w, h]} is no model: the page's stage takes that
# size and the window says it was resized, as a TV's would; {"__wait": ms}
# only lets time pass; {"__feed": model} is a model that is read in the same turn
# it arrives, not after a wait; {"__stall": ms} makes the crawl's next frame that late;
# {"__sample": ms} runs the page that long and reports it
# frame by frame: how far any tile of the dots crawl ever was from where it
# started (drift); each time the sign steps, [ms, offset, generation,
# pending, what the tiles read]; and cells: every cell of the message it
# began with, the look of the tile that first showed it (class | inline
# background | colour | opacity | glow). crawl: the crawl's items, one copy, the live
# ones carrying their kind (the dots crawl has no track: its items are
# window.ddmQuiniela.crawl()'s, with its tiles, and what they read), and
# whether the track is there and animated; on the track the first re-bet
# note's computed look (colour | opacity | glow) beside the horse's name
# before it and a replaced horse's struck name's.
# roster: the roster slide's rows
# when the page has one
# (filled by window.ddmQuiniela.fillRoster after each model, as the
# slideshow does when the slide loads).
BOARD_PROBE_JS = r"""
(() => {
    const MODELS = __MODELS__;
    const WAIT_MS = 700;
    const out = [];
    // Headless Chrome under a virtual time budget runs timers but next to no
    // frames, so requestAnimationFrame is driven by a timer here and the
    // count's tween runs as it does on a screen. CSS animations still do not
    // run, so a row's pulse shows as its is-pulse class, which the probe
    // takes off after each reading as the pulse's end would.
    // {"__stall": ms}: the crawl's next frame comes that late, as after a stalled main thread.
    let stallNext = 0;
    window.requestAnimationFrame = (cb) => {
        const late = cb.name === 'crawlFrame' && stallNext ? stallNext : 16;
        if (cb.name === 'crawlFrame') stallNext = 0;
        return setTimeout(() => cb(performance.now()), late);
    };
    window.cancelAnimationFrame = (id) => clearTimeout(id);
    const text = (sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : null; };
    const r2 = (v) => Math.round(v * 100) / 100;
    function strip(row) {
        const q = (s) => row.querySelector(s);
        const st = q('.qb-strip'), box = q('.qb-namebox'), name = q('.qb-name'), bets = q('.qb-bets');
        const tile = parseFloat(getComputedStyle(st).getPropertyValue('--qb-strip-tile-w'))
                     || parseFloat(getComputedStyle(st).fontSize) * 0.75;
        const rr = row.getBoundingClientRect(), sr = st.getBoundingClientRect();
        const a = name.getAnimations()[0];
        const shift = (k) => { const m = /translateX\((-?[\d.]+)px\)/.exec(k.transform || ''); return m ? -Number(m[1]) / tile : 0; };
        const frames = a ? a.effect.getKeyframes() : [];
        const duration = a ? a.effect.getTiming().duration : null;
        return {
            display: getComputedStyle(st).display, columns: getComputedStyle(row).gridTemplateColumns.split(' ').length,
            font: parseFloat(getComputedStyle(st).fontSize), tile: r2(tile), tiles: r2(sr.width / tile),
            area: r2(box.getBoundingClientRect().width / tile), bets: r2(bets.getBoundingClientRect().width / tile),
            need: r2(name.getBoundingClientRect().width / tile), clip: getComputedStyle(box).overflow,
            gap: r2(sr.left - q('.qb-saddle').getBoundingClientRect().right),
            padRight: parseFloat(getComputedStyle(row).paddingRight),
            leftover: r2(rr.right - 2 - parseFloat(getComputedStyle(row).paddingRight) - sr.right),
            betsRight: r2(sr.right - bets.getBoundingClientRect().right),
            opacity: getComputedStyle(bets).opacity, scrolls: name.getAnimations().length, playState: a ? a.playState : null,
            over: frames.length ? r2(shift(frames[frames.length - 1])) : 0, duration: duration,
            steps: frames.slice(1, -1).map((k) => [Math.round(k.offset * duration), r2(shift(k))]),
        };
    }
    function crawl() {
        const snapshot = window.ddmQuiniela && window.ddmQuiniela.crawl ? window.ddmQuiniela.crawl() : null;
        const track = document.getElementById('qb-track');
        const tiles = [...document.querySelectorAll('.qb-ct')];
        const first = tiles.length ? tiles[0].getBoundingClientRect() : null;
        const look = (el) => { if (!el) return null; const s = getComputedStyle(el); return s.color + '|' + s.opacity + '|' + s.textShadow; };
        const note = track ? track.querySelector('.qb-crawl-note') : null;
        const base = { snapshot: snapshot, tiles: tiles.length, text: tiles.map((t) => t.textContent || ' ').join(''),
                       room: r2(document.querySelector('.qb-crawl').getBoundingClientRect().width),
                       tileW: first ? r2(first.width) : null, tileH: first ? r2(first.height) : null,
                       classes: tiles.map((t) => t.className), colors: tiles.map((t) => t.style.backgroundColor + '|' + t.style.color),
                       note: note ? { note: look(note), name: look(note.previousElementSibling), text: note.textContent,
                                      struck: look(track.querySelector('.qb-crawl-was')) } : null,
                       animation: getComputedStyle(track).animationName, trackDisplay: getComputedStyle(track).display };
        if (snapshot) return Object.assign(base, { items: snapshot.items, marks: [] });
        const items = [...document.querySelectorAll('#qb-track .qb-crawl-item')].map((el) => {
            const live = el.querySelector('[data-live]');
            return live ? live.dataset.live + ':' + live.textContent : el.textContent.trim();
        });
        const again = items.indexOf(items[0], 1);
        const marks = [...document.querySelectorAll('#qb-track [data-live="post"]')].map((el) => el.dataset.mark || '');
        return Object.assign(base, { items: again > 0 ? items.slice(0, again) : items, marks });
    }
    function roster() {
        const root = document.querySelector('[data-roster="board"]');
        if (!root) return null;
        const panel = root.closest('.splash-tote-board') || root;
        const pr = panel.getBoundingClientRect();
        const bw = parseFloat(getComputedStyle(panel).borderRightWidth) || 0;
        return [...root.querySelectorAll('[data-roster-col]')].map((col, c) => [...col.querySelectorAll('.qb-row')].map((row) => {
            const rr = row.getBoundingClientRect(), cr = col.getBoundingClientRect();
            return Object.assign(strip(row), {
                horse: Number(row.dataset.horse), col: c, name: row.querySelector('.qb-name').textContent,
                odds: row.querySelector('.qb-bets').textContent, empty: row.classList.contains('is-empty'),
                inside: rr.right <= cr.right + 0.5 && rr.bottom <= pr.bottom - bw + 0.5 && rr.left >= cr.left - 0.5,
            });
        }));
    }
    function read() {
        const board = document.getElementById('quiniela-board');
        const rows = {};
        const strips = {};
        for (const el of document.querySelectorAll('.qb-rows [data-horse]')) {
            rows[el.dataset.horse] = el.querySelector('.qb-bets').textContent.trim();
            strips[el.dataset.horse] = strip(el);
        }
        const results = {};
        for (const p of ['win', 'place', 'show']) {
            results[p] = { horse: document.getElementById('qb-result-' + p).dataset.horse,
                           bets: text('#qb-result-' + p + ' .qb-result-count'),
                           prize: text('#qb-result-' + p + ' .qb-result-prize') };
        }
        const toast = document.getElementById('qb-toast');
        const pulses = [];
        for (const el of document.querySelectorAll('.qb-rows .qb-row.is-pulse')) {
            pulses.push(Number(el.dataset.horse));
            el.classList.remove('is-pulse');
        }
        // The header, where the POT figure and the hand count's tag sit: each box as
        // [left, top, width, height], and the figure's own text (a Range).
        const box = (sel) => { const b = document.querySelector(sel).getBoundingClientRect(); return [r2(b.left), r2(b.top), r2(b.width), r2(b.height)]; };
        const fig = document.createRange();
        fig.selectNodeContents(document.getElementById('qb-pot'));
        const fb = fig.getBoundingClientRect();
        const tag = document.getElementById('qb-counted');
        const header = { pot: box('#qb-pot'), figure: [r2(fb.left), r2(fb.top), r2(fb.width), r2(fb.height)],
                         label: box('.qb-pot-label'), win: box('.qb-prize--win'), place: box('.qb-prizes .qb-prize:nth-child(2)'),
                         show: box('.qb-prizes .qb-prize:nth-child(3)'), banner: box('#qb-banner'), logo: box('.qb-logo') };
        // The tag's lettering: its computed face, size, colour and box; the width of its text (a Range, so the pill's
        // padding is not in it) against the same text set in Impact, in Anton and in the browser's plain sans-serif at
        // the tag's own size and spacing; and the height of its capitals (a canvas in the tag's own font).
        const tagCs = getComputedStyle(tag);
        const faceWidth = (family) => {
            const s = document.createElement('span');
            s.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;text-transform:uppercase;font-family:' + family
                + ';font-size:' + tagCs.fontSize + ';letter-spacing:' + tagCs.letterSpacing;
            s.textContent = 'HAND COUNTED';
            board.appendChild(s);
            const w = s.getBoundingClientRect().width;
            board.removeChild(s);
            return r2(w);
        };
        const textRange = document.createRange();
        textRange.selectNodeContents(tag);
        const canvas = document.createElement('canvas').getContext('2d');
        canvas.font = tagCs.fontSize + ' ' + tagCs.fontFamily;
        const counted = { on: tag.classList.contains('is-on'), text: tag.textContent.trim(), display: tagCs.display,
                          hidden: tag.getAttribute('aria-hidden'), rect: box('#qb-counted'), font: tagCs.fontFamily,
                          boardFont: getComputedStyle(board).fontFamily, color: tagCs.color, background: tagCs.backgroundImage,
                          border: tagCs.borderTopWidth, size: tagCs.fontSize, spacing: tagCs.letterSpacing,
                          textWidth: r2(textRange.getBoundingClientRect().width), cap: r2(canvas.measureText('H').actualBoundingBoxAscent),
                          faces: { impact: faceWidth('Impact'), anton: faceWidth('Anton'), generic: faceWidth('sans-serif') } };
        return { state: board.dataset.state, view: board.dataset.view || 'rows', look: board.dataset.look,
                 visible: board.classList.contains('is-visible'), banner: text('#qb-banner-text'),
                 pot: text('#qb-pot'), prizes: [text('#qb-prize-win'), text('#qb-prize-place'), text('#qb-prize-show')],
                 rows: rows, strips: strips, results: results, pulses: pulses, crawl: crawl(), sample: takeSample(), roster: roster(),
                 header: header, counted: counted,
                 toast: toast.classList.contains('is-shown') ? text('#qb-toast-num') + ' ' + text('#qb-toast-name') + ' ' + text('#qb-toast-delta') : null };
    }
    // {"__sample": ms}: for that long, every frame, how far any tile of the dots crawl is from where it
    // started (drift), and each time the sign steps (the snapshot's offset changes) what the tiles read.
    let sampled = null;
    async function sample(ms) {
        const tiles = [...document.querySelectorAll('.qb-ct')];
        const lefts = tiles.map((t) => t.getBoundingClientRect().left);
        const textOf = () => tiles.map((t) => t.textContent || ' ').join('');
        const first = window.ddmQuiniela.crawl();
        const startText = textOf();
        let offset = first.offset, drift = 0, frames = 0;
        const steps = [];
        const cells = {};
        const lookOf = (t) => { const c = getComputedStyle(t);
                                return [t.className, t.style.backgroundColor, c.color, c.opacity, c.textShadow].join('|'); };
        const t0 = performance.now();
        await new Promise((resolve) => {
            const tick = () => {
                frames++;
                for (let i = 0; i < tiles.length; i++) drift = Math.max(drift, Math.abs(tiles[i].getBoundingClientRect().left - lefts[i]));
                const s = window.ddmQuiniela.crawl();
                if (s.offset !== offset) {
                    offset = s.offset;
                    steps.push([Math.round(performance.now() - t0), s.offset, s.generation, s.pending, textOf()]);
                }
                if (s.generation === first.generation && s.length) {
                    for (let i = 0; i < tiles.length; i++) {
                        const k = s.offset + i;
                        if (k >= 0 && !((k % s.length) in cells)) cells[k % s.length] = lookOf(tiles[i]);
                    }
                }
                if (performance.now() - t0 < ms) requestAnimationFrame(tick); else resolve();
            };
            requestAnimationFrame(tick);
        });
        sampled = { frames: frames, drift: r2(drift), tiles: tiles.length, start: [first.offset, first.generation, startText], steps: steps,
                    cells: cells };
    }
    function takeSample() { const s = sampled; sampled = null; return s; }
    let stream = null;
    window.fetch = () => new Promise(() => {});
    window.ddmSlideshow = { hold() {}, release() {} };
    window.EventSource = class {
        constructor() { stream = this; setTimeout(feed, 0); }
        addEventListener() {}
        close() {}
    };
    async function feed() {
        for (const m of MODELS) {
            if (m.__stage) {
                const stage = document.getElementById('qb-stage');
                stage.style.width = m.__stage[0] + 'px';
                stage.style.height = m.__stage[1] + 'px';
                window.dispatchEvent(new Event('resize'));
            } else if (m.__wait) {
                await new Promise((resolve) => setTimeout(resolve, m.__wait));
            } else if (m.__feed) {
                stream.onmessage({ data: JSON.stringify(m.__feed) });
                out.push(read());                      // in the same turn: nothing has had a frame to catch up
                continue;
            } else if (m.__stall) {
                stallNext = m.__stall;
            } else if (m.__sample) {
                await sample(m.__sample);
            } else {
                stream.onmessage({ data: JSON.stringify(m) });
                const root = document.querySelector('[data-roster="board"]');
                if (root && window.ddmQuiniela) window.ddmQuiniela.fillRoster(root);
            }
            await new Promise((resolve) => setTimeout(resolve, WAIT_MS));
            out.push(read());
            for (const el of document.querySelectorAll('#qb-track [data-live="post"]')) el.dataset.mark = String(out.length);
        }
        document.getElementById('probe').textContent = JSON.stringify(out);
    }
})();
"""


def run_board(models: List[dict], look: str = "impact", styled: bool = False,
              stage: Tuple[int, int] = (1920, 1080), roster: bool = False, css: str = "",
              slow_face: float = 0.0, slow: Optional[Dict[str, float]] = None, query: str = "") -> List[dict]:
    """The board's real template and script in a page of their own, fed
    `models` in turn; what the board showed after each. Styled, the page is
    served over loopback with the board's stylesheet and fonts, the board on
    a `stage` (1920x1080 unless said), which is what the tote look's rows
    need to be measured; otherwise it is a file with the template and the
    script alone. roster: the slideshow's roster slide as well (styled),
    in a slide layer of its own under the board, as on the TV. css: extra
    rules after the stylesheet (styled), to put the page in a corner of
    its own. slow_face: seconds the tote face takes to arrive (styled), a
    kiosk loading cold, so a model comes before it. slow: the same for any
    static file, {"Anton-Regular.ttf": 1.5}. query: the URL's query string
    ("crawl_tps=60"), which the page's script reads."""
    with server.app.test_request_context("/"):
        board_html = server.render_template("splash/quiniela_live.html", quiniela_look=look)
        roster_html = server.render_template("splash/horse_roster.html") if roster else ""
    probe = BOARD_PROBE_JS.replace("__MODELS__", json.dumps(models))
    tmp = Path(tempfile.mkdtemp(prefix="qb_page_"))
    srv = thread = None
    try:
        if styled:
            # The board fills a 1920x1080 stage, as it fills #slideshow-stage on
            # the TV: a headless window's viewport is its size less a frame.
            page = ('<!doctype html><html><head><meta charset="utf-8">'
                    + ('<link rel="stylesheet" href="/static/css/ddm_style.css">' if roster else '')
                    + '<link rel="stylesheet" href="/static/css/quiniela_board.css">'
                    + (f'<style>{css}</style>' if css else '') + '</head><body style="margin: 0">\n'
                    f'<div id="qb-stage" style="position: relative; width: {stage[0]}px; height: {stage[1]}px; '
                    'overflow: hidden">'
                    + ('<div class="slide is-active">' + roster_html + '</div>' if roster else '') + board_html
                    + '</div>\n<pre id="probe" style="display: none"></pre>\n<script>' + probe
                    + '</script>\n<script src="/static/js/quiniela_board.js"></script>\n</body></html>\n')
            app = Flask("board_page", static_folder=str(HERE / "static"), static_url_path="/static")
            app.add_url_rule("/", "page", lambda: page)
            delays = dict(slow or {})
            if slow_face:
                delays["DDMTote.ttf"] = slow_face
            if delays:
                @app.before_request
                def _slow_files():
                    for name, seconds in delays.items():
                        if request.path.endswith(name):
                            time.sleep(seconds)
            logging.getLogger("werkzeug").setLevel(logging.ERROR)      # no line per request
            srv = make_server("127.0.0.1", 0, app, threaded=True)
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{srv.server_port}/" + ("?" + query if query else "")
        else:
            script = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
            page = ('<!doctype html><html><head><meta charset="utf-8"></head><body>\n' + board_html
                    + '\n<pre id="probe"></pre>\n<script>' + probe + '</script>\n<script>' + script
                    + '</script>\n</body></html>\n')
            path = tmp / "board.html"
            path.write_text(page, encoding="utf-8")
            url = path.resolve().as_uri() + ("?" + query if query else "")
        cmd = [CHROME, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--hide-scrollbars", "--window-size=1920,1080", "--user-data-dir=" + str((tmp / "profile").resolve()),
               "--virtual-time-budget=" + str(1000 + 900 * len(models) + sum(int(m.get("__wait", 0)) + int(m.get("__sample", 0))
                                                                  for m in models)),
               "--dump-dom", url]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        found = re.search(r'<pre id="probe"[^>]*>(.*?)</pre>', done.stdout, re.S)
        if not found or not found.group(1).strip():
            raise AssertionError(f"no reading from the page (exit {done.returncode}): {done.stderr[-2000:]}")
        return json.loads(html.unescape(found.group(1)))
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
            thread.join(2.0)
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(CHROME, "no Chrome or Chromium to run the board's script in")
class BoardPageTests(unittest.TestCase):
    """The TV page itself (static/js/quiniela_board.js) in headless Chrome:
    once betting has closed it shows pi5's closing, the figures at the post,
    whatever the live fields say, so a TV loaded after the winners' cups were
    emptied for the draw pays what they held."""

    @classmethod
    def setUpClass(cls) -> None:
        # The 2026 field's race as the harness plays it (pi5's closing rule):
        # WIN 19 / PLACE 1 / SHOW 22 held 4 / 11 / 7 of 158 tokens, pot $154
        # (20's 4 are out), WIN $92 / PLACE $39 / SHOW $23.
        fake = fake_pi5.FakePi5(fake_pi5.PHASES["open"], tokens=fake_pi5.RESULTS_TOKENS,
                                scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                                names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS, names_rev=2)
        snap = lambda: json.loads(fake.model_json())       # noqa: E731
        cls.open = snap()
        fake.set_phase(fake_pi5.PHASES["closed"])
        cls.closed = snap()
        fake.bump(7)                                         # a token after the post
        fake.set_phase(fake_pi5.PHASES["running"])
        cls.running = snap()
        for horse in fake_pi5.RESULTS_WPS:                   # the winners' cups emptied for the draw
            fake.empty(horse)
        fake.set_phase(fake_pi5.PHASES["winner"])
        cls.coming = snap()
        fake.set_results(*fake_pi5.RESULTS_WPS)
        cls.results = snap()
        fake.set_phase(fake_pi5.PHASES["after"])
        cls.after = snap()
        fake.reset({}, fake_pi5.PHASES["open"])
        cls.reopened = snap()

    WINNERS = {"win": {"horse": "19", "bets": "4", "prize": "$92"},
               "place": {"horse": "1", "bets": "11", "prize": "$39"},
               "show": {"horse": "22", "bets": "7", "prize": "$23"}}

    def test_the_models_are_what_the_test_says(self) -> None:
        self.assertEqual((self.results["pot"], self.results["prizes"]), (133.0, {"win": 80, "place": 33, "show": 20}),
                         "the live pot has fallen: the winners' cups are empty, one late token")
        self.assertEqual((self.results["closing"]["pot"], self.results["closing"]["prizes"]),
                         (154.0, {"win": 92, "place": 39, "show": 23}))
        self.assertIsNone(self.reopened["closing"])

    def test_a_page_loaded_after_the_draw_shows_the_figures_at_the_post(self) -> None:
        [seen] = run_board([self.results])
        self.assertEqual((seen["view"], seen["banner"], seen["visible"]), ("results", "Official results", True))
        self.assertEqual(seen["results"], self.WINNERS, "closing's bets and prizes, not the live model's")
        self.assertEqual((seen["pot"], seen["prizes"]), ("$154", ["$92", "$39", "$23"]))

    def test_the_frozen_board_and_official_results_coming_read_closing_too(self) -> None:
        seen = run_board([self.running])
        self.assertEqual((seen[0]["banner"], seen[0]["pot"]), ("Betting closed", "$154"))
        self.assertEqual(seen[0]["rows"]["7"], "23", "the token after the post is not on the board")
        seen = run_board([self.coming])
        self.assertEqual((seen[0]["banner"], seen[0]["view"], seen[0]["pot"]), ("Official results coming", "rows", "$154"))
        self.assertEqual([seen[0]["rows"][n] for n in ("19", "1", "22")], ["4", "11", "7"], "the emptied cups as they were")

    def test_a_page_open_through_the_whole_race(self) -> None:
        seen = run_board([self.open, self.closed, self.running, self.coming, self.results, self.after, self.reopened])
        opened, closed, running, coming, results, after, reopened = seen
        self.assertEqual((opened["banner"], opened["pot"], opened["rows"]["7"]), ("Betting open", "$154", "23"))
        self.assertEqual((closed["banner"], closed["pot"]), ("Betting closed", "$154"))
        self.assertEqual((running["pot"], running["rows"]["7"]), ("$154", "23"))
        self.assertEqual((coming["banner"], coming["pot"], coming["rows"]["19"]), ("Official results coming", "$154", "4"))
        self.assertEqual((results["view"], results["results"], results["pot"]), ("results", self.WINNERS, "$154"))
        self.assertFalse(after["visible"], "AFTER_PARTY hands the TV back")
        self.assertEqual((reopened["visible"], reopened["banner"], reopened["pot"], reopened["rows"]["7"]),
                         (True, "Betting open", "$0", "No bets"), "betting open again: live, from nothing")

    def test_without_closing_the_page_keeps_its_own_freeze(self) -> None:
        # An older pi5 serves no closing: a page open through the close keeps
        # what it showed when betting closed, and a page loaded after the
        # draw can only show the live model. The second is what closing fixes.
        old = [dict(m) for m in (self.open, self.closed, self.results)]
        for m in old:
            m.pop("closing")
        seen = run_board(old)
        self.assertEqual((seen[-1]["results"], seen[-1]["pot"]), (self.WINNERS, "$154"))
        [seen] = run_board([old[-1]])
        self.assertEqual(seen["results"]["win"], {"horse": "19", "bets": "0", "prize": "$80"})
        self.assertEqual(seen["pot"], "$133")


@unittest.skipUnless(CHROME, "no Chrome or Chromium to run the board's script in")
class HandCountTagTests(unittest.TestCase):
    """The tag by the POT figure while the pot is the host's hand count of the
    cash box (pi5's hand_counted), in headless Chrome with the stylesheet and
    the face at 1920x1080, in all three looks: HAND COUNTED, lettered in
    Impact in every look (the board's own stack, Anton where Impact is not
    installed; amber and plain in the tote looks, a gold pill in impact), hung
    to the left of the figure out of the flow, so the figure, the prizes and
    the header's columns stay where they are. The feed is tools/fake_pi5.py's
    counted pot: the 2026 race, scale pot $154."""

    LOOKS = ("impact", "dots", "numbers")
    SCALE = ["$92", "$39", "$23"]
    COUNTED = ["$91", "$38", "$23"]          # a count of $152
    GOLD = "rgb(232, 197, 58)"               # --qb-gold: the impact look's tag
    AMBER = "rgb(212, 160, 0)"               # --qb-amber: the tote looks' (the figure's colour)
    # The dot tag the Impact lettering replaced: twelve tiles of 18 px, capitals of 7 dots at a
    # 3 px pitch (21 px); the impact look's pill, which did not change, was 236.73 px.
    OLD_DOT_WIDTH = 216
    OLD_PILL_WIDTH = 236.73
    OLD_CAP_HEIGHT = 21

    @classmethod
    def setUpClass(cls) -> None:
        fake = fake_pi5.FakePi5(fake_pi5.PHASES["closed"], tokens=fake_pi5.RESULTS_TOKENS,
                                scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                                names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS, names_rev=2)
        cls.fake = fake
        snap = lambda: json.loads(fake.model_json())          # noqa: E731
        cls.plain = snap()                                    # at the post, nothing counted: $154
        fake.set_counted(154)
        cls.same = snap()                                     # counted as the scales said: only the tag differs
        fake.set_counted(152)
        cls.counted = snap()                                  # $152, WIN $91 / PLACE $38 / SHOW $23
        fake.set_phase(fake_pi5.PHASES["running"])
        cls.running = snap()
        fake.set_phase(fake_pi5.PHASES["final"])
        cls.final = snap()                                    # held through FINAL CALL, where the pot is live again
        fake.set_phase(fake_pi5.PHASES["winner"])
        fake.set_results(*fake_pi5.RESULTS_WPS)
        cls.results = snap()
        fake.set_counted(None)
        cls.cleared = snap()

    def test_the_models_are_what_the_tests_say(self) -> None:
        self.assertEqual((self.plain["pot"], self.plain["hand_counted"]), (154.0, False))
        self.assertEqual((self.same["pot"], self.same["pot_counted"], self.same["hand_counted"]), (154.0, 154, True))
        self.assertEqual((self.counted["pot"], self.counted["prizes"]), (152.0, {"win": 91, "place": 38, "show": 23}))
        self.assertEqual((self.final["race_state"], self.final["pot"], self.final["pot_counted"], self.final["hand_counted"]),
                         (2, 154.0, 152, False))
        self.assertEqual((self.results["race_state"], self.results["hand_counted"], self.cleared["hand_counted"]), (5, True, False))

    def test_the_tag_is_up_with_a_count_and_down_without(self) -> None:
        for look in self.LOOKS:
            plain, counted, cleared = run_board([self.plain, self.counted, self.cleared], look=look, styled=True)
            self.assertEqual((plain["counted"]["on"], plain["counted"]["display"], plain["counted"]["hidden"], plain["pot"]),
                             (False, "none", "true", "$154"), look)
            self.assertEqual((counted["counted"]["on"], counted["counted"]["text"], counted["counted"]["display"],
                              counted["counted"]["hidden"]), (True, "HAND COUNTED", "block", "false"), look)
            self.assertEqual((counted["pot"], counted["prizes"]), ("$152", self.COUNTED), look)
            self.assertEqual((cleared["counted"]["on"], cleared["pot"], cleared["prizes"]), (False, "$154", self.SCALE), look)

    def test_nothing_in_the_header_moves(self) -> None:
        """The same figure with and without the tag ($154, counted as $154):
        the POT figure, its label, the three prize tiles, the banner and the
        logo stay within 2 px (in fact they do not move at all)."""
        for look in self.LOOKS:
            without, tagged = run_board([self.plain, self.same], look=look, styled=True)
            self.assertEqual((without["counted"]["on"], tagged["counted"]["on"]), (False, True), look)
            self.assertEqual((without["pot"], tagged["pot"]), ("$154", "$154"), look)
            for part in ("pot", "figure", "label", "win", "place", "show", "banner", "logo"):
                for a, b in zip(without["header"][part], tagged["header"][part]):
                    self.assertLessEqual(abs(a - b), 2, f"{look}: the {part} moved from {without['header'][part]} to {tagged['header'][part]}")

    def test_the_tag_hangs_left_of_the_figure_at_its_middle_clear_of_the_logo(self) -> None:
        for look in self.LOOKS:
            [seen] = run_board([self.counted], look=look, styled=True)
            left, top, width, height = seen["counted"]["rect"]
            fx, fy, fw, fh = seen["header"]["figure"]
            logo = seen["header"]["logo"]
            self.assertAlmostEqual(left + width, fx - 22, delta=1, msg=f"{look}: a 22 px gap to the figure")
            self.assertAlmostEqual(top + height / 2, fy + fh / 2, delta=1.5, msg=f"{look}: at the figure's middle")
            self.assertGreaterEqual(left, logo[0] + logo[2] + 24, f"{look}: clear of the logo")
            self.assertLess(width, 300, f"{look}: a small tag")
            self.assertLess(height, 50, look)

    def test_the_tag_is_lettered_in_impact_in_every_look(self) -> None:
        for look in self.LOOKS:
            [seen] = run_board([self.counted], look=look, styled=True)
            tag = seen["counted"]
            self.assertEqual((tag["on"], tag["text"]), (True, "HAND COUNTED"), look)
            # the board's own stack, not a face of its own: Impact, Anton behind it
            self.assertEqual(tag["font"], tag["boardFont"], f"{look}: the tag takes the face the board declares")
            self.assertIn("Impact", tag["font"], look)
            self.assertNotIn("DDM Tote", tag["font"], f"{look}: no dot face")
            self.assertEqual(tag["background"], "none", f"{look}: no tile behind the letters")
            # and the glyphs really are that stack's: the text's width is the one set in Impact, or in Anton where
            # Impact is not installed, never the browser's plain sans-serif
            faces = tag["faces"]
            nearest = min(abs(tag["textWidth"] - faces["impact"]), abs(tag["textWidth"] - faces["anton"]))
            self.assertLess(nearest, 0.6, f"{look}: {tag['textWidth']} px against {faces}")
            self.assertGreater(abs(tag["textWidth"] - faces["generic"]), 5, f"{look}: not the fallback face")
            # 26 px letters: capitals about the height of the dot tag's (21 px), and the tag no wider than it was
            self.assertEqual(tag["size"], "26px", look)
            self.assertLessEqual(abs(tag["cap"] - self.OLD_CAP_HEIGHT), 2, f"{look}: capitals {tag['cap']} px")
            if look == "impact":
                self.assertEqual((tag["color"], tag["border"]), (self.GOLD, "3px"), "the impact look's gold pill, as it was")
                self.assertLessEqual(tag["rect"][2], self.OLD_PILL_WIDTH + 0.5, look)
            else:
                self.assertEqual((tag["color"], tag["border"]), (self.AMBER, "0px"), f"{look}: the figure's amber, no pill")
                self.assertLessEqual(tag["rect"][2], self.OLD_DOT_WIDTH, f"{look}: no wider than the dot tag was")

    def test_at_1680_wide_in_dots(self) -> None:
        """The screen DevPi's report fits: the header's middle cell is 656 px
        there. Nothing moves with the tag, and it stays clear of the logo."""
        without, tagged = run_board([self.plain, self.same], look="dots", styled=True, stage=(1680, 1050))
        self.assertEqual((without["counted"]["on"], tagged["counted"]["on"], tagged["counted"]["text"]), (False, True, "HAND COUNTED"))
        for part in ("pot", "figure", "label", "win", "place", "show", "banner", "logo"):
            for a, b in zip(without["header"][part], tagged["header"][part]):
                self.assertLessEqual(abs(a - b), 2, f"1680: the {part} moved from {without['header'][part]} to {tagged['header'][part]}")
        left, top, width, height = tagged["counted"]["rect"]
        logo = tagged["header"]["logo"]
        self.assertAlmostEqual(left + width, tagged["header"]["figure"][0] - 22, delta=1, msg="the 22 px gap to the figure")
        self.assertGreaterEqual(left, logo[0] + logo[2] + 24, "clear of the logo")
        self.assertLessEqual(width, self.OLD_DOT_WIDTH, "no wider than the dot tag was")

    def test_the_tag_is_measured_after_its_face_has_loaded(self) -> None:
        """Where Impact is not installed the tag is lettered in Anton, a web
        font: until it arrives the text is laid out in the browser's plain
        sans-serif, which is wider. Here the room beside the figure (a wide
        logo) is between the two widths, so a measure on the fallback would
        pick COUNTED; with the face slow to arrive the tag must still end up
        HAND COUNTED, decided with Anton's real width."""
        anton = '.qb-counted { font-family: "Anton", sans-serif !important; }'
        [probe] = run_board([self.counted], look="dots", styled=True, css=anton)
        faces = probe["counted"]["faces"]
        self.assertGreater(faces["generic"] - faces["anton"], 40, str(faces))
        self.assertEqual(probe["counted"]["text"], "HAND COUNTED")
        room = (faces["anton"] + faces["generic"]) / 2           # more than Anton needs, less than the fallback does
        logo_w = round(probe["header"]["figure"][0] - 22 - 24 - probe["header"]["logo"][0] - room)
        css = anton + " .qb-logo { width: %dpx !important; }" % logo_w
        [tight] = run_board([self.counted], look="dots", styled=True, css=css)
        self.assertEqual(tight["counted"]["text"], "HAND COUNTED", "Anton fits the room")
        early, slow = run_board([self.counted, {"__wait": 3500}], look="dots", styled=True, css=css, slow={"Anton-Regular.ttf": 2.0})
        self.assertGreater(early["counted"]["faces"]["anton"], faces["anton"] + 40, "the first reading is taken before Anton has arrived")
        self.assertEqual((early["counted"]["on"], early["counted"]["display"]), (False, "none"),
                         "the tag stays down while its face is on the way, instead of being measured in the fallback")
        self.assertEqual((slow["counted"]["on"], slow["counted"]["text"]), (True, "HAND COUNTED"),
                         "decided with the real width once the face had arrived, not with the fallback's")
        self.assertAlmostEqual(slow["counted"]["textWidth"], faces["anton"], delta=0.6)
        [too_tight] = run_board([self.counted], look="dots", styled=True, css=css.replace("%dpx" % logo_w, "%dpx" % (logo_w + 60)))
        self.assertEqual(too_tight["counted"]["text"], "COUNTED", "a room Anton does not fill gives way, face loaded or not")

    def test_the_results_screen_and_the_race_in_progress(self) -> None:
        results, running = run_board([self.results, self.running], look="dots", styled=True)
        self.assertEqual((results["view"], results["counted"]["on"], results["pot"]), ("results", True, "$152"))
        self.assertEqual([results["results"][p]["prize"] for p in ("win", "place", "show")], self.COUNTED)
        self.assertEqual((running["state"], running["counted"]["on"], running["pot"], running["prizes"]),
                         ("4", True, "$152", self.COUNTED))

    def test_held_through_final_call_it_is_not_the_pot(self) -> None:
        [seen] = run_board([self.final], look="dots", styled=True)
        self.assertEqual((seen["banner"], seen["counted"]["on"], seen["pot"]), ("Final call", False, "$154"))

    def test_the_longer_text_gives_way(self) -> None:
        [seen] = run_board([self.counted], look="dots", styled=True)
        self.assertEqual(seen["counted"]["text"], "HAND COUNTED")
        [seen] = run_board([self.counted], look="dots", styled=True, css=".qb-logo { width: 640px !important; }")
        self.assertEqual(seen["counted"]["text"], "COUNTED", "no room for HAND COUNTED between a wide logo and the figure")
        [seen] = run_board([self.counted], look="impact", styled=True, css=".qb-logo { width: 640px !important; }")
        self.assertEqual(seen["counted"]["text"], "COUNTED")

    def test_a_resized_window_takes_the_tag_along(self) -> None:
        for look in ("dots", "impact"):
            before, after = run_board([self.counted, {"__stage": [1800, 1080]}], look=look, styled=True)
            self.assertNotEqual(before["header"]["figure"][0], after["header"]["figure"][0], f"{look}: the figure moved with the window")
            left, _top, width, _height = after["counted"]["rect"]
            self.assertAlmostEqual(left + width, after["header"]["figure"][0] - 22, delta=1, msg=look)

    def test_a_late_face_takes_the_tag_along(self) -> None:
        """The tote face arrives after the first model (a kiosk loading cold):
        the dotted figure changes width when it does, and the tag, which is
        not set in that face, follows its left edge."""
        before, after = run_board([self.counted, {"__wait": 3000}], look="dots", styled=True, slow_face=1.5)
        self.assertEqual((before["pot"], after["pot"], after["counted"]["on"]), ("$152", "$152", True))
        for seen in (before, after):
            left, _top, width, _height = seen["counted"]["rect"]
            self.assertAlmostEqual(left + width, seen["header"]["figure"][0] - 22, delta=1)
        self.assertEqual(after["header"]["figure"][2], 288, "four tiles of 72 px: the face, once it is there")

    def test_a_figure_that_changes_width_takes_its_tag_along(self) -> None:
        fake = fake_pi5.FakePi5(fake_pi5.PHASES["closed"], tokens=fake_pi5.RESULTS_TOKENS,
                                scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                                names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS, names_rev=2)
        fake.set_counted(99)
        short = json.loads(fake.model_json())
        fake.set_counted(1000)
        long_ = json.loads(fake.model_json())
        for look in ("dots", "impact"):
            a, b = run_board([short, long_], look=look, styled=True)
            self.assertEqual((a["pot"], b["pot"]), ("$99", "$1000"), look)
            for seen in (a, b):
                left, _top, width, _height = seen["counted"]["rect"]
                self.assertAlmostEqual(left + width, seen["header"]["figure"][0] - 22, delta=1, msg=look)
            self.assertGreater(a["header"]["figure"][0], b["header"]["figure"][0], f"{look}: the longer figure starts further left")


@unittest.skipUnless(CHROME, "no Chrome or Chromium to run the board's script in")
class DotsRowTests(unittest.TestCase):
    """The tote look's rows in headless Chrome, with the stylesheet and the
    face, at 1920x1080: one strip of tiles per row that fills the room from
    the cloth to the row's right padding (the gap after the cloth again),
    every row the same; the bets in the strip's last tiles (a dim 0 for an
    empty cup), one dark tile, the name in the rest; a name longer than that
    scrolls a tile at a time inside its area. The feed is tools/fake_pi5.py's
    --phase strip."""

    # 805 px of room between the cloth and the row's right padding, 42 px
    # tiles at pitch 7: 19 fit, and 20 are each 40.25 px, 95.8 % of 42, not
    # under 94 %, so there are twenty.
    TILES = 20
    TILE = 40.25
    STEP_MS = TILE / 120 * 1000         # a tile at the crawl's 120 px/s

    def strip_fake(self, names: Optional[Dict[int, str]] = None, **tokens) -> "fake_pi5.FakePi5":
        counts = dict(fake_pi5.STRIP_TOKENS)
        counts.update({int(k[1:]): v for k, v in tokens.items()})
        return fake_pi5.FakePi5(fake_pi5.PHASES["open"], tokens=counts, scratched=(), offline=(), events=[],
                                names={**fake_pi5.STRIP_NAMES, **(names or {})}, names_rev=1)

    @staticmethod
    def model(fake) -> dict:
        return json.loads(fake.model_json())

    def expect_scroll(self, s: Dict[str, Any], over: int, why: str) -> None:
        """A strip's scroll: `over` tiles, the start held 2 s, a tile every
        335 ms (40.25 px at the crawl's 120 px/s), the end held 1 s."""
        if over <= 0:
            self.assertEqual((s["scrolls"], s["over"]), (0, 0), why + ": a name that fits stands still")
            return
        self.assertEqual(s["scrolls"], 1, why)
        self.assertEqual(s["over"], over, why + ": until its last character is in the area's last tile")
        self.assertAlmostEqual(s["duration"], 2000 + (over - 1) * self.STEP_MS + 1000, places=3, msg=why)
        self.assertEqual([k for _, k in s["steps"]], list(range(1, over + 1)), why + ": a whole tile a step")
        for ms, k in s["steps"]:
            self.assertLessEqual(abs(ms - (2000 + (k - 1) * self.STEP_MS)), 1, why + f": step {k} at {ms} ms")

    def expect_filled(self, strips: Dict[str, Any], tiles: int, tile: float, why: str) -> None:
        """Every row: `tiles` tiles of `tile` px, from 22 px after the cloth
        to the row's right padding, which is those 22 px again."""
        for horse, s in strips.items():
            here = f"{why}, row {horse}"
            self.assertEqual((s["tiles"], s["tile"]), (tiles, tile), here + ": every row the same tiles")
            self.assertEqual((s["gap"], s["padRight"]), (22.0, 22.0), here + ": the right padding is the gap after the cloth")
            self.assertLessEqual(abs(s["leftover"]), 2, here + ": the last tile ends at the row's right padding")

    def test_every_row_is_one_strip_of_the_same_tiles(self) -> None:
        [seen] = run_board([self.model(self.strip_fake())], look="dots", styled=True)
        self.assertEqual((seen["look"], seen["visible"]), ("dots", True))
        strips = seen["strips"]
        self.assertEqual(len(strips), 20, "both columns")
        self.expect_filled(strips, self.TILES, self.TILE, "1920 x 1080")
        for horse, s in strips.items():
            count = fake_pi5.STRIP_TOKENS[int(horse)]
            digits = len(str(count))
            why = f"row {horse} ({count} bets)"
            self.assertEqual((s["display"], s["columns"], s["font"]), ("block", 2, 56.0), why + ": pitch 7, the names' largest")
            self.assertEqual((s["bets"], s["betsRight"]), (digits, 0), why + ": the bets in the strip's last tiles, one a digit")
            self.assertEqual(s["area"], self.TILES - digits - 1, why + ": the name's area, one dark tile before the bets")
            self.assertEqual(s["clip"], "hidden", why + ": the area clips the name at its edges")
            self.assertEqual(seen["rows"][horse], str(count), why + ": a 0, not NO BETS")
            self.assertEqual(s["opacity"], "0.5" if count == 0 else "1", why + ": the 0 dim, any count full amber")
        self.assertEqual(strips["3"]["bets"], 1, "a dim 0 takes one tile")
        self.assertEqual(strips["4"]["area"], 16, "104: three tiles of bets, sixteen of name")

    def test_the_tiles_fill_the_row_on_other_screens(self) -> None:
        # EMERGING MARKET (15) with a one-digit count. At 1680 x 1050, the
        # screen DevPi's report fits, 3d5e844 laid 16 tiles, left 35 px empty
        # before the border and scrolled it a tile; now 685 px of room take
        # 17 tiles of 40.29 px (16 fit at 42, 17 are 95.9 % of it) and its
        # 15 fit. At 1330 x 1080 the 510 px take 12 tiles, 13 would be
        # 39.23 px (93.4 %), so the twelve are stretched to 42.5 px.
        model = self.model(self.strip_fake(h5=7))
        for (w, h), (tiles, tile, scrolls) in {(1920, 1080): (20, 40.25, 0), (1680, 1050): (17, 40.29, 0),
                                               (1330, 1080): (12, 42.5, 1)}.items():
            [seen] = run_board([model], look="dots", styled=True, stage=(w, h))
            why = f"{w} x {h}"
            self.expect_filled(seen["strips"], tiles, tile, why)
            s = seen["strips"]["5"]
            self.assertEqual((s["font"], s["area"], s["need"], s["scrolls"]), (56.0, tiles - 2, 15, scrolls),
                             why + ": EMERGING MARKET, 7 bets")

    def test_a_resize_measures_the_strips_again(self) -> None:
        # GRAND MO THE FIRST (18, 7 bets): 18 tiles of name at 1920, 15 at 1680.
        seen = run_board([self.model(self.strip_fake()), {"__stage": [1680, 1050]}, {"__stage": [1920, 1080]}],
                         look="dots", styled=True)
        for s, (tiles, tile) in zip(seen, [(20, 40.25), (17, 40.29), (20, 40.25)]):
            self.expect_filled(s["strips"], tiles, tile, f"{tiles} tiles")
        self.assertEqual([s["strips"]["2"]["over"] for s in seen], [0, 3, 0], "GRAND MO THE FIRST scrolls on the narrower screen only")

    def test_a_name_longer_than_its_area_scrolls_a_tile_at_a_time(self) -> None:
        [seen] = run_board([self.model(self.strip_fake())], look="dots", styled=True)
        strips = seen["strips"]
        scrolling = []
        for horse, s in strips.items():
            name = fake_pi5.STRIP_NAMES[int(horse)]
            self.assertEqual(s["need"], len(name), f"{name}: one tile a character")
            over = len(name) - s["area"]
            self.expect_scroll(s, over, f"{name} in {s['area']} tiles")
            if over > 0:
                scrolling.append(int(horse))
        self.assertEqual(sorted(scrolling), [6, 11, 20], "the eighteen-letter names with two digits of bets")
        self.assertEqual((strips["6"]["over"], strips["2"]["scrolls"], strips["4"]["scrolls"], strips["5"]["scrolls"]),
                         (1, 0, 0, 0), "WHISKEY IN THE JAR: 18 in 17; GRAND MO THE FIRST: 18 fits in 18; "
                                       "CATCHING FREEDOM: 16 fits in 16 at 104; EMERGING MARKET: 15 fits in 17")

    def test_nine_to_ten_takes_a_tile_from_the_name(self) -> None:
        # GRAND MO THE FIRST (18) fits its 18 tiles at 9 and is one too long
        # for 17 at 10; back at 9 it stands still again. A longer name on 8,
        # BLUEGRASS THUNDERBOLT (21), scrolls either way: 3 tiles, then 4.
        fake = self.strip_fake(names={8: "Bluegrass Thunderbolt"}, h2=9, h8=9)
        at9 = self.model(fake)
        fake.bump(2)
        fake.bump(8)
        at10 = self.model(fake)
        fake.bump(2, -1)
        fake.bump(8, -1)
        back = self.model(fake)
        seen9, seen10, seen_back = run_board([at9, at10, back], look="dots", styled=True)
        self.assertEqual((seen9["rows"]["2"], seen10["rows"]["2"], seen_back["rows"]["2"]), ("9", "10", "9"))
        self.assertEqual((seen9["strips"]["2"]["area"], seen10["strips"]["2"]["area"]), (18, 17), "10 takes a tile from the name")
        self.assertEqual(seen10["strips"]["2"]["bets"], 2)
        self.expect_scroll(seen9["strips"]["2"], 0, "GRAND MO THE FIRST at 9")
        self.expect_scroll(seen10["strips"]["2"], 1, "GRAND MO THE FIRST at 10: newly too long, it starts")
        self.expect_scroll(seen_back["strips"]["2"], 0, "GRAND MO THE FIRST back at 9: it fits again and stops")
        self.expect_scroll(seen9["strips"]["8"], 3, "BLUEGRASS THUNDERBOLT at 9")
        self.expect_scroll(seen10["strips"]["8"], 4, "BLUEGRASS THUNDERBOLT at 10: measured again, four tiles now")
        self.expect_scroll(seen_back["strips"]["8"], 3, "BLUEGRASS THUNDERBOLT back at 9")
        self.assertEqual(set(seen10["pulses"]), {2, 8}, "the count ticks and the row pulses, in dots")
        self.assertIn(seen10["toast"], ("2 GRAND MO THE FIRST +1", "8 BLUEGRASS THUNDERBOLT +1"), "a toast still fires")

    def test_the_scrolls_run_only_while_the_rows_are_on_screen(self) -> None:
        fake = self.strip_fake()
        models = [self.model(fake)]
        fake.set_phase(fake_pi5.PHASES["winner"])
        fake.set_results(4, 2, 1)
        models.append(self.model(fake))                        # the results screen stands in the rows' place
        fake.set_phase(fake_pi5.PHASES["open"])
        models.append(self.model(fake))                        # betting open again: the rows are back
        fake.set_phase(fake_pi5.PHASES["idle"])
        models.append(self.model(fake))                        # PRE_RACE: the board hands the TV back
        seen = run_board(models, look="dots", styled=True)
        self.assertEqual([s["view"] for s in seen], ["rows", "results", "rows", "rows"])
        self.assertEqual([s["visible"] for s in seen], [True, True, True, False])
        self.assertEqual([s["strips"]["6"]["playState"] for s in seen], ["running", "paused", "running", "paused"],
                         "WHISKEY IN THE JAR scrolls on the rows only")
        self.assertEqual(seen[0]["strips"]["5"]["playState"], None, "a name that fits has no scroll to run")

    def test_impact_and_numbers_keep_their_rows(self) -> None:
        model = self.model(self.strip_fake())
        for look in ("impact", "numbers"):
            [seen] = run_board([model], look=look, styled=True)
            for horse, s in seen["strips"].items():
                why = f"{look}, row {horse}"
                self.assertEqual((s["display"], s["columns"], s["scrolls"]), ("contents", 3, 0), why)
                self.assertEqual(seen["rows"][horse], "No bets" if fake_pi5.STRIP_TOKENS[int(horse)] == 0
                                 else str(fake_pi5.STRIP_TOKENS[int(horse)]), why)


def redesign_model(**kw) -> dict:
    """The 2026 field as tools/fake_pi5.py's redesign feed serves it (5 -> 21,
    9 -> 22, 13 -> 23 drawn in, 20 scratched with no replacement), with its
    race, odds and weather unless told otherwise."""
    fake = fake_pi5.FakePi5(fake_pi5.PHASES["open"], tokens=fake_pi5.REDESIGN_TOKENS,
                            scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                            names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS, names_rev=2, **kw)
    return json.loads(fake.model_json())


@unittest.skipUnless(CHROME, "no Chrome or Chromium to run the board's script in")
class RosterSlideTests(unittest.TestCase):
    """The slideshow's roster slide ("field and odds") in headless Chrome with
    both stylesheets and the face: the TV board's own rows, filled from La
    Quiniela's model by window.ddmQuiniela.fillRoster in the slide's panel."""

    FIELD = [n for n in range(1, 20) if n not in (5, 9, 13)] + [21, 22, 23]

    def test_the_2026_field_with_its_odds_in_two_columns(self) -> None:
        # The board itself in Impact: the roster is the tote look's strip whatever the board's look.
        [seen] = run_board([redesign_model()], look="impact", styled=True, roster=True)
        cols = seen["roster"]
        self.assertEqual([[r["horse"] for r in col] for col in cols], [self.FIELD[:10], self.FIELD[10:]],
                         "the field by number, the replacements under their own numbers, 20 gone, ten a column")
        rows = [r for col in cols for r in col]
        for r in rows:
            why = f"row {r['horse']}"
            odds = fake_pi5.REDESIGN_ODDS.get(r["horse"])
            self.assertEqual(r["name"], fake_pi5.REDESIGN_NAMES[r["horse"]].upper(), why)
            self.assertEqual((r["odds"], r["empty"]), (odds or "—", odds is None), why + ": no odds, a dim dash")
            self.assertEqual(r["bets"], len(odds or "—"), why + ": as many tiles as the odds have characters")
            self.assertEqual(r["area"], r["tiles"] - r["bets"] - 1, why + ": one dark tile before the odds")
            self.assertEqual((r["display"], r["betsRight"]), ("block", 0), why + ": the odds in the strip's last tiles")
            self.assertLessEqual(abs(r["leftover"]), 2, why + ": the strip fills the row")
            self.assertTrue(r["inside"], why + ": inside its column and the panel, nothing clipped")
            self.assertEqual(r["scrolls"], 1 if len(r["name"]) > r["area"] else 0,
                             why + ": a name longer than its area scrolls a tile at a time")
        self.assertEqual(len({(r["tiles"], r["tile"], r["font"]) for r in rows}), 1, "every row the same strip")
        self.assertEqual({rows[0]["odds"], rows[-1]["odds"]}, {"8-1", "—"}, "odds present (1) and absent (23)")

    def test_no_odds_at_all(self) -> None:
        [seen] = run_board([redesign_model(odds=None)], look="dots", styled=True, roster=True)
        rows = [r for col in seen["roster"] for r in col]
        self.assertEqual({(r["odds"], r["empty"], r["bets"]) for r in rows}, {("—", True, 1)})
        self.assertEqual(len(rows), 19)


CRAWL_NOW = 1_809_218_520.0          # 2027-05-01 7:42 PM CDT (00:42 UTC on the 2nd), pi5's clock


def crawl_model(post_in: Optional[float] = 74 * 60, weather: bool = True, now: float = CRAWL_NOW) -> dict:
    """The 2026 field as tools/fake_pi5.py's redesign feed serves it (three
    scratches with a replacement, one without) with its race and Dallas'
    weather, on pi5's clock `now`, the post `post_in` seconds on from it
    (None: no post time)."""
    m = redesign_model(post_at=(now + post_in) if post_in is not None else None,
                       weather=dict(fake_pi5.DEFAULT_WEATHER) if weather else None)
    m["now"] = now
    return m


def run_boards(jobs: Dict[str, Tuple[List[dict], Dict[str, Any]]]) -> Dict[str, Any]:
    """Several run_board calls at once, each its own Chrome: name -> its
    readings, or the exception it raised (the test that wanted it raises it)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(job: Tuple[List[dict], Dict[str, Any]]) -> Any:
        try:
            return run_board(job[0], **job[1])
        except Exception as err:                # noqa: BLE001 - handed to the test that asked
            return err

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(one, job) for name, job in jobs.items()}
        return {name: future.result() for name, future in futures.items()}


def is_scratch_item(item: str) -> bool:
    """A scratch among the crawl's items: the same-day ones under SCRATCHED,
    or in dots a replacement's own line (9 THE PUMA SCRATCHED - 22 OCELLI
    DRAWS IN)."""
    return item.upper().startswith("SCRATCHED") or item.upper().endswith(" DRAWS IN")


def crawl_window(loop: str, offset: int, tiles: int) -> str:
    """What the tiles read with the loop's cell `offset` on the first one
    (blank before the message starts)."""
    return "".join(" " if offset + i < 0 else loop[(offset + i) % len(loop)] for i in range(tiles))


def tote_chars() -> set:
    """Every character DDMTote.ttf draws."""
    data = (HERE / "static" / "fonts" / "DDMTote.ttf").read_bytes()
    return {chr(code) for code in _cmap(data, _sfnt(data)["tables"])}


@unittest.skipUnless(CHROME, "no Chrome or Chromium to run the board's script in")
class CrawlLiveTests(unittest.TestCase):
    """The crawl's live items, after its first line: the time of day on the
    race's clock, the time to post, the weather; in place as time passes,
    left out when there is nothing to say."""

    NOW = CRAWL_NOW

    def model(self, post_in: Optional[float], weather: bool = True) -> dict:
        return crawl_model(post_in, weather)

    def items(self, seen: dict) -> List[str]:
        return seen["crawl"]["items"]

    def test_time_post_and_weather_between_the_first_line_and_the_scratches(self) -> None:
        [seen] = run_board([self.model(74 * 60)], look="dots", styled=True)
        items = self.items(seen)
        self.assertEqual(items[0], fake_pi5.CHYRON_LINES[0])
        self.assertEqual(items[1:4], ["time:7:42 PM", "post:Post in 1:14", "weather:Dallas 88°F Sunny"])
        self.assertTrue(is_scratch_item(items[4]), items[4])
        self.assertEqual(items[-1], fake_pi5.CHYRON_LINES[-1])

    def test_under_ten_minutes_the_seconds_tick_in_place(self) -> None:
        first, later = run_board([self.model(582), {"__wait": 2000}], look="dots", styled=True)
        self.assertEqual(self.items(first)[2], "post:Post in 9:42")
        post = self.items(later)[2]
        self.assertRegex(post, r"^post:Post in 9:[34]\d$")
        self.assertLess(post, "post:Post in 9:42", "it counted down")
        before, after = first["crawl"]["snapshot"], later["crawl"]["snapshot"]
        self.assertEqual(after["generation"], before["generation"],
                         "the same message: changed in place, the crawl not rebuilt")
        self.assertGreater(after["offset"], before["offset"], "and it went on from where it was")

    def test_after_the_post_and_without_weather_they_are_left_out(self) -> None:
        past, no_post = run_board([self.model(-60, weather=False), self.model(None, weather=False)],
                                  look="dots", styled=True)
        for seen in (past, no_post):
            items = self.items(seen)
            self.assertEqual(items[1], "time:7:42 PM")
            self.assertTrue(is_scratch_item(items[2]), items)
            self.assertFalse(any(i.startswith(("post:", "weather:")) for i in items), items)

    def test_impact_too(self) -> None:
        [seen] = run_board([self.model(74 * 60)], look="impact", styled=True)
        self.assertEqual(self.items(seen)[1:4], ["time:7:42 PM", "post:Post in 1:14", "weather:Dallas 88°F Sunny"])


@unittest.skipUnless(CHROME, "no Chrome or Chromium to run the board's script in")
class StepCrawlTests(unittest.TestCase):
    """The crawl of "dots" in headless Chrome, with the stylesheet and the
    face, at 1920x1080: a row of tiles that never moves, built by the rows'
    fill rule at the crawl's pitch, and the message stepping across it a tile
    at a time, at CRAWL_TILES_PER_SEC or ?crawl_tps=, from animation-frame
    timestamps. The page is read frame by frame ({"__sample": ms}), under a
    virtual clock, so every number here is the page's own, not the machine's."""

    JS = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
    GAP = " ◆ "                          # a blank, the diamond, a blank: the track's 34 px each side, in tiles
    TILES = 78                                # 1852 px of band, 24 px tiles: 77 fit, 78 are each 23.74 px (98.9 %)
    # What a scratch the crawl has never met looks like: a horse with no replacement.
    EXTRA_SCRATCH = {"was": {"number": 14, "name": "POTENTE"}, "now": None}

    @classmethod
    def setUpClass(cls) -> None:
        plain = dict(look="dots", styled=True)
        # The post is 74 and a half minutes on, so "Post in 1:14" stands for the half minute these runs last and
        # only the jobs that mean it see a live item change.
        m = crawl_model(post_in=74 * 60 + 30)
        minute_on = crawl_model(post_in=73 * 60 + 30, now=CRAWL_NOW + 60)   # the same post time, a minute later
        longer = crawl_model(post_in=74 * 60 + 30)
        longer["scratches"] = longer["scratches"] + [cls.EXTRA_SCRATCH]
        longer["names_rev"] += 1
        reordered = crawl_model(post_in=74 * 60 + 30)               # the same scratches, each record's keys the other way round
        reordered["scratches"] = [{"now": s["now"], "was": s["was"]} for s in reordered["scratches"]]
        exotic = crawl_model(post_in=74 * 60 + 30)
        exotic["chyron"] = ["Se\u00f1or  Ocelli \u2603 [x] \u00bd \u00df \u0178 \u00ff \u017e \u0100 \u2019 "
                            "\u2014\u00a0\u00e9\u200b\u00e4 \U0001f642 end", "second"]
        # Spec section 6: only a scratch with no replacement says RE-BET YOUR TOKENS; the replacements say neither.
        replaced_only = crawl_model(post_in=74 * 60 + 30)
        replaced_only["scratches"] = [s for s in replaced_only["scratches"] if s.get("now") is not None]
        undone = crawl_model(post_in=74 * 60 + 30)                  # 9 -> 22 undone on the admin page
        undone["scratches"] = [s for s in undone["scratches"] if s["was"]["number"] != 9]
        undone["names_rev"] += 1
        cls.runs = run_boards({
            "default": ([m, {"__sample": 5000}], plain),
            "slow": ([m, {"__sample": 5000}], dict(plain, query="crawl_tps=2")),
            "fast": ([m, {"__sample": 14000}], dict(plain, query="crawl_tps=60")),
            "bad": ([m], dict(plain, query="crawl_tps=abc")),
            "huge": ([m], dict(plain, query="crawl_tps=500")),
            "tiny": ([m], dict(plain, query="crawl_tps=0.01")),
            "narrow": ([m, {"__stage": [1680, 1050]}], plain),
            # at 20 a second the clock and the countdown are on the tiles when the minute turns
            "inplace": ([m, {"__wait": 3500}, {"__feed": minute_on}], dict(plain, query="crawl_tps=20")),
            "longer": ([m, {"__wait": 1300}, longer, {"__sample": 5500}], dict(plain, query="crawl_tps=60")),
            "undo": ([m, {"__wait": 1300}, undone, {"__sample": 6500}], dict(plain, query="crawl_tps=60")),
            "reordered": ([m, {"__wait": 1300}, {"__feed": reordered}], plain),
            "stall": ([m, {"__stall": 1500}, {"__sample": 3000}], plain),
            "hidden": ([m, dict(m, race_state=0, race_state_name="PRE_RACE"), {"__sample": 2000}, m, {"__sample": 1500}], plain),
            "numbers": ([m], dict(look="numbers", styled=True)),
            "impact": ([m], dict(look="impact", styled=True)),
            "exotic": ([exotic], plain),
            "replaced-only": ([replaced_only], plain),
            "replaced-only-impact": ([replaced_only], dict(look="impact", styled=True)),
        })

    def got(self, name: str) -> List[dict]:
        result = self.runs[name]
        if isinstance(result, Exception):
            raise result
        return result

    def sample(self, name: str) -> dict:
        return self.got(name)[-1]["sample"]

    def test_the_row_is_the_rows_fill_rule_at_the_crawls_pitch(self) -> None:
        # 24 x 32 px tiles (pitch 4, the crawl's size in the tote look all along), as many as the band holds or one
        # more at 94 % of that, each the band's width / N: 1852 px at 1920 x 1080, 1612 at 1680 x 1050.
        wide, narrow = self.got("narrow")
        for reading, (tiles, room) in ((wide, (78, 1852.0)), (narrow, (68, 1612.0))):
            c = reading["crawl"]
            snap = c["snapshot"]
            self.assertEqual((snap["tiles"], c["tiles"], c["room"], c["tileH"]), (tiles, tiles, room, 32.0), f"{room} px")
            self.assertAlmostEqual(snap["tile"] * tiles, room, places=2, msg=f"{room} px: the tiles fill the band edge to edge")
            self.assertAlmostEqual(c["tileW"], snap["tile"], places=1)
            self.assertGreaterEqual(snap["tile"], 0.94 * 24, "one more tile only while each is still 94 % of 24 px")
            self.assertLess(snap["tile"], 25)
        self.assertEqual(wide["crawl"]["snapshot"]["generation"], narrow["crawl"]["snapshot"]["generation"],
                         "a resize measures again: the message goes on")
        self.assertGreater(narrow["crawl"]["snapshot"]["offset"], wide["crawl"]["snapshot"]["offset"])

    def loop_cells(self, name: str) -> Tuple[str, List[str]]:
        """A run's loop as the snapshot reads it, and each cell's look on the tile that showed it (its sample)."""
        first, reading = self.got(name)[0], self.got(name)[-1]
        loop, cells = first["crawl"]["snapshot"]["text"], reading["sample"]["cells"]
        self.assertEqual(len(cells), len(loop), f"{name}: the sample saw every cell of the loop")
        return loop, [cells[str(k)] for k in range(len(loop))]

    def test_on_the_tiles_only_scratched_is_red_and_nothing_is_a_chip_a_strike_or_an_arrow(self) -> None:
        # Joey, 2026-10-06: the crawl is one-colour dot-matrix tiles. The whole loop as the tiles showed it, a cell at
        # a time: the red SCRATCHED over the same-day scratches, and every other cell the plain tile in the lit amber:
        # no cloth, no colour of its own, nothing struck or dim, no arrow. A replacement's own SCRATCHED is plain like
        # the rest of its line.
        loop, looks = self.loop_cells("fast")
        self.assertIn("SCRATCHED 20 FULLEFFORT", loop, "the same-day scratch, its number plain, right after SCRATCHED")
        label = loop.index("SCRATCHED 20 FULLEFFORT")
        red = set(range(label, label + len("SCRATCHED")))
        self.assertEqual({looks[k].split("|")[0] for k in red}, {"qb-ct is-lbl"}, "SCRATCHED over the same-day scratch: red")
        self.assertEqual({tuple(looks[k].split("|")[:2]) for k in range(len(loop)) if k not in red}, {("qb-ct", "")},
                         "every other cell the plain tile: no cloth, no colour of its own, nothing struck or dim")
        lit = {looks[k] for k in range(len(loop)) if k not in red and loop[k] != " "}
        self.assertEqual(len(lit), 1, "one look for every character but SCRATCHED's: " + str(lit))
        self.assertEqual(next(iter(lit)).split("|")[2:4], ["rgb(212, 160, 0)", "1"], "the lit amber, opaque")
        self.assertNotIn("\u25b6", loop, "no arrow")

    def test_a_replacement_reads_its_own_line_then_the_same_day_scratches(self) -> None:
        # Joey, 2026-10-06: {old number} {old name} SCRATCHED - {new number} {new name} DRAWS IN, an item of its own,
        # in number order (the model's); then the same-day scratch under SCRATCHED, as it read before, its number plain.
        snap = self.got("default")[0]["crawl"]["snapshot"]
        lines = ["5 RIGHT TO PARTY SCRATCHED - 21 GREAT WHITE DRAWS IN", "9 THE PUMA SCRATCHED - 22 OCELLI DRAWS IN",
                 "13 SILENT TACTIC SCRATCHED - 23 ROBUSTA DRAWS IN"]
        self.assertEqual([x for x in snap["items"] if is_scratch_item(x)],
                         lines + ["Scratched20FULLEFFORT\u00b7 RE-BET YOUR TOKENS"], "the scratches, in this order")
        for line in lines:
            self.assertIn(self.GAP + line + self.GAP, snap["text"], f"{line}: on the tiles, an item between two diamonds")
        self.assertIn(self.GAP + "SCRATCHED 20 FULLEFFORT \u00b7 RE-BET YOUR TOKENS" + self.GAP, snap["text"],
                      "the same-day scratch reads as it did")

    def test_undoing_a_replacement_takes_its_line_away_at_the_next_loop(self) -> None:
        first, waited, offered, sampled = self.got("undo")
        line = "9 THE PUMA SCRATCHED - 22 OCELLI DRAWS IN"
        old = offered["crawl"]["snapshot"]
        self.assertTrue(old["pending"], "the undo is a new message: it waits")
        self.assertIn(line, old["items"], "the old message, the line in it, is still the one on the tiles")
        s, final = sampled["sample"], sampled["crawl"]["snapshot"]
        steps = s["steps"]
        swap = next(i for i, step in enumerate(steps) if step[1] == 0)
        self.assertEqual(steps[swap - 1][1], old["length"] - 1, "swapped at the loop boundary")
        self.assertEqual((steps[swap][2], steps[swap][3]), (old["generation"] + 1, False))
        self.assertNotIn(line, final["items"])
        for gone in ("THE PUMA", "OCELLI"):
            self.assertNotIn(gone, final["text"], "nothing of it left on the tiles")
        self.assertEqual(final["length"], old["length"] - len(line) - len(self.GAP), "one line and its gap shorter")
        self.assertIn("5 RIGHT TO PARTY SCRATCHED - 21 GREAT WHITE DRAWS IN", final["items"], "the other lines stay")

    def test_the_re_bet_note_is_as_bright_as_the_text_around_it(self) -> None:
        # It tells the bettors what to do, so it is not fine print: in every look its colour, opacity and glow are
        # the horse's name's before it (in dots the lit tile's, amber), while on the track of numbers and impact the
        # struck name of a replaced horse stays dim (dots has none: it says a replacement in words).
        for run in ("numbers", "impact"):
            n = self.got(run)[-1]["crawl"]["note"]
            self.assertIsNotNone(n, f"{run}: the track carries the note")
            self.assertEqual(n["text"], "\u00b7 RE-BET YOUR TOKENS", run)
            self.assertEqual(n["note"], n["name"], f"{run}: the note's colour | opacity | glow against the name's")
            self.assertEqual(n["note"].split("|")[1], "1", f"{run}: opaque")
            self.assertNotEqual(n["struck"], n["name"], f"{run}: a replaced horse's struck name stays muted")
        loop, looks = self.loop_cells("fast")
        note = loop.index("\u00b7 RE-BET YOUR TOKENS")
        name = loop.rindex("FULLEFFORT", 0, note)
        lit = {looks[name + i] for i in range(len("FULLEFFORT"))}
        self.assertEqual(len(lit), 1, "dots: the name's tiles, one look")
        self.assertEqual(next(iter(lit)).split("|")[2:4], ["rgb(212, 160, 0)", "1"], "dots: the lit tile, amber and opaque")
        self.assertEqual({looks[note + i] for i, ch in enumerate("\u00b7 RE-BET YOUR TOKENS") if ch != " "}, lit,
                         "dots: every character of the note on a tile lit like the name's")

    def test_the_same_scratches_in_another_key_order_are_no_update(self) -> None:
        # The model a page gets when it loads (the relay's, sorted keys) and pi5's stream (its own order) carry the same
        # record: that is not a new message, which would wait for the loop boundary and, hidden, start it again.
        first, waited, again = (r["crawl"]["snapshot"] for r in self.got("reordered"))
        self.assertEqual((again["generation"], again["pending"]), (first["generation"], False))
        self.assertEqual(again["items"], first["items"])
        self.assertGreater(again["offset"], first["offset"])

    def test_a_stall_is_not_made_up_for(self) -> None:
        # The main thread is away for a second and a half: the next frame is owed eight steps at 5 a second and
        # takes none of them, so the crawl goes on from where it was and never jumps.
        s = self.sample("stall")
        offsets = [s["start"][0]] + [step[1] for step in s["steps"]]
        self.assertTrue(all(b - a == 1 for a, b in zip(offsets, offsets[1:])), "a step is a tile, never a jump: " + str(offsets))
        self.assertLessEqual(len(s["steps"]), 12, "the steps of the stall were not made up for (15 without it)")
        self.assertGreaterEqual(len(s["steps"]), 6, "and the crawl went on")

    def test_the_crawl_is_paused_while_the_board_is_hidden_and_goes_on_where_it_was(self) -> None:
        shown, hidden, asleep, back, going = self.got("hidden")
        self.assertEqual((shown["visible"], hidden["visible"], back["visible"]), (True, False, True))
        self.assertEqual(asleep["sample"]["steps"], [], "no step while nobody sees the board")
        self.assertEqual(asleep["sample"]["drift"], 0)
        snaps = [r["crawl"]["snapshot"] for r in (shown, hidden, asleep, back, going)]
        self.assertEqual(snaps[2]["offset"], snaps[1]["offset"], "where it stood")
        self.assertEqual(len({s["generation"] for s in snaps}), 1, "the same message: hiding and showing are no update")
        self.assertGreaterEqual(len(going["sample"]["steps"]), 6, "it went on (1.5 s at 5 a second)")
        self.assertGreater(snaps[4]["offset"], snaps[0]["offset"])

    def test_a_message_comes_in_from_the_right(self) -> None:
        first = self.got("default")[0]["crawl"]
        self.assertLess(first["snapshot"]["offset"], 0, "it starts from blank tiles")
        self.assertTrue(first["text"].startswith(" " * 60), "the left of the band is blank: " + repr(first["text"]))
        self.assertTrue(first["text"].rstrip(" ").strip(), "and the message is on its way in")

    def test_the_tiles_never_move(self) -> None:
        s = self.sample("default")
        self.assertGreaterEqual(s["frames"], 250, "every frame of five seconds")
        self.assertEqual(s["tiles"], self.TILES)
        self.assertEqual(s["drift"], 0, "no tile was ever anywhere but where it started")
        self.assertGreaterEqual(len(s["steps"]), 23, "and the message stepped all the while (5 a second for five seconds)")

    def test_a_step_moves_the_whole_message_one_tile_left(self) -> None:
        s = self.sample("default")
        before = s["start"][2]
        for _t, offset, _generation, _pending, text in s["steps"]:
            self.assertEqual(text[:-1], before[1:], f"offset {offset}: every character one tile to the left")
            before = text
        self.assertNotEqual(s["start"][2].strip(), "", "the message was coming in")

    def test_the_rate_is_the_constant_and_the_url_overrides_it(self) -> None:
        match = re.search(r"const CRAWL_TILES_PER_SEC = ([\d.]+);", self.JS)
        self.assertTrue(match, "a named constant")
        self.assertEqual(float(match.group(1)), 5.0, "a default of 5 tiles a second")
        # 5 tiles of 23.74 px at 1920 px: the speed of the track the sign replaced (CRAWL_PX_S), which 8 tiles a second,
        # tried first on DevPi, was far too fast for
        track = re.search(r"const CRAWL_PX_S\s+= (\d+);", self.JS)
        self.assertTrue(track and float(track.group(1)) == 120.0)
        tile = self.got("default")[-1]["crawl"]["snapshot"]["tile"]
        self.assertAlmostEqual(5 * tile, 120.0, delta=3.6, msg="the default is the old track's px/s, in tiles")
        for name, tps, seconds in (("default", 5, 5), ("slow", 2, 5), ("fast", 60, 14)):
            s = self.sample(name)
            self.assertEqual(self.got(name)[-1]["crawl"]["snapshot"]["tps"], tps, name)
            self.assertLessEqual(abs(len(s["steps"]) - tps * seconds), 1, f"{name}: {tps} tiles a second for {seconds} s")
        for name in ("default", "slow"):
            gaps = [b[0] - a[0] for a, b in zip(self.sample(name)["steps"], self.sample(name)["steps"][1:])]
            tps = self.got(name)[-1]["crawl"]["snapshot"]["tps"]
            self.assertTrue(all(abs(g - 1000 / tps) <= 17 for g in gaps), f"{name}: a step every {1000 / tps:.0f} ms, give or take a frame: {gaps}")
        for name, tps in (("bad", 5), ("huge", 60), ("tiny", 0.25)):
            self.assertEqual(self.got(name)[0]["crawl"]["snapshot"]["tps"], tps,
                             f"crawl_tps={name}: not a number is the default, and the rate stays between 0.25 and 60")

    def test_a_loop_wraps_with_the_gap_and_no_tile_sticks_at_the_seam(self) -> None:
        reading = self.got("fast")[-1]
        s, snap = reading["sample"], reading["crawl"]["snapshot"]
        loop, tiles = snap["text"], snap["tiles"]
        self.assertEqual(len(loop), snap["length"])
        self.assertTrue(loop.endswith(self.GAP), "the loop ends in the gap: a blank, the diamond, a blank")
        self.assertEqual(loop.count(self.GAP), len(snap["items"]), "one gap after every item, the last one the seam's")
        frames = [(s["start"][0], s["start"][2])] + [(offset, text) for _t, offset, _g, _p, text in s["steps"]]
        for offset, text in frames:
            self.assertEqual(text, crawl_window(loop, offset, tiles), f"offset {offset}: the loop from that cell on, wrapped")
        wraps = 0
        for (offset, _), (after, _) in zip(frames, frames[1:]):
            if after == 0 and offset > 0:
                wraps += 1
                self.assertEqual(offset, snap["length"] - 1, "the loop wraps from its last cell to its first")
            else:
                self.assertEqual(after, offset + 1, "a step is one cell")
        self.assertGreaterEqual(wraps, 2, "the sample went round the seam twice")
        self.assertGreater(len(frames), 700)

    def test_a_live_item_that_keeps_its_length_is_written_where_it_stands(self) -> None:
        first, waited, later = (r["crawl"] for r in self.got("inplace"))
        a, b, c = first["snapshot"], waited["snapshot"], later["snapshot"]
        self.assertEqual(a["items"][1:3], ["time:7:42 PM", "post:Post in 1:14"])
        self.assertEqual(c["items"][1:3], ["time:7:43 PM", "post:Post in 1:13"], "a minute on, the same lengths")
        self.assertEqual((b["generation"], c["generation"], c["pending"]), (a["generation"], a["generation"], False),
                         "the same message: written in place, not rebuilt, and nothing waits for the loop boundary")
        self.assertEqual(c["length"], a["length"])
        self.assertGreater(c["offset"], a["offset"], "and the crawl went on: nothing started it again")
        self.assertIn("7:42 PM", waited["text"], "on the tiles")
        self.assertIn("7:43 PM", later["text"], "on the tiles at once: read in the turn the model came in")
        self.assertIn("POST IN 1:13", later["text"])
        self.assertNotIn("7:42 PM", later["text"])

    def test_a_longer_message_waits_for_the_loop_boundary_and_nothing_restarts_the_crawl(self) -> None:
        first, waited, offered, sampled = self.got("longer")
        before, mid, old = first["crawl"]["snapshot"], waited["crawl"]["snapshot"], offered["crawl"]["snapshot"]
        self.assertTrue(old["pending"], "a scratch is a new message: it waits")
        self.assertEqual((old["items"], old["length"], old["generation"]), (mid["items"], mid["length"], before["generation"]),
                         "the old message is still the one on the tiles")
        self.assertGreater(mid["offset"], 0, "the crawl is mid-loop")
        self.assertGreater(old["offset"], mid["offset"], "and going on: the offer did not start it again")
        s, final = sampled["sample"], sampled["crawl"]["snapshot"]
        steps = s["steps"]
        swap = next(i for i, step in enumerate(steps) if step[1] == 0)
        self.assertEqual(steps[swap - 1][1], old["length"] - 1, "at the boundary: the old message's last cell, then its first")
        for t, offset, generation, pending, text in steps[:swap]:
            self.assertEqual((generation, pending), (old["generation"], True), f"offset {offset}: waiting")
        for t, offset, generation, pending, text in steps[swap:]:
            self.assertEqual((generation, pending), (old["generation"] + 1, False), f"offset {offset}: swapped in")
        offsets = [steps[0][1]] + [step[1] for step in steps[1:swap]]
        self.assertEqual(offsets, list(range(offsets[0], offsets[0] + len(offsets))), "one cell a step right up to the boundary")
        self.assertEqual([step[1] for step in steps[swap:swap + 5]], [0, 1, 2, 3, 4], "and on from the start of the new message")
        self.assertIn("POTENTE", "".join(final["items"]).upper(), "the scratch is in the new message")
        self.assertGreater(final["length"], old["length"])
        for t, offset, generation, pending, text in steps[swap:swap + 20]:
            self.assertEqual(text, crawl_window(final["text"], offset, final["tiles"]), "the tiles read the new message")

    def test_impact_and_numbers_keep_the_track_and_say_the_same_but_the_scratches(self) -> None:
        # numbers and impact are as they were: the track, and one SCRATCHED item with the cloths, the struck names and
        # the arrows. The tiles say everything else the same, in the same order: the scratches they say in words, in
        # the track's place (one line per replacement, then the same-day ones under SCRATCHED).
        said = self.got("default")[0]["crawl"]["items"]
        self.assertEqual(said[0], fake_pi5.CHYRON_LINES[0])
        tracks = {}
        for look in ("numbers", "impact"):
            c = self.got(look)[0]["crawl"]
            self.assertIsNone(c["snapshot"], f"{look}: no tiles, nothing to snapshot")
            self.assertEqual((c["tiles"], c["animation"], c["trackDisplay"]), (0, "qbCrawl", "flex"),
                             f"{look}: the track is there and animated, as it was")
            tracks[look] = c["items"]
        self.assertEqual(tracks["numbers"], tracks["impact"], "numbers and impact: the same items")
        track = tracks["impact"]
        k = next(i for i, x in enumerate(track) if x.startswith("Scratched"))
        self.assertEqual(track[k], "Scratched5RIGHT TO PARTY\u25b621GREAT WHITE9THE PUMA\u25b622OCELLI13SILENT TACTIC\u25b623ROBUSTA"
                                   "20FULLEFFORT\u00b7 RE-BET YOUR TOKENS", "the track's scratches as they were")
        self.assertEqual(track[:k] + track[k + 1:], [x for x in said if not is_scratch_item(x)],
                         "everything but the scratches: the same items, in the same order, with the same separators")
        self.assertTrue(all(is_scratch_item(x) for x in said[k:k + 4]), "the tiles' scratches where the track has them")
        dots = self.got("default")[0]["crawl"]
        self.assertEqual((dots["trackDisplay"], dots["animation"]), ("none", "qbCrawl"), "dots: the track is out of the way")

    def test_a_same_day_scratch_says_re_bet_your_tokens_in_every_look(self) -> None:
        # Spec section 6, kind 2: a scratch with no replacement (20 FULLEFFORT in the redesign feed) is handed back
        # to be re-bet; nothing is refunded. Every look says so after that horse, once, and REFUND nowhere; on the
        # dots tiles every character of it is its own (none a blank or a base letter).
        for name in ("default", "numbers", "impact"):
            items = self.got(name)[0]["crawl"]["items"]
            scratch = next(i for i in items if i.upper().startswith("SCRATCHED"))
            self.assertEqual(scratch.count('\u00b7 RE-BET YOUR TOKENS'), 1, f'{name}: {scratch!r}')
            self.assertLess(scratch.index('FULLEFFORT'), scratch.index('\u00b7 RE-BET YOUR TOKENS'), name)
            self.assertNotIn("REFUND", " ".join(items).upper(), name)
        loop = self.got("default")[0]["crawl"]["snapshot"]["text"]
        self.assertIn('FULLEFFORT \u00b7 RE-BET YOUR TOKENS', loop, 'dots: on the tiles, each character its own')
        self.assertNotIn("REFUND", loop)
        self.assertNotIn("refund", json.dumps(redesign_model()).lower(), "the served model says nothing of a refund")

    def test_a_replacement_scratch_says_neither(self) -> None:
        # 9 -> 22 and the like: settled before betting opens, nothing to re-bet, so neither phrase, on the tiles or on
        # the track. With only replacements the tiles have their lines and no SCRATCHED item at all; the track has its
        # one SCRATCHED item, as it was.
        dots = self.got("replaced-only")[0]["crawl"]["snapshot"]
        self.assertEqual([x for x in dots["items"] if is_scratch_item(x)],
                         ["5 RIGHT TO PARTY SCRATCHED - 21 GREAT WHITE DRAWS IN", "9 THE PUMA SCRATCHED - 22 OCELLI DRAWS IN",
                          "13 SILENT TACTIC SCRATCHED - 23 ROBUSTA DRAWS IN"], "dots: the lines, no SCRATCHED item")
        for said in (dots["text"], " ".join(dots["items"]).upper()):
            self.assertNotIn("RE-BET", said)
            self.assertNotIn("REFUND", said)
            self.assertNotIn("\u25b6", said)
        items = self.got("replaced-only-impact")[0]["crawl"]["items"]
        scratch = next(i for i in items if i.upper().startswith("SCRATCHED"))
        self.assertIn('\u25b6', scratch, 'impact: the replacements are there, as they were')
        self.assertNotIn("RE-BET", scratch.upper())
        self.assertNotIn("REFUND", " ".join(items).upper())

    def test_a_character_the_face_lacks_is_its_base_letter_or_a_blank(self) -> None:
        # "Se\u00f1or  Ocelli \u2603 [x] \u00bd \u00df \u0178 \u00ff \u017e \u0100 ...": capitals; the face's own
        # accented letters, quotes and dashes as they are (it draws them as the plain ones); a letter it has
        # no accented form of, its base letter; a snowman, brackets, a half, an eszett and an emoji: a blank
        # each; runs of blanks one blank; a zero-width space nothing at all.
        loop = self.got("exotic")[0]["crawl"]["snapshot"]["text"]
        expected = "SE\u00d1OR OCELLI X Y \u00ff Z A \u2019 \u2014 \u00c9\u00c4 END"
        self.assertTrue(loop.startswith(expected + self.GAP), repr(loop[:80]))
        faces = tote_chars()
        for name in ("default", "exotic"):
            text = self.got(name)[0]["crawl"]["snapshot"]["text"]
            self.assertLessEqual(set(text) - {" "}, faces, f"{name}: every character on the tiles is one DDMTote.ttf draws")


class StepCrawlSourceTests(unittest.TestCase):
    """What the crawl of "dots" is made of, read off the source: the rate is a
    named constant at the top of the script, the steps come from animation
    frames and not from a timer, and nothing in the stylesheet moves the row
    or a tile."""

    JS = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
    CSS = (HERE / "static" / "css" / "quiniela_board.css").read_text(encoding="utf-8")

    def test_the_board_says_refund_nowhere(self) -> None:
        # A same-day scratch's tokens are handed back to be re-bet (spec section 6): no word of a refund in the
        # board's script, stylesheet or page, and the crawl's note is the new wording.
        page = (HERE / "templates" / "splash" / "quiniela_live.html").read_text(encoding="utf-8")
        for name, text in (("script", self.JS), ("stylesheet", self.CSS), ("page", page)):
            self.assertNotIn("refund", text.lower(), name)
        self.assertEqual(self.JS.count("'\u00b7 RE-BET YOUR TOKENS'"), 2, "the track and the tiles")

    def test_the_rate_is_a_constant_at_the_top_and_the_url_can_change_it(self) -> None:
        self.assertLess(self.JS.index("const CRAWL_TILES_PER_SEC"), self.JS.index("function makeRow"))
        self.assertIn("get('crawl_tps')", self.JS)
        self.assertIn("CRAWL_TPS_MIN", self.JS)
        self.assertIn("CRAWL_TPS_MAX", self.JS)

    def test_the_steps_come_from_animation_frames_not_a_timer(self) -> None:
        self.assertIn("requestAnimationFrame(crawlFrame)", self.JS)
        self.assertIn("function crawlFrame(now)", self.JS)
        timers = re.findall(r"setInterval\((\w+)", self.JS)
        self.assertEqual(sorted(timers), ["tickCloses", "tickLive", "updateNoLink"], "no timer steps the crawl: " + str(timers))
        frame = self.JS[self.JS.index("function crawlFrame(now)"):self.JS.index("function crawlStart()")]
        self.assertNotIn("setTimeout", frame)
        self.assertIn("(now - crawlClock.t0) / stepMs", frame, "step n is due at n / rate after the crawl started: a late frame does not drift it")

    def test_nothing_in_the_tiles_rules_moves(self) -> None:
        bare = re.sub(r"/\*.*?\*/", "", self.CSS, flags=re.S)
        rules = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", bare)
                 if ".qb-crawl-tiles" in sel or ".qb-ct" in sel]
        self.assertGreaterEqual(len(rules), 3, [r[0] for r in rules])      # the row, a tile, SCRATCHED's red
        for selector, body in rules:
            self.assertIn('[data-look="dots"]', selector, "dots only")
            for prop in ("transform", "animation", "transition", "translate", "will-change"):
                self.assertNotIn(prop, body, f"{selector}: the tiles stand still ({prop})")
        self.assertIn('.qb[data-look="dots"] .qb-crawl-track { display: none; }', self.CSS, "dots has no track")

    def test_every_look_the_script_gives_a_cell_has_a_rule(self) -> None:
        # The tiles have two looks: the plain lit one and SCRATCHED's red. A scratch is said in words, so no cloth, dim
        # or struck tile is left, in the script or the stylesheet.
        bare = re.sub(r"/\*.*?\*/", "", self.CSS, flags=re.S)
        self.assertIn(".qb-ct.is-lbl", bare, "the stylesheet has no rule for is-lbl")
        self.assertIn("'qb-ct is-lbl'", self.JS)
        for cls in ("is-dim", "is-strike", "is-cloth", "is-cs", "is-cl", "is-cm", "is-cr"):
            self.assertNotIn(".qb-ct." + cls, bare, f"{cls}: no tile is drawn that way any more")
            self.assertNotIn(cls, self.JS, f"{cls}: no tile is drawn that way any more")

    def test_the_other_looks_rules_for_the_track_are_not_touched(self) -> None:
        for needle in ("animation: qbCrawl 40s linear infinite;", "@keyframes qbCrawl {",
                       ".qb:not(.is-visible) .qb-crawl-track { animation-play-state: paused; }"):
            self.assertIn(needle, self.CSS)
        self.assertIn("animationiteration", self.JS)
        self.assertIn("function applyCrawl(html)", self.JS)
        self.assertIn("function buildCrawl(lines, scratches, live)", self.JS)


def _sfnt(data: bytes) -> Dict[str, Any]:
    """The table directory of a TrueType file: tag -> (checksum, offset, length)."""
    version, count = struct.unpack(">IH", data[:6])
    tables = {}
    for i in range(count):
        tag, checksum, offset, length = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
        tables[tag.decode("latin-1")] = (checksum, offset, length)
    return {"version": version, "tables": tables}


def _checksum(data: bytes) -> int:
    data += b"\0" * (-len(data) % 4)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF


def _cmap(data: bytes, tables: Dict[str, Any]) -> Dict[int, int]:
    """Code point -> glyph id from the font's format 4 subtable."""
    base = tables["cmap"][1]
    platform, encoding, offset = struct.unpack(">HHI", data[base + 4:base + 12])
    assert (platform, encoding) == (3, 1)
    s = base + offset
    fmt, _length, _lang, segx2 = struct.unpack(">HHHH", data[s:s + 8])
    assert fmt == 4
    n = segx2 // 2
    ends = struct.unpack(">%dH" % n, data[s + 14:s + 14 + 2 * n])
    starts = struct.unpack(">%dH" % n, data[s + 16 + 2 * n:s + 16 + 4 * n])
    deltas = struct.unpack(">%dH" % n, data[s + 16 + 4 * n:s + 16 + 6 * n])
    offsets = struct.unpack(">%dH" % n, data[s + 16 + 6 * n:s + 16 + 8 * n])
    assert not any(offsets), "every segment maps by delta"
    assert list(ends) == sorted(ends) and ends[-1] == 0xFFFF
    out = {}
    for a, b, d in zip(starts, ends, deltas):
        if a == 0xFFFF:
            continue
        for code in range(a, b + 1):
            out[code] = (code + d) & 0xFFFF
    return out


class ToteFontTests(unittest.TestCase):
    """static/fonts/DDMTote.ttf is the dashboard's 5x7 table as a face."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.path = HERE / "static" / "fonts" / "DDMTote.ttf"
        cls.data = cls.path.read_bytes()
        cls.font = _sfnt(cls.data)
        cls.tables = cls.font["tables"]
        cls.cmap = _cmap(cls.data, cls.tables)

    def table(self, tag: str) -> bytes:
        _, offset, length = self.tables[tag]
        return self.data[offset:offset + length]

    def contours(self, glyph: int) -> int:
        loca = self.table("loca")
        a, b = struct.unpack(">II", loca[4 * glyph:4 * glyph + 8])
        if a == b:
            return 0
        start = self.tables["glyf"][1] + a
        return struct.unpack(">h", self.data[start:start + 2])[0]

    @unittest.skipUnless(make_tote_font.GLYPH_SOURCE.exists(), "the dashboard's table (pi5/) is not in this checkout")
    def test_the_face_on_disk_is_what_the_dashboards_table_says(self) -> None:
        self.assertEqual(make_tote_font.build(), self.data,
                         "dotPatterns changed: run python tools/make_tote_font.py and commit the face")
        self.assertEqual(make_tote_font.build(), make_tote_font.build(), "the same bytes on every run")

    @unittest.skipUnless(make_tote_font.GLYPH_SOURCE.exists(), "the dashboard's table (pi5/) is not in this checkout")
    def test_every_pattern_is_a_glyph_of_as_many_dots(self) -> None:
        patterns = make_tote_font.read_patterns()
        self.assertGreaterEqual(len(patterns), 64)
        for ch, rows in patterns.items():
            self.assertEqual(len(rows), 7, ch)
            self.assertIn(ord(ch), self.cmap, ch)
            self.assertEqual(self.contours(self.cmap[ord(ch)]), sum(bin(r).count("1") for r in rows), ch)
        self.assertEqual(self.contours(self.cmap[ord(" ")]), 0)
        self.assertEqual(self.contours(self.cmap[make_tote_font.SOCKET]), 35, "the socket glyph: every bulb")
        self.assertEqual(self.contours(0), 0, ".notdef is blank")
        # the slashed zero is the dashboard's, not the letter O
        self.assertNotEqual(patterns["0"], patterns["O"])

    def test_what_the_board_prints_is_covered(self) -> None:
        text = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 $.,'-&!/:+#%()?"
                "\u00b7\u25c6\u25b6"                       # the crawl's middle dot, diamond and arrow
                "abcxyz\u00d1\u00e9\u00dc\u2019\u2013")       # lower case, accents, a curly apostrophe, a dash
        for ch in text:
            self.assertIn(ord(ch), self.cmap, repr(ch))
        for low, up in (("a", "A"), ("z", "Z"), ("\u00f1", "N"), ("\u00c9", "E"), ("\u2019", "'"), ("\u2013", "-")):
            self.assertEqual(self.cmap[ord(low)], self.cmap[ord(up)], f"{low!r} is drawn as {up!r}")
        for line in fake_pi5.CHYRON_LINES + ["SCRATCHED", "\u00b7 RE-BET YOUR TOKENS", "NO BETS", "$1,234"]:
            for ch in line:
                self.assertIn(ord(ch), self.cmap, f"{ch!r} in {line!r}")

    @unittest.skipUnless((HERE.parent / "pi5" / "config.py").exists(), "pi5/ is not in this checkout")
    def test_the_crawls_lines_are_pi5s(self) -> None:
        """The disclaimer lines the crawl carries are pi5's LQ_CHYRON_LINES: pi5/config.py's, which are in
        force, and la_quiniela/betting.py's default, the same. The fake serves those very lines, so the check
        above covers what the TV prints. The first line's separator is a hyphen with a space each side (Joey,
        2026-10-03: a dash, not a dot; the face draws every dash as this one glyph)."""
        def value_of(path: Path, name: str) -> Any:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    return ast.literal_eval(node.value)
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == name:
                            return ast.literal_eval(value)
            raise AssertionError(f"no {name} in {path}")
        pi5 = HERE.parent / "pi5"
        in_force = value_of(pi5 / "config.py", "LQ_CHYRON_LINES")
        self.assertEqual(value_of(pi5 / "la_quiniela" / "betting.py", "LQ_CHYRON_LINES"), in_force,
                         "pi5/config.py and betting.py's default say the same")
        self.assertEqual(fake_pi5.CHYRON_LINES, in_force, "the fake serves pi5's lines")
        self.assertEqual(in_force[0], "TOTALS BASED ON CHEAP CHINESE ELECTRONICS - FINAL RESULTS HAND COUNTED")

    def test_the_crawls_character_list_is_the_faces(self) -> None:
        """quiniela_board.js keeps its own list of what the face draws (the
        crawl's tiles take only those characters): the face's character map,
        less the every-bulb socket, nothing more and nothing less."""
        js = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
        block = re.search(r"const TOTE_CHARS = new Set\(\[(.*?)\]\.join\(''\)\);", js, re.S)
        self.assertTrue(block, "a list of the face's characters in the script")
        literals = re.findall(r"'((?:[^'\\]|\\.)*)'", block.group(1))
        listed = "".join(ast.literal_eval("'" + lit + "'") for lit in literals)
        self.assertEqual(len(listed), len(set(listed)), "no character twice")
        self.assertEqual(set(listed), {chr(code) for code in self.cmap} - {chr(make_tote_font.SOCKET)},
                         "the script's list is the face's character map")

    def test_the_file_is_a_sound_truetype(self) -> None:
        self.assertEqual(self.font["version"], 0x00010000)
        self.assertEqual(sorted(self.tables), sorted(["OS/2", "cmap", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "name", "post"]))
        self.assertEqual(list(self.tables), sorted(self.tables), "the directory is sorted by tag")
        head_at = self.tables["head"][1]
        zeroed = self.data[:head_at + 8] + b"\0\0\0\0" + self.data[head_at + 12:]
        for tag, (checksum, offset, length) in self.tables.items():
            self.assertEqual(offset % 4, 0, tag)
            self.assertEqual(_checksum(zeroed[offset:offset + length]), checksum, tag)
        adjustment = struct.unpack(">I", self.data[head_at + 8:head_at + 12])[0]
        self.assertEqual((_checksum(zeroed) + adjustment) & 0xFFFFFFFF, 0xB1B0AFBA)
        magic, _flags, upem = struct.unpack(">IHH", self.data[head_at + 12:head_at + 20])
        self.assertEqual((magic, upem), (0x5F0F3CF5, 800))
        _v, ascent, descent, gap, advance_max = struct.unpack(">IhhhH", self.table("hhea")[:12])
        self.assertEqual((ascent, descent, gap, advance_max), (750, -50, 0, 600),
                         "the cell is the line box: 8 pitches tall, 6 wide")
        glyphs = struct.unpack(">H", self.table("maxp")[4:6])[0]
        self.assertEqual(len(self.table("hmtx")), 4 * glyphs)
        for i in range(glyphs):
            self.assertEqual(struct.unpack(">H", self.table("hmtx")[4 * i:4 * i + 2])[0], 600, f"glyph {i}")
        self.assertEqual(len(self.table("loca")), 4 * (glyphs + 1))
        self.assertTrue(all(g < glyphs for g in self.cmap.values()))
        self.assertIn("DDM Tote".encode("utf-16-be"), self.table("name"))

    def test_the_face_is_served_and_committed(self) -> None:
        client = server.app.test_client()
        resp = client.get("/static/fonts/DDMTote.ttf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), self.data)
        resp.close()
        ignore = HERE.parent / ".gitignore"
        if ignore.exists():
            self.assertIn("!splash_display/static/fonts/DDMTote.ttf", ignore.read_text(encoding="utf-8"),
                          "*.ttf is ignored: the face needs its exception to be committed")


class HarnessTests(unittest.TestCase):
    """tools/fake_pi5.py serves what pi5 serves: the same keys, a horse's
    cup as a MAC string or null, board states 1..5, and the results."""

    def derby(self, phase: str = "winner", results=None) -> "fake_pi5.FakePi5":
        return fake_pi5.FakePi5(fake_pi5.PHASES[phase], tokens=fake_pi5.RESULTS_TOKENS,
                                scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                                names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS,
                                names_rev=2, results=results)

    def post(self, fake, cmd: str):
        with contextlib.redirect_stdout(io.StringIO()):      # the fake prints every command it takes
            return fake.app.test_client().post("/api/quiniela/cmd", json={"cmd": cmd})

    def test_the_model_is_pi5s_contract(self) -> None:
        m = json.loads(fake_pi5.FakePi5(fake_pi5.PHASES["open"]).model_json())
        self.assertEqual(set(m), PI5_MODEL_KEYS)
        self.assertEqual(m["board_states"], [1, 2, 3, 4, 5])
        self.assertIsNone(m["results"])
        self.assertEqual(sorted(m["horses"], key=int), [str(n) for n in range(1, 25)])
        for n, h in m["horses"].items():
            self.assertEqual(set(h), PI5_HORSE_KEYS, n)
            self.assertIs(h["conflict"], False)
            if int(n) <= 20:
                self.assertEqual(h["cup"], fake_pi5.mac_of(int(n)), "a MAC string, never a cup number")
                self.assertRegex(h["cup"], r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
                self.assertEqual(h["cups"], [h["cup"]])
            else:
                self.assertIsNone(h["cup"])
                self.assertEqual(h["cups"], [])
        self.assertEqual(m["cups_online"], 19, "twenty cups, one of them gone quiet")
        self.assertEqual(m["cups_no_horse"], 0)
        self.assertIs(m["horses"]["11"]["online"], False)

    def test_a_renumbered_cup_keeps_its_mac(self) -> None:
        m = json.loads(self.derby().model_json())
        self.assertEqual(m["horses"]["22"]["cup"], fake_pi5.mac_of(9), "the cup that was 9 carries 22")
        self.assertIsNone(m["horses"]["9"]["cup"])
        self.assertEqual((m["horses"]["22"]["tokens"], m["horses"]["22"]["replaced"]), (7, "THE PUMA"))

    def test_results_static_is_the_example_picture(self) -> None:
        m = json.loads(self.derby(results=fake_pi5.RESULTS_WPS).model_json())
        self.assertEqual((m["race_state"], m["race_state_name"]), (5, "WINNER"))
        self.assertEqual(m["results"], {"win": 19, "place": 1, "show": 22})
        self.assertEqual((m["pot"], m["total_tokens"]), (154.0, 158))
        self.assertEqual(m["prizes"], {"win": 92, "place": 39, "show": 23})
        self.assertEqual([m["horses"][n]["tokens"] for n in ("19", "1", "22")], [4, 11, 7])
        self.assertEqual([m["horses"][n]["name"] for n in ("19", "1", "22")], ["GOLDEN TEMPO", "RENEGADE", "OCELLI"])
        c = m["closing"]
        self.assertEqual((c["pot"], c["prizes"], c["total_tokens"]), (154.0, {"win": 92, "place": 39, "show": 23}, 158),
                         "WINNER from the start: the figures at the post are these")
        self.assertEqual([c["horses"][n]["tokens"] for n in ("19", "1", "22")], [4, 11, 7])
        self.assertEqual(set(c), {"pot", "prizes", "total_tokens", "horses", "at"})
        self.assertEqual(sorted(c["horses"], key=int), [str(n) for n in range(1, 25)])

    def test_the_figures_at_the_post_follow_pis_rule(self) -> None:
        fake = fake_pi5.FakePi5(fake_pi5.PHASES["open"], tokens=fake_pi5.RESULTS_TOKENS,
                                scratched=fake_pi5.REDESIGN_SCRATCHED, offline=(), events=[],
                                names=fake_pi5.REDESIGN_NAMES, renumbers=fake_pi5.REDESIGN_RENUMBERS, names_rev=2)
        model = lambda: json.loads(fake.model_json())       # noqa: E731
        self.assertIsNone(model()["closing"], "betting open: none")
        fake.set_phase(fake_pi5.PHASES["final"])
        self.assertIsNone(model()["closing"], "final call: none")
        fake.set_phase(fake_pi5.PHASES["closed"])
        c = model()["closing"]
        self.assertEqual((c["pot"], c["prizes"]), (154.0, {"win": 92, "place": 39, "show": 23}), "taken at the post")
        fake.bump(7)
        fake.set_phase(fake_pi5.PHASES["running"])
        for horse in fake_pi5.RESULTS_WPS:
            fake.empty(horse)
        fake.set_phase(fake_pi5.PHASES["winner"])
        self.assertTrue(fake.set_results(*fake_pi5.RESULTS_WPS))
        m = model()
        self.assertEqual(m["closing"], c, "a late token and the emptied cups move the live fields only")
        self.assertEqual((m["pot"], m["horses"]["19"]["tokens"]), (133.0, 0))
        fake.set_phase(fake_pi5.PHASES["final"])
        fake.set_phase(fake_pi5.PHASES["after"])
        self.assertEqual(model()["closing"], c, "2 and 6 leave them")
        fake.set_phase(fake_pi5.PHASES["open"])
        self.assertIsNone(model()["closing"], "state 1 drops them")
        fake.set_phase(fake_pi5.PHASES["winner"])
        self.assertEqual(model()["closing"]["pot"], 133.0, "WINNER straight from betting takes them, from the cups as they are")
        fake.reset(fake_pi5.RESULTS_TOKENS, fake_pi5.PHASES["idle"])
        self.assertIsNone(model()["closing"], "a reset drops them")
        fake.reset(fake_pi5.RESULTS_TOKENS, fake_pi5.PHASES["running"])
        self.assertEqual(model()["closing"]["pot"], 154.0, "the results cycle's reset into RUNNING takes them again")

    def test_the_hand_count_follows_pis_rule(self) -> None:
        """pi5's counted pot on the fake: taken in 3-5 once the figures at the
        post exist; closing's pot and prizes (and from the post to the end the
        model's own) are the count's, through the fake's one prizes_for; held
        with the figures; dropped by 0, 1 and a reset."""
        scale = {"win": 92, "place": 39, "show": 23}
        counted = {"win": 91, "place": 38, "show": 23}
        fake = self.derby("open")
        model = lambda: json.loads(fake.model_json())                                # noqa: E731
        keys = lambda m: (m["pot_scale"], m["pot_counted"], m["hand_counted"])       # noqa: E731
        self.assertEqual(keys(model()), (None, None, False), "betting open: no figures at the post, no count")
        self.assertIn("betting closes", fake.set_counted(152))
        self.assertIsNone(fake.counted)
        fake.set_phase(fake_pi5.PHASES["closed"])
        m = model()
        self.assertEqual((keys(m), m["pot"], m["prizes"]), ((154.0, None, False), 154.0, scale))
        self.assertIsNone(fake.set_counted(152))
        m = model()
        self.assertEqual((keys(m), m["pot"], m["prizes"]), ((154.0, 152, True), 152.0, counted))
        self.assertEqual((m["closing"]["pot"], m["closing"]["prizes"]), (152.0, counted))
        self.assertEqual(set(m), PI5_MODEL_KEYS)
        self.assertEqual(set(m["closing"]), {"pot", "prizes", "total_tokens", "horses", "at"}, "closing keeps its five keys")
        self.assertEqual((m["total_tokens"], [m["horses"][n]["tokens"] for n in ("19", "1", "22")]), (158, [4, 11, 7]),
                         "bets per horse are the scales'")
        self.assertEqual([m["closing"]["horses"][n]["tokens"] for n in ("19", "1", "22")], [4, 11, 7])
        self.assertEqual((fake_pi5.prizes_for(154), fake_pi5.prizes_for(152)), (scale, counted))
        for amount in (0, 1, 2, 30, 154, 155, 10000):
            self.assertIsNone(fake.set_counted(amount))
            m = model()
            self.assertEqual((m["prizes"], m["closing"]["prizes"]), (fake_pi5.prizes_for(amount),) * 2, amount)
            self.assertEqual(sum(m["prizes"].values()), amount, "the prizes always sum to the pot")
        for bad in (True, 1.5, "152", -1, 10001):
            self.assertIn("whole number", fake.set_counted(bad), bad)
        self.assertEqual(fake.counted, 10000, "a refused count changes nothing")
        self.assertIsNone(fake.set_counted(152))
        fake.set_phase(fake_pi5.PHASES["final"])
        m = model()
        self.assertEqual((keys(m), m["pot"], m["closing"]["pot"]), ((154.0, 152, False), 154.0, 152.0),
                         "held through FINAL CALL, where betting is open again: the live pot, no hand count")
        fake.set_phase(fake_pi5.PHASES["winner"])
        self.assertEqual(keys(model()), (154.0, 152, True))
        fake.set_phase(fake_pi5.PHASES["after"])
        m = model()
        self.assertEqual((keys(m), m["pot"]), ((154.0, 152, True), 152.0), "AFTER_PARTY keeps it")
        self.assertIn("betting closes", fake.set_counted(1), "and it is read-only there")
        fake.set_phase(fake_pi5.PHASES["open"])
        self.assertEqual(keys(model()), (None, None, False), "state 1 drops it with the figures")
        fake.set_phase(fake_pi5.PHASES["closed"])
        self.assertEqual(keys(model()), (154.0, None, False), "the next post starts clean")
        fake.set_counted(152)
        fake.reset(fake_pi5.RESULTS_TOKENS, fake_pi5.PHASES["closed"])
        self.assertEqual(keys(model()), (154.0, None, False), "a reset drops it too")
        q: "queue.Queue[str]" = queue.Queue(maxsize=32)
        fake._subs.append(q)
        fake.set_counted(152)
        self.assertEqual(json.loads(q.get_nowait())["pot_counted"], 152, "published at once")
        fake.set_counted(152)
        self.assertTrue(q.empty(), "the same count again publishes nothing")
        fake.set_counted(None)
        self.assertIsNone(json.loads(q.get_nowait())["pot_counted"])

    def test_the_fakes_counted_command(self) -> None:
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        fake = self.derby("closed")
        self.assertEqual(self.post(fake, "counted 152").status_code, 200)
        self.assertEqual(fake.counted, 152)
        self.assertEqual(json.loads(fake.model_json())["prizes"], {"win": 91, "place": 38, "show": 23})
        for bad in ("counted x", "counted 1.5", "counted -1", "counted 10001", "counted 1 2"):
            resp = self.post(fake, bad)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIs(resp.get_json()["ok"], False)
            self.assertEqual(fake.counted, 152, bad)
        self.assertEqual(self.post(fake, "counted").status_code, 200)
        self.assertIsNone(fake.counted)
        fake.set_phase(fake_pi5.PHASES["open"])
        resp = self.post(fake, "counted 152")
        self.assertEqual((resp.status_code, fake.counted), (400, None), "not before the post, as on pi5")

    def test_results_are_three_different_horses_or_nothing(self) -> None:
        fake = self.derby()
        self.assertIsNone(json.loads(fake.model_json())["results"])
        for bad in ((19, 19, 22), (0, 1, 2), (1, 2, 25), (1, 2), (1, 2, 3, 4), ("a", 1, 2)):
            self.assertFalse(fake.set_results(*bad) if len(bad) == 3 else fake._set_results_locked(bad), bad)
            self.assertIsNone(fake.results, bad)
        self.assertTrue(fake.set_results(19, 1, 22))
        self.assertEqual(json.loads(fake.model_json())["results"], {"win": 19, "place": 1, "show": 22})
        with self.assertRaises(ValueError):
            self.derby(results=(7, 7, 3))

    def test_the_results_are_published_and_a_reset_clears_them(self) -> None:
        fake = self.derby()
        q: "queue.Queue[str]" = queue.Queue(maxsize=32)
        fake._subs.append(q)
        self.assertTrue(fake.set_results(19, 1, 22))
        self.assertEqual(json.loads(q.get_nowait())["results"], {"win": 19, "place": 1, "show": 22})
        self.assertTrue(fake.set_results(19, 1, 22))
        self.assertTrue(q.empty(), "the same results again publish nothing")
        fake.empty(19)
        m = json.loads(q.get_nowait())
        self.assertEqual((m["horses"]["19"]["tokens"], m["events"][0]), (0, {"horse": 19, "delta": -4, "ts": m["events"][0]["ts"]}))
        self.assertEqual(m["results"], {"win": 19, "place": 1, "show": 22}, "emptying a cup for the draw leaves the results")
        fake.empty(19)
        self.assertTrue(q.empty(), "an empty cup has nothing to empty")
        fake.reset({}, fake_pi5.PHASES["idle"])
        m = json.loads(q.get_nowait())
        self.assertEqual((m["race_state"], m["results"], m["total_tokens"], m["events"]), (0, None, 0, []))
        fake.set_results(1, 2, 3)
        q.get_nowait()
        fake.clear_results()
        self.assertIsNone(json.loads(q.get_nowait())["results"])

    def test_the_fakes_own_commands(self) -> None:
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        fake = self.derby("running")
        resp = self.post(fake, "state 5")
        self.assertEqual((resp.status_code, resp.get_json()), (200, {"ok": True, "echo": "state 5"}))
        self.assertEqual(fake.phase, 5)
        self.assertEqual(self.post(fake, "results 19 1 22").status_code, 200)
        self.assertEqual(fake.results, (19, 1, 22))
        for bad in ("results 19 19 22", "results 1 2", "results 1 2 25", "results a b c", "results 1 2 3 4"):
            resp = self.post(fake, bad)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIs(resp.get_json()["ok"], False)
            self.assertEqual(fake.results, (19, 1, 22), bad)
        self.assertEqual(self.post(fake, "results").status_code, 200)
        self.assertIsNone(fake.results)
        # a scratch names the horse, as everything does since protocol v2
        self.assertEqual(self.post(fake, "scratch 22 1").status_code, 200)
        m = json.loads(fake.model_json())
        self.assertEqual((m["horses"]["22"]["scratched"], m["horses"]["22"]["in_field"], m["pot"]), (True, False, 147.0))
        self.assertEqual(self.post(fake, "scratch 22 0").status_code, 200)
        self.assertEqual(json.loads(fake.model_json())["pot"], 154.0)
        self.assertEqual(self.post(fake, "scratch 9 1").status_code, 200)
        self.assertNotIn(9, fake.scratched, "a horse with no cup cannot be scratched at the gateway")
        self.post(fake, "results 19 1 22")
        self.assertEqual(self.post(fake, "reset").status_code, 200)
        self.assertEqual((fake.phase, fake.results), (0, None))

    def test_the_strip_feed(self) -> None:
        """--phase strip: names that fit and names that do not (at 1920 px a
        row has 20 tiles, the name 20 - digits - 1), counts of every width
        and a dim 0, nothing ticking, and a bet cycle through 9 -> 10."""
        fake = fake_pi5.FakePi5(fake_pi5.PHASES["open"], tokens=fake_pi5.STRIP_TOKENS, scratched=(), offline=(),
                                events=[], names=fake_pi5.STRIP_NAMES, names_rev=1)
        m = json.loads(fake.model_json())
        field = {int(n): h for n, h in m["horses"].items() if h["in_field"]}
        self.assertEqual(sorted(field), list(range(1, 21)))
        counts = {n: h["tokens"] for n, h in field.items()}
        for count in (0, 7, 23, 104):
            self.assertIn(count, counts.values())
        self.assertEqual({len(str(c)) for c in counts.values()}, {1, 2, 3}, "every bet width")
        names = {n: h["name"] for n, h in field.items()}
        for name in ("GRAND MO THE FIRST", "EMERGING MARKET", "CATCHING FREEDOM"):
            self.assertIn(name, names.values())
        too_long = sorted(n for n, name in names.items() if len(name) > 20 - len(str(counts[n])) - 1)
        self.assertEqual(too_long, [6, 11, 20], "three rows scroll at 1920 px, and 2 from 10 bets")
        self.assertIsNone(m["closes_at"], "no countdown: nothing on the board ticks by itself")
        self.assertEqual(counts[fake_pi5.STRIP_BUMP_HORSE], 7)
        seen = [fake_pi5.strip_bet(fake) for _ in range(7)]
        self.assertEqual(seen, [8, 9, 10, 11, 12, 7, 8], "7 -> 12, crossing 9 -> 10, then back to 7")
        events = json.loads(fake.model_json())["events"]
        self.assertEqual([e["delta"] for e in events[:3]], [1, -5, 1], "the way back is one removal")

    def test_the_cycle_has_somebody_to_win(self) -> None:
        fake = fake_pi5.FakePi5(fake_pi5.PHASES["running"])
        self.assertEqual(fake_pi5.top_three(fake), (7, 3, 10), "most tokens first; 13 is scratched")
        self.assertTrue(fake.set_results(*fake_pi5.top_three(fake)))


class ServerStartupTests(unittest.TestCase):
    def test_link_starts_only_in_the_serving_process(self) -> None:
        saved_debug = config.DEBUG
        saved_env = os.environ.get("WERKZEUG_RUN_MAIN")
        try:
            for debug, env, expected in (
                (False, None, True),     # systemd: one process, no reloader
                (False, "true", True),
                (True, None, False),     # the reloader's parent only watches files
                (True, "true", True),    # the reloader's child serves requests
            ):
                config.DEBUG = debug
                if env is None:
                    os.environ.pop("WERKZEUG_RUN_MAIN", None)
                else:
                    os.environ["WERKZEUG_RUN_MAIN"] = env
                self.assertIs(server._serves_requests(), expected, (debug, env))
        finally:
            config.DEBUG = saved_debug
            if saved_env is None:
                os.environ.pop("WERKZEUG_RUN_MAIN", None)
            else:
                os.environ["WERKZEUG_RUN_MAIN"] = saved_env

    def test_config_points_at_pi5(self) -> None:
        self.assertEqual(config.FLASK_PORT, 5001)
        self.assertTrue(config.PI5_URL.startswith("http://"))
        self.assertEqual(quiniela.link.base_url, config.PI5_URL.rstrip("/"))
        # The race is La Quiniela's (the relayed model): no post time, no
        # race poller, no roster URL of the splash's own.
        for gone in ("GATEWAY_PORT", "TOKEN_VALUE", "QUINIELA_LOG", "QUINIELA_BOARD_STATES",
                     "DDM_2026_POST_TIME_ISO", "DASHBOARD_RACE_URL", "RACE_DATA_STALENESS_S"):
            self.assertFalse(hasattr(config, gone), gone)


if __name__ == "__main__":
    unittest.main(verbosity=2)

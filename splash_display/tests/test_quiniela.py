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
import race_poller  # noqa: E402

# The dev harness, by path (tools/ is not a package). Importing it starts
# nothing: its servers and the splash's own modules only come with main().
_spec = importlib.util.spec_from_file_location("fake_pi5", HERE / "tools" / "fake_pi5.py")
fake_pi5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_pi5)

# ... and the tool that builds the tote look's face.
_spec = importlib.util.spec_from_file_location("make_tote_font", HERE / "tools" / "make_tote_font.py")
make_tote_font = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_tote_font)

# Keep the background threads out of the test process: the dashboard poller
# would hit joeydevpi.local every 30 s and the link thread would try pi5
# every few seconds. Neither is under test; start_*() are idempotent guards,
# so marking them started makes server's module-load calls no-ops.
race_poller._started = True
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
}
PI5_HORSE_KEYS = {"tokens", "share", "scratched", "online", "cup", "conflict", "cups",
                  "name", "replaced", "in_field"}
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


class RosterTests(unittest.TestCase):
    """The horse-roster slide takes the field as pi5's /api/race lists it:
    La Quiniela's names, program numbers 1-24, scratched horses absent."""

    def setUp(self) -> None:
        self._saved = race_poller.get_race_data

    def tearDown(self) -> None:
        race_poller.get_race_data = self._saved

    def feed(self, numbers) -> None:
        horses = [{"number": n, "name": f"Horse {n}", "odds": None, "finish": None} for n in numbers]
        race_poller.get_race_data = lambda: {"race_state": "pre-race", "post_time": "6:57 PM ET",
                                             "post_time_iso": "", "last_updated": "", "horses": horses,
                                             "winner": None}

    def test_two_columns_of_ten_in_numeric_order(self) -> None:
        self.feed(range(1, 21))
        ctx = server._build_horse_roster_context()
        self.assertEqual([h["number"] for h in ctx["horses_left"]], list(range(1, 11)))
        self.assertEqual([h["number"] for h in ctx["horses_right"]], list(range(11, 21)))

    def test_an_also_eligible_that_drew_in_is_listed_where_its_number_sorts(self) -> None:
        field = [n for n in range(1, 20) if n != 9] + [22]      # 9 -> 22, 20 scratched
        self.feed(reversed(field))
        ctx = server._build_horse_roster_context()
        self.assertEqual([h["number"] for h in ctx["horses_left"]], [1, 2, 3, 4, 5, 6, 7, 8, 10, 11])
        self.assertEqual([h["number"] for h in ctx["horses_right"]], [12, 13, 14, 15, 16, 17, 18, 19, 22])

    def test_no_horses_drops_the_slide(self) -> None:
        self.feed([])
        self.assertIsNone(server._build_horse_roster_context())
        self.feed([0, 25])
        self.assertIsNone(server._build_horse_roster_context())

    def test_the_slide_renders_a_cloth_for_22(self) -> None:
        self.feed([1, 22])
        with server.app.test_request_context("/"):
            html = server.render_template("splash/horse_roster.html", **server._build_horse_roster_context())
        self.assertIn("splash-saddle--pos-22", html)
        css = (HERE / "static" / "css" / "ddm_style.css").read_text(encoding="utf-8")
        for n in (21, 22, 23, 24):
            self.assertIn(f".splash-saddle--pos-{n} ", css)


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
        # names: a row's (the template) and the results' three
        self.assertEqual(html.count('data-tote="name"'), 1 + 3)
        for sign in ('class="qb-saddle"', 'class="qb-result-saddle"', 'id="qb-banner-text"', 'id="qb-toast-name"',
                     'class="qb-result-place"', 'class="qb-prize-k"'):
            tag = html[html.index(sign) - 40:html.index(">", html.index(sign))]
            self.assertNotIn("data-tote", tag, f"{sign} is a cloth or a sign, not a tote field")

    def test_the_stylesheet_and_the_script_know_the_looks(self) -> None:
        css = (HERE / "static" / "css" / "quiniela_board.css").read_text(encoding="utf-8")
        self.assertIn('font-family: "DDM Tote";', css)
        self.assertIn('url("../fonts/DDMTote.ttf")', css)
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
                       "function layoutStrips", "function updateScroll", "STRIP_TILE_MIN", "--qb-strip-tile-w"):
            self.assertIn(needle, js)
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
# size and the window says it was resized, as a TV's would.
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
    window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 16);
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
        return { state: board.dataset.state, view: board.dataset.view || 'rows', look: board.dataset.look,
                 visible: board.classList.contains('is-visible'), banner: text('#qb-banner-text'),
                 pot: text('#qb-pot'), prizes: [text('#qb-prize-win'), text('#qb-prize-place'), text('#qb-prize-show')],
                 rows: rows, strips: strips, results: results, pulses: pulses,
                 toast: toast.classList.contains('is-shown') ? text('#qb-toast-num') + ' ' + text('#qb-toast-name') + ' ' + text('#qb-toast-delta') : null };
    }
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
            } else {
                stream.onmessage({ data: JSON.stringify(m) });
            }
            await new Promise((resolve) => setTimeout(resolve, WAIT_MS));
            out.push(read());
        }
        document.getElementById('probe').textContent = JSON.stringify(out);
    }
})();
"""


def run_board(models: List[dict], look: str = "impact", styled: bool = False,
              stage: Tuple[int, int] = (1920, 1080)) -> List[dict]:
    """The board's real template and script in a page of their own, fed
    `models` in turn; what the board showed after each. Styled, the page is
    served over loopback with the board's stylesheet and fonts, the board on
    a `stage` (1920x1080 unless said), which is what the tote look's rows
    need to be measured; otherwise it is a file with the template and the
    script alone."""
    with server.app.test_request_context("/"):
        board_html = server.render_template("splash/quiniela_live.html", quiniela_look=look)
    probe = BOARD_PROBE_JS.replace("__MODELS__", json.dumps(models))
    tmp = Path(tempfile.mkdtemp(prefix="qb_page_"))
    srv = thread = None
    try:
        if styled:
            # The board fills a 1920x1080 stage, as it fills #slideshow-stage on
            # the TV: a headless window's viewport is its size less a frame.
            page = ('<!doctype html><html><head><meta charset="utf-8">'
                    '<link rel="stylesheet" href="/static/css/quiniela_board.css"></head><body style="margin: 0">\n'
                    f'<div id="qb-stage" style="position: relative; width: {stage[0]}px; height: {stage[1]}px; '
                    'overflow: hidden">' + board_html
                    + '</div>\n<pre id="probe" style="display: none"></pre>\n<script>' + probe
                    + '</script>\n<script src="/static/js/quiniela_board.js"></script>\n</body></html>\n')
            app = Flask("board_page", static_folder=str(HERE / "static"), static_url_path="/static")
            app.add_url_rule("/", "page", lambda: page)
            logging.getLogger("werkzeug").setLevel(logging.ERROR)      # no line per request
            srv = make_server("127.0.0.1", 0, app, threaded=True)
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{srv.server_port}/"
        else:
            script = (HERE / "static" / "js" / "quiniela_board.js").read_text(encoding="utf-8")
            page = ('<!doctype html><html><head><meta charset="utf-8"></head><body>\n' + board_html
                    + '\n<pre id="probe"></pre>\n<script>' + probe + '</script>\n<script>' + script
                    + '</script>\n</body></html>\n')
            path = tmp / "board.html"
            path.write_text(page, encoding="utf-8")
            url = path.resolve().as_uri()
        cmd = [CHROME, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--hide-scrollbars", "--window-size=1920,1080", "--user-data-dir=" + str((tmp / "profile").resolve()),
               "--virtual-time-budget=" + str(1000 + 900 * len(models)), "--dump-dom", url]
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
        for line in fake_pi5.CHYRON_LINES + ["SCRATCHED", "\u00b7 TOKENS REFUNDED", "NO BETS", "$1,234"]:
            for ch in line:
                self.assertIn(ord(ch), self.cmap, f"{ch!r} in {line!r}")

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
        self.assertEqual(config.DASHBOARD_RACE_URL, config.PI5_URL + "/api/race")
        self.assertEqual(quiniela.link.base_url, config.PI5_URL.rstrip("/"))
        for gone in ("GATEWAY_PORT", "TOKEN_VALUE", "QUINIELA_LOG", "QUINIELA_BOARD_STATES"):
            self.assertFalse(hasattr(config, gone), gone)


if __name__ == "__main__":
    unittest.main(verbosity=2)

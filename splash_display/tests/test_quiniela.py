"""
Unit tests for splash_display/quiniela.py (the pi5 relay).

Run from splash_display/ (stdlib unittest, no pytest needed):

    python -m unittest -v tests.test_quiniela

No network beyond loopback is used: pi5 is simulated by an injected opener
(the link's HTTP seam), a fake sleeper and a fake clock drive the reconnect
loop without real time, and the one in-process fake pi5 (the cmd relay test)
listens on an ephemeral loopback port.
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
import quiniela  # noqa: E402
import race_poller  # noqa: E402

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
UNASSIGNED = {"tokens": 0, "share": 0.0, "scratched": False, "online": False, "cup": None}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pi5_model(race_state: int = 1, tokens: Optional[Dict[int, int]] = None, link_ok: bool = True,
              updated: float = 1_700_000_000.0, events=None, board_states=(1, 2, 3, 4), **extra):
    """A model as pi5 serves it: cups 1-based (cup n on horse n), share 4 dp,
    leader = strictly most tokens (lowest horse on a tie)."""
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
            "cup": n if n in tokens else None,
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
        self.assertEqual(served["horses"]["7"]["cup"], 7, "cup numbers are pi5's 1-based ones")
        self.assertEqual(served["updated"], 1_600_000_000.0, "updated is kept as received")
        self.assertIs(served["link_ok"], True)
        self.assertTrue(self.board.pi5_ok())
        # pi5's own link_ok false (its gateway is quiet) is served as false.
        self.assertTrue(self.board.apply_model(pi5_model(link_ok=False)))
        self.assertIs(self.board.model()["link_ok"], False)
        # ...and a truthy non-bool becomes a bool.
        self.assertTrue(self.board.apply_model(pi5_model(link_ok="yes")))
        self.assertIs(self.board.model()["link_ok"], True)

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
        self.assertEqual(m["horses"]["7"]["cup"], 7)
        self.assertEqual(m["board_states"], [1, 2, 3, 4])
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

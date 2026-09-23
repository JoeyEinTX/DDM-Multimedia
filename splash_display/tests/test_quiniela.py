"""
Unit tests for splash_display/quiniela.py.

Run from splash_display/ (stdlib unittest, no pytest needed):

    python -m unittest -v tests.test_quiniela

No serial port and no network are used: the gateway is simulated by feeding
lines into GatewayLink.feed_line(), clocks are injected, and the event log is
pointed at a temp dir.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config  # noqa: E402
import quiniela  # noqa: E402
import race_poller  # noqa: E402

# Keep the background threads out of the test process: the dashboard poller
# would hit joeydevpi.local every 30 s and the link thread would scan for a
# port every few seconds. Neither is under test; start_*() are idempotent
# guards, so marking them started makes server's module-load calls no-ops.
race_poller._started = True
quiniela._started = True

import server  # noqa: E402

from quiniela import BettingBoard, GatewayLink, sse_events, validate_cmd  # noqa: E402

LOGGER = "splash_display.quiniela"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSerial:
    """Just enough of serial.Serial for send()."""

    def __init__(self) -> None:
        self.written: list = []
        self.fail = False
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.fail:
            raise OSError("write failed")
        self.written.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


def cup(cid: int, h: int, tok: int = 0, scr: int = 0, age: int = 100, **extra):
    entry = {
        "id": cid,
        "mac": "20:50:0D:11:D9:%02X" % cid,
        "h": h,
        "scr": scr,
        "tok": tok,
        "rssi": -63,
        "up": -61,
        "age": age,
    }
    entry.update(extra)
    return entry


def state(st: int = 1, cups=(), seq: int = 1):
    return {
        "t": "state",
        "seq": seq,
        "st": st,
        "demo": 0,
        "mac": "A4:F0:0F:5E:0B:08",
        "cups": list(cups),
    }


def state_line(**kw) -> str:
    return json.dumps(state(**kw))


class BoardCase(unittest.TestCase):
    """A fresh BettingBoard with injected clocks and a temp log dir."""

    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.wall = FakeClock(1_700_000_000.0)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name) / "logs"
        self.board = BettingBoard(clock=self.clock, wall=self.wall, log_dir=self.log_dir)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class ModelTests(BoardCase):
    def test_empty_state_model_shape(self) -> None:
        self.assertTrue(self.board.apply_state(state(st=1, cups=[])))
        m = self.board.model()
        self.assertEqual(
            set(m),
            {
                "link_ok", "race_state", "race_state_name", "token_value", "pot",
                "total_tokens", "horses", "leader", "events", "updated", "board_states",
            },
        )
        self.assertTrue(m["link_ok"])
        self.assertEqual(m["race_state"], 1)
        self.assertEqual(m["race_state_name"], "BETTING_OPEN")
        self.assertEqual(m["token_value"], float(config.TOKEN_VALUE))
        self.assertEqual(m["pot"], 0.0)
        self.assertEqual(m["total_tokens"], 0)
        self.assertEqual(sorted(m["horses"], key=int), [str(n) for n in range(1, 21)])
        for entry in m["horses"].values():
            self.assertEqual(
                entry, {"tokens": 0, "share": 0, "scratched": False, "online": False, "cup": None}
            )
        self.assertIsNone(m["leader"])
        self.assertEqual(m["events"], [])
        self.assertEqual(m["updated"], self.wall.now)
        self.assertEqual(m["board_states"], list(config.QUINIELA_BOARD_STATES))

    def test_tokens_share_leader_online_scratched(self) -> None:
        self.board.apply_state(state(cups=[
            cup(0, 7, tok=23, age=180),
            cup(1, 3, tok=10, scr=1, age=7000),
            cup(2, 12, tok=0, age=6000),
        ]))
        m = self.board.model()
        self.assertEqual(m["total_tokens"], 33)
        self.assertEqual(m["pot"], round(33 * float(config.TOKEN_VALUE), 2))
        h7, h3, h12 = m["horses"]["7"], m["horses"]["3"], m["horses"]["12"]
        self.assertEqual(h7, {"tokens": 23, "share": round(23 / 33, 4), "scratched": False,
                              "online": True, "cup": 0})
        self.assertEqual(h3["tokens"], 10)
        self.assertEqual(h3["share"], round(10 / 33, 4))
        self.assertTrue(h3["scratched"])
        self.assertFalse(h3["online"], "age 7000 ms is offline")
        self.assertEqual(h3["cup"], 1)
        self.assertTrue(h12["online"], "age 6000 ms is the online limit, inclusive")
        self.assertEqual(h12["share"], 0)
        self.assertEqual(m["leader"], 7)

    def test_leader_none_without_tokens_and_lowest_on_tie(self) -> None:
        self.board.apply_state(state(cups=[cup(0, 4), cup(1, 9)]))
        self.assertIsNone(self.board.model()["leader"])
        self.board.apply_state(state(cups=[cup(0, 9, tok=5), cup(1, 4, tok=5)]))
        self.assertEqual(self.board.model()["leader"], 4)

    def test_unassigned_and_odd_cups_never_raise(self) -> None:
        self.board.apply_state(state(cups=[
            {"id": 0, "mac": "", "h": 5, "age": -1},          # never heard, no tok
            {"id": 1, "h": 0, "tok": 4},                       # horse 0 = unassigned
            {"id": 2, "h": 21, "tok": 4},                      # out of range
            {"id": 3, "h": "8", "tok": "6", "scr": "1", "age": "50"},  # strings
            {"h": 9, "tok": 3},                                # no id
            "garbage",                                         # not a dict
            {"id": 5, "h": None, "tok": 2},
            {"id": 6, "h": 10, "tok": -4, "age": 10},          # negative tokens clamp to 0
        ]))
        m = self.board.model()
        self.assertEqual(m["horses"]["5"], {"tokens": 0, "share": 0, "scratched": False,
                                            "online": False, "cup": 0})
        self.assertEqual(m["horses"]["8"], {"tokens": 6, "share": 1.0, "scratched": True,
                                            "online": True, "cup": 3})
        self.assertEqual(m["horses"]["10"]["tokens"], 0)
        self.assertEqual(m["horses"]["10"]["cup"], 6)
        for n in (1, 2, 9, 12, 20):
            self.assertEqual(m["horses"][str(n)], {"tokens": 0, "share": 0, "scratched": False,
                                                   "online": False, "cup": None})
        self.assertEqual(m["total_tokens"], 6)
        # Missing / non-list cups and a missing st must not raise either.
        self.board.apply_state({"t": "state"})
        self.board.apply_state({"t": "state", "st": "x", "cups": {"id": 0}})
        self.assertEqual(self.board.model()["total_tokens"], 0)

    def test_events_diff_newest_first_last_eight(self) -> None:
        self.board.apply_state(state(cups=[cup(0, 7, tok=23), cup(1, 3, tok=2)]))
        self.assertEqual(self.board.model()["events"], [],
                         "the first state line is the baseline, not a bet")
        self.wall.advance(1)
        self.board.apply_state(state(cups=[cup(0, 7, tok=24), cup(1, 3, tok=2)]))
        self.assertEqual(self.board.model()["events"],
                         [{"horse": 7, "delta": 1, "ts": self.wall.now}])
        self.wall.advance(1)
        self.board.apply_state(state(cups=[cup(0, 7, tok=24), cup(1, 3, tok=0)]))
        events = self.board.model()["events"]
        self.assertEqual([e["horse"] for e in events], [3, 7])
        self.assertEqual(events[0]["delta"], -2)
        self.assertEqual(events[0]["ts"], self.wall.now)
        # An unchanged snapshot adds nothing; then ten more drops keep only 8.
        self.board.apply_state(state(cups=[cup(0, 7, tok=24), cup(1, 3, tok=0)]))
        self.assertEqual(len(self.board.model()["events"]), 2)
        for i in range(1, 11):
            self.wall.advance(1)
            self.board.apply_state(state(cups=[cup(0, 7, tok=24 + i), cup(1, 3, tok=0)]))
        events = self.board.model()["events"]
        self.assertEqual(len(events), 8)
        self.assertEqual([e["delta"] for e in events], [1] * 8)
        self.assertGreater(events[0]["ts"], events[-1]["ts"], "newest first")
        self.assertEqual(self.board.model()["horses"]["7"]["tokens"], 34)

    def test_duplicate_horse_keeps_lowest_cup_and_warns_once(self) -> None:
        cups = [cup(3, 7, tok=5), cup(1, 7, tok=9), cup(2, 7, tok=1)]   # out of order on purpose
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            self.board.apply_state(state(cups=cups))
            self.board.apply_state(state(cups=cups, seq=2))
            self.board.apply_state(state(cups=cups, seq=3))
        warnings = [r for r in captured.records if "both claim horse 7" in r.getMessage()]
        self.assertEqual(len(warnings), 2, "one WARNING per distinct (cup, cup) pair")
        h7 = self.board.model()["horses"]["7"]
        self.assertEqual(h7["cup"], 1)
        self.assertEqual(h7["tokens"], 9)
        self.assertEqual(self.board.model()["total_tokens"], 9)

    def test_race_state_names(self) -> None:
        expected = {0: "PRE_RACE", 1: "BETTING_OPEN", 2: "FINAL_CALL", 3: "AT_THE_POST",
                    4: "RUNNING", 5: "WINNER", 6: "AFTER_PARTY"}
        for st, name in expected.items():
            self.board.apply_state(state(st=st))
            m = self.board.model()
            self.assertEqual((m["race_state"], m["race_state_name"]), (st, name))
        self.board.apply_state(state(st=9))
        self.assertEqual(self.board.model()["race_state_name"], "STATE_9")
        self.board.apply_state({"t": "state", "phase": 2, "cups": []})
        self.assertEqual(self.board.model()["race_state_name"], "FINAL_CALL",
                         "phase is accepted as a fallback for st")

    def test_link_ok_expiry_via_tick(self) -> None:
        q = self.board.subscribe()
        self.assertFalse(self.board.model()["link_ok"])
        self.board.apply_state(state())
        self.assertTrue(self.board.model()["link_ok"])
        self.assertEqual(q.qsize(), 1)
        self.clock.advance(4.9)
        self.assertFalse(self.board.tick())
        self.assertTrue(self.board.model()["link_ok"])
        self.assertEqual(q.qsize(), 1, "nothing published while link_ok is unchanged")
        self.clock.advance(0.2)
        self.assertTrue(self.board.tick())
        m = self.board.model()
        self.assertFalse(m["link_ok"])
        self.assertEqual(m["race_state"], 1, "other fields keep their last values")
        self.assertEqual(q.qsize(), 2)
        self.assertFalse(json.loads(q.get_nowait())["link_ok"] is True and q.qsize() == 0)
        published = json.loads(q.get_nowait())
        self.assertFalse(published["link_ok"])
        self.assertFalse(self.board.tick(), "already published")
        self.board.apply_state(state(seq=2))
        self.assertTrue(self.board.model()["link_ok"], "a new state line restores it")

    def test_unchanged_state_is_not_published(self) -> None:
        q = self.board.subscribe()
        self.assertTrue(self.board.apply_state(state(cups=[cup(0, 7, tok=1)])))
        self.wall.advance(1)
        self.assertFalse(self.board.apply_state(state(cups=[cup(0, 7, tok=1)], seq=2)))
        self.assertEqual(q.qsize(), 1)
        # A cup going quiet is a model change (online flips) and is published.
        self.assertTrue(self.board.apply_state(state(cups=[cup(0, 7, tok=1, age=9000)], seq=3)))
        self.assertEqual(q.qsize(), 2)

    def test_subscriber_queue_drops_oldest_when_full(self) -> None:
        q = self.board.subscribe()
        for i in range(quiniela.SSE_QUEUE_SIZE + 8):
            self.board.apply_state(state(cups=[cup(0, 7, tok=i)]))
        self.assertEqual(q.qsize(), quiniela.SSE_QUEUE_SIZE)
        newest = None
        while not q.empty():
            newest = json.loads(q.get_nowait())
        self.assertEqual(newest["horses"]["7"]["tokens"], quiniela.SSE_QUEUE_SIZE + 7)
        self.board.unsubscribe(q)
        self.assertEqual(self.board.subscriber_count(), 0)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------
class EventLogTests(BoardCase):
    def _lines(self):
        files = list(self.log_dir.glob("quiniela_*.jsonl")) if self.log_dir.exists() else []
        if not files:
            return None, []
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        return files[0], [json.loads(line) for line in text.splitlines() if line]

    def test_writes_one_line_per_model_change(self) -> None:
        self.assertFalse(self.log_dir.exists(), "created lazily")
        self.board.apply_state(state(st=1, cups=[cup(0, 7, tok=23)]))
        path, lines = self._lines()
        self.assertIsNotNone(path)
        import datetime
        self.assertEqual(path.name, f"quiniela_{datetime.date.today().isoformat()}.jsonl")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], {
            "ts": round(self.wall.now, 3),
            "race_state": 1,
            "changes": [{"horse": 7, "tokens": [0, 23]}, {"race_state": [0, 1]}],
            "total_tokens": 23,
        })
        self.wall.advance(2)
        self.board.apply_state(state(st=2, cups=[cup(0, 7, tok=24, scr=1)], seq=2))
        _, lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["changes"], [
            {"horse": 7, "tokens": [23, 24]},
            {"horse": 7, "scratched": [False, True]},
            {"race_state": [1, 2]},
        ])
        self.assertEqual(lines[1]["total_tokens"], 24)
        # Same again: no line. Only "online" flips: no line either.
        self.board.apply_state(state(st=2, cups=[cup(0, 7, tok=24, scr=1)], seq=3))
        self.board.apply_state(state(st=2, cups=[cup(0, 7, tok=24, scr=1, age=9999)], seq=4))
        self.clock.advance(10)
        self.board.tick()
        _, lines = self._lines()
        self.assertEqual(len(lines), 2)

    def test_disabled_by_config(self) -> None:
        saved = config.QUINIELA_LOG
        config.QUINIELA_LOG = False
        try:
            self.board.apply_state(state(cups=[cup(0, 7, tok=1)]))
        finally:
            config.QUINIELA_LOG = saved
        self.assertFalse(self.log_dir.exists())

    def test_write_failure_warns_once_and_disables(self) -> None:
        self.log_dir.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.write_text("not a directory")   # mkdir() on it raises OSError
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            self.board.apply_state(state(cups=[cup(0, 7, tok=1)]))
            self.board.apply_state(state(cups=[cup(0, 7, tok=2)]))
        warnings = [r for r in captured.records if "event log disabled" in r.getMessage()]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.board.model()["horses"]["7"]["tokens"], 2, "model unaffected")

    def test_module_log_dir_is_the_default(self) -> None:
        saved = quiniela.LOG_DIR
        quiniela.LOG_DIR = Path(self.tmp.name) / "module_logs"
        try:
            board = BettingBoard(clock=self.clock, wall=self.wall)   # no log_dir given
            board.apply_state(state(cups=[cup(0, 7, tok=1)]))
            self.assertEqual(len(list(quiniela.LOG_DIR.glob("quiniela_*.jsonl"))), 1)
        finally:
            quiniela.LOG_DIR = saved


# ---------------------------------------------------------------------------
# Gateway link (no port)
# ---------------------------------------------------------------------------
class LinkTests(BoardCase):
    def setUp(self) -> None:
        super().setUp()
        self.link = GatewayLink(on_state=self.board.apply_state, clock=self.clock)

    def test_feed_line_discards_everything_but_state_and_hello(self) -> None:
        with self.assertLogs(LOGGER, level="DEBUG") as captured:
            self.assertIsNone(self.link.feed_line("# build: DDM_AUTO_DEMO=0"))
            self.assertIsNone(self.link.feed_line(""))
            self.assertIsNone(self.link.feed_line("{not json"))
            self.assertIsNone(self.link.feed_line("[1,2,3]"))
            self.assertIsNone(self.link.feed_line('{"t":"status","seq":1}'))
            self.assertIsNone(self.link.feed_line('{"t":"telem","id":0}'))
            self.assertIsNone(self.link.feed_line('{"t":"err","why":"invalid"}'))
        self.assertTrue(all(r.levelname == "DEBUG" for r in captured.records))
        snap = self.link.snapshot()
        self.assertEqual(snap, {"received_at": None, "link_ok": False})
        self.assertFalse(self.board.model()["link_ok"])
        self.assertFalse(self.link.hello_pending)

    def test_state_line_updates_snapshot_board_and_link_ok(self) -> None:
        line = state_line(st=3, cups=[cup(0, 7, tok=23)])
        self.assertEqual(self.link.feed_line(line + "\r"), "state")
        snap = self.link.snapshot()
        self.assertTrue(snap["link_ok"])
        self.assertEqual(snap["received_at"], self.clock.now)
        self.assertEqual(snap["st"], 3)
        self.assertEqual(snap["cups"][0]["tok"], 23)
        m = self.board.model()
        self.assertEqual(m["race_state_name"], "AT_THE_POST")
        self.assertEqual(m["horses"]["7"]["tokens"], 23)
        self.clock.advance(5.5)
        self.assertFalse(self.link.snapshot()["link_ok"])
        self.assertEqual(self.link.snapshot()["st"], 3, "last state is kept")

    def test_handler_failure_does_not_propagate(self) -> None:
        def boom(_obj):
            raise RuntimeError("model bug")
        link = GatewayLink(on_state=boom, clock=self.clock)
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(link.feed_line(state_line()), "state")
        self.assertTrue(link.snapshot()["link_ok"])

    def test_send_without_port(self) -> None:
        self.assertFalse(self.link.connected)
        self.assertFalse(self.link.send("state 1"))

    def test_send_with_port_and_write_failure(self) -> None:
        fake = FakeSerial()
        self.link._attach_port(fake, "/dev/fake")
        self.assertTrue(self.link.connected)
        self.assertTrue(self.link.send("state 1"))
        self.assertTrue(self.link.send("horse 0 7\n"))
        self.assertEqual(fake.written, [b"state 1\n", b"horse 0 7\n"])
        fake.fail = True
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertFalse(self.link.send("state 2"))
        self.link._detach_port()
        self.assertTrue(fake.closed)
        self.assertFalse(self.link.connected)

    def test_json_handshake_and_resend_rules(self) -> None:
        fake = FakeSerial()
        self.link._attach_port(fake, "/dev/fake")
        self.link._send_json_on(first=True)
        self.assertEqual(fake.written, [b"json 1\n"])
        # Quiet but within 5 s: nothing.
        self.clock.advance(4.0)
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 1)
        # A state line resets the silence clock.
        self.link.feed_line(state_line())
        self.clock.advance(4.0)
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 1)
        # 5 s without a state line (and 5 s since the last send): re-send once.
        self.clock.advance(1.5)
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 2)
        self.clock.advance(1.0)
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 2, "at most once per 5 s")
        # A hello means the gateway rebooted: re-send, rate limited to 1/s.
        self.clock.advance(0.5)   # 1.5 s since the last send
        self.assertEqual(self.link.feed_line('{"t":"hello","fw":"x"}'), "hello")
        self.assertTrue(self.link.hello_pending)
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 3)
        self.assertFalse(self.link.hello_pending)
        # The gateway repeats hello every 2 s while silent; a second one
        # right away waits for the 1 s gap.
        self.assertEqual(self.link.feed_line('{"t":"hello","fw":"x"}'), "hello")
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 3, "not within 1 s of the last send")
        self.assertTrue(self.link.hello_pending)
        self.clock.advance(1.0)
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 4)
        self.assertFalse(self.link.hello_pending)
        self.assertEqual(set(fake.written), {b"json 1\n"})

    def test_repeat_hello_while_state_lines_flow_is_not_a_reboot(self) -> None:
        # The gateway repeats hello every 2 s until pi5 sends it a downlink
        # state line, which this display never does, so with auto-emit on
        # the port carries a state line every second and a hello every two.
        fake = FakeSerial()
        self.link._attach_port(fake, "/dev/fake")
        self.link._send_json_on(first=True)
        self.clock.advance(1.0)
        self.link.feed_line(state_line())
        self.clock.advance(0.5)
        with self.assertLogs(LOGGER, level="DEBUG") as captured:
            self.assertEqual(self.link.feed_line('{"t":"hello","v":1}'), "hello")
            self.assertTrue(self.link.hello_pending)
            self.link._maybe_resend_json()
        self.assertFalse(self.link.hello_pending, "dropped, not deferred")
        self.assertEqual(fake.written, [b"json 1\n"], "no re-send while state lines flow")
        self.assertTrue(any("not a reboot" in r.getMessage() for r in captured.records))
        # ...however long it goes on (here a hello between every two state lines).
        for seq in range(2, 12):
            self.clock.advance(0.5)
            self.link.feed_line('{"t":"hello","v":1}')
            self.link._maybe_resend_json()
            self.clock.advance(0.5)
            self.link.feed_line(state_line(seq=seq))
            self.link._maybe_resend_json()
        self.assertEqual(fake.written, [b"json 1\n"])
        # Then the gateway reboots: the state lines stop. Its first hello
        # comes 0.8 s after the last state line, inside the 1.5 s window, so
        # it still looks like a repeat; the next one, 2 s later, does not.
        self.clock.advance(0.8)
        self.link.feed_line('{"t":"hello","v":1}')
        self.link._maybe_resend_json()
        self.assertEqual(len(fake.written), 1, "too soon to tell from a repeat")
        self.assertFalse(self.link.hello_pending)
        self.clock.advance(2.0)
        self.link.feed_line('{"t":"hello","v":1}')
        self.link._maybe_resend_json()
        self.assertEqual(fake.written, [b"json 1\n"] * 2, "state silent for 2.8 s: re-sent")
        self.assertFalse(self.link.hello_pending)
        # A hello during silence that the 1 s gap holds back stays pending;
        # if state lines resume before the gap is up (the re-send worked) it
        # is dropped without a second send.
        self.clock.advance(0.2)
        self.link.feed_line('{"t":"hello","v":1}')
        self.link._maybe_resend_json()
        self.assertTrue(self.link.hello_pending, "within 1 s of the last send")
        self.assertEqual(len(fake.written), 2)
        self.clock.advance(0.3)
        self.link.feed_line(state_line(seq=99))
        self.link._maybe_resend_json()
        self.assertFalse(self.link.hello_pending)
        self.assertEqual(len(fake.written), 2)

    def test_autodetect_matches_known_bridges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pattern = os.path.join(tmp, "*")
            self.assertIsNone(GatewayLink.autodetect(pattern))
            for name in ("usb-Some_Printer-if00", "usb-Silicon_Labs_CP2102_USB_to_UART-if00-port0",
                         "usb-1a86_USB_Serial-if00-port0"):
                Path(tmp, name).write_text("")
            self.assertEqual(os.path.basename(GatewayLink.autodetect(pattern)),
                             "usb-1a86_USB_Serial-if00-port0", "first match in sorted order")
        self.assertIsNone(GatewayLink.autodetect(os.path.join(tmp, "*")), "dir gone")

    def test_autodetect_matches_every_ch34x_by_id_name(self) -> None:
        # udev names a CH34x by QinHeng's vendor id (its manufacturer string
        # is empty) plus the product string, which differs per chip: a CH340G
        # says "USB2.0-Serial", newer CH340s "USB Serial", a CH9102 "USB
        # Single Serial". None of those carries "ch340".
        for name in (
            "usb-1a86_USB2.0-Serial-if00-port0",
            "usb-1a86_USB_Serial-if00-port0",
            "usb-1a86_USB_Single_Serial_54E5029053-if00",
            "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0",
        ):
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "usb-Some_Printer-if00").write_text("")
                Path(tmp, "usb-FTDI_FT232R_USB_UART_A1B2C3-if00-port0").write_text("")
                Path(tmp, name).write_text("")
                found = GatewayLink.autodetect(os.path.join(tmp, "*"))
                self.assertIsNotNone(found, name)
                self.assertEqual(os.path.basename(found), name)

    def test_start_link_without_pyserial_warns_and_idles(self) -> None:
        saved_serial, saved_started = quiniela.serial, quiniela._started
        quiniela.serial, quiniela._started = None, False
        try:
            with self.assertLogs(LOGGER, level="WARNING") as captured:
                quiniela.start_link()
                quiniela.start_link()   # idempotent: no second warning
        finally:
            quiniela.serial, quiniela._started = saved_serial, saved_started
        self.assertEqual(len(captured.records), 1)
        self.assertIn("pyserial", captured.records[0].getMessage())
        self.assertIsNone(quiniela.link._thread)
        self.assertFalse(quiniela.link.connected)
        link = GatewayLink(on_state=lambda _o: None, serial_module=None)
        link._serial = None
        self.assertFalse(link.start())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
class RouteCase(unittest.TestCase):
    """Swap in a fresh board + link so the module singletons stay untouched."""

    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.wall = FakeClock(1_700_000_000.0)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = BettingBoard(clock=self.clock, wall=self.wall, log_dir=Path(self.tmp.name))
        self.link = GatewayLink(on_state=self.board.apply_state, clock=self.clock)
        self._saved = (quiniela.board, quiniela.link, quiniela.SSE_HEARTBEAT_S)
        quiniela.board, quiniela.link = self.board, self.link
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        quiniela.board, quiniela.link, quiniela.SSE_HEARTBEAT_S = self._saved


class ApiTests(RouteCase):
    def test_fresh_process_reports_link_down_and_board_states(self) -> None:
        resp = self.client.get("/api/quiniela")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        m = resp.get_json()
        self.assertIs(m["link_ok"], False)
        self.assertEqual(m["board_states"], list(config.QUINIELA_BOARD_STATES))
        self.assertEqual(m["race_state"], 0)
        self.assertEqual(m["race_state_name"], "PRE_RACE")
        self.assertEqual(len(m["horses"]), 20)
        self.assertEqual(m["total_tokens"], 0)
        self.assertIsNone(m["leader"])
        self.assertEqual(m["token_value"], float(config.TOKEN_VALUE))

    def test_model_follows_fed_lines(self) -> None:
        self.link.feed_line(state_line(st=2, cups=[cup(0, 7, tok=23), cup(1, 3, tok=2)]))
        m = self.client.get("/api/quiniela").get_json()
        self.assertTrue(m["link_ok"])
        self.assertEqual(m["race_state_name"], "FINAL_CALL")
        self.assertEqual(m["horses"]["7"]["tokens"], 23)
        self.assertEqual(m["leader"], 7)
        self.assertEqual(m["total_tokens"], 25)

    def test_cmd_whitelist(self) -> None:
        def post(payload, raw=False):
            if raw:
                return self.client.post("/api/quiniela/cmd", data=payload,
                                        content_type="application/json")
            return self.client.post("/api/quiniela/cmd", json=payload)

        for bad in (
            {"cmd": ""},
            {"cmd": "   "},
            {"cmd": "reboot"},
            {"cmd": "State 1"},
            {"cmd": "state 1\nreboot"},
            {"cmd": "state 1\rreboot"},
            {"cmd": "state " + "1" * 200},
            {"cmd": 5},
            {"nope": "state 1"},
            ["state 1"],
        ):
            resp = post(bad)
            self.assertEqual(resp.status_code, 400, bad)
            body = resp.get_json()
            self.assertIs(body["ok"], False)
            self.assertTrue(body["error"])
        resp = post("not json at all", raw=True)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/quiniela/cmd")
        self.assertEqual(resp.status_code, 400)

    def test_cmd_without_gateway_is_503(self) -> None:
        for good in ("state 1", "horse 0 7", "scratch 0 1", "demo", "roster", "json 1", "  state 1  "):
            resp = self.client.post("/api/quiniela/cmd", json={"cmd": good})
            self.assertEqual(resp.status_code, 503, good)
            self.assertEqual(resp.get_json(), {"ok": False, "error": "gateway not connected"})

    def test_cmd_is_written_to_the_port(self) -> None:
        fake = FakeSerial()
        self.link._attach_port(fake, "/dev/fake")
        resp = self.client.post("/api/quiniela/cmd", json={"cmd": "  state 1 "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})
        self.assertEqual(fake.written, [b"state 1\n"])
        fake.fail = True
        with self.assertLogs(LOGGER, level="WARNING"):
            resp = self.client.post("/api/quiniela/cmd", json={"cmd": "demo"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": False})
        self.link._detach_port()

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
        self.assertEqual(model["board_states"], list(config.QUINIELA_BOARD_STATES))
        self.assertEqual(self.board.subscriber_count(), 1)

        ping = next(gen)
        self.assertEqual(ping, ": heartbeat\n\nevent: ping\ndata: {\"ts\":%s}\n\n"
                         % json.dumps(round(self.wall.now, 3)))

        self.board.apply_state(state(cups=[cup(0, 7, tok=5)]))
        update = next(gen)
        self.assertTrue(update.startswith("data: "))
        self.assertEqual(json.loads(update[6:])["total_tokens"], 5)

        gen.close()
        self.assertEqual(self.board.subscriber_count(), 0, "unsubscribed on close")

    def test_route_headers_first_event_and_ping(self) -> None:
        quiniela.SSE_HEARTBEAT_S = 0.1
        self.board.apply_state(state(cups=[cup(0, 7, tok=3)]))
        resp = self.client.get("/api/quiniela/stream", buffered=False)
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, "text/event-stream")
            self.assertEqual(resp.headers["Cache-Control"], "no-cache")
            self.assertEqual(resp.headers["X-Accel-Buffering"], "no")
            chunks = iter(resp.response)
            first = next(chunks).decode("utf-8")
            self.assertTrue(first.startswith("data: "))
            self.assertEqual(json.loads(first[6:])["horses"]["7"]["tokens"], 3)
            second = next(chunks).decode("utf-8")
            self.assertIn(": heartbeat\n\n", second)
            self.assertIn("event: ping\ndata: {\"ts\":", second)
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


class ValidateCmdTests(unittest.TestCase):
    def test_validate(self) -> None:
        self.assertEqual(validate_cmd("state 1"), ("state 1", None))
        self.assertEqual(validate_cmd("  horse 0 7 \n"), ("horse 0 7", None))
        for word in ("state", "horse", "scratch", "demo", "roster", "json"):
            self.assertIsNone(validate_cmd(word)[1], word)
        for bad in ("", "   ", None, 3, "help", "debug on", "STATE 1", "state\n1", "x" * 201):
            text, error = validate_cmd(bad)
            self.assertIsNone(text, bad)
            self.assertTrue(error, bad)
        self.assertIsNone(validate_cmd("json " + "1" * 195)[1], "exactly 200 chars is allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)

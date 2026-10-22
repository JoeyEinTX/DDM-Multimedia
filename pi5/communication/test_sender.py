# communication/test_sender.py - the device senders: the LED wall and the tote board never block the race
#
# Run with: python -m communication.test_sender  (from the pi5/ dir)
#
# One queued, coalescing worker per device (communication/sender.py). A send
# is handed to the worker and returns; a device that is down holds at most a
# handful of pending sends; a send that fails is dropped, not retried, except
# that the state keys that failed while the device was down are sent once
# more when it answers again, so the device shows the present. The worker
# probes on its own timer; an ask fails fast while the device is marked down.
# The journal gets one line when a device goes away and one when it is back.
# The LED client is tried against a real socket nothing answers on, the tote
# client against a local HTTP server that hangs. Same tiny runner as the other suites.

import contextlib
import http.server
import io
import os
import socket
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from communication.sender import QUEUE_MAX, Sender, Unreachable  # noqa: E402
from communication.esp32_client import ESP32Client  # noqa: E402
from communication.tote_client import ToteClient  # noqa: E402

_results = []


def _check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((status, name, detail))
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


def wait_for(pred, timeout=3.0, every=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(every)
    return bool(pred())


class FakeClock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def key_of(command):
    """The LED wall's keys, as esp32_client gives them (checked against it below)."""
    from communication.esp32_client import key_of as real
    return real(command)


def is_state(command):
    from communication.esp32_client import is_state as real
    return real(command)


class FakeDevice:
    """A device for the Sender: records every command, answers as told, and can hold the
    worker on a gate (so what is waiting in the queue can be looked at) or be down."""

    def __init__(self):
        self.calls = []
        self.down = False
        self.gate = threading.Event()
        self.gate.set()
        self.answer = lambda command: "OK:" + command

    def exchange(self, command):
        self.gate.wait()
        self.calls.append(command)
        if self.down:
            raise Unreachable("TIMEOUT", "timed out")
        return self.answer(command)


def sender(device, clock=None, log=None, timeout=0.2, probe=None):
    return Sender("LED wall", "[ESP32]", "10.0.0.42:5005", device, timeout, key_of, is_state, probe=probe,
                  clock=clock or FakeClock(), log=(log if log is not None else []).append)


# -----------------------------------------------------------------------------
# The queue: a send returns at once, the newest of each kind replaces the older one waiting
# -----------------------------------------------------------------------------

def test_send_returns_at_once_and_coalesces():
    device = FakeDevice()
    device.gate.clear()                         # the worker holds on the first command
    s = sender(device)
    t0 = time.monotonic()
    s.send("ANIM:WELCOME")
    wait_for(lambda: device.calls == ["ANIM:WELCOME"] or s.health()["queued"] == 0, 1.0)   # the worker took it and is held
    for command in ("ANIM:BETTING_60", "LED:ALL_OFF", "ANIM:FINISH", "CUP:LOCK:7:255:215:0", "CUP:LOCK:3:192:192:192",
                    "CUP:UNLOCK:7", "STATUS", "STATUS", "STATUS", "RESULTS:FINALIZE", "LED:BRIGHTNESS:40"):
        s.send(command)
    took = time.monotonic() - t0
    _check("eleven sends return at once while the worker is held (well under the device's timeout)", took < 0.15, f"{took:.3f} s")
    _check("what waits: the newest ANIM, each cup's newest, STATUS once, in the order of the newest items",
           s.pending() == ["LED:ALL_OFF", "ANIM:FINISH", "CUP:LOCK:3:192:192:192", "CUP:UNLOCK:7", "STATUS", "RESULTS:FINALIZE",
                           "LED:BRIGHTNESS:40"], str(s.pending()))
    _check("health counts them", s.health()["queued"] == 7, str(s.health()))
    device.gate.set()
    _check("released, the worker sends them in that order after the first",
           wait_for(lambda: len(device.calls) == 8, 2.0)
           and device.calls == ["ANIM:WELCOME", "LED:ALL_OFF", "ANIM:FINISH", "CUP:LOCK:3:192:192:192", "CUP:UNLOCK:7", "STATUS",
                                "RESULTS:FINALIZE", "LED:BRIGHTNESS:40"], str(device.calls))
    _check("the queue is empty again, the device online", s.pending() == [] and s.health()["online"] is True and s.health()["queued"] == 0)
    # an older item of a key is dropped and the new one appended, so OFF then B never becomes B then OFF
    device.gate.clear()
    device.calls.clear()
    s.send("STATUS")
    wait_for(lambda: s.health()["queued"] == 0, 1.0)
    s.send("ANIM:A")
    s.send("LED:ALL_OFF")
    s.send("ANIM:B")
    _check("ANIM:A, then LED:ALL_OFF, then ANIM:B: the wall will be told OFF, then B (A is gone)",
           s.pending() == ["LED:ALL_OFF", "ANIM:B"], str(s.pending()))
    device.gate.set()
    wait_for(lambda: len(device.calls) == 3, 2.0)
    _check("...and that is the order sent", device.calls == ["STATUS", "LED:ALL_OFF", "ANIM:B"], str(device.calls))


def test_queue_is_bounded():
    device = FakeDevice()
    device.gate.clear()
    s = sender(device)
    s.send("ANIM:WELCOME")
    wait_for(lambda: s.health()["queued"] == 0, 1.0)
    for i in range(QUEUE_MAX + 40):
        s.send(f"PARAM:SET:k{i}:{i}")            # every one its own key: nothing coalesces
    _check(f"a flood of {QUEUE_MAX + 40} distinct sends with the worker held: at most {QUEUE_MAX} wait, the oldest dropped",
           s.health()["queued"] == QUEUE_MAX and s.pending()[0] == "PARAM:SET:k40:40" and s.pending()[-1] == f"PARAM:SET:k{QUEUE_MAX + 39}:{QUEUE_MAX + 39}",
           f"{s.health()['queued']} {s.pending()[:2]}")
    for i in range(200):
        s.send(f"ANIM:X{i}")
    _check("200 animations more: one waits", sum(1 for c in s.pending() if c.startswith("ANIM:")) == 1 and s.health()["queued"] == QUEUE_MAX,
           str(s.health()))
    device.gate.set()
    wait_for(lambda: s.health()["queued"] == 0, 5.0)
    _check("drained", s.health()["queued"] == 0 and device.calls[-1] == "ANIM:X199", str(device.calls[-3:]))


# -----------------------------------------------------------------------------
# Down and back: one line each, nothing between; the state re-sent on return
# -----------------------------------------------------------------------------

def test_one_line_down_one_line_back():
    device = FakeDevice()
    device.down = True
    clock = FakeClock()
    log = []
    s = sender(device, clock=clock, log=log)
    _check("before any send: online unknown, nothing since", s.health()["online"] is None and s.health()["unreachable_since"] is None)
    for i in range(20):
        s.send(f"ANIM:X{i}")
        s.send("STATUS")
        time.sleep(0.02)
    wait_for(lambda: s.health()["queued"] == 0, 5.0)
    _check("down: one line, with the address, why, and the command it was sending",
           log == ["[ESP32] LED wall unreachable at 10.0.0.42:5005 (timed out, sending ANIM:X0)"], str(log))
    h = s.health()
    _check("health: offline since the clock's time of the first failure", h["online"] is False and h["unreachable_since"] == clock.t, str(h))
    anims = [c for c in device.calls if c.startswith("ANIM:")]
    _check("the failed sends were dropped, not retried: no animation tried twice", len(anims) == len(set(anims)), str(device.calls))
    device.down = False
    s.send("STATUS")
    wait_for(lambda: s.health()["online"] is True, 2.0)
    _check("back: one line saying so", log[-1] == "[ESP32] LED wall reachable again at 10.0.0.42:5005" and len(log) == 2, str(log))
    _check("health: online, nothing since", s.health()["online"] is True and s.health()["unreachable_since"] is None)
    device.down = True
    s.send("PING")
    wait_for(lambda: s.health()["online"] is False, 2.0)
    _check("down again: one more line (three in all)", len(log) == 3 and "sending PING" in log[-1], str(log))


def test_catch_up_on_reconnect():
    device = FakeDevice()
    device.down = True
    s = sender(device)
    for command in ("ANIM:RESULTS_ENTRY", "CUP:LOCK:7:255:215:0", "CUP:LOCK:3:192:192:192", "STATUS", "RESULTS:FINALIZE"):
        s.send(command)
        wait_for(lambda: s.health()["queued"] == 0, 2.0)
    _check("down: every send tried once and failed", device.calls == ["ANIM:RESULTS_ENTRY", "CUP:LOCK:7:255:215:0", "CUP:LOCK:3:192:192:192",
                                                                     "STATUS", "RESULTS:FINALIZE"], str(device.calls))
    device.down = False
    device.calls.clear()
    s.send("STATUS")                            # what the probe does on its own
    wait_for(lambda: len(device.calls) >= 5, 3.0)
    _check("back on a STATUS: the state keys that failed are sent again, in their order; STATUS is not (not state)",
           device.calls == ["STATUS", "ANIM:RESULTS_ENTRY", "CUP:LOCK:7:255:215:0", "CUP:LOCK:3:192:192:192", "RESULTS:FINALIZE"],
           str(device.calls))
    time.sleep(0.2)
    _check("...once: nothing more follows", len(device.calls) == 5, str(device.calls))
    # A newer state of a key that got through is not undone by the older one that failed
    device.down = True
    s.send("ANIM:GATES_BURST")
    wait_for(lambda: s.health()["online"] is False, 2.0)
    device.down = False
    device.calls.clear()
    s.send("ANIM:FINISH")
    wait_for(lambda: len(device.calls) >= 1, 2.0)
    time.sleep(0.2)
    _check("ANIM:GATES_BURST failed, then ANIM:FINISH got through: GATES_BURST is not re-sent after it",
           device.calls == ["ANIM:FINISH"], str(device.calls))
    # A newer send of a key waiting in the queue goes instead of the missed one
    device.down = True
    s.send("ANIM:A")
    wait_for(lambda: s.health()["online"] is False and s.health()["queued"] == 0, 2.0)
    device.down = False
    device.gate.clear()
    device.calls.clear()
    s.send("STATUS")                            # the worker takes it and is held
    wait_for(lambda: s.health()["queued"] == 0, 1.0)
    s.send("ANIM:B")                            # waits behind it
    device.gate.set()
    wait_for(lambda: len(device.calls) >= 2, 2.0)
    time.sleep(0.2)
    _check("ANIM:A failed and ANIM:B is waiting when STATUS gets through: B goes, A is not sent again before it",
           device.calls == ["STATUS", "ANIM:B"], str(device.calls))


def test_ask_waits_for_the_reply():
    device = FakeDevice()
    device.answer = lambda c: "PONG" if c == "PING" else "OK:" + c
    s = sender(device)
    _check("ask answers the device's reply", s.ask("PING") == "PONG")
    device.down = True
    t0 = time.monotonic()
    reply = s.ask("PING")
    _check("the device down: the ask is tried, ERROR:TIMEOUT within the device's timeout, and that marks it down",
           reply == "ERROR:TIMEOUT" and time.monotonic() - t0 < 1.0 and s.health()["online"] is False,
           f"{reply} {time.monotonic() - t0:.2f} s {s.health()}")
    device.calls.clear()
    t0 = time.monotonic()
    reply = s.ask("PING")
    _check("marked down: the next ask answers ERROR:UNREACHABLE at once, nothing queued or tried",
           reply == "ERROR:UNREACHABLE" and time.monotonic() - t0 < 0.05 and s.pending() == [] and device.calls == [],
           f"{reply} {time.monotonic() - t0:.3f} s {device.calls}")
    device.down = False
    s.send("STATUS")                            # what the probe does on its own
    wait_for(lambda: s.health()["online"] is True, 2.0)
    device.gate.clear()
    s.send("ANIM:X")
    wait_for(lambda: s.health()["queued"] == 0, 1.0)
    t0 = time.monotonic()
    reply = s.ask("PING", wait=0.3)
    _check("back, the worker held: an ask that waits longer than its limit gives up with ERROR:TIMEOUT, the send still queued",
           reply == "ERROR:TIMEOUT" and 0.25 < time.monotonic() - t0 < 1.0 and "PING" in s.pending(), f"{reply} {s.pending()}")
    device.gate.set()
    wait_for(lambda: "PING" in device.calls, 2.0)
    _check("...and goes out once the worker gets to it", "PING" in device.calls)


def test_the_worker_probes_on_its_own():
    device = FakeDevice()
    log = []
    saved, Sender.probe_every = Sender.probe_every, 0.2       # before the worker starts: it reads it at every wait
    try:
        s = sender(device, log=log, probe="STATUS")
        _check("idle, the worker asks after the device on its own: STATUS every probe_every",
               wait_for(lambda: device.calls.count("STATUS") >= 3, 3.0) and set(device.calls) == {"STATUS"}, str(device.calls))
        _check("...and its health is known with nothing sent", s.health()["online"] is True, str(s.health()))
        device.down = True
        _check("the device going away is noticed by the probe", wait_for(lambda: s.health()["online"] is False, 2.0), str(s.health()))
        device.down = False
        _check("...and its return, one line each", wait_for(lambda: s.health()["online"] is True, 2.0)
               and log == ["[ESP32] LED wall unreachable at 10.0.0.42:5005 (timed out, sending STATUS)",
                           "[ESP32] LED wall reachable again at 10.0.0.42:5005"], str(log))
        device.calls.clear()
        for i in range(8):
            s.send(f"ANIM:X{i}")
            time.sleep(0.05)
        _check("sends every 0.05 s (the probe at 0.2 s idle): no probe between them", "STATUS" not in device.calls, str(device.calls))
    finally:
        Sender.probe_every = saved


# -----------------------------------------------------------------------------
# The LED client: a real socket nothing answers on
# -----------------------------------------------------------------------------

class DeadWall:
    """A TCP listener: accepts, and either never answers (the wall off: pi5's connect
    succeeds in the backlog, the reply never comes) or answers like the controller."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.answering = False
        self.received = []
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn):
        with conn:
            conn.settimeout(2.0)
            try:
                line = conn.recv(1024).decode().strip()
            except OSError:
                return
            if not self.answering:
                time.sleep(1.0)                 # longer than the client's timeout: nothing comes back
                return
            self.received.append(line)
            if line == "STATUS":
                conn.sendall(b"STATUS:120:300:80\n")
            elif line == "PING":
                conn.sendall(b"PONG\n")
            else:
                conn.sendall(("OK:" + line + "\n").encode())

    def close(self):
        self._stop = True
        self.sock.close()


def capture(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args)
    return result, [ln for ln in out.getvalue().splitlines() if ln.strip()]


def test_led_client_against_a_socket_nothing_answers_on():
    wall = DeadWall()
    try:
        client = ESP32Client("127.0.0.1", wall.port, timeout=0.3)
        t0 = time.monotonic()
        sent = client.send("ANIM:FINISH")
        took = time.monotonic() - t0
        _check("a state send returns at once with the command, the wall not answering", sent == "ANIM:FINISH" and took < 0.05, f"{took:.3f} s")
        _check("the health is not known yet", client.health()["online"] is None and client.power is None)
        _check("...and known within the timeout: unreachable", wait_for(lambda: client.health()["online"] is False, 1.5), str(client.health()))
        wall.answering = True
        wall.received.clear()
        t0 = time.monotonic()
        _check("marked down, an ask (ping) says no at once, and nothing is tried",
               client.ping() is False and time.monotonic() - t0 < 0.05 and wall.received == [], str(wall.received))
        client.ip = "127.0.0.1"                 # the Control Center's IP field: given an address (here the same), what was known no longer holds
        _check("given an address again: health unknown, and the next ask tries it: PONG",
               client.health()["online"] is None and client.ping() is True, str(client.health()))
        _check("...and after it the missed ANIM:FINISH goes out again", wait_for(lambda: wall.received == ["PING", "ANIM:FINISH"], 2.0),
               str(wall.received))
        wall.received.clear()
        client.send("STATUS")
        _check("a STATUS answer: online, the power figures parsed",
               wait_for(lambda: client.power == {"current_ma": 120, "peak_ma": 300, "min_ma": 80}, 2.0)
               and client.health()["online"] is True and wall.received == ["STATUS"], str(client.health()))
        h = client.health()
        _check("health carries the address, enabled, the power", h["address"] == f"127.0.0.1:{wall.port}" and h["enabled"] is True
               and h["power"]["peak_ma"] == 300, str(h))
    finally:
        wall.close()


def test_led_client_without_an_address():
    opened = []
    real = socket.socket

    def refuse(*a, **k):
        opened.append(a)
        raise AssertionError("a socket was opened")

    socket.socket = refuse
    try:
        client = ESP32Client(ip="")
        (sent, lines) = capture(client.send, "ANIM:BETTING_60")
        (_, lines2) = capture(client.send, "STATUS")
        (reply, lines3) = capture(client.ask, "PING")
        time.sleep(0.1)
    finally:
        socket.socket = real
    _check("no LED address: a send is dropped (and returns what was asked), an ask answers ERROR:NO_LED_CONTROLLER at once, "
           "no socket opened, nothing logged",
           sent == "ANIM:BETTING_60" and client.power is None and reply == "ERROR:NO_LED_CONTROLLER" and opened == []
           and lines == lines2 == lines3 == [], f"{reply} {lines}")
    h = client.health()
    _check("health: not enabled, online unknown, no probe", h["enabled"] is False and h["online"] is None and h["queued"] == 0
           and client.sender.probe is None, str(h))


# -----------------------------------------------------------------------------
# The tote client: a local HTTP server that hangs
# -----------------------------------------------------------------------------

class FakeTote(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeToteHandler)
        self.answering = False
        self.seen = []                          # every request, answered or not
        self.received = []                      # the answered ones
        threading.Thread(target=self.serve_forever, daemon=True).start()


class FakeToteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append(self.path)
        if not self.server.answering:
            time.sleep(1.0)
            return
        self.server.received.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_tote_client_never_blocks():
    tote = FakeTote()
    try:
        port = tote.server_address[1]
        client = ToteClient("127.0.0.1", port, timeout=0.3)
        t0 = time.monotonic()
        (_, lines) = capture(client.betting_open, 60)
        took = time.monotonic() - t0
        _check("a tote send returns at once, the board not answering", took < 0.05 and lines == [], f"{took:.3f} s {lines}")
        _check("within the timeout the health says unreachable", wait_for(lambda: client.health()["online"] is False, 1.5), str(client.health()))
        client.welcome()
        client.final_call()
        wait_for(lambda: "/tote?mode=finalcall" in tote.seen, 2.0)
        time.sleep(0.4)                         # ...and the worker's try of it has timed out
        _check("the board down: sends are dropped (the newest tote state replaces an older one waiting), nothing blocks",
               client.health()["queued"] == 0 and client.health()["online"] is False)
        _check("marked down, ping answers no at once", client.ping() is False)
        tote.answering = True
        tote.received.clear()
        client.sender.send("/status")           # what the probe does on its own
        _check("the board back: the probe answers, the health says online, and the current figures (the last tote state) go out again",
               wait_for(lambda: client.health()["online"] is True, 2.0)
               and wait_for(lambda: tote.received == ["/status", "/tote?mode=finalcall"], 2.0), str(tote.received))
        time.sleep(0.2)
        _check("...once", tote.received == ["/status", "/tote?mode=finalcall"], str(tote.received))
    finally:
        tote.shutdown()
        tote.server_close()


def test_tote_journal_lines():
    tote = FakeTote()
    try:
        port = tote.server_address[1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            client = ToteClient("127.0.0.1", port, timeout=0.3)
            for _ in range(5):
                client.welcome()
                client.race_start()
                time.sleep(0.05)
            wait_for(lambda: client.health()["online"] is False and client.health()["queued"] == 0, 3.0)
            time.sleep(0.5)
            tote.answering = True
            client.sender.send("/status")
            wait_for(lambda: client.health()["online"] is True, 2.0)
            time.sleep(0.3)
        lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("[TOTE] Tote board")]
        _check("one line down (with the address, why, the send: welcome, or the race start that replaced it), one line back, nothing between",
               len(lines) == 2 and lines[0] in [f"[TOTE] Tote board unreachable at 127.0.0.1:{port} (timed out, sending /tote?mode={m})"
                                                for m in ("welcome", "racestart")]
               and lines[1] == f"[TOTE] Tote board reachable again at 127.0.0.1:{port}", str(lines))
        _check("the probe's answer prints no line; a display call's does",
               "[TOTE] GET /status" not in out.getvalue() and "[TOTE] GET /tote?mode=racestart | Status: 200" in out.getvalue(),
               out.getvalue()[-300:])
    finally:
        tote.shutdown()
        tote.server_close()


def test_keys_and_state():
    from communication.esp32_client import key_of as led_key, is_state as led_state
    cases = {"ANIM:WELCOME": "ANIM", "ANIM:RESULTS_ACTIVE:7:3:12": "ANIM", "LED:ALL_OFF": "LED", "LED:ALL_ON": "LED",
             "LED:COLOR:FFD700": "LED", "RESET": "LED", "LED:BRIGHTNESS:40": "LED:BRIGHTNESS", "LED:CUP:7:FFD700": "CUP:7",
             "CUP:LOCK:7:255:215:0": "CUP:7", "CUP:UNLOCK:7": "CUP:7", "CUP:UNLOCK:ALL": "CUP:ALL", "RESULTS:FINALIZE": "RESULTS",
             "STATUS": "STATUS", "PING": "PING", "PARAM:SET:k:1": "PARAM:SET:k:1", "PARAM:GET:ALL": "PARAM:GET:ALL",
             "SOMETHING:ELSE": "SOMETHING:ELSE"}
    got = {c: led_key(c) for c in cases}
    _check("the LED wall's keys: one per animation, one per cup, STATUS and PING once, the rest their own", got == cases, str(got))
    state = {c: led_state(c) for c in cases}
    _check("what is state (re-sent on return): everything but STATUS, PING and PARAM",
           [c for c, v in state.items() if not v] == ["STATUS", "PING", "PARAM:SET:k:1", "PARAM:GET:ALL"], str(state))


def main():
    print("Device senders test")
    _run("a send returns at once; the queue coalesces, in the taps' order", test_send_returns_at_once_and_coalesces)
    _run("the queue is bounded", test_queue_is_bounded)
    _run("one journal line down, one back", test_one_line_down_one_line_back)
    _run("the state is sent again when the device returns, never over a newer one", test_catch_up_on_reconnect)
    _run("ask waits for the reply, within a limit, and fails fast while the device is down", test_ask_waits_for_the_reply)
    _run("the worker probes on its own timer", test_the_worker_probes_on_its_own)
    _run("the LED client against a socket nothing answers on", test_led_client_against_a_socket_nothing_answers_on)
    _run("the LED client without an address", test_led_client_without_an_address)
    _run("the tote client never blocks; the figures go out again on return", test_tote_client_never_blocks)
    _run("the tote's journal lines", test_tote_journal_lines)
    _run("the LED wall's keys and what is state", test_keys_and_state)
    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 50)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# communication/sender.py - One queued, coalescing worker per device
#
# The LED wall and the tote board are decoration; a race-state change is a
# fact about the race and must never wait on either (Joey, DevPi, 2026-10-20:
# with the wall off every LED-bound request waited 5 s on pi5, and the Control
# Center issues them all the time, the STATUS poll, a PING per action, each
# tap's animation, so the browser's few connections per host queued the race
# state behind them: a minute's lag, FINISH stuck). So a request hands its
# send to the device's worker and returns, and nothing a request does waits
# on the device: health is what the worker last learned, and an ask while the
# device is marked down answers at once.
#
# The queue coalesces: the newest command of each key (key_of) replaces the
# older one still waiting (dropped, the new one appended), so the newest items
# go in the order they were asked for (ANIM:A, LED:ALL_OFF, ANIM:B leaves OFF
# then B, never B then OFF). A device that is down therefore holds at most a
# handful of sends, and the queue is capped at QUEUE_MAX anyway, the oldest
# dropped. A send that fails is dropped, not retried. The device catches up
# when it returns: on a success, every state key (is_state) whose last send
# failed goes out again, ahead of what waits, in the order the keys last
# failed, unless a newer send of that key already got through or is waiting.
#
# The probe is the worker's own timer: left idle for probe_every seconds it
# sends `probe` (STATUS to the wall, /status to the tote), so a device's
# health stays fresh and one that is back is noticed within probe_every and
# its timeout, with nothing asking for it.
#
# ask() is for the few callers that need the device's reply (PING, PARAM): it
# waits up to `wait` seconds, ERROR:TIMEOUT when the reply has not come (the
# command still goes out when the worker reaches it), ERROR:<code> when the
# device did not answer, and ERROR:UNREACHABLE at once, nothing queued, while
# the device is marked down.
#
# Health: online is None until the first exchange (and again when the device
# is given another address), then True or False, with unreachable_since (the
# clock's time of the first failure after a success). The journal gets one
# line when the device goes away, with the address, the reason and the
# command, one when it answers again, and none between.

import collections
import os
import threading
import time
from typing import Any, Dict, List, Optional

QUEUE_MAX = 64


def logs_probes() -> bool:
    """Whether a probe's answer prints its line (every other exchange prints
    one): no, unless DDM_ACCESS_LOG is 1/true/yes/on, the switch that brings
    werkzeug's request lines back too."""
    return os.environ.get("DDM_ACCESS_LOG", "").strip().lower() in ("1", "true", "yes", "on")


class Unreachable(Exception):
    """Raised by an exchange when the device did not answer: `code` is the
    ERROR:<code> an ask gets back, `reason` the words for the journal line."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _Item:
    __slots__ = ("command", "key", "reply", "done")

    def __init__(self, command: Any, key: Any, wants_reply: bool) -> None:
        self.command = command
        self.key = key
        self.reply: Optional[str] = None
        self.done = threading.Event() if wants_reply else None


class Sender:
    probe_every = 5.0                       # seconds idle before the worker probes

    def __init__(self, name, tag, address, device, timeout, key_of, is_state, probe=None, clock=time.time, log=print):
        self.name = name
        self.tag = tag
        self.timeout = timeout
        self.probe = probe
        self._address = address
        self._device = device               # what has exchange(command): the client, or a test's stand-in
        self._key_of = key_of
        self._is_state = is_state
        self._clock = clock
        self._log = log
        self._lock = threading.Condition()
        self._queue: "collections.deque[_Item]" = collections.deque()
        self._online: Optional[bool] = None
        self._since: Optional[float] = None
        self._missed: Dict[Any, Any] = {}   # state keys whose last send failed, in the order they last did
        threading.Thread(target=self._work, name=f"{name} sender", daemon=True).start()

    @property
    def address(self) -> str:
        return self._address

    @address.setter
    def address(self, value: str) -> None:
        """The device given another address (the Control Center's IP field):
        what was known of it no longer holds."""
        with self._lock:
            self._address = value
            self._online, self._since = None, None

    def send(self, command: Any) -> None:
        """Queue a command and return. Coalesced by its key."""
        self._put(command, self._key_of(command), wants_reply=False)

    def ask(self, command: Any, wait: Optional[float] = None) -> str:
        """Queue a command and wait for its reply, up to `wait` seconds (the
        device's timeout and a second by default)."""
        with self._lock:
            down = self._online is False
        if down:
            return "ERROR:UNREACHABLE"
        item = self._put(command, None, wants_reply=True)
        if not item.done.wait(self.timeout + 1.0 if wait is None else wait):
            return "ERROR:TIMEOUT"
        return item.reply

    def _put(self, command: Any, key: Any, wants_reply: bool) -> _Item:
        item = _Item(command, key, wants_reply)
        with self._lock:
            if key is not None:
                for waiting in [w for w in self._queue if w.key == key]:
                    self._queue.remove(waiting)
            self._queue.append(item)
            while len(self._queue) > QUEUE_MAX:
                dropped = self._queue.popleft()
                if dropped.done is not None:
                    dropped.reply = "ERROR:DROPPED"
                    dropped.done.set()
            self._lock.notify()
        return item

    def pending(self) -> List[Any]:
        """The commands waiting, in the order they will go."""
        with self._lock:
            return [item.command for item in self._queue]

    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {"online": self._online, "unreachable_since": self._since, "address": self._address,
                    "queued": len(self._queue)}

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._queue:
                    if not self._lock.wait(self.probe_every) and self.probe is not None:
                        self._queue.append(_Item(self.probe, self._key_of(self.probe), False))
                item = self._queue.popleft()
            try:
                reply = self._device.exchange(item.command)
            except Unreachable as exc:
                self._failed(item, exc)
                continue
            except Exception as exc:           # a fault in the exchange itself: the device reads as unreachable, with why
                self._failed(item, Unreachable("EXCEPTION:" + str(exc), str(exc)))
                continue
            self._answered(item, reply)

    def _failed(self, item: _Item, exc: Unreachable) -> None:
        with self._lock:
            if self._is_state(item.command):
                key = item.key if item.key is not None else self._key_of(item.command)
                self._missed.pop(key, None)
                self._missed[key] = item.command
            went_down = self._online is not False
            self._online = False
            if went_down:
                self._since = self._clock()
        if item.done is not None:
            item.reply = "ERROR:" + exc.code
            item.done.set()
        if went_down:
            self._log(f"{self.tag} {self.name} unreachable at {self._address} ({exc.reason}, sending {item.command})")

    def _answered(self, item: _Item, reply: str) -> None:
        with self._lock:
            key = item.key if item.key is not None else self._key_of(item.command)
            self._missed.pop(key, None)                 # a newer send of this key got through
            came_back = self._online is False
            self._online, self._since = True, None
            waiting = {w.key for w in self._queue}      # a newer send of a key waiting goes instead of the missed one
            for key, command in reversed(self._missed.items()):
                if key not in waiting:
                    self._queue.appendleft(_Item(command, key, False))
            self._missed.clear()
        if item.done is not None:
            item.reply = reply
            item.done.set()
        if came_back:
            self._log(f"{self.tag} {self.name} reachable again at {self._address}")

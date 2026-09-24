"""
La Quiniela live board — the splash's HTTP client of pi5, and the SSE fan-out
to the TV.

The cup gateway (firmware/quiniela/ddm_gateway) plugs into DevPi, where pi5's
bridge (pi5/la_quiniela/bridge.py) owns the USB port, keeps the betting model
and serves it as GET /api/quiniela, GET /api/quiniela/stream (SSE) and
POST /api/quiniela/cmd. This module mirrors that model so the board on the
TV keeps talking to its own origin:

  * Pi5Link     — a daemon thread that follows pi5's SSE stream and, while
                  the stream is down, polls GET /api/quiniela once a second;
                  reconnects forever with backoff. forward_cmd() relays a
                  POST /api/quiniela/cmd body to pi5 and returns its answer.
  * BoardRelay  — holds the last model received from pi5, flips its link_ok
                  to false when pi5 has not been heard for LINK_TIMEOUT_S,
                  and fans every change out to SSE subscribers.
  * sse_events() — the generator behind the splash's GET /api/quiniela/stream
                   (byte-identical to pi5's).

Nothing here is required for the slideshow: with pi5 unreachable the link
keeps retrying, the model reports link_ok=false with an empty board_states,
and the board stays hidden.

Race states (DdmRaceState in firmware/quiniela/ddm_common.h):
    0 PRE_RACE, 1 BETTING_OPEN, 2 FINAL_CALL, 3 AT_THE_POST, 4 RUNNING,
    5 WINNER, 6 AFTER_PARTY.
"""

from __future__ import annotations

import http.client
import json
import logging
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import config

log = logging.getLogger("splash_display.quiniela")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# link_ok window: pi5 heard (a model or a ping) within this. pi5 pings after
# every 5 s of silence, measured from its previous chunk, so consecutive
# contacts arrive >= 5 s apart; a window of exactly 5 s let a 1 Hz tick land
# in the few ms between "5 s since the last ping" and the next one and flip
# link_ok off and back on. 7.5 s clears one ping period with margin and is
# still under the page's own 10 s NO LINK rule (quiniela_board.js STALE_MS).
LINK_TIMEOUT_S = 7.5
POLL_INTERVAL_S = 1.0             # GET /api/quiniela cadence while the stream is down
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 10.0
STREAM_RETRY_S = 10.0             # how long the poll fallback runs before the stream is tried again
STREAM_READ_TIMEOUT_S = 15.0      # pi5 pings every 5 s; a stream silent for 15 s is dead
FETCH_TIMEOUT_S = 5.0             # one GET / POST to pi5

HORSE_COUNT = 20

SSE_HEARTBEAT_S = 5.0
SSE_QUEUE_SIZE = 32

RACE_STATE_NAMES = {
    0: "PRE_RACE",
    1: "BETTING_OPEN",
    2: "FINAL_CALL",
    3: "AT_THE_POST",
    4: "RUNNING",
    5: "WINNER",
    6: "AFTER_PARTY",
}

# The exceptions one HTTP exchange with pi5 can raise. HTTPError is a URLError
# and URLError is an OSError; socket.timeout is TimeoutError; listed anyway
# so the intent reads at a glance.
_NET_ERRORS: Tuple[type, ...] = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
    OSError,
    socket.timeout,
    http.client.HTTPException,
)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def race_state_name(state: int) -> str:
    return RACE_STATE_NAMES.get(state, f"STATE_{state}")


def _unassigned() -> Dict[str, Any]:
    return {"tokens": 0, "share": 0.0, "scratched": False, "online": False, "cup": None}


def _reason(exc: BaseException) -> str:
    """A one-line reason for a failed exchange: URLError carries the socket
    error in .reason, HTTPError its status; everything else is str()."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    reason = getattr(exc, "reason", None)
    text = str(reason) if reason is not None else str(exc)
    return text or type(exc).__name__


# ---------------------------------------------------------------------------
# The relayed model
# ---------------------------------------------------------------------------
class BoardRelay:
    """The last model received from pi5, served to the TV.

    apply_model() is called with every model pi5 sends (stream or poll);
    touch() with every ping; tick() about once a second so pi5 going quiet
    is noticed. The served link_ok is pi5's own link_ok AND pi5 heard within
    LINK_TIMEOUT_S; the rest of the model is pi5's, untouched. Every change
    is published (as compact JSON) to every subscriber queue.

    clock is a monotonic seconds source (contact timing); wall is unix time
    (the "updated" stamp of a link_ok flip). Both are injectable for tests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._subs: List["queue.Queue[str]"] = []
        self._heard_at: Optional[float] = None
        self._pi5_link_ok = False          # link_ok as pi5 last reported it
        self._model: Dict[str, Any] = self.empty_model()
        self._json: str = _dumps(self._model)

    # -- model ---------------------------------------------------------------
    def empty_model(self) -> Dict[str, Any]:
        """What is served until pi5 has been heard: link down, nothing bet,
        and no board_states, so the board stays hidden (pi5 decides the
        takeover states)."""
        return {
            "link_ok": False,
            "race_state": 0,
            "race_state_name": race_state_name(0),
            "token_value": 1.0,
            "pot": 0.0,
            "total_tokens": 0,
            "horses": {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)},
            "leader": None,
            "events": [],
            "updated": self._wall(),
            "board_states": [],
        }

    def model(self) -> Dict[str, Any]:
        """A fresh copy of the current model (safe to mutate)."""
        with self._lock:
            text = self._json
        return json.loads(text)

    def model_json(self) -> str:
        """The current model as the compact JSON the SSE stream sends."""
        with self._lock:
            return self._json

    def pi5_ok(self) -> bool:
        """pi5 heard (a model or a ping) within LINK_TIMEOUT_S."""
        with self._lock:
            return self._heard_locked()

    def _heard_locked(self) -> bool:
        return (
            self._heard_at is not None
            and (self._clock() - self._heard_at) <= LINK_TIMEOUT_S
        )

    def apply_model(self, m: Any) -> bool:
        """Serve one model from pi5. Returns True if it differed from what was
        served (and was published). Anything that is not a dict with a
        "horses" dict is ignored."""
        if not isinstance(m, dict) or not isinstance(m.get("horses"), dict):
            return False
        served = dict(m)
        served["link_ok"] = bool(m.get("link_ok"))   # just received: pi5 is fresh by definition
        try:
            text = _dumps(served)
        except (TypeError, ValueError) as exc:
            log.debug("pi5 model not serializable: %s", exc)
            return False
        with self._lock:
            self._heard_at = self._clock()
            self._pi5_link_ok = served["link_ok"]
            # Compare as dicts, not as text: the stream carries pi5's key
            # order and the poll fallback (jsonify) sorted keys, and the
            # same model in another order is not a change worth publishing.
            if served == self._model:
                return False
            self._set_locked(served, text)
            return True

    def touch(self) -> None:
        """Record contact without a model (a ping). If a tick had flipped
        link_ok off between two pings, this flips it back."""
        with self._lock:
            self._heard_at = self._clock()
            self._reconcile_locked()

    def tick(self) -> bool:
        """Re-evaluate the served link_ok (pi5 heard within LINK_TIMEOUT_S
        AND pi5's own link_ok); publish if it flipped. Returns True if it did."""
        with self._lock:
            return self._reconcile_locked()

    def _reconcile_locked(self) -> bool:
        want = self._heard_locked() and self._pi5_link_ok
        if want == self._model["link_ok"]:
            return False
        model = dict(self._model)
        model["link_ok"] = want
        model["updated"] = self._wall()
        self._set_locked(model, _dumps(model))
        return True

    def _set_locked(self, model: Dict[str, Any], text: str) -> None:
        self._model = model
        self._json = text
        for q in self._subs:
            _offer(q, text)

    # -- subscribers ---------------------------------------------------------
    def subscribe(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=SSE_QUEUE_SIZE)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


def _offer(q: "queue.Queue[str]", item: str) -> None:
    """Non-blocking put; when the subscriber is not keeping up, drop its
    oldest item (every item is a full model, so the newest is all it needs)."""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


# ---------------------------------------------------------------------------
# The pi5 link
# ---------------------------------------------------------------------------
def _default_opener(request: Any, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


class Pi5Link:
    """Follows pi5's betting model on a daemon thread.

    Stream first: GET base_url/api/quiniela/stream and feed every SSE event
    to on_model (a JSON dict in an unnamed or "message" event) / on_alive
    (that, or a "ping"). When the stream ends or fails, poll
    GET base_url/api/quiniela once per POLL_INTERVAL_S for STREAM_RETRY_S
    (a failed poll sleeps a backoff of 1 s doubling to 10 s), then try the
    stream again. Forever; nothing pi5 does or does not do ends the thread.

    opener(request, timeout) must return a response usable as a context
    manager that iterates lines (the stream) and has .read() (the rest);
    sleeper(seconds) must return early when stop() was called. Both default
    to urllib.request.urlopen and the stop Event's wait, and exist so tests
    can drive the loop without sockets or real time.
    """

    def __init__(
        self,
        on_model: Callable[[Dict[str, Any]], Any],
        on_alive: Callable[[], Any],
        base_url: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        opener: Optional[Callable[[Any, float], Any]] = None,
        sleeper: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self._on_model = on_model
        self._on_alive = on_alive
        self.base_url = (base_url if base_url is not None else config.PI5_URL).rstrip("/")
        self._clock = clock
        self._opener = opener if opener is not None else _default_opener
        self._sleeper = sleeper if sleeper is not None else self._default_sleeper
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._heard_at: Optional[float] = None
        self._resp: Any = None               # the open stream, closed by stop()
        self._backoff = BACKOFF_MIN_S
        self._stream_up_logged = False
        self._stream_lost_warned = False
        self._poll_failing = False

    # -- public --------------------------------------------------------------
    @property
    def connected(self) -> bool:
        """pi5 heard (a model or a ping) within LINK_TIMEOUT_S."""
        with self._lock:
            at = self._heard_at
        return at is not None and (self._clock() - at) <= LINK_TIMEOUT_S

    def start(self) -> bool:
        """Start the link thread (idempotent)."""
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name="quiniela-link", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """End the loop (idempotent) without blocking the caller.

        The socket under an open stream is shut down, which wakes a reader
        blocked in recv() on Linux (the splash Pi) at once; on Windows
        shutdown() does not interrupt a blocked recv, so there the thread
        ends at the next byte from pi5 (a ping within 5 s) or at
        STREAM_READ_TIMEOUT_S. The response itself is closed by the link
        thread's own ``with``: closing it from here would wait on the
        reader's buffer lock for as long as the blocked read lasts."""
        self._stop.set()
        with self._lock:
            resp = self._resp
        if resp is not None:
            _shutdown_socket(resp)

    def feed_sse(self, lines: Iterable[Any]) -> None:
        """Parse SSE framing from an iterable of lines (bytes or str, with or
        without the newline) until it ends or stop() is called.

        A blank line dispatches the event: an unnamed or "message" event
        whose data is a JSON dict -> on_model(dict) and on_alive(); "ping"
        -> on_alive(); anything else is ignored at DEBUG. Several data lines
        join with a newline; ": comment" lines and unknown fields are skipped.
        """
        event: Optional[str] = None
        data: List[str] = []
        for raw in lines:
            if self._stop.is_set():
                return
            line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            line = line.rstrip("\r\n")
            if line == "":
                self._dispatch(event, data)
                event, data = None, []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
            elif field == "event":
                event = value
            # id / retry / anything else: not used

    def _dispatch(self, event: Optional[str], data: List[str]) -> None:
        if not data:
            return
        if event == "ping":
            self._mark_heard()
            self._call(self._on_alive)
            return
        if event not in (None, "", "message"):
            log.debug("pi5 stream: ignored %r event", event)
            return
        payload = "\n".join(data)
        try:
            obj = json.loads(payload)
        except ValueError:
            log.debug("pi5 stream: unparseable JSON: %s", payload[:160])
            return
        if not isinstance(obj, dict):
            log.debug("pi5 stream: ignored non-object payload")
            return
        self._mark_heard()
        self._call(self._on_alive)
        self._call(self._on_model, obj)

    def forward_cmd(self, body: Any, timeout: float = FETCH_TIMEOUT_S) -> Tuple[int, Dict[str, Any]]:
        """POST body (as received; {} when it is not a dict) to pi5's
        /api/quiniela/cmd and return (status, payload). pi5 validates the
        command; a JSON-object answer comes back unchanged, status included.
        pi5 unreachable -> (503, {"ok": false, "error": "pi5 not reachable: ..."});
        a non-JSON body -> {"ok": false, "error": "non-JSON reply ..."} with
        pi5's status, or 502 when that status was a 2xx (see _json_reply)."""
        payload = body if isinstance(body, dict) else {}
        req = urllib.request.Request(
            self.base_url + "/api/quiniela/cmd",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self._opener(req, timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            finally:
                exc.close()
            return _json_reply(raw, exc.code)
        except _NET_ERRORS as exc:
            return 503, {"ok": False, "error": f"pi5 not reachable: {_reason(exc)}"}
        return _json_reply(raw, status)

    # -- thread --------------------------------------------------------------
    def _default_sleeper(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _mark_heard(self) -> None:
        with self._lock:
            self._heard_at = self._clock()

    @staticmethod
    def _call(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — a relay bug must not kill the link
            log.exception("pi5 model handler failed")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._stream()
                if self._stop.is_set():
                    break
                if not self._stream_lost_warned:
                    self._stream_lost_warned = True
                    log.warning("pi5 stream lost (%s); polling /api/quiniela", reason)
                else:
                    log.debug("pi5 stream still down (%s); polling /api/quiniela", reason)
                self._poll_for(STREAM_RETRY_S)
            except Exception:  # noqa: BLE001 — the link thread never dies
                log.exception("pi5 link loop failed; retrying in %.0fs", BACKOFF_MIN_S)
                self._sleeper(BACKOFF_MIN_S)

    def _stream(self) -> str:
        """Follow the stream until it ends or fails. Returns the reason."""
        url = self.base_url + "/api/quiniela/stream"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with self._opener(req, STREAM_READ_TIMEOUT_S) as resp:
                with self._lock:
                    self._resp = resp
                self._backoff = BACKOFF_MIN_S
                self._poll_failing = False
                if not self._stream_up_logged:
                    self._stream_up_logged = True
                    log.info("pi5 link up (stream) %s", url)
                self._stream_lost_warned = False
                self.feed_sse(resp)
            reason = "stream ended"
        except _NET_ERRORS as exc:
            reason = _reason(exc)
        finally:
            with self._lock:
                self._resp = None
            self._stream_up_logged = False
        return reason

    def _poll_for(self, seconds: float) -> None:
        deadline = self._clock() + seconds
        while not self._stop.is_set() and self._clock() < deadline:
            if self._poll_once():
                self._backoff = BACKOFF_MIN_S
                self._sleeper(POLL_INTERVAL_S)
            else:
                self._sleeper(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

    def _poll_once(self) -> bool:
        url = self.base_url + "/api/quiniela"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with self._opener(req, FETCH_TIMEOUT_S) as resp:
                raw = resp.read()
            obj = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except _NET_ERRORS as exc:
            if not self._poll_failing:
                self._poll_failing = True
                log.warning("pi5 poll failed (%s); retrying with backoff", _reason(exc))
            else:
                log.debug("pi5 poll failed again (%s)", _reason(exc))
            return False
        except ValueError as exc:
            log.debug("pi5 poll: invalid JSON: %s", exc)
            return False
        if not isinstance(obj, dict):
            log.debug("pi5 poll: ignored non-object payload")
            return False
        if self._poll_failing:
            self._poll_failing = False
            log.info("pi5 link up (poll) %s", url)
        self._mark_heard()
        self._call(self._on_alive)
        self._call(self._on_model, obj)
        return True


def _json_reply(raw: Any, status: int) -> Tuple[int, Dict[str, Any]]:
    """(status, body) to relay: pi5's reply as received when it is a JSON
    object; otherwise a JSON error naming what it was. A non-JSON error page
    (Flask's HTML 404/405/500) keeps its status; a non-JSON 2xx becomes 502,
    because "ok" is the contract and a 200 must never say ok:false."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        obj = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        obj = None
    if isinstance(obj, dict):
        return status, obj
    error = {"ok": False, "error": f"non-JSON reply from pi5 (HTTP {status})"}
    return (502 if 200 <= status < 300 else status), error


def _shutdown_socket(resp: Any) -> None:
    """Shut down the socket under an http.client response (urllib keeps it
    at resp.fp.raw._sock) so a thread blocked reading it wakes up. A
    response without one (a test fake) or a socket already gone is left
    alone."""
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------
def sse_events(
    target: Optional[BoardRelay] = None,
    heartbeat_s: Optional[float] = None,
    wall: Callable[[], float] = time.time,
) -> Iterator[str]:
    """Generator behind GET /api/quiniela/stream.

    First yields the current model as a ``data:`` event, then every published
    model as it arrives. After heartbeat_s (default SSE_HEARTBEAT_S) of
    silence it yields a ``: heartbeat`` comment plus an ``event: ping`` (an
    EventSource cannot see comments, so the ping is what the page watches).
    """
    b = target if target is not None else board
    period = heartbeat_s if heartbeat_s is not None else SSE_HEARTBEAT_S
    q = b.subscribe()
    try:
        yield "data: " + b.model_json() + "\n\n"
        while True:
            try:
                payload = q.get(timeout=period)
            except queue.Empty:
                yield ": heartbeat\n\nevent: ping\ndata: " + _dumps({"ts": round(wall(), 3)}) + "\n\n"
                continue
            yield "data: " + payload + "\n\n"
    finally:
        b.unsubscribe(q)


# ---------------------------------------------------------------------------
# Module singletons and start-up
# ---------------------------------------------------------------------------
board = BoardRelay()
link = Pi5Link(on_model=board.apply_model, on_alive=board.touch)

_started = False
_stop = threading.Event()      # ends the ticker; not tied to the link


def _tick_loop() -> None:
    """Once a second, let the relay notice pi5 going silent."""
    while not _stop.wait(1.0):
        try:
            board.tick()
        except Exception:  # noqa: BLE001
            log.exception("board tick failed")


def start_link() -> None:
    """Start the pi5 link and the link_ok ticker. Idempotent: tests and
    tools pre-set _started = True before importing server to keep both
    threads out of their process."""
    global _started
    if _started:
        return
    _started = True
    link.start()
    threading.Thread(target=_tick_loop, name="quiniela-tick", daemon=True).start()
    log.info("quiniela pi5 link started (url=%s)", link.base_url)

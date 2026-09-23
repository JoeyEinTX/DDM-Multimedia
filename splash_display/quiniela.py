"""
La Quiniela live board — gateway serial link, betting model, SSE fan-out.

The cup gateway (firmware/quiniela/ddm_gateway) plugs into this Pi's USB port
and, once told ``json 1``, prints one ``{"t":"state",...}`` JSON line whenever
its picture of the cups changes (and periodically). This module turns those
lines into the betting model the TV board renders:

  * GatewayLink   — a daemon thread that owns the serial port: auto-detects
                    it, opens it without touching DTR/RTS (the ESP32 would
                    reboot), sends the ``json 1`` handshake, hands every line
                    to feed_line(), and reconnects forever with backoff.
  * BettingBoard  — digests each state line into the model (tokens per horse,
                    shares, leader, recent events, link_ok), appends every
                    change to the JSONL event log, and fans the model out to
                    SSE subscribers.
  * sse_events()  — the generator behind GET /api/quiniela/stream.
  * validate_cmd() — the whitelist behind POST /api/quiniela/cmd.

Nothing here is required for the slideshow: without pyserial, or without a
gateway plugged in, the link idles and the model reports link_ok=false.

Race states (DdmRaceState in firmware/quiniela/ddm_common.h):
    0 PRE_RACE, 1 BETTING_OPEN, 2 FINAL_CALL, 3 AT_THE_POST, 4 RUNNING,
    5 WINNER, 6 AFTER_PARTY.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import queue
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import config

try:
    import serial  # pyserial
except ImportError:  # fresh install without requirements.txt
    serial = None  # type: ignore[assignment]

log = logging.getLogger("splash_display.quiniela")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"       # event log directory (tests point this at a temp dir)

BAUD = 115200                     # 8N1 is pyserial's default
PORT_GLOB = "/dev/serial/by-id/*"
# Substrings of a /dev/serial/by-id name that mark a gateway, matched
# case-insensitively. udev builds the name from the USB manufacturer and
# product strings, or the vendor id when the manufacturer string is empty,
# as it is on every CH34x (QinHeng, 1a86): a CH340G shows up as
# usb-1a86_USB2.0-Serial-if00-port0, newer CH340s as usb-1a86_USB_Serial-...
PORT_HINTS = ("ch340", "1a86", "usb2.0-serial", "usb_serial", "cp210", "esp32")
READ_TIMEOUT_S = 1.0
LINK_TIMEOUT_S = 5.0              # link_ok = a state line arrived within this window
STATE_LINE_S = 1.0                # the gateway's auto-emit cadence (its STATE_LINE_MS)
HELLO_SILENCE_S = 1.5 * STATE_LINE_S   # a hello is a reboot only after this much state silence
JSON_RESEND_S = 5.0               # re-send "json 1" after this much state silence
JSON_MIN_GAP_S = 1.0              # ...but never more often than this
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 10.0
MAX_LINE_BYTES = 8192             # a state line is ~2 KB; anything longer is garbage

HORSE_COUNT = 20
CUP_ONLINE_MAX_AGE_MS = 6000      # cups report every 2 s; 3 misses = offline
MAX_EVENTS = 8

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

CMD_WHITELIST = frozenset({"state", "horse", "scratch", "demo", "roster", "json"})
CMD_MAX_LEN = 200


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Coerce a JSON value to int, or return default. Never raises."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def race_state_name(state: int) -> str:
    return RACE_STATE_NAMES.get(state, f"STATE_{state}")


def _unassigned() -> Dict[str, Any]:
    return {"tokens": 0, "share": 0.0, "scratched": False, "online": False, "cup": None}


# ---------------------------------------------------------------------------
# Betting model
# ---------------------------------------------------------------------------
class BettingBoard:
    """The betting model, its event log, and its SSE subscribers.

    apply_state() is called with every parsed state line (by the link thread,
    or directly by tests); tick() is called about once a second to notice the
    gateway going silent. Both publish the full model to every subscriber
    queue when, and only when, the model changed.

    clock is a monotonic seconds source (link_ok timing); wall is unix time
    (timestamps in the model and the log). Both are injectable for tests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        log_dir: Optional[Path] = None,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._log_dir = log_dir            # None = module-level LOG_DIR, read at write time
        self._lock = threading.Lock()
        self._subs: List["queue.Queue[str]"] = []
        self._received_at: Optional[float] = None
        self._seen_state = False
        self._events: List[Dict[str, Any]] = []
        self._dup_warned: set = set()
        self._log_enabled = True
        self._model: Dict[str, Any] = self._empty_model()
        self._json: str = _dumps(self._model)

    # -- model ---------------------------------------------------------------
    def _empty_model(self) -> Dict[str, Any]:
        return {
            "link_ok": False,
            "race_state": 0,
            "race_state_name": race_state_name(0),
            "token_value": float(config.TOKEN_VALUE),
            "pot": 0.0,
            "total_tokens": 0,
            "horses": {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)},
            "leader": None,
            "events": [],
            "updated": self._wall(),
            "board_states": list(config.QUINIELA_BOARD_STATES),
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

    def link_ok(self) -> bool:
        with self._lock:
            return self._link_ok_locked()

    def _link_ok_locked(self) -> bool:
        return (
            self._received_at is not None
            and (self._clock() - self._received_at) <= LINK_TIMEOUT_S
        )

    def _digest(self, state: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
        """Turn one gateway state line into (horses, race_state, total_tokens).

        Every horse 1..20 gets an entry. A cup claims a horse via its "h";
        two cups claiming the same horse keep the lowest cup id. Missing or
        odd fields never raise.
        """
        st = _as_int(state.get("st"))
        if st is None:
            st = _as_int(state.get("phase"), 0) or 0
        cups = state.get("cups")
        if not isinstance(cups, list):
            cups = []

        entries: List[Tuple[int, int, Dict[str, Any]]] = []
        for cup in cups:
            if not isinstance(cup, dict):
                continue
            cid = _as_int(cup.get("id"))
            horse = _as_int(cup.get("h"))
            if cid is None or horse is None or not (1 <= horse <= HORSE_COUNT):
                continue
            entries.append((cid, horse, cup))
        entries.sort(key=lambda e: e[0])

        horses = {str(n): _unassigned() for n in range(1, HORSE_COUNT + 1)}
        claimed: Dict[int, int] = {}
        for cid, horse, cup in entries:
            if horse in claimed:
                key = (horse, claimed[horse], cid)
                if key not in self._dup_warned:
                    self._dup_warned.add(key)
                    log.warning(
                        "cups %d and %d both claim horse %d; keeping cup %d",
                        claimed[horse], cid, horse, claimed[horse],
                    )
                continue
            claimed[horse] = cid
            tokens = _as_int(cup.get("tok"), 0) or 0
            age = _as_int(cup.get("age"), -1)
            horses[str(horse)] = {
                "tokens": max(0, tokens),
                "share": 0.0,
                "scratched": bool(_as_int(cup.get("scr"), 0)),
                "online": age is not None and 0 <= age <= CUP_ONLINE_MAX_AGE_MS,
                "cup": cid,
            }

        total = sum(h["tokens"] for h in horses.values())
        if total:
            for h in horses.values():
                h["share"] = round(h["tokens"] / total, 4)
        return horses, st, total

    def apply_state(self, state: Dict[str, Any]) -> bool:
        """Digest one state line. Returns True if the model changed (and was
        published to subscribers)."""
        now_m = self._clock()
        now_w = self._wall()
        horses, race_state, total = self._digest(state)

        leader: Optional[int] = None
        best = 0
        for n in range(1, HORSE_COUNT + 1):
            tokens = horses[str(n)]["tokens"]
            if tokens > best:
                leader, best = n, tokens

        record: Optional[Dict[str, Any]] = None
        with self._lock:
            self._received_at = now_m
            old = self._model
            old_horses = old["horses"]

            changes: List[Dict[str, Any]] = []
            new_events: List[Dict[str, Any]] = []
            for n in range(1, HORSE_COUNT + 1):
                key = str(n)
                before, after = old_horses[key], horses[key]
                if before["tokens"] != after["tokens"]:
                    changes.append({"horse": n, "tokens": [before["tokens"], after["tokens"]]})
                    if self._seen_state:   # the first snapshot is the baseline, not a bet
                        new_events.append(
                            {"horse": n, "delta": after["tokens"] - before["tokens"], "ts": now_w}
                        )
                if before["scratched"] != after["scratched"]:
                    changes.append(
                        {"horse": n, "scratched": [before["scratched"], after["scratched"]]}
                    )
            if old["race_state"] != race_state:
                changes.append({"race_state": [old["race_state"], race_state]})
            self._seen_state = True
            if new_events:
                self._events = (new_events + self._events)[:MAX_EVENTS]

            token_value = float(config.TOKEN_VALUE)
            model = {
                "link_ok": True,
                "race_state": race_state,
                "race_state_name": race_state_name(race_state),
                "token_value": token_value,
                "pot": round(total * token_value, 2),
                "total_tokens": total,
                "horses": horses,
                "leader": leader,
                "events": list(self._events),
                "updated": old["updated"],
                "board_states": list(config.QUINIELA_BOARD_STATES),
            }
            changed = model != old
            if changed:
                model["updated"] = now_w
                self._set_locked(model)
            if changes:
                record = {
                    "ts": round(now_w, 3),
                    "race_state": race_state,
                    "changes": changes,
                    "total_tokens": total,
                }
        if record is not None:
            self._write_log(record)
        return changed

    def tick(self) -> bool:
        """Re-evaluate link_ok; publish if it flipped. Returns True if it did."""
        with self._lock:
            ok = self._link_ok_locked()
            if ok == self._model["link_ok"]:
                return False
            model = dict(self._model)
            model["link_ok"] = ok
            model["updated"] = self._wall()
            self._set_locked(model)
            return True

    def _set_locked(self, model: Dict[str, Any]) -> None:
        self._model = model
        self._json = _dumps(model)
        for q in self._subs:
            _offer(q, self._json)

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

    # -- event log -----------------------------------------------------------
    def _write_log(self, record: Dict[str, Any]) -> None:
        """One compact JSON line per model change. A write failure logs one
        WARNING and disables the log for the rest of the process."""
        if not self._log_enabled or not getattr(config, "QUINIELA_LOG", True):
            return
        directory = Path(self._log_dir) if self._log_dir is not None else LOG_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"quiniela_{date.today().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(_dumps(record) + "\n")
        except OSError as exc:
            self._log_enabled = False
            log.warning("event log disabled: cannot write under %s: %s", directory, exc)


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
# Gateway serial link
# ---------------------------------------------------------------------------
class GatewayLink:
    """Owns the gateway's USB serial port on a daemon thread.

    Port: the constructor's port, else config.GATEWAY_PORT, else auto-detect
    (autodetect()). Opening never touches DTR/RTS, so (re)starting this
    service does not reboot the gateway. On any error the port is closed and
    reopened after a backoff of 1 s doubling to 10 s, forever.

    feed_line(line) is what the thread calls for every line read; tests call
    it directly. on_state(obj) is called with every parsed state line.
    """

    def __init__(
        self,
        on_state: Callable[[Dict[str, Any]], None],
        port: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        serial_module: Any = None,
    ) -> None:
        self._on_state = on_state
        self._port_cfg = port
        self._clock = clock
        self._serial = serial_module if serial_module is not None else serial
        self._ser: Any = None
        self._ser_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_state: Optional[Dict[str, Any]] = None
        self._received_at: Optional[float] = None
        self._hello_pending = False
        self._opened_at: float = 0.0
        self._last_json_sent: float = -1e9
        self._no_port_logged = False
        self._open_fail_logged: Optional[str] = None
        self.port: Optional[str] = None      # the port currently open

    # -- public --------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._ser is not None

    @property
    def hello_pending(self) -> bool:
        """True after a gateway hello until _maybe_resend_json() has either
        re-sent "json 1" (state stream silent: a reboot) or dropped it (state
        lines still flowing: one of the gateway's 2 s repeats)."""
        return self._hello_pending

    def snapshot(self) -> Dict[str, Any]:
        """The last parsed state line plus received_at (monotonic seconds)
        and link_ok (a state line arrived within LINK_TIMEOUT_S)."""
        with self._state_lock:
            state = self._last_state
            at = self._received_at
        snap: Dict[str, Any] = dict(state) if state else {}
        snap["received_at"] = at
        snap["link_ok"] = at is not None and (self._clock() - at) <= LINK_TIMEOUT_S
        return snap

    def send(self, cmd: str) -> bool:
        """Write cmd + newline to the gateway. False if not connected or the
        write failed. Callers validate cmd first (validate_cmd)."""
        data = (str(cmd).rstrip("\r\n") + "\n").encode("utf-8")
        with self._ser_lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(data)
                return True
            except Exception as exc:  # noqa: BLE001 — pyserial raises several types
                log.warning("gateway write failed: %s", exc)
                return False

    def feed_line(self, line: str) -> Optional[str]:
        """Digest one line from the gateway. Returns "state" or "hello" when
        the line was one of those, None for everything else (discarded)."""
        line = line.strip()
        if not line:
            return None
        if not line.startswith("{"):
            log.debug("gateway: %s", line[:160])
            return None
        try:
            obj = json.loads(line)
        except ValueError:
            log.debug("gateway: unparseable JSON: %s", line[:160])
            return None
        if not isinstance(obj, dict):
            return None
        kind = obj.get("t")
        if kind == "state":
            with self._state_lock:
                self._last_state = obj
                self._received_at = self._clock()
            try:
                self._on_state(obj)
            except Exception:  # noqa: BLE001 — a model bug must not kill the link
                log.exception("state line handler failed")
            return "state"
        if kind == "hello":
            # The gateway prints hello at boot and repeats it every 2 s until
            # a downlink state line (pi5's; this display never sends one), so
            # a hello alone does not mean a reboot. _maybe_resend_json()
            # treats it as one only when the 1 Hz state stream has stopped.
            log.debug("gateway hello: %s", line[:160])
            self._hello_pending = True
            return "hello"
        log.debug("gateway: ignored %r line", kind)
        return None

    def start(self) -> bool:
        """Start the port thread (idempotent). False when pyserial is absent."""
        if self._serial is None:
            return False
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name="quiniela-link", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._detach_port()

    @staticmethod
    def autodetect(pattern: str = PORT_GLOB) -> Optional[str]:
        """First /dev/serial/by-id entry whose name carries one of PORT_HINTS
        (a CH34x by its 1a86 vendor id or product string, CP210x, ESP32), or
        None."""
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path).lower()
            if any(hint in name for hint in PORT_HINTS):
                return path
        return None

    # -- thread --------------------------------------------------------------
    def _resolve_port(self) -> Optional[str]:
        cfg = self._port_cfg if self._port_cfg is not None else getattr(config, "GATEWAY_PORT", None)
        if cfg:
            return str(cfg)
        return self.autodetect()

    def _open_port(self, port: str) -> Any:
        """The recipe from pi5/la_quiniela/bridge.py: build the port unopened,
        never touch DTR/RTS (Linux raises both together on open, which the
        ESP32's auto-reset circuit ignores; setting them separately reboots
        it), take it exclusively, then open."""
        ser = self._serial.Serial()
        ser.port = port
        ser.baudrate = BAUD
        ser.bytesize = 8
        ser.parity = "N"
        ser.stopbits = 1
        ser.timeout = READ_TIMEOUT_S
        ser.write_timeout = 1.0
        try:
            ser.exclusive = True
        except Exception:  # noqa: BLE001 — not every platform supports it
            pass
        ser.open()
        return ser

    def _attach_port(self, ser: Any, port: str = "?") -> None:
        with self._ser_lock:
            self._ser = ser
            self.port = port
        self._opened_at = self._clock()

    def _detach_port(self) -> None:
        with self._ser_lock:
            ser, self._ser, self.port = self._ser, None, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stop.is_set():
            port = self._resolve_port()
            if port is None:
                if not self._no_port_logged:
                    self._no_port_logged = True
                    log.info("no gateway serial port found (%s); will keep looking", PORT_GLOB)
                else:
                    log.debug("no gateway serial port found; retry in %.0fs", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            try:
                ser = self._open_port(port)
            except Exception as exc:  # noqa: BLE001
                if self._open_fail_logged != port:
                    self._open_fail_logged = port
                    log.warning("gateway port %s: open failed: %s", port, exc)
                else:
                    log.debug("gateway port %s: open failed again: %s", port, exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue

            self._no_port_logged = False
            self._open_fail_logged = None
            backoff = BACKOFF_MIN_S
            self._attach_port(ser, port)
            log.info("gateway link up on %s", port)
            try:
                self._serve(ser)
            except Exception as exc:  # noqa: BLE001 — unplug, EIO, decode, anything
                log.warning("gateway link on %s lost: %s", port, exc)
            finally:
                self._detach_port()
                log.info("gateway link down")
            self._stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _serve(self, ser: Any) -> None:
        """Read the open port until it fails or stop() is called."""
        self._send_json_on(first=True)
        buf = b""
        while not self._stop.is_set():
            waiting = ser.in_waiting
            chunk = ser.read(waiting if waiting > 0 else 1)   # blocks <= READ_TIMEOUT_S
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self.feed_line(raw.decode("utf-8", "replace"))
                if len(buf) > MAX_LINE_BYTES:
                    log.debug("gateway: dropping %d bytes with no newline", len(buf))
                    buf = b""
            self._maybe_resend_json()

    def _send_json_on(self, first: bool = False) -> None:
        self._last_json_sent = self._clock()
        self._hello_pending = False
        ok = self.send("json 1")
        if first:
            log.info("gateway: sent 'json 1' (%s)", "ok" if ok else "write failed")
        else:
            log.debug("gateway: re-sent 'json 1' (%s)", "ok" if ok else "write failed")

    def _maybe_resend_json(self) -> None:
        """Auto-emit is not persisted on the gateway, so "json 1" goes out
        again when the gateway rebooted and whenever no state line has come
        for JSON_RESEND_S (at most once per JSON_RESEND_S).

        A reboot shows as a hello while the state stream is silent. The
        gateway repeats hello every 2 s for as long as it has not had a
        downlink state line (which this display never sends), so a hello
        that arrives while state lines are still flowing (the last one under
        HELLO_SILENCE_S ago) is a repeat and is dropped; re-sending on it
        would make the gateway print an ack and an extra state line every
        2 s for the whole event. A real reboot stops the 1 Hz state lines,
        so the next hello (2 s later at the latest) re-sends, at most once
        per JSON_MIN_GAP_S; the silence rule is the backstop either way.
        """
        now = self._clock()
        since_sent = now - self._last_json_sent
        with self._state_lock:
            at = self._received_at
        if self._hello_pending:
            if at is not None and now - at <= HELLO_SILENCE_S:
                log.debug("gateway hello while state lines flow: not a reboot")
                self._hello_pending = False
            elif since_sent >= JSON_MIN_GAP_S:
                self._send_json_on()
            return
        last_activity = max(at if at is not None else 0.0, self._opened_at)
        if now - last_activity > JSON_RESEND_S and since_sent >= JSON_RESEND_S:
            self._send_json_on()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def validate_cmd(cmd: Any) -> Tuple[Optional[str], Optional[str]]:
    """Check one line bound for the gateway. Returns (clean_cmd, None) or
    (None, error). Only the first word is whitelisted; the gateway itself
    validates the arguments and answers with an err line if it dislikes them."""
    if not isinstance(cmd, str):
        return None, "cmd must be a string"
    text = cmd.strip()
    if not text:
        return None, "empty command"
    if len(text) > CMD_MAX_LEN:
        return None, f"command longer than {CMD_MAX_LEN} characters"
    if "\n" in text or "\r" in text:
        return None, "command must be a single line"
    word = text.split()[0]
    if word not in CMD_WHITELIST:
        return None, f"command not allowed: {word} (allowed: {' '.join(sorted(CMD_WHITELIST))})"
    return text, None


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------
def sse_events(
    target: Optional[BettingBoard] = None,
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
board = BettingBoard()
link = GatewayLink(on_state=board.apply_state)

_started = False


def _tick_loop() -> None:
    """Once a second, let the board notice the gateway going silent."""
    while not link._stop.wait(1.0):
        try:
            board.tick()
        except Exception:  # noqa: BLE001
            log.exception("board tick failed")


def start_link() -> None:
    """Start the gateway link and the link_ok ticker. Idempotent, and a no-op
    (with one WARNING) when pyserial is not installed."""
    global _started
    if _started:
        return
    _started = True
    if serial is None:
        log.warning(
            "pyserial is not installed; La Quiniela gateway link disabled "
            "(pip install -r requirements.txt)"
        )
        return
    link.start()
    threading.Thread(target=_tick_loop, name="quiniela-tick", daemon=True).start()
    log.info(
        "quiniela gateway link started (port=%s)",
        getattr(config, "GATEWAY_PORT", None) or "auto-detect",
    )

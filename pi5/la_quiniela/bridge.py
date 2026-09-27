# la_quiniela/bridge.py - The DevPi end of the gateway's serial link
#
# A daemon thread reads the gateway's JSON lines, keeps a live picture of the
# cups in memory, writes the lq_cups / telemetry / events tables sparingly
# (DevPi runs on an SD card), and emits lq_* SocketIO events to the "lq"
# room. It writes the state line down, answers the gateway's hello, and
# re-sends whenever a status line shows the gateway out of sync. Nothing here
# may take Flask down: every serial failure is caught, logged and retried.
#
# Protocol v2 (2026-09-27): the cup owns its horse number. A cup is known by
# its MAC and by the horse it reports in every packet; pi5 learns which
# horses have cups by listening, and sends nothing per cup. What goes down is
# one state line keyed by horse number: the phase, the horses scratched with
# no replacement, the renumber pairs (a replacement scratch: the cup that was
# 9 becomes 22) and the three results. There are no cup IDs, slots, rosters
# or MAC tables on this side of the wire.
#
# Conventions follow la_subasta: a module-level init called from main.py, a
# stub-able socketio, raw sqlite3, plain threads. SocketIO runs in threading
# mode in this app (no eventlet/gevent), so socketio.emit() from this thread
# is the same path main.py's odds poller uses.

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from la_quiniela import protocol as P
from la_quiniela.models import LqDb, default_db_path, utc_now_iso

logger = logging.getLogger(__name__)

LQ_ROOM = "lq"


def console_print(text: str) -> None:
    """The app's startup text goes to stdout, and nothing the logger writes
    reaches it. Anyone watching the terminal has to be able to tell a working
    bridge from a deaf one, so the few things that matter are printed too."""
    try:
        print(text, flush=True)
    except Exception:           # a closed or odd stdout must never reach the thread
        pass

# Configuration keys, their defaults, and the environment override DDM_<key>.
DEFAULTS: Dict[str, Any] = {
    "LQ_BRIDGE_ENABLED": True,
    "LQ_SERIAL_PORT": "",
    "LQ_SERIAL_BAUD": 115200,
    "LQ_SERIAL_LINES": "leave",   # "leave" = never touch DTR/RTS; "low" = hold both low
    "LQ_HEARTBEAT_LOG_S": 10,
    "LQ_CUP_OFFLINE_S": 6,
    "LQ_GATEWAY_OFFLINE_S": 12,
    "LQ_DEAF_REOPEN_S": 20,       # port open but no valid JSON line for this long -> reopen
    "LQ_REOPEN_MIN_GAP_S": 30,    # never reopen more often than this
}

# Fixed timing. The gateway side of each is documented in firmware/quiniela/README.md.
SERIAL_LINE_MODES = ("leave", "low")

READ_TIMEOUT_S = 1.0          # serial read timeout, so the thread notices a stop request
READ_CHUNK_BYTES = 4096       # most bytes taken from the port in one read
OPEN_SETTLE_S = 0.1           # pause after opening, before the input buffer is dropped
THREAD_RESTART_S = 5.0        # supervisor pause before the thread's loop starts over
RETRY_S = 5.0                 # port open / reopen retry interval
PORT_LOG_MIN_S = 60.0         # repeated port failures are logged at most this often
TICK_S = 1.0                  # timer check interval
RESEND_MIN_S = 2.0            # at most one reconcile re-send per this
HELLO_EVENT_MIN_S = 10.0      # gateway_hello event at most this often
GATEWAY_STALE_MS = 3000       # the gateway's own STALE threshold: its status ages under this count as heard
PENDING_MAX = 4 * P.MAX_LINE_BYTES   # partial-line buffer ceiling

LINK_REASONS = ("boot", "reboot", "online", "offline", "port_open", "port_closed",
                "status", "protocol_mismatch", "deaf")

# Every MAC the cup simulator invents starts with this. Nothing real does: it
# is a locally-administered address, and no ESP32 ships with one. The moment
# a real gateway says hello, simulated cups are dropped from the cache.
SIM_MAC_PREFIX = "02:DD:4D:"


def is_sim_mac(mac: Optional[str]) -> bool:
    return bool(mac) and mac.upper().startswith(SIM_MAC_PREFIX)


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

def _env_override(name: str, default: Any) -> Any:
    raw = os.environ.get("DDM_" + name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


def load_settings(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """DEFAULTS, then pi5/config.py, then DDM_* environment variables, then
    explicit overrides (tests)."""
    settings = dict(DEFAULTS)
    try:
        import config as app_config  # pi5/config.py, on sys.path when the app runs
    except Exception:
        app_config = None
    for key in settings:
        if app_config is not None and hasattr(app_config, key):
            settings[key] = getattr(app_config, key)
        settings[key] = _env_override(key, settings[key])
    if overrides:
        settings.update(overrides)
    return settings


# -----------------------------------------------------------------------------
# The real serial port
# -----------------------------------------------------------------------------

def open_serial_port(port: str, baud: int, timeout: float, lines: str = "leave",
                     serial_class=None):
    """Open the gateway's port. The promise is that restarting the app does
    not reboot the gateway.

    lines="leave", the default, does not touch DTR or RTS at all: not before
    opening, not after, and it never reads them either. On the CP2102 board
    on the bench (2026-09-19) that is what keeps the gateway running. Linux
    raises both lines together on open, which the ESP32's auto-reset circuit
    ignores.

    lines="low" builds the port unopened and holds both lines low first. That
    was meant to prevent a reset and on that board causes one, because setting
    them one after the other passes through DTR low with RTS high, which is
    exactly the combination that pulls EN low. It is kept for a board that
    turns out to need it.

    exclusive=True stops a second copy of the app opening the same port.
    serial_class is a seam for the tests; the real one is serial.Serial."""
    if serial_class is None:
        import serial
        serial_class = serial.Serial
    ser = serial_class()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.write_timeout = 1.0
    if lines == "low":
        for attr in ("dtr", "rts"):
            try:
                setattr(ser, attr, False)
            except Exception:       # a virtual port may refuse the control lines
                pass
    try:
        ser.exclusive = True
    except Exception:               # not every platform supports it
        pass
    ser.open()
    return ser


def pyserial_factory(port: str, baud: int, timeout: float):
    """open_serial_port with the default line handling, for a caller that has
    no settings to hand."""
    return open_serial_port(port, baud, timeout)


# -----------------------------------------------------------------------------
# Live records
# -----------------------------------------------------------------------------

@dataclass
class CupLive:
    mac: str
    horse: int = 0                       # the cup's own claim, 0 = none set
    count: Optional[int] = None
    raw: Optional[int] = None
    rssi: Optional[int] = None
    up: Optional[int] = None
    drop: Optional[int] = None
    seq: Optional[int] = None
    online: bool = False
    hello: bool = False                  # its last packet was a HELLO: no gateway MAC yet
    last_seen_mono: Optional[float] = None
    last_seen_ts: Optional[str] = None
    last_row_mono: Optional[float] = None    # when the last telemetry row was written
    last_row_count: Optional[int] = None     # the token count in that row


@dataclass
class LinkLive:
    port_open: bool = False
    gateway_online: bool = False
    in_sync: bool = False
    reason: str = "boot"
    gateway_mac: Optional[str] = None
    phase: Optional[int] = None
    state_rev: int = 0                   # what the gateway last reported
    cups_heard: int = 0                  # cups in the gateway's table heard within its STALE window
    rejects: int = 0
    up_s: Optional[int] = None
    last_line_mono: Optional[float] = None           # any line at all, junk included
    last_good_line_mono: Optional[float] = None      # a line that parsed as JSON
    last_hello_event_mono: Optional[float] = None
    last_resend_mono: Optional[float] = None
    last_reopen_mono: Optional[float] = None


# -----------------------------------------------------------------------------
# The bridge
# -----------------------------------------------------------------------------

class LqBridge:

    def __init__(self, settings: Optional[Dict[str, Any]] = None, db_path: Optional[str] = None,
                 serial_factory: Optional[Callable[[str, int, float], Any]] = None,
                 socketio=None, clock: Optional[Callable[[], float]] = None,
                 console: Optional[Callable[[str], None]] = None):
        self.settings = dict(DEFAULTS)
        self.settings.update(settings or {})
        self.socketio = socketio
        self._listeners: List[Callable[[], None]] = []   # see add_listener()
        self._factory = serial_factory
        self._clock = clock or time.monotonic

        self._lock = threading.RLock()        # everything below
        self._write_lock = threading.Lock()   # the port's write side
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._port = None
        self._pending = b""
        self._port_failures = 0
        self._port_fail_logged_mono: Optional[float] = None

        self.cups: Dict[str, CupLive] = {}    # by MAC
        self.link = LinkLive()
        self._resync = False              # drop bytes until the next newline
        self._last_link_key = None        # what the last emitted lq_link said
        self._lines_warned = False        # LQ_SERIAL_LINES warned about once
        self._console = console or console_print
        self.stats = {"lines": 0, "text": 0, "bad_json": 0, "too_long": 0,
                      "unknown_type": 0, "sent": 0, "bytes_rx": 0, "reopens": 0,
                      "thread_restarts": 0}

        # DevPi-owned state, keyed by horse number. DevPi always holds a
        # state (PRE_RACE with nothing scratched until something is set) and
        # answers every hello with it; state_rev starts at 1 and only goes up.
        self.state_rev = 1
        self.phase: int = int(P.Phase.PRE_RACE)
        self.scratched: List[int] = []
        self.renum: List[Tuple[int, int]] = []
        self.results: List[int] = [0] * P.RESULT_SLOTS

        self.db = LqDb(db_path or default_db_path())
        self.schema_error = self.db.check_shape()
        if self.schema_error:
            logger.error("La Quiniela bridge: %s", self.schema_error)
        else:
            self.db.init_schema()
            self._load_persisted()
            self._load_cups()

    # -- persistence ----------------------------------------------------------

    def _load_persisted(self) -> None:
        row = self.db.load_link_state()
        self.state_rev = max(1, int(row["state_rev"] or 0))
        if row["state_json"]:
            try:
                data = json.loads(row["state_json"])
                if not isinstance(data, dict):
                    raise TypeError("state_json is not an object")
                if "horses" in data or isinstance(data.get("scratched"), dict):
                    # A v1 state_json (cup slots: horses and scratched keyed
                    # by slot) carries nothing v2 can use but the phase.
                    data = {"phase": data.get("phase")}
                self.phase = P.validate_phase(data["phase"])
                self.scratched = P.validate_scratched(data.get("scratched", []))
                self.renum = P.validate_renum(data.get("renum", []))
                self.results = P.validate_results(data.get("results", [0] * P.RESULT_SLOTS))
            except (ValueError, KeyError, TypeError) as exc:
                logger.error("La Quiniela bridge: persisted state unreadable (%s), "
                             "starting from PRE_RACE", exc)
                self.phase = int(P.Phase.PRE_RACE)
                self.scratched, self.renum, self.results = [], [], [0] * P.RESULT_SLOTS
        if not row["state_json"] or int(row["state_rev"] or 0) < 1:
            self._persist()

    def _persist(self) -> None:
        self.db.save_link_state(self.state_rev, json.dumps({
            "phase": self.phase,
            "scratched": list(self.scratched),
            "renum": [[f, t] for f, t in self.renum],
            "results": list(self.results),
        }))

    def _load_cups(self) -> None:
        """Seed the live table from the lq_cups rows so a snapshot right after
        a restart shows the last known cups and their horses, all offline."""
        for row in self.db.load_cups():
            live = CupLive(mac=row["mac"])
            live.horse = P.parse_horse(row["horse"])
            live.count = row["last_count"]
            live.raw = row["last_raw"]
            live.rssi = row["rssi"]
            live.up = row["up_rssi"]
            live.last_seen_ts = row["last_seen"]
            live.online = False
            self.cups[live.mac] = live

    # -- the port -------------------------------------------------------------

    def lines_mode(self) -> str:
        """Which DTR/RTS handling to open the port with. Anything that is not
        a known mode warns once and behaves as "leave"."""
        raw = self.settings.get("LQ_SERIAL_LINES", "leave")
        mode = str(raw).strip().lower()
        if mode in SERIAL_LINE_MODES:
            return mode
        if not self._lines_warned:
            self._lines_warned = True
            logger.warning("La Quiniela bridge: LQ_SERIAL_LINES is %r, which is not one of %s; "
                           "leaving DTR and RTS alone", raw, " or ".join(SERIAL_LINE_MODES))
            self.say("LQ_SERIAL_LINES is %r, not one of %s; leaving the control lines alone"
                     % (raw, " or ".join(SERIAL_LINE_MODES)))
        return "leave"

    def _default_factory(self, port: str, baud: int, timeout: float):
        """The real port, opened the way the settings say. Read at open time,
        so a watchdog reopen uses the same mode as the first open."""
        return open_serial_port(port, baud, timeout, lines=self.lines_mode())

    # -- console --------------------------------------------------------------

    def say(self, text: str) -> None:
        """One short line to the terminal the app runs in. Only for things a
        person watching would want to know; never per telemetry line."""
        try:
            self._console("[LQ] " + text)
        except Exception:
            pass

    # -- lifecycle ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the serial thread. Returns False, after one log line, when
        the bridge is disabled, mis-schemad, has no port configured, or
        pyserial is missing. The rest of the app carries on either way."""
        if self.schema_error:
            logger.error("La Quiniela bridge not started: %s", self.schema_error)
            return False
        if not self.settings.get("LQ_BRIDGE_ENABLED", True):
            logger.info("La Quiniela bridge disabled (LQ_BRIDGE_ENABLED is false)")
            return False
        if self._factory is None:
            try:
                import serial  # noqa: F401
            except ImportError:
                logger.warning("La Quiniela bridge disabled: pyserial is not installed "
                               "(pip install -r requirements.txt)")
                return False
            self._factory = self._default_factory
        if not self.settings.get("LQ_SERIAL_PORT"):
            logger.warning("La Quiniela bridge idle: LQ_SERIAL_PORT is empty. Set it in "
                           "pi5/config.py or export DDM_LQ_SERIAL_PORT; on DevPi use the "
                           "/dev/serial/by-id/... path from `ls -l /dev/serial/by-id/`")
            return False
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lq-bridge", daemon=True)
        self._thread.start()
        logger.info("La Quiniela bridge started on %s @ %s",
                    self.settings["LQ_SERIAL_PORT"], self.settings["LQ_SERIAL_BAUD"])
        self.say("bridge started on %s @ %s, lines: %s"
                 % (self.settings["LQ_SERIAL_PORT"], self.settings["LQ_SERIAL_BAUD"],
                    self.lines_mode()))
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._close_port("stop")
        self._thread = None

    def close(self) -> None:
        self.stop()
        self.db.close()

    # -- the thread -----------------------------------------------------------

    def _run(self) -> None:
        """The thread's whole life. Nothing may end it but a stop request: an
        unexpected error is logged with its traceback, the port is dropped and
        the loop starts over, because a bridge that has quietly died looks
        exactly like a bridge with nothing to say."""
        while not self._stop.is_set():
            try:
                self._loop()
            except BaseException:       # including anything a C extension raises
                self.stats["thread_restarts"] += 1
                logger.exception("La Quiniela bridge: reader thread failed, restarting in %.0f s",
                                 THREAD_RESTART_S)
                self.say("reader thread failed (%d), restarting in %.0f s - see the log for the traceback"
                         % (self.stats["thread_restarts"], THREAD_RESTART_S))
                try:
                    self._close_port("port_closed")
                except Exception:
                    pass
                self._stop.wait(THREAD_RESTART_S)
        self._close_port("stop")

    def _loop(self) -> None:
        next_tick = self._clock()
        while not self._stop.is_set():
            try:
                if self._port is None:
                    if not self._open_port():
                        self._stop.wait(RETRY_S)
                else:
                    self._read_once()
            except Exception:           # an ordinary error only costs the port
                logger.exception("La Quiniela bridge: unexpected error, port will be reopened")
                self._close_port("port_closed")
                self._stop.wait(RETRY_S)
            now = self._clock()
            if now >= next_tick:
                next_tick = now + TICK_S
                try:
                    self.tick(now)
                except Exception:
                    logger.exception("La Quiniela bridge: timer error")

    def _open_port(self) -> bool:
        factory = self._factory or self._default_factory
        try:
            port = factory(self.settings["LQ_SERIAL_PORT"],
                           int(self.settings["LQ_SERIAL_BAUD"]), READ_TIMEOUT_S)
        except Exception as exc:
            self._port_failures += 1
            now = self._clock()
            if (self._port_fail_logged_mono is None
                    or now - self._port_fail_logged_mono >= PORT_LOG_MIN_S):
                self._port_fail_logged_mono = now
                logger.warning("La Quiniela bridge: cannot open %s (%s); retrying every %.0f s"
                               "%s", self.settings["LQ_SERIAL_PORT"], exc, RETRY_S,
                               "" if self._port_failures == 1 else
                               f" (attempt {self._port_failures})")
                self.say("cannot open %s (%s); retrying every %.0f s"
                         % (self.settings["LQ_SERIAL_PORT"], exc, RETRY_S))
            with self._lock:
                self._set_link(port_open=False, gateway_online=False, reason="port_closed")
            return False
        # A port that has just been opened is mid-conversation: the gateway
        # never stops talking, and the bytes that arrived before the baud rate
        # was applied are junk. Let them land, drop them, and refuse to parse
        # anything before the first newline.
        self._stop.wait(OPEN_SETTLE_S)
        try:
            port.reset_input_buffer()
        except Exception as exc:        # a pty or a fake has nothing to reset
            logger.debug("La Quiniela bridge: reset_input_buffer not available (%s)", exc)
        with self._lock:
            self._port = port
            self._pending = b""
            self._resync = True         # nothing is a line until a newline says so
            self._port_failures = 0
            self._port_fail_logged_mono = None
            now = self._clock()
            self.link.last_good_line_mono = now     # the watchdog counts from here
            logger.info("La Quiniela bridge: %s open", self.settings["LQ_SERIAL_PORT"])
            self._set_link(port_open=True, reason="port_open")
        return True

    def _close_port(self, reason: str) -> None:
        with self._lock:
            port, self._port = self._port, None
            self._pending = b""
            self._resync = False
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
                # The caller's reason, when it is one clients know; "stop" and
                # anything else read as an ordinary close.
                self._set_link(port_open=False, gateway_online=False,
                               reason=reason if reason in LINK_REASONS else "port_closed")

    def _read_once(self) -> None:
        """Take at most one bounded chunk from the port and turn it into lines.

        Never readline(). pyserial's readline() has no size limit and no
        overall timeout: it reads one byte at a time until it sees a newline,
        so a stream that never sends one - a desynchronised UART, a run of
        NULs, a wrong baud rate - blocks this thread for as long as the bytes
        keep coming. That also starves tick(), so nothing notices and nothing
        recovers. A bounded read cannot do that."""
        port = self._port
        if port is None:                       # closed under us by a failed write
            return
        try:
            want = READ_CHUNK_BYTES
            waiting = getattr(port, "in_waiting", None)
            if isinstance(waiting, int) and waiting > 0:
                want = min(waiting, READ_CHUNK_BYTES)
            chunk = port.read(want)
        except (OSError, ValueError) as exc:   # SerialException is an OSError
            logger.warning("La Quiniela bridge: read failed (%s); reopening in %.0f s", exc, RETRY_S)
            self._close_port("port_closed")
            self._stop.wait(RETRY_S)
            return
        if chunk:
            self.feed_bytes(chunk)

    def feed_bytes(self, chunk: bytes) -> None:
        """Bytes in, whole lines out. Whatever arrives, a newline always puts
        this back into a clean start-of-line state, and nothing is buffered
        without bound."""
        self.stats["bytes_rx"] += len(chunk)
        buf = self._pending + chunk
        self._pending = b""
        if self._resync:
            nl = buf.find(b"\n")
            if nl < 0:
                return                          # still inside the junk: keep none of it
            buf = buf[nl + 1:]
            self._resync = False
        parts = buf.split(b"\n")
        self._pending = parts.pop()             # the tail, still unterminated
        for part in parts:
            line = part.rstrip(b"\r")
            if line:
                self.handle_raw_line(line)
        if len(self._pending) > PENDING_MAX:
            # Far too long to be a line. Drop it and everything up to the next
            # newline, rather than growing a buffer for a talker that never
            # ends a line.
            self._pending = b""
            self._resync = True
            self.stats["too_long"] += 1

    # -- uplink ---------------------------------------------------------------

    def handle_raw_line(self, raw: bytes) -> None:
        """One raw line from the port. Anything that is not a JSON object is
        dropped; the gateway's "# " text, bad JSON and over-long lines are
        counted. Never raises."""
        obj, why = P.decode_line(raw)
        with self._lock:
            now = self._clock()
            self._touch_gateway(now)
            if obj is None:
                if why == "text":
                    self.stats["text"] += 1
                elif why == "too_long":
                    self.stats["too_long"] += 1
                    logger.warning("La Quiniela bridge: dropped a line over %d bytes", P.MAX_LINE_BYTES)
                elif why == "bad_json":
                    self.stats["bad_json"] += 1
                    logger.warning("La Quiniela bridge: bad JSON (%d so far): %r",
                                   self.stats["bad_json"], raw[:60])
                return
            self.stats["lines"] += 1
            self.link.last_good_line_mono = now     # what the watchdog waits for
            try:
                self.handle_message(obj, now)
            except Exception:
                logger.exception("La Quiniela bridge: error handling %r", raw[:120])

    def handle_message(self, msg: Dict[str, Any], now: Optional[float] = None) -> None:
        if now is None:
            now = self._clock()
        with self._lock:
            kind = msg.get("t")
            if kind == "telem":
                self._on_telem(msg, now)
            elif kind == "hello":
                self._on_hello(msg, now)
            elif kind == "status":
                self._on_status(msg, now)
            elif kind == "err":
                self._on_err(msg)
            else:
                self.stats["unknown_type"] += 1

    @staticmethod
    def _int(msg: Dict[str, Any], key: str) -> Optional[int]:
        value = msg.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _live_for(self, mac: str) -> CupLive:
        live = self.cups.get(mac)
        if live is None:
            live = CupLive(mac=mac)
            self.cups[mac] = live
        return live

    def _on_telem(self, msg: Dict[str, Any], now: float) -> None:
        """One packet from a cup: what it says it is and what it weighs.
        Events when it comes online and when its horse changes; a telemetry
        row when the count changed, when the heartbeat interval has passed or
        when the cup comes back; the lq_cups row and lq_update at those
        moments; a fresh snapshot when the per-horse picture moved (a horse
        came online or changed hands)."""
        mac = P.normalize_mac(msg.get("mac"))
        if mac is None:
            return
        live = self._live_for(mac)
        old_horse = live.horse
        live.horse = P.parse_horse(msg.get("horse"))
        live.count = self._int(msg, "count")
        live.raw = self._int(msg, "raw")
        live.rssi = self._int(msg, "rssi")
        live.up = self._int(msg, "up")
        live.drop = self._int(msg, "drop")
        live.seq = self._int(msg, "seq")
        live.hello = self._int(msg, "hello") == 1
        live.last_seen_mono = now
        live.last_seen_ts = utc_now_iso()
        came_online = not live.online
        live.online = True
        horse_changed = live.horse != old_horse

        if came_online:
            self._event("cup_online", live.horse or None, {"mac": mac, "horse": live.horse})
        if horse_changed:
            self._event("cup_horse", live.horse or None, {"mac": mac, "from": old_horse, "to": live.horse})
            if old_horse or live.horse:
                self.say("cup %s is horse %s%s" % (mac, live.horse or "none",
                                                   "" if not old_horse else " (was %d)" % old_horse))

        count_changed = live.count != live.last_row_count
        heartbeat_due = (live.last_row_mono is None
                         or now - live.last_row_mono >= float(self.settings["LQ_HEARTBEAT_LOG_S"]))
        row_due = count_changed or heartbeat_due or came_online
        if row_due:
            reason = "change" if count_changed else "heartbeat"
            self.db.insert_telemetry(live.last_seen_ts, mac, live.horse or None, live.raw, live.count,
                                     live.seq, live.drop, live.rssi, live.up, reason)
            live.last_row_mono = now
            live.last_row_count = live.count
        if row_due or horse_changed:
            self._upsert_cup_row(live)
            self._emit_cup(live)
        if horse_changed or came_online:
            self._emit_snapshot()      # the per-horse picture moved: the whole table is the honest picture

    def _on_hello(self, msg: Dict[str, Any], now: float) -> None:
        version = self._int(msg, "v")
        mac = P.normalize_mac(msg.get("mac"))
        if mac:
            self.link.gateway_mac = mac
        # A gateway with nothing to hear repeats its hello every 2 s, so the
        # console says so on the same 10 s rhythm as the event.
        last = self.link.last_hello_event_mono
        announce = last is None or now - last >= HELLO_EVENT_MIN_S
        if announce:
            self.say("gateway hello from %s" % (mac or "an unnamed gateway"))
        if version != P.LINE_PROTO_VERSION:
            logger.error("La Quiniela bridge: gateway speaks line protocol v%s, this bridge v%d; "
                         "sending nothing", version, P.LINE_PROTO_VERSION)
            self._set_link(reason="protocol_mismatch", force=True, in_sync=False)
            return
        # A hello means the gateway knows nothing: it has no state.
        self.link.state_rev = 0
        if announce:
            self.link.last_hello_event_mono = now
            self._event("gateway_hello", None, {"mac": mac, "v": version,
                                                 "proto": self._int(msg, "proto")})
        # The cup simulator's cups live in the same cache. A real gateway
        # saying hello means the simulator is done: its cups are dropped so
        # they do not sit on the admin page as offline cups forever.
        if mac and not is_sim_mac(mac):
            dropped = self.forget_cups(SIM_MAC_PREFIX, quiet=True)
            if dropped:
                self.say("dropped %d simulated cup(s) from the cache: a real gateway said hello" % dropped)
        self._send_state()
        if announce:
            self.say("answered the hello with state rev %d" % self.state_rev)
        self.link.last_resend_mono = now
        self._set_link(reason="boot", force=True, in_sync=self._compute_sync())

    def _on_status(self, msg: Dict[str, Any], now: float) -> None:
        """The gateway's heartbeat: its phase and state rev (reconciled if
        they differ from ours) and its cup table. The table is a backstop:
        an entry fresher than anything heard directly from that cup (after a
        pi5 restart, or a telem line lost to a reopen) updates the cup's
        horse, count and signal, and puts it online if the gateway heard it
        within the offline window."""
        up_s = self._int(msg, "up_s")
        rebooted = (up_s is not None and self.link.up_s is not None and up_s < self.link.up_s)
        self.link.phase = self._int(msg, "phase")
        self.link.state_rev = self._int(msg, "state_rev") or 0
        self.link.rejects = self._int(msg, "rejects") or 0
        self.link.up_s = up_s
        if rebooted:
            self._event("gateway_reboot", None, {"up_s": up_s, "gseq": self._int(msg, "gseq")})

        entries = msg.get("cups")
        heard = 0
        changed = False
        if isinstance(entries, list):
            cup_limit = float(self.settings["LQ_CUP_OFFLINE_S"])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                mac = P.normalize_mac(entry.get("mac"))
                age = self._int(entry, "age")
                if mac is None or age is None or age < 0:
                    continue
                if age <= GATEWAY_STALE_MS:
                    heard += 1
                seen_at = now - age / 1000.0
                live = self._live_for(mac)
                if live.last_seen_mono is not None and seen_at <= live.last_seen_mono:
                    continue                    # we heard this cup ourselves, more recently
                old_horse, was_online = live.horse, live.online
                live.horse = P.parse_horse(entry.get("horse"))
                if self._int(entry, "tok") is not None:
                    live.count = self._int(entry, "tok")
                if self._int(entry, "rssi") is not None:
                    live.rssi = self._int(entry, "rssi")
                if self._int(entry, "up") is not None:
                    live.up = self._int(entry, "up")
                live.last_seen_mono = seen_at
                live.last_seen_ts = utc_now_iso()
                live.online = age / 1000.0 < cup_limit
                if live.online and not was_online:
                    self._event("cup_online", live.horse or None, {"mac": mac, "horse": live.horse, "via": "status"})
                if live.horse != old_horse:
                    self._event("cup_horse", live.horse or None, {"mac": mac, "from": old_horse, "to": live.horse, "via": "status"})
                if live.horse != old_horse or live.online != was_online or live.count != live.last_row_count:
                    self._upsert_cup_row(live)
                    changed = True
        self.link.cups_heard = heard
        if self.link.state_rev != self.state_rev:
            self._reconcile(now)
        self._set_link(in_sync=self._compute_sync(), reason="reboot" if rebooted else "status")
        if changed:
            self._emit_snapshot()

    def _on_err(self, msg: Dict[str, Any]) -> None:
        detail = {"msg": msg.get("msg"), "line": msg.get("line")}
        logger.warning("La Quiniela bridge: gateway rejected a line: %s %r",
                       detail["msg"], detail["line"])
        self._event("gateway_err", None, detail)

    # -- timers ---------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> None:
        """Offline detection. Called about once a second by the thread; tests
        call it with a clock of their own."""
        if now is None:
            now = self._clock()
        with self._lock:
            cup_limit = float(self.settings["LQ_CUP_OFFLINE_S"])
            snapshot_dirty = False
            for live in self.cups.values():
                if live.online and live.last_seen_mono is not None \
                        and now - live.last_seen_mono >= cup_limit:
                    live.online = False
                    self._event("cup_offline", live.horse or None, {"mac": live.mac, "horse": live.horse})
                    self.db.set_cup_online(live.mac, False)
                    self._emit_cup(live)
                    snapshot_dirty = True
            if snapshot_dirty:
                self._emit_snapshot()
            gw_limit = float(self.settings["LQ_GATEWAY_OFFLINE_S"])
            if self.link.gateway_online and self.link.last_line_mono is not None \
                    and now - self.link.last_line_mono >= gw_limit:
                self._set_link(gateway_online=False, in_sync=False, reason="offline")
                self.say("gateway offline: nothing heard for %.0f s" % gw_limit)
            deaf = self._watchdog_due(now)
        if deaf:
            # Outside the lock: reopening blocks, and a Flask request thread
            # must not be held up behind it.
            self._reopen_deaf(now)

    def _watchdog_due(self, now: float) -> bool:
        """True when the port is open but nothing valid has come out of it for
        LQ_DEAF_REOPEN_S. The gateway sends status every 5 s, so silence that
        long means the port is open onto something that is not talking to us."""
        if self._port is None or not self.link.port_open:
            return False
        limit = float(self.settings["LQ_DEAF_REOPEN_S"])
        last = self.link.last_good_line_mono
        if last is None or now - last < limit:
            return False
        gap = float(self.settings["LQ_REOPEN_MIN_GAP_S"])
        if self.link.last_reopen_mono is not None and now - self.link.last_reopen_mono < gap:
            return False
        return True

    def _reopen_deaf(self, now: float) -> None:
        """Close the port and open it again through the normal path. With the
        gateway genuinely absent this simply repeats, which is harmless."""
        silent = now - (self.link.last_good_line_mono or now)
        with self._lock:
            self.link.last_reopen_mono = now
            self.stats["reopens"] += 1
            self._event("bridge_reopen", None, {"silent_s": round(silent, 1),
                                                "reopens": self.stats["reopens"],
                                                "bytes_rx": self.stats["bytes_rx"]})
        logger.warning("La Quiniela bridge: no valid line for %.0f s on an open port; "
                       "closing and reopening %s", silent, self.settings["LQ_SERIAL_PORT"])
        self.say("no data from the gateway for %.0f s - reopening the port (%d so far)"
                 % (silent, self.stats["reopens"]))
        self._close_port("deaf")
        self._open_port()

    # -- downlink -------------------------------------------------------------

    def _send_line(self, text: str) -> bool:
        """Write one line to the port. False if the port is not open or the
        write failed (the port is then closed and reopened by the thread)."""
        failure = None
        with self._write_lock:
            port = self._port
            if port is None:
                return False
            try:
                port.write((text + "\n").encode("utf-8"))
            except Exception as exc:
                failure = exc
        # Close outside the write lock: _close_port takes the state lock, and
        # the bridge thread may hold that lock while waiting for the write lock.
        if failure is not None:
            logger.warning("La Quiniela bridge: write failed (%s); reopening", failure)
            self._close_port("port_closed")
            return False
        self.stats["sent"] += 1
        return True

    def state_line(self) -> str:
        """The state line as it goes down the wire, byte-exact."""
        return P.build_state_line(self.state_rev, self.phase, self.scratched, self.renum, self.results)

    def _send_state(self) -> bool:
        return self._send_line(self.state_line())

    def _reconcile(self, now: float) -> bool:
        """Rate-limited re-send of the state, used when a status line shows
        the gateway holding another rev."""
        last = self.link.last_resend_mono
        if last is not None and now - last < RESEND_MIN_S:
            return False
        if self._port is None:
            return False
        self.link.last_resend_mono = now
        self._send_state()
        self.say("re-sent state rev %d to the gateway" % self.state_rev)
        return True

    # -- link bookkeeping -----------------------------------------------------

    def _touch_gateway(self, now: float) -> None:
        self.link.last_line_mono = now
        if not self.link.gateway_online:
            self._set_link(gateway_online=True, reason="online")
            self.say("gateway online")

    def _compute_sync(self) -> bool:
        """In sync means the gateway holds the state DevPi holds."""
        return self.link.gateway_online and self.link.state_rev == self.state_rev

    def _set_link(self, reason: str, force: bool = False, **changes: Any) -> None:
        """Apply changes to the link record and emit lq_link only when one of
        port_open, gateway_online or in_sync actually changed, or when force
        is set and the reason is new (a hello, a protocol mismatch)."""
        before = (self.link.port_open, self.link.gateway_online, self.link.in_sync)
        for key, value in changes.items():
            setattr(self.link, key, value)
        if not self.link.port_open:
            self.link.gateway_online = False
        if not self.link.gateway_online:
            self.link.in_sync = False
        after = (self.link.port_open, self.link.gateway_online, self.link.in_sync)
        if reason == "online" and not self.link.gateway_online:
            # "online" is the gateway's word, not the port's. Opening the port
            # used to claim it, which is how a deaf bridge came to describe
            # itself as online with gateway_online false.
            reason = "port_open" if self.link.port_open else "port_closed"
        # A forced emit is deduped on more than the reason word. Using the
        # word alone dropped the second of two boots, so a gateway that came
        # back with a different MAC never reached the displays.
        key = (after, reason, self.link.gateway_mac)
        if after != before or (force and key != self._last_link_key):
            self.link.reason = reason
            self._last_link_key = key
            self._emit("lq_link", self._link_payload())

    # -- database writes ------------------------------------------------------

    def _upsert_cup_row(self, live: CupLive) -> None:
        self.db.upsert_cup(live.mac, live.horse, live.last_seen_ts, live.rssi,
                           live.up, live.count, live.raw, live.online)

    def _event(self, type_: str, horse: Optional[int], detail: Optional[Dict[str, Any]]) -> None:
        self.db.insert_event(utc_now_iso(), type_, horse,
                             json.dumps(detail) if detail is not None else None)

    # -- in-process listeners -------------------------------------------------

    def add_listener(self, fn: Callable[[], None]) -> None:
        """Register fn() to be called whenever the bridge's picture changed:
        after every lq_update / lq_snapshot / lq_link emit and at the end of
        set_state() (a phase-only set_state emits nothing on its own, so the
        emits alone would miss it). It carries no payload: the caller reads
        get_snapshot() when it is ready.

        Listeners run on the reader thread, with the bridge's RLock held, so
        they must only set a threading.Event or queue.put_nowait(): never
        block, never take a lock another thread may hold while waiting for
        the bridge, and never call back into the bridge from another thread
        synchronously. An exception in a listener is logged and dropped."""
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception as exc:       # a listener bug must not reach the reader thread
                logger.warning("La Quiniela bridge: listener %r failed: %s", fn, exc)

    # -- SocketIO -------------------------------------------------------------

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.socketio is not None:
            try:
                self.socketio.emit(event, payload, room=LQ_ROOM)
            except Exception:
                logger.exception("La Quiniela bridge: emit %s failed", event)
        self._notify()

    def _emit_cup(self, live: CupLive) -> None:
        self._emit("lq_update", self._cup_payload(live))

    def _emit_snapshot(self) -> None:
        self._emit("lq_snapshot", self.get_snapshot())

    @staticmethod
    def _cup_payload(live: CupLive) -> Dict[str, Any]:
        return {
            "mac": live.mac,
            "horse": live.horse,
            "count": live.count,
            "raw": live.raw,
            "rssi": live.rssi,
            "up": live.up,
            "drop": live.drop,
            "seq": live.seq,
            "online": bool(live.online),
            "hello": bool(live.hello),
            "last_seen": live.last_seen_ts,
        }

    def _link_payload(self) -> Dict[str, Any]:
        link = self.link
        return {
            "port_open": link.port_open,
            "gateway_online": link.gateway_online,
            "in_sync": link.in_sync,
            "reason": link.reason,
            "gateway_mac": link.gateway_mac,
            "phase": link.phase,
            "state_rev": link.state_rev,
            "cups_heard": link.cups_heard,
            "rejects": link.rejects,
            "up_s": link.up_s,
            # How the reader itself is doing, for when the link looks wrong.
            "thread_alive": self.running,
            "last_line_age_s": (None if link.last_line_mono is None
                                else round(self._clock() - link.last_line_mono, 1)),
            "lines_ok": self.stats["lines"],
            "lines_bad": (self.stats["text"] + self.stats["bad_json"]
                          + self.stats["too_long"] + self.stats["unknown_type"]),
            "bytes_rx": self.stats["bytes_rx"],
            "reopens": self.stats["reopens"],
        }

    def get_snapshot(self) -> Dict[str, Any]:
        """The whole picture: the link, DevPi's state (keyed by horse), and
        every cup in the cache by MAC with the horse it claims."""
        with self._lock:
            return {
                "link": self._link_payload(),
                "devpi": {"state_rev": self.state_rev, "phase": self.phase,
                          "scratched": list(self.scratched),
                          "renum": [[f, t] for f, t in self.renum],
                          "results": list(self.results)},
                "cups": [self._cup_payload(self.cups[mac]) for mac in sorted(self.cups)],
            }

    # -- public API -------------------------------------------------------------

    def set_state(self, phase: Any = None, scratched: Any = None, renum: Any = None,
                  results: Any = None) -> int:
        """Change any part of the state (the others stay): validate exactly as
        the gateway does, bump state_rev, persist, send if the port is open,
        emit a snapshot. The same values as the current state are a no-op that
        returns the rev."""
        new_phase = self.phase if phase is None else P.validate_phase(phase)
        new_scr = list(self.scratched) if scratched is None else P.validate_scratched(scratched)
        new_renum = list(self.renum) if renum is None else P.validate_renum(renum)
        new_results = list(self.results) if results is None else P.validate_results(results)
        with self._lock:
            if (new_phase == self.phase and new_scr == self.scratched
                    and new_renum == self.renum and new_results == self.results):
                return self.state_rev
            changed = [name for name, before, after in (
                ("phase", self.phase, new_phase), ("scratched", self.scratched, new_scr),
                ("renum", self.renum, new_renum), ("results", self.results, new_results)) if before != after]
            self.phase, self.scratched, self.renum, self.results = new_phase, new_scr, new_renum, new_results
            self.state_rev += 1
            self._persist()
            self._event("state_set", None, {"rev": self.state_rev, "phase": self.phase,
                                            "scratched": list(self.scratched),
                                            "renum": [[f, t] for f, t in self.renum],
                                            "results": list(self.results), "changed": changed})
            self._send_state()
            self._set_link(reason="status", in_sync=self._compute_sync())
            self._emit_snapshot()
            return self.state_rev

    def forget_cups(self, mac_prefix: Optional[str] = None, quiet: bool = False) -> int:
        """Drop cups from the cache (all, or those whose MAC starts with
        mac_prefix), in memory and in lq_cups. The cache decides nothing, so
        this changes nothing but what the admin page lists as offline; a cup
        that is still talking is back within a packet. Returns how many
        went."""
        with self._lock:
            macs = [m for m in self.cups if not mac_prefix or m.startswith(mac_prefix.upper())]
            for mac in macs:
                del self.cups[mac]
            dropped = self.db.delete_cups(mac_prefix)
            if macs or dropped:
                self._event("cups_forgotten", None, {"prefix": mac_prefix, "count": len(macs)})
                if not quiet:
                    logger.info("La Quiniela bridge: %d cup(s) dropped from the cache", len(macs))
                self._emit_snapshot()
            return len(macs)

    def set_gateway_debug(self, on: bool) -> bool:
        """Flip the gateway's human-readable output. True if the line was sent."""
        return self._send_line(P.build_debug_line(bool(on)))

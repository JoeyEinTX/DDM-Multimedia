# la_quiniela/bridge.py - The DevPi end of the gateway's serial link
#
# A daemon thread reads the gateway's JSON lines, keeps a live picture of the
# cups in memory, writes the cups / telemetry / events tables sparingly (DevPi
# runs on an SD card), and emits lq_* SocketIO events to the "lq" room. It
# writes roster and state lines down, answers the gateway's hello, and
# re-sends whenever a status line shows the gateway out of sync. Nothing here
# may take Flask down: every serial failure is caught, logged and retried.
#
# Conventions follow la_subasta: a module-level init called from main.py, a
# stub-able socketio, raw sqlite3, plain threads. SocketIO runs in threading
# mode in this app (no eventlet/gevent), so socketio.emit() from this thread
# is the same path main.py's odds poller uses.
#
# Cup numbers are 1-based everywhere in this file. The only conversions are
# protocol.wire_to_cup() / cup_to_wire(), called where a line is read or built.

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from la_quiniela import protocol as P
from la_quiniela.models import LqDb, default_db_path, utc_now_iso

logger = logging.getLogger(__name__)

LQ_ROOM = "lq"

# Configuration keys, their defaults, and the environment override DDM_<key>.
DEFAULTS: Dict[str, Any] = {
    "LQ_BRIDGE_ENABLED": True,
    "LQ_SERIAL_PORT": "",
    "LQ_SERIAL_BAUD": 115200,
    "LQ_HEARTBEAT_LOG_S": 10,
    "LQ_CUP_OFFLINE_S": 6,
    "LQ_GATEWAY_OFFLINE_S": 12,
    "LQ_DEV_ENDPOINTS": False,
}

# Fixed timing. The gateway side of each is documented in firmware/quiniela/README.md.
READ_TIMEOUT_S = 1.0          # serial read timeout, so the thread notices a stop request
RETRY_S = 5.0                 # port open / reopen retry interval
PORT_LOG_MIN_S = 60.0         # repeated port failures are logged at most this often
TICK_S = 1.0                  # timer check interval
RESEND_MIN_S = 2.0            # at most one reconcile re-send per this
HELLO_EVENT_MIN_S = 10.0      # gateway_hello event at most this often
PER_CUP_EVENT_MIN_S = 10.0    # cup_claim_mismatch / roster_mismatch per cup at most this often
PENDING_MAX = 4 * P.MAX_LINE_BYTES   # partial-line buffer ceiling

LINK_REASONS = ("boot", "reboot", "online", "offline", "port_closed", "status",
                "protocol_mismatch", "reset")

# Every MAC the cup simulator invents starts with this. Nothing real does: it
# is a locally-administered address, and no ESP32 ships with one.
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

def pyserial_factory(port: str, baud: int, timeout: float):
    """Open the gateway's port the careful way.

    Built unopened, DTR and RTS held low, then opened: a USB adapter pulses
    those lines on open and that resets an ESP32, and the promise is that a
    DevPi service restart does not reboot the gateway. exclusive=True stops a
    second copy of the app opening the same port."""
    import serial
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.write_timeout = 1.0
    for attr in ("dtr", "rts"):
        try:
            setattr(ser, attr, False)
        except Exception:           # a virtual port may refuse the control lines
            pass
    try:
        ser.exclusive = True
    except Exception:               # not every platform supports it
        pass
    ser.open()
    return ser


# -----------------------------------------------------------------------------
# Live records
# -----------------------------------------------------------------------------

@dataclass
class CupLive:
    mac: str
    cup: Optional[int] = None            # 1-based, None = not in the roster
    count: Optional[int] = None
    raw: Optional[int] = None
    rssi: Optional[int] = None
    up: Optional[int] = None
    drop: Optional[int] = None
    seq: Optional[int] = None
    online: bool = False
    last_seen_mono: Optional[float] = None
    last_seen_ts: Optional[str] = None
    last_row_mono: Optional[float] = None    # when the last telemetry row was written
    last_row_count: Optional[int] = None     # the token count in that row
    last_claim_event_mono: Optional[float] = None
    last_mismatch_event_mono: Optional[float] = None


@dataclass
class LinkLive:
    port_open: bool = False
    gateway_online: bool = False
    in_sync: bool = False
    reason: str = "boot"
    gateway_mac: Optional[str] = None
    phase: Optional[int] = None
    state_rev: int = 0                   # what the gateway last reported
    roster_rev: int = 0
    cups_heard: int = 0
    rejects: int = 0
    up_s: Optional[int] = None
    last_line_mono: Optional[float] = None
    last_hello_event_mono: Optional[float] = None
    last_resend_mono: Optional[float] = None


# -----------------------------------------------------------------------------
# The bridge
# -----------------------------------------------------------------------------

class LqBridge:

    def __init__(self, settings: Optional[Dict[str, Any]] = None, db_path: Optional[str] = None,
                 serial_factory: Optional[Callable[[str, int, float], Any]] = None,
                 socketio=None, clock: Optional[Callable[[], float]] = None):
        self.settings = dict(DEFAULTS)
        self.settings.update(settings or {})
        self.socketio = socketio
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
        self.stats = {"lines": 0, "text": 0, "bad_json": 0, "too_long": 0,
                      "unknown_type": 0, "sent": 0}

        # DevPi-owned state, 1-based cups. Revs start at 0 and only go up,
        # including across a reset_link(), so a rev never says whether DevPi
        # actually holds anything. has_state / has_roster say that.
        self.state_rev = 0
        self.has_state = False
        self.has_roster = False
        self.phase: int = int(P.Phase.PRE_RACE)
        self.horses: Dict[int, int] = {cup: 0 for cup in P.CUP_NUMBERS}
        self.scratched: Dict[int, bool] = {cup: False for cup in P.CUP_NUMBERS}
        self.roster_rev = 0
        self.roster: Dict[int, str] = {}      # cup -> MAC, filled slots only

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
        self.state_rev = int(row["state_rev"] or 0)
        self.roster_rev = int(row["roster_rev"] or 0)
        if row["state_json"]:
            try:
                data = json.loads(row["state_json"])
                self.phase = int(data["phase"])
                self.horses = {cup: int(data["horses"].get(str(cup), 0)) for cup in P.CUP_NUMBERS}
                self.scratched = {cup: bool(data["scratched"].get(str(cup), False))
                                  for cup in P.CUP_NUMBERS}
                self.has_state = True
            except (ValueError, KeyError, TypeError) as exc:
                logger.error("La Quiniela bridge: persisted state unreadable (%s), "
                             "treating it as unset", exc)
                self.has_state = False
        if row["roster_json"]:
            try:
                data = json.loads(row["roster_json"])
                self.roster = {int(cup): str(mac) for cup, mac in data.items() if mac}
                self.has_roster = True
            except (ValueError, AttributeError, TypeError) as exc:
                logger.error("La Quiniela bridge: persisted roster unreadable (%s), "
                             "treating it as unset", exc)
                self.has_roster = False
                self.roster = {}

    def _persist(self) -> None:
        state_json = json.dumps({
            "phase": self.phase,
            "horses": {str(cup): self.horses[cup] for cup in P.CUP_NUMBERS},
            "scratched": {str(cup): self.scratched[cup] for cup in P.CUP_NUMBERS},
        }) if self.has_state else None
        roster_json = json.dumps({str(cup): mac for cup, mac in self.roster.items()}) \
            if self.has_roster else None
        self.db.save_link_state(self.state_rev, state_json, self.roster_rev, roster_json)

    def _load_cups(self) -> None:
        """Seed the live table from the cups rows so a snapshot right after a
        restart shows the last known values, all offline."""
        for row in self.db.load_cups():
            live = CupLive(mac=row["mac"])
            live.cup = int(row["cup_id"]) if row["cup_id"] is not None else None
            live.count = row["last_count"]
            live.raw = row["last_raw"]
            live.rssi = row["rssi"]
            live.up = row["up_rssi"]
            live.last_seen_ts = row["last_seen"]
            live.online = False
            self.cups[live.mac] = live
        if self.has_roster:
            self._apply_roster_to_live()

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
            self._factory = pyserial_factory
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
        next_tick = self._clock()
        while not self._stop.is_set():
            try:
                if self._port is None:
                    if not self._open_port():
                        self._stop.wait(RETRY_S)
                else:
                    self._read_once()
            except Exception:           # nothing escapes this thread
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
        self._close_port("stop")

    def _open_port(self) -> bool:
        try:
            port = self._factory(self.settings["LQ_SERIAL_PORT"],
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
            with self._lock:
                self._set_link(port_open=False, gateway_online=False, reason="port_closed")
            return False
        with self._lock:
            self._port = port
            self._pending = b""
            self._port_failures = 0
            self._port_fail_logged_mono = None
            logger.info("La Quiniela bridge: %s open", self.settings["LQ_SERIAL_PORT"])
            self._set_link(port_open=True, reason="online")
        return True

    def _close_port(self, reason: str) -> None:
        with self._lock:
            port, self._port = self._port, None
            self._pending = b""
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
                self._set_link(port_open=False, gateway_online=False, reason="port_closed")

    def _read_once(self) -> None:
        port = self._port
        if port is None:                       # closed under us by a failed write
            return
        try:
            chunk = port.readline()
        except (OSError, ValueError) as exc:   # SerialException is an OSError
            logger.warning("La Quiniela bridge: read failed (%s); reopening in %.0f s", exc, RETRY_S)
            self._close_port("port_closed")
            self._stop.wait(RETRY_S)
            return
        if not chunk:
            return
        if not chunk.endswith(b"\n"):          # timeout mid-line: keep the fragment
            self._pending += chunk
            if len(self._pending) > PENDING_MAX:
                self._pending = b""
                self.stats["too_long"] += 1
            return
        raw = self._pending + chunk
        self._pending = b""
        self.handle_raw_line(raw)

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
            elif kind == "cup_hello":
                self._on_cup_hello(msg, now)
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

    def _resolve_cup(self, live: CupLive, reported: Optional[int], now: float) -> None:
        """Who owns cup numbers. With no roster DevPi mirrors the gateway;
        with one, DevPi is right and a disagreeing gateway is told again."""
        if not self.has_roster:
            if reported != live.cup:
                if reported is not None:
                    for other in self.cups.values():
                        if other is not live and other.cup == reported:
                            other.cup = None
                live.cup = reported
        else:
            mine = self._roster_cup_for(live.mac)
            live.cup = mine
            if reported != mine:
                last = live.last_mismatch_event_mono
                if last is None or now - last >= PER_CUP_EVENT_MIN_S:
                    live.last_mismatch_event_mono = now
                    self._event("roster_mismatch", mine, {
                        "mac": live.mac, "devpi_cup": mine, "gateway_cup": reported,
                        "roster_rev": self.roster_rev})
                self._reconcile(now, roster=True, state=self.has_state)

    def _roster_is_simulated(self) -> bool:
        """True when DevPi holds a roster the cup simulator wrote. One
        simulated MAC is enough: a scenario names all twenty of its own."""
        return self.has_roster and any(is_sim_mac(mac) for mac in self.roster.values())

    def _roster_cup_for(self, mac: str) -> Optional[int]:
        for cup, roster_mac in self.roster.items():
            if roster_mac == mac:
                return cup
        return None

    def _on_telem(self, msg: Dict[str, Any], now: float) -> None:
        mac = P.normalize_mac(msg.get("mac"))
        if mac is None:
            return
        live = self._live_for(mac)
        old_cup = live.cup
        unassigned_before = self._unassigned_macs()
        self._resolve_cup(live, P.wire_to_cup(self._int(msg, "cup")), now)
        cup_changed = live.cup != old_cup

        live.count = self._int(msg, "count")
        live.raw = self._int(msg, "raw")
        live.rssi = self._int(msg, "rssi")
        live.up = self._int(msg, "up")
        live.drop = self._int(msg, "drop")
        live.seq = self._int(msg, "seq")
        live.last_seen_mono = now
        live.last_seen_ts = utc_now_iso()
        came_online = not live.online
        live.online = True

        claim = self._int(msg, "claim")
        if claim is not None:
            last = live.last_claim_event_mono
            if last is None or now - last >= PER_CUP_EVENT_MIN_S:
                live.last_claim_event_mono = now
                claimed = P.wire_to_cup(claim) if 0 <= claim < P.NUM_CUPS else None
                self._event("cup_claim_mismatch", live.cup, {
                    "mac": mac, "devpi_cup": live.cup, "claimed_cup": claimed,
                    "claimed_wire_id": claim})

        if live.cup is None:
            # An unassigned cup: it exists in cups with a NULL id and in the
            # snapshot's unassigned list, and never gets a telemetry row.
            self._upsert_cup_row(live)
            if cup_changed or came_online or self._unassigned_macs() != unassigned_before:
                self._emit_snapshot()
            return

        if came_online:
            self._event("cup_online", live.cup, {"mac": mac})

        # A telemetry row when the count changed, when the heartbeat interval
        # has passed, or when the cup comes back; the cups row and lq_update at
        # those moments and when the cup number itself changed.
        count_changed = live.count != live.last_row_count
        heartbeat_due = (live.last_row_mono is None
                         or now - live.last_row_mono >= float(self.settings["LQ_HEARTBEAT_LOG_S"]))
        row_due = count_changed or heartbeat_due or came_online
        if row_due:
            reason = "change" if count_changed else "heartbeat"
            self.db.insert_telemetry(live.last_seen_ts, live.cup, mac, live.raw, live.count,
                                     live.seq, live.drop, live.rssi, live.up, reason)
            live.last_row_mono = now
            live.last_row_count = live.count
        if row_due or cup_changed:
            self._upsert_cup_row(live)
            self._emit_cup(live.cup, live)
        if cup_changed:
            self._emit_snapshot()      # a slot changed hands: the whole table is the honest picture

    def _on_cup_hello(self, msg: Dict[str, Any], now: float) -> None:
        mac = P.normalize_mac(msg.get("mac"))
        if mac is None:
            return
        live = self._live_for(mac)
        unassigned_before = self._unassigned_macs()
        self._resolve_cup(live, P.wire_to_cup(self._int(msg, "cup")), now)
        live.last_seen_mono = now
        live.last_seen_ts = utc_now_iso()
        came_online = not live.online
        live.online = True
        self._event("cup_hello", live.cup, {"mac": mac})
        self._upsert_cup_row(live)
        if live.cup is None:
            if self._unassigned_macs() != unassigned_before or came_online:
                self._emit_snapshot()
        elif came_online:
            self._event("cup_online", live.cup, {"mac": mac})
            self._emit_cup(live.cup, live)

    def _on_hello(self, msg: Dict[str, Any], now: float) -> None:
        version = self._int(msg, "v")
        mac = P.normalize_mac(msg.get("mac"))
        if mac:
            self.link.gateway_mac = mac
        if version != P.LINE_PROTO_VERSION:
            logger.error("La Quiniela bridge: gateway speaks line protocol v%s, this bridge v%d; "
                         "sending nothing", version, P.LINE_PROTO_VERSION)
            self._set_link(reason="protocol_mismatch", force=True, in_sync=False)
            return
        # A hello means the gateway knows nothing: it has no state and no roster.
        self.link.state_rev = 0
        self.link.roster_rev = 0
        last = self.link.last_hello_event_mono
        if last is None or now - last >= HELLO_EVENT_MIN_S:
            self.link.last_hello_event_mono = now
            self._event("gateway_hello", None, {"mac": mac, "v": version,
                                                 "proto": self._int(msg, "proto")})
        # A simulator session leaves its fake roster in the database, and the
        # next hello would hand it to whatever gateway said it. A real gateway
        # given fake MACs owns no real cups at all: every real cup comes back
        # as -1 and sits on its MAC screen with nothing on it to explain why.
        # So the moment a real gateway turns up holding a simulated roster,
        # throw the roster away and answer with nothing.
        if self._roster_is_simulated() and not is_sim_mac(mac):
            logger.warning("La Quiniela bridge: the stored roster is a simulator roster and the "
                           "gateway that just said hello is not the simulator (%s); discarding it",
                           mac or "no MAC")
            self.reset_link("sim_roster_discarded")
        if self.has_roster:
            self._send_roster()
        if self.has_state:
            self._send_state()
        self.link.last_resend_mono = now
        self._set_link(reason="boot", force=True, in_sync=self._compute_sync())

    def _on_status(self, msg: Dict[str, Any], now: float) -> None:
        up_s = self._int(msg, "up_s")
        rebooted = (up_s is not None and self.link.up_s is not None and up_s < self.link.up_s)
        self.link.phase = self._int(msg, "phase")
        self.link.state_rev = self._int(msg, "state_rev") or 0
        self.link.roster_rev = self._int(msg, "roster_rev") or 0
        self.link.cups_heard = self._int(msg, "cups") or 0
        self.link.rejects = self._int(msg, "rejects") or 0
        self.link.up_s = up_s
        if rebooted:
            self._event("gateway_reboot", None, {"up_s": up_s, "gseq": self._int(msg, "gseq")})
        # Reconcile: a roster mismatch sends roster then state, a state
        # mismatch sends state, at most one re-send every RESEND_MIN_S.
        roster_off = self.has_roster and self.link.roster_rev != self.roster_rev
        state_off = self.has_state and self.link.state_rev != self.state_rev
        if roster_off:
            self._reconcile(now, roster=True, state=self.has_state)
        elif state_off:
            self._reconcile(now, roster=False, state=True)
        self._set_link(in_sync=self._compute_sync(), reason="reboot" if rebooted else "status")

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
                    self._event("cup_offline", live.cup, {"mac": live.mac})
                    self.db.set_cup_online(live.mac, False)
                    if live.cup is not None:
                        self._emit_cup(live.cup, live)
                    else:
                        snapshot_dirty = True
            if snapshot_dirty:
                self._emit_snapshot()
            gw_limit = float(self.settings["LQ_GATEWAY_OFFLINE_S"])
            if self.link.gateway_online and self.link.last_line_mono is not None \
                    and now - self.link.last_line_mono >= gw_limit:
                self._set_link(gateway_online=False, in_sync=False, reason="offline")

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

    def _send_roster(self) -> bool:
        return self._send_line(P.build_roster_line(self.roster_rev, self.roster))

    def _send_state(self) -> bool:
        return self._send_line(P.build_state_line(self.state_rev, self.phase,
                                                  self.horses, self.scratched))

    def _reconcile(self, now: float, roster: bool, state: bool) -> bool:
        """Rate-limited re-send used by status and telemetry mismatches."""
        last = self.link.last_resend_mono
        if last is not None and now - last < RESEND_MIN_S:
            return False
        if self._port is None:
            return False
        self.link.last_resend_mono = now
        if roster and self.has_roster:
            self._send_roster()
        if state and self.has_state:
            self._send_state()
        return True

    # -- link bookkeeping -----------------------------------------------------

    def _touch_gateway(self, now: float) -> None:
        self.link.last_line_mono = now
        if not self.link.gateway_online:
            self._set_link(gateway_online=True, reason="online")

    def _compute_sync(self) -> bool:
        """In sync means the gateway agrees about everything DevPi holds. With
        nothing held there is nothing to disagree about, which matters after a
        reset: the revs have moved on but DevPi is claiming nothing."""
        return (self.link.gateway_online
                and (not self.has_state or self.link.state_rev == self.state_rev)
                and (not self.has_roster or self.link.roster_rev == self.roster_rev))

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
        if after != before or (force and self.link.reason != reason):
            self.link.reason = reason
            self._emit("lq_link", self._link_payload())

    # -- database writes ------------------------------------------------------

    def _upsert_cup_row(self, live: CupLive) -> None:
        horse = self.horses.get(live.cup) if live.cup is not None else None
        self.db.upsert_cup(live.mac, live.cup, horse or None, live.last_seen_ts, live.rssi,
                           live.up, live.count, live.raw, live.online)

    def _event(self, type_: str, cup: Optional[int], detail: Optional[Dict[str, Any]]) -> None:
        self.db.insert_event(utc_now_iso(), type_, cup,
                             json.dumps(detail) if detail is not None else None)

    # -- SocketIO -------------------------------------------------------------

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.socketio is None:
            return
        try:
            self.socketio.emit(event, payload, room=LQ_ROOM)
        except Exception:
            logger.exception("La Quiniela bridge: emit %s failed", event)

    def _emit_cup(self, cup: int, live: Optional[CupLive]) -> None:
        self._emit("lq_update", self._cup_payload(cup, live))

    def _emit_snapshot(self) -> None:
        self._emit("lq_snapshot", self.get_snapshot())

    def _cup_payload(self, cup: int, live: Optional[CupLive]) -> Dict[str, Any]:
        horse = self.horses.get(cup, 0)
        return {
            "cup": cup,
            "mac": live.mac if live else None,
            "horse": horse if horse else None,
            "scratched": bool(self.scratched.get(cup, False)),
            "count": live.count if live else None,
            "raw": live.raw if live else None,
            "rssi": live.rssi if live else None,
            "up": live.up if live else None,
            "drop": live.drop if live else None,
            "online": bool(live.online) if live else False,
            "last_seen": live.last_seen_ts if live else None,
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
            "roster_rev": link.roster_rev,
            "cups_heard": link.cups_heard,
            "rejects": link.rejects,
            "up_s": link.up_s,
        }

    def _mac_by_cup(self) -> Dict[int, str]:
        if self.has_roster:
            return dict(self.roster)
        return {live.cup: live.mac for live in self.cups.values() if live.cup is not None}

    def _unassigned_macs(self) -> List[str]:
        return sorted(live.mac for live in self.cups.values() if live.cup is None and live.online)

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            mac_by_cup = self._mac_by_cup()
            cups = []
            for cup in P.CUP_NUMBERS:
                mac = mac_by_cup.get(cup)
                cups.append(self._cup_payload(cup, self.cups.get(mac) if mac else None))
            unassigned = [{"mac": mac, "last_seen": self.cups[mac].last_seen_ts}
                          for mac in self._unassigned_macs()]
            return {
                "link": self._link_payload(),
                "devpi": {"state_rev": self.state_rev, "roster_rev": self.roster_rev,
                          "phase": self.phase, "has_state": self.has_state,
                          "has_roster": self.has_roster},
                "cups": cups,
                "unassigned": unassigned,
            }

    # -- public API (1-based cups) ---------------------------------------------

    def set_state(self, phase: int, horses: List[int], scratched: List[Any]) -> int:
        """Validate exactly as the gateway does, bump state_rev, persist, send
        if the port is open, emit lq_update for every cup that changed. The
        same values as the current state are a no-op that returns the rev."""
        phase_i, horses_by_cup, scratched_by_cup = P.validate_state(phase, horses, scratched)
        with self._lock:
            if (self.has_state and phase_i == self.phase
                    and horses_by_cup == self.horses and scratched_by_cup == self.scratched):
                return self.state_rev
            changed = [cup for cup in P.CUP_NUMBERS
                       if horses_by_cup[cup] != self.horses[cup]
                       or scratched_by_cup[cup] != self.scratched[cup]]
            self.phase = phase_i
            self.horses = horses_by_cup
            self.scratched = scratched_by_cup
            self.state_rev += 1
            self.has_state = True
            self._persist()
            self.db.set_cup_horses({cup: (self.horses[cup] or None) for cup in P.CUP_NUMBERS})
            self._event("state_set", None, {"rev": self.state_rev, "phase": self.phase,
                                            "changed_cups": changed})
            self._send_state()
            self._set_link(reason="status", in_sync=self._compute_sync())
            mac_by_cup = self._mac_by_cup()
            for cup in changed:
                mac = mac_by_cup.get(cup)
                self._emit_cup(cup, self.cups.get(mac) if mac else None)
            return self.state_rev

    def set_roster(self, macs: List[Optional[str]]) -> int:
        """Validate, bump roster_rev, persist, rewrite cup_id in cups, send
        roster then state, emit a fresh snapshot to the room."""
        by_cup = P.validate_roster(macs)
        with self._lock:
            self.roster = by_cup
            self.roster_rev += 1
            self.has_roster = True
            self._persist()
            self.db.rewrite_cup_ids(self.roster)
            self._apply_roster_to_live()
            self.db.set_cup_horses({cup: (self.horses[cup] or None) for cup in P.CUP_NUMBERS})
            self._event("roster_set", None, {"rev": self.roster_rev,
                                             "macs": {str(c): m for c, m in self.roster.items()}})
            self._send_roster()
            if self.has_state:
                self._send_state()
            self.link.last_resend_mono = self._clock()
            self._set_link(reason="status", in_sync=self._compute_sync())
            self._emit_snapshot()
            return self.roster_rev

    def _apply_roster_to_live(self) -> None:
        for live in self.cups.values():
            live.cup = self._roster_cup_for(live.mac)
        for cup, mac in self.roster.items():
            self._live_for(mac).cup = cup

    def adopt_roster(self) -> int:
        """Turn the cup numbers currently mirrored from the gateway into
        DevPi's first roster. When two MACs claim one cup the most recently
        heard one wins."""
        with self._lock:
            chosen: Dict[int, CupLive] = {}
            for live in self.cups.values():
                if live.cup is None:
                    continue
                current = chosen.get(live.cup)
                if current is None or (live.last_seen_mono or -1) > (current.last_seen_mono or -1):
                    chosen[live.cup] = live
            macs = [chosen[cup].mac if cup in chosen else "" for cup in P.CUP_NUMBERS]
            return self.set_roster(macs)

    def reset_link(self, reason: str = "manual") -> Dict[str, int]:
        """Forget DevPi's roster and state and go back to mirroring the gateway.

        This is how a simulator session is thrown away. The revs still only
        ever increase, so a gateway can never mistake the reset for an older
        roster; what changes is that DevPi stops claiming to hold one, and
        answers the next hello with nothing.

        Nothing is sent to the gateway. A gateway that already holds a roster
        keeps it until it is power-cycled, because there is no line in the
        protocol that means "forget what I told you".

        Cup numbers and horses are cleared on every cups row. Rows for
        simulated cups are deleted outright. Telemetry and event history are
        left alone, and one lq_reset event records what happened."""
        with self._lock:
            self.has_state = False
            self.has_roster = False
            self.roster = {}
            self.phase = int(P.Phase.PRE_RACE)
            self.horses = {cup: 0 for cup in P.CUP_NUMBERS}
            self.scratched = {cup: False for cup in P.CUP_NUMBERS}
            self.state_rev += 1
            self.roster_rev += 1
            self._persist()
            dropped = self.db.clear_cup_assignments(SIM_MAC_PREFIX)
            for mac in [m for m in self.cups if is_sim_mac(m)]:
                del self.cups[mac]
            for live in self.cups.values():
                live.cup = None
            result = {"state_rev": self.state_rev, "roster_rev": self.roster_rev,
                      "cups_dropped": dropped}
            self._event("lq_reset", None, dict(result, reason=reason))
            logger.warning("La Quiniela bridge: link reset (%s); roster and state forgotten, "
                           "%d simulated cup row(s) deleted", reason, dropped)
            self.link.in_sync = self._compute_sync()
            self.link.reason = "reset"
            self._emit("lq_link", self._link_payload())
            self._emit_snapshot()
            return result

    def set_gateway_debug(self, on: bool) -> bool:
        """Flip the gateway's human-readable output. True if the line was sent."""
        return self._send_line(P.build_debug_line(bool(on)))

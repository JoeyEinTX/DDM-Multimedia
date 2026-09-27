# la_quiniela/sim/model.py - the emulated gateway and cups, no I/O
#
# GatewaySim mirrors ddm_gateway.ino at protocol v2: silent boot, hello every
# 2 s until a state line, status every 5 s (carrying the cup table) and after
# every applied line, the 500 ms broadcast, a table of every cup heard, the
# debug text lines. CupSim mirrors a betting cup: it owns its horse number
# (set from the scenario as the touch menu would), HELLO (broadcast) until it
# hears a state packet, telemetry every 2 s after, follows renumber pairs,
# and a scale with the real calibration. Everything is driven by tick(now)
# calls and writes its serial output to GatewaySim.out; the runner moves
# those lines to the pty and prints console events from GatewaySim.events.

import random
from typing import Dict, List, Optional, Set, Tuple

from la_quiniela.sim import protocol as W
from la_quiniela.sim.protocol import Phase

# Scale, from the real calibration (tools/hx711_calibrate, README "Scale")
COUNTS_PER_TOKEN = 6212
NOISE_COUNTS = 100              # about +-100 counts of reading noise
OVERSHOOT_PCT = 3.0             # a landing reads ~3% high...
OVERSHOOT_SETTLE_S = 1.0        # ...and settles over about a second

# Cadences (real time, whatever --speed says)
CUP_HELLO_S = 1.0               # HELLO_MS in ddm_cup.ino: broadcast until the gateway's MAC is known
CUP_TELEM_S = 2.0               # TELEMETRY_MS
GW_BROADCAST_S = 0.5            # BROADCAST_MS
GW_STATUS_S = 5.0               # STATUS_MS
GW_HELLO_S = 2.0                # HELLO_MS (gateway hello repeat)
GW_SUMMARY_S = 5.0              # SUMMARY_MS
GW_STALE_S = 3.0                # STALE_MS
GW_FORGET_S = 600.0             # FORGET_MS: a cup silent this long leaves the table
GW_DEMO_STEP_S = 3.0            # DEMO_STEP_MS
BROADCAST_LOSS = 0.01           # a cup misses about one broadcast in a hundred (drop creeps up)

# A fixed address the bridge can recognise. Everything the simulator
# invents starts 02:DD:4D:, and DevPi drops those cups from its cache the
# moment a gateway with any other address says hello.
SIM_MAC_PREFIX = "02:DD:4D:"
GATEWAY_MAC = SIM_MAC_PREFIX + "FF:FF:FF"
NUM_SPARES = 2


def cup_mac(number: int) -> str:
    """Fixed fake MACs: cup 1 is 02:DD:4D:00:00:01 ... cup 20 is ...:14, the
    spares (21 and 22) are ...:15 and ...:16."""
    return "02:DD:4D:00:00:%02X" % number


class CupSim:
    """One betting cup. `number` is the human cup number it was built as
    (1..20, spares 21..22). Its horse is its own (NVS): cups 1..20 come out
    of the box set to their number, the spares to none."""

    def __init__(self, number: int, rng: random.Random, ideal: bool, stagger: float):
        self.number = number
        self.mac = cup_mac(number)
        self.rng = rng
        self.ideal = ideal
        self.stagger = stagger
        self.powered = False
        self.horse = number if number <= 20 else 0    # NVS "horse"; a spare is unset
        self.gateway_known = False               # learned from the first state packet
        self.tokens = 0
        self.tare = rng.randint(-40000, 40000)  # fixed per cup, never re-tared
        self.rssi = rng.randint(-68, -60)
        self.up = rng.randint(-68, -60)
        self.seq = 0
        self.have_seq = False
        self.dropped = 0
        self.renums = 0                          # renumber pairs followed since boot
        self.scratched = False                   # its bit in the last state packet
        self.place: Optional[int] = None         # 0 WIN, 1 PLACE, 2 SHOW from the results, else None
        self.race_state: Optional[int] = None
        self.overshoot = 0.0
        self.overshoot_at: Optional[float] = None
        self.next_hello: Optional[float] = None
        self.next_telem: Optional[float] = None
        self.booted_at: Optional[float] = None

    # -- power ----------------------------------------------------------------

    def power_on(self, now: float) -> None:
        """Boot: forget the link, keep the horse (NVS), tare and tokens."""
        self.powered = True
        self.gateway_known = False
        self.seq = 0
        self.have_seq = False
        self.dropped = 0
        self.booted_at = now
        self.next_hello = now + 0.2 + self.stagger
        self.next_telem = None

    def power_off(self) -> None:
        self.powered = False
        self.gateway_known = False
        self.next_hello = None
        self.next_telem = None

    # -- the horse ------------------------------------------------------------

    def set_horse(self, horse: int, now: Optional[float] = None) -> None:
        """The touch menu's HORSE / serial n<N>: saved, on the screen at
        once, in the next packet (sent straight away)."""
        self.horse = int(horse)
        if now is not None and self.powered:
            if self.gateway_known:
                self.next_telem = now
            else:
                self.next_hello = now

    # -- tokens ---------------------------------------------------------------

    def drop(self, n: int, now: float) -> None:
        self.tokens += int(n)
        if not self.ideal:
            self.overshoot = self.tokens * COUNTS_PER_TOKEN * OVERSHOOT_PCT / 100.0
            self.overshoot_at = now

    def take(self, n: int, now: float) -> int:
        taken = min(int(n), self.tokens)
        self.tokens -= taken
        self.overshoot = 0.0
        return taken

    def sample(self, now: float) -> Tuple[int, int]:
        """(raw, count) the way the firmware reports them: raw is the HX711
        reading minus nothing (tare is the cup's business), count is
        round((raw - tare) / counts_per_token)."""
        raw = self.tare + self.tokens * COUNTS_PER_TOKEN
        if not self.ideal:
            raw += self.rng.randint(-NOISE_COUNTS, NOISE_COUNTS)
            if self.overshoot_at is not None:
                left = 1.0 - (now - self.overshoot_at) / OVERSHOOT_SETTLE_S
                if left > 0:
                    raw += int(self.overshoot * left)
                else:
                    self.overshoot_at = None
        count = int(round((raw - self.tare) / COUNTS_PER_TOKEN))
        return int(raw), max(0, count)

    # -- radio ----------------------------------------------------------------

    def hear_state(self, gseq: int, phase: int, scratched: Set[int],
                   renum: List[Tuple[int, int]], results: List[int], now: float) -> None:
        """A state broadcast reached this cup. Gaps count as drops; a seq that
        went backwards (gateway reboot) resyncs, as in ddm_cup.ino. The first
        one makes the gateway's MAC known (telemetry from now on). A renumber
        pair whose from is my horse makes me its to, pairs walked in order."""
        if self.have_seq and gseq > self.seq + 1:
            self.dropped += gseq - self.seq - 1
        self.seq = gseq
        self.have_seq = True
        if not self.gateway_known:
            self.gateway_known = True
            self.next_telem = now + 0.3 + self.stagger
        for frm, to in renum:
            if frm and frm == self.horse and to and to != frm:
                self.horse = to
                self.renums += 1
        self.race_state = phase
        self.scratched = self.horse in scratched
        self.place = None
        if phase in (int(Phase.WINNER), int(Phase.AFTER_PARTY)) and self.horse:
            for i, h in enumerate(results):
                if h == self.horse:
                    self.place = i

    def wander_rssi(self) -> None:
        if self.ideal:
            return
        self.rssi = max(-75, min(-55, self.rssi + self.rng.randint(-2, 2)))
        self.up = max(-75, min(-55, self.up + self.rng.randint(-2, 2)))


class GatewaySim:
    """The gateway sketch, in Python. Output lines go to .out in order;
    console-worthy happenings go to .events as (kind, text)."""

    def __init__(self, now: float, rng: random.Random, auto_demo: bool = False,
                 ideal: bool = False, mac: str = GATEWAY_MAC):
        self.rng = rng
        self.auto_demo = auto_demo
        self.ideal = ideal
        self.mac = mac
        self.out: List[str] = []
        self.events: List[Tuple[str, str]] = []
        self.applied_log: List[Tuple[str, int]] = []   # ("state"|"debug", rev) in order
        self.boots = 0
        self.boot(now)

    # -- boot / reboot --------------------------------------------------------

    def boot(self, now: float) -> None:
        self.boots += 1
        self.boot_at = now
        self.gseq = 0
        self.phase = int(Phase.PRE_RACE)          # statePkt.raceState at boot
        self.scratched: Set[int] = set()
        self.renum: List[Tuple[int, int]] = []
        self.results: List[int] = [0] * W.RESULT_SLOTS
        self.state_rev = 0
        # the cup table: MAC -> {horse, tok, rssi, up, last_seen, hello}, in the order first heard
        self.cups: Dict[str, Dict] = {}
        self.broadcasting = self.auto_demo
        self.demo = self.auto_demo
        self.demo_step = 0
        self.hello_active = True
        self.debug = False
        self.rejects = 0
        self.next_broadcast = now
        self.next_demo = now
        self.next_hello = now + GW_HELLO_S
        self.next_status = now + GW_STATUS_S
        self.next_summary = now + GW_SUMMARY_S
        self.rx_buf = b""
        self.rx_overflow = False
        self.text("DDM La Quiniela gateway - ESP-NOW <-> serial JSON bridge")
        self.text("proto v%d, line proto v%d, channel 6, cup table %d"
                  % (W.PROTO_VERSION, W.LINE_PROTO_VERSION, W.MAX_CUPS))
        self.text("build: DDM_AUTO_DEMO=%d DDM_DEBUG_TEXT=0" % (1 if self.auto_demo else 0))
        self.text("gateway MAC: %s" % self.mac)
        if self.auto_demo:
            self.text("[demo] on (DDM_AUTO_DEMO build): broadcasting from boot; a JSON state line takes over")
        else:
            self.text("silent: no state broadcast until a JSON state line or a typed state/scratch/renum/results/demo command")
        self.out.append(W.hello_line(self.mac))
        self.events.append(("gateway", "booted, hello sent" + (" (auto-demo build)" if self.auto_demo else ", silent")))

    def reboot(self, now: float) -> None:
        self.boot(now)

    # -- output helpers -------------------------------------------------------

    def text(self, line: str) -> None:
        self.out.append("# " + line)

    def up_s(self, now: float) -> int:
        return int(now - self.boot_at)

    def cups_heard(self, now: float) -> int:
        return sum(1 for c in self.cups.values() if now - c["last_seen"] <= GW_STALE_S)

    def cup_entries(self, now: float) -> List[Dict]:
        return [W.cup_entry(mac, c["horse"], c["tok"], c["rssi"], c["up"], int(round((now - c["last_seen"]) * 1000)))
                for mac, c in self.cups.items()]

    def forget_stale(self, now: float) -> None:
        for mac in [m for m, c in self.cups.items() if now - c["last_seen"] > GW_FORGET_S]:
            del self.cups[mac]

    def emit_status(self, now: float) -> None:
        self.out.append(W.status_line(self.gseq, self.phase, self.state_rev, self.cup_entries(now),
                                      self.rejects, self.up_s(now)))
        self.next_status = now + GW_STATUS_S

    def emit_state_report(self, now: float) -> None:
        self.out.append(W.state_report_line(self.gseq, self.demo, self.mac, self.phase, sorted(self.scratched),
                                            self.renum, self.results, self.cup_entries(now)))

    # -- downlink -------------------------------------------------------------

    def handle_bytes(self, data: bytes) -> None:
        """Feed raw bytes; the line reader is the sketch's: 1024 bytes, then
        discard to the next newline with one err overflow."""
        for i in range(len(data)):
            c = data[i:i + 1]
            if c == b"\n":
                if self.rx_overflow:
                    self.rx_overflow = False
                    self.rx_buf = b""
                    continue
                line = self.rx_buf
                self.rx_buf = b""
                if line.endswith(b"\r"):
                    line = line[:-1]
                if line:
                    self.handle_line(line)
            elif self.rx_overflow:
                continue
            elif len(self.rx_buf) < W.MAX_LINE:
                self.rx_buf += c
            else:
                self.rx_overflow = True
                self.rx_buf = b""
                self.out.append(W.err_line("overflow"))
                self.events.append(("gateway", "rejected a line: overflow"))

    def handle_line(self, line: bytes, now: Optional[float] = None) -> None:
        """One complete line, no newline. JSON if it starts with '{', else a
        typed bench command."""
        if line[:1] == b"{":
            self._handle_json(line, now)
        else:
            self._handle_command(line.decode("utf-8", errors="replace"), now)

    def _handle_json(self, line: bytes, now: Optional[float]) -> None:
        try:
            kind, data = W.parse_downlink(line)
        except W.Rejected as exc:
            self.out.append(W.err_line(exc.msg, line))
            self.events.append(("gateway", "rejected a line: %s (%s)" % (exc.msg, exc.why or line[:40].decode('utf-8', 'replace'))))
            return
        if kind == "state":
            self._apply_state(data, now, "state line")
        elif kind == "debug":
            self.debug = data
            self.text("[debug] text %s (debug line)" % ("on" if data else "off"))
            self.applied_log.append(("debug", 1 if data else 0))
            self.events.append(("gateway", "debug text %s" % ("on" if data else "off")))
            self._emit_status_now(now)
        # unknown t (a v1 roster line, say): ignored without a word

    def _emit_status_now(self, now: Optional[float]) -> None:
        if now is None:
            now = self.boot_at
        self.emit_status(now)

    def _start_broadcast(self, why: str, now: Optional[float]) -> None:
        if not self.broadcasting:
            self.broadcasting = True
            self.text("[bcast] state broadcast on (%s)" % why)
        if now is not None:
            self.next_broadcast = now            # the next pass sends at once

    def _demo_off(self, why: str) -> None:
        if self.demo:
            self.demo = False
            self.text("[demo] off (%s)" % why)

    def _apply_state(self, data: Dict, now: Optional[float], why: str) -> None:
        self.phase = data["st"]
        self.scratched = set(data["scr"])
        self.renum = list(data["renum"])
        self.results = list(data["res"])
        self.state_rev = data["rev"]
        self._demo_off(why)
        self._start_broadcast(why, now)
        self.hello_active = False
        if self.debug:
            self.text("[state] rev %d applied: st %d" % (self.state_rev, self.phase))
        self.applied_log.append(("state", self.state_rev))
        self.events.append(("gateway", "state rev %d applied, %s%s%s%s" % (
            self.state_rev, W.phase_name(self.phase),
            (", scratched %s" % sorted(self.scratched)) if self.scratched else "",
            (", renum %s" % self.renum) if self.renum else "",
            (", results %s" % self.results) if any(self.results) else "")))
        self._emit_status_now(now)

    def _set_renum(self, frm: int, to: int) -> bool:
        pairs = [p for p in self.renum if p[0] != frm]
        if to == 0:
            self.renum = pairs
            return True
        if len(pairs) >= W.RENUM_SLOTS:
            return False
        self.renum = pairs + [(frm, to)]
        return True

    def _handle_command(self, line: str, now: Optional[float]) -> None:
        line = line.strip()
        if not line:
            return
        parts = line.split()
        cmd = parts[0]
        ints = all(p.lstrip("-").isdigit() for p in parts[1:])
        if cmd == "help":
            for l in ("Commands (newline-terminated; every reply starts with '# '):",
                      "  state <0-6>              set raceState  (0 PRE_RACE 1 BETTING_OPEN 2 FINAL_CALL",
                      "                           3 AT_THE_POST 4 RUNNING 5 WINNER 6 AFTER_PARTY)",
                      "  scratch <horse> <0|1>    set/clear horse 1-24 scratched (no replacement)",
                      "  renum <from> <to>        cups at horse <from> become <to> (1-24); <to> 0 removes the pair; 4 pairs at most",
                      "  results <w> <p> <s>      the WIN, PLACE and SHOW horses (0 = not yet); results 0 0 0 clears",
                      "  cups                     dump the cup table (MAC, horse, tokens, signal, age)",
                      "  demo                     toggle demo mode (WINNER with the results walking 1-24 every 3s)",
                      "  debug on|off             human-readable TELEM lines and 5s summary table",
                      "  help                     this text"):
                self.text(l)
        elif cmd == "demo":
            self.demo = not self.demo
            self.text("[demo] %s" % ("on" if self.demo else "off"))
            if self.demo:
                self._start_broadcast("demo command", now)
        elif cmd == "debug" and len(parts) == 2 and parts[1] in ("on", "off"):
            self.debug = parts[1] == "on"
            self.text("[debug] text %s (command)" % parts[1])
        elif cmd == "cups":
            t = self.boot_at if now is None else now
            self.text("CUPS bcast=%s demo=%s rev=%d" % ("on" if self.broadcasting else "off",
                                                        "on" if self.demo else "off", self.state_rev))
            self.text("CUPS mac               horse  tok  rssi  up_rssi  age_ms  status")
            for mac, c in self.cups.items():
                age = int((t - c["last_seen"]) * 1000)
                self.text("CUPS %s %5d %4d  %4d     %4d %7d  %s%s" % (
                    mac, c["horse"], c["tok"], c["rssi"], c["up"], age,
                    "STALE" if age > GW_STALE_S * 1000 else "OK",
                    " (hello: no gateway MAC yet)" if c["hello"] else ""))
            if not self.cups:
                self.text("CUPS (none heard yet)")
        elif cmd == "json":
            self.emit_state_report(self.boot_at if now is None else now)
        elif cmd == "state" and len(parts) == 2 and ints:
            v = int(parts[1])
            if v < W.PHASE_MIN or v > W.PHASE_MAX:
                self.text("ERR state 0-6")
                return
            self._demo_off("state command")
            self.phase = v
            self._start_broadcast("state command", now)
            self.text("OK state=%d" % v)
        elif cmd == "scratch" and len(parts) == 3 and ints:
            a, b = int(parts[1]), int(parts[2])
            if a < 1 or a > W.MAX_HORSE:
                self.text("ERR horse 1-%d" % W.MAX_HORSE); return
            if b not in (0, 1):
                self.text("ERR scratch 0|1"); return
            self._demo_off("scratch command")
            if b:
                self.scratched.add(a)
            else:
                self.scratched.discard(a)
            self._start_broadcast("scratch command", now)
            self.text("OK scratch horse=%d -> %d" % (a, b))
        elif cmd == "renum" and len(parts) == 3 and ints:
            a, b = int(parts[1]), int(parts[2])
            if a < 1 or a > W.MAX_HORSE:
                self.text("ERR from 1-%d" % W.MAX_HORSE); return
            if b < 0 or b > W.MAX_HORSE or b == a:
                self.text("ERR to 0-%d, not %d" % (W.MAX_HORSE, a)); return
            self._demo_off("renum command")
            if not self._set_renum(a, b):
                self.text("ERR no free renum slot (%d in use)" % W.RENUM_SLOTS); return
            self._start_broadcast("renum command", now)
            self.text("OK renum %d -> %d" % (a, b) if b else "OK renum %d removed" % a)
        elif cmd == "results" and len(parts) == 4 and ints:
            vals = [int(p) for p in parts[1:]]
            if any(v < 0 or v > W.MAX_HORSE for v in vals):
                self.text("ERR results 0-%d each" % W.MAX_HORSE); return
            self._demo_off("results command")
            self.results = vals
            self._start_broadcast("results command", now)
            self.text("OK results win=%d place=%d show=%d" % tuple(vals))
        else:
            self.text("ERR unknown command, try: help")

    # -- uplink from the cups -------------------------------------------------

    def recv_packet(self, cup: CupSim, now: float, hello: bool) -> None:
        """A HELLO or telemetry packet from a cup: tracked, reported as a
        telem line, the same either way."""
        raw, count = cup.sample(now)
        entry = self.cups.get(cup.mac)
        if entry is None:
            if len(self.cups) >= W.MAX_CUPS:
                stalest = max(self.cups, key=lambda m: now - self.cups[m]["last_seen"])
                if now - self.cups[stalest]["last_seen"] <= GW_STALE_S:
                    self.text("ERR cup table full (%d fresh cups), %s ignored" % (W.MAX_CUPS, cup.mac))
                    self.out.append(W.telem_line(cup.mac, cup.horse, raw, count, cup.seq, cup.dropped,
                                                 cup.rssi, cup.up, hello))
                    return
                del self.cups[stalest]
            entry = self.cups[cup.mac] = {}
        entry.update({"horse": cup.horse, "tok": count, "rssi": cup.rssi, "up": cup.up,
                      "last_seen": now, "hello": hello})
        self.out.append(W.telem_line(cup.mac, cup.horse, raw, count, cup.seq, cup.dropped,
                                     cup.rssi, cup.up, hello))
        if self.debug:
            self.text("%s mac=%s horse=%d seq=%d dropped=%d rssi=%d up_rssi=%d tokens=%d"
                      % ("HELLO" if hello else "TELEM", cup.mac, cup.horse, cup.seq, cup.dropped,
                         cup.rssi, cup.up, count))

    # -- timers ---------------------------------------------------------------

    def tick(self, now: float, cups: List[CupSim]) -> None:
        if self.demo and now >= self.next_demo:
            self.next_demo = now + GW_DEMO_STEP_S
            self.demo_step += 1
            self.phase = int(Phase.WINNER)
            self.results = [((self.demo_step + k) % W.MAX_HORSE) + 1 for k in range(W.RESULT_SLOTS)]
        if self.broadcasting and now >= self.next_broadcast:
            self.next_broadcast = now + GW_BROADCAST_S
            self.gseq += 1
            for cup in cups:
                if not cup.powered:
                    continue
                if not self.ideal and self.rng.random() < BROADCAST_LOSS:
                    continue
                cup.hear_state(self.gseq, self.phase, self.scratched, self.renum, self.results, now)
        if self.hello_active and now >= self.next_hello:
            self.next_hello = now + GW_HELLO_S
            self.out.append(W.hello_line(self.mac))
        if now >= self.next_status:
            self.forget_stale(now)
            self.emit_status(now)
        if self.debug and now >= self.next_summary:
            self.next_summary = now + GW_SUMMARY_S
            self._summary(now)

    def _summary(self, now: float) -> None:
        self.text("---- CUPS seq=%d state=%d demo=%s rejects=%d heard=%d ----"
                  % (self.gseq, self.phase, "on" if self.demo else "off", self.rejects, self.cups_heard(now)))
        if not self.broadcasting:
            self.text("  (state broadcast OFF: waiting for a JSON state line or a typed command)")
        self.text(" mac               horse  age_ms   drop  rssi  up_rssi  tok  status")
        for mac, c in self.cups.items():
            age = int((now - c["last_seen"]) * 1000)
            self.text(" %s %5d %7d %6d  %4d     %4d %4d  %s" % (mac, c["horse"], age, 0, c["rssi"], c["up"], c["tok"],
                                                             "STALE" if age > GW_STALE_S * 1000 else "OK"))
        if not self.cups:
            self.text("  (no cups yet - waiting for a HELLO)")


def tick_cup(cup: CupSim, gw: GatewaySim, now: float) -> None:
    """A cup's loop(): HELLO broadcasts until a state packet has told it the
    gateway's MAC, telemetry every 2 s after that."""
    if not cup.powered:
        return
    if not cup.gateway_known:
        if cup.next_hello is not None and now >= cup.next_hello:
            cup.next_hello = now + CUP_HELLO_S
            gw.recv_packet(cup, now, hello=True)
        return
    if cup.next_telem is None:
        cup.next_telem = now + 0.3 + cup.stagger
    if now >= cup.next_telem:
        cup.next_telem = now + CUP_TELEM_S
        cup.wander_rssi()
        gw.recv_packet(cup, now, hello=False)

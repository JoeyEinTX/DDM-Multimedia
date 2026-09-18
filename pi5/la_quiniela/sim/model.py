# la_quiniela/sim/model.py - the emulated gateway and cups, no I/O
#
# GatewaySim mirrors ddm_gateway.ino at commit 56b24f8: silent boot, hello
# every 2 s until a state line, status every 5 s and after every applied
# line, the 500 ms broadcast, roster ownership, the debug text lines. CupSim
# mirrors a betting cup: HELLO until acked, telemetry every 2 s, a scale
# with the real calibration. Everything is driven by tick(now) calls and
# writes its serial output to GatewaySim.out; the runner moves those lines
# to the pty and prints console events from GatewaySim.events.
#
# Slots (0..19) are the gateway's own world, so this module works in slots;
# cup numbers for people are converted in sim/protocol.py only.

import random
from typing import Dict, List, Optional, Tuple

from la_quiniela.sim import protocol as W
from la_quiniela.sim.protocol import Phase

# Scale, from the real calibration (tools/hx711_calibrate, README "Scale")
COUNTS_PER_TOKEN = 6212
NOISE_COUNTS = 100              # about +-100 counts of reading noise
OVERSHOOT_PCT = 3.0             # a landing reads ~3% high...
OVERSHOOT_SETTLE_S = 1.0        # ...and settles over about a second

# Cadences (real time, whatever --speed says)
CUP_HELLO_S = 1.0               # HELLO_MS in ddm_cup.ino
CUP_TELEM_S = 2.0               # TELEMETRY_MS
GW_BROADCAST_S = 0.5            # BROADCAST_MS
GW_STATUS_S = 5.0               # STATUS_MS
GW_HELLO_S = 2.0                # HELLO_MS (gateway hello repeat)
GW_SUMMARY_S = 5.0              # SUMMARY_MS
GW_STALE_S = 3.0                # STALE_MS
GW_DEMO_STEP_S = 3.0            # DEMO_STEP_MS
BROADCAST_LOSS = 0.01           # a cup misses about one broadcast in a hundred (drop creeps up)

GATEWAY_MAC = "02:DD:4D:00:00:00"
NUM_SPARES = 2


def cup_mac(number: int) -> str:
    """Fixed fake MACs: cup 1 is 02:DD:4D:00:00:01 ... cup 20 is ...:14, the
    spares (21 and 22) are ...:15 and ...:16."""
    return "02:DD:4D:00:00:%02X" % number


class CupSim:
    """One betting cup. `number` is the human cup number it was built as
    (1..20, spares 21..22); the slot it is known by on the wire is `cup_id`
    and comes from the gateway's ack."""

    def __init__(self, number: int, rng: random.Random, ideal: bool, stagger: float):
        self.number = number
        self.mac = cup_mac(number)
        self.rng = rng
        self.ideal = ideal
        self.stagger = stagger
        self.powered = False
        self.cup_id: Optional[int] = None       # believed wire slot, None = unassigned (0xFF)
        self.tokens = 0
        self.tare = rng.randint(-40000, 40000)  # fixed per cup, never re-tared
        self.rssi = rng.randint(-68, -60)
        self.up = rng.randint(-68, -60)
        self.seq = 0
        self.have_seq = False
        self.dropped = 0
        self.overshoot = 0.0
        self.overshoot_at: Optional[float] = None
        self.next_hello: Optional[float] = None
        self.next_telem: Optional[float] = None
        self.booted_at: Optional[float] = None

    # -- power ----------------------------------------------------------------

    def power_on(self, now: float) -> None:
        """Boot: forget the ID and the link, keep tare and tokens."""
        self.powered = True
        self.cup_id = None
        self.seq = 0
        self.have_seq = False
        self.dropped = 0
        self.booted_at = now
        self.next_hello = now + 0.2 + self.stagger
        self.next_telem = None

    def power_off(self) -> None:
        self.powered = False
        self.cup_id = None
        self.next_hello = None
        self.next_telem = None

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

    def hear_state(self, gseq: int) -> None:
        """A state broadcast reached this cup. Gaps count as drops; a seq that
        went backwards (gateway reboot) resyncs, as in ddm_cup.ino."""
        if self.have_seq and gseq > self.seq + 1:
            self.dropped += gseq - self.seq - 1
        self.seq = gseq
        self.have_seq = True

    def wander_rssi(self) -> None:
        if self.ideal:
            return
        self.rssi = max(-75, min(-55, self.rssi + self.rng.randint(-2, 2)))
        self.up = max(-75, min(-55, self.up + self.rng.randint(-2, 2)))

    def wire_id(self) -> int:
        """cupId as the cup would put it in a packet: 0xFF while unassigned."""
        return 0xFF if self.cup_id is None else self.cup_id


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
        self.applied_log: List[Tuple[str, int]] = []   # ("state"|"roster"|"debug", rev) in order
        self.boots = 0
        self.boot(now)

    # -- boot / reboot --------------------------------------------------------

    def boot(self, now: float) -> None:
        self.boots += 1
        self.boot_at = now
        self.gseq = 0
        self.phase = int(Phase.BETTING_OPEN)      # statePkt.raceState at boot
        self.horse = [0] * W.NUM_CUPS
        self.scr = [0] * W.NUM_CUPS
        self.state_rev = 0
        self.roster_rev = 0
        self.roster: List[Optional[str]] = [None] * W.NUM_CUPS   # slot -> MAC (RAM only)
        self.last_seen: Dict[str, float] = {}                     # MAC -> last packet time
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
        self.text("proto v%d, line proto v%d, channel 6, max cups %d"
                  % (W.PROTO_VERSION, W.LINE_PROTO_VERSION, W.NUM_CUPS))
        self.text("build: DDM_AUTO_DEMO=%d DDM_DEBUG_TEXT=0" % (1 if self.auto_demo else 0))
        self.text("gateway MAC: %s" % self.mac)
        self.text("roster seeded with 0 known cup(s)")
        if self.auto_demo:
            self.text("[demo] on (DDM_AUTO_DEMO build): broadcasting from boot; a JSON state line takes over")
        else:
            self.text("silent: no state broadcast until a JSON state line or a typed state/horse/scratch/demo command")
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
        n = 0
        for mac in self.roster:
            if mac is not None and mac in self.last_seen and now - self.last_seen[mac] <= GW_STALE_S:
                n += 1
        return n

    def emit_status(self, now: float) -> None:
        self.out.append(W.status_line(self.gseq, self.phase, self.state_rev, self.roster_rev,
                                      self.cups_heard(now), self.rejects, self.up_s(now)))
        self.next_status = now + GW_STATUS_S

    def slot_of(self, mac: str) -> Optional[int]:
        for slot, m in enumerate(self.roster):
            if m == mac:
                return slot
        return None

    def _free_slot(self) -> Optional[int]:
        for slot, m in enumerate(self.roster):
            if m is None:
                return slot
        return None

    def _add_cup(self, mac: str) -> Optional[int]:
        """Bench mode only (roster_rev == 0): next free slot for a new MAC."""
        slot = self._free_slot()
        if slot is None:
            self.text("ERR roster full, cup ignored")
            return None
        self.roster[slot] = mac
        self.text("NEWCUP id=%d mac=%s" % (slot, mac))
        octets = mac.split(":")
        self.text("  paste into KNOWN_CUPS[]:  { { %s } },  // cup %d"
                  % (", ".join("0x" + o for o in octets), slot))
        return slot

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
        elif kind == "roster":
            self._apply_roster(data, now)
        elif kind == "debug":
            self.debug = data
            self.text("[debug] text %s (debug line)" % ("on" if data else "off"))
            self.applied_log.append(("debug", 1 if data else 0))
            self.events.append(("gateway", "debug text %s" % ("on" if data else "off")))
            self._emit_status_now(now)
        # unknown t: ignored without a word

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
        self.phase = data["phase"]
        self.horse = list(data["horse"])
        self.scr = list(data["scr"])
        self.state_rev = data["rev"]
        self._demo_off(why)
        self._start_broadcast(why, now)
        self.hello_active = False
        if self.debug:
            self.text("[state] rev %d applied: phase %d" % (self.state_rev, self.phase))
        self.applied_log.append(("state", self.state_rev))
        self.events.append(("gateway", "state rev %d applied, phase %s" % (self.state_rev, W.phase_name(self.phase))))
        self._emit_status_now(now)

    def _apply_roster(self, data: Dict, now: Optional[float]) -> None:
        old = list(self.roster)
        new = list(data["macs"])
        kept = moved = added = left = 0
        self.moved: List[Tuple[str, int]] = []     # (MAC, new slot) re-acked this line
        for slot in range(W.NUM_CUPS):
            mac = new[slot]
            if mac is None:
                continue
            was = old.index(mac) if mac in old else None
            if was == slot:
                kept += 1
            elif was is None:
                added += 1
            else:
                moved += 1
                self.moved.append((mac, slot))
        for mac in old:
            if mac is not None and mac not in new:
                left += 1
                self.last_seen.pop(mac, None)
        self.roster = new
        self.roster_rev = data["rev"]
        self.text("[roster] rev %d applied: %d kept, %d moved (re-acked), %d added, %d left"
                  % (self.roster_rev, kept, moved, added, left))
        self.applied_log.append(("roster", self.roster_rev))
        self.events.append(("gateway", "roster rev %d applied (%d kept, %d moved, %d added, %d left)"
                            % (self.roster_rev, kept, moved, added, left)))
        self._emit_status_now(now)

    def _handle_command(self, line: str, now: Optional[float]) -> None:
        line = line.strip()
        if not line:
            return
        parts = line.split()
        cmd = parts[0]
        if cmd == "help":
            for l in ("Commands (newline-terminated; every reply starts with '# '):",
                      "  state <0-6>              set raceState  (0 PRE_RACE 1 BETTING_OPEN 2 FINAL_CALL",
                      "                           3 AT_THE_POST 4 RUNNING 5 WINNER 6 AFTER_PARTY)",
                      "  horse <cupId> <0-20>     assign horse to cup (0 = unassigned); cupId is 0-based",
                      "  scratch <cupId> <0|1>    set/clear scratched flag",
                      "  roster                   dump MAC-to-ID table",
                      "  demo                     toggle demo mode (horse walk every 3s)",
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
        elif cmd == "roster":
            self.text("ROSTER rev=%d owner=%s bcast=%s demo=%s" % (
                self.roster_rev, "DevPi (roster line)" if self.roster_rev else "KNOWN_CUPS[] + runtime HELLO",
                "on" if self.broadcasting else "off", "on" if self.demo else "off"))
            self.text("ROSTER id mac               source")
            for slot, mac in enumerate(self.roster):
                if mac is not None:
                    self.text("ROSTER %2d %s %s" % (slot, mac, "roster line" if self.roster_rev else "runtime"))
        elif cmd == "state" and len(parts) == 2 and parts[1].lstrip("-").isdigit():
            v = int(parts[1])
            if v < W.PHASE_MIN or v > W.PHASE_MAX:
                self.text("ERR state 0-6")
                return
            self._demo_off("state command")
            self.phase = v
            self._start_broadcast("state command", now)
            self.text("OK state=%d" % v)
        elif cmd == "horse" and len(parts) == 3 and all(p.lstrip("-").isdigit() for p in parts[1:]):
            a, b = int(parts[1]), int(parts[2])
            if a < 0 or a >= W.NUM_CUPS:
                self.text("ERR cupId 0-%d" % (W.NUM_CUPS - 1)); return
            if b < 0 or b > W.MAX_HORSE:
                self.text("ERR horse 0-20"); return
            self._demo_off("horse command")
            self.horse[a] = b
            self._start_broadcast("horse command", now)
            self.text("OK horse cup=%d -> %d" % (a, b))
        elif cmd == "scratch" and len(parts) == 3 and all(p.lstrip("-").isdigit() for p in parts[1:]):
            a, b = int(parts[1]), int(parts[2])
            if a < 0 or a >= W.NUM_CUPS:
                self.text("ERR cupId 0-%d" % (W.NUM_CUPS - 1)); return
            if b not in (0, 1):
                self.text("ERR scratch 0|1"); return
            self._demo_off("scratch command")
            self.scr[a] = b
            self._start_broadcast("scratch command", now)
            self.text("OK scratch cup=%d -> %d" % (a, b))
        else:
            self.text("ERR unknown command, try: help")

    # -- uplink from the cups -------------------------------------------------

    def recv_hello(self, cup: CupSim, now: float) -> None:
        slot = self.slot_of(cup.mac)
        if slot is None and self.roster_rev == 0:
            slot = self._add_cup(cup.mac)
        if slot is not None:
            self.last_seen[cup.mac] = now
            cup.cup_id = slot                 # the ack: the cup adopts it at once
        self.out.append(W.cup_hello_line(-1 if slot is None else slot, cup.mac))
        if self.debug:
            self.text("HELLO cup=%d mac=%s up_rssi=%d" % (-1 if slot is None else slot, cup.mac, cup.up))

    def recv_telem(self, cup: CupSim, now: float) -> None:
        slot = self.slot_of(cup.mac)
        believed = cup.wire_id()
        if slot is None and self.roster_rev == 0:
            slot = self._add_cup(cup.mac)        # gateway rebooted, cup has an old ID: re-adopt
        if slot is not None:
            self.last_seen[cup.mac] = now
        raw, count = cup.sample(now)
        claim = believed if (slot is None or believed != slot) else None
        self.out.append(W.telem_line(-1 if slot is None else slot, cup.mac, raw, count,
                                     cup.seq, cup.dropped, cup.rssi, cup.up, claim))
        if self.debug:
            self.text("TELEM cup=%d horse=%d seq=%d dropped=%d rssi=%d up_rssi=%d tokens=%d"
                      % (-1 if slot is None else slot, self.horse[slot] if slot is not None else 0,
                         cup.seq, cup.dropped, cup.rssi, cup.up, count))
        if slot is not None and believed != slot:
            cup.cup_id = slot                 # the claim-mismatch re-ack: adopted after this packet

    # -- timers ---------------------------------------------------------------

    def tick(self, now: float, cups: List[CupSim]) -> None:
        if self.demo and now >= self.next_demo:
            self.next_demo = now + GW_DEMO_STEP_S
            self.demo_step += 1
            for i in range(W.NUM_CUPS):
                self.horse[i] = ((self.demo_step + i * 5) % 20) + 1
        if self.broadcasting and now >= self.next_broadcast:
            self.next_broadcast = now + GW_BROADCAST_S
            self.gseq += 1
            for cup in cups:
                if not cup.powered:
                    continue
                if not self.ideal and self.rng.random() < BROADCAST_LOSS:
                    continue
                cup.hear_state(self.gseq)
        if self.hello_active and now >= self.next_hello:
            self.next_hello = now + GW_HELLO_S
            self.out.append(W.hello_line(self.mac))
        if now >= self.next_status:
            self.emit_status(now)
        if self.debug and now >= self.next_summary:
            self.next_summary = now + GW_SUMMARY_S
            self._summary(now)

    def _summary(self, now: float) -> None:
        self.text("---- CUPS seq=%d state=%d demo=%s rejects=%d ----"
                  % (self.gseq, self.phase, "on" if self.demo else "off", self.rejects))
        if not self.broadcasting:
            self.text("  (state broadcast OFF: waiting for a JSON state line or a typed command)")
        self.text(" id mac                age_ms   drop  rssi  up_rssi  status")
        any_row = False
        for slot, mac in enumerate(self.roster):
            if mac is None:
                continue
            any_row = True
            if mac not in self.last_seen:
                self.text(" %2d %s       -      -     -        -  NEVER" % (slot, mac))
            else:
                age = int((now - self.last_seen[mac]) * 1000)
                self.text(" %2d %s %7d %6d  %4d     %4d  %s" % (slot, mac, age, 0, 0, 0,
                                                              "STALE" if age > GW_STALE_S * 1000 else "OK"))
        if not any_row:
            self.text("  (no cups yet - waiting for HELLO)")


def tick_cup(cup: CupSim, gw: GatewaySim, now: float) -> None:
    """A cup's loop(): HELLO while unassigned, telemetry once assigned."""
    if not cup.powered:
        return
    if cup.cup_id is None:
        if cup.next_hello is not None and now >= cup.next_hello:
            cup.next_hello = now + CUP_HELLO_S
            gw.recv_hello(cup, now)
            if cup.cup_id is not None:
                cup.next_telem = now + 0.3 + cup.stagger
        return
    if cup.next_telem is None:
        cup.next_telem = now + 0.3 + cup.stagger
    if now >= cup.next_telem:
        cup.next_telem = now + CUP_TELEM_S
        cup.wander_rssi()
        gw.recv_telem(cup, now)

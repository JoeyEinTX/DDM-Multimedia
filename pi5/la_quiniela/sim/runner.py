# la_quiniela/sim/runner.py - runs the world: cups, gateway, pty, scenario,
# operator steps, hand commands, console output and the self-check.

import json
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from la_quiniela.sim import protocol as W
from la_quiniela.sim.model import (
    NUM_SPARES, CupSim, GatewaySim, cup_mac, tick_cup,
)
from la_quiniela.sim.protocol import Phase, cup_to_slot, slot_to_cup
from la_quiniela.sim.scenarios import DESCRIPTIONS, SCENARIOS

LOOP_S = 0.02
OPERATOR_RETRY_S = 5.0     # re-ask DevPi for an operator step that has had no effect


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class VirtualClock:
    """Time that only moves when someone sleeps: tests run scenarios in an
    instant without changing any cadence."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# -----------------------------------------------------------------------------
# Operator drivers: how an operator step gets done
# -----------------------------------------------------------------------------

class HttpOperator:
    """Drives DevPi through the bridge's dev endpoints (LQ_DEV_ENDPOINTS on)."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _post(self, path: str, body: Optional[dict]) -> dict:
        data = json.dumps(body).encode() if body is not None else b"{}"
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError("%s -> HTTP %d: %s" % (path, exc.code, exc.read().decode(errors="replace")[:200]))

    def state(self, phase: int, horses: List[int], scratched: List[int]) -> None:
        self._post("/api/lq/dev/state", {"phase": phase, "horses": horses, "scratched": scratched})

    def roster(self, macs: List[str]) -> None:
        self._post("/api/lq/dev/roster", {"macs": macs})

    def adopt(self) -> None:
        self._post("/api/lq/dev/roster/adopt", None)

    def reset(self, reason: str = "simulator") -> dict:
        """Tell DevPi to forget its roster and state. Returns the new revs."""
        return self._post("/api/lq/dev/reset", {"reason": reason})


class ApiOperator:
    """Drives the bridge in-process through its Python API (tests)."""

    def __init__(self, bridge):
        self.bridge = bridge

    def state(self, phase, horses, scratched):
        self.bridge.set_state(phase, horses, scratched)

    def roster(self, macs):
        self.bridge.set_roster(macs)

    def adopt(self):
        self.bridge.adopt_roster()


# -----------------------------------------------------------------------------
# Things a scenario can yield
# -----------------------------------------------------------------------------

class Wait:
    def __init__(self, seconds: float, min_real: float = 0.0):
        self.seconds = seconds
        self.min_real = min_real


class Until:
    def __init__(self, desc: str, pred: Callable[[], bool], timeout: float):
        self.desc = desc
        self.pred = pred
        self.timeout = timeout


class OperatorStep:
    def __init__(self, kind: str, desc: str, pred: Callable[[], bool], payload: Dict[str, Any]):
        self.kind = kind              # "state" | "roster" | "adopt"
        self.desc = desc
        self.pred = pred
        self.payload = payload


class ScenarioFailed(Exception):
    pass


# -----------------------------------------------------------------------------
# The simulator
# -----------------------------------------------------------------------------

class Simulator:

    def __init__(self, link=None, seed: Optional[int] = None, ideal: bool = False,
                 auto_demo: bool = False, speed: float = 1.0, quiet: bool = False,
                 wire: bool = False, operator=None, operator_timeout: float = 120.0,
                 clock=None, out: Callable[[str], None] = None, settle_s: float = 12.0):
        self.link = link
        self.seed = seed
        self.rng = random.Random(seed)
        self.ideal = ideal
        self.speed = max(0.01, float(speed))
        self.quiet = quiet
        self.wire = wire
        self.operator = operator
        self.operator_timeout = operator_timeout
        self.clock = clock or RealClock()
        self.out = out or (lambda s: print(s, flush=True))
        self.settle_s = settle_s
        self.started_at = self.clock.now()
        # cups announce in number order: cup 1 first, the spares last
        self.cups: Dict[int, CupSim] = {
            n: CupSim(n, self.rng, ideal, stagger=i * 0.09)
            for i, n in enumerate(range(1, 21 + NUM_SPARES))
        }
        self.on_tx: Optional[Callable[[str], None]] = None   # tests: a fake DevPi watching the wire
        self.gw = GatewaySim(self.clock.now(), self.rng, auto_demo=auto_demo, ideal=ideal)
        self.tx_log: List[str] = []           # every line sent (tests)
        self.rx_log: List[bytes] = []         # every line received (tests)
        self.commands: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self.failures: List[str] = []
        self.notes: List[str] = []
        self.expected_events: List[str] = []
        self.intended_finish: Optional[List[int]] = None
        self.empty_cup: Optional[int] = None
        # what the operator is meant to have set (DevPi's state), human cup numbers
        self.intended_phase: int = int(Phase.BETTING_OPEN)
        self.intended_horses: Dict[int, int] = {c: 0 for c in range(1, 21)}
        self.intended_scratched: Dict[int, bool] = {c: False for c in range(1, 21)}
        self.intended_roster: Dict[int, Optional[str]] = {c: None for c in range(1, 21)}
        self.scenario_name: Optional[str] = None
        self.reboot_log_mark: int = 0        # gw.applied_log length at the last reboot
        self._gen = None
        self._pending = None
        self._pending_deadline: Optional[float] = None
        self._pending_started: Optional[float] = None
        self._pending_asked: float = 0.0     # when the operator was last asked
        self._pending_error: Optional[str] = None   # why DevPi last refused the request
        self.result: Optional[bool] = None

    # -- console --------------------------------------------------------------

    def elapsed(self) -> str:
        s = int(self.clock.now() - self.started_at)
        return "[%02d:%02d]" % (s // 60, s % 60)

    def say(self, text: str, important: bool = False) -> None:
        if self.quiet and not important:
            return
        self.out("%s %s" % (self.elapsed(), text))

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.say("scenario: " + text)

    # -- cup-side actions (human cup numbers) ---------------------------------

    def cup(self, number: int) -> CupSim:
        if number not in self.cups:
            raise ValueError("no cup %s (1..20, spares 21 and 22)" % number)
        return self.cups[number]

    def drop(self, number: int, n: int) -> None:
        c = self.cup(number)
        c.drop(n, self.clock.now())
        self.say("cup %d: +%d tokens (now %d)" % (number, n, c.tokens))

    def take(self, number: int, n: int) -> None:
        c = self.cup(number)
        taken = c.take(n, self.clock.now())
        self.say("cup %d: -%d tokens (now %d)" % (number, taken, c.tokens))

    def kill(self, number: int) -> None:
        c = self.cup(number)
        c.power_off()
        self.say("cup %d: power off" % number)

    def boot(self, number: int) -> None:
        c = self.cup(number)
        c.power_on(self.clock.now())
        self.say("cup %d: power on (%s), sending HELLO" % (number, c.mac))

    def boot_spare(self, k: int) -> CupSim:
        if k not in (1, 2):
            raise ValueError("spares are 1 and 2")
        c = self.cup(20 + k)
        c.power_on(self.clock.now())
        self.say("spare %d: power on (%s), sending HELLO" % (k, c.mac))
        return c

    def reboot_gateway(self) -> None:
        # Where the gateway's applied log stood when it went down. Everything
        # after this mark is DevPi noticing the reboot and putting the gateway
        # back on its own, which is the thing worth asserting on.
        self.reboot_log_mark = len(self.gw.applied_log)
        self.gw.reboot(self.clock.now())
        self.say("gateway: REBOOT")

    def tokens_in(self, number: int) -> int:
        return self.cup(number).tokens

    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.cups.values())

    def slot_of_cup(self, number: int) -> int:
        return cup_to_slot(number)

    def move_tokens_to_spare(self, number: int, spare: CupSim) -> None:
        dead = self.cup(number)
        n = dead.tokens
        dead.tokens = 0
        spare.drop(n, self.clock.now())
        self.say("cup %d's %d tokens moved into %s (spare)" % (number, n, spare.mac))

    # -- scenario helpers -----------------------------------------------------

    def wait(self, seconds: float, min_real: float = 0.0) -> Wait:
        return Wait(seconds, min_real)

    def until(self, desc: str, pred: Callable[[], bool], timeout: float = 60.0) -> Until:
        return Until(desc, pred, timeout)

    def expect_event(self, type_: str, cup: Optional[int]) -> None:
        self.expected_events.append("%s%s" % (type_, "" if cup is None else " for cup %d" % cup))

    def expect_no_event(self, type_: str, cup: int) -> None:
        self.expected_events.append("no %s for cup %d" % (type_, cup))

    def assert_equal(self, what: str, a, b) -> None:
        if a != b:
            self.failures.append("%s: %r != %r" % (what, a, b))
            self.say("CHECK FAILED: %s: %r != %r" % (what, a, b), important=True)
        else:
            self.say("check ok: %s" % what)

    def _state_payload(self) -> Dict[str, Any]:
        return {"phase": int(self.intended_phase),
                "horses": [self.intended_horses[c] for c in range(1, 21)],
                "scratched": [1 if self.intended_scratched[c] else 0 for c in range(1, 21)]}

    def _roster_payload(self) -> List[str]:
        return [self.intended_roster[c] or "" for c in range(1, 21)]

    def _state_applied(self) -> bool:
        gw = self.gw
        if gw.state_rev == 0 or gw.phase != int(self.intended_phase):
            return False
        for c in range(1, 21):
            slot = cup_to_slot(c)
            if gw.horse[slot] != self.intended_horses[c]:
                return False
            if bool(gw.scr[slot]) != self.intended_scratched[c]:
                return False
        return True

    def _roster_applied(self) -> bool:
        gw = self.gw
        if gw.roster_rev == 0:
            return False
        return all(gw.roster[cup_to_slot(c)] == self.intended_roster[c] for c in range(1, 21))

    def operator_state(self, desc: str, phase=None, horses: Optional[Dict[int, int]] = None,
                       scratched: Optional[Dict[int, bool]] = None) -> OperatorStep:
        if phase is not None:
            self.intended_phase = int(phase)
        if horses:
            self.intended_horses.update(horses)
        if scratched:
            self.intended_scratched.update(scratched)
        return OperatorStep("state", desc, self._state_applied, self._state_payload())

    def operator_roster(self, desc: str, cup: int, mac: Optional[str]) -> OperatorStep:
        for c, m in self.intended_roster.items():
            if m == mac:
                self.intended_roster[c] = None
        self.intended_roster[cup] = mac
        return OperatorStep("roster", desc, self._roster_applied, {"macs": self._roster_payload()})

    def operator_adopt(self) -> OperatorStep:
        # what adopting should produce: every powered, assigned cup in its slot
        for c in range(1, 21):
            self.intended_roster[c] = None
        for c in self.cups.values():
            if c.powered and c.cup_id is not None:
                self.intended_roster[slot_to_cup(c.cup_id)] = c.mac
        return OperatorStep("adopt", "adopt the roster (POST /api/lq/dev/roster/adopt)",
                            self._roster_applied, {})

    def operator_roster_all(self, desc: str) -> OperatorStep:
        """Put the 20 simulated cups in cups 1..20, by MAC.

        Scenarios start from this rather than from adopt. Adopt only turns the
        cup numbers DevPi has already mirrored from the gateway into DevPi's
        own roster, so it leaves out any cup DevPi had not heard from yet -
        and leaves it out for good, because from then on DevPi owns the roster
        and the gateway reports that cup as -1. DevPi also keeps its roster
        between runs, so one short adopt would spoil every later run. Naming
        all 20 MACs is exact from any starting state."""
        for c in range(1, 21):
            self.intended_roster[c] = self.cups[c].mac
        return OperatorStep("roster", desc, self._roster_applied,
                            {"macs": self._roster_payload()})

    def prologue(self):
        """Every scenario starts the same way: cups power on and get slots,
        DevPi is given the roster, horses 1..20 go to cups 1..20, BETTING_OPEN."""
        for n in range(1, 21):
            self.boot(n)
        # The roster goes down before the cups are checked, not after. DevPi
        # keeps its roster between runs, and while it holds one the gateway
        # hands out no number of its own: any cup outside that roster would
        # wait for a slot that is never coming. Naming all twenty first makes
        # the start of a scenario the same from any DevPi state.
        yield self.operator_roster_all("put the 20 cups in cups 1..20 (POST /api/lq/dev/roster)")
        yield self.until("all 20 cups hold their numbers",
                         lambda: all(self.cups[n].cup_id == cup_to_slot(n) for n in range(1, 21)),
                         timeout=30)
        yield self.operator_state("assign horses 1..20 to cups 1..20 and open betting",
                                  phase=Phase.BETTING_OPEN, horses={c: c for c in range(1, 21)},
                                  scratched={c: False for c in range(1, 21)})

    # -- hand commands ----------------------------------------------------------

    HELP = ("drop CUP N       tokens into a cup (cups are 1..20)\n"
            "take CUP N       tokens out of a cup\n"
            "kill CUP         cup loses power\n"
            "boot CUP         cup powers on (keeps its tokens)\n"
            "boot-spare 1|2   power on a spare cup (unassigned until the roster includes it)\n"
            "reboot-gateway   the gateway reboots\n"
            "cups             table of cups: number, MAC, slot, count, powered\n"
            "help             this text\n"
            "quit             stop the simulator")

    def command(self, line: str) -> None:
        parts = line.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        try:
            if cmd == "help":
                self.out(self.HELP)
            elif cmd == "quit":
                self._stop.set()
            elif cmd == "cups":
                self.out(self.cups_table())
            elif cmd == "reboot-gateway":
                self.reboot_gateway()
            elif cmd == "boot-spare" and len(parts) == 2:
                self.boot_spare(int(parts[1]))
            elif cmd in ("drop", "take") and len(parts) == 3:
                (self.drop if cmd == "drop" else self.take)(int(parts[1]), int(parts[2]))
            elif cmd in ("kill", "boot") and len(parts) == 2:
                (self.kill if cmd == "kill" else self.boot)(int(parts[1]))
            else:
                self.out("? unknown command, try: help")
        except (ValueError, KeyError) as exc:
            self.out("? %s" % exc)

    def cups_table(self) -> str:
        now = self.clock.now()
        rows = [" cup  mac                slot  count  powered"]
        for n, c in sorted(self.cups.items()):
            label = ("%2d" % n) if n <= 20 else ("s%d" % (n - 20))
            slot = "-" if c.cup_id is None else str(slot_to_cup(c.cup_id))
            rows.append(" %3s  %s  %4s  %5d  %s" % (label, c.mac, slot, c.sample(now)[1] if c.powered else c.tokens,
                                                    "yes" if c.powered else "no"))
        return "\n".join(rows)

    def _stdin_reader(self) -> None:
        try:
            for line in sys.stdin:
                self.commands.put(line)
                if line.strip().lower() == "quit":
                    break
        except (OSError, ValueError):
            pass

    # -- the loop -------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run(self, scenario: Optional[str] = None, stdin_commands: bool = False,
            duration: Optional[float] = None, check: Optional[Callable[[], dict]] = None,
            tolerance: Optional[int] = None) -> int:
        """Run until the scenario ends (then check), until `duration`, or
        until quit. Returns the exit code: 0 for PASS or a clean stop, 1 for
        FAIL."""
        if scenario is not None:
            if scenario not in SCENARIOS:
                raise ValueError("unknown scenario %r; --list shows them" % scenario)
            self.scenario_name = scenario
            self._gen = SCENARIOS[scenario](self)
            self.say("scenario %s: %s" % (scenario, DESCRIPTIONS[scenario]), important=True)
        if scenario is None:                     # no script: power the cups and take commands
            for n in range(1, 21):
                self.boot(n)
        if stdin_commands:
            threading.Thread(target=self._stdin_reader, name="lq-sim-stdin", daemon=True).start()
        end_at = None if duration is None else self.clock.now() + duration
        scenario_done = scenario is None
        try:
            while not self._stop.is_set():
                now = self.clock.now()
                self._pump(now)
                if not scenario_done:
                    try:
                        scenario_done = not self._advance(now)
                    except ScenarioFailed as exc:
                        self.failures.append(str(exc))
                        self.say("SCENARIO FAILED: %s" % exc, important=True)
                        scenario_done = True
                    if scenario_done:
                        if not self.failures:
                            self.say("scenario %s finished" % scenario, important=True)
                        break
                if end_at is not None and now >= end_at:
                    break
                self.clock.sleep(LOOP_S)
        finally:
            pass
        if scenario is not None:
            self._print_expectations()
            if check is not None:
                ok = self.check(check, tolerance)
                self.result = ok and not self.failures
                self.out("PASS" if self.result else "FAIL")
                return 0 if self.result else 1
            self.result = not self.failures
            if self.failures:
                self.out("FAIL")
                return 1
        return 0

    def _pump(self, now: float) -> None:
        """One pass: bridge lines in, cups and gateway tick, lines out."""
        if self.link is not None:
            for raw in self.link.drain():
                self.rx_log.append(raw)
                if self.wire:
                    self.out("%s RX %s" % (self.elapsed(), raw.decode("utf-8", errors="replace")))
                self.gw.handle_line(raw, now)
        while True:
            try:
                self.command(self.commands.get_nowait())
            except queue.Empty:
                break
        for c in self.cups.values():
            tick_cup(c, self.gw, now)
        self.gw.tick(now, list(self.cups.values()))
        for kind, text in self.gw.events:
            self.say("%s: %s" % (kind, text))
        self.gw.events.clear()
        lines, self.gw.out = self.gw.out, []
        for line in lines:
            self.tx_log.append(line)
            if self.wire:
                self.out("%s TX %s" % (self.elapsed(), line))
            if self.link is not None:
                self.link.write_line(line)
            if self.on_tx is not None:
                self.on_tx(line)

    def _advance(self, now: float) -> bool:
        """Move the scenario on. False when it has finished."""
        if self._pending is not None:
            p = self._pending
            if isinstance(p, Wait):
                if now < self._pending_deadline:
                    return True
            elif isinstance(p, Until):
                if p.pred():
                    self.say("ok: %s" % p.desc)
                elif now >= self._pending_deadline:
                    raise ScenarioFailed("timed out after %.0f s waiting for: %s" % (p.timeout, p.desc))
                else:
                    return True
            elif isinstance(p, OperatorStep):
                if p.pred():
                    self.say("operator step done: %s" % p.desc, important=self.operator is None)
                elif now >= self._pending_deadline:
                    raise ScenarioFailed("no %s line from DevPi within %.0f s for: %s%s"
                                         % (p.kind if p.kind != "adopt" else "roster",
                                            self.operator_timeout, p.desc,
                                            "" if not self._pending_error
                                            else " (last error: %s)" % self._pending_error))
                else:
                    # Nothing came back. Ask again: DevPi may not have had the
                    # port open when the first request went out, or (for adopt)
                    # may not yet have heard from every cup.
                    if (self.operator is not None
                            and now - self._pending_asked >= OPERATOR_RETRY_S):
                        self._dispatch_operator(p, now, again=True)
                    return True
            self._pending = None
        try:
            step = next(self._gen)
        except StopIteration:
            return False
        self._pending = step
        self._pending_started = now
        if isinstance(step, Wait):
            self._pending_deadline = now + max(step.seconds / self.speed, step.min_real)
        elif isinstance(step, Until):
            self._pending_deadline = now + step.timeout
        elif isinstance(step, OperatorStep):
            self._pending_deadline = now + self.operator_timeout
            self._pending_asked = now
            self._pending_error = None
            if step.pred():
                return True                       # already in that state
            self._dispatch_operator(step, now)
        return True

    def _dispatch_operator(self, step: "OperatorStep", now: float, again: bool = False) -> None:
        """Ask the operator (or the human at the console) for one step. Called
        again every OPERATOR_RETRY_S while the step has had no effect: a
        request made before DevPi opened the port is simply gone, and an adopt
        is only as good as the cups DevPi had heard from when it ran."""
        self._pending_asked = now
        if self.operator is None:
            if not again:
                self.say("WAITING FOR OPERATOR: %s" % step.desc, important=True)
            return
        self.say("operator (driven%s): %s" % (", again" if again else "", step.desc))
        try:
            if step.kind == "state":
                self.operator.state(**step.payload)
            elif step.kind == "roster":
                self.operator.roster(step.payload["macs"])
            else:
                self.operator.adopt()
            self._pending_error = None
        except Exception as exc:
            # DevPi may not be up yet: LQ_SIMULATOR.md starts this window
            # first. Keep the request pending and try again rather than
            # failing the scenario; the operator timeout is the real limit.
            self._pending_error = "%s" % exc
            if not again:
                self.say("DevPi did not take the request (%s); retrying every %.0f s"
                         % (exc, OPERATOR_RETRY_S), important=True)

    # -- expectations and the self-check --------------------------------------

    def expectation(self) -> Dict[str, Any]:
        """What DevPi should end up believing, by human cup number."""
        now = self.clock.now()
        by_mac = {c.mac: c for c in self.cups.values()}
        cups = {}
        for n in range(1, 21):
            mac = self.gw.roster[cup_to_slot(n)]
            c = by_mac.get(mac) if mac else None
            cups[n] = {
                "mac": mac,
                "count": (c.tokens if c else None),
                "online": bool(c and c.powered and c.cup_id == cup_to_slot(n)),
            }
        return {"cups": cups, "in_sync": True, "events": list(self.expected_events),
                "intended_finish": self.intended_finish, "empty_cup": self.empty_cup}

    def _print_expectations(self) -> None:
        exp = self.expectation()
        lines = ["", "EXPECTED RESULTS (%s%s)" % (self.scenario_name, "" if self.seed is None else ", seed %s" % self.seed),
                 " cup  mac                count  online"]
        for n in range(1, 21):
            e = exp["cups"][n]
            lines.append(" %3d  %-17s  %5s  %s" % (n, e["mac"] or "-", "-" if e["count"] is None else e["count"],
                                                   "yes" if e["online"] else "no"))
        lines.append(" total tokens in play: %d" % sum(e["count"] or 0 for e in exp["cups"].values()))
        lines.append(" link in_sync: true")
        if exp["events"]:
            lines.append(" events that should have been logged: " + ", ".join(exp["events"]))
        if exp["intended_finish"]:
            lines.append(" intended finish order (win, place, show): %s; cup %s is empty"
                         % (", ".join("cup %d" % c for c in exp["intended_finish"]), exp["empty_cup"]))
        for f in self.failures:
            lines.append(" FAILED: " + f)
        self.out("\n".join(lines))

    def compare(self, snapshot: dict, tolerance: int) -> List[str]:
        exp = self.expectation()
        problems = []
        cups = {c["cup"]: c for c in snapshot.get("cups", [])}
        for n in range(1, 21):
            e = exp["cups"][n]
            s = cups.get(n)
            if s is None:
                problems.append("cup %d missing from the snapshot" % n)
                continue
            if (s.get("mac") or None) != e["mac"]:
                problems.append("cup %d: mac %s, expected %s" % (n, s.get("mac"), e["mac"]))
            if bool(s.get("online")) != e["online"]:
                problems.append("cup %d: online %s, expected %s" % (n, s.get("online"), e["online"]))
            if e["count"] is not None:
                got = s.get("count")
                if got is None or abs(int(got) - e["count"]) > tolerance:
                    problems.append("cup %d: count %s, expected %d%s" % (n, got, e["count"],
                                                                        "" if tolerance == 0 else " (+-%d)" % tolerance))
        if not snapshot.get("link", {}).get("in_sync"):
            problems.append("link in_sync is %s, expected true" % snapshot.get("link", {}).get("in_sync"))
        return problems

    def check(self, fetch: Callable[[], dict], tolerance: Optional[int] = None) -> bool:
        """Poll DevPi's snapshot until it matches or settle_s runs out; print
        one line per mismatch."""
        tol = (0 if self.ideal else 1) if tolerance is None else tolerance
        deadline = self.clock.now() + self.settle_s
        problems: List[str] = ["no snapshot yet"]
        while True:
            self._pump(self.clock.now())
            try:
                snap = fetch()
                problems = self.compare(snap, tol)
            except Exception as exc:
                problems = ["could not fetch the snapshot: %s" % exc]
            if not problems or self.clock.now() >= deadline:
                break
            self.clock.sleep(0.5)
        for p in problems:
            self.out("MISMATCH: " + p)
        return not problems


def http_snapshot(base_url: str) -> Callable[[], dict]:
    url = base_url.rstrip("/") + "/api/lq/snapshot"

    def fetch() -> dict:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    return fetch

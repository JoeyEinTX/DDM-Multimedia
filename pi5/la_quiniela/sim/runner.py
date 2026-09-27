# la_quiniela/sim/runner.py - runs the world: cups, gateway, pty, scenario,
# operator steps, hand commands, console output and the self-check.
#
# Protocol v2: every cup knows its own horse (cups 1..20 come up as horses
# 1..20, the spares as none until the scenario sets them), so there is no
# roster and no adopt. Operator steps are DevPi's: a race state, a scratch
# (with or without a replacement) and its undo, and the between-races
# reset, driven through the board's routes (--devpi) or its Python API
# (tests), or waited for from the state line DevPi sends.

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
    NUM_SPARES, SIM_MAC_PREFIX, CupSim, GatewaySim, cup_mac, tick_cup,
)
from la_quiniela.sim.protocol import Phase
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
    """Drives DevPi through the betting board's routes (no flag needed)."""

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

    def state(self, phase: int) -> None:
        self._post("/api/quiniela/cmd", {"cmd": "state %d" % int(phase)})

    def scratch(self, horse: int, replacement: Optional[int] = None, name: Optional[str] = None) -> None:
        body: Dict[str, Any] = {"horse": int(horse)}
        if replacement is not None:
            body["replacement"] = {"number": int(replacement), "name": name or ""}
        self._post("/api/quiniela/scratch", body)

    def unscratch(self, horse: int) -> None:
        self._post("/api/quiniela/unscratch", {"horse": int(horse)})

    def reset(self) -> dict:
        """The between-races reset. Returns the route's reply."""
        return self._post("/api/quiniela/reset", None)

    def forget_cups(self) -> int:
        """Drop this run's pretend cups from DevPi's cache (POST
        /api/lq/cups/forget with the simulator's MAC prefix). DevPi does the
        same by itself the next time a real gateway says hello."""
        return int(self._post("/api/lq/cups/forget", {"prefix": SIM_MAC_PREFIX}).get("forgotten", 0))


class ApiOperator:
    """Drives the board in-process through its Python API (tests)."""

    def __init__(self, board):
        self.board = board

    def state(self, phase):
        self.board.set_race_state(int(phase), source="cmd")      # the path the routes take

    def scratch(self, horse, replacement=None, name=None):
        if replacement is None:
            self.board.store.scratch_gateway(int(horse))
        else:
            self.board.store.scratch_replace(int(horse), int(replacement), name)
        self.board.refresh()

    def unscratch(self, horse):
        now = self.board.store.replacement_of(int(horse))
        if now is not None:
            self.board.queue_undo_renum(now, int(horse))
            self.board.store.unscratch_replace(int(horse))
        else:
            self.board.store.unscratch_gateway(int(horse))
        self.board.refresh()

    def reset(self):
        return self.board.reset_betting()

    def forget_cups(self):
        return self.board.bridge.forget_cups(SIM_MAC_PREFIX)


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
        self.kind = kind              # "state" | "scratch" | "unscratch" | "reset"
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
        # what the operator is meant to have set (DevPi's state), by horse number
        self.intended_phase: int = int(Phase.PRE_RACE)
        self.intended_scratched: set = set()
        self.intended_renum: List[tuple] = []     # (was, now) pairs that must be in the state line
        self.intended_gone: set = set()           # horses no pair may start from any more (an undone renumber)
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

    def cup_for_horse(self, horse: int) -> Optional[CupSim]:
        """The powered cup that says it is `horse` (the most recently booted
        one if two do), or None."""
        found = [c for c in self.cups.values() if c.powered and c.horse == horse]
        if not found:
            return None
        return max(found, key=lambda c: c.booted_at or 0)

    def drop(self, number: int, n: int) -> None:
        c = self.cup(number)
        c.drop(n, self.clock.now())
        self.say("cup %d (horse %s): +%d tokens (now %d)" % (number, c.horse or "-", n, c.tokens))

    def take(self, number: int, n: int) -> None:
        c = self.cup(number)
        taken = c.take(n, self.clock.now())
        self.say("cup %d (horse %s): -%d tokens (now %d)" % (number, c.horse or "-", taken, c.tokens))

    def kill(self, number: int) -> None:
        c = self.cup(number)
        c.power_off()
        self.say("cup %d: power off" % number)

    def boot(self, number: int) -> None:
        c = self.cup(number)
        c.power_on(self.clock.now())
        self.say("cup %d: power on (%s, horse %s), sending HELLO" % (number, c.mac, c.horse or "none"))

    def boot_spare(self, k: int) -> CupSim:
        if k not in (1, 2):
            raise ValueError("spares are 1 and 2")
        c = self.cup(20 + k)
        c.power_on(self.clock.now())
        self.say("spare %d: power on (%s, no horse), sending HELLO" % (k, c.mac))
        return c

    def set_horse(self, number: int, horse: int) -> None:
        """The touch menu on that cup: HORSE -> the number -> SET."""
        c = self.cup(number)
        c.set_horse(horse, self.clock.now())
        self.say("cup %d: HORSE set to %s on the cup" % (number, horse or "none"))

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

    def move_tokens_to_spare(self, number: int, spare: CupSim) -> None:
        dead = self.cup(number)
        n = dead.tokens
        dead.tokens = 0
        spare.drop(n, self.clock.now())
        self.say("cup %d's %d tokens moved into %s (spare, horse %s)" % (number, n, spare.mac, spare.horse or "-"))

    # -- scenario helpers -----------------------------------------------------

    def wait(self, seconds: float, min_real: float = 0.0) -> Wait:
        return Wait(seconds, min_real)

    def until(self, desc: str, pred: Callable[[], bool], timeout: float = 60.0) -> Until:
        return Until(desc, pred, timeout)

    def expect_event(self, type_: str, horse: Optional[int]) -> None:
        self.expected_events.append("%s%s" % (type_, "" if horse is None else " for horse %d" % horse))

    def expect_no_event(self, type_: str, horse: int) -> None:
        self.expected_events.append("no %s for horse %d" % (type_, horse))

    def assert_equal(self, what: str, a, b) -> None:
        if a != b:
            self.failures.append("%s: %r != %r" % (what, a, b))
            self.say("CHECK FAILED: %s: %r != %r" % (what, a, b), important=True)
        else:
            self.say("check ok: %s" % what)

    def _state_applied(self) -> bool:
        gw = self.gw
        if gw.state_rev == 0 or gw.phase != int(self.intended_phase):
            return False
        if set(gw.scratched) != set(self.intended_scratched):
            return False
        if any(f in self.intended_gone for f, _ in gw.renum):
            return False                          # the undone record's pair is still in the packet
        return all(tuple(pair) in gw.renum for pair in self.intended_renum)

    def operator_state(self, desc: str, phase) -> OperatorStep:
        self.intended_phase = int(phase)
        return OperatorStep("state", desc, self._state_applied, {"phase": int(phase)})

    def operator_scratch(self, desc: str, horse: int, replacement: Optional[int] = None,
                         name: Optional[str] = None) -> OperatorStep:
        if replacement is None:
            self.intended_scratched.add(int(horse))
        else:
            self.intended_renum.append((int(horse), int(replacement)))
            self.intended_gone.discard(int(horse))
        return OperatorStep("scratch", desc, self._state_applied,
                            {"horse": int(horse), "replacement": replacement, "name": name})

    def operator_unscratch(self, desc: str, horse: int) -> OperatorStep:
        self.intended_scratched.discard(int(horse))
        if any(p[0] == int(horse) for p in self.intended_renum):
            self.intended_gone.add(int(horse))    # DevPi must take the pair out (and send it back for a while)
        self.intended_renum = [p for p in self.intended_renum if p[0] != int(horse)]
        return OperatorStep("unscratch", desc, self._state_applied, {"horse": int(horse)})

    def operator_reset(self, desc: str = "reset betting (POST /api/quiniela/reset)") -> OperatorStep:
        self.intended_phase = int(Phase.PRE_RACE)
        return OperatorStep("reset", desc, self._state_applied, {})

    def prologue(self):
        """Every scenario starts the same way: cups 1..20 power on (each
        knows it is horse 1..20), the gateway hears them all, DevPi opens
        betting."""
        for n in range(1, 21):
            self.boot(n)
        yield self.until("the gateway has heard all 20 cups",
                         lambda: sum(1 for n in range(1, 21) if self.cups[n].mac in self.gw.cups) == 20,
                         timeout=30)
        yield self.operator_state("open betting (BETTING_OPEN)", phase=Phase.BETTING_OPEN)

    # -- hand commands ----------------------------------------------------------

    HELP = ("drop CUP N       tokens into a cup (cups are 1..20)\n"
            "take CUP N       tokens out of a cup\n"
            "kill CUP         cup loses power\n"
            "boot CUP         cup powers on (keeps its tokens and its horse)\n"
            "boot-spare 1|2   power on a spare cup (no horse until set-horse)\n"
            "set-horse CUP H  the touch menu on that cup: HORSE -> H (0 = none); spares are cups 21 and 22\n"
            "reboot-gateway   the gateway reboots\n"
            "cups             table of cups: number, MAC, horse, count, powered\n"
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
            elif cmd == "set-horse" and len(parts) == 3:
                self.set_horse(int(parts[1]), int(parts[2]))
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
        rows = [" cup  mac                horse  count  powered"]
        for n, c in sorted(self.cups.items()):
            label = ("%2d" % n) if n <= 20 else ("s%d" % (n - 20))
            rows.append(" %3s  %s  %5s  %5d  %s" % (label, c.mac, c.horse or "-",
                                                    c.sample(now)[1] if c.powered else c.tokens,
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
                    raise ScenarioFailed("no state line from DevPi within %.0f s for: %s%s"
                                         % (self.operator_timeout, p.desc,
                                            "" if not self._pending_error
                                            else " (last error: %s)" % self._pending_error))
                else:
                    # Nothing came back. Ask again: DevPi may not have had the
                    # port open when the first request went out.
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
        request made before DevPi opened the port is simply gone."""
        self._pending_asked = now
        if self.operator is None:
            if not again:
                self.say("WAITING FOR OPERATOR: %s" % step.desc, important=True)
            return
        self.say("operator (driven%s): %s" % (", again" if again else "", step.desc))
        try:
            if step.kind == "state":
                self.operator.state(step.payload["phase"])
            elif step.kind == "scratch":
                if not again:      # a scratch is not idempotent: DevPi refuses a repeat as "already scratched"
                    self.operator.scratch(step.payload["horse"], step.payload["replacement"], step.payload["name"])
            elif step.kind == "unscratch":
                if not again:
                    self.operator.unscratch(step.payload["horse"])
            else:
                self.operator.reset()
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
        """What DevPi should end up believing, by cup MAC: the horse each cup
        says it is, its count, and whether it is online."""
        cups = {}
        for c in self.cups.values():
            if c.booted_at is None:
                continue                          # never powered: DevPi has never heard of it
            cups[c.mac] = {"horse": c.horse, "count": c.tokens, "online": bool(c.powered)}
        return {"cups": cups, "in_sync": True, "events": list(self.expected_events),
                "intended_finish": self.intended_finish, "empty_cup": self.empty_cup}

    def _print_expectations(self) -> None:
        exp = self.expectation()
        lines = ["", "EXPECTED RESULTS (%s%s)" % (self.scenario_name, "" if self.seed is None else ", seed %s" % self.seed),
                 " mac                horse  count  online"]
        for mac in sorted(exp["cups"]):
            e = exp["cups"][mac]
            lines.append(" %-17s  %5s  %5d  %s" % (mac, e["horse"] or "-", e["count"], "yes" if e["online"] else "no"))
        lines.append(" total tokens in play: %d" % sum(e["count"] for e in exp["cups"].values()))
        lines.append(" link in_sync: true")
        if exp["events"]:
            lines.append(" events that should have been logged: " + ", ".join(exp["events"]))
        if exp["intended_finish"]:
            lines.append(" intended finish order (win, place, show): %s; horse %s has no tokens"
                         % (", ".join("horse %d" % c for c in exp["intended_finish"]), exp["empty_cup"]))
        for f in self.failures:
            lines.append(" FAILED: " + f)
        self.out("\n".join(lines))

    def compare(self, snapshot: dict, tolerance: int) -> List[str]:
        exp = self.expectation()
        problems = []
        by_mac = {c.get("mac"): c for c in snapshot.get("cups", []) if isinstance(c, dict)}
        for mac, e in exp["cups"].items():
            s = by_mac.get(mac)
            if s is None:
                problems.append("cup %s missing from the snapshot" % mac)
                continue
            if int(s.get("horse") or 0) != e["horse"]:
                problems.append("cup %s: horse %s, expected %s" % (mac, s.get("horse"), e["horse"]))
            if bool(s.get("online")) != e["online"]:
                problems.append("cup %s: online %s, expected %s" % (mac, s.get("online"), e["online"]))
            got = s.get("count")
            if got is None or abs(int(got) - e["count"]) > tolerance:
                problems.append("cup %s: count %s, expected %d%s" % (mac, got, e["count"],
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

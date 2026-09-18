# la_quiniela/sim/scenarios.py - the scripted party scenarios
#
# A scenario is a generator that receives the runner's context and yields
# waits and operator steps. Cup-side steps (tokens, power, gateway reboot) it
# performs itself through the context; operator steps are DevPi's job, and
# the runner either drives them through the bridge (--devpi, or the Python
# API in tests) or prints WAITING FOR OPERATOR and waits for the state or
# roster line from DevPi to show the change.
#
# Every number here is a human cup number, 1..20 (spares 21 and 22).

import random
from typing import Callable, Dict, List

from la_quiniela.sim.protocol import Phase

SCENARIOS: Dict[str, Callable] = {}
DESCRIPTIONS: Dict[str, str] = {}


def scenario(name: str, description: str):
    def wrap(fn):
        SCENARIOS[name] = fn
        DESCRIPTIONS[name] = description
        return fn
    return wrap


def _betting(ctx, rng: random.Random, seconds: float, favourites: List[int], skip: List[int] = ()):
    """Tokens arriving unevenly over a betting window: a few drops per cup,
    the favourites getting more, in bursts of one to three tokens."""
    weights = {cup: (4 if cup in favourites else 1) for cup in range(1, 21) if cup not in skip}
    rounds = 12
    for _ in range(rounds):
        cups = list(weights)
        picks = rng.choices(cups, weights=[weights[c] for c in cups], k=4)
        for cup in picks:
            ctx.drop(cup, rng.choice((1, 1, 2, 3)))
        yield ctx.wait(seconds / rounds)


@scenario("normal", "a full party: uneven betting with two favourites, a rush at FINAL_CALL, then every phase to AFTER_PARTY")
def normal(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 40, favourites=[3, 12])
    yield ctx.operator_state("set phase to FINAL_CALL", phase=Phase.FINAL_CALL)
    for _ in range(6):                             # the rush
        for cup in rng.sample(range(1, 21), 5):
            ctx.drop(cup, rng.choice((1, 2, 2, 3)))
        yield ctx.wait(2)
    yield ctx.operator_state("set phase to AT_THE_POST", phase=Phase.AT_THE_POST)
    yield ctx.wait(5)
    yield ctx.operator_state("set phase to RUNNING", phase=Phase.RUNNING)
    yield ctx.wait(8)
    yield ctx.operator_state("set phase to WINNER", phase=Phase.WINNER)
    yield ctx.wait(5)
    yield ctx.operator_state("set phase to AFTER_PARTY", phase=Phase.AFTER_PARTY)
    yield ctx.wait(3)


@scenario("scratch-rebet", "betting is under way, a horse with tokens is scratched, its cup is emptied and the same tokens are re-dropped elsewhere")
def scratch_rebet(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 30, favourites=[5, 9])
    if ctx.tokens_in(5) == 0:
        ctx.drop(5, 4)
        yield ctx.wait(2)
    before = ctx.total_tokens()
    ctx.note("tokens in play before the scratch: %d (cup 5 holds %d)" % (before, ctx.tokens_in(5)))
    yield ctx.operator_state("scratch horse 5 (cup 5)", scratched={5: True})
    moved = 0
    while ctx.tokens_in(5) > 0:
        handful = min(3, ctx.tokens_in(5))
        ctx.take(5, handful)
        moved += handful
        yield ctx.wait(2)
    ctx.note("cup 5 reads zero; re-dropping %d tokens into other cups" % moved)
    others = [c for c in range(1, 21) if c != 5]
    while moved > 0:
        n = min(moved, rng.choice((1, 2, 3)))
        ctx.drop(rng.choice(others), n)
        moved -= n
        yield ctx.wait(1.5)
    after = ctx.total_tokens()
    ctx.note("tokens in play after the re-bet: %d" % after)
    ctx.assert_equal("tokens in play unchanged by the scratch", before, after)
    yield ctx.operator_state("set phase to AT_THE_POST", phase=Phase.AT_THE_POST)
    yield ctx.wait(3)


@scenario("late-tokens", "normal betting, then a few more tokens land in two cups after AT_THE_POST")
def late_tokens(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 30, favourites=[7, 15])
    yield ctx.operator_state("set phase to FINAL_CALL", phase=Phase.FINAL_CALL)
    yield ctx.wait(5)
    yield ctx.operator_state("set phase to AT_THE_POST", phase=Phase.AT_THE_POST)
    yield ctx.wait(4)
    ctx.note("late tokens after AT_THE_POST")
    ctx.drop(7, 2)
    yield ctx.wait(3)
    ctx.drop(15, 1)
    yield ctx.wait(3)
    ctx.drop(7, 1)
    yield ctx.wait(3)


@scenario("dropout-reboot", "one cup goes silent for 20 s and comes back with the same count; another browns out for 3 s")
def dropout_reboot(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 20, favourites=[7, 12])
    ctx.note("cup 7 loses power for 20 s (DevPi should mark it offline)")
    ctx.kill(7)
    ctx.expect_event("cup_offline", 7)
    yield ctx.wait(20, min_real=20)
    ctx.boot(7)
    ctx.expect_event("cup_hello", 7)
    ctx.expect_event("cup_online", 7)
    yield ctx.wait(8, min_real=6)
    ctx.note("cup 12 browns out for 3 s (too short to be marked offline)")
    ctx.kill(12)
    yield ctx.wait(3, min_real=3)
    ctx.boot(12)
    ctx.expect_event("cup_hello", 12)
    ctx.expect_no_event("cup_offline", 12)
    yield ctx.wait(8, min_real=6)
    yield from _betting(ctx, rng, 10, favourites=[7])
    yield ctx.wait(3)


@scenario("empty-show-cup", "betting where one horse receives no tokens at all and finishes third")
def empty_show_cup(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 30, favourites=[3, 9], skip=[14])
    ctx.intended_finish = [3, 9, 14]
    ctx.empty_cup = 14
    yield ctx.operator_state("set phase to FINAL_CALL", phase=Phase.FINAL_CALL)
    yield ctx.wait(4)
    yield ctx.operator_state("set phase to AT_THE_POST", phase=Phase.AT_THE_POST)
    yield ctx.wait(3)
    yield ctx.operator_state("set phase to RUNNING", phase=Phase.RUNNING)
    yield ctx.wait(3)


@scenario("gateway-reboot", "mid-betting the gateway reboots; DevPi must re-send roster then state on its own")
def gateway_reboot(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 15, favourites=[4, 16])
    ctx.note("gateway reboots: hello again, revs and up_s back to 0, silent until DevPi answers")
    ctx.reboot_gateway()
    ctx.expect_event("gateway_hello", None)
    ctx.expect_event("gateway_reboot", None)
    yield ctx.until("DevPi re-sent roster and state after the reboot",
                    lambda: ctx.gw.roster_rev > 0 and ctx.gw.state_rev > 0, timeout=30)
    order = [kind for kind, _ in ctx.gw.applied_log]
    ctx.assert_equal("roster arrived before state after the reboot", order[:2], ["roster", "state"])
    yield ctx.wait(6, min_real=6)              # a status with matching revs, so in_sync comes back
    yield from _betting(ctx, rng, 10, favourites=[4])
    yield ctx.operator_state("set phase to FINAL_CALL", phase=Phase.FINAL_CALL)
    yield ctx.wait(3)


@scenario("cup-swap", "one cup dies for good; a spare is powered on and takes over its slot and horse")
def cup_swap(ctx):
    rng = ctx.rng
    yield from ctx.prologue()
    yield from _betting(ctx, rng, 15, favourites=[7, 11])
    if ctx.tokens_in(7) == 0:
        ctx.drop(7, 3)
        yield ctx.wait(2)
    dead_tokens = ctx.tokens_in(7)
    ctx.note("cup 7 dies for good with %d tokens in it" % dead_tokens)
    ctx.kill(7)
    ctx.expect_event("cup_offline", 7)
    yield ctx.wait(8, min_real=8)
    ctx.note("spare 1 powers on: it should show up as unassigned")
    spare = ctx.boot_spare(1)
    ctx.expect_event("cup_hello", None)
    yield ctx.wait(3, min_real=3)
    yield ctx.operator_roster("put the spare's MAC %s into cup 7's slot" % spare.mac, cup=7, mac=spare.mac)
    yield ctx.until("the spare took over as cup 7", lambda: spare.cup_id == ctx.slot_of_cup(7), timeout=20)
    ctx.note("the spare is cup 7 now; its tokens move over")
    ctx.move_tokens_to_spare(7, spare)
    yield ctx.wait(4, min_real=4)
    yield from _betting(ctx, rng, 8, favourites=[7])
    yield ctx.wait(3)

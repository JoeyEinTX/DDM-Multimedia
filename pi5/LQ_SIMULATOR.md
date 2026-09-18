# La Quiniela cup simulator

## What it is

The simulator pretends to be the cup gateway and all twenty betting cups.
It plugs into the DevPi app through a fake serial port, so the app cannot
tell it from the real thing. It plays out party scenarios, lets you poke at
cups by hand, and at the end checks what DevPi believes and says PASS or
FAIL.

## Before you start

> **Warning**
>
> 1. Stop the normal app first. Never run two copies of the app at once.
> 2. While the app is pointed at the simulator, the real gateway is
>    ignored, even if it is plugged in.
> 3. Everything here is for the bench. Do not run it on party day.

## Running it on DevPi

You need two terminal windows.

**Window 1: the simulator.**

1. Go to the app folder:

   ```
   cd ~/DDM-Multimedia/pi5
   ```

2. Start the simulator. This one runs the `normal` party and checks the
   result at the end:

   ```
   python -m la_quiniela.simulator --scenario normal --ideal --devpi http://localhost:5000 --check http://localhost:5000
   ```

   It prints the fake serial port it created and the exact command for
   window 2. Leave it running.

   It is fine that the app is not up yet. The simulator keeps asking DevPi
   until it answers, so take as long as you like over window 2. While it
   waits it prints `DevPi did not take the request ...; retrying every 5 s`.

**Window 2: the app, pointed at the simulator.**

3. Go to the same folder and start the app like this, all on one line:

   ```
   cd ~/DDM-Multimedia/pi5
   DDM_LQ_SERIAL_PORT=/tmp/ddm-lq-sim DDM_LQ_DEV_ENDPOINTS=1 python main.py
   ```

   The first setting points the app at the simulator instead of the real
   gateway. The second lets the simulator change the race phase and the cup
   roster for you.

4. Watch window 1. The cups power on, get their numbers, tokens start
   landing, the phases advance, and at the end you see a table of expected
   results and then `PASS` or `FAIL`.

To stop either window, press `Ctrl` and `C` together.

When you stop the simulator, window 2 prints one line like
`read failed (device reports readiness to read ...); reopening in 5 s`,
then keeps saying it cannot open the port. That is the app noticing the
fake port has gone and waiting for it to come back. It is not a fault.
Stop the app too, or start the simulator again.

### DevPi remembers the cups

DevPi keeps its cup roster between runs, the same way it does with the real
gateway. Every scenario starts by telling DevPi all twenty cup addresses
again, so a leftover roster from an earlier run is corrected rather than
inherited. You never need to clear anything by hand between runs.

## Seeing it work

While both windows run, open this address in a browser on the same
network, using DevPi's address instead of `<devpi>`:

```
http://<devpi>:5000/api/lq/snapshot
```

You get a page of text showing what DevPi believes about the link and every
cup: its number, its address, its token count, and whether it is online.
Refresh the page and the counts change as the scenario runs.

## The scenarios

Each line below is the command for a self-checking run. Start the app in
window 2 the same way every time. Change `--speed` to a smaller number to
watch things happen more slowly; leave `--ideal` on for exact counts.

| Scenario | What happens |
| --- | --- |
| `normal` | A full party: uneven betting with two favourites, a rush at final call, then every phase through to the after-party. |
| `scratch-rebet` | Betting is under way, one horse with tokens in its cup is scratched, that cup is emptied, and the same tokens are dropped into other cups. |
| `late-tokens` | Normal betting, then a few more tokens land in two cups after the horses are at the post. |
| `dropout-reboot` | One cup goes silent for 20 seconds and comes back with the same count; another cup blinks off for 3 seconds. |
| `empty-show-cup` | Betting where one horse gets no tokens at all, and that horse is meant to finish third. |
| `gateway-reboot` | The gateway reboots in the middle of betting; DevPi must restore it without any help. |
| `cup-swap` | One cup dies for good; a spare is switched on and takes over that cup's number and horse. |

```
python -m la_quiniela.simulator --scenario normal --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario scratch-rebet --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario late-tokens --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario dropout-reboot --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario empty-show-cup --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario gateway-reboot --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario cup-swap --ideal --devpi http://localhost:5000 --check http://localhost:5000
```

`python -m la_quiniela.simulator --list` prints the same list.

Useful extras:

- `--speed 10` makes the waits between steps ten times shorter. The cups
  still report every 2 seconds, as real cups do.
- `--seed 5` makes a run repeat exactly, so a problem can be reproduced.
- Leave out `--ideal` and the scale gets realistic noise and the landing
  bump; a count is then allowed to be one token off at the check.
- Leave out `--devpi` and the simulator stops at each operator step,
  prints `WAITING FOR OPERATOR: ...`, and waits for you (or the admin page)
  to make that change in DevPi. It gives up after two minutes.
- `--operator-timeout 300` gives DevPi longer to answer each step, for a
  slow start or when you are making the changes by hand.
- `--quiet` prints only the prompts, the expected results and the verdict.
  `--wire` prints every line that goes over the fake serial port.
- `--auto-demo` pretends the gateway was flashed as a bench build that
  starts demo mode on its own.

## Driving it by hand

Start the simulator with no scenario and it just powers the cups and waits
for you to type:

```
python -m la_quiniela.simulator
```

Cup numbers are 1 to 20, the same numbers DevPi shows.

| Type | Does |
| --- | --- |
| `drop 7 3` | drops 3 tokens into cup 7 |
| `take 7 2` | takes 2 tokens out of cup 7 |
| `kill 7` | cup 7 loses power |
| `boot 7` | cup 7 powers back on, with the same tokens |
| `boot-spare 1` | switches on spare cup 1 (there is also spare 2); it stays unassigned until the roster includes it |
| `reboot-gateway` | the gateway reboots |
| `cups` | a table of every cup: number, address, slot, count, powered |
| `help` | the list above |
| `quit` | stops the simulator |

You can also type these while a scenario is running.

## PASS and FAIL

At the end of a scenario the simulator prints an **EXPECTED RESULTS** table:
the token count each cup should show, which cups should be online, and the
events DevPi should have logged. With `--check` it then reads DevPi's
snapshot and compares it.

- **PASS** means DevPi's picture matches: every cup has the right address,
  the right count and the right online flag, and the link says it is in
  sync.
- **FAIL** means at least one thing differs. Each difference is printed on
  its own line starting with `MISMATCH:`, before the word FAIL. A scenario
  that could not finish, for example because an operator step never
  happened, also says FAIL and prints why.

If it says FAIL, send back all of this:

1. The exact command you ran.
2. Everything the simulator printed from `EXPECTED RESULTS` to `FAIL`.
3. The last screen or two of window 2 (the app).
4. The page at `http://<devpi>:5000/api/lq/snapshot`, saved or copied.

## Checking the simulator itself

```
cd ~/DDM-Multimedia/pi5
python -m la_quiniela.test_simulator
```

This runs the simulator against the real bridge inside one process, with
no app and no hardware, and takes about a minute.

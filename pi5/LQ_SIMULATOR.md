# La Quiniela cup simulator

## What it is

The simulator pretends to be the cup gateway and all twenty betting cups.
It plugs into the DevPi app through a fake serial port, so the app cannot
tell it from the real thing. It plays out party scenarios, lets you poke at
cups by hand, and at the end checks what DevPi believes and says PASS or
FAIL.

Like the real cups, every pretend cup knows its own horse number: cups 1 to
20 come up as horses 1 to 20, and the two spare cups come up with no horse
until you set one, the way you would on a real cup's touch menu.

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
   DDM_LQ_SERIAL_PORT=/tmp/ddm-lq-sim python main.py
   ```

   That setting points the app at the simulator instead of the real
   gateway. Nothing else is needed: the simulator changes the race state
   and makes its scratches through the same betting-board routes the admin
   page uses.

4. Watch window 1. The cups power on, each saying which horse it is, tokens
   start landing, the phases advance, and at the end you see a table of
   expected results and then `PASS` or `FAIL`.

To stop either window, press `Ctrl` and `C` together.

When you stop the simulator, window 2 prints one line like
`read failed (device reports readiness to read ...); reopening in 5 s`,
then keeps saying it cannot open the port. That is the app noticing the
fake port has gone and waiting for it to come back. It is not a fault.
Stop the app too, or start the simulator again.

### DevPi remembers the cups

DevPi keeps a small memory of every cup it has ever heard (its address, the
horse it last said it was, when it was last heard), the same way it does
with real cups. That memory decides nothing: a cup that is talking is back
in the picture within a packet, and every cup tells DevPi its horse itself.
So a leftover memory from an earlier run is simply corrected as the cups
report, and you never need to clear anything by hand between runs.

### Going back to the real gateway

Nothing to remember here either. The twenty cups the simulator invents are
not real, and DevPi can tell: their addresses all start `02:DD:4D:`. The
moment the real gateway is plugged back in and says hello, DevPi drops the
pretend cups from its memory so they do not sit on the admin page as
offline cups, and answers the hello with its state as it always does. The
real cups then show up under their own numbers as they report.

One thing is worth knowing: the gateway itself keeps whatever state it was
last sent until it loses power or DevPi sends the next one, which the hello
answer is. So if you have been simulating and then plug the real gateway
back in, a quick power cycle of the gateway makes it say hello and get the
current state straight away.

## Seeing it work

While both windows run, open this address in a browser on the same
network, using DevPi's address instead of `<devpi>`:

```
http://<devpi>:5000/api/lq/snapshot
```

You get a page of text showing what DevPi believes about the link and every
cup: its address, the horse it says it is, its token count, and whether it
is online. Refresh the page and the counts change as the scenario runs. The
admin page, `http://<devpi>:5000/quiniela/admin`, shows the same thing as
the Horses list.

## The scenarios

Each line below is the command for a self-checking run. Start the app in
window 2 the same way every time. Change `--speed` to a smaller number to
watch things happen more slowly; leave `--ideal` on for exact counts.

| Scenario | What happens |
| --- | --- |
| `normal` | A full party: uneven betting with two favourites, a rush at final call, then every phase through to the after-party. |
| `scratch-rebet` | Betting is under way, one horse with tokens in its cup is scratched with no replacement (the cup shows its X), that cup is emptied, and the same tokens are dropped into other cups. |
| `scratch-renumber` | Horse 9 is scratched with 22 drawing in: the cup that was 9 becomes 22 on its own, tokens and all. Then the scratch is undone and the cup goes back to 9. |
| `late-tokens` | Normal betting, then a few more tokens land in two cups after the horses are at the post. |
| `dropout-reboot` | One cup goes silent for 20 seconds and comes back with the same count and the same horse; another cup blinks off for 3 seconds. |
| `empty-show-cup` | Betting where one horse gets no tokens at all, and that horse is meant to finish third. |
| `gateway-reboot` | The gateway reboots in the middle of betting; DevPi must send it the state again without any help. |
| `cup-swap` | One cup dies for good; a spare is switched on, set to the dead cup's horse on its own screen, and takes over. |

```
python -m la_quiniela.simulator --scenario normal --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario scratch-rebet --ideal --devpi http://localhost:5000 --check http://localhost:5000
python -m la_quiniela.simulator --scenario scratch-renumber --ideal --devpi http://localhost:5000 --check http://localhost:5000
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
- `--forget-after` tells DevPi to drop this run's pretend cups from its
  memory the moment the run ends, instead of waiting for the real gateway
  to turn up. Needs `--devpi`. Tidy rather than necessary: DevPi does it by
  itself either way.

## Driving it by hand

Start the simulator with no scenario and it just powers the cups and waits
for you to type:

```
python -m la_quiniela.simulator
```

Cup numbers are 1 to 20; cup 7 is the cup that came up as horse 7. The
spares are cups 21 and 22.

| Type | Does |
| --- | --- |
| `drop 7 3` | drops 3 tokens into cup 7 |
| `take 7 2` | takes 2 tokens out of cup 7 |
| `kill 7` | cup 7 loses power |
| `boot 7` | cup 7 powers back on, with the same tokens and the same horse |
| `boot-spare 1` | switches on spare cup 1 (there is also spare 2); it shows up with no horse until you set one |
| `set-horse 21 7` | the touch menu on that cup: HORSE → 7 → SET (here on spare 1); `0` means none |
| `reboot-gateway` | the gateway reboots |
| `cups` | a table of every cup: number, address, horse, count, powered |
| `help` | the list above |
| `quit` | stops the simulator |

You can also type these while a scenario is running.

## PASS and FAIL

At the end of a scenario the simulator prints an **EXPECTED RESULTS** table:
for every cup, by address, the horse it should be saying, the token count it
should show and whether it should be online, plus the events DevPi should
have logged. With `--check` it then reads DevPi's snapshot and compares it.

- **PASS** means DevPi's picture matches: every cup is there under its
  address with the right horse, the right count and the right online flag,
  and the link says it is in sync.
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

This runs the simulator against the real bridge and a real betting board
inside one process, with no app and no hardware, and takes about a minute.
The end-to-end part needs a Linux pty, so on a Windows PC it reports those
tests as skipped and runs the rest.

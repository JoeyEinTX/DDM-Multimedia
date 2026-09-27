# La Quiniela — Race Night Runbook

One action per line. Do them in order. Commands are typed on DevPi unless it says
otherwise. `ADMIN` means the page at `http://joeydevpi.local:5000/quiniela/admin`.

Race states: 0 PRE_RACE · 1 BETTING_OPEN · 2 FINAL_CALL · 3 AT_THE_POST · 4 RUNNING ·
5 WINNER · 6 AFTER_PARTY. The board owns the TV in 1–4 and hands it back in 0, 5, 6.

---

## 0. Before the party (do these once, weeks out)

- [ ] pi5 and the splash display run as services and come up on their own after a
      power cycle. Until they do, they're two foreground terminals (section 3).
- [ ] The TV kiosk points at the splash on **5001**, not 5000.
      (Files: `splash_display/deploy/kiosk.sh`, `splash_display/deploy/splash_display.service`,
      `splash_display/deploy/autostart_setup.md`. pi5 has no service file yet.)
- [ ] `impact.ttf` is in `~/.fonts/` on DevPi and `fc-cache -f` has been run, so the
      board on the TV uses Impact, not the fallback.
- [ ] Every cup and the gateway: flashed from the current firmware (`fc87f17` or later,
      both of them), sleeve installed, orientation right, touch working (`p` in serial
      shows touches), `CAL 10` done with the sleeve on, and a 10-token drop/dump test passes.
- [ ] Every cup has a cup ID (`CUP n / NO HORSE` on its screen before a horse is
      assigned; n counts from 0). Write the ID and the MAC on tape under the cup. The ID
      is not stored in the cup: the gateway hands it out from its roster by the cup's MAC
      (its built-in list until DevPi adopts, DevPi's saved roster after that), so it
      survives power as long as the roster does.
- [ ] Power: one supply for all cups, fed from the middle of the bus, bulk cap per cup.
      No cup on a USB port. Boot all twenty at once and watch for any that blink or
      reboot — that's a sag, not a bug.
- [ ] Gateway on DevPi's USB; `pi5/.env` has `DDM_LQ_SERIAL_PORT` set to it and
      `DDM_LQ_DEV_ENDPOINTS=1` (the `reset`, `adopt` and `dev/state` routes below are dev
      routes: 404 without it).
- [ ] Spare cup, spare gateway, spare CYD, flashed and in the drawer.

## 1. Derby week — the field

- [ ] Tuesday after the draw: `ADMIN` → Horse names → paste the twenty names in post
      order, one per line, plus the also-eligibles on lines 21–24 → Save names.
- [ ] Each scratch before Friday 9 a.m. ET: `ADMIN` → Scratches → In the field → that
      horse's row → pick the replacement number (the list offers the unused
      also-eligibles, 21 first) and its name → Scratch. The board lists the new number at
      the bottom; the cup for that post shows it once it has a horse assigned (party day,
      section 4), or at once if it already has.
- [ ] Wrong? `ADMIN` → Scratches → Scratched → `Undo` on that line. Undo the most recent
      scratch first if there were several (the page greys out the others).

## 2. Party day — setup, before guests arrive

- [ ] Cups on the mantle, sleeves in, powered. Every screen shows a number, or
      `CUP n / NO HORSE`. A screen stuck on `WAITING FOR GATEWAY` has no cup ID yet: the
      gateway isn't up, or that cup isn't in the roster.
- [ ] Gateway plugged into DevPi.

### 3. Start the apps (skip if they're services)

Terminal 1:
```bash
cd ~/DDM-Multimedia/pi5 && python main.py
```
Wait for `[LQ] bridge started on …` and then `[LQ] gateway online`. A `gateway hello
from …` line only appears if the gateway was just powered; `answered the hello with …`
follows it.

Terminal 2:
```bash
cd ~/DDM-Multimedia/splash_display && python3 server.py
```
Wait for `pi5 link up`.

Check:
```bash
curl -s localhost:5001/api/quiniela | python3 -c "import sys,json;d=json.load(sys.stdin);print('link',d['link_ok'],'state',d['race_state'],'pot',d['pot'])"
```
`link True` or stop here and fix that first.

### 4. Clean slate

Cups must be **empty** for this.

```bash
curl -s -X POST localhost:5000/api/lq/dev/reset
```
Pot 0, state 0, close time cleared. Names and scratches are kept. **Every cup's horse
and the roster are forgotten**, and a no-replacement scratch is forgotten with them, so
the next lines put them back. Don't power-cycle the gateway: it keeps its roster, and
the cups keep showing their old numbers until the new assignment lands.

- [ ] Wait ten seconds so every cup reports again, then adopt the roster (it copies only
      the cups heard so far):
```bash
curl -s -X POST localhost:5000/api/lq/dev/roster/adopt
```
- [ ] Give every cup its horse. The cup number is the screen's `CUP n` **plus one** (the
      screen counts from 0, the command from 1); the horse is the number the board lists.
      One command per cup, cup 1 → horse 7 looks like this:
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"horse 1 7"}'
```
      Or all twenty in one go (list the horses for cups 1..20 in order; put a
      replacement's number in its post's slot, e.g. 22 where 9 was scratched, and a 1
      in `scratched` for a no-replacement scratch):
```bash
curl -s -X POST localhost:5000/api/lq/dev/state -H 'Content-Type: application/json' -d '{"phase":0,"horses":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20],"scratched":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}'
```
- [ ] Walk the mantle: every cup shows its number, no `NO HORSE`, no `NO LINK` badge on
      any cup. `ADMIN` → Board says `LINK OK` and `20 cups online`.
- [ ] Drop one token in one cup: `ADMIN` → Board shows Pot $1 (the TV board and its toast
      only show from state 1). Take it out: Pot $0. Don't `reset` again — that would
      forget the assignments you just made.

### 5. Set the close time

`ADMIN` → Betting closes → pick the time and `Set` (or `+60 min`) → `CLOSES IN` appears
under the banner once betting opens.
Set it for a few minutes before post time; you still close by hand (next section).

## 6. Open betting

```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
```
The board takes the TV. `BETTING OPEN`.

While open:
- Tokens go **in** by tipping them onto the sleeve. Don't fling.
- A cup that gets bumped or lifted freezes its count (`HANDLED` on its diag) and
  re-reads itself when it's set down. Don't touch it; wait five seconds.
- Someone takes a token back out: the count drops and the pot drops. That's correct.
- A same-day scratch (vet scratch, gate scratch): `ADMIN` → Scratches → In the field →
  that horse's row → tick *No replacement* → Scratch. The row leaves the board, the crawl
  says `TOKENS REFUNDED`, its tokens leave the pot. Empty that cup and hand the tokens back.
- Something's wrong with a count: pull the tokens, count by hand, put them back in one
  motion. The settled weight is the truth and the board follows it (it shows that as a
  drop to 0 and then a `+N BETS` toast; that's fine).

## 7. Close betting

Two minutes out:
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 2"}'
```
`FINAL CALL` pulses.

At the post:
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 3"}'
```
`BETTING CLOSED`, board frozen. **Write down POT / WIN / PLACE / SHOW now** — the board
leaves the TV at state 5. (They're also on the `ADMIN` status line any time.)

They're off:
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 4"}'
```

## 8. Results and the draw

```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 5"}'
```
TV goes back to the slideshow.

- [ ] WIN cup: shake it, pull **one** token. Read its number aloud. The guest with the
      other half takes the WIN prize.
- [ ] PLACE cup: one token, PLACE prize.
- [ ] SHOW cup: one token, SHOW prize.
- [ ] Pay in whole dollars, the amounts you wrote down.
- [ ] Undrawn tokens are just tokens. Nobody else wins anything.

Then:
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 6"}'
```

## 9. Another race on the same night

Empty every cup (each count drops to 0 by itself; Pot $0). Then section 5 (close time)
and section 6 (`state 1`). No `reset`: it would forget the cup assignments. Names and
scratches stay; if the next race has a different field, paste new names first and redo
the scratches.

---

## If something goes wrong

| You see | It means | Do |
|---|---|---|
| `NO LINK` in the board's corner | splash can't reach pi5, or pi5 can't hear the gateway | `ADMIN` → Board. `NO LINK` there too: the gateway (cable, `DDM_LQ_SERIAL_PORT`). `LINK OK` there: Terminal 1 alive? `curl -s localhost:5000/api/quiniela` answers? Restart pi5, then splash. |
| Gateway `hello` lines forever, never answered | pi5 has no state yet, or wrong port | `state 0` via the cmd route; check `DDM_LQ_SERIAL_PORT` in `pi5/.env`. |
| Cup shows `NO HORSE` | not assigned (or roster lost) | `adopt`, then `horse <cup> <n>` (cup = the screen's number plus one). |
| Cup count stuck, `HANDLED` | it was moved/bumped | Leave it alone five seconds. |
| Count wrong after a bump | baseline drifted | Pull all tokens, wait for 0, put them back in one pour. |
| Cup screen upside down / mirrored | orientation stepped (BOOT held 15 s) | Serial: `x`. Or touch menu (hold 3 s) → FLIP 180. |
| Cup blinks / reboots | power sag | Check the bus voltage at that cup; never USB. |
| Board shows old pot/bets on startup | pi5 persisted last session | `reset` with empty cups, then section 4. |
| Pot isn't $0 after a `reset` | tokens were still in the cups; their counts come back with the assignment | Empty the cups (each count drops to 0 by itself). |
| Board on the TV, wrong state | state didn't take | `curl -s localhost:5000/api/quiniela` → `race_state`; send the state again. |
| Wrong number on a cup after a scratch | old firmware (pre-`fc87f17`) | Flash the gateway and the cup. |

## The rules, for anyone who asks

- $1 a token. Drop it in the cup of the horse you like. Keep the other half.
- After the race, one token is drawn from the WIN cup, one from PLACE, one from SHOW.
  The drawn token's owner takes that prize.
- Prizes are 60 / 25 / 15 percent of the pot, whole dollars, WIN takes the rounding.
- Fewer tokens in a cup, better shot at the draw.
- Scratched before Friday: the alternate takes over, with its own number.
  Scratched on Saturday: tokens refunded.
- Totals based on cheap Chinese electronics. Final results hand counted.

# La Quiniela — Race Night Runbook

One action per line. Do them in order. The night is run from `ADMIN`, the page at
`http://joeydevpi.local:5000/quiniela/admin`, on a phone. Commands are typed on DevPi only
in the appendix (if the page is down).

Race states: 0 PRE-RACE · 1 BETTING OPEN · 2 FINAL CALL · 3 AT THE POST · 4 RUNNING ·
5 WINNER · 6 AFTER PARTY. They are the seven buttons under `ADMIN` → Race, the current
one lit. The board owns the TV in 1–4 and hands it back in 0, 5, 6.

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
- [ ] Gateway on DevPi's USB; `pi5/.env` has `DDM_LQ_SERIAL_PORT` set to it.
      `DDM_LQ_DEV_ENDPOINTS=1` is needed only for the next line (`Adopt` and `Forget cups`
      on the page, the dev curls in the appendix): set it for the bench, restart pi5, take
      it out again once the roster is adopted. The night itself doesn't need it.
- [ ] Adopt the roster once, on the bench, with every cup powered: `ADMIN` → Race → Cups
      shows every cup `online` → **Adopt**. Then pick each cup's horse in its row (cup 1 →
      `#1`, … cup 20 → `#20`; the cup number is the screen's `CUP n` **plus one**). pi5
      keeps the roster and the assignments across restarts and across **Reset betting**,
      so this is done once. A cup that reports later is not added by a second Adopt: see
      the table at the end.
- [ ] Spare cup, spare gateway, spare CYD, flashed and in the drawer.

## 1. Derby week — the field

- [ ] Tuesday after the draw: `ADMIN` → Horse names → paste the twenty names in post
      order, one per line, plus the also-eligibles on lines 21–24 → Save names.
- [ ] Each scratch before Friday 9 a.m. ET: `ADMIN` → Scratches → In the field → that
      horse's row → pick the replacement number (the list offers the unused
      also-eligibles, 21 first) and its name → Scratch. The board lists the new number at
      the bottom and the cup for that post shows it at once (nothing moves on the mantle;
      a cup with no horse yet shows it when one is picked).
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

Check: open `ADMIN` on the phone. Race says `LINK OK` and `20 cups online`.
`NO LINK`, or fewer cups: stop here and fix that first (table at the end).

### 4. Clean slate

Cups must be **empty** for this.

- [ ] `ADMIN` → Race → Cups: every cup `online`, its horse in the picker (`#7 NAME`).
      A cup showing `—`: pick its horse. A cup `offline` or `no cup`: table at the end.
- [ ] `ADMIN` → Race → **Reset betting** → OK. The line under the button says
      `Reset. Pot $0 · 20 cups keep their horses`. If it names cups with tokens still in
      them, empty those cups and press it again. Nothing else changes: the cups keep their
      numbers and their horses, names and scratches (both kinds) stay.
- [ ] Walk the mantle: every cup shows its number, no `NO HORSE`, no `NO LINK` badge on
      any cup.
- [ ] Drop one token in one cup: `ADMIN` → Race shows Pot $1 (the TV board and its toast
      only show from BETTING OPEN). Take it out: Pot $0.

### 5. Set the close time

`ADMIN` → Betting closes → pick the time and `Set` (or `+60 min`) → `CLOSES IN` appears
under the banner once betting opens.
Set it for a few minutes before post time; you still close by hand (next section).

## 6. Open betting

`ADMIN` → Race → **BETTING OPEN**. The button lights and the line under the buttons says
`BETTING OPEN`; the board takes the TV. Red text on that line instead: the reason,
verbatim, from pi5. `(gateway offline …)` after the state: pi5 kept it and sends it when
the gateway is back.

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

Two minutes out: `ADMIN` → Race → **FINAL CALL**. `FINAL CALL` pulses on the TV.

At the post: `ADMIN` → Race → **AT THE POST**. `BETTING CLOSED`, board frozen.
**Write down POT / WIN / PLACE / SHOW now**, from the big figures under the buttons —
the board leaves the TV at WINNER.

They're off: `ADMIN` → Race → **RUNNING**.

## 8. Results and the draw

`ADMIN` → Race → **WINNER**. TV goes back to the slideshow.

- [ ] WIN cup: shake it, pull **one** token. Read its number aloud. The guest with the
      other half takes the WIN prize.
- [ ] PLACE cup: one token, PLACE prize.
- [ ] SHOW cup: one token, SHOW prize.
- [ ] Pay in whole dollars, the amounts you wrote down.
- [ ] Undrawn tokens are just tokens. Nobody else wins anything.

Then: `ADMIN` → Race → **AFTER PARTY**.

## 9. Another race on the same night

- [ ] `ADMIN` → Race → **Reset betting** → OK. The cups keep their numbers and their
      horses; names and scratches stay. The line says `Reset. Pot $0`; if it names cups
      with tokens still in them, empty those (each count drops to 0 by itself) and press
      it again.
- [ ] Section 5 (close time), then section 6 (**BETTING OPEN**).
- [ ] A different field next race: `ADMIN` → Horse names → paste the new names → Save
      names, and undo / redo the scratches, before opening.

---

## If something goes wrong

| You see | It means | Do |
|---|---|---|
| `NO LINK` in the board's corner on the TV | splash can't reach pi5, or pi5 can't hear the gateway | `ADMIN` → Race. `NO LINK` there too: the gateway (cable, `DDM_LQ_SERIAL_PORT`). `LINK OK` there: the splash can't reach pi5 (Terminal 2 alive? its `PI5_URL`); restart the splash. |
| Gateway `hello` lines forever, never answered | pi5 has no state yet, or wrong port | `ADMIN` → Race → **PRE-RACE** (any state button answers it); check `DDM_LQ_SERIAL_PORT` in `pi5/.env`. |
| Cup shows `NO HORSE` | no horse on that cup | `ADMIN` → Race → Cups → that cup's row → pick the horse. |
| Cup shows `WAITING FOR GATEWAY`; on the page it's `no cup` or `offline` | the gateway has no number for it (not in the roster: a swapped-in spare), or the gateway is down, or the cup is | Gateway up and the other cups online? Then it's that cup's power, or the roster: "spare cup" below. |
| Cup count stuck, `HANDLED` | it was moved/bumped | Leave it alone five seconds. |
| Count wrong after a bump | baseline drifted | Pull all tokens, wait for 0, put them back in one pour. |
| Cup screen upside down / mirrored | orientation stepped (BOOT held 15 s) | Serial: `x`. Or touch menu (hold 3 s) → FLIP 180. |
| Cup blinks / reboots | power sag | Check the bus voltage at that cup; never USB. |
| Board shows old pot/bets on startup | pi5 persisted last session | Empty the cups, then `ADMIN` → Race → **Reset betting**. |
| Pot isn't $0 after **Reset betting** | tokens were still in the cups; the line under the button names them | Empty those cups (each count drops to 0 by itself) and press it again. |
| Board on the TV, wrong state | state didn't take | `ADMIN` → Race: the lit button is the state pi5 holds. Press the right one and read the line under the buttons. |
| A button's line is red | pi5 refused, or can't be reached; the text is the reason | `cannot reach pi5`: the phone's Wi-Fi, or pi5 is down (appendix). Anything else names the fix. |
| Wrong number on a cup after a scratch | old firmware (pre-`fc87f17`) | Flash the gateway and the cup. |
| A spare cup swapped in never gets a number | its MAC isn't in the roster; once DevPi has sent a roster the gateway gives numbers only to MACs in it | `DDM_LQ_DEV_ENDPOINTS=1`, restart pi5, then the roster curl in the appendix with the spare's MAC (tape under the cup) in the dead cup's slot; every other cup keeps its number. Blunt way: `ADMIN` → Race → Cups → **Forget cups**, power-cycle the gateway, wait for every cup to show `online`, **Adopt**, pick the horses again. |
| `ADMIN` doesn't load | pi5 down, or the phone isn't on the house Wi-Fi | On DevPi: `curl -s localhost:5000/api/quiniela` answers? No: restart pi5 (Terminal 1). Yes: the phone. Meanwhile, the appendix. |

## The rules, for anyone who asks

- $1 a token. Drop it in the cup of the horse you like. Keep the other half.
- After the race, one token is drawn from the WIN cup, one from PLACE, one from SHOW.
  The drawn token's owner takes that prize.
- Prizes are 60 / 25 / 15 percent of the pot, whole dollars, WIN takes the rounding.
- Fewer tokens in a cup, better shot at the draw.
- Scratched before Friday: the alternate takes over, with its own number.
  Scratched on Saturday: tokens refunded.
- Totals based on cheap Chinese electronics. Final results hand counted.

---

## Appendix — if the page is down

The same actions as curls, typed on DevPi. State numbers are the ones at the top.

A state (0–6):
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
```

Reset betting (cups, horses, names and scratches are kept; the reply names any cups with tokens still in them):
```bash
curl -s -X POST localhost:5000/api/quiniela/reset
```

A horse on a cup (cup 1 → horse 7; the cup number is the screen's `CUP n` plus one, the horse the number the board lists):
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"horse 1 7"}'
```

The figures (what the page shows):
```bash
curl -s localhost:5000/api/quiniela | python3 -c "import sys,json;d=json.load(sys.stdin);print('link',d['link_ok'],'state',d['race_state'],'pot',d['pot'],'prizes',d['prizes'])"
```

The cups (number, MAC, horse, online, count):
```bash
curl -s localhost:5000/api/lq/snapshot | python3 -c "import sys,json;[print(c['cup'],c['mac'],c['horse'],c['online'],c['count']) for c in json.load(sys.stdin)['cups']]"
```

The splash's view of the board (must say `link True`):
```bash
curl -s localhost:5001/api/quiniela | python3 -c "import sys,json;d=json.load(sys.stdin);print('link',d['link_ok'],'state',d['race_state'],'pot',d['pot'])"
```

Dev routes (need `DDM_LQ_DEV_ENDPOINTS=1` in `pi5/.env` and a pi5 restart; 404 without it):

Adopt (the cups heard so far become the roster; do it once every cup is online):
```bash
curl -s -X POST localhost:5000/api/lq/dev/roster/adopt
```

Forget cups (roster and assignments dropped, pi5 mirrors the gateway again; names and scratches stay; nothing is sent to the gateway):
```bash
curl -s -X POST localhost:5000/api/lq/dev/roster/clear
```

Name the roster yourself (twenty MACs, position = cup number, `""` for an empty slot; the spare cup's MAC goes in the dead cup's slot):
```bash
curl -s -X POST localhost:5000/api/lq/dev/roster -H 'Content-Type: application/json' -d '{"macs":["A0:B7:65:00:00:01","A0:B7:65:00:00:02","","","","","","","","","","","","","","","","","",""]}'
```

`POST /api/lq/dev/reset` still works and does both, Reset betting and then Forget cups. It is deprecated; use the two above.

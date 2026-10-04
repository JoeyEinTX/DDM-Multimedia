# La Quiniela — Race Night Runbook

One action per line. Do them in order. The night is run from two pages:

- `DASH`, the dashboard at `http://joeydevpi.local:5000/` (the touchscreen). Its buttons
  run the LEDs **and** the race.
- `ADMIN`, the page at `http://joeydevpi.local:5000/quiniela/admin`, on a phone: names,
  scratches, the closing time, **Reset betting**, the Horses list. It is also in the
  dashboard's menu (☰ → La Quiniela Admin).

Commands are typed on DevPi only in the appendix (if the pages are down).

There is **one race state**. A dashboard button sets it, the cups and the TV follow:

| `DASH` button | Race state |
|---|---|
| WELCOME · TEST · STANDBY | 0 PRE-RACE |
| 60 MIN · 30 MIN | 1 BETTING OPEN |
| FINAL CALL | 2 FINAL CALL |
| AT THE GATE | 3 AT THE POST |
| THEY'RE OFF! · CHAOS · FINISH | 4 RUNNING |
| SET WINNERS (once confirmed) · HEARTBEAT | 5 WINNER |
| RESET | 6 AFTER PARTY |

The seven buttons under `ADMIN` → Race set the same state, the current one lit; they are
the same thing without the LEDs. Whichever you press, the other page shows it within five
seconds (the lit button on `ADMIN`, the ticker on `DASH`). The board owns the TV in 1–5
(in WINNER it is the **results board**, prizes and all) and hands it back in 0 and 6.

---

## 0. Before the party (do these once, weeks out)

- [ ] pi5 and the splash display run as services and come up on their own after a
      power cycle. Until they do, they're two foreground terminals (section 3).
- [ ] The TV kiosk points at the splash on **5001**, not 5000.
      (Files: `splash_display/deploy/kiosk.sh`, `splash_display/deploy/splash_display.service`,
      `splash_display/deploy/autostart_setup.md`. pi5 has no service file yet.)
- [ ] `impact.ttf` is in `~/.fonts/` on DevPi and `fc-cache -f` has been run, so the
      board on the TV uses Impact, not the fallback.
- [ ] The board on the TV is the tote look (`dots`, the default: amber dots on black
      tiles, each row one strip with the bets at its end, a long name scrolling). Check it
      on the TV itself: with betting open (`ADMIN` → Race → **BETTING OPEN**) and names
      that scroll on the board, drop a token while you watch: the count must tick and the
      crawl and the names keep moving without a hitch (`d` on the kiosk's keyboard shows
      the FPS). `impact` (the board before the tote look) and `numbers` (the tote look's
      figures, names in Impact) stay: `http://joeydevpi.local:5001/?look=impact` from a
      laptop or phone; on the TV `pkill -f chromium`, then
      `SPLASH_URL='http://localhost:5001/display?look=impact' ~/DDM-Multimedia/splash_display/deploy/kiosk.sh &`.
      To change the default: `QUINIELA_LOOK` in `splash_display/config.py`, restart the
      splash and the kiosk.
- [ ] Every cup and the gateway: flashed from the current firmware (cup v0.7, the brownout
      build, and the protocol v2 gateway), sleeve installed, orientation right, touch working
      (`p` in serial shows touches), `CAL 10` done with the sleeve on, then **tared empty**
      (cup empty → hold the screen → `TARE` → `YES`) so its saved empty reading is fresh, and
      a 10-token drop/dump test passes.
- [ ] Every cup: hold the screen → `HORSE` → tap the top half of the number to go up, the
      bottom half to go down, to its post (the cup for post 1 → 1, … post 20 → 20) →
      `SET`. The number is the cup's own: it keeps it across power and across **Reset
      betting**, so this is done once. A cup showing `NO HORSE` hasn't been set. Nothing
      is written on tape; pi5 has no list of cups to keep.
- [ ] Power: one supply for all cups, fed from the middle of the bus, bulk cap per cup.
      No cup on a USB port. Boot all twenty at once and watch for any that blink or
      reboot — that's a sag, not a bug.
- [ ] Gateway on DevPi's USB; `pi5/.env` has `DDM_LQ_SERIAL_PORT` set to it. No other
      flag: the night needs none and there are no dev routes.
- [ ] On the bench with every cup powered: `ADMIN` → Race → Horses shows every horse 1–20
      `● online` and the line above the list says `20 cups online · 0 with no horse`. A
      `○ no cup` row: no cup says that number (walk the mantle for the `NO HORSE` screen
      and set it). A `⚠ 2 CUPS` row: two cups say the same number; set one of them right.
- [ ] Spare cup, spare gateway, spare CYD, flashed and in the drawer. Tare the spare cup
      empty before it goes in: a cup remembers its last count and would report it for the
      first ~30 s after power-up.

## 1. Derby week — the field

- [ ] Tuesday after the draw: `ADMIN` → Race info → the race's name (leave it empty for
      KENTUCKY DERBY), the date and the post time on a Central clock (5:57 PM for
      Churchill's 6:57 PM ET) → Save race info. The line under it reads back
      `KENTUCKY DERBY 2027 · post 2027-05-01 5:57 PM CDT`. The TV's countdown slide
      counts down to it (it stays out of the slideshow while no post time is set), the
      roster slide prints it, and Reset betting never clears it.
- [ ] Same visit: `ADMIN` → Horse names → paste the twenty names in post order, one per
      line, plus the also-eligibles on lines 21–24 → Save names.
- [ ] Each scratch before Friday 9 a.m. ET: `ADMIN` → Scratches → In the field → that
      horse's row → pick the replacement number (the list offers the unused
      also-eligibles, 21 first) and its name → Scratch. The board lists the new number at
      the bottom and the cup that was that horse becomes the new number on its own, tokens
      and all (nothing moves on the mantle; a cup set to the old number later becomes the
      new one the moment it hears the gateway).
- [ ] Wrong? `ADMIN` → Scratches → Scratched → `Undo` on that line. Undo the most recent
      scratch first if there were several (the page greys out the others).
- The names and scratches entered here feed La Subasta too: the auction at
  `/la-subasta` lists the field as it stands, by number (22 OCELLI, no 9). Nothing is
  entered in La Subasta. A scratch made during the auction voids its bids on that horse
  (after the auction locks, the owner's purchase too) and guest phones drop the horse on
  their own. Undo puts the horse back in the field with its auction bids and, after the
  lock, its owner.
- Before the auction locks, buy the horses nobody bid on (mark yourself **No cap** on
  `/la-subasta/admin` and take them at the minimum bid): the lock warns you and lists them,
  and locks anyway only if you confirm.

## 2. Party day — setup, before guests arrive

- [ ] Cups on the mantle, sleeves in, powered. Walk the mantle: every screen shows its
      number. A screen showing `NO HORSE` hasn't been set: hold it → `HORSE` → its number
      → `SET`. A `NO LINK` badge in a corner means that cup can't hear the gateway (not up
      yet, or too far).
- [ ] Gateway plugged into DevPi.
- [ ] Once the apps are up (section 3), watch the TV's slideshow for a lap: the countdown
      slide counts down to today's post under `KENTUCKY DERBY 2027`, and the roster slide
      lists the field by number, replacements under their own numbers, with the odds (a dim
      `—` where there are none: start the odds poller or type them, appendix). When the
      board is up its crawl carries the time, `POST IN 1:14` and the weather.

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

- [ ] `ADMIN` → Race → Horses: every horse 1–20 `● online`, no `○ no cup`, no `⚠`.
      `○ no cup`: the cup for that post isn't set or isn't powered (walk the mantle).
      `● offline` or `⚠ 2 CUPS`: table at the end.
- [ ] `ADMIN` → Race → **Reset betting** → OK. The line under the button says
      `Reset. Pot $0 · 20 cups online`. If it names horses with tokens still in their
      cups, empty those cups and press it again. Nothing else changes: the cups keep their
      numbers (they're the cups' own), names and scratches (both kinds) stay.
- [ ] Walk the mantle: every cup shows its number, no `NO HORSE`, no `NO LINK` badge on
      any cup.
- [ ] Drop one token in one cup: `ADMIN` → Race shows Pot $1 (the TV board and its toast
      only show from BETTING OPEN). Take it out: Pot $0.

### 5. Set the close time

`ADMIN` → Betting closes → pick the time and `Set` (or `+60 min`) → `CLOSES IN` appears
under the banner once betting opens.
Set it for a few minutes before post time; you still close by hand (next section).

## 6. Open betting

`DASH` → **60 MIN** (later **30 MIN**: same state, the next LED animation). The
notification says `Animation: BETTING_60 · BETTING OPEN` and the board takes the TV.
`Error: … · BETTING OPEN` means the LED controller didn't answer and the race state was
set all the same; `race state not set (…)` means pi5's La Quiniela side refused, and says
why.

From the phone instead: `ADMIN` → Race → **BETTING OPEN**. The button lights and the line
under the buttons says `BETTING OPEN`. Red text on that line: the reason, verbatim, from
pi5. `(gateway offline …)` after the state: pi5 kept it and sends it when the gateway is
back.

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

Two minutes out: `DASH` → **FINAL CALL**. `FINAL CALL` pulses on the TV.

At the post: `DASH` → **AT THE GATE**. `BETTING CLOSED`, board frozen: pi5 keeps the pot,
the prizes and every cup's bets as they are now, and the TV shows those until the race is
over and paid (section 8), through a reload of the TV or a restart of pi5.
(Backup only, if you like: a photo of `ADMIN` → Race's figures now; they are the same.)

The cash box can be counted from now on: it is the first step of section 8. The scales
are estimates, so the pot you pay from is your count of the BETS compartment.

They're off: `DASH` → **THEY'RE OFF!** (then **CHAOS**, **FINISH** as the race runs: all
three are RUNNING).

(`ADMIN` → Race has the same states under their own names: FINAL CALL, AT THE POST,
RUNNING.)

## 8. Results and the draw

`DASH` → **SET WINNERS** → pick WIN, PLACE, SHOW (each picker says the number and the
name, `19 · GOLDEN TEMPO`; a horse that drew in shows under its own number) → **CONFIRM
RESULTS**. That is WINNER: the LEDs light the three cups, the three cups' screens say WIN,
PLACE, SHOW, and the TV flips to the **results board**, `OFFICIAL RESULTS`: three rows,
WIN / PLACE / SHOW, each with the horse's cloth and name, the bets its cup held and its
prize, big, at the right; the pot above. Opening the pickers changes nothing; confirming
does. (WINNER without results, from **HEARTBEAT** or `ADMIN` → WINNER: the TV says
`OFFICIAL RESULTS COMING` over the frozen board until the results are confirmed.)
Each tap fills one slot, the next empty one (WIN, then PLACE, then SHOW); to change a
pick, tap its slot on the right first, then the new horse (its × empties it). A horse
already in another slot is refused, never moved. A horse dimmed and tagged red `NO BETS`
had nothing in its cup at the post and can't pay: skip it and tap the next finisher in
its place (nobody bet the show horse: 4th place is SHOW). Read the three listed over
**CONFIRM RESULTS** before you press it.
The results are saved whether or not the LED controller answers: a red `Results set: …
· LEDs unreachable` means the TV and the cups have them and only the LEDs missed them.

- [ ] **Count the cash**, any time after AT THE GATE and always before you pay and before
      **RESET** (in AFTER PARTY the box is read-only). Open the cash box's BETS
      compartment, count the dollars. `ADMIN` → Race → Counted pot → type the
      number → **Save count**. The line under the buttons reads `Saved: counted $152
      (the scales said $154, −$2). WIN $91 · PLACE $38 · SHOW $23.` From now on
      the pot and the three prizes, on `ADMIN` and on the TV (tagged `HAND COUNTED`
      beside the pot), are those: the same 60 / 25 / 15, whole dollars. The bets per
      horse stay as the scales read them, and the scales' figure and the difference
      sit beside the box for information only: a few dollars either way is the
      scales, nothing to fix. Typed it wrong, or counted again after a late bet? Type
      it again and **Save count** (it overwrites), or **Clear** to go back to the
      scales' figures. No `HAND COUNTED` tag on the TV means no count is saved.
- [ ] WIN cup: shake it, pull **one** token. Read its number aloud. The guest with the
      other half takes the WIN prize.
- [ ] PLACE cup: one token, PLACE prize.
- [ ] SHOW cup: one token, SHOW prize.
- [ ] Pay in whole dollars, the amounts on the results board: your counted ones, with the
      `HAND COUNTED` tag by the pot. Emptying the cups doesn't change them: the board
      shows what each cup held when betting closed, and so does a TV reloaded, or pi5
      restarted, in between; the count is kept with those figures.
- [ ] Undrawn tokens are just tokens. Nobody else wins anything.

Then, with the count entered and the prizes paid: `DASH` → **RESET** → OK (clears the
results, LEDs off): AFTER PARTY, and the TV goes back to the slideshow. The saved
count stays and shows read-only on `ADMIN` until **Reset betting**. Or `ADMIN` → Race →
**AFTER PARTY**, which hands the TV back too and leaves the results and the LEDs as
they are.

## 9. Another race on the same night

- [ ] `ADMIN` → Race → **Reset betting** → OK. The cups keep their numbers; names and
      scratches stay; the figures at the post and the hand count go. The line says
      `Reset. Pot $0`; if it names horses with tokens still
      in their cups, empty those (each count drops to 0 by itself) and press it again.
- [ ] Section 5 (close time), then section 6 (`DASH` → **60 MIN**).
- [ ] A different field next race: `ADMIN` → Horse names → paste the new names → Save
      names, and undo / redo the scratches, before opening.

---

## If something goes wrong

| You see | It means | Do |
|---|---|---|
| `NO LINK` in the board's corner on the TV | splash can't reach pi5, or pi5 can't hear the gateway | `ADMIN` → Race. `NO LINK` there too: the gateway (cable, `DDM_LQ_SERIAL_PORT`). `LINK OK` there: the splash can't reach pi5 (Terminal 2 alive? its `PI5_URL`); restart the splash. |
| Gateway `hello` lines forever, never answered | pi5 isn't reading that port (wrong `DDM_LQ_SERIAL_PORT`, or pi5 is down); pi5 answers every hello with its state by itself | Check `DDM_LQ_SERIAL_PORT` in `pi5/.env` and that Terminal 1 said `bridge started`. |
| Cup shows `NO HORSE` | that cup hasn't been set | Hold the screen → `HORSE` → tap to its number → `SET`. |
| A horse is `○ no cup` on the page | no cup says that number | Walk the mantle: the cup for that post shows `NO HORSE` (set it) or another number (set it right), or it has no power. |
| A horse is `● offline` on the page; the cup's screen has a `NO LINK` badge | the cup can't hear the gateway, or the gateway is down, or the cup is | Gateway up and the other horses online? Then it's that cup's power. Dead for good: "cup died" below. |
| A horse is `⚠ 2 CUPS` on the page | two cups say the same number | Find both (the page can't say which); set one of them to its right number. |
| Cup count stuck, `HANDLED` | it was moved/bumped | Leave it alone five seconds. |
| Count wrong after a bump | baseline drifted | Pull all tokens, wait for 0, put them back in one pour. |
| Cup screen upside down / mirrored | orientation stepped (BOOT held 15 s) | Serial: `x`. Or touch menu (hold 3 s) → FLIP 180. |
| Cup blinks / reboots | power sag | Check the bus voltage at that cup; never USB. |
| A cup rebooted (power blip) with tokens in it | it comes back with its count: it reports the count it saved through its 30 s warm-up, then re-reads the pile from the weight (`[boot] … keep zero, count N` on its serial) | Nothing. Leave it alone for the first 35 s. |
| An empty cup comes back from a power blip reporting 1 | its empty reading drifted more than half a token across the power-off, so the boot read took the cup for a pile of one | Hold the screen → `TARE` → `YES` (cup empty). A pull-and-refill does not clear it. |
| No countdown slide, or the wrong race on it; the roster's post time blank | Race info has no post time, or last year's | `ADMIN` → Race info → the date and the post time → Save race info (section 1). A countdown on screen shows it on its next second; one left out of the slideshow comes back on the next lap (the playlist is rebuilt every ~35 slides, a few minutes). |
| No roster slide | no horse in the field has a name | `ADMIN` → Horse names → Save names (section 1); it joins on the next lap. |
| The crawl has no weather | pi5 has no `WEATHER_API_KEY`, or no internet yet | Nothing needed: the item is left out. With a key it shows within `WEATHER_CACHE_MINUTES` of pi5's start. |
| Board shows old pot/bets, or last race's results, on startup | pi5 persisted last session (it keeps the results, the figures at the post and the hand count until **Reset betting**) | Empty the cups, then `ADMIN` → Race → **Reset betting**. `DASH` drops the old results tote by itself within 5 s. |
| Pot isn't $0 after **Reset betting** | tokens were still in the cups; the line under the button names them | Empty those cups (each count drops to 0 by itself) and press it again. |
| No Counted pot box on `ADMIN`, or Save answers `The count can only be entered after betting closes …` | the box shows from AT THE POST to WINNER; before that betting is still open (nothing to count against yet), after it (AFTER PARTY) a saved count is read-only | Close betting first (section 7: `DASH` → **AT THE GATE**); the box appears within five seconds. |
| A count typed wrong, or the cash changed after a late bet | the count is whatever was saved last | `ADMIN` → Race → Counted pot → type it again → **Save count** (it overwrites), or **Clear** to go back to the scales' figures. |
| The count is wrong or missing and **RESET** was already pressed | AFTER PARTY makes the box read-only | `ADMIN` → Race → **WINNER**: the box is back (the results board is gone after RESET; `ADMIN`'s figures carry the prizes). Enter it and pay from those. |
| The scales and the count differ by a few dollars | the scales are estimates ("totals based on cheap Chinese electronics") | Nothing: pay from the count. The difference is shown for information, nothing blocks. |
| `Saved: … NOT stored: pi5 could not write it…` in red under Save count | pi5 holds the count but its database refused the write | Pay from it. If pi5 restarts before the draw is paid, enter the count again. |
| Board on the TV, wrong state | state didn't take, or somebody pressed another button | `ADMIN` → Race: the lit button is the state pi5 holds (the ticker on `DASH` says the same). Press the right one, on either page, and read what it answers. |
| TV stays on `OFFICIAL RESULTS COMING` | it is WINNER and pi5 has no results: they were never confirmed, or **CONFIRM RESULTS** answered `Error: results not saved …` (pi5 could not write the file; the LED controller has nothing to do with it) | `DASH` → **SET WINNERS** → confirm again. Meanwhile the prizes are in the board's header (WIN / PLACE / SHOW under the pot, the figures at the post); draw and pay from those. |
| `Results set: … · LEDs unreachable` in red on `DASH` | the results are saved, on the TV and on the cups; only the LED controller missed them | Nothing for the results. The LEDs: check the LED controller (the device icon, top left of `DASH`). |
| pi5 restarted, or the TV reloaded, during the draw | nothing is lost: pi5 keeps the results and the figures at the post | Nothing. The TV finds pi5 again by itself and shows the results board as it was. |
| Results board shows fewer bets or smaller prizes than at the close | betting was reopened before the draw was paid (a `DASH` WELCOME / TEST / STANDBY / 60 MIN / 30 MIN, `ADMIN` PRE-RACE / BETTING OPEN, or Reset betting): pi5 dropped the figures at the post, and a later close took the cups as they were then | Pay from the backup photo (section 7), if there is one; otherwise count what those cups held and count the cash again (the hand count went with the figures). |
| `DASH` button says `Error: …` | the LED controller didn't answer | If the text goes on `· BETTING OPEN` (the state's name), the race state was set and only the LEDs are missing: check the LED controller (the device icon, top left of `DASH`). |
| A button's line is red | pi5 refused, or can't be reached; the text is the reason | `cannot reach pi5`: the phone's Wi-Fi, or pi5 is down (appendix). Anything else names the fix. |
| Wrong number on a cup after a scratch | old firmware (pre-`45510f8`, protocol v2) | Flash the gateway and the cup. |
| A cup died | it's gone for good | Spare cup: power it, hold the screen, `HORSE` → the dead cup's number → `SET`, set it down in that post; empty the dead cup's tokens into it. The page shows that horse `● online` again; the dead cup just stops being heard. |
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

A state (0–6), what an `ADMIN` button does:
```bash
curl -s -X POST localhost:5000/api/quiniela/cmd -H 'Content-Type: application/json' -d '{"cmd":"state 1"}'
```

The state now, and the `DASH` button that set it (`mode` is null when `ADMIN` or a curl did):
```bash
curl -s localhost:5000/api/quiniela/mode
```

The results, what `DASH` → SET WINNERS → CONFIRM RESULTS does (horse numbers; it sets WINNER, and the TV flips to the results board; the reply's `leds` says whether the LED controller took them, the results are saved either way):
```bash
curl -s -X POST localhost:5000/api/results -H 'Content-Type: application/json' -d '{"win":19,"place":1,"show":22}'
```

Reset betting (cups, horses, names and scratches are kept; the reply names any cups with tokens still in them):
```bash
curl -s -X POST localhost:5000/api/quiniela/reset
```

The figures (what the page shows; `at the post` is what the TV shows from AT THE GATE on):
```bash
curl -s localhost:5000/api/quiniela | python3 -c "import sys,json;d=json.load(sys.stdin);c=d.get('closing') or {};print('link',d['link_ok'],'state',d['race_state'],'pot',d['pot'],'prizes',d['prizes'],'results',d['results'],'at the post',c.get('pot'),c.get('prizes'),'counted',d.get('pot_counted'),'scale',d.get('pot_scale'))"
```

The hand count, what `ADMIN` → Race → Counted pot → Save count does (whole dollars; AT THE POST, RUNNING or WINNER only; `{"amount":null}` clears it):
```bash
curl -s -X PUT localhost:5000/api/quiniela/counted_pot -H 'Content-Type: application/json' -d '{"amount":152}'
```

The cups pi5 hears (MAC, the horse each says it is, online, count):
```bash
curl -s localhost:5000/api/lq/snapshot | python3 -c "import sys,json;[print(c['mac'],c['horse'],c['online'],c['count']) for c in json.load(sys.stdin)['cups']]"
```

The splash's view of the board (must say `link True`):
```bash
curl -s localhost:5001/api/quiniela | python3 -c "import sys,json;d=json.load(sys.stdin);print('link',d['link_ok'],'state',d['race_state'],'pot',d['pot'])"
```

The race's name and post time, what `ADMIN` → Race info → Save does (date and time on a Central clock; both `""` clear the post time):
```bash
curl -s -X PUT localhost:5000/api/quiniela/race -H 'Content-Type: application/json' -d '{"name":"Kentucky Derby","date":"2027-05-01","time":"17:57"}'
```

The track's odds for the roster slide (optional; La Quiniela pays no odds). Fetched every 5 minutes while this runs (needs `ANTHROPIC_API_KEY` and internet), or typed from the program:
```bash
curl -s -X POST localhost:5000/api/quiniela/odds/start
curl -s -X PUT localhost:5000/api/quiniela/odds -H 'Content-Type: application/json' -d '{"odds":{"1":"5-2","2":"8-1","21":"30-1"}}'
```

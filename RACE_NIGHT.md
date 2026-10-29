# La Quiniela — Race Night Runbook

One action per line. Do them in order. The night is run from two pages, open on the iPad
as two tabs (section 2):

- `DASH`, the Control Center at `http://joeydevpi.local:5000/`. Its buttons run the LEDs
  **and** the race: it is the one place the race state changes.
- `ADMIN`, the page at `http://joeydevpi.local:5000/quiniela/admin`: names, scratches,
  the closing time, the counted pot, **Reset betting**, the Horses list, and the race
  state, read-only. It is also in the dashboard's menu (☰ → La Quiniela Admin).

pi5 and the splash start on their own when DevPi boots (section 0). Commands are typed on DevPi
only to set that up and to start fresh before the party, and in the appendix (if the pages are
down).

There is **one race state**. A dashboard button sets it, the cups and the TV follow:

| `DASH` button | Race state |
|---|---|
| WELCOME · STANDBY | 0 PRE-RACE |
| 60 MIN · 30 MIN | 1 BETTING OPEN |
| FINAL CALL | 2 FINAL CALL |
| AT THE GATE | 3 AT THE POST |
| THEY'RE OFF! · CHAOS · FINISH | 4 RUNNING |
| SET WINNERS (once confirmed) · HEARTBEAT | 5 WINNER |
| RESET | 6 AFTER PARTY |

**TEST** is an LED test only (the colour wheel): it never changes the race state, so it is
safe at any time, the figures at the post and the hand count included.

`ADMIN` → Race shows the state read-only (`3 · AT THE POST`) and follows `DASH` within five
seconds. To change it, use `DASH`: the link under the state on `ADMIN` takes you there (if
`DASH` is down, the appendix). The board owns the TV in 1–5 (in WINNER it is the **results
board**, prizes and all) and hands it back in 0 and 6.

**Reopening betting asks first.** While pi5 holds the figures at the post, which is from AT THE
POST to AFTER PARTY (3–6: after a **RESET** too), WELCOME, STANDBY, 60 MIN, 30 MIN and FINAL CALL
would reopen betting, so `DASH` asks before it sends anything: `This reopens betting. The
figures at the post (pot $86) and the hand count ($80) will be cleared.` (the real figures; no
hand count, no second half). **Cancel** sends nothing. **Reopen betting** sends it, with your
yes: pi5 clears the figures at the post and the hand count, and the next AT THE GATE takes new
ones. pi5 refuses the same state without that yes (an out-of-date page, a curl): `race state
not set (This reopens betting: …)` in red, nothing cleared. The figures and the count are
dropped only by **Reset betting** (`ADMIN`, which asks too and names them) or a reopen you
confirmed. In 0–2 there is nothing to clear and they go at once. With the results set the
buttons stay greyed out, as ever.

**AUTO / MANUAL** (the switch at the top of `DASH`): race night is **MANUAL**. AUTO is the
old racing demo: a timer walks its own race through the LED animations by itself (La
Quiniela, the cups and the TV do not follow it). While it is on every race button is greyed
out and says `switch to MANUAL`. Switched to AUTO at AT THE POST, RUNNING or WINNER, `DASH`
asks first.

---

## 0. Before the party (do these once, weeks out)

- [ ] DevPi's own settings and the keys go in `pi5/.env`, never in a tracked file (the repo is
      public; `pi5/.env` is git-ignored). Once, on DevPi:

```bash
cd ~/DDM-Multimedia && cp pi5/.env.example pi5/.env && nano pi5/.env
```

  - `DDM_LQ_SERIAL_PORT`: the gateway's port, the `/dev/serial/by-id/...` path from
    `ls -l /dev/serial/by-id/` (never `/dev/ttyUSB0`). Empty: no cups.
  - `DDM_ESP32_IP`: the LED controller's address (its little screen shows it at boot, or
    the router's list of clients). Empty: the LEDs are off. The Control Center's device panel
    writes this line when the address is changed there.
  - `DDM_WEATHER_API_KEY`: the WeatherAPI.com key, from the account at weatherapi.com.
    Empty: no weather on the dashboard or in the TV's crawl.
  - `DDM_ANTHROPIC_API_KEY`: only for the track's odds poller (appendix). Empty: no poller.
  - `DDM_LS_ADMIN_PIN`: the host's PIN for La Subasta's admin page. Choose your own (the
    template has `change-me`, which the installer warns about). Empty: the page and its routes
    are off. The Control Center and `ADMIN` have no PIN.
  - The rest of the file is commented out: the defaults are right. pi5 says once at start
    which of these is missing (`journalctl -u ddm-pi5 -n 30`), and the installer below checks
    each one (a `WARN` line per empty one; nothing blocks).
  - The splash needs no `.env` on DevPi (it finds pi5 on the same box). A splash on another
    machine: `cp splash_display/.env.example splash_display/.env`, set `DDM_PI5_URL`.
  - A box that still has a local edit of `pi5/config.py` from before (DevPi did, until
    2026-10): "Updating DevPi" at the end moves it into `pi5/.env` once.
- [ ] pi5 and the splash run as the services `ddm-pi5` and `ddm-splash` and come up on their
      own after a power cycle. Install them once, on DevPi, as `joey` (stop any copy started by
      hand first: Ctrl+C in its terminal):

```bash
~/DDM-Multimedia/deploy/install_services.sh
```

- It checks first and installs nothing on a `STOP` line (each says what to do): `joey` in
  `dialout`, every Python package pi5 and the splash import, nothing started by hand on 5000
  or 5001. A `WARN` line is only a warning (the gateway unplugged: pi5 opens it when it
  appears). `--dry-run` shows all of it and changes nothing; `--uninstall` removes it. Running
  it again (after a pull that changed `deploy/`) is fine.
- [ ] Check: `systemctl status ddm-pi5 ddm-splash`, both `active (running)`. The logs:
      `journalctl -u ddm-pi5 -f` and `journalctl -u ddm-splash -f` (Ctrl+C stops the reading,
      not the app). Restart: `sudo systemctl restart ddm-pi5` (or `ddm-splash`, or both).
      Updating DevPi and running an app in a terminal: the end of this runbook.
- [ ] `ddm-tv`, by the cups, shows `/board` ("The TV boxes", below), set up from DevPi with
      `~/DDM-Multimedia/deploy/tv_box.sh install joey@ddm-tv.local --reboot`. Check the TV: the
      La Quiniela board, no pointer, and it never blanks. `ddm-tv2`, once it exists, the same with
      `--server http://joeydevpi.local:5001/slides`.
- [ ] DevPi's own kiosk stays off: `~/DDM-Multimedia/deploy/install_services.sh --kiosk status`
      says off (`--kiosk off` if not). It is the fallback if the TV box dies: `--kiosk on`, from
      the next login, Chromium full screen on `http://localhost:5001/board` on DevPi's screen.
      (Files: `splash_display/deploy/kiosk.sh`, `deploy/kiosk_autostart.py`.)
- [ ] `impact.ttf` is in `~/.fonts/` on DevPi and `fc-cache -f` has been run, so the
      board on the TV uses Impact, not the fallback.
- [ ] The board on the TV is the tote look (`dots`, the default: amber dots on black
      tiles, each row one strip with the bets at its end, a long name scrolling). Check it
      on the TV itself: with betting open (`DASH` → **60 MIN**) and names
      that scroll on the board, drop a token while you watch: the count must tick and the
      crawl and the names keep moving without a hitch (`d` on a keyboard plugged into the TV box shows
      the FPS). `impact` (the board before the tote look) and `numbers` (the tote look's
      figures, names in Impact) stay: `http://joeydevpi.local:5001/?look=impact` from a
      laptop or phone; on the TV,
      `~/DDM-Multimedia/deploy/tv_box.sh install joey@ddm-tv.local --server http://joeydevpi.local:5001/board?look=impact`
      (install again without `--server` to bring the default back).
      To change the default: `DDM_QUINIELA_LOOK=impact` in `splash_display/.env`
      (copy `splash_display/.env.example`), then `sudo systemctl restart ddm-splash` and
      restart the TV's browser: `ssh joey@ddm-tv.local pkill -f ddm-tv/chromium` (the box starts
      it again in 3 s; a setting is not a new page revision, so the TV doesn't reload on its own).
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
      flag: the night needs none and there are no dev or test routes, La Subasta's included.
      No `DDM_LQ_DEV_ENDPOINTS` line in `pi5/.env` on the night (the installer WARNs about one:
      it does nothing, delete it).
- [ ] On the bench with every cup powered: `ADMIN` → Race → Horses shows every horse 1–20
      `● online` and the line above the list says `20 cups online · 0 with no horse`. A
      `○ no cup` row: no cup says that number (walk the mantle for the `NO HORSE` screen
      and set it). A `⚠ 2 CUPS` row: two cups say the same number; set one of them right.
- [ ] Spare cup, spare gateway, spare CYD, flashed and in the drawer. Tare the spare cup
      empty before it goes in: a cup remembers its last count and would report it for the
      first ~30 s after power-up.

## Before the party: start fresh

Testing leaves names, race info, scratches, results and La Subasta's guests and bids on
DevPi. One command clears all of it, after a backup it checks file by file.

- **When:** on DevPi, after the last test and before you enter the field (section 1): it
  clears the names, the race info and the scratches too. **Never after betting opens.** It
  is not Reset betting (section 4), which keeps names, race info and scratches.
- [ ] Stop pi5: `sudo systemctl stop ddm-pi5` (the command refuses while pi5 runs, and says
      how to stop it). The splash can stay up.
- [ ] The list first. It changes nothing; read what it will clear and what it keeps:

```bash
cd ~/DDM-Multimedia && python3 pi5/tools/wipe.py --dry-run
```

- [ ] Then the real run. It shows the same list and asks you to type `WIPE`; anything else
      stops with nothing changed. It backs up every file it touches to
      `backups/wipe-<date-time>/`, checks each copy, clears, checks nothing is left, and
      prints the command that undoes it:

```bash
python3 pi5/tools/wipe.py
```

- **Clears:** La Quiniela's names, race info, scratches, closing time, race state (back to
  0 PRE-RACE), figures at the post, counted pot, results, cup cache and readings, and logs,
  plus the old Race Setup file; La Subasta's guests, bids, owners, payouts, auction state
  and changed settings; the splash's old betting log.
- **Keeps:** the code, `pi5/config.py`, `pi5/.env` (DevPi's own settings and the keys),
  `pi5/.env.example`, the splash's config and its `.env` if it has one, the LEDs'
  animation setup and the backups.
- **To undo it,** pi5 stopped: `python3 pi5/tools/wipe.py --restore backups/wipe-<date-time>`,
  then type `RESTORE`. Every file comes back byte for byte (what is there is backed up first).
- [ ] Start pi5 again: `sudo systemctl start ddm-pi5`. `ADMIN`: no names, no race info, no scratches,
      `0 · PRE-RACE`, Pot $0. `DASH`: no results. `/la-subasta`: no guests, no bids.
- [ ] What the wipe can't reach (it prints this too):
  - The cups keep their own count: empty every cup, then `ADMIN` → Race says Pot $0. A cup
    that still counts tokens when empty: hold its screen → `TARE` → `YES`.
  - The cups keep their own numbers: a cup a replacement scratch moved (9 became 22) still
    says 22. Walk the mantle and set any cup that isn't its post (hold → `HORSE` → `SET`).
  - The gateway keeps the old race state and its list of cups until it loses power: unplug
    it from DevPi and plug it back in before you start pi5 again.
  - Phones that joined La Subasta before rejoin on their own: the page sends them back to
    its join screen ("Welcome back. Join again for this year's auction."). Nothing to do on them.

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
- Every Scratch asks first, in its row, and sends nothing until you answer: it says what it will do
  (`Scratch #9 THE PUMA: #22 OCELLI draws in.`, or `…, no replacement: its tokens are handed back to
  re-bet.`); check it, then **Confirm scratch** (Cancel keeps what you typed). With no name and
  *No replacement* unticked it asks which it is.
- [ ] Wrong? `ADMIN` → Scratches → Scratched → `Undo` on that line. Undo the most recent
      scratch first if there were several (the page greys out the others). Undo puts the
      names back as they were before the scratch: a name the scratch gave the new number
      is cleared, one entered ahead in Horse names stays.
      Undo of a replacement is refused from 60 MIN to WINNER while the replacement's cup
      holds tokens: they are bets on the new number, and they would go with the cup back to the
      old one. The row says `Cup 9 holds 4 tokens bet on #22. Hand them back and empty the
      cup, then Undo.` Hand the tokens back (empty the cup), and Undo goes through as ever.
      Undo works at any time, however soon after the scratch; a cup that was off catches up
      when it's back on. `⏳ renumbering` on a row (Race → Horses, not the Scratches rows) means
      a cup hasn't followed for a minute (hover or tap it: which cup, and whether it's off);
      switch it on or check its screen. An offline row (Race → Horses, not the Scratches rows)
      says when its cup was last heard: a date (`heard Sep 20`) is a leftover record from an
      old database, not a cup to look for.
- The names and scratches entered here feed La Subasta too: the auction at
  `/la-subasta` lists the field as it stands, by number (22 OCELLI, no 9). Nothing is
  entered in La Subasta. A scratch made during the auction voids its bids on that horse
  (after the auction locks, the owner's purchase too) and guest phones drop the horse on
  their own. Undo puts the horse back in the field with its auction bids and, after the
  lock, its owner. The auction's own night is its section below ("La Subasta on the night").

## 2. Party day — setup, before guests arrive

- [ ] Cups on the mantle, sleeves in, powered. Walk the mantle: every screen shows its
      number. A screen showing `NO HORSE` hasn't been set: hold it → `HORSE` → its number
      → `SET`. A `NO LINK` badge in a corner means that cup can't hear the gateway (not up
      yet, or too far).
- [ ] Gateway plugged into DevPi.
- [ ] The iPad, held in landscape, in Safari (two tabs is the tested way): open `ADMIN`,
      then tap **Change the race state on the Control Center** under its race state:
      `DASH` opens in a second tab, and from then on that link brings it back (to get to
      `ADMIN`, tap its tab).
- [ ] Optional: on `DASH`, Share → **Add to Home Screen** (its icon says `DDM`). It opens
      the Control Center full screen, with no tabs: there ☰ → La Quiniela Admin and
      `ADMIN`'s link back each open the other page in place.
- [ ] Once the apps are up (section 3), watch the TV's slideshow for a lap: the countdown
      slide counts down to today's post under `KENTUCKY DERBY 2027`, and the roster slide
      lists the field by number, replacements under their own numbers, with the odds (a dim
      `—` where there are none: start the odds poller or type them, appendix). When the
      board is up its crawl carries the time, `POST IN 1:14` and the weather.

### 3. The apps are up

pi5 and the splash started with DevPi (section 0). To watch them, on DevPi:
```bash
systemctl status ddm-pi5 ddm-splash
journalctl -u ddm-pi5 -f
```
Both `active (running)`. pi5's log has `[LQ] bridge started on …` and then
`[LQ] gateway online`. A `gateway hello from …` line only appears if the gateway was just
powered; `answered the hello with …` follows it. The splash's log
(`journalctl -u ddm-splash -f`) has `pi5 link up`. Ctrl+C stops the reading, not the app.

Check: open `ADMIN` on the iPad. Race says `LINK OK` and `20 cups online`.
`NO LINK`, or fewer cups: stop here and fix that first (table at the end).

`DASH`'s top left has two pills, `LED wall` and `Tote board`: `online`, `unreachable since
hh:mm`, or `off` (no address in `pi5/.env`). The race never waits on either: a wall that
drops gets one red line on `DASH`, every button works as before, and the wall picks up the
current animation by itself when it answers again (one green line).

### 4. Clean slate

Cups must be **empty** for this.

- [ ] `ADMIN` → Race → Horses: every horse 1–20 `● online`, no `○ no cup`, no `⚠`.
      `○ no cup`: the cup for that post isn't set or isn't powered (walk the mantle).
      `● offline` or `⚠ 2 CUPS`: table at the end.
- [ ] `ADMIN` → Race → **Reset betting** → OK. The line under the button says
      `Reset. Pot $0 · 20 cups online`. If it names horses with tokens still in their
      cups, empty those cups and press it again. Nothing else changes: the cups keep their
      numbers (they're the cups' own), names and scratches (both kinds) stay.
      Reset betting works only before a race (0 PRE-RACE) or after it (6 AFTER PARTY). If the
      line says `Reset betting works only before the race (0) or after it (6). …`, a rehearsal
      left the race in 1–5: `DASH` → **STANDBY** (from AT THE POST on it asks first: **Reopen
      betting**), then Reset betting again.
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
`race state not set (…)` means pi5's La Quiniela side refused, and says why. The LED wall
is not in the line: its pill, top left, says whether it is answering; the race goes on either
way.

`ADMIN` → Race says `1 · BETTING OPEN` within five seconds.

While open:
- Tokens go **in** by tipping them onto the sleeve. Don't fling.
- A cup that gets bumped or lifted freezes its count (`HANDLED` on its diag) and
  re-reads itself when it's set down. Don't touch it; wait five seconds.
- Someone takes a token back out: the count drops and the pot drops. That's correct.
- Enter any also-eligible replacement before pressing **60 MIN**. After 60 MIN every scratch
  means: empty that cup and hand the tokens back to re-bet. If a replacement is known, enter
  it: it draws in with the empty cup (the row and the TV say `re-bet`). From **AT THE GATE** on
  there is no replacement (section 7).
- A same-day scratch (vet scratch, gate scratch): `ADMIN` → Scratches → In the field →
  that horse's row → tick *No replacement* → Scratch. The row leaves the board, the crawl
  says `RE-BET YOUR TOKENS`, its tokens leave the pot. Empty that cup and hand the tokens
  back to re-drop in other cups; one not re-bet is settled by the counted pot.
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

A late bettor after AT THE GATE, before the race: `DASH` → **FINAL CALL** asks `This
reopens betting. The figures at the post (pot $86) …`; **Reopen betting** clears the figures
and any hand count, take the bet, then **AT THE GATE** again: new figures, from the cups as
they are then (count the cash again if you had). Pressed by mistake: **Cancel**, nothing
changes. AT THE GATE pressed twice changes nothing either.

They're off: `DASH` → **THEY'RE OFF!** (then **CHAOS**, **FINISH** as the race runs: all
three are RUNNING).

(`ADMIN` → Race shows them under their own names: `2 · FINAL CALL`, `3 · AT THE POST`,
`4 · RUNNING`.)

A horse scratched at the post or after has no replacement: the field was set at the gate. `ADMIN` →
Scratches offers only *No replacement*, ticked (pi5 refuses a replacement, `409 The field is set at the
gate`); its cup's tokens are refunded in cash, not re-bet, the crawl says `TOKENS REFUNDED`, and
section 8 lists the refund. If the row still offers a number list, the race is not in AT THE POST,
RUNNING or WINNER: check the state on `ADMIN` → Race.

## 8. Results and the draw

In order: **FINISH** on `DASH` → **SET WINNERS** → **CONFIRM RESULTS** → refund any cup scratched
after the post → count the cash → **Counted pot** on `ADMIN` → read `ADMIN` → **RESULTS** →
draw → pay. The rule behind the RESULTS section is at the end of this section;
`DDM_La_Quiniela_Spec.md` has it with worked examples.

**SET WINNERS.** `DASH` → **SET WINNERS** → tap the horses that crossed **1st, 2nd and 3rd**, the
real finish (the pickers run in program-number order, a horse that drew in at the end, a scratched
horse gone with no blank; each says the number and the name, `19 · GOLDEN TEMPO`) →
**CONFIRM RESULTS**. That is WINNER: the LEDs light the three cups,
the three cups' screens say WIN, PLACE, SHOW, and the TV flips to the **results board**,
`OFFICIAL RESULTS`: one row per prize paid, the horse whose cup is drawn from, with its cloth and
name, the bets its cup held and its prize, a skipped finisher noted; the pot above. Opening the
pickers changes nothing; confirming does. (WINNER without results, from **HEARTBEAT**: the TV says
`OFFICIAL RESULTS COMING` over the frozen board until the results are confirmed.) When no cup in the
finish held bets, nobody is paid and the board says `NO LA QUINIELA PRIZES THIS RACE`, the reason in a
line, and `cup 19 scratched after the post: tokens refunded` for each refunded cup; `ADMIN` → RESULTS
says the same. pi5 refuses a
result before AT THE GATE (`results before the post …`): there are no figures at the post to pay
from yet.
Each tap fills one slot, the next empty one (WIN, then PLACE, then SHOW); to change a
pick, tap its slot on the right first, then the new horse (its × empties it). A horse
already in another slot is refused, never moved. A horse dimmed and tagged red `NO BETS`
had nothing in its cup at the post: La Quiniela skips it for the next finisher (La Subasta
pays its owner all the same), so tap it where it finished. Once the three slots cannot hold
the horses with bets La Quiniela needs, a `4TH` card appears under SHOW (a `5TH` if that one
had no bets either, and so on until three horses with bets are named, or every horse with
bets when fewer had any), and **CONFIRM RESULTS** waits until they are filled; no card while
the three slots are still empty. The cards on the right are the check: read them before you
press **CONFIRM RESULTS**. If a horse you picked had no bets, one grey note above the button
names it (`#15, #12: no bets at the post. La Quiniela skips them; La Subasta still pays their
owners.`).
**A dead heat.** Tap the first of the tied horses into its slot, then the small **DEAD HEAT**
link on the seam under its card: the two cards become one, headed `WIN · PLACE — DEAD HEAT`,
with the other horse beside the first (the one already there, or the next you tap). Three
deep: the link under that card again (the third horse sits under the two). A dead heat for 3rd
merges SHOW with a `4TH` card. **UNTIE** on the merged card undoes the whole dead heat; to
take one horse out, untie and tap its **×**; to change one, tap its cell, then the new horse.
The record carries the tie; the RESULTS section pools the prizes.
**Opening SET WINNERS again** while a result stands shows `CHECK THE RESULTS` with the saved
picks (the next finishers and a dead heat too), the three cups lit again: a correction is one
tap on a slot, then the horse, then **CONFIRM RESULTS**; **CANCEL** leaves the result and puts
the mantle back on it.
The result is one record for both games: La Quiniela's RESULTS section reads it, and so does
La Subasta's ledger (its section, below).
**While the results stand the field is frozen:** `ADMIN` refuses a scratch or an undo with
`The results are in, so the field is frozen: clear them in SET WINNERS first (RESET in the
dialog), then scratch, then enter them again.` To scratch after the results: **SET WINNERS** →
**RESET**, it asks `Clear the saved result?` → **Clear the result** (AFTER PARTY: the TV back to
the slideshow, the LEDs off) → the scratch on `ADMIN` → back in the dialog, which has
followed it (the horse is out of the grid, its slot empty, a line under the heading says so)
→ tap again → **CONFIRM RESULTS**. A confirm that races a scratch is refused by pi5 and the
grid refreshed: the scratched horse's card is empty, a line under the heading says so, and
**CONFIRM RESULTS** is gone until you pick it again.
**A confirm pi5 refuses with the picks as they are** (the race is not at the post yet) leaves
the dialog open: pi5's reason stands right over the button in red and the button turns into a
red-outlined **NOT SAVED** until you change a pick. Nothing was saved.
The line that follows names the race state the results set: `Results set: Win=19, Place=1,
Show=22 · WINNER`, green. The results are saved whether or not the LED wall answers (its
pill, top left, says; a wall that is back shows them by itself). A red `Results set: … ·
race state not set (La Quiniela: …)` means they are saved but La Quiniela did not go to
WINNER, so the TV and the cups don't show them yet: the text after `La Quiniela:` says why
(table at the end).

- [ ] **A cup scratched after the post?** Its tokens are refunded in cash at token value,
      before you count: `ADMIN` → RESULTS → Refunds lists them (`cup 7: 12 tokens → refund
      $12`). Hand the cash back; the horse cannot be in the finish (pi5 refuses it).
- [ ] **Count the cash**, any time after AT THE GATE (after the refunds) and always before you
      pay and before **RESET** (in AFTER PARTY the box is read-only). Open the cash box's BETS
      compartment, count the dollars. `ADMIN` → Race → Counted pot → type the number →
      **Save count**. The line under the buttons reads `Saved: counted $152 (the scales said
      $154, −$2). WIN $91 · PLACE $38 · SHOW $23.` From now on the pot and the three prizes,
      on `ADMIN` and on the TV (tagged `HAND COUNTED` beside the pot), are those: the same
      60 / 25 / 15, whole dollars. The bets per horse stay as the scales read them, and the
      scales' figure and the difference sit beside the box for information only: a few
      dollars either way is the scales, nothing to fix. Typed it wrong, or counted again
      after a late bet? Type it again and **Save count** (it overwrites), or **Clear** to go
      back to the scales' figures. No `HAND COUNTED` tag on the TV means no count is saved.
- [ ] **Read `ADMIN` → RESULTS** (under the Race section; it appears once the result is in,
      within 5 s). The line at the top says which pot it uses (`your hand count`; `the scales
      at the post`, less the refunds, until you enter one) and the three prizes. Then, for
      WIN, PLACE and SHOW, the horse that pays, its prize and the draw: `WIN · #19 · $92 ·
      draw one token from cup 19`. A finisher with no bets is shown skipped: `#7 no bets →
      #3`. A dead heat shows both horses, each one's share, and `draw one token from each
      cup`. With fewer than three horses with bets in the finish, the unpaid prizes are
      added to WIN and the section says so. Pay what it says.
- [ ] Each paying cup: shake it, pull **one** token. Read its number aloud. The guest with
      the other half takes that cup's prize (in a dead heat each tied cup is drawn once, for
      its own share).
- [ ] Pay in whole dollars, the amounts in the RESULTS section (the TV's results screen shows
      the same horses and prizes to the room). Emptying the cups doesn't change them: the
      figures are the ones at the post, and so is a TV reloaded, or pi5 restarted, in
      between; the count is kept with those figures.
- [ ] Undrawn tokens are just tokens. Nobody else wins anything.

**The rule** (OD3, OD5, Joey's examples of 2026-10-22; `DDM_La_Quiniela_Spec.md` has them):
60 % WIN, 25 % PLACE, 15 % SHOW of the pot, whole dollars, PLACE and SHOW rounded half up,
WIN takes the rest. The pot is the counted pot once entered, the scales' figure at the post
less any refunds until then. A finisher whose cup held no bets at the post is skipped: the
next finisher with bets takes that position's prize. Fewer than three horses with bets in the
finish: the unpaid prizes go to WIN. Dead heat: the tied horses share the prizes of every
position they jointly occupy, whole dollars, the odd dollar to the lower program number, one
token drawn from each cup; a tied horse with no bets drops out of the tie. A horse scratched
after the post cannot be in the finish and its cup's tokens are refunded at token value. The
computation is `payout_for` in `pi5/la_quiniela/betting.py`, served as the model's `payout`
(`GET /api/quiniela`): when the RESULTS section looks wrong, compare that against the record
(`pi5/data/results.json`) and `closing`.

## 9. Another race on the same night

- [ ] `ADMIN` → Race → **Reset betting** → OK (after **RESET**: AFTER PARTY). The cups keep
      their numbers; names and scratches stay; the figures at the post and the hand count
      go. The line says `Reset. Pot $0`; if it names horses with tokens still
      in their cups, empty those (each count drops to 0 by itself) and press it again.
- [ ] Section 5 (close time), then section 6 (`DASH` → **60 MIN**).
- [ ] A different field next race: `ADMIN` → Horse names → paste the new names → Save
      names, and undo / redo the scratches, before opening.

---

## La Subasta on the night

The auction runs from `/la-subasta/admin` on pi5 (`http://joeydevpi.local:5000/la-subasta/admin`,
a third tab on the iPad); the guests are on `/la-subasta` on their phones. Nothing about the
race is entered there: the horses, the scratches and the result are La Quiniela's and the
Control Center's.

**Before guests arrive**
- [ ] `EVENT_YEAR` in `pi5/la_subasta/config.py` is this year (2027 since 2026-10-20). It is a
      constant, bumped once a year by a commit before the party; a guest belongs to one year,
      so a wrong year sends every phone back to the join screen. Check it on DevPi:
      `grep EVENT_YEAR ~/DDM-Multimedia/pi5/la_subasta/config.py`.
- [ ] The wipe has run ("Before the party: start fresh"), so there are no guests, bids or owners
      from a rehearsal. `/la-subasta/admin` says `NOT_STARTED`, Bidders `0`, Pot `0 pesos`.
- [ ] The host PIN is set. `/la-subasta/admin` on the iPad asks for it, once: the iPad then
      remembers it for 30 days (a cookie, so a restart of pi5 does not ask again; clearing
      Safari's website data does, and so does changing the PIN). A wrong PIN is refused and
      nothing says how close it was; there is no lockout. A guest's phone has no way to open,
      lock or settle the auction without it. Forgot it: it is in `pi5/.env` on DevPi
      (`grep DDM_LS_ADMIN_PIN ~/DDM-Multimedia/pi5/.env`); to choose a new one, edit that line,
      `sudo systemctl restart ddm-pi5`, and enter the new one on the iPad. The page says `No
      host PIN is set` when the line is empty.
- [ ] `/la-subasta/admin` → **Open auction**. The pill says `OPEN`; a phone on `/la-subasta`
      joins (name and emoji) and sees the field with no bids.

**During**
- [ ] Guests join on their phones and bid; the page shows every bid as it lands. A horse nobody
      has bid on near the close: buy it yourself (mark yourself **No cap** on your row first;
      the three-horse cap stays for everyone else), at the minimum bid. There is no House.
- [ ] A guest whose phone lost its storage (a new phone, a cleared browser) is sent to the join
      screen and **cannot reclaim their bidder by retyping their name**: the name is taken, on
      purpose (M4). Find them in the admin list (Bidders): their horses and what they owe are
      there, and you settle with them face to face; they join again under a new name only if
      they still want to bid.
- [ ] Close to post time: **Final hour** (the phones go urgent), then **Lock auction**. The
      lock lists any horse nobody bid on and locks only on **Lock anyway**; buy them first if
      you can. Locked, every bidder's row says what they owe: **Mark paid** as they pay you.
- [ ] A guest can take a bid back (their 10 seconds) only while bidding is open. After the lock
      their phone is refused, and so is any other: the horse has an owner who owes the bid, and
      only the host's void (`/api/admin/void`, no button yet) can change that, moving the horse
      to the runner-up at their bid. A guest's phone proves who it is with a secret it was
      given when it joined (not their name, which every list shows): a phone that lost it, or
      from before the secret existed, lands on the join screen.
- [ ] A same-day scratch during the auction is entered on `ADMIN` (La Quiniela), never here:
      the bids on that horse are voided (after the lock, the owner's purchase too, and a paid
      owner's row says `refund owed`). Undo on `ADMIN` puts it all back.

**Results**
- [ ] The result is entered once, in `DASH` → **SET WINNERS** (section 8), the real 1st, 2nd
      and 3rd. La Subasta reads that record: the moment it is confirmed, `/la-subasta/admin` →
      Payouts shows the pot, the three amounts (whole pesos, WIN takes the rounding) and a row
      per slot: `WIN · 7 pesos`, `#19 GOLDEN TEMPO finished`, `pay Alice 🌮`. The owner of the
      horse that crossed the line is paid, whether or not anyone bet it in La Quiniela.
- [ ] A slot flagged `NO OWNER` (nobody bought that horse: the lock warned you): nobody is paid
      it until you name the horse that pays it instead, the next finisher, in the row's picker
      → **Set**. The pick stays with that finisher. Only a `NO OWNER` slot has a picker: a slot
      whose horse has an owner pays that owner, and the server refuses to point it elsewhere.
- [ ] Wrong pick? Correct it in **SET WINNERS** (section 8), never here: the payouts follow the
      new record at the next refresh (the page reads every 4 s). While the result stands the
      field is frozen (no scratch, no undo); the dialog's **RESET** clears the result first.
- [ ] **Paid on an earlier result**, in red on top of Payouts: a slot was marked paid out and
      the result then moved away from it (a correction, RESET, Reset betting, or the owner or
      amount changed under it). Each line says who was paid what for which horse and what the
      slot pays now (`Alice 🌮 was paid 7 pesos for WIN (#1); WIN now pays Carol 💃 7 pesos for
      #4`). Settle up with those two, then **Unmark** the line. It stays until you do, or
      until the result comes back to what they were paid on.

**Payouts**
- [ ] Read them under Payouts on `/la-subasta/admin` (`GET /la-subasta/api/admin/payouts` is
      the same ledger). Pay each owner the amount on the row (an unpaid owner: net what they
      owe you), then **Mark paid out** on the row; it then says `paid out to Alice 🌮, 7
      pesos`. **Unmark** takes it back.
- [ ] The ledger is derived, not stored: if pi5 restarts, it is the same. If the page and
      `DASH` disagree, `GET /api/results` on pi5 is the record both read.

**Start fresh**
- [ ] After the party, or before the next rehearsal: `pi5/tools/wipe.py` on DevPi, pi5 stopped
      ("Before the party: start fresh"). It clears the guests, bids, owners, payout slots (the
      paid-out marks included), the auction's state and the result. Phones from before join
      again by themselves.

## The TV boxes (ddm-tv, ddm-tv2)

The pages are named for what they show; the boxes are numbered, interchangeable screens, and which
page a box shows is one line in its config (`DDM_TV_URL`, set by `--server`). This table says which:

| Box | Page | Shows |
|---|---|---|
| `ddm-tv` (Pi 5), by the cups beside the race broadcast | `http://joeydevpi.local:5001/board` | the La Quiniela board, in every race state, nothing else |
| `ddm-tv2` (hardware not chosen yet) | `http://joeydevpi.local:5001/slides` | the slideshow (trivia, La Subasta, the rest), and the La Quiniela board from AT THE GATE until the results are in (AT THE POST, RUNNING, WINNER; `DDM_SLIDES_BOARD_STATES` in `splash_display/.env`, default 3,4,5); the slideshow again with the next state after the results (RESET or WELCOME) |

Each box does nothing but show its page full screen. DevPi runs the servers and the gateway; its own
kiosk stays off (the fallback, on `/board`). A box runs no DDM code and has no repo: DevPi sets it up
over SSH, and a pull on DevPi updates the pages as before (each TV reloads itself within a minute of
the restart).

- **Any screen, either port:** plug the TV into either micro-HDMI port (HDMI0, next to the USB-C
  power, wins if both have a screen). The box uses 1920x1080, the board's size, when the screen
  offers it, else the screen's own preferred mode, and the TV can be switched on before or after
  the box: within about 10 s of a screen appearing the board is on it.
- **Power-on order doesn't matter.** Until DevPi's board answers, the TV shows the box's own
  holding screen, `Waiting for DDM…`, and it switches to the board by itself, within about 30 s of
  DevPi answering. The same when `ddm-splash` restarts, DevPi is off for a while, the box's cable
  comes out, or the box loses power: no touch needed.
- **The holding screen stays up:** is DevPi on? Is `ddm-splash` running
  (`systemctl status ddm-splash` on DevPi)? Is the box's network cable in? The line under
  `Waiting for DDM…` says what it last saw.
- **Status, from DevPi:** `~/DDM-Multimedia/deploy/tv_box.sh status joey@ddm-tv.local`: the board's
  address, whether DevPi answers the box, the screen in use (`HDMI-A-2 1024x600@60.000`), Chromium
  running, what the TV shows (`DDM TV - board <revision>`, the same revision as DevPi's within a
  minute of a pull and restart, or `DDM TV - waiting`).
- **The box's log:** `ssh joey@ddm-tv.local journalctl -t ddm-tv -b`.
- **Setting it up or updating it,** from DevPi, after a pull:
  `~/DDM-Multimedia/deploy/tv_box.sh install joey@ddm-tv.local` (sudo asks for the box's password;
  the first time add `--reboot`). By default the box shows the La Quiniela page on DevPi's name,
  `http://joeydevpi.local:5001/board`. **To point a box at another page:** install again with
  `--server`, e.g. `ddm-tv2`, once it exists:
  `~/DDM-Multimedia/deploy/tv_box.sh install joey@ddm-tv2.local --server http://joeydevpi.local:5001/slides --reboot`
  (`?look=impact` on either page for the old look), and change the table above.
- **Read-only SD card (optional):** `--overlay on` makes the box's root read-only, so a yanked power
  cord can't corrupt the card. Turn it off before installing again (`--overlay off`, reboot, install).
- **Fallback, if the box dies on the day:** plug the TV into DevPi (HDMI0) and
  `~/DDM-Multimedia/deploy/install_services.sh --kiosk on`, then log out and in (or reboot DevPi).
  `--kiosk off` when the box is back.

## If something goes wrong

| You see | It means | Do |
|---|---|---|
| The box's desktop on the TV, but no board | Chromium isn't on that screen, or isn't running (before 2026-10-19 the box forced HDMI0 on, so a screen on HDMI1 got the desktop) | Wait 10 s: the box moves the board to the screen that is there. Still not: `deploy/tv_box.sh status joey@ddm-tv.local` on DevPi (the screen line, Chromium running?), `ssh joey@ddm-tv.local journalctl -t ddm-tv -b`, then reboot the box. After a box was installed before 2026-10-19, install again with `--reboot` once (it takes out the forced HDMI0). |
| `Waiting for DDM…` on the TV | the TV box can't reach DevPi's board | Is DevPi on, `ddm-splash` running, the box's cable in? It switches back by itself. `deploy/tv_box.sh status joey@ddm-tv.local` on DevPi ("The TV boxes"). |
| `NO LINK` in the board's corner on the TV | splash can't reach pi5, or pi5 can't hear the gateway | `ADMIN` → Race. `NO LINK` there too: the gateway (cable, `DDM_LQ_SERIAL_PORT`). `LINK OK` there: the splash can't reach pi5 (`systemctl status ddm-splash` running? on DevPi it looks for pi5 on the same box; a `DDM_PI5_URL` in `splash_display/.env` overrides that); `sudo systemctl restart ddm-splash`. |
| Gateway `hello` lines forever, never answered | pi5 isn't reading that port (wrong `DDM_LQ_SERIAL_PORT`, or pi5 is down); pi5 answers every hello with its state by itself | Check `DDM_LQ_SERIAL_PORT` in `pi5/.env` and that pi5's log says `bridge started` (`journalctl -u ddm-pi5 -n 50`). |
| `ddm-pi5 is running under systemd. Stop it first: …` when you type `python main.py` (or `ddm-splash …` for `python3 server.py`) | the service is running; a second copy would fight it for the gateway and the port | Do what it says: `sudo systemctl stop ddm-pi5`, work by hand, then `sudo systemctl start ddm-pi5` (end of this runbook). |
| `Address already in use`, or `systemctl status ddm-pi5` says `activating (auto-restart)` over and over | a copy started by hand before the service holds port 5000 (or 5001 for the splash) | Ctrl+C that copy in its terminal; the service starts by itself within 3 s. |
| `ddm-pi5` restarts in a loop at boot (`Address already in use` in `journalctl -u ddm-pi5`), with nothing started by hand | something else starts pi5 at boot too (on 2026-10-11 an old `ddm.service`) and holds port 5000 and the gateway | Who holds the port: `sudo ss -ltnp 'sport = :5000'`, then `systemctl status <its pid>` names the unit. Retire it: `~/DDM-Multimedia/deploy/install_services.sh --retire-old` (it finds every other starter, prints each step; `--dry-run` first to see them). |
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
| The crawl has no weather | `pi5/.env` has no `DDM_WEATHER_API_KEY` (pi5's log says `no weather key` at start), or no internet yet | Nothing needed: the item is left out. To have it: the key in `pi5/.env`, `sudo systemctl restart ddm-pi5`; it shows within `WEATHER_CACHE_MINUTES` of pi5's start. |
| The `LED wall` pill on `DASH` says `off` | `pi5/.env` has no `DDM_ESP32_IP` (pi5's log says `no LED controller set` at start) | The race goes on. The LEDs: the controller's address in the device panel on `DASH` (tap the pills; it is saved to `pi5/.env` and tried at once), or in `pi5/.env` and `sudo systemctl restart ddm-pi5`. |
| Board shows old pot/bets, or last race's results, on startup | pi5 persisted last session (it keeps the results, the figures at the post and the hand count until **Reset betting**) | Empty the cups, then `ADMIN` → Race → **Reset betting**. `DASH` drops the old results tote by itself within 5 s. If it refuses (next row), `DASH` → **STANDBY** first. |
| **Reset betting** answers `Reset betting works only before the race (0) or after it (6). Change the race state on the Control Center.` | the race is in 1–5; Reset betting would change the race state behind `DASH`'s back (the mantle would stay on the old animation) | After the race: `DASH` → **RESET** (AFTER PARTY), then Reset betting. Before it (a rehearsal left the state there): `DASH` → **STANDBY** (from AT THE POST on it asks first), then Reset betting. |
| `DASH` asks `Reopen betting?` | you tapped WELCOME, STANDBY, 60 MIN, 30 MIN or FINAL CALL at AT THE POST, RUNNING, WINNER or AFTER PARTY: it would clear the figures at the post and the hand count it names | **Cancel**: nothing is sent. Only for a late bettor before the race: **Reopen betting**, take the bet, **AT THE GATE** again (new figures), count the cash again. |
| Every `DASH` race button greyed out, each saying `switch to MANUAL` | the AUTO / MANUAL switch is on AUTO (the LED demo) | Switch it to MANUAL. Race night is MANUAL. |
| Pot isn't $0 after **Reset betting** | tokens were still in the cups; the line under the button names them | Empty those cups (each count drops to 0 by itself) and press it again. |
| No Counted pot box on `ADMIN`, or Save answers `The count can only be entered after betting closes …` | the box shows from AT THE POST to WINNER; before that betting is still open (nothing to count against yet), after it (AFTER PARTY) a saved count is read-only | Close betting first (section 7: `DASH` → **AT THE GATE**); the box appears within five seconds. |
| A count typed wrong, or the cash changed after a late bet | the count is whatever was saved last | `ADMIN` → Race → Counted pot → type it again → **Save count** (it overwrites), or **Clear** to go back to the scales' figures. |
| The count is wrong or missing and **RESET** was already pressed | AFTER PARTY makes the box read-only | `DASH` → **HEARTBEAT** (WINNER again; the TV says `OFFICIAL RESULTS COMING`): the box is back on `ADMIN`, whose figures carry the prizes. Enter it, pay from those, then `DASH` → **RESET** again. |
| The scales and the count differ by a few dollars | the scales are estimates ("totals based on cheap Chinese electronics") | Nothing: pay from the count. The difference is shown for information, nothing blocks. |
| `Saved: … NOT stored: pi5 could not write it…` in red under Save count | pi5 holds the count but its database refused the write | Pay from it. If pi5 restarts before the draw is paid, enter the count again. |
| Board on the TV, wrong state | state didn't take, or somebody pressed another button | `ADMIN` → Race: its state line is the state pi5 holds. Press the right button on `DASH` and read what it answers. |
| TV stays on `OFFICIAL RESULTS COMING` | it is WINNER and pi5 has no results: they were never confirmed, or **CONFIRM RESULTS** answered `Error: results not saved …` (pi5 could not write the file; the LED controller has nothing to do with it) | `DASH` → **SET WINNERS** → confirm again. Meanwhile the prizes are in the board's header (WIN / PLACE / SHOW under the pot, the figures at the post); draw and pay from those. |
| `ADMIN` has no RESULTS section | no result is in, or the race never reached AT THE GATE (no figures at the post to pay from) | `DASH` → **AT THE GATE** (then the race as it ran), **SET WINNERS** → **CONFIRM RESULTS**; the section appears within 5 s. |
| **CONFIRM RESULTS** turns red and says **NOT SAVED** under `results before the post: …` | the race is in PRE-RACE, BETTING OPEN or FINAL CALL: no figures at the post to pay from | `DASH` → **AT THE GATE** (then THEY'RE OFF, FINISH as the race ran), then **SET WINNERS** again. |
| The `LED wall` pill on `DASH` says `unreachable since hh:mm` (one red line said so when it dropped) | the LED controller stopped answering: power, Wi-Fi, or it rebooted | Nothing for the race: every button works as before, and the results are saved whatever the wall does. When it answers again the pill goes green, one green line says so, and the wall picks up the current animation by itself. If it stays red: power and Wi-Fi at the mantle; its address in the device panel (tap the pills). |
| `Results set: … · race state not set (La Quiniela: …)` in red on `DASH` | the results are saved, but La Quiniela refused WINNER, so the TV and the cups don't show them; the text says why (`bridge not initialised`: pi5's La Quiniela side is not up) | Fix what it names (`journalctl -u ddm-pi5 -n 50`), then `DASH` → **SET WINNERS** → the same three → **CONFIRM RESULTS** again. |
| pi5 restarted, or the TV reloaded, during the draw | nothing is lost: pi5 keeps the results and the figures at the post | Nothing. The TV finds pi5 again by itself and shows the results board as it was. |
| Results board shows fewer bets or smaller prizes than at the close | betting was reopened before the draw was paid: a `DASH` WELCOME / STANDBY / 60 MIN / 30 MIN / FINAL CALL at AT THE POST or later, confirmed with **Reopen betting** (the question named the figures and the hand count it would clear); pi5 cleared them, and the next close took the cups as they were then | Pay from the backup photo (section 7), if there is one; otherwise count what those cups held and count the cash again (the hand count went with the figures). TEST never does this: it is LEDs only. |
| A button's line is red, or `ADMIN`'s state says `cannot reach pi5` | pi5 refused, or can't be reached; the text is the reason | `cannot reach pi5`: the iPad's Wi-Fi, or pi5 is down (appendix). Anything else names the fix. |
| Wrong number on a cup after a scratch | old firmware (pre-`45510f8`, protocol v2) | Flash the gateway and the cup. |
| A cup died | it's gone for good | Spare cup: power it, hold the screen, `HORSE` → the dead cup's number → `SET`, set it down in that post; empty the dead cup's tokens into it. The page shows that horse `● online` again; the dead cup just stops being heard. |
| `DASH` or `ADMIN` doesn't load | pi5 down, or the iPad isn't on the house Wi-Fi | On DevPi: `curl -s localhost:5000/api/quiniela` answers? No: `sudo systemctl restart ddm-pi5`, and `journalctl -u ddm-pi5 -n 50` says why it was down. Yes: the iPad. Meanwhile, the appendix (the race state: "If `DASH` is down"). |
| A reopening button on `DASH` answers `race state not set (This reopens betting: …)` in red, or `ADMIN` → Undo answers `Cup 9 holds 4 tokens bet on #22. …` | pi5 refused: the page did not ask first (it was out of date), or the replacement's cup holds bets that would move with it. Nothing changed | Reload `DASH` and tap again: it asks. For Undo: empty the cup (hand the tokens back), then Undo. |
| `/la-subasta/admin` shows a PIN form, or says `No host PIN is set` | the iPad has no cookie for the PIN (first visit, website data cleared, the PIN changed in `pi5/.env`, 30 days), or `DDM_LS_ADMIN_PIN` is empty (pi5's log says `no host PIN` at start; the installer WARNs) | Enter the PIN. Forgot it, or empty: set `DDM_LS_ADMIN_PIN` in `pi5/.env`, `sudo systemctl restart ddm-pi5`, reload the page. |
| `ADMIN` → Scratch or Undo answers `The results are in, so the field is frozen: …` | the results stand, and the field can't move under the payouts derived from them (La Subasta's) | Section 8: `DASH` → **SET WINNERS** → **RESET** → **Clear the result**, then the scratch, then the results again. |
| **CONFIRM RESULTS** leaves the dialog open: the scratched horse's card is empty with a line under the heading, or the button is red and says **NOT SAVED** under `#7 (WIN) had no bets at the post: … enter the next finisher too (4th)` | pi5 refused the record: a horse scratched since the dialog opened (the grid has just refreshed), or a `4TH` finisher is needed | Tap again what the dialog asks for and confirm. The results are never guessed: a refusal saves nothing. |
| No **DEAD HEAT** link between two cards, or the picks column runs past the bottom of the iPad | the link shows only under a filled card, and only where the dead heat would be two or three horses (a three-way one is full); the column is built by `renderResultCards()` in `ddm_control.js` into `#results-cards`, the grey note and the red **NOT SAVED** reason are `#results-confirm-notes` and `#results-confirm-refusal` over the button | Fill the card above the link first. Past the bottom: the iPad at 1180 x 820 landscape shows all of it; Safari's bars cost about 80 px, and the dialog then scrolls. |

## The rules, for anyone who asks

- $1 a token. Drop it in the cup of the horse you like. Keep the other half.
- After the race, one token is drawn from the WIN cup, one from PLACE, one from SHOW.
  The drawn token's owner takes that prize.
- Prizes are 60 / 25 / 15 percent of the pot, whole dollars, WIN takes the rounding.
- Fewer tokens in a cup, better shot at the draw.
- Scratched before Friday: the alternate takes over, with its own number.
  Scratched on Saturday: your tokens come back to you; re-bet them on another horse (or, once
  betting has closed, your money comes back).
- Totals based on cheap Chinese electronics. Final results hand counted.

---

## Appendix — if the page is down

The same actions as curls, typed on DevPi.

### If `DASH` is down

A `DASH` button does two things, and so do these. The race state, by the button's mode
(`WELCOME`, `BETTING_60`, `BETTING_30`, `FINAL_CALL`, `AT_THE_GATE`, `GATES_BURST` for
THEY'RE OFF!, `CHAOS`, `FINISH`, `HEARTBEAT_COOLDOWN`), on the route the button itself
uses: the cups and the TV follow, `ADMIN` shows it, and it does nothing to the LEDs:
```bash
curl -s -X POST localhost:5000/api/quiniela/mode -H 'Content-Type: application/json' -d '{"mode":"AT_THE_GATE"}'
```
The curl does not ask first, and it cannot reopen betting by accident: from AT THE POST to
AFTER PARTY, `WELCOME`, `STANDBY`, `BETTING_60`, `BETTING_30` and `FINAL_CALL` answer 409 and
say what they would clear, unless the body also carries `"reopen":true` (add it only to mean it:
the figures at the post and the hand count go at once). (`TEST` is not a mode: the LED test
sets no race state.)
The LEDs, the button's other half (the same name; skip it if the LED controller is the
trouble):
```bash
curl -s -X POST localhost:5000/api/animation/AT_THE_GATE
```
**RESET** (AFTER PARTY: clears the results, LEDs off, the TV back to the slideshow):
```bash
curl -s -X POST localhost:5000/api/results/clear
```
**SET WINNERS** is the results curl below. `{"cmd":"state 3"}` on `/api/quiniela/cmd` sets a
state by its number alone (the simulator and the tests use it); prefer the mode.

The state now, and the `DASH` button that set it (`mode` is null when a `state N` curl or
**Reset betting** did):
```bash
curl -s localhost:5000/api/quiniela/mode
```

### The other actions

The results, what `DASH` → SET WINNERS → CONFIRM RESULTS does (horse numbers, all three, three different horses in the field, or a 400 that says which and nothing saved; it sets WINNER, and the TV flips to the results board; the reply's `leds` is the LED wall's health as last known (`online`, `unreachable`, `unknown`, `off`), the results are saved either way, and `race_error` says why when La Quiniela did not go to WINNER):
```bash
curl -s -X POST localhost:5000/api/results -H 'Content-Type: application/json' -d '{"win":19,"place":1,"show":22}'
```

Reset betting (cups, horses, names and scratches are kept; the reply names any cups with tokens still in them; only in PRE-RACE or AFTER PARTY, a 409 that says so in 1–5):
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

The track's odds for the roster slide (optional; La Quiniela pays no odds). Fetched every 5 minutes while this runs (needs `DDM_ANTHROPIC_API_KEY` in `pi5/.env` and internet), or typed from the program:
```bash
curl -s -X POST localhost:5000/api/quiniela/odds/start
curl -s -X PUT localhost:5000/api/quiniela/odds -H 'Content-Type: application/json' -d '{"odds":{"1":"5-2","2":"8-1","21":"30-1"}}'
```

---

## Updating DevPi

Nothing tracked holds DevPi's own settings (they are in `pi5/.env`), so a plain pull:
```bash
cd ~/DDM-Multimedia && git pull
sudo systemctl restart ddm-pi5 ddm-splash
```
A pull that changed `deploy/`: run `~/DDM-Multimedia/deploy/install_services.sh` again
(it restarts both). After the restart the pages come up fresh by themselves: `ADMIN` and the
Control Center reload within a few seconds when a pull changed them (not while you're typing
or a dialog is open), the TV within a minute, between slides, and never into a splash that
isn't answering. Pages opened before 2026-10-18 can't: close both iPad apps and open them again,
once. A pull that changed `deploy/tv_box/`: run `~/DDM-Multimedia/deploy/tv_box.sh install
joey@ddm-tv.local` too.
`git status` should be clean; `pi5/.env` doesn't show (it is git-ignored).

A pull that changed `pi5/la_quiniela/` (the bridge, the board) or the cup simulator: run its
tests too, when DevPi is idle (not during the party):
```bash
cd ~/DDM-Multimedia/pi5 && python3 -m la_quiniela.test_simulator
```
It puts the real bridge and a betting board against the simulator over a pty, in one process,
with a temporary database, no gateway port and no port 5000, so **`ddm-pi5` keeps running**:
there is nothing to stop first. About four minutes; every line starts `[OK]` and the last
says `0 failed`. A line starting `[XX]` is the one that broke.

**Once, on a box that still has a local edit of `pi5/config.py`** (`git status` lists it;
DevPi did, with the gateway's port and the LED controller's address in it): stash the edit,
pull, move its values from the stash into `pi5/.env`, and the tree is clean for good. No
`git stash pop`: the pull rewrote the very lines the edit changed, so a pop would conflict.
```bash
cd ~/DDM-Multimedia
git stash
git pull
python3 pi5/tools/env_from_config.py --stash
python3 pi5/tools/env_from_config.py --stash --write
nano pi5/.env
git status
git stash drop
sudo systemctl restart ddm-pi5 ddm-splash
```
The first `env_from_config.py` changes nothing: read what it would write (a secret shows
only its length). `--write` merges it into `pi5/.env`, never changing a value already there
(and `splash_display/.env`, if the splash's config had a local setting too). The old weather
key is not copied (it was committed to the public repo, so it was rotated): put the new one
in `pi5/.env` by hand (`DDM_WEATHER_API_KEY=`). `git status`: nothing to commit. Then drop
the stash, which held only the old edit. (`--from FILE` reads a copy of the old file kept
aside instead; a `pi5/config.py` left with conflict markers by a `git stash pop` is read on
the stash's side, then `git checkout -- pi5/config.py` and `git stash drop`.)

## Working on it by hand

To run pi5 in a terminal (to watch its console, or with the cup simulator), stop its
service first and start it again after:
```bash
sudo systemctl stop ddm-pi5
cd ~/DDM-Multimedia/pi5 && python main.py
```
Ctrl+C when done, then:
```bash
sudo systemctl start ddm-pi5
```
The splash the same way: `sudo systemctl stop ddm-splash`,
`cd ~/DDM-Multimedia/splash_display && python3 server.py`, `sudo systemctl start ddm-splash`.
Forget the stop and the copy you start refuses with `ddm-pi5 is running under systemd. Stop it
first: …`. Forget the start and it stays stopped until DevPi next boots.
DevPi's own kiosk stays off: the TV box shows the board. `--kiosk on` is the fallback ("The TV boxes").
The logs leave out a line per request and per answer to the dashboard's 5 s LED `STATUS` poll;
`DDM_ACCESS_LOG=1 python main.py` prints them all.

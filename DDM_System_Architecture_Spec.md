# DDM System Architecture Spec

**Version:** 1.0
**Date:** 2026-09-29
**Target:** DDM 2027
**Repo:** `github.com/JoeyEinTX/DDM-Multimedia`
**Matches:** `main` at `6fc2be9` (2026-09-27), plus the decisions of 2026-09-29

This spec says how the pieces of the DDM system fit together and what the rules are. It
replaces v0.5 in the repo and the v0.9 draft, which described protocol v1 (cup IDs,
rosters, Race Setup) and no longer match the code.

Markers:

- **[DECIDED]** settled by Joey. Code that disagrees is a bug.
- **[BUILT]** in the code on `main`, not separately decided.
- **[TO BUILD]** decided, not built yet. Listed in section 15.
- **[PROPOSED]** a suggestion, not confirmed.

### Where the details live

This spec is the map. The contract documents below own the details, and **they win on any
detail**. If one disagrees with a rule marked [DECIDED] here, that is a bug to fix.

| Document | Owns |
|---|---|
| `firmware/quiniela/README.md` | Cup and gateway firmware, ESP-NOW protocol v2, the serial line protocol v2, scale, touch menu |
| `pi5/LQ_BRIDGE.md` | The DevPi bridge, the betting board model, every `/api/quiniela` and `/api/lq` route, the admin page, tables |
| `pi5/LQ_SIMULATOR.md` | The cup simulator |
| `RACE_NIGHT.md` | The race-night runbook |
| `splash_display/README.md` | The TV slideshow and the LQ board |
| `DDM_La_Subasta_Spec.md` | La Subasta |

`DDM_La_Quiniela_Spec.md` (v2.3, May 2026) is **superseded**. It describes one controller
reading all 20 scales and driving LED matrices, which is not how the system was built.

---

## 1. Principles

1. **pi5 on DevPi is the race brain.** It holds the race state, race info, horse names,
   scratches, results, the figures at the post and the counted pot.
2. **Every other device is a sensor or a renderer, with one exception: each cup owns its
   horse number.** [DECIDED, protocol v2] The number is set on the cup, saved in the cup,
   and sent in every packet. A cup also keeps its own scale settings (calibration,
   orientation, brightness, and once built, its empty reading and last count). Nothing else
   stores anything pi5 cannot rebuild.
3. **One race state, one names list.** Everything that shows a horse reads La Quiniela's
   store: the TV, the dashboard, the roster slide, and La Subasta (section 8, [BUILT]).
4. **Everything survives a reboot.** Any single device, pi5 included, can restart mid-party
   and the system recovers on its own.
5. **Loose coupling.** A missing subsystem (no LED controller, no HUB75, no Derby Dash, no
   internet) breaks nothing else.
6. **The dashboard runs the show; the admin page holds the data.** [DECIDED 2026-09-29]
   See section 10.

---

## 2. System map

```
  DDM Control Center        LQ admin page          Guest phones
  (touchscreen, "DASH")     (Joey's phone)         (La Subasta)
            \                     |                     /
             └──── pi5 on DevPi, port 5000: Flask + SQLite ────┐
                race state · names · scratches · results ·     │
                figures at the post · counted pot              │
       USB serial │          TCP :5005 │        HTTP + event stream
            Gateway ESP32      LED controller ESP32       splash display, port 5001
       ESP-NOW ch 6 │          (the mantle's cup LEDs)    slideshow + LQ board → TV
         up to 24 cups
                                          JoeyAI box (optional source of odds and names)
```

| Device | Role | Link to pi5 | Stores |
|---|---|---|---|
| pi5 (Flask app on DevPi, port 5000) | Race brain; serves the dashboard, the admin page, La Subasta | n/a | Everything (SQLite, one results file) |
| Gateway (ESP32 WROOM-32) | Radio-to-USB bridge | USB serial, JSON lines | The last state line, RAM only |
| Betting cups (CYD + HX711), 20 plus spares | Weigh tokens, show their horse number | ESP-NOW via the gateway | Horse number and scale settings in NVS |
| LED controller (ESP32, `10.0.0.44:5005`) | Mantle LED animations | TCP | Nothing |
| Splash display (Flask app, port 5001) | TV slideshow and the LQ board | HTTP and the `/api/quiniela/stream` event stream from pi5 | Nothing about the race |
| TV kiosk | Chromium on the splash's `/display` | HTTP | Nothing |
| DDM Control Center | The touchscreen dashboard at `/` | Same server | Nothing |
| LQ admin page | `/quiniela/admin`, on a phone | Same server | Nothing |
| Guest phones | La Subasta at `/la-subasta` | Same server | Nothing |
| Tote board (Pi 4B + HUB75), optional | Would show the LQ board | HTTP or the event stream | Nothing |
| Derby Dash | Arcade game | Loosely coupled, section 9 | Its own scores |
| JoeyAI (GEEKOM A8 MAX, `10.0.0.54`), optional | Odds and names source | HTTP, called by pi5 | Nothing DDM-related |

The drink jug LED ring was dropped from DDM 2027.

---

## 3. Race state

Seven states, from `DdmRaceState` in `firmware/quiniela/ddm_common.h`:

`0 PRE_RACE → 1 BETTING_OPEN → 2 FINAL_CALL → 3 AT_THE_POST → 4 RUNNING → 5 WINNER → 6 AFTER_PARTY`

The Python `Phase` enum is pinned to the header by `test_smoke`, so the two cannot drift.
[BUILT]

### The dashboard is the source [BUILT]

Each of the dashboard's mode buttons starts its LED animation and also sets the race state
(`MODE_STATES` in `pi5/la_quiniela/betting.py`, the only copy of this table):

| Dashboard button | Race state |
|---|---|
| WELCOME, TEST, STANDBY | 0 PRE_RACE |
| 60 MIN, 30 MIN | 1 BETTING_OPEN |
| FINAL CALL | 2 FINAL_CALL |
| AT THE GATE | 3 AT_THE_POST |
| THEY'RE OFF!, CHAOS, FINISH | 4 RUNNING |
| SET WINNERS (once confirmed), HEARTBEAT | 5 WINNER |
| RESET | 6 AFTER_PARTY |

- **One path.** Every change goes through `BettingBoard.set_race_state()`. It is persisted,
  and one full state line goes to the gateway carrying the state together with the
  scratches, renumber pairs and results, so a cup never shows WINNER before it knows who won.
- **The LEDs are the button's own request**, not fanned out from the race state. The race
  state never waits on the LEDs, and the results never wait on the LEDs.
- **Admin page state buttons: removed.** [DECIDED 2026-09-29, TO BUILD] They changed the
  state without the LEDs, so the cups and TV could disagree with the mantle. The admin page
  keeps showing the current state, read-only. [PROPOSED] The `state N` command on
  `POST /api/quiniela/cmd` stays as the runbook appendix's emergency curl. [PROPOSED]
- Consequence: AFTER_PARTY is reached by the dashboard's RESET (which also clears the results
  and turns the LEDs off), or by the curl.
- The mock racing service's AUTO mode (`/api/racing/*`) is not part of any of this.

---

## 4. Data

All tables live in pi5's one SQLite file, `pi5/data/la_subasta.db`, shared with La Subasta.
`pi5/LQ_BRIDGE.md` has the full shapes. [BUILT]

| Where | Holds |
|---|---|
| `lq_cups` | Every cup ever heard, by MAC: the horse it last claimed, last seen, signal, last count |
| `telemetry` | Append-only cup readings by MAC and horse, raw weight beside the count; written on change and on a heartbeat, not every packet |
| `events` | Audit log: cups online/offline, a cup changing horse, gateway hello/reboot/errors, state changes |
| `lq_link_state` | The state line pi5 holds (`state_rev`, phase, scratched, renumber pairs, results); how a gateway hello is answered after a restart |
| `lq_horses` | Names for horses 1–24 (1–20 the field, 21–24 the also-eligibles) |
| `lq_scratches` | One row per scratch: the horse that left, and the horse standing in (or none) |
| `lq_board` | Names revision, the betting close time |
| `lq_closing` | The figures at the post (section 6) |
| `lq_race` | Race name, year, post time |
| `pi5/data/results.json` | WIN, PLACE, SHOW horse numbers. The single store: the dashboard writes it, La Quiniela reads it |
| In memory only | Track odds, weather, the dashboard mode that set the state |
| On each cup (NVS) | Horse number, counts per token, orientation, brightness; the saved empty reading and last count once built (section 5) |

- **No per-token or per-guest data** is stored anywhere. Token numbers exist only on the
  plastic.
- The counted pot (section 6) is stored beside the figures at the post and cleared with
  them. [BUILT]

---

## 5. La Quiniela data path

### Identity: a cup is its MAC, a horse is the number the cup reports [DECIDED, BUILT]

- The number is set on the cup: touch menu → `HORSE` → `SET`, or `n7` on its serial port.
  It is saved in NVS, survives power and reflashing, and is sent in every packet.
- The picker is locked in states 1–4 and free in 0, 5 and 6.
- pi5 learns which horses have cups by listening. There are **no cup IDs, slots or rosters
  anywhere**, and nothing on pi5 ever addresses a cup.
- Everything sent down (scratches, renumbers, results) is keyed by horse number, and each
  cup applies what concerns its own number.
- **Two cups claiming one horse is a conflict**, shown on the admin page as `⚠ 2 CUPS`, not
  resolved automatically.
- **Spare cup:** set it to the dead cup's number, put it in that post, move the tokens.

### Up: cup → TV

1. Each cup sends one telemetry packet every 2 s over ESP-NOW (MAC, horse, count, raw
   weight, signal).
2. The gateway writes one `telem` JSON line per packet, and a `status` line every 5 s
   carrying its whole cup table.
3. The bridge thread in pi5 updates `lq_cups`, `telemetry` and `events`.
4. The betting board builds one model (pot, prizes, bets per horse, names, scratches,
   results, figures at the post, race info, weather) and serves it at `GET /api/quiniela`
   and as an event stream at `/api/quiniela/stream`.
5. The splash display reads that model and shows it on the TV. The admin page polls it
   every 5 s.

The bridge also emits SocketIO events (`lq_update`, `lq_link`, `lq_snapshot`) to the room
`lq`, but **nothing consumes them today**. See open question 6.

### Down: pi5 → cups

1. pi5 holds one state: a revision number, the race state, scratched horses, renumber
   pairs, and results.
2. Any change sends one full-snapshot `state` line (never a delta).
3. The gateway broadcasts it to every cup every 500 ms.
4. A gateway `hello` is answered with the current line. A `status` showing a different
   revision gets a re-send.

### The serial line protocol

Line protocol v2 with ESP-NOW protocol v2, specified in `firmware/quiniela/README.md`,
which wins on any detail. In short:

| Direction | Line | Purpose |
|---|---|---|
| Up | `telem` | One per cup packet: `mac`, `horse`, `raw`, `count`, `seq`, `drop`, `rssi`, `up` |
| Up | `hello` | Gateway booted; repeats every 2 s until a state line is applied |
| Up | `status` | Every 5 s and after each applied line: state revision, gateway uptime, the cup table. The only acknowledgement |
| Up | `err` | A rejected downlink line |
| Up | `state` | A gateway report, only when asked by hand (`json`); never parsed by pi5 |
| Down | `state` | `rev`, `st`, `scr` (scratched, no replacement), `renum` (up to 4 pairs), `res` (WIN, PLACE, SHOW) |
| Down | `debug` | Turns the gateway's human-readable output on or off |

There is no roster line. A v1 `roster` line is ignored.

### Link rules, proven on the bench [BUILT]

- **The gateway is addressed by `/dev/serial/by-id/...`** in `DDM_LQ_SERIAL_PORT`, never
  `/dev/ttyUSB0`. With two CH340 boards on DevPi, use `/dev/serial/by-path/...`.
- **Opening the port must not reboot the gateway.** The default `LQ_SERIAL_LINES=leave`
  does not touch DTR or RTS. The bench test of 2026-09-19 showed that holding them low
  *causes* the reset on the CP2102 gateway. No capacitor is needed.
- **Silent boot.** [DECIDED] A gateway broadcasts nothing until pi5 sends a state line, and
  never falls into demo mode on its own. Demo mode is the hand-typed `demo` command or a
  bench build with `DDM_AUTO_DEMO 1`, never on the party gateway.
- **Watchdog.** A port that is open but silent for 20 s is closed and reopened.
- **A gateway restart is not packet loss.** Cups count only forward gaps in the broadcast
  sequence.
- **Batch `ddm_common.h` changes.** Any struct change means reflashing every device.

### Cup rules

- **The settled weight is the source of truth** for the count, and the cup's count is
  what pi5 uses. Raw weight is logged beside it for recounting. [BUILT]
- **Per-cup calibration** (`CAL 10` in the touch menu) with the sleeve installed. [BUILT]
- **Handling rule:** a cup that is bumped, lifted or dumped stops counting until it is
  flat again, then re-reads itself from the weight with no phantom bets. [BUILT]
- **Brownout: a cup comes back with its count.** [DECIDED 2026-09-29, BUILT]
  - Before v0.7 a cup re-measured "empty" 30 s after every power-up. A cup that rebooted
    with tokens in it decided the pile was empty, read zero, and the pot dropped.
  - Fix (cup sketch v0.7, `ec0e063`): the cup saves its empty reading in NVS. At startup, after the warm-up, it re-zeroes
    only if it reads empty. A cup with a pile keeps its saved empty reading, so the weight
    gives back the right count.
  - The cup also saves its last count and reports it during the 30 s warm-up, so the board
    does not dip to zero and back.
  - Manual tare (3 s BOOT hold or the touch menu `TARE`) is unchanged, and saves the new
    empty reading.
  - No protocol change; a cup reflash only.
  - Acceptance test, passed on the bench 2026-10-01 after a `c30` calibration with the
    sleeve on and a tare: 30 tokens in, power off 15 s, power on. The cup reported 30
    through the warm-up and the boot read `net=+180126 (29.97 tok) → keep zero, count 30`;
    the board never dipped. Empty cup, power off 15 s, power on: `net=-5 (-0.00 tok) →
    empty, re-zero (drift -5 counts)`. At ≈6004 counts per token that drift is ~0.001
    token, so the saved empty reading is good enough on its own (open question 4,
    resolved).
- **No CLOSED screen on the cups.** [DECIDED 2026-09-29] The TV shows the race state and
  freezes at the post.

---

## 6. Betting rules

### Money

- **$1 per token.** The guest keeps one half and drops the other in the cup of their horse.
  [DECIDED]
- **The draw.** After the race one token is drawn from the WIN cup, one from PLACE, one
  from SHOW. The drawn token's owner takes that cup's whole prize. Three winners. [DECIDED]
- **Split: 60% / 25% / 15%** of the pot (`LQ_SPLIT_*` in `pi5/config.py`). [DECIDED]
- **Whole dollars, always summing to the pot:** PLACE and SHOW round half up, WIN takes the
  rest. [BUILT]
- **Live figures are scale estimates.** The pot and prizes during betting come from the
  cups.
- **The figures at the post** (pot, prizes, bets per horse) are taken when the state first
  reaches 3 AT_THE_POST (or 4 or 5 if 3 was skipped), saved, and held through the race, the
  draw and any restart. They are dropped by Reset betting and by any state that reopens
  betting (0 or 1). The TV shows them in states 3–5. [BUILT]
- **Counted pot: the payout comes from the hand count.** [DECIDED 2026-09-29, BUILT]
  - After betting closes (states 3–5), the admin page shows a **Counted pot** box.
  - The host counts the cash box's BETS compartment and enters the amount.
  - The pot and all three prizes on the TV and the admin page recalculate from it with the
    same split and rounding. Bets per horse stay as the scales read them.
  - The board marks the pot as hand counted. [DECIDED 2026-10-02, BUILT] The admin page
    shows the scale figure beside the counted one. [DECIDED 2026-10-02, BUILT]
  - Saved; cleared by Reset betting and by reopening betting, the same as the figures at
    the post.
  - This is what makes the crawl's `FINAL RESULTS HAND COUNTED` true.
- **An empty cup in the money: its share goes to the next finisher.** [DECIDED]
  - The host enters **the three horses that pay**, not strictly the race's first three. If
    a finisher's cup had no bets at the post, it is skipped and the next finisher takes its
    place. Example: nobody bet the show horse, so 4th place is entered as SHOW.
  - The TV, the cups and the LEDs then show those three as WIN, PLACE and SHOW.
  - The SET WINNERS picker marks any horse whose cup had no bets at the post, from the
    figures at the post, so the host does not have to spot it. [DECIDED 2026-09-29, TO BUILD]

### Betting lock

- At AT THE GATE the TV freezes on the figures at the post. The cups keep counting
  underneath, which is what the admin page's live figures show. [BUILT]
- The cups' horse pickers lock in states 1–4. [BUILT]

### Scratches: two kinds [BUILT, one change]

1. **Before the Friday deadline, with a replacement.** An also-eligible (21–24) draws in and
   keeps its own program number, as at Churchill (2026: #22 Ocelli ran for #9 The Puma).
   The admin page records `9 → 22`, the cup that says 9 becomes 22 on its own, and nothing
   moves on the mantle. Normally this happens before betting opens, so the cup is empty.
2. **Same day, no replacement: re-bet.** [DECIDED]
   - The horse leaves the field, its cup shows the red X, and its tokens drop out of the pot
     while they are in that cup.
   - The host empties the cup and **hands the tokens back to the bettors**, who re-drop them
     in other cups. The pot climbs back as they do.
   - Anyone who takes a token back and does not re-bet is settled by the counted pot.
   - **The `TOKENS REFUNDED` wording goes**, from the TV board, `RACE_NIGHT.md` and
     `pi5/LQ_BRIDGE.md`. [DECIDED 2026-09-29, TO BUILD] Replacement wording on the board,
     such as `· RE-BET YOUR TOKENS`. [PROPOSED] Every character must exist in the tote
     look's dot font.

- A scratch of either kind is entered once, on the admin page, and can be undone (a chain
  undoes last record first). [BUILT]
- La Subasta must read the same scratch. [BUILT, section 8]

---

## 7. Displays

- **One TV, fed by the splash display app** (port 5001, `splash_display/`). It reads pi5's
  model over HTTP and the event stream and re-serves it. [BUILT]
- **The slideshow** runs trivia, the La Subasta primer, the countdown to post, the roster
  with track odds, Derby Dash and brand slides. [BUILT]
- **The LQ board takes over the TV in states 1–5** and hands it back in 0 and 6. In WINNER
  it is the results board: WIN, PLACE, SHOW, each with cloth, name, bets and prize, and
  `OFFICIAL RESULTS COMING` until the results are confirmed. [BUILT]
- **Board looks:** `dots` (tote look, the default), `impact`, `numbers`. The race state
  shows at the top right. [BUILT]
- **The crawl** carries the disclaimer lines from `LQ_CHYRON_LINES`, the time, time to post
  and the weather. [BUILT; the disclaimer is DECIDED]
  - `TOTALS BASED ON CHEAP CHINESE ELECTRONICS · FINAL RESULTS HAND COUNTED`
  - `NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS`
- **Any screen can show the board** at `joeydevpi.local:5001/`: a phone, a laptop, a second
  TV.
- **Two screens or one?** The earlier plan had one screen always on La Quiniela and a
  second rotating other info. What was built is one TV that switches by race state. Open
  question 1.
- **HUB75 tote board: optional and deferred.** If built, it reads `/api/quiniela` or the
  event stream like the splash does.

---

## 8. La Subasta

- **Today** [BUILT]:
  - Names and program numbers come from La Quiniela's store, read in the same process
    (`HorseStore.field()`, through `pi5/la_subasta/field.py`). The auction lists the field
    as it stands, `in_field` horses by number, 1–24, a replacement under its own number;
    `horse_id` in bids, ownership and payouts is the program number. The mock racing
    service no longer feeds La Subasta.
  - Scratches are La Quiniela's: entered once, on the LQ admin page. La Subasta's own
    scratch button, route and `horse_state` table are gone. Its store listener
    (`pi5/la_subasta/scratches.py`) applies a scratch on change, idempotently, by the
    auction's own rule: while open, the horse's bids are voided; after the lock, its
    ownership too, and the owner's total owed drops. Guest phones update over SocketIO.
- **Decision** [DECIDED 2026-09-29, BUILT]: La Subasta reads
  names, program numbers (1–24, `in_field`) and scratches from La Quiniela's store. A
  scratch is entered once, on the LQ admin page.
- **Timing makes replacements simple.** The auction runs at the party, after the Friday
  scratch deadline, so any replacement has already happened. The auction sells the field as
  it stands, for example #22 in place of #9.
- A same-day scratch after the auction follows La Subasta's own rule in
  `DDM_La_Subasta_Spec.md`.
- **No House.** [DECIDED 2026-10-01, BUILT] No payout ever goes to a House or the DDM
  build fund. A horse nobody bids on is bought by a bidder before the lock, in practice the
  host, at the minimum bid.
- **Undo restores everything.** [DECIDED 2026-10-01, BUILT] Undoing a scratch on the LQ
  admin page puts the horse back in the field and un-voids the bids, and after the lock the
  ownership row, that the scratch voided, so the owner owes again. No horse is ever left
  ownerless by an undo. (This replaces the `ad49ac7` note that an undone horse pays the
  House.)
- **Unsold-horse guard.** [DECIDED 2026-10-01, BUILT] The admin page warns before locking
  if any horse in the field has no bid, and lists them. The lock goes ahead only on confirm.
- **One bidder may be exempt from the max-three cap.** [DECIDED 2026-10-01, BUILT] The
  host, so he can pick up the stragglers. The admin marks the bidder; the cap still applies
  to everyone else.
- **A paying horse with no owner is skipped.** [DECIDED 2026-10-01, BUILT] The next
  finisher pays, the same rule La Quiniela uses for an empty cup. The admin payout ledger
  flags the slot and the admin enters the horse that pays it. No automatic House award.
- Guest pages are on pi5 at `/la-subasta`, port 5000.

---

## 8A. Race info, names and odds

**Race Setup is gone** (`0a050dd`). La Quiniela's store is the one home of race
information. [BUILT]

- **Race info**: the race's name, date and post time, entered on the admin page on Central
  time (`LQ_RACE_TZ`). The TV's countdown and roster slides and the crawl read it.
- **Horse names**: 24 lines pasted into the admin page's names box.
- **Track odds**, for the roster slide only. La Quiniela pays no odds, and the LQ board
  shows none; a horse's line is its bets. The odds come from the odds poller
  (`pi5/la_quiniela/odds.py`, Claude with web search every 5 min, needs `ANTHROPIC_API_KEY`
  and internet) or are typed by hand from the program. They are kept in memory only.
- `GET /api/race` is built from this store for anything that wants a roster.

### Where JoeyAI fits [PROPOSED]

JoeyAI is the GEEKOM A8 MAX mini PC (`10.0.0.54`) running `joeyai-web` (Flask under
gunicorn, port 5000, open to the LAN) with built-in DuckDuckGo web search, and Ollama
behind it with `qwen2.5:7b-instruct`. It can plug in at two points:

1. **The odds poller's fetch** (`OddsPoller.fetch` in `odds.py`): JoeyAI first, Claude as
   the fallback.
2. **A "fill names" helper** that proposes the field into the admin page's names box.

Rules for either:

- **Operator review is mandatory.** A source proposes; only Save commits.
- **pi5 makes one HTTP call to a new `joeyai-web` endpoint** (for example
  `/api/derby-field`), rather than calling Ollama directly. The search, fetch and parsing
  live on the JoeyAI box, and no firewall change is needed.
- **Make a 7B model reliable:**
  - Fetch a full field page rather than trusting search snippets.
  - Constrain the output with a JSON schema.
  - Validate in code: at most 24 entries, program numbers unique and in 1–24, odds that
    parse.
  - Fall back on any failure.
- **Test against the 2026 field.** The draw lands about a week before the Derby, inside
  the code freeze.

---

## 9. Derby Dash

- Loosely coupled; the game does not need the race state to run.
- It could post high scores to pi5 for a leaderboard slide. [PROPOSED]
- It could pause or show "race in progress" during RUNNING. [PROPOSED]

---

## 10. Operator control: who does what [DECIDED 2026-09-29]

| DDM Control Center (touchscreen, `joeydevpi.local:5000/`) | LQ admin page (phone, `joeydevpi.local:5000/quiniela/admin`) |
|---|---|
| LED animations and the Animation Library | Link status, cups online, the Horses list (read-only) |
| The mode buttons: LEDs **and** race state | The current race state, read-only [PROPOSED] |
| SET WINNERS: results, WINNER, the three LED cups; the no-bets marker [TO BUILD] | Figures: pot, prizes, bets |
| RESET: end of the race, clears results, LEDs off, AFTER_PARTY | Counted pot [BUILT] |
| Menu links to the admin page and the board | Reset betting: between races, back to PRE_RACE, new baseline; names, scratches and cup numbers kept |
| | Race info, horse names, scratches and undo, the betting close time |

- **The admin page's seven race-state buttons are removed.** [DECIDED 2026-09-29, TO BUILD]
- **Two different resets.** RESET on the dashboard ends a race. Reset betting on the admin
  page prepares the next one. The runbook says which to use when.
- **No PIN.** [DECIDED 2026-09-29] The dashboard, the admin page and La Subasta's guest
  pages share one server on port 5000. A guest who trims the La Subasta address lands on
  the dashboard. Accepted as unlikely.

---

## 11. Network

- **DevPi by hostname:** `joeydevpi.local`. pi5 on port 5000, the splash on port 5001.
  [BUILT]
- **LED controller:** TCP to `10.0.0.44:5005` per the repo's `pi5/config.py`. DevPi's local
  config was reported as `10.0.0.42`, so one of them is stale. Open question 5.
- **ESP-NOW on channel 6.** Cups and gateway never touch WiFi, so guest load on the router
  cannot affect betting.
- For the party, the router's 2.4 GHz band sits on channel 1 or 11. [PROPOSED]
- DHCP reservations for every fixed device. [PROPOSED]

---

## 12. Resilience

| Failure | Behavior |
|---|---|
| pi5 restarts | The gateway keeps broadcasting the last line. pi5 restores the state line from `lq_link_state`, the results from `results.json`, the figures at the post from `lq_closing`, and seeds each cup's count from `lq_cups`, so no phantom bets. Opening the port does not reboot the gateway. [BUILT] |
| DevPi reboots | Same, and the splash finds pi5 again on its own. [BUILT] |
| Gateway reboots | Sends `hello`, stays silent until pi5 answers, never enters demo mode. Cups keep showing their own numbers. [BUILT] |
| Power blip takes out DevPi and the gateway | The gateway is back in a second and silent. Cups show their own numbers with a `NO LINK` badge. Everything resumes when pi5 is up. [BUILT] |
| A cup reboots | Its horse number and its count come back from NVS. Through the warm-up it reports the saved count, then re-zeroes only if it reads empty; a cup with a pile keeps its saved empty reading (section 5). Bench-tested 2026-10-01. [BUILT] |
| A cup dies | Spare cup: set its number, put it in the post, move the tokens. [BUILT] |
| TV or splash reloads | Fetches the model; shows the figures at the post, not emptied cups. [BUILT] |
| LED controller unreachable | The race state and results still apply; the dashboard says `LEDs unreachable`. [BUILT] |
| No internet | No odds and no weather; nothing else notices. [BUILT] |
| SD card failure | SQLite copied off the box on a schedule, and a cloned SD card on hand. [PROPOSED] |

- pi5 has no service file yet (`RACE_NIGHT.md` section 0). It should start on its own after
  a power cycle, under systemd only, never alongside a foreground copy. [PROPOSED]
- The one-week code freeze before DDM applies.

---

## 13. Open questions

1. **Two screens or one?** Is the one TV that switches between the slideshow and the board
   the plan, or is a second, always-La-Quiniela screen still wanted?
2. **JoeyAI for 2027?** Build it as an odds and names source (section 8A), or leave the
   Claude odds poller as is?
3. **Same-day scratch wording** on the board, replacing `TOKENS REFUNDED`.
4. ~~**Scale drift across a power cycle.**~~ Resolved 2026-10-01: the brownout bench test
   read a drift of −5 counts (≈0.001 token at ≈6004 counts per token) across a power
   cycle; the saved empty reading is good enough on its own.
5. **LED controller address:** `10.0.0.44` (repo) or `10.0.0.42` (DevPi's local config)?
6. **The bridge's SocketIO events:** keep them for a future screen (HUB75), or remove them
   as unused?

Resolved since v0.9: cup counting (the cup counts; the settled weight decides), odds on
the board (none; bets only), LED sub-phases (the mode table), display transport (HTTP and
an event stream), La Subasta migration (yes, section 8), admin PIN (no), cups showing
CLOSED (no), scale drift across a power cycle (−5 counts; the saved zero is enough).

---

## 14. Cup simulator [BUILT]

`pi5/la_quiniela/simulator.py`, documented in `pi5/LQ_SIMULATOR.md`. A virtual gateway,
twenty cups and two spares speaking the real line protocol, with scripted scenarios and a
self-check.
Its MACs all start `02:DD:4D:`, and pi5 drops them from its cup cache as soon as a real
gateway says hello.

---

## 15. Build order

### Done

- Serial line protocol and gateway (now v2)
- The DevPi bridge
- Protocol v2: the cup owns its number
- One race state with the dashboard as its source
- The cup simulator
- The TV board and results board
- The admin page
- The figures at the post
- Race info and names in La Quiniela's store; Race Setup removed
- Cup brownout: a cup comes back with its count (cup sketch v0.7, bench-tested 2026-10-01)
- La Subasta on La Quiniela's store: names, program numbers 1–24, scratches
- Counted pot: the pot and the prizes come from the host's hand count of the cash box

### Next

One single-concern Claude Code prompt each, in this order:

1. ~~**Cup brownout:** save the empty reading and the last count; re-zero at startup only
   when empty. Reflash the four cups, then run the bench test (section 5).~~ Done
   (bench-tested 2026-10-01; listed under Done).
2. ~~**La Subasta on La Quiniela's store:** names, program numbers 1–24, scratches.~~
   Done (2026-10-01; listed under Done).
3. ~~**Counted pot** on the admin page; pot and prizes from the hand count.~~ Done
   (2026-10-02; listed under Done).
4. **SET WINNERS no-bets marker.**
5. **Remove the admin page's race-state buttons**; show the state read-only; update
   `RACE_NIGHT.md`, which uses them in several places.
6. **Re-bet wording** for a same-day scratch: the board, `RACE_NIGHT.md`, `pi5/LQ_BRIDGE.md`.

### Later

7. pi5 as a systemd service.
8. JoeyAI as an odds and names source, if open question 2 says yes.
9. HUB75 renderer, if it earns its place.

### Housekeeping

- Commit this file over the v0.5 copy at the repo root.
- Mark `DDM_La_Quiniela_Spec.md` superseded, at its top.
- Update the vault status page. The repo's `CLAUDE.md` tells Claude Code to trust the vault
  over the repo docs, so a stale vault page misleads every session.

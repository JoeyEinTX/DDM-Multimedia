# DDM System Architecture Spec

**Version:** 0.5 (draft for review)
**Date:** 2026-09-18
**Target:** DDM 2027
**Repo:** `github.com/JoeyEinTX/DDM-Multimedia`

This spec defines how information moves between every device and display in the DDM
system. Subsystem specs (La Subasta, La Quiniela firmware, Derby Dash) describe what each
piece does on its own; this document describes how they share one picture of the race.

Items marked **[PROPOSED]** are design suggestions not yet confirmed. Items marked
**[DECIDED]** were settled in discussion.

---

## 1. Principles

1. **DevPi is the race brain.** One state machine and one SQLite database on DevPi hold
   everything that matters: horse field, phase, scratches, bet counts, results.
2. **Every other device is a sensor or a renderer.** Cups sense weight and render a
   number. Displays render pages. The LED controller renders animations. None of them
   store anything the system cannot rebuild from DevPi.
3. **One phase enum, one horse table.** Defined once on DevPi, mirrored everywhere else.
4. **Everything survives a reboot.** Any single device, including DevPi, can restart
   mid-party and the system recovers on its own.
5. **Loose coupling.** A subsystem being absent (no HUB75, no Derby Dash) must not break
   anything else.

---

## 2. System map

```
              ┌────────── DevPi: Flask + SocketIO + SQLite ──────────┐
              │   race state machine · horse field · bets · results   │
              └───┬───────────┬─────────────┬──────────────┬─────────┘
        USB serial│        UDP│     SocketIO│      SocketIO│
              Gateway      LED ESP32     Browsers        Tote Pi 4B
           ESP-NOW│        (mantle)    splash x2 / TV /   (HUB75,
              20 cups                  phones / admin      optional)
```

| Device | Role | Transport to DevPi | Stores |
|---|---|---|---|
| DevPi (Pi 5) | Race brain, web server | n/a | Everything (SQLite) |
| Gateway (ESP32 WROOM-32) | Radio-to-USB bridge | USB serial | Last state packet, RAM only |
| 20 betting cups (CYD + HX711) | Weigh tokens, show horse number | ESP-NOW via gateway | Tare + calibration in NVS |
| LED controller (ESP32, 10.0.0.44:5005) | Mantle animations | UDP over WiFi | Nothing |
| Splash display A (kiosk Pi) | La Quiniela status | SocketIO (browser) | Nothing |
| Splash display B (kiosk Pi) | Rotating info | SocketIO (browser) | Nothing |
| TV | Any display URL | SocketIO (browser) | Nothing |
| Guest phones | La Subasta | SocketIO (browser) | Nothing |
| Tote board (Pi 4B + HUB75) | Optional LQ renderer | SocketIO (Python client) | Nothing |
| Derby Dash | Arcade game | Loosely coupled, see section 9 | Own scores |
| JoeyAI (GEEKOM A8 MAX mini PC, 10.0.0.54) | Horse field lookup, see section 8A | HTTP, called by DevPi | Nothing DDM-related |

The drink jug LED ring has been dropped from DDM 2027 and is not part of this design.

---

## 3. Race state machine

Phases, matching `ddm_common.h`:

`PRE_RACE → BETTING_OPEN → FINAL_CALL → AT_THE_POST → RUNNING → WINNER → AFTER_PARTY`

- DevPi owns the current phase and persists it to SQLite on every change.
- Only the operator (admin page) advances the phase.
- A phase change is one function on DevPi that fans out to every transport together:
  1. Write to SQLite
  2. Serial → gateway → ESP-NOW broadcast to cups
  3. UDP → LED controller
  4. SocketIO → all browsers and the tote board
- The numeric enum values in `ddm_common.h` are the wire format. DevPi's Python enum must
  match them. **[PROPOSED]** A smoke test asserts the Python enum against the header so
  they cannot drift.

---

## 4. Data model [PROPOSED]

| Table | Purpose | Key fields |
|---|---|---|
| `race` | One row per race/year | year, current phase, locked_at |
| `horses` | The field, shared by La Quiniela and La Subasta | number, name, scratched, scratched_at |
| `cups` | Hardware identity | mac, cup_id, horse number, last_seen, rssi |
| `telemetry` | Append-only log of cup packets | cup_id, ts, raw_weight, token_count, seq, dropped |
| `lock_snapshot` | Counts frozen at AT_THE_POST | cup_id, token_count, raw_weight |
| `results` | Finish order, as deep as needed for payouts | position, horse number |
| `events` | Audit log | ts, type (phase change, scratch, tare, cup swap), detail |

Notes:

- **Raw weight is always logged next to token count.** Given the measured hysteresis, this
  allows recounting after the fact.
- **No per-token or per-guest data is stored.** See section 6.
- `telemetry` at 20 cups is small, but **[PROPOSED]** log on change plus a periodic
  heartbeat row rather than every packet.

---

## 5. La Quiniela data flow

### Up: cup → DevPi

1. Cup unicasts `DdmTelemetryPacket` to the gateway (existing).
2. Gateway writes one line per packet to USB serial. **[PROPOSED]** JSON lines, for
   example `{"t":"telem","cup":7,"raw":812345,"count":14,"rssi":-64,"seq":9021,"drop":2}`.
   Human-readable in a serial monitor, trivially parsed in Python.
3. A bridge thread on DevPi (pyserial) parses each line, updates `cups`, appends to
   `telemetry`, and emits a SocketIO `lq_update` event.
4. Displays update live from that event.

### Down: DevPi → cups

1. On any change to phase, horse-per-cup, or scratches, DevPi sends one state line down
   serial.
2. Gateway stores it in RAM and rebroadcasts at 2 Hz (existing behavior).
3. If DevPi's service restarts, the gateway keeps broadcasting the last state. Cups never
   notice.
4. On gateway boot with no state from DevPi yet, it sends a `hello` line up serial and
   DevPi replies with current state.

### Cup rules

- **Tare lives in NVS and is never applied automatically at boot.** A cup that browns out
  with tokens inside must come back with the same count. Tare changes only from the hidden
  long-touch menu, and each tare is reported to DevPi for the `events` log.
- **Cup identity lives on DevPi.** The `cups` table maps MAC → cup ID. Swapping in a spare
  cup is an admin-page action, not a reflash.
- **Standalone fallback.** With no gateway heard for N seconds, the cup keeps showing its
  last horse number and keeps counting locally.
- **CLOSED card.** From AT_THE_POST onward the cup display shows betting closed.

---

## 6. Betting rules

### Payout: draw from the winning cup [DECIDED]

- Guests keep half of a numbered token; the other half goes in a cup.
- After the race, halves are drawn from the winning cup; the matching half wins.
- **Win, place, and show all pay: 60% / 25% / 15% of the pot.** [DECIDED] Three cups are
  drawn from, so DevPi needs the full finish order, and after the race `/display/lq` and
  the admin page show the host which three cups to draw from and what each pays.
- One half is drawn per cup, three winners total. **[ASSUMED, not yet confirmed]**
- **Live totals are estimates; the payout is not.** Live pot and odds come from the scales.
  Before the draw, the host counts the BETS compartment of the cash box and enters the
  real pot on the admin page. DevPi computes the three payouts from that number, not from
  the scale total. **[PROPOSED]**
- **[PROPOSED]** Payouts round down to whole dollars, with the remainder going to win.
- **Empty cup in the money: the share rolls to the next finisher.** [DECIDED] If nobody bet
  the show horse, the 15% goes to the cup of the 4th-place horse, and so on down until a
  cup with tokens is found. Consequences:
  - The `results` table stores finish order as an ordered list, not three fixed columns.
  - At results entry, the admin page checks each paying cup against `lock_snapshot`. If
    one is empty, it asks the host for the next finisher before showing payouts.
  - Scratched horses do not run, so only unbet horses can trigger this.
- **Consequence:** counts are display data, not money. They drive pot size, odds, and
  "hot horse" displays. An off-by-one count never costs anyone a dollar.
- Token numbers exist only on the plastic. The system never knows who bet what.
- Winner names are not recorded. **[OPTIONAL]** A free-text "last year's winner" field for
  a callout at the next DDM.

### Betting lock

- At AT_THE_POST, DevPi copies current counts into `lock_snapshot` and all displays freeze
  on those numbers.
- Tokens detected after lock are flagged in `events`, not added to displayed counts.
- The real enforcement is physical: cups show CLOSED.

### Scratches: re-bet [DECIDED]

1. Operator marks the horse scratched on the admin page.
2. `horses.scratched` is set; the cup shows the alternating greyed number / red X; La
   Subasta sees the same flag from the same table.
3. DevPi records the cup's count at scratch time in `events`.
4. Host empties the cup; guests re-drop those tokens in other cups.
5. The cup's count falls to zero on its own. **Nobody re-tares.**

Pot handling during a re-bet: tokens are only ever sold, so the true pot never decreases.
**[PROPOSED]** The displayed pot holds its value while a scratched cup is being emptied
and redistributed, rather than dipping and recovering on screen.

---

## 7. Displays

Each display is a URL on DevPi, not a separate app. Kiosks boot Chromium to a URL and
subscribe to the same SocketIO feed.

| URL | Content |
|---|---|
| `/display/lq` | Always La Quiniela: pot, odds, count per horse, phase, open/closed |
| `/display/info` | Rotating: trivia, La Subasta status, schedule, Derby Dash scores |
| `/admin` | Operator control (section 10) |

- What a screen shows is a routing decision on DevPi. Screens can be swapped, added, or
  pointed at the TV without touching the kiosk.
- **Cheeky accuracy disclaimer on `/display/lq`.** [DECIDED] Live totals are real dollars
  measured by hobby-grade scales, so the LQ screen carries a tongue-in-cheek disclaimer.
  It rides in a scrolling ticker rather than sitting on screen permanently. Ticker lines
  live in a config list on DevPi so they can be edited without touching the page. Example:
  "Totals are based on cheap Chinese electronics."
- **HUB75 tote board is optional and deferred.** It is one more subscriber to the same
  `lq_update` feed. Nothing else depends on it. Decision on whether it adds value comes
  after the hardware is built.
- **[PROPOSED]** Every display page shows a small "reconnecting" indicator when its socket
  drops, and re-requests full state on reconnect rather than waiting for the next change.

---

## 8. La Subasta integration

- La Subasta and La Quiniela read the same `horses` table. A scratch is entered once.
- La Subasta already exists with its own horse list (spec v1.2). Migrating it onto the
  shared table is an open item; see section 13.
- `/display/info` shows Subasta status by subscribing to Subasta's existing events.

---

## 8A. Horse field ingestion

The dashboard already has a Race Setup modal (built for DDM 2026) with 20 post-position
fields, post time, and three actions: fetch from the Racing API, "Ask JoeyAI", and Save to
`/api/race-setup`. Today the "Ask JoeyAI" button calls the Anthropic API with web search.
The goal for DDM 2027 is for that button to call the real JoeyAI.

### What JoeyAI is today

- Dedicated headless mini PC: GEEKOM A8 MAX, hostname JoeyAI, `10.0.0.54`. (Not the Jetson
  cluster; that was the earlier repo.)
- `joeyai-web` (repo `JoeyEinTX/joeyai-web`): Flask app under gunicorn on port 5000, open
  to the LAN subnet. SQLite conversations, profiles, and a **DuckDuckGo web search
  feature already built in**.
- Ollama on port 11434 with `qwen2.5:7b-instruct-q4_K_M`, firewalled to specific hosts.

### How DevPi reaches it [PROPOSED]

Two paths exist. Path B is recommended.

| | Path A: Ollama direct | Path B: joeyai-web endpoint |
|---|---|---|
| Call | DevPi → `10.0.0.54:11434` | DevPi → `10.0.0.54:5000/api/derby-field` (new) |
| Web search | DevPi would have to build its own | Reuses the search JoeyAI already has |
| Firewall | New ufw rule for DevPi | Already open to the LAN |
| Where the logic lives | DDM repo | joeyai-web repo |

Path B keeps DDM simple: DevPi makes one HTTP call and gets back field JSON. All the
search, fetch, and parsing lives on the JoeyAI box, where it can be reused and tested on
its own.

### Making a 7B model reliable at this

- **Fetch a known page, do not rely on search snippets.** Snippets rarely contain all 20
  horses with odds. The endpoint should fetch the full text of a field page and have the
  model extract from it.
- **Constrain the output.** Use Ollama's structured output (JSON schema) so the model can
  only return the field shape.
- **Validate in code, not in the model:** at most 20 entries, post positions unique and in
  1..20, odds parse as a fraction. Reject and fall back on failure.
- **Test before the field exists.** The post position draw happens about a week before the
  Derby, which is inside the code freeze. Build and test against the 2026 field pages.

Architecturally, all of these are **sources that fill the Race Setup form**. None of them
write to the `horses` table directly.

```
Racing API ─┐
JoeyAI      ├─→ Race Setup form → operator reviews → Save → horses table → everything
Anthropic   │
Manual      ┘
```

Rules:

- **Operator review is mandatory.** A source proposes; only Save commits. A wrong horse
  name on 20 cups and two screens is worse than typing the field by hand.
- **All sources return the same JSON shape:** post position, name, morning-line odds,
  scratched flag. The form does not care which source produced it.
- **Sources fall back in order.** [PROPOSED] JoeyAI → Anthropic API → Racing API → manual.
  If the JoeyAI box is down on Derby day, the button still works.
- **A local model does not know this year's field.** It has to read it from the web; the
  model's job is to parse what it finds into the JSON shape. That fetch path is the real
  work in this feature, not the model.
- Migration: Race Setup currently saves to a JSON file. It moves to the shared `horses`
  table so La Quiniela, La Subasta, the ticker, and the results banner all read one source.

### Two kinds of odds

| | Source | Changes | Meaning |
|---|---|---|---|
| **Track odds** | Racing API / JoeyAI | Until post time | What Churchill Downs says |
| **Party odds** | Cup counts | Until betting lock | What your guests think |

- Track odds that matter on the day are live, so a scheduled Racing API refresh suits them
  better than an LLM lookup. **[PROPOSED]** JoeyAI fills names and morning line once; the
  Racing API, if subscribed, refreshes odds and scratches on a timer.
- An automated scratch from any source is a **suggestion** shown on the admin page, never
  applied on its own, because a scratch triggers a re-bet at the mantle.

---

## 9. Derby Dash

- Loosely coupled. The game does not need race state to run.
- **[PROPOSED]** It posts high scores to DevPi so `/display/info` can show a leaderboard.
- **[PROPOSED]** It listens for phase, so it can pause or show "race in progress" during
  RUNNING.

---

## 10. Operator control

- One admin page drives everything: phase advance, scratches, cup-to-horse assignment, cup
  swap, results entry.
- Guests are on the same WiFi for La Subasta, so `/admin` requires a PIN. **[PROPOSED]**
- Phase advance and scratch each need a confirm step; both are hard to undo mid-party.

---

## 11. Network

- DHCP reservations for every fixed device (DevPi, ddm-splash, second kiosk, LED
  controller, tote Pi).
- DevPi currently has two IPs (10.0.0.87 / 10.0.0.83). Pick one as canonical for kiosk
  URLs, or use a hostname.
- ESP-NOW is pinned to channel 6. For the party, the home router's 2.4 GHz band should sit
  on channel 1 or 11.
- Cups and gateway never touch WiFi, so guest load on the router cannot affect betting.

---

## 12. Resilience

| Failure | Behavior |
|---|---|
| DevPi service restarts | Gateway keeps rebroadcasting last state; on start, DevPi restores phase from SQLite and resends state |
| DevPi full reboot | Same, plus kiosks auto-reconnect and re-request state |
| Gateway reboots | Sends `hello` up serial, gets state back, resumes broadcast |
| Cup reboots | Keeps tare from NVS, sends HELLO, picks up state within one broadcast |
| Cup dies | Swap spare, reassign on admin page |
| Kiosk drops | Reconnects, re-requests full state |
| SD card failure | **[PROPOSED]** SQLite copied off-box on a schedule; cloned SD card on hand |

DevPi is both the dev box and the production box. The existing one-week code freeze
applies. **[PROPOSED]** Party mode runs under systemd only, never alongside a foreground
script.

---

## 13. Open questions

1. **Halves drawn per cup:** the spec assumes one half per cup, three winners total.
   Confirm.
2. **La Subasta horse list migration:** move it onto the shared `horses` table now, or
   bridge the two until after DDM 2027?
3. **LED-only sub-phases:** the LED animations may use states not in `ddm_common.h` (for
   example a cooldown after WINNER). Are these real phases, or animation details inside a
   phase?
4. **Who counts tokens:** the cup computes `tokenCount` today. Keep the cup authoritative,
   or have DevPi recompute from raw weight with its own calibration table?
5. **Odds display:** true pari-mutuel style odds from counts, or just counts and
   percentages?
6. **Second kiosk hardware:** another Pi 5, or something already on the shelf?
7. **Which odds go on screen:** track odds, party odds, or both side by side on
   `/display/lq`?
8. **JoeyAI integration path:** Path A (Ollama direct) or Path B (new `joeyai-web`
   endpoint)? See section 8A. Also: does JoeyAI's existing web search fetch full pages, or
   only search snippets?
9. **Existing dashboard transport:** the DDM 2026 dashboard synced results over SSE. Move
   everything to SocketIO, or leave working SSE paths alone?

---

## 14. Cup simulator [PROPOSED]

A Python script that speaks the gateway's serial line protocol and fakes 20 cups.

- All Flask, display, and admin work proceeds with no hardware on the bench.
- Can replay a recorded `telemetry` log from a real party.
- Can inject faults: cup dropout, reboot, late tokens after lock, a scratch and re-bet.

This is the over-engineered option, and it is the one most likely to pay for itself,
because it removes hardware from the critical path of every software task below.

---

## 15. Suggested build order

Each item is sized to be one single-concern CC prompt.

1. Serial line protocol: gateway emits and accepts JSON lines
2. DevPi serial bridge thread: parse, store, emit `lq_update`
3. Shared `horses` and `cups` tables plus the phase state machine with fan-out
4. Cup simulator
5. `/display/lq` page
6. `/admin` page: phase, scratch, cup assignment, PIN
7. Cup firmware: NVS tare, CLOSED card, standalone fallback
8. Betting lock and `lock_snapshot`
9. `/display/info` page
10. Boot-restore and reconnect behavior, tested with the simulator's fault injection
11. La Subasta onto the shared `horses` table
12. Race Setup saves to the `horses` table instead of JSON
13. JoeyAI as a Race Setup source, with Anthropic fallback
14. HUB75 renderer, if it earns its place

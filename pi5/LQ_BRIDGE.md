# La Quiniela bridge (`pi5/la_quiniela/`)

The DevPi end of the La Quiniela gateway link. A daemon thread inside the
Flask app talks to the ESP32 gateway over USB serial:

- **reads** the gateway's JSON lines, keeps a live picture of the 20 cups,
  writes the `cups`, `telemetry` and `events` tables, and emits SocketIO
  events for displays;
- **writes** `roster` and `state` lines down, answers the gateway's `hello`,
  and re-sends whenever a `status` line shows the gateway out of sync;
- **never takes Flask down**: every serial failure is caught, logged and
  retried.

The line protocol is `firmware/quiniela/README.md`, section "Serial line
protocol". That document wins on any difference.

## The one ID rule

Cup IDs are **0-based on the wire** and **1-based everywhere on DevPi**:
database, Python API, SocketIO events, HTTP JSON. Wire `-1` (MAC not in the
roster) is `None`. `la_quiniela/protocol.py` holds the only two conversions,
`wire_to_cup()` and `cup_to_wire()`, used where a line is read or built and
nowhere else; the test suite greps for any other `± 1` on a cup ID.

## Turning it on, on DevPi

1. `pip install -r requirements.txt` (adds `pyserial`).
2. Find the gateway's port:

   ```
   ls -l /dev/serial/by-id/
   ```

   Use that `/dev/serial/by-id/usb-...` path, never `/dev/ttyUSB0`, which
   can change between boots. A CH340-based board has no serial number, so
   if two CH340 devices are ever plugged in, use the physical-slot path from
   `ls -l /dev/serial/by-path/` instead.
3. Set `LQ_SERIAL_PORT` in `pi5/config.py`, or export
   `DDM_LQ_SERIAL_PORT=/dev/serial/by-id/...` for the service.
4. Restart the app. The log shows `La Quiniela bridge started on ...`, then
   the gateway's `hello` is answered with whatever roster and state DevPi has.

With no port configured the bridge idles and logs once how to set it. With
`LQ_BRIDGE_ENABLED = False`, or without pyserial, it logs one line and the
rest of the app, La Subasta included, runs exactly as before.

The port is opened with DTR and RTS held low, so a DevPi service restart
does not reset the gateway, and with `exclusive=True`, so a second copy of
the app cannot open the same port. A missing or vanished port is retried
every 5 s forever, logged at most once a minute.

## Configuration (`pi5/config.py`)

Every key can be overridden by an environment variable of the same name
prefixed `DDM_`, so a simulator can point the app at a virtual port without
editing files (`DDM_LQ_SERIAL_PORT=/dev/pts/3`).

| Key | Default | Meaning |
| --- | --- | --- |
| `LQ_BRIDGE_ENABLED` | `True` | Master switch |
| `LQ_SERIAL_PORT` | `""` | Port path; empty = idle |
| `LQ_SERIAL_BAUD` | `115200` | |
| `LQ_SERIAL_LINES` | `"leave"` | `"leave"` never touches the port's DTR and RTS control lines; `"low"` holds both low before opening. See below. |
| `LQ_HEARTBEAT_LOG_S` | `10` | Per-cup heartbeat interval for logging and display refresh |
| `LQ_CUP_OFFLINE_S` | `6` | No telemetry for this long = cup offline |
| `LQ_GATEWAY_OFFLINE_S` | `12` | No line at all for this long = gateway offline |
| `LQ_DEAF_REOPEN_S` | `20` | Port open but no valid line for this long = close it and open it again |
| `LQ_REOPEN_MIN_GAP_S` | `30` | Never reopen more often than this |
| `LQ_DEV_ENDPOINTS` | `False` | Enables the dev-only POST routes |

## Database

The tables live in the app's one SQLite database, the file La Subasta uses
(`pi5/data/la_subasta.db`), created idempotently at startup. A table of the
same name with a different shape is reported and never altered; the bridge
then refuses to start. Timestamps are UTC ISO 8601 with a `Z`.

| Table | Rows |
| --- | --- |
| `cups` | one per MAC ever seen: `mac` (PK), `cup_id` (1..20 or NULL, unique when set), `horse`, `last_seen`, `rssi`, `up_rssi`, `last_count`, `last_raw`, `online` |
| `telemetry` | append only: `ts`, `cup_id`, `mac`, `raw_weight`, `token_count`, `seq`, `dropped`, `rssi`, `up_rssi`, `reason` (`change` or `heartbeat`) |
| `events` | audit log: `ts`, `type`, `cup_id` (nullable), `detail` (JSON) |
| `lq_link_state` | one row: `state_rev`, `state_json`, `roster_rev`, `roster_json`; how the bridge answers a `hello` after a restart |
| `lq_horses` | the betting board's: `horse` (PK, 1..20), `name` (as typed), `replaced` (the scratched horse this number stands in for, or NULL) |
| `lq_board` | one row: `names_rev`, `closes_at` (unix time or NULL) |

Telemetry arrives about ten lines a second and DevPi runs on an SD card, so
the database is written only when a token count changes, when the heartbeat
interval passes, when a cup goes online or offline, when a cup number
changes, and for events.

Event types: `cup_hello`, `cup_online`, `cup_offline`, `cup_claim_mismatch`,
`roster_mismatch`, `gateway_hello`, `gateway_reboot`, `gateway_err`,
`state_set`, `roster_set`.

## Who owns cup numbers

- **No roster yet** (`has_roster` false): DevPi mirrors the gateway. A MAC's
  `cup_id` is whatever the gateway reports.
- **With a roster**: DevPi is right. A cup the gateway reports differently
  logs a `roster_mismatch` event and triggers a roster re-send; DevPi's
  table is never overwritten. `adopt_roster()` turns the mirrored numbers
  into the first roster without typing 20 MACs.

Adopt only once every cup has reported. It copies the cups DevPi has heard
from at that moment, so a cup that has not reported yet is left out, and
adopting again will not add it: from the first roster on, DevPi owns the cup
numbers, so the gateway reports that cup as `-1` and there is nothing left
to mirror. A cup left out this way is put back by naming it in a roster,
through the admin page or `set_roster()`. The roster is also persisted, so a
short roster is inherited by the next run of the app. The simulator avoids
all of this by naming all 20 MACs at the start of every scenario rather than
adopting.

### Whether DevPi holds anything is not a rev

`state_rev` and `roster_rev` only ever increase, including across a reset, so
a rev of 0 no longer means "nothing set". Two flags say that: `has_state` and
`has_roster`, both in `get_snapshot()["devpi"]`. Everything that used to read
a rev of 0 reads a flag instead, the answer to a `hello` included. They are
persisted as the presence of `state_json` and `roster_json` in
`lq_link_state`, so no schema change was needed.

## Resetting the link

`reset_link(reason)` makes DevPi forget its roster and its state and go back
to mirroring the gateway. It exists because the cup simulator writes a roster
of twenty invented MACs into this same database, and that roster must never
reach a real gateway.

- Both revs go up, so a gateway can never mistake the reset for an older
  roster. What changes is that DevPi stops claiming to hold one, and answers
  the next `hello` with nothing.
- `cups.cup_id` and `cups.horse` are set to NULL on every row. Rows whose MAC
  starts with `02:DD:4D:` are deleted outright: they are simulated cups and
  there is no real cup behind them.
- `telemetry` and `events` are left alone. One `lq_reset` event records the
  reason and the new revs.
- A fresh `lq_snapshot` and `lq_link` go to the room.
- **Nothing is sent to the gateway.** There is no line in the protocol that
  means "forget what I told you", so a gateway that already holds a roster
  keeps it until it is power-cycled. Reset DevPi, then power-cycle the
  gateway, and both start clean.

### The automatic guard

Nobody has to remember to do this. Every MAC the simulator invents starts
`02:DD:4D:`, its gateway included (`02:DD:4D:FF:FF:FF`), and nothing real uses
that prefix. So when a `hello` arrives, if the stored roster holds any
`02:DD:4D:` MAC and the gateway saying hello does not, the bridge calls
`reset_link("sim_roster_discarded")` before answering, and then answers with
nothing.

The reverse needs no guard: a simulator scenario names all twenty of its own
MACs at the start, replacing whatever roster was there.

Without this, the first `hello` after a simulator session hands the real
gateway twenty MACs that do not exist. Every real cup is then reported as
`-1`, never gets a number, and sits on its MAC waiting screen with nothing on
it to explain why.

## If starting the app reboots the gateway

`LQ_SERIAL_LINES` decides how the bridge handles the two control lines on the
USB serial cable, DTR and RTS. On an ESP32 board those lines are wired to the
reset circuit, so how they are driven when the port is opened decides whether
the gateway keeps running or starts over.

- **`leave`**, the default, does not touch them at all. This is right for the
  CP2102 gateway board tested on the bench on 19 September 2026. Linux raises
  both lines together when a port is opened, which that board's reset circuit
  ignores, so the gateway carries on through an app restart.
- **`low`** holds both lines low before opening. This was meant to prevent a
  reset and on that board causes one, because setting them one after the other
  passes through the one combination that pulls the reset line down. It is
  kept in case another board turns out to need it.

### How to tell which one a board needs

Leave the gateway running with a race set up, so it is holding a state, then
restart the app and watch its console. A gateway that already holds a state
never introduces itself, so:

- No `[LQ] gateway hello` line: the gateway kept running. The setting is right.
- `[LQ] gateway hello` within a few seconds, and `up_s` in
  `/api/lq/snapshot` back near zero: starting the app rebooted the gateway.
  Try the other value.

To change it, put this line in `pi5/.env` and restart the app:

```
DDM_LQ_SERIAL_LINES=low
```

`pi5/.env` is read before anything else, so nothing in `pi5/config.py` needs
editing. The `[LQ] bridge started` line names the mode in use, for example
`[LQ] bridge started on /dev/serial/by-id/... @ 115200, lines: leave`. A value
that is neither `leave` nor `low` warns once and behaves as `leave`.

## If the gateway shows offline

### What the console says

The app prints a few lines about the bridge while it runs, each starting with
`[LQ]`. They are the quickest way to tell a working bridge from a stuck one
without opening a browser. Telemetry is never printed, so a quiet console
after `gateway online` is a good sign, not a bad one.

| Line | Means |
| --- | --- |
| `bridge started on /dev/serial/by-id/... @ 115200, lines: leave` | The reader thread is running. This should be the first one. The last word is `LQ_SERIAL_LINES`. |
| `cannot open ... ; retrying every 5 s` | The port is not there. Check the cable and the path. Printed once, then at most once a minute. |
| `gateway hello from 24:6F:...` | The gateway introduced itself. It does this until DevPi answers. |
| `answered the hello with roster rev N and state rev N` | DevPi told the gateway what it knows. |
| `gateway online` | Lines are arriving. |
| `re-sent state rev N to the gateway` | The gateway had drifted and was corrected. |
| `gateway offline: nothing heard for 12 s` | The gateway stopped talking. |
| `no data from the gateway for 20 s - reopening the port` | The watchdog, below. |
| `reader thread failed (N), restarting in 5 s` | Something unexpected. The traceback is in the log; the bridge carries on. |

### The watchdog

The gateway sends a `status` line every 5 seconds, so on a healthy link
something valid arrives constantly. If the port is open but nothing valid has
come out of it for `LQ_DEAF_REOPEN_S` (20 s), the bridge closes the port and
opens it again through the normal path, writes a `bridge_reopen` event and
says so on the console. It will not do this more often than
`LQ_REOPEN_MIN_GAP_S` (30 s). With no gateway plugged in at all this simply
repeats every 30 seconds, which is harmless.

This exists because of a real failure on the bench. A USB serial adapter hands
over a burst of junk the instant the port is opened: bytes that arrived before
the baud rate was applied, on a line that never stops talking. The old reader
used `readline()`, which has no size limit and no overall deadline, so a burst
with no newline in it blocked the reader thread for as long as the bytes kept
coming. That also starved the timer, so nothing noticed and nothing recovered
until the app was restarted. The reader now takes bounded chunks and splits
lines itself, a newline always returns it to a clean start of line, and the
watchdog is the backstop for anything else.

### What the snapshot says

`GET /api/lq/snapshot` carries these under `link`, alongside the older fields:

| Field | Means |
| --- | --- |
| `thread_alive` | The reader thread is running. False here is the whole answer. |
| `last_line_age_s` | Seconds since any line at all, junk included. |
| `lines_ok` | Lines that parsed as JSON. |
| `lines_bad` | Lines that did not: the gateway's `# ` text, bad JSON, over-long, unknown type. |
| `bytes_rx` | Bytes taken off the port. |
| `reopens` | How many times the watchdog has reopened the port. |

Read them together. Bytes climbing with `lines_ok` stuck at zero means the port
is delivering something that is not this protocol, usually the wrong baud rate
or the wrong port. Bytes not moving at all means nothing is being sent. Both
climbing normally with `gateway_online` false means the lines stopped
arriving recently, so check the gateway.

`reason` names the last change the link went through. It is never `online`
unless the gateway really is: opening the port says `port_open`, which is what
the bench snapshot should have said.

## SocketIO events

Guest phones share this server for La Subasta, so La Quiniela events go to
the room `lq` only. A display sends `lq_request_snapshot` on connect and on
reconnect; that joins it to the room and answers with `lq_snapshot`.

`lq_update`, one cup, on a count change, heartbeat, online/offline change,
horse or scratched change, or cup number change:

```json
{"cup":8,"mac":"A0:B7:65:12:34:56","horse":8,"scratched":false,"count":14,"raw":812345,"rssi":-64,"up":-61,"drop":2,"online":true,"last_seen":"2027-05-01T21:14:07Z"}
```

`lq_link`, on every change of port-open, gateway-online or in-sync, plus a
`hello` (`boot`) and a protocol mismatch:

```json
{"port_open":true,"gateway_online":true,"in_sync":true,"reason":"status","gateway_mac":"24:6F:28:AA:BB:CC","phase":1,"state_rev":42,"roster_rev":7,"cups_heard":18,"rejects":0,"up_s":5230}
```

`reason` is one of `boot`, `reboot`, `online`, `offline`, `port_closed`,
`status`, `protocol_mismatch`. `in_sync` is true when both gateway revs
match DevPi's.

`lq_snapshot`, to a requesting client or to the room when the unassigned
list or the roster changes:

```json
{"link":{"...same shape as lq_link..."},
 "devpi":{"state_rev":42,"roster_rev":7,"phase":1},
 "cups":["...exactly 20 entries, cup 1..20, same shape as lq_update; mac null and online false for an empty slot..."],
 "unassigned":[{"mac":"A0:B7:65:12:34:99","last_seen":"2027-05-01T21:14:07Z"}]}
```

`horse` and `scratched` per cup come from the last state DevPi set, not from
the gateway.

## Python API (`la_quiniela.get_bridge()`, all cup IDs 1-based)

| Call | Does |
| --- | --- |
| `set_state(phase, horses, scratched) -> rev` | `horses` and `scratched` are 20-item lists, position 0 = cup 1. Validated exactly as the gateway does (`ValueError` on bad input). Bumps `state_rev`, persists, updates `cups.horse`, sends if the port is open, emits `lq_update` for every cup whose horse or flag changed. Identical values are a no-op returning the current rev. |
| `set_roster(macs) -> rev` | 20-item list, `None` or `""` for an empty slot. Rejects bad MACs, duplicates and `FF:FF:FF:FF:FF:FF`. Bumps `roster_rev`, persists, rewrites `cups.cup_id`, sends roster then state, emits a fresh `lq_snapshot`. |
| `adopt_roster() -> rev` | Builds a roster from the cup numbers mirrored from the gateway and calls `set_roster`. |
| `reset_link(reason) -> dict` | Forgets the roster and the state, clears `cups.cup_id` and `cups.horse`, deletes simulated cup rows, keeps the history, sends the gateway nothing. Returns the new revs and how many rows were deleted. Also bumps `reset_count` (in-memory, `get_snapshot()["devpi"]["reset_count"]`), which the betting board watches to clear the closing time. |
| `get_snapshot() -> dict` | The `lq_snapshot` payload. |
| `set_gateway_debug(on) -> bool` | Sends `{"t":"debug","on":...}`; True if it went out. |

`Phase` in `la_quiniela/protocol.py` is an `IntEnum` matching `DdmRaceState`
in `ddm_common.h`; a test parses the header so the two cannot drift.

## HTTP

- `GET /api/lq/snapshot`: always on, read-only, returns `get_snapshot()`.
- Only with `LQ_DEV_ENDPOINTS` true (otherwise 404):
  - `POST /api/lq/dev/state` body `{"phase":1,"horses":[...20],"scratched":[...20]}`
  - `POST /api/lq/dev/roster` body `{"macs":[...20]}`
  - `POST /api/lq/dev/roster/adopt`
  - `POST /api/lq/dev/debug` body `{"on":true}`
  - `POST /api/lq/dev/reset` body `{"reason":"..."}` (optional), returns
    `{"success":true,"state_rev":N,"roster_rev":N,"cups_dropped":N}`

  The others return `{"success":true,"rev":N}` or a 400 with the validation
  message.

## Tests

```
cd pi5
python -m la_quiniela.test_smoke
```

No hardware and no real port: a fake serial port feeds the gateway's lines
and captures what the bridge writes, a stub SocketIO records every emit, and
a fake clock drives the timers. Byte-exact roster and state lines are checked
against the README examples.

## Betting board (`/api/quiniela`)

The splash display's TV board (`splash_display/templates/splash/quiniela_live.html`
and `static/js/quiniela_board.js`) renders one JSON model: the pot, the three
prizes, tokens per horse, the horses' names, the last few drops, the race
state and whether the link is up. That model is built here, in
`la_quiniela/betting.py`, from the bridge's own picture (`get_snapshot()`)
plus the operator's names (`la_quiniela/horses.py`), and served by
`la_quiniela/board.py` at the three paths the page expects. The splash
display is an HTTP client of these routes and re-serves them on its own
origin, so the page itself never changed. Nothing in the board reads the
serial port.

### How La Quiniela pays

- A token is $1 and one raffle ticket. After the race one token is drawn from
  the WIN cup, one from the PLACE cup and one from the SHOW cup, and each
  drawn token's owner takes that cup's **whole** prize. Nobody splits
  anything and there are no odds; the only number per horse is how many
  tokens are in its cup.
- The prizes are fixed fractions of the pot, `LQ_SPLIT_WIN` / `LQ_SPLIT_PLACE`
  / `LQ_SPLIT_SHOW` in `pi5/config.py` (0.60 / 0.25 / 0.15; they should sum to
  1 and the board warns once if they do not).
- Whole dollars, always summing to the pot: `place = round_half_up(pot *
  LQ_SPLIT_PLACE)`, `show = round_half_up(pot * LQ_SPLIT_SHOW)`, `win = pot -
  place - show`. Half up, never Python's `round()` (banker's rounding, which
  calls 38.5 a 38): pot $154 gives place 38.50 -> **$39**, show 23.10 ->
  **$23**, win **$92**; pot $0 gives 0 / 0 / 0; pot $1 gives win $1, place $0,
  show $0. `round_half_up()` and `prizes_for(pot, split)` in `betting.py`.

### Two kinds of scratch

1. **With a replacement** (`POST /api/quiniela/scratch {"horse": 9,
   "replacement": "Epic Ride"}`): the number stays live and the cup keeps
   counting; the old name becomes `horses["9"].replaced`, the new one `name`,
   and the pair appears in `scratches`. Nothing goes to the gateway.
2. **No replacement** (`{"horse": 9}` alone): the bridge's existing scratch,
   `set_state()` with the scratched flag on the cup carrying horse 9, so the
   cup shows SCRATCHED and `horses["9"].scratched` is true as before. **Its
   tokens are excluded from the pot and the prizes**; Joey refunds them by
   hand. `total_tokens` still counts every cup. This pot rule is an
   assumption about how a no-replacement scratch is settled; if the tokens
   should stay in the pot instead, it is the one `if not h["scratched"]` in
   `apply_snapshot()`.

Changing names or scratches of either kind never produces an event on the
ticker and never resets the baseline: events only ever come from token
diffs, and names live outside the bridge's revisions.

### Admin page

`GET /quiniela/admin` (on pi5, port 5000, so `http://joeydevpi.local:5000/quiniela/admin`)
is one plain page for a phone: the 20 names in a textarea (`1. NAME` lines,
Save puts them back as text), a row per horse with a replacement box, a
Scratch button, a "No replacement" checkbox for the gateway kind and Undo on
any horse that is scratched or replaced, the closing time (a date-time picker
plus +15 / +30 / +60 min and Clear), and a status line (race state, pot, the
three prizes, link, cups online) refreshed every 5 s. Race state stays on
`POST /api/quiniela/cmd`; the page has no buttons for it.

### Ports

pi5 (this app) listens on **5000**, the splash display on **5001**. They can
run on the same DevPi; the splash reaches pi5 at its `config.PI5_URL`
(default `http://joeydevpi.local:5000`; `http://localhost:5000` also works
when both share DevPi).
Only pi5 opens the gateway's USB port (`LQ_SERIAL_PORT`); the splash display
holds no serial port at all. The port is opened `exclusive`, so a second
owner would fail to open it in any case.

### Routes

The blueprint `quiniela_board_bp` has no URL prefix, so the paths are exactly:

| Route | Returns |
| --- | --- |
| `GET /api/quiniela` | The model JSON below, `Cache-Control: no-store`. |
| `GET /api/quiniela/stream` | Server-sent events, `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`. The first chunk is `data: <model>\n\n`; every published model follows as another `data:` chunk; after 5 s of silence a `: heartbeat` comment plus `event: ping\ndata: {"ts":<unix>}\n\n`. Each subscriber has a 32-deep queue and the oldest model is dropped when it is full. |
| `POST /api/quiniela/cmd` | Body `{"cmd": "state 1"}`; see the translation table. |
| `GET /api/quiniela/horses` | `{"1": {"name": "Encino", "replaced": null}, ...}`, names **as typed** (the model upper-cases them). |
| `PUT /api/quiniela/horses` | Body `{"text": "1. Dornoch\n2. Sierra Leone\n..."}` or the GET shape (`name` required, `replaced` optional per entry). Only the horses given are touched. 400 with the parse message. Returns `{"ok": true, "names_rev": N, "horses": {...}}`. Text rules: one name per line in post order; a leading `7.` / `#7` / `7)` / `7:` names that horse instead, a bare `7` clears it, a blank line leaves it alone; a bare `7 Name` (number, space, name) is a prefix only when every non-blank line is numbered, so in a plain list `8 Belles` is a name. |
| `POST /api/quiniela/scratch` | `{"horse": 9, "replacement": "Epic Ride"}` -> kind 1, `{"ok": true, "kind": "replacement", "horse": 9, "was": "Encino", "now": "Epic Ride", "names_rev": N}`. `{"horse": 9}` (or an empty / null replacement) -> kind 2 through `set_state()`, `{"ok": true, "kind": "gateway", "horse": 9, "cup": 9, "scratched": true, "rev": R, "gateway_online": bool}`; 400 `horse 9 is not on any cup`, 503 `bridge not initialised`. |
| `POST /api/quiniela/unscratch` | `{"horse": 9}` reverses either kind: a replacement is undone first if there is one, else the gateway flag on the horse's cup is cleared; 400 `horse 9 is not scratched` when neither applies. |
| `PUT /api/quiniela/closes_at` | `{"at": <unix time>}`, `{"in_minutes": 30}` (from the server's clock) or `{"at": null}` -> `{"ok": true, "closes_at": ...}`. |
| `GET /quiniela/admin` | The admin page above. |

The five new routes refresh the model synchronously before answering, so a
`GET /api/quiniela` right after one already shows the change (the store's
`on_change` also wakes the board thread). Errors are `{"ok": false, "error":
...}`.

```
curl -X PUT  localhost:5000/api/quiniela/horses    -H 'Content-Type: application/json' -d '{"text": "1. Dornoch\n2. Sierra Leone\n9. Encino"}'
curl -X POST localhost:5000/api/quiniela/scratch   -H 'Content-Type: application/json' -d '{"horse": 9, "replacement": "Epic Ride"}'
curl -X POST localhost:5000/api/quiniela/scratch   -H 'Content-Type: application/json' -d '{"horse": 9}'
curl -X POST localhost:5000/api/quiniela/unscratch -H 'Content-Type: application/json' -d '{"horse": 9}'
curl -X PUT  localhost:5000/api/quiniela/closes_at -H 'Content-Type: application/json' -d '{"in_minutes": 30}'
curl -X PUT  localhost:5000/api/quiniela/closes_at -H 'Content-Type: application/json' -d '{"at": null}'
```

They are deliberately not under `la_quiniela_bp`, whose `after_request`
rewrites `Cache-Control`.

### The model

```json
{"link_ok": true,
 "race_state": 1, "race_state_name": "BETTING_OPEN",
 "token_value": 1.0, "pot": 33.0, "total_tokens": 33,
 "horses": {"1": {"tokens": 0, "share": 0.0, "scratched": false, "online": false, "cup": null,
                  "name": "", "replaced": null},
            "7": {"tokens": 23, "share": 0.697, "scratched": false, "online": true, "cup": 1,
                  "name": "HONOR MARIE", "replaced": null},
            "9": {"tokens": 10, "share": 0.303, "scratched": false, "online": true, "cup": 9,
                  "name": "EPIC RIDE", "replaced": "ENCINO"},
            "...": "...20 entries, keys \"1\"..\"20\"..."},
 "leader": 7,
 "events": [{"horse": 7, "delta": 1, "ts": 1777662847.2}],
 "updated": 1777662847.2,
 "board_states": [1, 2, 3, 4],
 "now": 1777662850.4,
 "closes_at": 1777664400.0,
 "prizes": {"win": 20, "place": 8, "show": 5},
 "split": {"win": 0.6, "place": 0.25, "show": 0.15},
 "chyron": ["TOTALS BASED ON CHEAP CHINESE ELECTRONICS \u00b7 FINAL RESULTS HAND COUNTED",
            "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS"],
 "names_rev": 4,
 "scratches": [{"horse": 9, "was": "ENCINO", "now": "EPIC RIDE"}]}
```

The first eleven keys are the original contract and are unchanged; the rest
are additive (the splash relays the whole model untouched). From the snapshot
and the store as follows:

- `link_ok` = `link.port_open and link.gateway_online`.
- `race_state` = `devpi.phase` (DevPi's own phase, the one `set_state()`
  holds), `race_state_name` its `Phase` name, `STATE_<n>` for a value the
  enum does not know.
- `horses[n]`: the cup whose `horse` is `n` supplies `tokens` (`count`,
  `None` until the first telemetry line reads as 0, negatives clamp to 0),
  `scratched`, `online` and `cup`. **`cup` is the 1-based cup number**, as
  everywhere on pi5; the old splash model carried the gateway's 0-based slot
  there, and the page only tests it for `null`. When two cups claim one horse
  the lowest cup number wins and one WARNING is logged per pair. `share` =
  `round(tokens / total, 4)`, 0.0 with no tokens.
- `pot` = `round(tokens * token_value, 2)` over the horses whose cup is **not**
  scratched at the gateway (kind 2 above); `total_tokens` is every cup.
- `leader`: strictly the most tokens, the lowest horse number on a tie, `null`
  when every count is 0. Scratched horses are not excluded.
- `events`: the last eight token changes, newest first. The first snapshot a
  board digests is a baseline, not a bet, so a restart never invents drops:
  the bridge seeds each cup's count from the `cups` table before the gateway
  is heard, and only a count that moved while pi5 was down shows up as a
  (late but real) drop. A reset-shaped transition is a baseline too: when
  `devpi.roster_rev` moves (`reset_link()`, `set_roster()`, `adopt_roster()`)
  the events are cleared and nothing is diffed, so a pre-party
  `POST /api/lq/dev/reset` leaves no `-50` ghosts on the ticker (found on the
  bench, 2026-09-25); and a horse moved to another cup, or unassigned, gets no
  event for the count that came with the cup. Only a count that changed on the
  same cup under the same roster is a bet or a removal, and that cuts both
  ways: cups emptied or re-tared between two races on one evening (same cups,
  same roster) are removals, and their `-N` chips stay on the ticker into the
  next BETTING_OPEN until eight newer bets push them off. Before a second
  race, reset (`POST /api/lq/dev/reset`, which needs `LQ_DEV_ENDPOINTS`) or
  restart pi5 once the zeros have been heard. The log records a reset as a
  baseline (see the log below). The one exception is a fresh or deleted
  `la_subasta.db` started with tokens already in the cups: the baseline then
  holds zero for every cup, and the first telemetry shows those counts once
  as drops on the ticker.
- `updated`: wall time of the last change. A snapshot that changes nothing
  publishes nothing and leaves it alone.
- `board_states`: the race states in which the page takes over the TV.
- `now`: the server's clock, stamped when the model is serialised (`GET
  /api/quiniela`, every SSE chunk), never stored and never part of the
  changed-comparison, so the stream stays quiet between real changes. The
  page counts down `closes_at` against it rather than the phone's clock.
- `closes_at`: unix time or `null`, from `PUT /api/quiniela/closes_at`.
  **A `reset_link()` clears it** (the board watches `devpi.reset_count`);
  names survive a reset.
- `prizes`: `{"win", "place", "show"}` whole dollars summing to `pot`, by the
  rule above. `split`: the three fractions from config.
- `chyron`: `LQ_CHYRON_LINES` from config, for the crawl along the bottom.
- `names_rev`: the names store's revision, bumped by any name change, any
  replacement scratch and its undo, persisted in `lq_board`; a change
  publishes a model (it differs) but yields no events. A gateway
  (no-replacement) scratch or unscratch is the bridge's state, versioned by
  its state rev, and leaves `names_rev` alone (the model still publishes:
  `scratched`, `pot` and `prizes` moved).
- `horses[n].name` / `replaced`: the store's names **upper-cased** (`""` /
  `null` when unset); `scratches`: `[{"horse", "was", "now"}]` for every horse
  with a replacement, upper-cased too. `replaced` is `""` (not `null`) when
  the scratched horse had no name yet: the `""` is what marks the number as
  replaced so undo still works, and `scratches` then carries `"was": ""`
  (the board's chyron prints `UNNAMED` there). A second replacement of the
  same number keeps the horse actually scratched in `replaced` (the first
  replacement never ran and is dropped), so undo goes straight back to it.
- `share` and `leader` stay as they were; nothing new depends on `share`.

### `POST /api/quiniela/cmd`

The splash's whitelist of gateway text commands is kept, so the operator's
habits and the splash's tests carry over, but nothing is ever written to
the port as text: the bridge's revision model is the source of truth, and a
hand-typed line would be overwritten by the next hello answer. Each command
is translated onto the bridge's own API. **Cup numbers are 1-based**, as
everywhere on pi5 (the gateway's own `horse` command took a 0-based slot).

| Command | Does | Answer |
| --- | --- | --- |
| `state N` (0..6) | `set_state(N, horses, scratched)` with the current lists from the snapshot | `{"ok":true,"rev":R,"phase":N,"gateway_online":bool}` |
| `horse C H` (cup 1..20, horse 0..20) | `set_state(phase, horses with cup C = H, scratched)` | as above plus `"cup":C,"horse":H` |
| `scratch C F` (cup 1..20, F 0 or 1) | `set_state(phase, horses, scratched with cup C = F)` | as above plus `"cup":C,"scratched":bool` |
| `roster` | nothing written | `{"ok":true,"roster":[20 MACs or null, cup 1..20],"roster_rev":R,"has_roster":bool}` |
| `demo` | refused, 400 | `demo is not routed through pi5: the bridge speaks the JSON line protocol, and every state line turns demo off` |
| `json` | refused, 400 | `json is not routed through pi5: the bridge already reads the gateway's protocol, and the up state line would exceed its 1024-byte cap` |

A state change is applied even when the gateway is offline: DevPi holds it
and re-sends it on the next hello or status line, and `gateway_online` in the
answer says which of the two happened. Errors are `{"ok":false,"error":...}`:
400 for a command that is not a string, empty, longer than 200 characters,
not a single line, or whose first word is not one of `state horse scratch
demo roster json` (case matters); 400 `usage: state 0-6`, `usage: horse <cup
1-20> <horse 0-20>` or `usage: scratch <cup 1-20> <0|1>` for bad arguments;
400 with the message from `validate_state` if the bridge rejects the state;
503 `bridge not initialised` when there is no bridge.

### Configuration

Three more keys in `pi5/config.py`, with the names the splash display used,
each overridable by `DDM_<KEY>` in the environment or `pi5/.env`:

| Key | Default | Meaning |
| --- | --- | --- |
| `TOKEN_VALUE` | `1.00` | Dollars per token, for the board's POT (`DDM_TOKEN_VALUE`, a float) |
| `QUINIELA_LOG` | `True` | Write the JSONL log below (`DDM_QUINIELA_LOG`: 1/true/yes/on or 0/false/no/off) |
| `QUINIELA_BOARD_STATES` | `[1, 2, 3, 4]` | Race states in which the splash board owns the TV (`DDM_QUINIELA_BOARD_STATES`, a comma list such as `1,2,3,4`) |
| `LQ_SPLIT_WIN` | `0.60` | The WIN prize's share of the pot; it takes the remainder after PLACE and SHOW (`DDM_LQ_SPLIT_WIN`) |
| `LQ_SPLIT_PLACE` | `0.25` | The PLACE prize's share, rounded half up to whole dollars (`DDM_LQ_SPLIT_PLACE`) |
| `LQ_SPLIT_SHOW` | `0.15` | The SHOW prize's share, likewise (`DDM_LQ_SPLIT_SHOW`) |
| `LQ_CHYRON_LINES` | two lines | What crawls along the bottom of the board (`DDM_LQ_CHYRON_LINES`, lines separated by `\|`) |

A value that does not parse is logged and the default kept; a `config.py`
without these keys works unchanged. `load_board_settings()` in `betting.py`
resolves them, and warns once when the three splits do not sum to 1 within
0.001.

### How the board follows the bridge

The bridge has a small in-process hook: `add_listener(fn)` registers a
no-argument callable that is called after every `lq_update`, `lq_snapshot`
and `lq_link` emit and at the end of `set_state()` and `reset_link()` (a
phase-only `set_state` emits nothing, so the emits alone would miss it).
Listeners run on the reader thread with the bridge's lock held, so they may
only set an `Event` or `put_nowait()`; the board's `wake()` does exactly
that. A listener that raises is logged at WARNING and dropped.

The board's daemon thread, `lq-board`, waits on that event with a one second
timeout and calls `refresh()` on every wake, so the model also follows a
gateway that has simply gone quiet (the bridge's offline timer emits, but the
timeout is the backstop). `refresh()` takes `get_snapshot()` without holding
the board's own lock and only then applies it under that lock, so the lock
order is always bridge then board and nothing can deadlock against the reader
thread. `main.py` calls `init_board()` at import and `start_board()` from its
`__main__` block only, after `start_la_quiniela()`; the thread runs even when
the bridge has no port, so the routes answer with `link_ok` false rather than
a stale picture.

### The log

With `QUINIELA_LOG` on, every token, scratch or race-state change appends one
compact JSON line to `pi5/data/quiniela_YYYY-MM-DD.jsonl` (local date,
git-ignored):

```json
{"ts":1777662847.2,"race_state":1,"changes":[{"horse":7,"tokens":[23,24]},{"horse":7,"scratched":[false,true]},{"race_state":[1,2]}],"total_tokens":24}
```

An `online` or `link_ok` flip alone writes nothing. Two marks keep the log
honest about what was not a bet: a reset-shaped transition (`roster_rev`
moved, see `events` above) writes a record with `"baseline": true`, with an
empty `changes` list when nothing else moved, so a roster change mid-race
leaves a trace; and a count that came with a cup move under the same roster
(the horse re-assigned or unassigned) carries the move in its change, e.g.
`{"horse":3,"tokens":[0,51],"cup":[null,1]}`. The first write failure
logs one WARNING and disables the log for the rest of the process; the model
is unaffected.

### Tests

```
cd pi5
python -m la_quiniela.test_betting
```

Hand-built snapshots and a real `LqBridge` over the smoke test's fake port
exercise the model, the log, the settings, the listener hook, the thread,
the command translation (byte-exact against `protocol.build_state_line`),
the prize rounding, both kinds of scratch, the names text parser, the
closing time (and its reset), the `now` stamp, the two tables and every
route on a Flask test app. `test_smoke` also checks that importing `main.py`
starts no `lq-board` thread and registers the routes, the admin page
included.

# La Quiniela bridge (`pi5/la_quiniela/`)

Race night: see `RACE_NIGHT.md` at the repo root.

The DevPi end of the La Quiniela gateway link. A daemon thread inside the
Flask app talks to the ESP32 gateway over USB serial:

- **reads** the gateway's JSON lines, keeps a live picture of every cup it
  has heard (by MAC, with the horse number the cup says it is), writes the
  `lq_cups`, `telemetry` and `events` tables, and emits SocketIO events for
  displays;
- **writes** the `state` line down (race state, scratched horses, renumber
  pairs, results), answers the gateway's `hello` with it, and re-sends it
  whenever a `status` line shows the gateway holding another revision;
- **never takes Flask down**: every serial failure is caught, logged and
  retried.

The line protocol is `firmware/quiniela/README.md`, section "Serial line
protocol" (line protocol v2, with ESP-NOW protocol v2). That document wins on
any difference.

## The one identity rule

Since protocol v2 (2026-09-27) **a cup is its MAC and a horse is the number
the cup reports.** The cup owns its horse number: it is set on the cup (the
touch menu's `HORSE`, or `n <1-24|0>` on its serial port), saved in the cup's
NVS, and carried in every packet the cup sends. DevPi learns which horses
have cups by listening. There are no cup IDs, slots, rosters or adoption
anywhere on DevPi: everything the bridge stores, emits or serves is keyed by
MAC (which cup) and horse (what it says it is), and everything DevPi sends
down (a scratch, a renumber, the results) is keyed by horse number. The
gateway keeps a table of the cups it hears, but only its contents (MAC,
horse, tokens, signal, age) ever reach DevPi; its indexing never does.

`la_quiniela/protocol.py` holds the two readers used everywhere a line is
parsed: `normalize_mac()` (upper-case, colon-separated, or `None`) and
`parse_horse()` (1..`MAX_HORSE`, anything else reads as 0, "none").

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
   the gateway's `hello` is answered with the state line DevPi holds.

With no port configured the bridge idles and logs once how to set it. With
`LQ_BRIDGE_ENABLED = False`, or without pyserial, it logs one line and the
rest of the app, La Subasta included, runs exactly as before.

The port is opened with `exclusive=True`, so a second copy of the app cannot
open the same port. A missing or vanished port is retried every 5 s forever,
logged at most once a minute.

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
| `LQ_CUP_OFFLINE_S` | `6` | No packet from a cup for this long = cup offline |
| `LQ_GATEWAY_OFFLINE_S` | `12` | No line at all for this long = gateway offline |
| `LQ_DEAF_REOPEN_S` | `20` | Port open but no valid line for this long = close it and open it again |
| `LQ_REOPEN_MIN_GAP_S` | `30` | Never reopen more often than this |

`LQ_DEV_ENDPOINTS` is gone with the dev routes it gated (below); a value left
in `pi5/config.py` or `pi5/.env` is ignored.

## Database

The tables live in the app's one SQLite database, the file La Subasta uses
(`pi5/data/la_subasta.db`), created idempotently at startup. A table of the
same name with a different shape is reported and never altered; the bridge
then refuses to start. The one exception is the protocol v1 shape, which
`init_schema()` migrates (below). Timestamps are UTC ISO 8601 with a `Z`.

| Table | Rows |
| --- | --- |
| `lq_cups` | the cup cache: one per MAC ever heard: `mac` (PK), `horse` (the number it last claimed, 0 = none), `last_seen`, `rssi`, `up_rssi`, `last_count`, `last_raw`, `online` |
| `telemetry` | append only: `ts`, `mac`, `horse`, `raw_weight`, `token_count`, `seq`, `dropped`, `rssi`, `up_rssi`, `reason` (`change` or `heartbeat`) |
| `events` | audit log: `ts`, `type`, `horse` (nullable), `detail` (JSON) |
| `lq_link_state` | one row: `state_rev`, `state_json` (`{"phase","scratched","renum","results"}`); how the bridge answers a `hello` after a restart |
| `lq_horses` | the betting board's: `horse` (PK, 1..24: 1..20 the field, 21..24 the also-eligibles), `name` (as typed), `replaced` (legacy, from the name-swap replacement of 275a64f; kept NULL and never read except for one WARNING at load, `legacy name-swap replacement on horse N ignored; scratch it again with a number`) |
| `lq_scratches` | one row per scratch: `was` (PK, 1..24, the horse that left the field), `now` (1..24, the horse standing in for it, on the same cup; NULL for a no-replacement scratch, whose tokens are refunded). A table created with `now NOT NULL` (c70d894) is rebuilt by `init_schema()` (`_migrate_lq_scratches`), rows kept |
| `lq_board` | one row: `names_rev`, `closes_at` (unix time or NULL) |
| `lq_closing` | one row: `closing`, the board's figures at the post as JSON (the model's `closing`, below), or NULL while there are none. A database from before it gains it at start (`CREATE TABLE IF NOT EXISTS`); nothing else changes |

**The v1 tables are migrated on start** (`_migrate_v2`, each step in its own
transaction, only when the old shape is found, idempotent): the v1 `cups`
table (slots: `mac` plus `cup_id`) is dropped, since its rows were per slot
and `lq_cups` fills from the air within seconds; `telemetry` and `events` are
rebuilt with a `horse` column in place of `cup_id`, rows kept with `horse`
NULL (the old slot numbers were not horses); `lq_link_state` loses
`roster_rev` and `roster_json`, keeping `state_rev` and `state_json`. A v1
`state_json` (`horses` and `scratched` keyed by slot) is read for its phase
and nothing else: scratched, renum and results start empty.

`lq_horses` was first created with `CHECK (horse BETWEEN 1 AND 20)` and is
live on DevPi that way. SQLite cannot alter a CHECK, so `init_schema()`
rebuilds a table whose CREATE statement still says `1 AND 20` (create
`lq_horses_new` with the 1..24 CHECK, copy the rows, drop, rename) in one
transaction before the `CREATE TABLE IF NOT EXISTS` script; a second start
finds the 1..24 CHECK and does nothing, a stale `lq_horses_new` from an
interrupted run is dropped first, and `check_shape()` (column names only)
passes either way.

Telemetry arrives about ten lines a second and DevPi runs on an SD card, so
the database is written only when a token count changes, when the heartbeat
interval passes, when a cup comes online or goes offline, when a cup's horse
changes, and for events.

Event types: `cup_online`, `cup_offline`, `cup_horse` (`{"mac","from","to"}`,
also for a cup first heard with a horse, `from` 0), `cups_forgotten`,
`gateway_hello`, `gateway_reboot`, `gateway_err`, `state_set` (with what
changed), `bridge_reopen`. The `horse` column of a cup event is the horse the
cup reports (NULL for none).

## Who owns horse numbers: the cup

- The cup: `HORSE` in its touch menu (hold the screen, `HORSE`, tap the top
  half of the number to go up and the bottom half to go down, `NONE` or
  1..24, `SET`), or `n <1-24|0>` on its serial port. Saved in NVS, shown big
  on the screen, sent in every packet. The picker is locked while the race
  is in BETTING_OPEN, FINAL_CALL, AT_THE_POST or RUNNING (`HORSE (LOCKED)`)
  and free in PRE_RACE, WINNER and AFTER_PARTY. A cup with no horse shows
  `NO HORSE` and, smaller, `HOLD TO SET`.
- DevPi listens. Every `telem` line says which MAC claims which horse; the
  bridge's cache follows, and the betting board's model puts each horse's
  tokens under the cup that claims it.
- **Two cups claiming one horse is a conflict**, reported, not resolved:
  both MACs are listed, the one heard most recently (online first) supplies
  the count, one WARNING is logged per pair, and the admin page shows
  `⚠ 2 CUPS` on that row until one of them is set to something else or goes
  quiet. A cup that has gone offline while another took its horse (a spare
  swapped in) is not a conflict.
- A scratch, a renumber and the results go down **keyed by horse number**,
  in the one `state` line (below); the gateway broadcasts them and every cup
  applies what concerns the number it holds. Nothing on DevPi ever addresses
  a cup.

### The state DevPi holds

`state_rev`, `phase`, `scratched` (horses scratched with no replacement),
`renum` (`[from, to]` pairs: a cup whose horse is `from` becomes `to`, saves
it and reports it from then on) and `results` (WIN, PLACE, SHOW horse
numbers, 0 = not yet). A fresh bridge starts at rev 1 with PRE_RACE and
nothing else, and persists that, so a `hello` always has a line to get. Any
part changes through `set_state()` (the others stay), validated exactly as
the gateway validates the line, and every change bumps the rev, persists,
sends the line if the port is open, and logs a `state_set` event with what
changed. A `hello` is answered with the current line; a `status` whose
`state_rev` differs gets a re-send (at most one every 2 s). `in_sync` in the
link means the gateway reports DevPi's rev.

### The cup cache

`lq_cups` remembers every cup DevPi has heard, with the horse it last
claimed and when it was last seen, across restarts. It is what lets the
admin page tell `● offline` (seen before, silent now) from `○ no cup` (never
heard) and it decides nothing: a cup that is still talking is back in the
picture within one packet. `forget_cups(prefix)` / `POST /api/lq/cups/forget`
drops entries (one MAC, a prefix, or all), in memory and in the table, with
a `cups_forgotten` event; the only reason to do so is a cup that went home
and would otherwise sit on the page as offline.

**The simulator's cups drop out by themselves.** Every MAC the cup
simulator invents starts `02:DD:4D:`, its gateway included
(`02:DD:4D:FF:FF:FF`), and nothing real uses that prefix. When a `hello`
arrives from a gateway whose MAC does not start that way, the bridge drops
every `02:DD:4D:` cup from the cache before answering the hello with its
state, as it answers every hello. There is no longer a "reset the link":
DevPi holds nothing about cups that a real gateway could be misled by (its
state line names horses, not cups), so after a simulator session the real
gateway is simply plugged in; the gateway itself keeps whatever state it was
last sent until it is power-cycled or DevPi sends the next line, which the
hello answer is.

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
| `answered the hello with state rev N` | DevPi sent the gateway its state line. |
| `dropped N simulated cup(s) from the cache: a real gateway said hello` | The cup simulator's cups left the cache (above). |
| `gateway online` | Lines are arriving. |
| `cup A0:B7:65:12:34:56 is horse 7 (was 3)` | A cup reported a horse it had not reported before (the first report of a cup with a horse says `is horse 7` alone). |
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
until the app was restarted. The reader now takes bounded chunks (lines up to
4096 bytes, room for the cup table in a `status`) and splits lines itself, a
newline always returns it to a clean start of line, and the watchdog is the
backstop for anything else.

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

`lq_update`, one cup, on a count change, a heartbeat, a cup coming online or
going offline, or a change of the horse it claims:

```json
{"mac":"A0:B7:65:12:34:56","horse":7,"count":14,"raw":812345,"rssi":-64,"up":-61,"drop":2,"seq":9021,"online":true,"hello":false,"last_seen":"2027-05-01T21:14:07Z"}
```

`hello` is true while the cup is still broadcasting because it has not heard
a state packet yet (its `telem` lines carry `"hello":1`).

`lq_link`, on every change of port-open, gateway-online or in-sync, plus a
`hello` (`boot`) and a protocol mismatch:

```json
{"port_open":true,"gateway_online":true,"in_sync":true,"reason":"status","gateway_mac":"24:6F:28:AA:BB:CC","phase":1,"state_rev":42,"cups_heard":18,"rejects":0,"up_s":5230,"thread_alive":true,"last_line_age_s":0.4,"lines_ok":9021,"lines_bad":12,"bytes_rx":1480233,"reopens":0}
```

`reason` is one of `boot`, `reboot`, `online`, `offline`, `port_open`,
`port_closed`, `status`, `protocol_mismatch`. `in_sync` is true when the
gateway's `state_rev` matches DevPi's. `cups_heard` is the gateway's count of
cups it heard within its 3 s window, from its `status` table.

`lq_snapshot`, to a requesting client, and to the room whenever the per-horse
picture moved (a cup came online, went offline or changed its horse, cups
were forgotten, the state was set, or a `status` table brought a cup up to
date):

```json
{"link":{"...same shape as lq_link..."},
 "devpi":{"state_rev":42,"phase":1,"scratched":[20],"renum":[[9,22]],"results":[0,0,0]},
 "cups":["...one entry per cup in the cache, the lq_update shape, sorted by MAC..."]}
```

`devpi` is the state DevPi holds, keyed by horse (above). Two cups claiming
one horse are both in `cups`; the betting board decides what to show.

## Python API (`la_quiniela.get_bridge()`)

| Call | Does |
| --- | --- |
| `set_state(phase=None, scratched=None, renum=None, results=None) -> rev` | Change any part of the state, the others kept. Validated exactly as the gateway does (`ValueError` on bad input: phase 0..6, scratched horses 1..24, at most 4 renum pairs with `from != to` and no `from` twice, exactly 3 results 0..24). Bumps `state_rev`, persists, logs `state_set`, sends if the port is open, emits `lq_snapshot`. Identical values are a no-op returning the current rev. |
| `forget_cups(mac_prefix=None) -> int` | Drop cups from the cache (all, or those whose MAC starts with the prefix), in memory and in `lq_cups`; a `cups_forgotten` event and a fresh `lq_snapshot`. Returns how many went. Decides nothing (above). |
| `get_snapshot() -> dict` | The `lq_snapshot` payload. |
| `set_gateway_debug(on) -> bool` | Sends `{"t":"debug","on":...}`; True if it went out. |
| `add_listener(fn)` | `fn()` is called after every emit and at the end of `set_state()` (the betting board's hook, below). |

`state_rev`, `phase`, `scratched`, `renum` and `results` are readable
attributes. `Phase` in `la_quiniela/protocol.py` is an `IntEnum` matching
`DdmRaceState` in `ddm_common.h`; `test_smoke` parses the header so the two
cannot drift, and pins `MAX_HORSE`, `RENUM_SLOTS`, `RESULT_SLOTS` and the
protocol version to the header's `DDM_MAX_HORSE`, `DDM_RENUM_SLOTS`,
`DDM_RESULT_SLOTS` and `DDM_PROTO_VERSION`.

## HTTP

- `GET /api/lq/snapshot`: read-only, returns `get_snapshot()`.
- `POST /api/lq/debug` body `{"on": true}`: the gateway's human-readable
  serial output, a bench toggle. Returns `{"success": true, "sent": bool}`.
- `POST /api/lq/cups/forget` body `{"mac": "A0:..."}` for one cup,
  `{"prefix": "02:DD:4D:"}` for a family, `{}` for all. Returns
  `{"success": true, "forgotten": N}`.

None of them needs a flag. The v1 dev routes (`/api/lq/dev/state`,
`/dev/roster`, `/dev/roster/adopt`, `/dev/roster/clear`, `/dev/reset`,
`/dev/debug`) are gone and answer 404; the race state is set through the
betting board's `POST /api/quiniela/cmd`, scratches through
`POST /api/quiniela/scratch`, and there is nothing to adopt or clear.

## Tests

```
cd pi5
python -m la_quiniela.test_smoke
```

No hardware and no real port: a fake serial port feeds the gateway's lines
and captures what the bridge writes, a stub SocketIO records every emit, and
a fake clock drives the timers. The state line is checked byte-exact against
the README examples; the v1 database shape is built by hand and migrated; the
hello answer, the status reconcile, the cup table backstop, a cup changing
its horse, two cups claiming one horse, offline detection, the watchdog and
the HTTP routes (the dev paths 404) are all exercised.

## Betting board (`/api/quiniela`)

The splash display's TV board (`splash_display/templates/splash/quiniela_live.html`
and `static/js/quiniela_board.js`) renders one JSON model: the pot, the three
prizes, tokens per horse, the horses' names, the last few drops, the race
state and whether the link is up. That model is built here, in
`la_quiniela/betting.py`, from the bridge's own picture (`get_snapshot()`)
plus the operator's names and scratches (`la_quiniela/horses.py`) and the
dashboard's results, and served by `la_quiniela/board.py` at the three paths
the page expects. The splash display is an HTTP client of these routes and
re-serves them on its own origin, so the page itself never changed (it only
tests a horse's `cup` for `null`, which is why the change of `cup` from a cup
number to a MAC needed nothing there). Nothing in the board reads the serial
port.

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

### Horses 1..24

Horses are numbers 1..24 (`protocol.MAX_HORSE`, which `test_smoke` pins to
`DDM_MAX_HORSE` in `firmware/quiniela/ddm_common.h`): 1..20 are the field,
21..24 the also-eligibles, whose names can be entered ahead of time but who
are not in the field until they replace someone. The model's `horses` map is
keyed `"1"`..`"24"` and every entry carries `in_field`. A cup can be set to
any of the 24; a cup set to an also-eligible that is standing in for nobody
is listed on that horse (its tokens counted in `total_tokens`) but the horse
is not in the field.

### Two kinds of scratch

1. **With a replacement: the renumber rule.** At Churchill an also-eligible
   that draws in keeps its own program number (2026: The Puma, #9, scratched
   and Ocelli ran as #22, not as #9). `POST /api/quiniela/scratch {"horse":
   9, "replacement": {"number": 22, "name": "Ocelli"}}` therefore changes the
   cup's horse number, through the cup itself: the store records `{was: 9,
   now: 22}` in `lq_scratches`, gives 22 the name if one was sent (else 22
   keeps its stored name), bumps `names_rev` once, and the board's
   `refresh()` puts the pair `[9, 22]` into the gateway's state line. The
   cup whose horse is 9 hears it, becomes 22, saves 22 and reports 22 from
   then on, tokens and all, because it is the same cup; 9 leaves the field;
   the board lists whoever is in the field in numeric order. Nothing is
   physically moved on the mantle and no cup is addressed: **the pair stays
   in the line for as long as the record stands**, so a cup set to 9 later
   (a spare, a cup that was off) becomes 22 the moment it hears the gateway,
   and a cup that has already followed it no longer matches. With no cup
   saying 9 yet, only the record is made and the pair still goes down. The
   name-swap of 726d4c2 (same number, new name) is gone: a bare string
   `replacement` is a 400 that says the shape.

   **Undo** (`POST /api/quiniela/unscratch {"horse": 9}`) removes the record
   (22's name stays stored), bumps `names_rev`, and the board sends the pair
   back, `[22, 9]`, for `UNDO_RENUM_S` (60 s) or until a cup reports 9,
   whichever is first, so the cup that became 22 goes back to 9. A fresh
   record that contradicts a pending undo pair (9 scratched again with 22, a
   record moving cups onto 22, a record with the same `from`) wins and the
   undo pair is dropped, so the line never asks a cup to bounce. At most
   `RENUM_SLOTS` (4) pairs fit the line: records first, an undo pair waits
   for a free slot with one warning (four also-eligibles make more than four
   records impossible through the routes).
2. **No replacement** (`{"horse": 9}` alone, or `"replacement": null`): a
   scratch is about the horse, not the cup (bench, 2026-09-26: with one cup
   online, scratching 20 answered "not on any cup"). pi5 records it in
   `lq_scratches` (`was` 9, `now` NULL) whether or not a cup says 9, so
   `horses["9"].scratched` is true, `in_field` false and the entry `{"was":
   {...}, "now": null}` is in `scratches` at once; `names_rev` bumps; and the
   board's `refresh()` puts 9 into the state line's `scr` list, so the cup
   whose horse is 9 draws its X, now or whenever a cup is set to 9. `unscratch`
   mirrors it: the record goes and the bit leaves the line. A repeat scratch
   is a 400 `horse 9 is already scratched`. **Its tokens are excluded from
   the pot and the prizes**; Joey refunds them by hand. `total_tokens` still
   counts every cup. This pot rule is an assumption about how a
   no-replacement scratch is settled; if the tokens should stay in the pot
   instead, it is the one `if not h["scratched"]` in `apply_snapshot()`.

**`in_field`**, per horse, computed by pi5: 1..20 true unless scratched
(either kind); 21..24 true only while standing in for a scratched horse. A
horse of any number that is the `now` of a replacement record is in the
field; a horse that is the `was` of a record, or in the state line's `scr`
list, is not (`horses.in_field()`). `horses[n].replaced` is the upper-cased
name of the horse n stands in for (the `was` of the record whose `now` is
n), else `null`.

**The unused-number rule.** The replacement number must be unused: not the
horse of any cup DevPi has heard, not in the field, not the `was` or `now`
of any record (400 `22 is in use`; the scratched horse's own number counts
as in use). The horse itself must be in the field (400 `horse 9 is not in
the field`), so a horse is never scratched twice; 22 can be scratched in
turn (22 -> 23: the records chain, both pairs ride in the line and a cup
walks them in order, so a cup set to 9 ends up 23; undo walks back one
record at a time, last record first: undoing 9 while 22 -> 23 stands is a
400 `horse 9: undo 22 first`, since the cup says 23 and nothing could go
back to 9, and 22 would be left out of the field with no record to bring it
back; the admin page withholds that Undo and says so).

Changing names or scratches of either kind never produces an event on the
ticker and never resets the baseline: events only ever come from token
diffs on the same cup, and a renumber is exactly a cup move (horse 9 goes
from its cup's MAC to none and horse 22 from none to that MAC, tokens
along), so the ticker keeps what it had, other horses' bets in the same
snapshot still count, the pot does not move, and the JSONL log carries the
move as `"cup": ["A0:B7:65:12:34:56", null]` and `"cup": [null,
"A0:B7:65:12:34:56"]`. The one blind spot of diffing by horse: a token that
lands in the renumbered cup in the very packet that carries the renumber is
counted, in the pot and in the log, but not tickered, because on that horse
the count change is also a cup move.

### Results

The dashboard's `POST /api/results` (main.py) writes
`pi5/data/results.json` (`{"win","place","show","timestamp"}`). The board
reads that file on every `refresh()` (`read_results()`: a missing or broken
file, a number outside 1..24 or a horse named twice all read as no results)
and puts the three numbers into the state line's `res`, so in WINNER and
AFTER_PARTY the named cups show their WIN / PLACE / SHOW frame; the model
carries them as `results`: `{"win": 19, "place": 1, "show": 22}`, or `null`
while none is named. (The dashboard names all three at once. A hand-made
file naming only some gives the dict with `null` for the rest, and the
state line's `res` a 0 there.) **Reset betting** removes the file and clears
them, and so does the dashboard's RESET; nothing else does. pi5 keeps the
file when it starts (it used to delete it), so a restart in WINNER comes back
with the results, the cups' frames and the TV's results screen. The file is
the single store: the dashboard writes it (whole or not at all: a temporary
file flushed to the card and renamed over it), La Quiniela reads it.

**The LED rule.** The results are facts about the race, not about the LEDs.
`POST /api/results` saves them first, always, and sets WINNER with them;
only then does it tell the LED controller (`RESULTS:FINALIZE`, the winners'
chase settling into the heartbeat: the three cups were already locked as
they were picked). Its reply carries `"leds": "ok"` or `"unreachable"`, the
dashboard's notification says `· LEDs unreachable` in red, and the results
stand either way. `success` is false only when the file could not be written
(500, `results not saved: ...`); then nothing moves.

#### The results board

`QUINIELA_BOARD_STATES` is `[1, 2, 3, 4, 5]`: the TV board keeps the screen
through WINNER, the moment the prizes are needed, and hands it back to the
slideshow in 0 (PRE_RACE) and 6 (AFTER_PARTY). What it shows in WINNER
depends on `results` alone:

- `null` (HEARTBEAT, or the admin page's WINNER button, before SET WINNERS):
  the betting board with the figures at the post (below), under the banner
  `OFFICIAL RESULTS COMING`.
- all three named: the results screen, `OFFICIAL RESULTS`. Three rows, WIN /
  PLACE / SHOW, each with the horse's saddle cloth and name, the bets its
  cup held and its prize, big, at the right; the pot above them; under them
  `ONE TOKEN DRAWN FROM EACH CUP · DRAWN TOKEN TAKES THE PRIZE`. The crawl
  stays. It flips the moment the model carries the results: `POST
  /api/results` saves them and sets WINNER in one state line, and the model
  that follows has both.

#### The figures at the post (`closing`)

The bets and the prizes on the frozen board (3 and 4), under `OFFICIAL
RESULTS COMING` and on the results screen are the figures as they were when
betting closed, and **pi5 holds them**, not the page: the model's `closing`.

- **Taken** the first time the race state is 3 AT_THE_POST with none held,
  or 4 or 5 when AT THE POST was skipped (from the same snapshot as the
  model that first says so, so the TV never sees the closed state without
  them): the pot, the prizes, `total_tokens` and every horse's tokens, in
  the live fields' shapes, plus `at`. Logged as a change, `{"closing":
  {"pot", "prizes", "total_tokens"}}`, and saved in `lq_closing` before the
  model goes out.
- **Held** whatever the cups do afterwards (a token after the post, the
  winners' cups emptied for the draw), through FINAL CALL pressed by
  mistake (2 neither takes nor drops them, so WINNER again shows the same
  figures, never ones taken again from emptied cups), AFTER_PARTY, and a
  restart of pi5 (the store loads them).
- **Dropped** by Reset betting and by a state that reopens betting: 0
  PRE_RACE or 1 BETTING_OPEN (the dashboard's WELCOME, TEST, STANDBY, 60 MIN,
  30 MIN; the admin page's PRE-RACE and BETTING OPEN). Logged as
  `{"closing": null}`. To take them again after a mistaken close, reopen
  with 60 MIN (or BETTING OPEN) and close again.

The live fields keep following the cups underneath (that is the truth about
the cups, and what the admin page shows); nothing on the TV reads them in
3, 4 and 5. So a TV page loaded after the cups were emptied, a second
screen, a phone on the board or on `/api/quiniela`, and a restart of pi5 or
of the splash all show the numbers at the post. The page falls back to its
own freeze (what it showed when betting closed) only for a model without
`closing`, which is an older pi5. The runbook's written-down figures are now
a one-line backup, for a close that was reopened before the draw was paid.

### Admin page

`GET /quiniela/admin` (on pi5, port 5000, so `http://joeydevpi.local:5000/quiniela/admin`)
is one plain page for a phone and the race-night control surface:
`RACE_NIGHT.md` runs the night from it and keeps the curls for its appendix.
Top to bottom:

- **Race**: `LINK OK` / `NO LINK` and the cups online; the seven state
  buttons (PRE-RACE · BETTING OPEN · FINAL CALL · AT THE POST · RUNNING ·
  WINNER · AFTER PARTY), the current one lit, each sending the same `state N`
  to `POST /api/quiniela/cmd` and reporting the reply on the line under the
  buttons (the state name in green, `(gateway offline ...)` appended when pi5
  kept it for later, the server's error verbatim in red); the figures, Pot /
  WIN / PLACE / SHOW / Bets, big enough to read at arm's length (Bets is the
  tokens in the pot; tokens in scratched cups are counted beside the label);
  **Reset betting** behind a `confirm()`, calling `POST /api/quiniela/reset`
  and reporting `Reset. Pot $N (M tokens still in the cup(s) of horse(s) ...)
  · K cups online`; and **Horses**, one row per horse 1..24 from the model
  and nothing to click: the number in its cloth colour, the name, the status
  (`● online`, `● offline`, `○ no cup`, or `⚠ 2 CUPS` when more than one cup
  claims the horse), the tokens, and `WIN` / `PLACE` / `SHOW` / `SCR` tags
  from the results and the scratches. The line above it reads `N cups online
  · M with no horse` (`cups_online` and `cups_no_horse` from the model; a cup
  showing `NO HORSE` is counted there and appears in no row). There is no
  cups table, no picker, nothing to adopt and nothing to forget: a cup's
  number is set on the cup.
- **Horse names**: the 24 names in a textarea (`1. NAME` lines, 21..24
  labelled as the also-eligibles in the caption, Save puts them back as text).
- **Scratches**: a row per horse **in the field** with a picker of the unused
  numbers (21..24 not claimed by a cup or in any record; a 1..20 number is
  always in use, in the field or scratched), a name box prefilled from the
  store when the picked number has a name (re-prefilled when the pick
  changes), Scratch, and a "No replacement" checkbox that makes it the
  no-replacement kind; below that a row per scratched horse of either kind
  showing was -> now (or "no replacement") with Undo (withheld, with the
  reason, on a record whose `now` was scratched in turn: the later record is
  undone first). The 5 s refresh never rebuilds the scratch rows while the
  operator is in them and carries a pick, a typed name or a ticked box over
  into rebuilt rows.
- **Betting closes**: a date-time picker plus +15 / +30 / +60 min and Clear.

Everything refreshes every 5 s from `GET /api/quiniela` alone, and every
button reports into its own status line (the pressed row's, for a scratch),
errors verbatim in red, network and non-JSON failures named. Vanilla JS, no
CDN.

### The dashboard reads the names

The names store is the only one: the dashboard's Race Setup page is gone
(it kept a second list of twenty names) and the dashboard reads La
Quiniela's. Its menu links to `/quiniela/admin` and to the board on the
splash (`SPLASH_BOARD_URL`, default `http://{host}:5001/` with `{host}` the
name the dashboard was opened with; `DDM_SPLASH_BOARD_URL` overrides it; it
may name the board's look, `http://{host}:5001/?look=dots`).
The board's tote look draws its names and figures in the dashboard's own
dots: its face is built from `dotPatterns` in `static/js/ddm_control.js`
(`splash_display/tools/make_tote_font.py`), which is why that table carries
`$` and the crawl's marks, none of which the dashboard prints itself.
Its results tote prints the winners' names and its SET WINNERS pickers list
the field by post (`GET /api/quiniela/field`), each as `19 · GOLDEN TEMPO`:
post 9 offers `22 · OCELLI` after The Puma's scratch, a horse scratched with
no replacement is not offered. A pick lights the LED cup of the post and
records the horse, so `pi5/data/results.json` holds horse numbers, which is
what the cups and the TV board are told. `GET /api/race`, the roster the
splash's horse-roster slide shows, lists the same field under the same
names; what is left of Race Setup is its store of the post time and the odds
(`data/race_setup.json`, `GET`/`POST /api/race-setup` and the three
odds-polling routes), which that slide and the spectator page still read and
which no page edits any more.

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
| `POST /api/quiniela/cmd` | Body `{"cmd": "state 1"}`; see below. |
| `GET /api/quiniela/mode` | The one race state and the dashboard mode that set it: `{"ok": true, "state": 1, "state_name": "BETTING_OPEN", "mode": "BETTING_60", "label": "60 MIN", "source": "dashboard", "modes": {...the table...}}`, `Cache-Control: no-store`. `mode` and `label` are null when the state was set directly (`source` `"cmd"`: the admin page or `state N`; `"reset"`: Reset betting) or not since pi5 started (`source` null). |
| `POST /api/quiniela/mode` | `{"mode": "BETTING_60"}`: a dashboard mode; the race state is the table's. `{"ok": true, "mode": "BETTING_60", "state": 1, "state_name": "BETTING_OPEN", "source": "dashboard", "rev": R, "gateway_online": bool}`. 400 `unknown mode ...; one of ...`, 503 `bridge not initialised`. Sets the race state only: the LEDs are the button's own request. |
| `GET /api/quiniela/horses` | `{"1": {"name": "Encino"}, ..., "24": {"name": ""}}`, names **as typed** (the model upper-cases them). |
| `PUT /api/quiniela/horses` | Body `{"text": "1. Dornoch\n2. Sierra Leone\n...\n22. Ocelli"}` (up to 24 lines) or the GET shape (`name` required per entry; a `replaced` key, the 726d4c2 shape, is ignored). Only the horses given are touched. 400 with the parse message. Returns `{"ok": true, "names_rev": N, "horses": {...}}`. Text rules: one name per line in program order (lines 21..24 are the also-eligibles); a leading `7.` / `#7` / `7)` / `7:` / `22.` names that horse instead, a bare `7` clears it, a blank line leaves it alone; a bare `7 Name` (number, space, name) is a prefix only when every non-blank line is numbered, so in a plain list `8 Belles` is a name. |
| `POST /api/quiniela/scratch` | `{"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}}` -> the renumber (kind 1): `{"ok": true, "kind": "replacement", "cup": "<MAC of the cup saying 9, or null>", "renum": [9, 22], "rev": R, "gateway_online": bool, "names_rev": N, "was": {"number": 9, "name": "Encino"}, "now": {"number": 22, "name": "Ocelli"}}` (names as typed). `name` is optional (22 keeps its stored name). 400 `horse 9 is not in the field`, `horse 9 is already scratched`, `22 is in use`, `replacement number must be 1-24`, `replacement must be {"number": N, "name": "..."}` (a bare string or any other shape). `{"horse": 9}` (or `"replacement": null`) -> kind 2: recorded (`was` 9, `now` NULL) whether or not a cup says 9, the bit in the state line: `{"ok": true, "kind": "gateway", "horse": 9, "cup": "<MAC or null>", "scratched": true, "rev": R, "gateway_online": bool, "names_rev": N, "was": {"number": 9, "name": "Encino"}}`. Both work without a bridge (recorded, `cup` null, `rev` null). |
| `POST /api/quiniela/unscratch` | `{"horse": 9}` reverses either kind: if 9 is the `was` of a record, the record is removed (22's name stays stored), `names_rev` bumps and the pair `[22, 9]` goes down for a minute or until a cup reports 9: `{"ok": true, "kind": "replacement", "cup": "<MAC of the cup saying 22, or null>", "renum": [22, 9], "rev": R, "gateway_online": bool, "names_rev": N, "was": {...}, "now": {...}}` (400 `horse 9: undo 22 first` while a record 22 -> 23 stands: a chain is undone last record first); else the kind 2 undo: the record goes and the bit leaves the line, `{"ok": true, "kind": "gateway", "horse": 9, "cup": "<MAC or null>", "scratched": false, "rev": R, "gateway_online": bool, "names_rev": N}`; 400 `horse 9 is not scratched` when neither applies. |
| `GET /api/quiniela/field` | The field by post, for the dashboard's SET WINNERS pickers and its results tote: `{"names_rev": N, "posts": [{"post": 9, "horse": 22, "name": "OCELLI", "label": "22 · OCELLI", "replaces": 9}, ...], "names": {"1": "DORNOCH", ..., "24": ""}}`, `Cache-Control: no-store`. A post is a place on the mantle, 1..20, and the LED cup there. `posts` has one entry per post somebody runs from, in post order: the post's own horse, or the one standing in for it (the cup was renumbered and nothing moved, so 22 runs from post 9 and `replaces` says so; a chain 9 -> 22 -> 23 gives 23); a post whose horse was scratched with no replacement has no entry. Names are upper-cased, `""` where none is stored (the label then says `HORSE n`); `names` carries all 24. Works without a bridge. |
| `PUT /api/quiniela/closes_at` | `{"at": <unix time>}`, `{"in_minutes": 30}` (from the server's clock) or `{"at": null}` -> `{"ok": true, "closes_at": ...}`. |
| `POST /api/quiniela/reset` | The between-races reset, `reset_betting()`: PRE_RACE and the results cleared (the dashboard's file too) in one state line, the scratched bits and renumber pairs kept; the closing time and the closing figures (`closing`) cleared, the ticker cleared, the cups' current counts the new baseline so nothing shows as a bet; names and both kinds of scratch untouched; the cups keep their numbers, which are theirs. Tokens still in a cup are not an error, the pot reads them: `{"ok": true, "race_state": 0, "pot": 15.0, "total_tokens": 15, "horses_with_tokens": [9, 21], "cups_online": 20, "events": 0, "closes_at": null, "rev": R or null, "gateway_online": bool, "names_rev": N}`. Works without a bridge (`rev` null). |
| `GET /quiniela/admin` | The admin page above. |

These operator routes refresh the model synchronously before answering, so a
`GET /api/quiniela` right after one already shows the change (the store's
`on_change` also wakes the board thread). Errors are `{"ok": false, "error":
...}`.

```
curl -X PUT  localhost:5000/api/quiniela/horses    -H 'Content-Type: application/json' -d '{"text": "1. Dornoch\n2. Sierra Leone\n9. Encino\n22. Ocelli"}'
curl -X POST localhost:5000/api/quiniela/scratch   -H 'Content-Type: application/json' -d '{"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}}'
curl -X POST localhost:5000/api/quiniela/scratch   -H 'Content-Type: application/json' -d '{"horse": 9}'
curl -X POST localhost:5000/api/quiniela/unscratch -H 'Content-Type: application/json' -d '{"horse": 9}'
curl -X PUT  localhost:5000/api/quiniela/closes_at -H 'Content-Type: application/json' -d '{"in_minutes": 30}'
curl -X PUT  localhost:5000/api/quiniela/closes_at -H 'Content-Type: application/json' -d '{"at": null}'
curl -X POST localhost:5000/api/quiniela/reset
```

They are deliberately not under `la_quiniela_bp`, whose `after_request`
rewrites `Cache-Control`.

### The model

```json
{"link_ok": true,
 "race_state": 1, "race_state_name": "BETTING_OPEN",
 "token_value": 1.0, "pot": 33.0, "total_tokens": 33,
 "horses": {"1": {"tokens": 0, "share": 0.0, "scratched": false, "online": false, "cup": null,
                  "conflict": false, "cups": [], "name": "", "replaced": null, "in_field": true},
            "7": {"tokens": 23, "share": 0.697, "scratched": false, "online": true, "cup": "A0:B7:65:12:34:56",
                  "conflict": false, "cups": ["A0:B7:65:12:34:56"], "name": "HONOR MARIE", "replaced": null, "in_field": true},
            "9": {"tokens": 0, "share": 0.0, "scratched": false, "online": false, "cup": null,
                  "conflict": false, "cups": [], "name": "ENCINO", "replaced": null, "in_field": false},
            "22": {"tokens": 10, "share": 0.303, "scratched": false, "online": true, "cup": "A0:B7:65:12:34:57",
                   "conflict": false, "cups": ["A0:B7:65:12:34:57"], "name": "OCELLI", "replaced": "ENCINO", "in_field": true},
            "...": "...24 entries, keys \"1\"..\"24\"..."},
 "leader": 7,
 "events": [{"horse": 7, "delta": 1, "ts": 1777662847.2}],
 "updated": 1777662847.2,
 "board_states": [1, 2, 3, 4, 5],
 "now": 1777662850.4,
 "closes_at": 1777664400.0,
 "prizes": {"win": 20, "place": 8, "show": 5},
 "split": {"win": 0.6, "place": 0.25, "show": 0.15},
 "chyron": ["TOTALS BASED ON CHEAP CHINESE ELECTRONICS · FINAL RESULTS HAND COUNTED",
            "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS"],
 "names_rev": 4,
 "scratches": [{"was": {"number": 9, "name": "ENCINO"}, "now": {"number": 22, "name": "OCELLI"}},
               {"was": {"number": 20, "name": "SOCIETY MAN"}, "now": null}],
 "cups_online": 20, "cups_no_horse": 0,
 "results": null,
 "closing": null}
```

Once betting has closed, `closing` is the figures at the post, the live
fields' shapes plus `at`:

```json
"closing": {"pot": 33.0, "prizes": {"win": 20, "place": 8, "show": 5}, "total_tokens": 33,
            "horses": {"1": {"tokens": 0}, "7": {"tokens": 23}, "...": "...24 entries...", "22": {"tokens": 10}},
            "at": 1777664400.2}
```

The first eleven keys are the original contract and are unchanged; the rest
are additive (the splash relays the whole model untouched). From the snapshot,
the store and the results file as follows:

- `link_ok` = `link.port_open and link.gateway_online`.
- `race_state` = `devpi.phase` (DevPi's own phase, the one `set_state()`
  holds), `race_state_name` its `Phase` name, `STATE_<n>` for a value the
  enum does not know.
- `horses[n]`: the cups claiming horse `n` are the snapshot's cups whose
  `horse` is `n`, ordered online first and most recently heard first. The
  first supplies `tokens` (`count`, `None` until the first telemetry line
  reads as 0, negatives clamp to 0), `online` (true when any online cup
  claims the horse) and **`cup`, its MAC** (`null` when no cup claims the
  horse). `conflict` is true when more than one **online** cup claims the
  horse and `cups` lists their MACs (the one shown first); one WARNING is
  logged per (horse, cups). A cup that went offline while another took its
  horse is listed alone and is no conflict. `share` = `round(tokens /
  total, 4)`, 0.0 with no tokens. Since protocol v2 `cup` is a MAC where it
  used to be a cup number; the splash page only tests it for `null`.
- `pot` = `round(tokens * token_value, 2)` over the horses that are **not**
  scratched (the no-replacement kind above); `total_tokens` is every cup.
- `leader`: strictly the most tokens, the lowest horse number on a tie, `null`
  when every count is 0. Scratched horses are not excluded.
- `events`: the last eight token changes, newest first. The first snapshot a
  board digests is a baseline, not a bet, so a restart never invents drops:
  the bridge seeds each cup's count from `lq_cups` before the gateway is
  heard, and only a count that moved while pi5 was down shows up as a (late
  but real) drop. `reset_betting()` applies its snapshot with `baseline=True`:
  the events are cleared and nothing is diffed, so the between-races reset
  starts the ticker clean whatever sits in the cups. A horse whose cup
  changed (the MAC claiming it moved, or went away, or a cup newly heard on
  it) gets no event for the count that came with the cup: a replacement
  scratch, which renumbers the cup, is exactly that, and so is a spare
  swapped in with the dead cup's tokens. Only a count that changed on the
  same cup is a bet or a removal, and that cuts both ways: cups emptied or
  re-tared between two races on one evening are removals, and their `-N`
  chips stay on the ticker into the next BETTING_OPEN until eight newer bets
  push them off. Before a second race, empty the cups and press Reset
  betting (`POST /api/quiniela/reset`) once the zeros have been heard; a
  reset with tokens still in the cups makes those counts the baseline
  instead, and emptying them afterwards shows as removals. The log records a
  reset as a baseline (see the log below). The one exception is a fresh or
  deleted `la_subasta.db` started with tokens already in the cups: the
  baseline then holds zero for every cup, and the first telemetry shows
  those counts once as drops on the ticker.
- `updated`: wall time of the last change. A snapshot that changes nothing
  publishes nothing and leaves it alone.
- `board_states`: the race states in which the page takes over the TV.
- `now`: the server's clock, stamped when the model is serialised (`GET
  /api/quiniela`, every SSE chunk), never stored and never part of the
  changed-comparison, so the stream stays quiet between real changes. The
  page counts down `closes_at` against it rather than the phone's clock.
- `closes_at`: unix time or `null`, from `PUT /api/quiniela/closes_at`.
  `reset_betting()` clears it; names survive.
- `prizes`: `{"win", "place", "show"}` whole dollars summing to `pot`, by the
  rule above. `split`: the three fractions from config.
- `chyron`: `LQ_CHYRON_LINES` from config, for the crawl along the bottom.
- `names_rev`: the names store's revision, bumped by any name change, any
  scratch of either kind and its undo, persisted in `lq_board`; a change
  publishes a model (it differs) but yields no events.
- `horses[n].name`: the store's name **upper-cased** (`""` when unset).
  `horses[n].in_field`: the rule above. `horses[n].replaced`: the
  upper-cased name of the horse n stands in for (the `was` of the record
  whose `now` is n; `""` when that horse was unnamed), else `null`.
- `scratches`: one entry per scratch, ordered by `was.number`, names
  upper-cased and `""` when unnamed: a replacement record `{"was":
  {"number": 9, "name": "ENCINO"}, "now": {"number": 22, "name": "OCELLI"}}`,
  a no-replacement scratch `{"was": {"number": 20, "name": "SOCIETY MAN"},
  "now": null}`. A renumber never changes `pot` (the cup keeps counting
  under its new number); a no-replacement scratch takes its horse's tokens
  out of it.
- `cups_online`: every online cup, whatever it says; `cups_no_horse`: the
  online cups reporting horse 0 (their screens say `NO HORSE`). The admin
  page's `N cups online · M with no horse` line.
- `results`: `{"win": 19, "place": 1, "show": 22}` from the dashboard's
  file (above), or `null` while none is named. In WINNER it is what turns
  the TV's frozen board into the results screen.
- `closing`: the figures at the post, `{"pot", "prizes", "total_tokens",
  "horses": {"1": {"tokens"}, ... "24": ...}, "at"}`, or `null` while there
  are none: taken the first time the race state is 3 (or 4 or 5 when 3 was
  skipped), held through the race, the draw and a restart, dropped by Reset
  betting and by 0 or 1 ("The figures at the post", above). The TV reads
  them in 3, 4 and 5; the live `pot`, `prizes`, `total_tokens` and tokens
  keep following the cups.
- `share` and `leader` stay as they were; nothing new depends on `share`.

### One race state

There is one race state, and the dashboard's modes are its source. The
dashboard's thirteen buttons drive the LEDs, as they always did; each also
names its mode to pi5 (`POST /api/quiniela/mode`), and La Quiniela's race
state, the one the cups and the TV board follow, is derived from the mode:

| Dashboard button | Mode | La Quiniela state |
| --- | --- | --- |
| WELCOME, TEST, STANDBY | `WELCOME`, `TEST`, `STANDBY` | 0 PRE_RACE |
| 60 MIN, 30 MIN | `BETTING_60`, `BETTING_30` | 1 BETTING_OPEN |
| FINAL CALL | `FINAL_CALL` | 2 FINAL_CALL |
| AT THE GATE | `AT_THE_GATE` | 3 AT_THE_POST |
| THEY'RE OFF!, CHAOS, FINISH | `GATES_BURST`, `CHAOS`, `FINISH` | 4 RUNNING |
| SET WINNERS, once the results are applied | `RESULTS` | 5 WINNER |
| HEARTBEAT | `HEARTBEAT_COOLDOWN` | 5 WINNER |
| RESET | `RESET` | 6 AFTER_PARTY |

The table is `MODE_STATES` in `la_quiniela/betting.py` and nowhere else (the
page carries each button's mode in `data-mode` and asks pi5). Two lines
differ from the first draft of it: HEARTBEAT was not listed, and is WINNER
(the dashboard's own spectator map calls it OFFICIAL: the race is over, and
with no results yet the TV says OFFICIAL RESULTS COMING); and the dashboard
has no after-party mode, so RESET, which ends the race and clears the
results, is AFTER_PARTY.

**One path.** Every change goes through `BettingBoard.set_race_state()`: a
mode (`set_mode()`), the admin page's seven buttons and `state N` on `POST
/api/quiniela/cmd`. The shared value is the bridge's phase, the one that is
persisted and whose rev the gateway acknowledges; the mode that set it is
kept beside it (in memory) and named only while it still explains the state.
One state line goes down per change, carrying the state together with
whatever else the line should hold at that moment (scratched bits, renumber
pairs, the results), so a cup never shows WINNER before it knows who won.
Two modes of one state (60 MIN, then 30 MIN) send nothing the second time.

- SET WINNERS and RESET are told by the dashboard routes they call: `POST
  /api/results` sets mode `RESULTS` once the results are saved (its reply
  carries `"race": {...}`), `POST /api/results/clear` sets `RESET` after the
  file is gone. Opening the SET WINNERS modal moves nothing.
- The LED routes themselves (`/api/animation/<name>`, `/api/led/all_off`)
  never move the race state: the Animations list, the Animation Library's
  previews and a button toggled off are LEDs only. Nothing about the LEDs
  changed.
- The race state does not wait on the LEDs: with the LED controller
  unreachable a mode button still sets it, and its notification says both
  (`Error: ERROR:TIMEOUT · FINAL CALL`). Nor do the results: SET WINNERS
  saves them and sets WINNER first, and says `· LEDs unreachable` after
  (the LED rule, under Results).
- The admin page's seven buttons set the same value directly (they start no
  LED animation) and light the current one from the model's `race_state`
  within its 5 s refresh; the dashboard reads the state back every 5 s and
  shows it on its ticker, so a state set on the phone shows on the
  touchscreen and the other way round.
- `closes_at` is untouched by all of it: the countdown is still manual.
- Not part of it: the mock racing service (`/api/racing/*`, the dashboard's
  AUTO / MANUAL switch, states DORMANT .. OFFICIAL). In AUTO it drives the
  LEDs by itself and the mode buttons are disabled; La Quiniela does not
  follow it.

### `POST /api/quiniela/cmd`

The splash's whitelist of gateway text commands is kept, so the operator's
habits and the splash's tests carry over, but nothing is ever written to the
port as text: the bridge's revision model is the source of truth, and a
hand-typed line would be overwritten by the next hello answer. Since
protocol v2 only one command has a meaning here:

| Command | Does | Answer |
| --- | --- | --- |
| `state N` (0..6) | `set_race_state(N)`, the path a dashboard mode takes (above): the state, with the scratched bits, renumber pairs and results as pi5 has them | `{"ok":true,"rev":R,"phase":N,"gateway_online":bool}` |
| `demo` | refused, 400 | `demo is not routed through pi5: the bridge speaks the JSON line protocol, and every state line turns demo off` |
| `json` | refused, 400 | `json is not routed through pi5: the bridge already reads the gateway's protocol, and the up state line is the gateway's report, not a command` |

The v1 `horse`, `scratch` and `roster` commands no longer exist (400, the
first word is not one of `state demo json`): a cup's number is set on the
cup, scratches go through `POST /api/quiniela/scratch`, and there is no
roster. A state change is applied even when the gateway is offline: DevPi
holds it and re-sends it on the next hello or status line, and
`gateway_online` in the answer says which of the two happened. Errors are
`{"ok":false,"error":...}`: 400 for a command that is not a string, empty,
longer than 200 characters, not a single line, or whose first word is not
whitelisted (case matters); 400 `usage: state 0-6` for bad arguments; 400
with the message from `validate_phase` if the bridge rejects the state; 503
`bridge not initialised` when there is no bridge.

### Configuration

Three more keys in `pi5/config.py`, with the names the splash display used,
each overridable by `DDM_<KEY>` in the environment or `pi5/.env`:

| Key | Default | Meaning |
| --- | --- | --- |
| `TOKEN_VALUE` | `1.00` | Dollars per token, for the board's POT (`DDM_TOKEN_VALUE`, a float) |
| `QUINIELA_LOG` | `True` | Write the JSONL log below (`DDM_QUINIELA_LOG`: 1/true/yes/on or 0/false/no/off) |
| `QUINIELA_BOARD_STATES` | `[1, 2, 3, 4, 5]` | Race states in which the splash board owns the TV: betting, the race and the results; it is released in 0 and 6 (`DDM_QUINIELA_BOARD_STATES`, a comma list such as `1,2,3,4,5`) |
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
and `lq_link` emit and at the end of `set_state()`. Listeners run on the
reader thread with the bridge's lock held, so they may only set an `Event`
or `put_nowait()`; the board's `wake()` does exactly that. A listener that
raises is logged at WARNING and dropped.

The board's daemon thread, `lq-board`, waits on that event with a one second
timeout and calls `refresh()` on every wake, so the model also follows a
gateway that has simply gone quiet (the bridge's offline timer emits, but the
timeout is the backstop). `refresh()` takes `get_snapshot()` without holding
the board's own lock, applies it under that lock, and then **keeps the
gateway's state line in step with what pi5 knows** (`_sync_gateway()`): the
no-replacement scratches from `lq_scratches` as the `scr` list, the
replacement records as `[was, now]` pairs plus any undo pairs still being
sent, and the dashboard's results; when the bridge's `devpi` state differs,
one `set_state()` goes out and the fresh snapshot is applied. So every path
agrees: a scratch made without a bridge reaches the gateway on the first
refresh after one appears, a cup set to a scratched horse tomorrow finds its
bit already in the line, and nothing is ever addressed to a cup. The lock
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
honest about what was not a bet: a baseline (the first snapshot after a
start, or the between-races reset) writes a record with `"baseline": true`,
the reset adding `"reset": "betting"` and leaving its trace even when
nothing moved; and a count that came with a cup move (the MAC claiming the
horse changed, or went away, or arrived) carries the move in its change,
e.g. `{"horse":22,"tokens":[0,51],"cup":[null,"A0:B7:65:12:34:56"]}`. The
closing figures leave their trace too: taken, `{"closing":{"pot":154.0,
"prizes":{"win":92,"place":39,"show":23},"total_tokens":158}}` beside the
state change that took them; dropped, `{"closing":null}`. The
first write failure logs one WARNING and disables the log for the rest of
the process; the model is unaffected.

### Tests

```
cd pi5
python -m la_quiniela.test_betting
```

Hand-built snapshots and a real `LqBridge` over the smoke test's fake port
exercise the model (a conflict from two cups claiming one horse, a cup at
horse 0 counted but in no row, the cup-move rule by MAC), the log, the
settings, the listener hook, the thread, the `state` command (byte-exact
against `protocol.build_state_line`; the v1 commands refused), the prize
rounding, both kinds of scratch (the renumber 9 -> 22 on a real bridge: the
pair in the line, the cup's tokens under 22, no events, the pot unchanged,
undo with the pair sent back until the cup reports 9 or 60 s pass, a fresh
record beating a pending undo, the four-slot cap; a scratch before any cup
says the horse; the in_field rule; the unused-number and not-in-the-field
rejections), the results file into the state line and out again on reset,
the names text parser for 24 lines, the closing time, the between-races
one race state (each of the dashboard's thirteen modes against the table,
byte-exact lines, the cmd route and the admin page's buttons setting the
same value, WINNER and the results in one line), the between-races
reset (one byte-exact PRE_RACE line with the bits and pairs kept and the
results cleared, the ticker and `closes_at` zeroed, the tokens still in the
cups read and their horses named), the closing figures (taken on 2 -> 3,
0 -> 3, 1 -> 4 and 1 -> 5, in the very model that first says the closed
state; not moved by later counts, the emptied cups included; kept by 2, 6
and a snapshot with no phase; dropped by 1 and by Reset betting; logged,
saved in `lq_closing`, and a database error costing only the copy on disk),
a database from before `lq_closing` gaining it at start, a restart in
WINNER after the cups were emptied (a new bridge and board over the same
database and results file: WINNER, the results and the closing figures as
they were, the hello answered with them), the `now` stamp, the three tables
with the `lq_horses` and `lq_scratches` migrations and every route on a
Flask test app, the admin page's Race section included (the seven state
buttons, the figures, Reset betting behind a confirm, the Horses list with
its four statuses and no cup controls), and that the v1 dev routes are gone.
`test_smoke` pins `protocol.MAX_HORSE` to `DDM_MAX_HORSE` in `ddm_common.h`
and also checks that importing `main.py` starts no `lq-board` thread and
registers the routes, the admin page included.

```
python -m la_quiniela.test_dashboard
```

The dashboard's side, on the real app (`main.py`, its templates and static
files) with the LED controller stubbed and the tote board off: the menu, the
names on the tote and in the pickers, `/api/race`, the thirteen buttons each
carrying its mode, the LED command each one sends (unchanged), the race
state each one sets, the results making it WINNER and RESET making it
AFTER_PARTY; the results saved, WINNER in one line and `"leds":
"unreachable"` with the LED controller down (RESULTS:FINALIZE tried once,
after the save), a file that cannot be written moving nothing (500), the
page's red `· LEDs unreachable` and its following the server's results, and
that nothing main.py runs at start removes a file.

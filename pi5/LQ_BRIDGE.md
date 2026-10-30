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
3. Put `DDM_LQ_SERIAL_PORT=/dev/serial/by-id/...` in `pi5/.env` (copied
   from `pi5/.env.example`; git-ignored, because the port is this box's own
   setting and `pi5/config.py` is tracked). The `ddm-pi5` service reads
   `pi5/.env`; it doesn't see a variable exported in a shell.
4. Restart the app: `sudo systemctl restart ddm-pi5`. Its log
   (`journalctl -u ddm-pi5 -f`) shows `[LQ] bridge started on ...`, then
   the gateway's `hello` is answered with the state line DevPi holds.

With no port configured the bridge idles and logs once how to set it. With
`LQ_BRIDGE_ENABLED = False`, or without pyserial, it logs one line and the
rest of the app, La Subasta included, runs exactly as before.

The port is opened with `exclusive=True`, so a second copy of the app cannot
open the same port. A missing or vanished port is retried every 5 s forever,
logged at most once a minute.

## Configuration (`pi5/config.py`, `pi5/.env`)

`pi5/config.py` is tracked and holds only the defaults below; nothing secret
and nothing that belongs to one box goes in it (the repo is public). Every
key is set by an environment variable of the same name prefixed `DDM_`, and
on DevPi by a line in `pi5/.env` (git-ignored; `pi5/.env.example` lists
every setting): `DDM_LQ_SERIAL_PORT` is the one the night needs. A simulator
points the app at a virtual port the same way (`DDM_LQ_SERIAL_PORT=/dev/pts/3`).
A variable set in the environment wins over `pi5/.env`.

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
in `pi5/config.py` or `pi5/.env` is ignored, and the installer WARNs about a
`DDM_LQ_DEV_ENDPOINTS` line in `pi5/.env` (delete it).

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
| `lq_scratches` | one row per scratch: `was` (PK, 1..24, the horse that left the field), `now` (1..24, the horse standing in for it, on the same cup; NULL for a no-replacement scratch, whose tokens are handed back to be re-bet). A table created with `now NOT NULL` (c70d894) is rebuilt by `init_schema()` (`_migrate_lq_scratches`), rows kept |
| `lq_scratch_entered` | one row per scratch record entered since it exists: `was` (PK), `state` (the race state at entry, 0..6), `at` (unix time). The model's `rebet` comes from it; a record without a row is from before it and reads as before. A database from before it gains it at start (`CREATE TABLE IF NOT EXISTS`) |
| `lq_scratch_names` | one row per replacement record whose scratch changed its now's name: `was` (PK, the record's), `now`, `name_before` (`''` for none), `name_set`. Undo puts `name_before` back while the name is still `name_set`, and drops the row; a record without a row (one from before this table, or one whose scratch kept the stored name) leaves the name. Added at start like `lq_closing` |
| `lq_renum_undo` | one row per undone replacement whose cup may still need bringing back: `now` (PK, 1..24, the number the cup took), `was` (the number it goes back to), `undone_at` (unix time). Written by Undo; dropped once the undo is settled or `now` is a replacement again ("Renumber pairs: derived and acknowledged", below), so an undo outlives a restart. A database from before it gains it at start (`CREATE TABLE IF NOT EXISTS`) |
| `lq_board` | one row: `names_rev` (a database from before 2026-10-30 had a `closes_at` column too: the betting-closes time went, and `init_schema()` rebuilds the table without it) |
| `lq_closing` | one row: `closing`, the board's figures at the post as JSON (the model's `closing`, below), or NULL while there are none. A database from before it gains it at start (`CREATE TABLE IF NOT EXISTS`); nothing else changes. While the host's hand count is held it is one more key of this JSON, `pot_counted` (whole dollars), so the count is saved, held and dropped exactly as the figures are ("The counted pot", below); no schema change |
| `lq_race` | one row, the race: `name` (as typed, `''` for the default KENTUCKY DERBY), `year`, `post_at` (unix time; NULL while unset), `migrated` (1 once the old Race Setup file has been looked at). Reset betting never touches it. Added at start like `lq_closing` |

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
changed), `bridge_reopen`, `bridge_bad_open` (both under "The watchdog" and
"A bad open", below), and the betting board's `renum_ack` and `renum_late`
("Renumber pairs: derived and acknowledged", below). The `horse` column of
a cup event is the horse the cup reports (NULL for none).

To start fresh before a party, pi5 stopped: `python3 pi5/tools/wipe.py` (try
`--dry-run` first) empties every table of this database, La Subasta's too, and
removes results.json, the betting log and the old Race Setup file, after a
backup it checks file by file (RACE_NIGHT.md, "Before the party: start fresh").

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
picture within one packet. At start every cached cup is offline, in the
snapshot and in its row (`online = 0`), so a cup that never reports again does
not stay `online` in the table. `forget_cups(prefix)` / `POST /api/lq/cups/forget`
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
after `gateway online` is a good sign, not a bad one. Under the `ddm-pi5`
service the console is its log: `journalctl -u ddm-pi5 -f` (the last 50
lines: `journalctl -u ddm-pi5 -n 50`).

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

Two lines come from the log rather than the console, with no `[LQ]` in front.
Under the service they are in the same journal:

| Line | Means |
| --- | --- |
| `La Quiniela bridge: bad open on ...: N bytes in 3.0 s, no valid line; reopening at once (1 of 3). First 32 bytes: ...` | A bad open, below. One line per bad open. |
| `La Quiniela bridge: still no valid line after 3 quick reopens; back to reopening after 20 s without one` | The quick reopens did not help; the watchdog carries on. Said once. |

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

### Opening the port

Every open goes the same way, the first one and every reopen:

1. pyserial opens the port at 115200 baud, 8 data bits, no parity, 1 stop bit,
   raw, with a 1 s read timeout, and exclusively, so a second copy of the app
   cannot open it. With `LQ_SERIAL_LINES` at `leave` the bridge never sets DTR
   or RTS. Linux raises both lines together as the port opens and drops both
   together as it closes, and the gateway's reset circuit ignores that (above).
2. The bridge waits 0.1 s, then drops whatever arrived in that time.
3. Nothing counts as a line until the first newline, so the tail of a line cut
   by the open is dropped.
4. The watchdog's 20 s and the 3 s below count from here.

Linux does not open the gateway's adapter, a CP2102, the same way every time.
The first open after the adapter appears, at boot or after a replug, starts
from the kernel's defaults (9600 baud, echo on), and pyserial's 115200 follows
as a second request. Every later open starts from the settings the last close
left, 115200 raw.

On DevPi's cold boots (2026-10-11) the first open handed over 120 to 800 KB of
garbage in 20 s and no valid line. 800 KB in 21 s is more than a 115200 line
can carry. The watchdog's reopen at 20 s brought the link straight up, and the
gateway kept running through it (`up_s` 283 with DevPi up 4 minutes).

Setting the same baud rate again on an open port would not help: pyserial
skips settings that have not changed, and the kernel's CP2102 driver sends the
chip nothing when they have not. A reopen is what sends the chip a fresh set.

### A bad open: caught in 3 s

An open is bad when, `BAD_OPEN_S` (3 s) after it, all three of these hold:

- bytes have come out of the port;
- at least one of them is a byte the gateway never sends: anything but
  printable ASCII, tab, CR or LF. Every gateway line is ASCII JSON or `# ` text.
- not one valid line has arrived.

The bridge then closes the port and opens it again at once, through the same
path. It checks once a second, so in practice this happens 3 to 4 s after the
open.

- **At most three quick reopens in a row** (`BAD_OPEN_QUICK_MAX`). After that
  the watchdog's cadence takes over again: a reopen 20 s after the open, then
  every 30 s. The log says so once. A valid line gives the next bad open its
  three quick reopens back.
- **Some ports are left to the watchdog, as before.** A port that says nothing
  is not a bad open. Neither is a port that sends only ASCII, such as the tail
  of a long status cut by the open followed by nothing until the next status
  5 s later, or the gateway's `# ` text with debug on.
- **A quick reopen does not reset the gateway.** It is the same close and open
  the watchdog does, and an app restart does. Nothing is written to the port,
  and DTR and RTS are left as `LQ_SERIAL_LINES` says. With `leave`, the gateway
  keeps running. With `low`, every open resets that board, quick or not.

Each bad open writes one log line and one `bridge_bad_open` event, and nothing
per byte:

```
La Quiniela bridge: bad open on /dev/serial/by-id/...: 28672 bytes in 3.3 s, no valid line; reopening at once (1 of 3). First 32 bytes: f8 f8 78 00 80 00 f8 78 ...
```

The event's detail is `{"bytes": 28672, "elapsed_s": 3.3, "hex": "f8 f8 78
00 ...", "quick": 1, "of": 3}`:

- `bytes` and `elapsed_s` count from the open, after the 0.1 s drop;
- `hex` is the first 32 bytes;
- `quick` is which of the three quick reopens this was.

`bridge_reopen` stays the watchdog's alone. A quick reopen does not write one
and does not count in `reopens`. The watchdog still writes one for a port that
says nothing (its `bytes_rx` does not move) and for garbage that outlasts the
three quick reopens. On the console a quick reopen prints nothing.

To read them on DevPi:

```
sqlite3 ~/DDM-Multimedia/pi5/data/la_subasta.db "select ts, detail from events where type in ('bridge_bad_open','bridge_reopen') order by id desc limit 5"
```

#### Reading the hex sample

This is what a receiver at the wrong rate makes of the gateway's 115200 hello,
from a bit-level simulation of the line:

| Receiving at | Bytes for the 60 sent | First bytes |
| --- | --- | --- |
| 115200, the right rate | 60 | `7b 22 74 22 3a 22 68 65 6c 6c 6f`, which is `{"t":"hello` |
| 9600 | 5 | `4c 89 a5 5a 46` |
| 57600 | 26 | `d2 48 31 6b 28 e8 39 31 31 29 4a c8` |
| 230400 | 92 | `9e 86 60 60 86 60 98 18 60 80 9e 66` |
| 460800 | 148 | `f8 f8 78 00 80 00 f8 78 00 80 80 f8` |
| 921600 | 181 | `80 80 80 00 00 00 00 80 80 00 00 00` |

- **A receiver too fast** gives runs of one bit value: bytes like `00`, `80`,
  `f8`, `78` and `ff`, up to about three bytes per byte sent.
- **A receiver too slow** gives fewer bytes than were sent, with no pattern.
- **Either way, the count follows the gateway.** It sends about 50 bytes a
  second with no cups, and about 2 KB a second with 20 (each cup's telemetry
  every 2 s, the status with the cup table every 5 s). So hundreds of KB in 20 s
  is not the gateway's own words read at a wrong rate.
- **A line held low** (the gateway's TX low, or the gateway in reset) gives
  `00` after `00`, at the receiver's own pace, whatever the gateway does.
- **A floating or noisy input** gives values with no pattern, in bursts that do
  not follow the gateway's 2 s and 5 s.

### What the snapshot says

`GET /api/lq/snapshot` carries these under `link`, alongside the older fields:

| Field | Means |
| --- | --- |
| `thread_alive` | The reader thread is running. False here is the whole answer. |
| `last_line_age_s` | Seconds since any line at all, junk included. |
| `lines_ok` | Lines that parsed as JSON. |
| `lines_bad` | Lines that did not: the gateway's `# ` text, bad JSON, over-long, unknown type. |
| `bytes_rx` | Bytes taken off the port. |
| `reopens` | How many times the watchdog has reopened the port. Not the quick reopens of a bad open: those are `bridge_bad_open` events. |

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
one horse are both in `cups`; the betting board decides what to show. Each
cup here also carries `seen`: when its last report arrived, on the bridge's
own monotonic clock (`now()`), or null for a cup not heard since pi5
started. It means nothing outside the process; the betting board tells a
report made after a scratch or an undo from one made before it by this,
never by `last_seen` (whole seconds). `lq_update` does not carry it.

## Python API (`la_quiniela.get_bridge()`)

| Call | Does |
| --- | --- |
| `set_state(phase=None, scratched=None, renum=None, results=None) -> rev` | Change any part of the state, the others kept. Validated exactly as the gateway does (`ValueError` on bad input: phase 0..6, scratched horses 1..24, at most 4 renum pairs with `from != to` and no `from` twice, exactly 3 results 0..24). Bumps `state_rev`, persists, logs `state_set`, sends if the port is open, emits `lq_snapshot`. Identical values are a no-op returning the current rev. |
| `forget_cups(mac_prefix=None) -> int` | Drop cups from the cache (all, or those whose MAC starts with the prefix), in memory and in `lq_cups`; a `cups_forgotten` event and a fresh `lq_snapshot`. Returns how many went. Decides nothing (above). |
| `get_snapshot() -> dict` | The `lq_snapshot` payload. |
| `now() -> float` | The bridge's clock (monotonic seconds), what a snapshot cup's `seen` is measured on. |
| `record_event(type, horse, detail)` | One row in `events`, for the betting board's own (`renum_ack`, `renum_late`). Never raises. |
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
`/dev/debug`) are gone and answer 404; the race state is set by the
dashboard's modes (`POST /api/quiniela/mode`), or by `state N` on the betting
board's `POST /api/quiniela/cmd` (tests, the simulator, a curl), scratches through
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
   physically moved on the mantle and no cup is addressed: **the pair goes
   in the line whenever an online cup says 9, for as long as the record
   stands**, so a cup set to 9 later (a spare, a cup that was off) becomes
   22 within a report or two, and the pair leaves the line once no cup says
   9. With no cup saying 9 yet, only the record is made. The name-swap of
   726d4c2 (same number, new name) is gone: a bare string `replacement` is
   a 400 that says the shape. How the pairs are derived and when a cup has
   followed one: "Renumber pairs: derived and acknowledged", below.

   **Undo** (`POST /api/quiniela/unscratch {"horse": 9}`) removes the record
   and puts 22's name back as it was before the scratch (a name the scratch
   gave is cleared, one entered ahead stays, one changed since the scratch
   stays; `lq_scratch_names` keeps what the scratch did, and a record from
   before that table leaves the name), bumps `names_rev`, and keeps a small
   record of the undo in `lq_renum_undo` (`now` 22, `was` 9, `undone_at`).
   **Undo works at any time** (a replacement's, from 60 MIN to WINNER, once its cup is
   empty: with tokens in it the route answers 409 `Cup 9 holds 4 tokens bet on #22. Hand
   them back and empty the cup, then Undo.` and nothing moves): the pair back, `[22, 9]`, goes in the line
   whenever a cup reports 22, with no time limit, so the cup that became 22
   goes back to 9 whether the undo came 0.3 s after the scratch (before the
   cup had even followed it) or an hour later, and a cup that was off during
   the undo goes back whenever it returns. The undo record goes once it has
   nothing left to do, or when 22 is a replacement again (the cups on 22 are
   then where they belong). Undo 9 -> 22 then scratch 9 -> 23 within a
   second: a cup still on 22 gets `[22, 9]` first and `[9, 23]` only once it
   reports 9, so it ends on 23 with no bounce.
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
   the pot and the prizes**; Joey hands them back to be re-bet in other cups
   (the crawl says `· RE-BET YOUR TOKENS`), and a token not re-bet is settled by
   the counted pot. `total_tokens` still
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

### Renumber pairs: derived and acknowledged

The line's `renum` pairs are derived afresh on every `refresh()`
(`desired_state()`, `_renum_plan()` in `betting.py`) from the store and
the cups in the bridge's snapshot, each with its horse, whether it is
online and `seen` (when its last report arrived, on the bridge's clock).
There is no time window anywhere: **a pair goes in the line while an
online cup reports its `from`, and a cup has followed it only when a
report newer than the pair says its `to`.** Nothing else about scratches
depends on this.

- **A record** `9 -> 22` (a replacement scratch standing): `[9, 22]` while
  an online cup reports 9.
- **An undone record** (`lq_renum_undo`: `now` 22, `was` 9): `[22, 9]`
  while an online cup reports 22. It is kept until it has nothing left to
  do: no cup in the cache says 22 (online or not, so a cup that was off
  during the undo is brought back whenever it returns, five minutes or
  five hours later) and every cup on 9 that pi5 has heard since it
  started has reported more than `RENUM_SETTLE_S` (5 s, over two report
  cycles) after the undo; then it is settled and dropped. A cup on 9 in
  the cache that has not been heard since pi5 started (a record restored
  from an old database) cannot be one the renumber moved, so it holds
  nothing up (on DevPi, 2026-10-14, such a record held an undo for good). When 22 is a replacement again (9 scratched with
  22 again, or another horse with 22) it is dropped at once. A restart
  keeps it (the table) and restarts its clock: any report after the
  restart counts.
- **An undo leads.** While an undo is bringing a cup back to 9, a record
  whose `from` is 9 waits: undo 9 -> 22 and scratch 9 -> 23 within a
  second, and a cup still on 22 gets `[22, 9]` first and `[9, 23]` only
  once it reports 9 (two cups on one number: each gets the pair that fits
  what it says).
- **Chains go in one packet.** A pair whose `from` is the `to` of a live
  pair of the same kind is live too (9 -> 22 and 22 -> 23 both ride while
  a cup says 9; the cup walks them in order).
- **Order and limits.** Undo pairs first, oldest undo first, then the
  records by `was` (chain order). A `from` appears once, a record winning
  over an undo. Pairs that would make a cycle lose the undo pair in it,
  with one warning. At most `RENUM_SLOTS` (4) pairs; more wait for a free
  slot, with one warning.

A stale report is harmless both ways. A cup that has already followed no
longer matches the pair still in the line, and a report made before an
undo can neither acknowledge nor settle it. That was gap review B11: the
old undo pair lived for 60 s or until a cup reported 9, so an undo within
a second or two of the scratch was ended by the cup's own last report of
9, made before the scratch's pair (already on its way) had reached it;
the cup then became 22 and stayed there.

**Acknowledgements.** For every pair, a record's or an undo's, pi5
watches the cups it sees on the pair's `from` that are part of tonight's
picture: heard since pi5 started, and no longer than `RENUM_RECENT_S` (10
minutes) before the scratch or the undo, or since (a cup that turns up
later is watched from then). A cup in the cache that was not, a record
restored from an old database or a cup put away hours ago, still gets the
pair the moment it turns up and reports `from`, but it is never called
late: on DevPi such a record (offline since before the scratch) drew a
warning for the scratch 19 -> 21. Ten minutes rather than "heard since
the scratch or undo": that would miss a cup switched off just before an
undo, the very cup the warning is for, and one on when the scratch was
made that went off before it could follow. A watched cup whose report,
newer than the scratch or the undo, says the pair's `to` (or a number
further down a chain in the line) has followed it: an INFO line, `cup
A0:B7:65:12:34:57 followed 22 -> 9 (undo) 1.8 s after it was made`, and a
`renum_ack` event, horse the pair's `to`, detail `{"mac", "pair": [22,
9], "kind": "undo" or "scratch", "delay_s", "horse"}` (`horse`: where
the cup ended up). A cup that leaves `from` for any other number (set by
hand) is simply no longer watched.

**Late.** A watched cup still on `from` `RENUM_LATE_S` (60 s) after pi5
began watching it is late: one WARNING naming it, `cup A0:B7:65:12:34:57
still says 22 61 s after the undo; it should be 9 (it is off)` (or `it is
online but has not followed`), one `renum_late` event (`{"mac", "pair",
"kind", "waiting_s", "online"}`), and an entry in the model's
`renumbering` for as long as it lasts, which puts `⏳ renumbering` on the
admin page's row of the pair's `to` within one 5 s poll. The entry says
since when pi5 has waited (`since`), not for how long, so a late cup does
not change the model every second. The pair is still sent; when the cup
follows, the entry goes (the tag with it, within a poll) and the
acknowledgement is logged with its real delay. The WARNING reaches the
journal; the INFO lines do not (pi5 configures no logging, so only
warnings print): the events table has every acknowledgement.

### Results

The dashboard's `POST /api/results` (main.py) writes
`pi5/data/results.json` (`{"win","place","show","next","timestamp"}`): the
real finish, the horses that crossed 1st, 2nd and 3rd, and in `next` the
finishers after SHOW, 4th first, recorded when one of the three had no bets
at the post so that La Quiniela can skip it for the next finisher (its payout
computation, M6; `finishers_needed()` is the rule, and the route refuses a
record that stops short). One parser reads the file (`_results_file()`) and
two views of it are served. The board reads `read_results()` on every
`refresh()` (a missing or broken file, a number outside 1..24 or a horse
named twice all read as no results) and puts the three numbers into the
state line's `res`, so in WINNER and AFTER_PARTY the named cups show their
WIN / PLACE / SHOW frame; the model carries them as `results`: `{"win": 19,
"place": 1, "show": 22}`, or `null` while none is named: until M6 the board
shows the real finish. (The dashboard names all three at once. A hand-made
file naming only some gives the dict with `null` for the rest, and the state
line's `res` a 0 there.) `BettingBoard.results()` (`read_record()`) is the
whole record, `{"win", "place", "show", "next"}`, or `None` unless all three
places are named: the one way to read the result in this process, which is
where La Subasta's payouts come from. **Reset betting** removes the file and
clears them, and so does the dashboard's RESET (the panel's, and RESET in the
SET WINNERS dialog with a result saved, after a question); nothing else does.
pi5 keeps the file when it starts (it used to delete it), so a restart in
WINNER comes back with the results, the cups' frames and the TV's results
screen. The file is the single store: the dashboard writes it (whole or not
at all: a temporary file flushed to the card and renamed over it), La
Quiniela and La Subasta read it.

**The field is frozen while a result stands** (Joey, 2026-10-20): payouts
are derived from the result and the field as they stand, so neither may
move under the other. `POST /api/quiniela/scratch` and `/unscratch` answer
409 `The results are in, so the field is frozen: clear them in SET WINNERS
first (RESET in the dialog), then scratch, then enter them again.` until the
result is cleared; names stay editable. And the field is never guessed the
other way round: `POST /api/results` refuses the results (400, nothing
saved) while La Quiniela is not running or its store could not read its
database at start (`HorseStore.load_failed`: its field would be the default,
not the operator's), where it used to check them against posts 1-20.

**The LED rule.** The results are facts about the race, not about the LEDs.
`POST /api/results` saves them first, always, and sets WINNER with them;
only then does it tell the LED controller (`RESULTS:FINALIZE`, the winners'
chase settling into the heartbeat: the three cups were already locked as
they were picked). The command is handed to the wall's worker
(`communication/sender.py`: nothing in pi5 waits on the wall), its reply
carries `"leds"`, the wall's health as last known (`online`, `unreachable`,
`unknown`, `off`; the Control Center's pill says the same), and the results
stand either way. `success` is false only when the file could not be written
(500, `results not saved: ...`); then nothing moves.

#### The results board

The splash's pages say themselves when the board has the screen (`/board`
always, `/slides` from AT THE POST through WINNER). What it shows in WINNER
depends on `results` alone:

- `null` (HEARTBEAT, or a `state 5` curl, before SET WINNERS):
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
  winners' cups emptied for the draw, AT THE GATE pressed again), through
  AFTER_PARTY and back to WINNER (HEARTBEAT after RESET), and a restart of
  pi5 (the store loads them).
- **Dropped** by Reset betting and by a state that reopens betting: 0
  PRE_RACE, 1 BETTING_OPEN or 2 FINAL_CALL (the dashboard's WELCOME,
  STANDBY, 60 MIN, 30 MIN, FINAL CALL; Joey, 2026-10-13: going back to FINAL
  CALL after the post reopens betting, so a late bet is counted at the next
  close, which takes fresh figures). Only with the host's yes (Joey,
  2026-10-28): `set_race_state` refuses such a state while they are held
  unless the request says `reopen: true` (`ReopenRefused`: a 409 on `POST
  /api/quiniela/mode` and `/cmd` that names the figures and the count), so a
  stale page or a curl cannot drop them. From 3 to 6 (AFTER PARTY too) the
  dashboard asks first, naming what it would clear (`at_post` in `GET
  /api/quiniela/mode`), and sends `reopen: true` only after the yes; the admin
  page's Reset betting, the other way out, asks and names them as well.
  Logged as `{"closing": null}`, the
  record marked `"betting": "reopened"` with `"dropped"` (below). TEST,
  the dashboard's LED test, sets no race state and drops nothing.

The live fields keep following the cups underneath (that is the truth about
the cups, and what the admin page shows: the tokens always, the pot and the
prizes until a hand count is entered, below); nothing on the TV reads them in
3, 4 and 5. So a TV page loaded after the cups were emptied, a second
screen, a phone on the board or on `/api/quiniela`, and a restart of pi5 or
of the splash all show the numbers at the post. The page falls back to its
own freeze (what it showed when betting closed) only for a model without
`closing`, which is an older pi5. The runbook's written-down figures are now
a one-line backup, for a close that was reopened before the draw was paid.

#### The counted pot (the hand count)

The scales are "totals based on cheap Chinese electronics". After betting
closes the host counts the cash box's BETS compartment and enters the
dollars, and from then on **the pot and all three prizes come from that
number**, on the TV and on the admin page, with the same split and the same
whole-dollar rounding. Bets per horse stay exactly as the scales read them.
It is what makes the crawl's `FINAL RESULTS HAND COUNTED` true.

- **Stored inside the figures at the post.** The count is one more key of
  the `lq_closing` record, `pot_counted` (whole dollars, 0 or more; the key
  is absent while there is no count), so the two lifecycles cannot drift
  apart: it is saved when it is entered, held through 4, 5, 6 and a restart
  of pi5 (the store loads the record), and dropped with the figures by
  Reset betting and by 0, 1 or 2 (betting reopened). A
  record without the key, which is every record from before this, has no
  count. A stored value that is not a whole number of dollars is ignored.
- **One function.** `prizes_for(pot, split)` is the only place a prize is
  worked out (PLACE and SHOW rounded half up, WIN the rest). The scale pot
  and the count both go through it, so a count of 154 gives WIN 92 / PLACE
  39 / SHOW 23 and a count of 152 gives 91 / 38 / 23, always summing to the
  pot. Nothing downstream recomputes them: the admin page and the board's
  script only read the model.
- **In the model** ("The model", below): `closing.pot` and `closing.prizes`,
  which the TV paints from the post on, are the count's, and `closing` keeps
  its five keys. The model's own `pot` and `prizes` are the count's from the
  post to the end (3 to 6). In FINAL CALL (2) betting is open again and the
  figures and the count are gone (OD1), so they are the live ones. Three keys are
  new: `pot_scale` (the scale pot frozen at the post), `pot_counted` (the
  count, an int, or `null`) and `hand_counted` (whether the model's pot and
  prizes are the count's, which is what puts the tag on the board). Clear
  puts the scale figures back, key for key.
- **Entered with** `PUT /api/quiniela/counted_pot` (Routes, below):
  `{"amount": 152}` sets it, entering again overwrites, `{"amount": null}`
  clears it. Whole dollars from 0 to `COUNTED_POT_MAX` (10000, a ceiling for
  typos), anything else is a 400 with a plain message. It is refused with a
  409 when the race is not in 3 AT_THE_POST, 4 RUNNING or 5 WINNER, or there
  are no figures at the post yet; in 6 AFTER_PARTY a saved count stays and
  is read-only. The model is rebuilt and pushed to the stream at once, so
  the TV does not wait for a poll; each entry is logged ("The log"), and
  the reply says `"saved": false` when the database refused the write (the
  count is held, but a restart would lose it).
- **On the board** the pot wears a small `HAND COUNTED` tag
  (`splash_display/README.md`, "The hand count's tag").

### Admin page

`GET /quiniela/admin` (on pi5, port 5000, so `http://joeydevpi.local:5000/quiniela/admin`)
is one plain page for a phone or the iPad, the race-night surface for
everything but the race state, which changes on the dashboard (the Control
Center): `RACE_NIGHT.md` runs the night from both and keeps the curls for
its appendix. Top to bottom:

- **Race**: `LINK OK` / `NO LINK` and the cups online; the race state,
  read-only, as `3 · AT THE POST` (PRE-RACE · BETTING OPEN · FINAL CALL · AT
  THE POST · RUNNING · WINNER · AFTER PARTY) from the model's `race_state` on
  the page's 5 s refresh, and `cannot reach pi5` (or pi5's error) instead
  when the model cannot be read, never the last state; under it a link to
  the Control Center (`/`), where the state is changed, its mode buttons
  running the LEDs with it; the figures, Pot /
  WIN / PLACE / SHOW / Bets, big enough to read at arm's length (Bets is the
  tokens in the pot; tokens in scratched cups are counted beside the label);
  **Reset betting** behind a `confirm()` that names what it clears (the results, the figures at the post and the hand count while they are held), calling `POST /api/quiniela/reset`
  and reporting `Reset. Pot $N (M tokens still in the cup(s) of horse(s) ...)
  · K cups online` (only in 0 or 6: in 1–5 pi5 answers 409 and the page,
  asking nothing first, shows its reason in red); and **Horses**, one row per horse 1..24 from the model
  and nothing to click: the number in its cloth colour, the name, the status
  (`● online`, `● offline`, `○ no cup`, or `⚠ 2 CUPS` when more than one cup
  claims the horse; an offline row says under it when its cup was last
  heard, `heard 40 s ago`, `heard 3 min ago`, or a date such as `heard
  Sep 20` for a record restored from an old database, a ghost rather than
  a dead cup, from the model's `heard`), the tokens, and `WIN` / `PLACE` /
  `SHOW` / `SCR` tags from the results and the scratches, and `⏳
  renumbering` (amber) on the row a cup should have renumbered to while
  that cup is a minute late (the model's `renumbering`; the tag's tooltip
  says `cup <MAC> still says 22 (off)`; "Renumber pairs: derived and
  acknowledged", above). The line above it reads `N cups online
  · M with no horse` (`cups_online` and `cups_no_horse` from the model; a cup
  showing `NO HORSE` is counted there and appears in no row). There is no
  cups table, no picker, nothing to adopt and nothing to forget: a cup's
  number is set on the cup.
- **Counted pot**, in the Race section under the figures, in states 3 to 5
  (read-only in 6 while a count is saved; not shown in 0 to 2, before the
  figures at the post exist): the scale pot at the post, the counted pot and
  the difference (`+$2`, `−$2` or `same`; information only, nothing blocks), a
  number box with the phone's numeric keypad, a big **Save count**, **Clear**
  and a reply line (`Saved: counted $152 (the scales said $154, −$2). WIN
  $91 · PLACE $38 · SHOW $23.`, the server's error verbatim in red). The
  Pot above it reads `Pot · scale` from the post on (the scales' live figures)
  and `Pot · hand counted` once a count is saved, when Pot, WIN, PLACE and
  SHOW are the count's: the page shows the model's `pot` and `prizes` and
  works nothing out.
- **Race info**, first of the setup sections: the race's name (empty for
  KENTUCKY DERBY), the date and the post time as they read on the race's
  clock (`LQ_RACE_TZ`, Central), **Save race info** with its reply line
  (`Saved · post 5:57 PM CDT`, the server's error verbatim in red), and the
  line above it showing what is stored. Both date and time empty clear the
  post time (the TV's countdown slide then leaves the slideshow). Read from
  `GET /api/quiniela/race` at load and after a save; `PUT` to save.
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
  into rebuilt rows; only what the operator picked or typed comes over, so a
  row's default number follows the model (after an Undo the cup still says
  21 for a report or two, which makes 21 in use; once it is back on 19
  every untouched row offers #21 again). A replacement name typed with its
  number in front (`#21 Test`, `21 Test`, `21. Test`, `21 - Test`) with #21
  picked is `Test`, in the confirm step and the record; another horse's
  number in front is refused in the row, nothing sent: `You typed #22 but
  #21 is selected. Pick #22 in the list, or take it out of the name.`

Everything refreshes every 5 s from `GET /api/quiniela` alone, and every
button reports into its own status line (the pressed row's, for a scratch),
errors verbatim in red, network and non-JSON failures named. Vanilla JS, no
CDN.

**A page left open across an update reloads itself.** The iPad's Home
Screen app keeps this page for days, and a page loaded before a pull goes
on running its old script: on DevPi on 2026-10-14 the model listed a late
cup for three minutes and the page, from before `7b53c2c`, had no tag to
show it (the same flow on the rig, page loaded fresh, shows it within one
poll). pi5 fingerprints the pages it serves (`PAGE_REV` in
`pi5/page_rev.py`: a hash of every template and static JS and CSS file,
read once at start, as Flask reads a template), renders it into the page
(`<meta name="ddm-page-rev">`) and sends it with every response
(`X-Page-Rev`); `static/js/page_rev.js` compares the two on each model
read, the one implementation the Control Center uses too (off its 5 s
poll). When they differ the page reloads itself, but only while idle:
nothing focused, nothing typed since the last action that went through,
no Confirm scratch open. Otherwise the line at the top says `pi5 has a
newer version of this page: it reloads when you're done here.` and the
next idle poll reloads it. A restart that changed nothing reloads nothing.
The same revision is the `?v=` of every static URL, and every HTML page
pi5 serves is `Cache-Control: no-store` (one `after_request`,
`page_rev.init_app`).

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
`$`, the crawl's marks and the degree sign of its weather (`88°F`), none of
which the dashboard prints itself.
Its results tote prints the winners' names and its SET WINNERS pickers list
the field in program-number order (`GET /api/quiniela/field`, by post, which the
page sorts by horse), each as `19 · GOLDEN TEMPO`: `22 · OCELLI` stands at the end
after The Puma's scratch, a horse scratched with no replacement is not offered and
leaves no blank. A pick lights the LED cup of the post and
records the horse, so `pi5/data/results.json` holds horse numbers, which is
what the cups and the TV board are told. Race Setup is gone altogether: its
store (`data/race_setup.json`), `GET`/`POST /api/race-setup` and its three
odds-polling routes were a second copy of race information that La Quiniela
never fed (the TV's countdown slide counted down to last year's race, its
roster slide showed that store's field). The race's name and post time live
in La Quiniela's store and the odds poller is La Quiniela's (next section);
`GET /api/race` is built from them.

### Who reads the store

The store (names, both kinds of scratch, the race) has three readers, and
none keeps a copy. The **board** builds its model from it (`_name_horses()`:
`name`, `in_field`, `replaced`, `scratches`), and the dashboard's results
tote and SET WINNERS pickers read it through `GET /api/quiniela/field`. The
**splash** (the TV's board, roster and countdown slides, the crawl) reads
that model over HTTP and the event stream. **La Subasta** reads it in this
process through `HorseStore.field()` (one snapshot under the store's lock:
`names_rev`, every horse `in_field` by program number with its upper-cased
name and the horse it `replaces`, the scratch records, and `degraded`, true
when the store could not read its database at start), so the auction
sells the field as it stands, a replacement under its own number (22, not
9), and its `horse_id` is the program number. It also listens: the store
calls every listener given to `HorseStore.add_listener()` after each write
(after `on_change`, the board's `wake()`, outside the lock, on the writer's
thread), and La Subasta's applies a scratch to the auction there and then by
its own rule (bids voided while it is open, the ownership too after the
lock). A scratch is entered once, here; undo puts the horse back in the
field and gives it back what the scratch voided, its bids and, after the
lock, its owner (`DDM_La_Subasta_Spec.md`, "Horses: from La Quiniela's
store" and "Scratches"). La Subasta reads the race's result the same way:
`BettingBoard.results()` (the record SET WINNERS wrote, "Results" above),
from which `la_subasta/payouts.py` derives the payout ledger whenever it is
read; it keeps no result and no payout rows of its own.

### The race and the track's odds

La Quiniela's store is the one home of race information:

- **The race** (`lq_race`): its name (typed on the admin page; KENTUCKY
  DERBY while none is), its year and its post time, stored as unix seconds
  and entered and shown on the race's clock, `LQ_RACE_TZ`
  (`America/Chicago`): the admin page sends `2027-05-01` and `17:57`, the
  model says `5:57 PM CDT` and carries the instant, so the TV counts down to
  it whatever its own clock's zone. The year follows the post time. Reset
  betting never touches any of it. `racetime.py` does the arithmetic with
  `zoneinfo`; a machine without a time zone database (Windows without
  `tzdata`) gets the US zones' rules written out (daylight time from the
  second Sunday in March to the first Sunday in November), anything else
  UTC with a warning.
- **The track's odds** (`odds.py`), for the slideshow's roster slide only (La
  Quiniela pays no odds): the poller that used to live in `main.py`, started
  and stopped by hand as before (`POST /api/quiniela/odds/start`, `/stop`),
  asks Claude with web search for the current odds of the race the store
  names, keyed by **program number** (21..24 included: an also-eligible that
  draws in keeps its number, and its odds). They reach the model as
  `horses[n].odds`, a string, or null for a horse with none; they are kept
  in memory only, so a round that fails, a machine without
  `DDM_ANTHROPIC_API_KEY` in `pi5/.env` (an unprefixed `ANTHROPIC_API_KEY`
  works too) or a party with no internet leaves them as they were
  or null, and nothing errors. `PUT /api/quiniela/odds` sets them by hand
  (the program's morning line). Each successful round is also emitted as
  the spectator page's `odds_update`.
- **The weather**, for the TV's crawl (`DALLAS 88°F SUNNY`): the
  dashboard's own source, WeatherAPI.com through `main.py`'s cache
  (`weather_data()`, which `GET /api/weather` answers from too, so the API
  is asked at most once per `WEATHER_CACHE_MINUTES`). A thread started in
  `main.py`'s `__main__` when `DDM_WEATHER_API_KEY` is set in `pi5/.env`
  (without it pi5 says once at start that the weather line is off)
  (`feed_weather_to_board()`) hands the current conditions to the board
  every `WEATHER_CACHE_MINUTES`, 5 at least; the model carries them as
  `weather`. In memory only; a failed fetch keeps the last weather, and
  with no key there is none and the crawl leaves the item out.
- **The old file, once**: when pi5 starts (`main.py`'s `__main__`,
  `migrate_race_setup()`), if `data/race_setup.json` exists the log says it
  is obsolete, and the first time only (`lq_race.migrated`) its post time is
  copied into the race info when that is empty (a time of day, which Race
  Setup took as on Derby day 2026 on Churchill's clock: `18:57` is 6:57 PM
  EDT, 5:57 PM CDT) and its names when the store has none at all. The file
  is left where it is.

`GET /api/race`, for anything that wants a race roster (the splash's slides
read La Quiniela's model itself), keeps the shape it always had, every field
now La Quiniela's:

| Field | From |
| --- | --- |
| `race_state` | La Quiniela's race state: 0-3 `pre-race`, 4 `running`, 5-6 `post-race`; `unknown` while no horse in the field has a name (the mock racing service is not consulted any more) |
| `post_time` | the race's `post_local`, `5:57 PM CDT`; `""` while no post time is set |
| `post_time_iso` | the same instant in ISO 8601 with the race clock's offset, `2027-05-01T17:57:00-05:00`; `""` likewise |
| `horses` | the field (`in_field`), in numeric order, each under its own program number: `number`, `name` as typed (a horse with no name is left out), `odds` (`horses[n].odds`, by program number, or null), `finish` (1 / 2 / 3 from the results, else null) |
| `winner` | the WIN horse's number from the results, or null |
| `last_updated` | now, UTC |

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
| `GET /api/quiniela` | The model JSON below, `Cache-Control: no-store`, and `X-Page-Rev` (as every pi5 response): the revision of pi5's pages ("Admin page", the page left open across an update). |
| `GET /api/quiniela/stream` | Server-sent events, `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`. The first chunk is `data: <model>\n\n`; every published model follows as another `data:` chunk; after 5 s of silence a `: heartbeat` comment plus `event: ping\ndata: {"ts":<unix>}\n\n`. Each subscriber has a 32-deep queue and the oldest model is dropped when it is full. |
| `POST /api/quiniela/cmd` | Body `{"cmd": "state 1"}`, and `"reopen": true` to confirm a state 0-2 while the figures at the post are held (a 409 without it); see below. |
| `GET /api/quiniela/mode` | The one race state and the dashboard mode that set it: `{"ok": true, "state": 1, "state_name": "BETTING_OPEN", "mode": "BETTING_60", "label": "60 MIN", "source": "dashboard", "at_post": null, "modes": {...the table...}}`, `Cache-Control: no-store`. `at_post` is `{"pot": <the scale pot at the post>, "pot_counted": <the hand count or null>}` while figures at the post are held: what the dashboard's reopen confirm names. `mode` and `label` are null when the state was set directly (`source` `"cmd"`: the admin page or `state N`; `"reset"`: Reset betting) or not since pi5 started (`source` null). |
| `POST /api/quiniela/mode` | `{"mode": "BETTING_60"}`: a dashboard mode; the race state is the table's. A mode that reopens betting (60 MIN, 30 MIN, FINAL CALL, WELCOME, STANDBY) while the figures at the post are held is a 409 naming what it would clear (`This reopens betting: the figures at the post (pot $86) and the hand count ($80) would be cleared. Send "reopen": true to confirm.`), unless the body also says `"reopen": true`; the dashboard sends it only after the host confirms. `{"ok": true, "mode": "BETTING_60", "state": 1, "state_name": "BETTING_OPEN", "source": "dashboard", "rev": R, "gateway_online": bool}`. 400 `unknown mode ...; one of ...`, 503 `bridge not initialised`. Sets the race state only: the LEDs are the button's own request. |
| `GET /api/quiniela/horses` | `{"1": {"name": "Encino"}, ..., "24": {"name": ""}}`, names **as typed** (the model upper-cases them). |
| `PUT /api/quiniela/horses` | Body `{"text": "1. Dornoch\n2. Sierra Leone\n...\n22. Ocelli"}` (up to 24 lines) or the GET shape (`name` required per entry; a `replaced` key, the 726d4c2 shape, is ignored). Only the horses given are touched. 400 with the parse message. Returns `{"ok": true, "names_rev": N, "horses": {...}}`. Text rules: one name per line in program order (lines 21..24 are the also-eligibles); a leading `7.` / `#7` / `7)` / `7:` / `22.` names that horse instead, a bare `7` clears it, a blank line leaves it alone; a bare `7 Name` (number, space, name) is a prefix only when every non-blank line is numbered, so in a plain list `8 Belles` is a name. |
| `POST /api/quiniela/scratch` | `{"horse": 9, "replacement": {"number": 22, "name": "Ocelli"}}` -> the renumber (kind 1): `{"ok": true, "kind": "replacement", "cup": "<MAC of the cup saying 9, or null>", "renum": [9, 22], "rev": R, "gateway_online": bool, "names_rev": N, "was": {"number": 9, "name": "Encino"}, "now": {"number": 22, "name": "Ocelli"}}` (names as typed). `name` is optional (22 keeps its stored name). 400 `horse 9 is not in the field`, `horse 9 is already scratched`, `22 is in use`, `replacement number must be 1-24`, `replacement must be {"number": N, "name": "..."}` (a bare string or any other shape); 409 `The results are in, so the field is frozen: ...` while a result stands (either kind, and `unscratch`). `{"horse": 9}` (or `"replacement": null`) -> kind 2: recorded (`was` 9, `now` NULL) whether or not a cup says 9, the bit in the state line: `{"ok": true, "kind": "gateway", "horse": 9, "cup": "<MAC or null>", "scratched": true, "refund": false, "rev": R, "gateway_online": bool, "names_rev": N, "was": {"number": 9, "name": "Encino"}}`. From AT THE GATE on (race states 3 to 6, `POST_HELD_STATES`) the field is set and a scratch has no replacement (Joey, 2026-10-23): a replacement is refused, 409 `The field is set at the gate: from AT THE GATE on a scratch has no replacement, and the tokens in its cup are refunded in cash. Scratch it with no replacement.`, and a scratch with no replacement answers `"refund": true`. Both work without a bridge (recorded, `cup` null, `rev` null). |
| `POST /api/quiniela/unscratch` | `{"horse": 9}` reverses either kind: if 9 is the `was` of a record, the record is removed (22's name goes back to what it was before the scratch: cleared if the scratch gave it, kept if it was entered ahead), `names_rev` bumps and the undo is kept (`lq_renum_undo`): the pair `[22, 9]` goes down whenever a cup reports 22, with no time limit, until the cups have settled on 9 ("Renumber pairs: derived and acknowledged", above): `{"ok": true, "kind": "replacement", "cup": "<MAC of the cup saying 22, or null>", "renum": [22, 9], "rev": R, "gateway_online": bool, "names_rev": N, "was": {...}, "now": {...}}` (400 `horse 9: undo 22 first` while a record 22 -> 23 stands: a chain is undone last record first; 409 `Cup 9 holds 4 tokens bet on #22. Hand them back and empty the cup, then Undo.` from 60 MIN to WINNER while 22's cup holds tokens: they are bets on 22 and would go with the cup to 9 (`BETS_STAND_STATES`; Joey, 2026-10-28)); else the kind 2 undo: the record goes and the bit leaves the line, `{"ok": true, "kind": "gateway", "horse": 9, "cup": "<MAC or null>", "scratched": false, "rev": R, "gateway_online": bool, "names_rev": N}`; 400 `horse 9 is not scratched` when neither applies. |
| `GET /api/quiniela/field` | The field by post, for the dashboard's SET WINNERS pickers and its results tote: `{"names_rev": N, "posts": [{"post": 9, "horse": 22, "name": "OCELLI", "label": "22 · OCELLI", "replaces": 9}, ...], "names": {"1": "DORNOCH", ..., "24": ""}}`, `Cache-Control: no-store`. A post is a place on the mantle, 1..20, and the LED cup there. `posts` has one entry per post somebody runs from, in post order (the SET WINNERS page sorts them by horse for its grid): the post's own horse, or the one standing in for it (the cup was renumbered and nothing moved, so 22 runs from post 9 and `replaces` says so; a chain 9 -> 22 -> 23 gives 23); a post whose horse was scratched with no replacement has no entry. Names are upper-cased, `""` where none is stored (the label then says `HORSE n`); `names` carries all 24. Works without a bridge. |
| `PUT /api/quiniela/counted_pot` | The host's hand count of the cash box: `{"amount": 152}` sets it (whole dollars, 0 to 10000; again overwrites), `{"amount": null}` clears it. `{"ok": true, "pot_counted": 152, "pot_scale": 154.0, "pot": 152.0, "prizes": {"win": 91, "place": 38, "show": 23}, "hand_counted": true, "race_state": 3, "saved": true}`. 400 `the count must be a whole number of dollars, 0 to 10000 ...` for anything else (a float, a string, a bool, a negative, over the ceiling) or a body with no `amount`; 409 when the race is not in 3, 4 or 5 (in 6 a saved count is read-only) or there are no figures at the post yet. See "The counted pot". |
| `GET /api/quiniela/race` | `{"ok": true, "race": {...the model's race...}, "name": "Kentucky Derby", "date": "2027-05-01", "time": "17:57"}`: the name as typed, the post time's date and time on the race's clock (null while unset), what the admin page's form shows. `Cache-Control: no-store`. |
| `PUT /api/quiniela/race` | `{"name": "...", "date": "YYYY-MM-DD", "time": "HH:MM"}`, any part left out left alone; date and time on the race's clock and together (400 `date and time go together ...`), both `""` clear the post time and the year. Or `{"post_at": <unix time> or null}`. 400 for a date or time that does not parse, a name over 80 characters. Returns what GET does. |
| `GET /api/quiniela/odds` | `{"ok": true, "odds": {"1": "5-2", "22": "30-1"}, "polling": bool, "interval": 300, "last_update": "...Z", "next_update": "...Z"}`: the odds the model carries, by program number, and the poller's state. |
| `PUT /api/quiniela/odds` | `{"odds": {"1": "5-2", ...}}` replaces them all (by hand: the morning line), `{"odds": null}` clears them. Numbers 1..24, non-empty values of at most 7 characters, upper-cased; the rest dropped. |
| `POST /api/quiniela/odds/start` | `{"interval": 300}` (optional; 60 at least): the poller runs. 503 `ANTHROPIC_API_KEY not configured`, 409 when it already runs. |
| `POST /api/quiniela/odds/stop` | `{"ok": true, "stopped": bool}`. |
| `POST /api/quiniela/reset` | The between-races reset, `reset_betting()`: PRE_RACE and the results cleared (the dashboard's file too) in one state line, the scratched bits and renumber pairs kept; the closing figures (`closing`) cleared, the ticker cleared, the cups' current counts the new baseline so nothing shows as a bet; names and both kinds of scratch untouched; the cups keep their numbers, which are theirs. Tokens still in a cup are not an error, the pot reads them: `{"ok": true, "race_state": 0, "pot": 15.0, "total_tokens": 15, "horses_with_tokens": [9, 21], "cups_online": 20, "events": 0, "closes_at": null, "rev": R or null, "gateway_online": bool, "names_rev": N}`. Works without a bridge (`rev` null). Only before the race (0) or after it (6): in 1–5 a 409, `{"ok": false, "error": "Reset betting works only before the race (0) or after it (6). Change the race state on the Control Center."}`, and nothing moves (Joey, 2026-10-13: the Control Center stays the one place that changes the race state). `reset_betting()` itself does not check; the route does. |
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
curl -X PUT  localhost:5000/api/quiniela/counted_pot -H 'Content-Type: application/json' -d '{"amount": 152}'
curl -X PUT  localhost:5000/api/quiniela/counted_pot -H 'Content-Type: application/json' -d '{"amount": null}'
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
 "prizes": {"win": 20, "place": 8, "show": 5},
 "split": {"win": 0.6, "place": 0.25, "show": 0.15},
 "chyron": ["TOTALS BASED ON CHEAP CHINESE ELECTRONICS - FINAL RESULTS HAND COUNTED",
            "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS"],
 "names_rev": 4,
 "scratches": [{"was": {"number": 9, "name": "ENCINO"}, "now": {"number": 22, "name": "OCELLI"}},
               {"was": {"number": 20, "name": "SOCIETY MAN"}, "now": null}],
 "cups_online": 20, "cups_no_horse": 0,
 "results": null,
 "payout": null,
 "closing": null,
 "pot_scale": null, "pot_counted": null, "hand_counted": false,
 "race": {"name": "KENTUCKY DERBY", "year": 2027, "post_at": 1809212220.0, "post_local": "5:57 PM CDT",
          "tz": "America/Chicago"}}
```

Every horse also carries `"odds"`: the track's odds for that program number
(`"5-2"`), or null.

Once betting has closed, `closing` is the figures at the post, the live
fields' shapes plus `at`:

```json
"closing": {"pot": 33.0, "prizes": {"win": 20, "place": 8, "show": 5}, "total_tokens": 33,
            "horses": {"1": {"tokens": 0}, "7": {"tokens": 23}, "...": "...24 entries...", "22": {"tokens": 10}},
            "at": 1777664400.2}
```

Once SET WINNERS has a result and the figures at the post exist, `payout` is
La Quiniela's payout from them (`payout_for`; gap review M6, OD3: the rule and
the examples in `DDM_La_Quiniela_Spec.md`), what the LQ admin page's RESULTS
section and the TV's results screen show; `null` otherwise:

```json
"payout": {"pot": 154, "pot_source": "counted", "prizes": {"win": 92, "place": 39, "show": 23},
           "pays": [{"slots": ["win"], "horses": [{"horse": 3, "prize": 92}], "skipped": [7], "dead_heat": false},
                    {"slots": ["place", "show"], "horses": [{"horse": 5, "prize": 31}, {"horse": 12, "prize": 31}],
                     "skipped": [], "dead_heat": true}],
           "skipped": [7], "unpaid_to_win": 0, "refunds": [{"horse": 9, "tokens": 4, "refund": 4}]}
```

`pot_source` is `counted`, `scale` or `scale less refunds`; `pays` has one entry per prize paid, in finishing
order, `slots` the prize(s) the entry pays (two or three in a dead heat, pooled), `horses` the cups to draw
from with each one's share (lowest number first), `skipped` the no-bets finishers crossing in or ahead of
it; `unpaid_to_win` the prizes of positions nobody with bets filled, added to the first pool; `refunds` the
cups scratched after the post, each refunded at token value.

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
  Once the host has hand counted the cash box, `pot` is the count instead
  (`hand_counted`; "The counted pot", above).
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
- `board_states`: `QUINIELA_BOARD_STATES`. The splash's pages have their own
  sets; the board reads only that this list is not empty, as the sign that
  the model is pi5's and not the splash's empty one after a restart.
- `now`: the server's clock, stamped when the model is serialised (`GET
  /api/quiniela`, every SSE chunk), never stored and never part of the
  changed-comparison, so the stream stays quiet between real changes. The
  TV's crawl counts the time to post against it rather than its own clock.
  There is no time for betting to close: the race states are the host's to
  call, and the board shows the state (the betting-closes time and the TV's
  CLOSES IN went on 2026-10-30).
- `prizes`: `{"win", "place", "show"}` whole dollars summing to `pot`, by the
  rule above (from the count when the pot is hand counted). `split`: the
  three fractions from config.
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
  "now": null}`, each with `rebet`: whether its cup's tokens are handed back
  to re-bet (Joey, 2026-10-14: no replacement always; a replacement when it
  was entered while betting was open, `BETTING_OPEN_STATES` 1-2, the race
  state at entry kept in `lq_scratch_entered`; a record from before that
  table reads as before), and `refund`: whether they are refunded in cash
  instead (Joey, 2026-10-23: the scratch was entered in race states 3 to 6,
  `POST_HELD_STATES`; then `rebet` is false). The model's `betting_open` is the
  same one test, for the admin page's confirm step; the TV adds `· RE-BET YOUR
  TOKENS` to a replacement whose `rebet` is true and to a scratch with none,
  and `· TOKENS REFUNDED` to a scratch whose `refund` is true. A renumber never changes `pot` (the cup keeps counting
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
  betting and by 0, 1 or 2 ("The figures at the post", above). The TV reads
  them in 3, 4 and 5; the live `total_tokens` and tokens keep following the
  cups, and so do `pot` and `prizes` until a hand count is entered, which
  makes `closing`'s pot and prizes (and, from the post to the end, the
  model's own) the count's. Still five keys: the count is not one of them.
- `pot_scale`: the scale pot frozen at the post (`closing`'s pot as it was
  taken, whatever a count says), or `null` while there are no figures at
  the post.
- `pot_counted`: the host's hand count in whole dollars, or `null` (none
  entered, or no figures at the post). It is held with the figures, so
  FINAL CALL (2), which drops them (OD1), drops it too.
- `hand_counted`: whether the model's `pot` and `prizes` are the count's: a
  count is held and the race is in 3, 4, 5 or 6. It is what puts the `HAND
  COUNTED` tag on the TV's pot, and the only one of the three keys the TV
  reads.
- `race`: the race from the store ("The race and the track's odds",
  above): `name` upper-cased (KENTUCKY DERBY while none is stored), `year`
  (the stored one, else the post time's; null), `post_at` (unix seconds or
  null), `post_local` (the post time on the race's clock, `5:57 PM CDT`, or
  null), `tz` (that clock's zone, `LQ_RACE_TZ`, for a page that shows a
  date or the time of day). The TV's countdown and roster slides read it,
  and the crawl's time of day and time to post (`POST IN 1:14`).
- `horses[n].odds`: the track's odds for program number n, a string, or
  null (no poller, no internet, no odds for that horse). For the roster
  slide only; nothing about La Quiniela's payouts reads it.
- `weather`: `{"location": "Dallas", "temp_f": 88, "condition": "Sunny"}`
  from `main.py`'s feed (above), strings trimmed and cut at 40
  characters, the temperature a whole number, a missing part null; null
  while there is none. The TV's crawl reads it.
- `renumbering`: the cups late on a renumber pair ("Renumber pairs:
  derived and acknowledged", above), `[{"from": 22, "to": 9, "mac":
  "A0:B7:65:12:34:57", "kind": "undo", "since": 1777662850, "online":
  false}]` (`since`: unix seconds, when pi5 began waiting for that cup),
  sorted by `to` then MAC; `[]` while no cup is. The admin page's `⏳
  renumbering` tag reads it.
- `horses[n].heard`: while no cup claiming horse n is online, when the
  cup the row shows was last heard (unix seconds, from the cup cache's
  `last_seen`); null while one is online (each report would change the
  model) or no cup claims it. The admin page's `heard …` under an offline
  status reads it.
- `share` and `leader` stay as they were; nothing new depends on `share`.

### One race state

There is one race state, and the dashboard's modes are its source. The
dashboard's thirteen buttons drive the LEDs, as they always did; twelve of
them also name their mode to pi5 (`POST /api/quiniela/mode`), and La
Quiniela's race state, the one the cups and the TV board follow, is derived
from the mode. TEST, the LED test, names none: it never sets a race state
(Joey, 2026-10-13), and `TEST` is refused as a mode.

| Dashboard button | Mode | La Quiniela state |
| --- | --- | --- |
| WELCOME, STANDBY | `WELCOME`, `STANDBY` | 0 PRE_RACE |
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
mode (`set_mode()`) and `state N` on `POST /api/quiniela/cmd` (tests, the
simulator, a curl). The shared value is the bridge's phase, the one that is
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
- Nothing waits on the LEDs (since 2026-10-21): pi5 hands every LED command
  to the wall's worker and answers at once; a mode button's line names the
  race state (`Animation: FINAL_CALL · FINAL CALL`), and the wall's health is
  the pill's, top left. The results the same: SET WINNERS saves them and sets
  WINNER first (the LED rule, under Results).
- The admin page shows the state read-only, from the model's `race_state`
  within its 5 s refresh, with a link to the dashboard to change it. `state
  N` on the cmd route sets the same value directly and starts no LED
  animation: the simulator, the tests and a last-resort curl use it. The
  dashboard reads the state back every 5 s, and again when a tap could
  reopen betting, so a state set by a curl is what its confirm goes by (it
  has no ticker since 2026-10-13: the TV's crawl is the only crawl).
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
| `state N` (0..6) | `set_race_state(N)`, the path a dashboard mode takes (above), with the same 409 for 0-2 while the figures are held unless the body says `"reopen": true`: the state, with the scratched bits, renumber pairs and results as pi5 has them | `{"ok":true,"rev":R,"phase":N,"gateway_online":bool}` |
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

More keys in `pi5/config.py` (the first ones with the names the splash
display used), each overridable by `DDM_<KEY>` in the environment or
`pi5/.env`. These change the money: set one only on purpose, and in
`pi5/.env`, never by editing the tracked `pi5/config.py`:

| Key | Default | Meaning |
| --- | --- | --- |
| `TOKEN_VALUE` | `1.00` | Dollars per token, for the board's POT (`DDM_TOKEN_VALUE`, a float) |
| `QUINIELA_LOG` | `True` | Write the JSONL log below (`DDM_QUINIELA_LOG`: 1/true/yes/on or 0/false/no/off) |
| `QUINIELA_BOARD_STATES` | `[1, 2, 3, 4, 5]` | The model's `board_states`. The splash's pages decide for themselves when the board has the screen; the board reads only that the list is not empty (pi5 heard), so it must not be emptied (`DDM_QUINIELA_BOARD_STATES`, a comma list such as `1,2,3,4,5`) |
| `LQ_SPLIT_WIN` | `0.60` | The WIN prize's share of the pot; it takes the remainder after PLACE and SHOW (`DDM_LQ_SPLIT_WIN`) |
| `LQ_SPLIT_PLACE` | `0.25` | The PLACE prize's share, rounded half up to whole dollars (`DDM_LQ_SPLIT_PLACE`) |
| `LQ_SPLIT_SHOW` | `0.15` | The SHOW prize's share, likewise (`DDM_LQ_SPLIT_SHOW`) |
| `LQ_CHYRON_LINES` | two lines | What crawls along the bottom of the board (`DDM_LQ_CHYRON_LINES`, lines separated by `\|`) |
| `LQ_RACE_TZ` | `"America/Chicago"` | The race's clock: the post time is entered and shown in it, and the TV shows the time of day in it (`DDM_LQ_RACE_TZ`, a zone name) |

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
renumber pairs as "Renumber pairs: derived and acknowledged" (above)
derives them from the records, the undos and the cups, and the
dashboard's results; when the bridge's `devpi` state differs,
one `set_state()` goes out and the fresh snapshot is applied. So every path
agrees: a scratch made without a bridge reaches the gateway on the first
refresh after one appears, a cup set to a scratched horse tomorrow finds its
bit already in the line, and nothing is ever addressed to a cup. The lock
order is always bridge then board and nothing can deadlock against the reader
thread. `main.py` calls `init_board()` at import and `start_board()` from its
`__main__` block only, after `start_la_quiniela()`; the thread runs even when
the bridge has no port, so the routes answer with `link_ok` false rather than
a stale picture. It creates the odds poller at import too (no thread until
`POST /api/quiniela/odds/start`) and, in `__main__`, runs
`migrate_race_setup()` between the bridge's start and the board's.

### The log

With `QUINIELA_LOG` on, every token, scratch or race-state change appends one
compact JSON line to `pi5/data/quiniela_YYYY-MM-DD.jsonl` (local date,
git-ignored):

```json
{"ts":1777662847.2,"race_state":1,"changes":[{"horse":7,"tokens":[23,24]},{"horse":7,"scratched":[false,true]},{"race_state":[1,2]}],"total_tokens":24}
```

An `online` or `link_ok` flip alone writes nothing. Two marks keep the log
honest about what was not a bet. The between-races reset writes a record
with `"baseline": true` and `"reset": "betting"`, even when nothing moved.
And a count that came with a cup move (the MAC claiming the horse changed,
or went away, or arrived) carries the move in its change, e.g.
`{"horse":22,"tokens":[0,51],"cup":[null,"A0:B7:65:12:34:56"]}`.

The first snapshot after a start carries no mark. It is a baseline for the
ticker (no events), but its record is a plain one: each horse's count shows
as a change from 0, e.g. `{"horse":7,"tokens":[0,23]}`, with no `baseline`
flag and no cup marks. When it holds no changes, nothing is written at all.
Nothing in the log marks the start itself, so a replay cannot tell those
counts from bets by the record alone. A reset that is the very first snapshot
after a start writes `"reset": "betting"` without `"baseline": true`.

The closing figures leave their trace too: taken, `{"closing":{"pot":154.0,
"prizes":{"win":92,"place":39,"show":23},"total_tokens":158}}` beside the
state change that took them, the record marked `"betting":"closed"`;
dropped, `{"closing":null}`. A drop by a reopening state (0, 1 or 2) marks
its record `"betting":"reopened"` with what it dropped,
`"dropped":{"pot":86.0,"pot_counted":80,"total_tokens":86}` (the scale pot,
the hand count or null, the tokens), so a re-close reads as close, reopen,
close; a drop by Reset betting is its `"reset":"betting"` record. A hand count
is a record of its own when it is entered or cleared, `{"pot_counted":
[null,152]}` (before, after), with the race state and the token total. The
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
pair in the line, the cup's tokens under 22, no events, the pot unchanged;
undo at 0.3, 0.6, 1.5 and 2.5 s after the scratch always bringing the cup
back to 9 with its tokens, an undo with the cup off for five minutes and
across a restart, undo then scratch 9 -> 23 within a second ending on 23
with no bounce and both acknowledgements logged, two cups on one number,
a stale report never acknowledging, the late log line and `renumbering`
after 60 s, a record restored from an old database never late and never
holding an undo, the ten-minute rule (heard 11 minutes before a scratch:
not late; 9 minutes: late), a ghost that turns up following its pair,
no model change every second while a cup is late, the four-slot cap; a scratch before any cup says the horse; the in_field rule; the unused-number and not-in-the-field
rejections), the results file into the state line and out again on reset,
the names text parser for 24 lines, the between-races
one race state (each of the dashboard's twelve modes against the table,
`TEST` refused, byte-exact lines, the cmd route setting the
same value, WINNER and the results in one line), the between-races
reset (one byte-exact PRE_RACE line with the bits and pairs kept and the
results cleared, the ticker zeroed, the tokens still in the
cups read and their horses named; a 409 with nothing moved in each of 1–5),
the closing figures (taken on 2 -> 3,
0 -> 3, 1 -> 4 and 1 -> 5, in the very model that first says the closed
state; not moved by later counts, the emptied cups included; kept by 6, by
WINNER again and by a snapshot with no phase; dropped by 2, by 1 and by
Reset betting, a reopen marked with what it dropped; logged,
saved in `lq_closing`, and a database error costing only the copy on disk),
a re-close through the routes (close at $86 and count $80, AT THE GATE
again taking nothing, FINAL CALL clearing both, nine more tokens, the
second close at $96 in the TV's model; the log's close, reopen, close),
a database from before `lq_closing` gaining it at start, the counted pot
(`prizes_for` the only place a prize is worked out, for the scale pot and
the count alike, no other module doing the sum; 154 -> 92 / 39 / 23, a
spread of pots summing to themselves; bets per horse unchanged; held
through 4, 5 and 6, dropped by 0, 1, 2 and Reset betting; refused outside
3 to 5 and with no figures at the post; bad amounts rejected; Clear
putting the scale figures back, key for key; pushed at once, logged,
saved in `lq_closing` and kept by a restart; the route's 400s and 409s; a
database that refuses the write), a restart in
WINNER after the cups were emptied (a new bridge and board over the same
database and results file: WINNER, the results and the closing figures as
they were, the hello answered with them), the `now` stamp, the three tables
with the `lq_horses` and `lq_scratches` migrations and every route on a
Flask test app, the admin page's Race section included (the race state
read-only and no state buttons, the link to the Control Center, the figures,
Reset betting behind a confirm, the Horses list with
its four statuses and no cup controls, and the Race info section), that the
v1 dev routes are gone, and the race and the odds: the race info through
its routes, the model and the database (5:57 PM CDT in May and CST in
January, a unix time instead, the refusals, Reset betting keeping it, the
year following the post time, another `LQ_RACE_TZ`, the daylight-time
change, `lq_race` added to an older database); the odds by program number
(22 standing in for 9 with 22's own), null where there are none, what
`clean_odds()` drops, the PUT and GET routes, and the poller with a fetch of
its own (a round with odds lands in the model and is emitted, a round with
nothing leaves them, no key no start, the thread started and stopped
through the routes); the old Race Setup file copied once and left in
place (its post time and names into an empty store, nothing a second time,
nothing over race info or names already set, an unreadable file warned
about); and the weather in the model (null without any, the place, a whole
number of degrees and the sky, what `clean_weather()` drops, None
clearing it).
`test_smoke` pins `protocol.MAX_HORSE` to `DDM_MAX_HORSE` in `ddm_common.h`
and also checks that importing `main.py` starts no `lq-board` thread and
registers the routes, the admin page included.

```
python -m la_quiniela.test_dashboard
```

The dashboard's side, on the real app (`main.py`, its templates and static
files) with the LED controller stubbed and the tote board off: the menu, the
names on the tote and in the pickers, Race Setup gone (its routes 404, its
CSS removed), the page's CSS and JS asked for with `?v=<mtime>`, `/api/race`
built from La Quiniela (the field and names, the odds by program number,
the post time on the race's clock, the race state and the results), pi5's
weather reaching the model (the feed's thread, and none without a key or
conditions), the degree sign in the dots table, the
thirteen buttons, twelve
carrying a mode (TEST none: pi5 refuses it), the LED command each one sends (unchanged), the race
state each one sets, the results making it WINNER and RESET making it
AFTER_PARTY; the results saved, WINNER in one line and `"leds":
"unreachable"` with the LED wall down (RESULTS:FINALIZE handed to its worker
after the save), a file that cannot be written moving nothing (500), the
page's following the server's results, and
that nothing main.py runs at start removes a file; `POST /api/results`
refusing a missing, null, repeated or out-of-field horse with a 400 that
saves and sends nothing; and, in headless Chrome, the race-night guards:
TEST at the post sending no race state, every reopening button in 3, 4 and
5 asking with the real figures (Cancel sends nothing, Reopen betting once)
and in 0–2 going at once, a re-close through the page, CONFIRM RESULTS
naming WINNER or La Quiniela's reason in red, AUTO asking first at the post
and holding every mode button with `switch to MANUAL`, and Reset betting
refused at the post on the admin page.

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
| `LQ_HEARTBEAT_LOG_S` | `10` | Per-cup heartbeat interval for logging and display refresh |
| `LQ_CUP_OFFLINE_S` | `6` | No telemetry for this long = cup offline |
| `LQ_GATEWAY_OFFLINE_S` | `12` | No line at all for this long = gateway offline |
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
| `reset_link(reason) -> dict` | Forgets the roster and the state, clears `cups.cup_id` and `cups.horse`, deletes simulated cup rows, keeps the history, sends the gateway nothing. Returns the new revs and how many rows were deleted. |
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

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

- **No roster yet** (`roster_rev == 0`): DevPi mirrors the gateway. A MAC's
  `cup_id` is whatever the gateway reports.
- **With a roster**: DevPi is right. A cup the gateway reports differently
  logs a `roster_mismatch` event and triggers a roster re-send; DevPi's
  table is never overwritten. `adopt_roster()` turns the mirrored numbers
  into the first roster without typing 20 MACs.

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

  Each returns `{"success":true,"rev":N}` or a 400 with the validation message.

## Tests

```
cd pi5
python -m la_quiniela.test_smoke
```

No hardware and no real port: a fake serial port feeds the gateway's lines
and captures what the bridge writes, a stub SocketIO records every emit, and
a fake clock drives the timers. Byte-exact roster and state lines are checked
against the README examples.

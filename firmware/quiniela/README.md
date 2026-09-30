# La Quiniela — cup firmware

Firmware for the La Quiniela betting cups (target: DDM 2027).

Each cup is a self-contained node. It shows a horse number on its TFT, reads a
load cell through an HX711 to count the tokens dropped in it, and talks to a
single gateway over **ESP-NOW**. The horse number is the cup's own (protocol
v2, 2026-09-27): it is set on the cup, saved in its NVS and reported in every
packet the cup sends; nothing assigns it from outside. There are no cup IDs and
no MAC roster anywhere.

**There is no router and no WiFi network.** ESP-NOW is a direct radio protocol
between ESP32s. The gateway broadcasts state to every cup at once; each cup
unicasts telemetry back. Nothing associates, nothing gets a DHCP lease, and
nothing depends on venue infrastructure.

```
        DevPi ──USB serial── ESP32 gateway
                                  │
                        ESP-NOW, channel 6
                       broadcast ↓   ↑ unicast
                    ┌─────────┬──────┴───┬─────────┐
                  cup 1     cup 2      cup 3  …  cup 20   (each cup knows its own horse)
```

## Hardware

### Per cup — ESP32-2432S028R ("Cheap Yellow Display" / CYD)

An ESP32 dev board with a 2.8" 240×320 TFT already wired to it. It is sold
as an ILI9341, which at least one batch is not; see [the display controllers](#the-cyds-display-controllers-two-batches)
below before touching the display code.

**Power: 5V + GND into the P1 connector, and only P1.**

> **CN1 is a 3.3V rail. Feeding 5V into CN1 destroys the board.** These two
> connectors are the same physical part and sit next to each other. Check the
> silkscreen every single time before you plug anything in.

**HX711 → CN1:**

| CN1 pin | HX711 |
| ------- | ----- |
| 3.3V    | VCC   |
| GND     | GND   |
| GPIO27  | DT (data) |
| GPIO22  | SCK (clock) |

DT on GPIO27 and SCK on GPIO22 is how the bench cup is wired and what both
`ddm_cup.ino` and `tools/hx711_calibrate/` assume. If a cup ends up wired the
other way round, swap `HX711_DT` / `HX711_SCK` in the sketch rather than the plug.

Power the HX711 from **3.3V, not 5V**. The HX711 drives its data line at
whatever voltage it is powered from, so a 5V-powered HX711 pushes 5V into a
GPIO that is only rated for 3.3V. It may appear to work for a while and then
kill the pin or the chip.

**Connectors:** both P1 and CN1 are **1.25mm Molex PicoBlade**. They are almost
always sold online as "1.25mm JST", which is technically wrong but is the search
term that finds the right part. They are *not* JST-XH (2.54mm) or JST-PH (2.0mm)
— those will not fit.

### The CYD's display controllers (two batches)

The boards are sold as ILI9341 and `Adafruit_ILI9341` drives them fine, but
Board A's controller answers as an ST7789-family part: RDID4 (`0xD3`) reads all
`FF`, RDDID (`0x04`) reads `81 81 B3` after a software reset, and a software
reset does not clear MADCTL or COLMOD. The panel's reset pin is not on a GPIO,
so register state carries over from one sketch to the next. Three things in
the Adafruit init table go wrong on it, and `ddm_cup.ino` corrects all of them
right after `tft.begin()` (verified on Board A, 2026-09-12; the band symptom
was identical on every board, the other two rows only apply to Board A's
batch, see below):

| Symptom | Cause | Fix in `ddm_cup.ino` |
| ------- | ----- | -------------------- |
| Bottom quarter of the glass never repaints and keeps stale pixels | The init table sends Vertical Scrolling Start Address (`0x37`) and never Normal Display Mode ON, so the panel stays in scroll mode | `panelNormalMode()`: scroll area = whole panel (`0x33`), scroll start 0, NORON (`0x13`) |
| Picture sideways as soon as scroll mode ends; before that, rotations 1 and 3 stayed portrait | The table writes `0xC0 = 0x23`: Power Control 1 on an ILI9341, but LCMCTRL on an ST7789, a byte of XOR flags laid over MADCTL. Bit 1 (XMV) inverts the row/column-swap bit, which scroll mode had been masking | `panelNormalMode()` rewrites LCMCTRL as `0x01` |
| Red and blue swapped (horse 1 came up blue) | Bit 5 (XBGR) of the same byte inverts the driver's BGR bit | the same `0x01` |
| Rotation 0 flipped top-to-bottom, rotation 2 flipped left-to-right | Upright needs both MADCTL flip bits set; the library's portrait rotations set only one | `panelOrientation()` sends MADCTL `0xC8` (`0x08` for `ROTATION 2`) after `setRotation()` |

There are at least two panel batches. Board A answers RDDID `10 81 B3` and
wants MX and MY (`0xC8`) to be upright. The bench cup with the scale answers
`00 00 00` to both RDDID and RDID4, which is what a genuine ILI9341 does over
SPI; its status register behaves like the ILI9341 datasheet (normal mode OFF
while scrolling, ON after NORON, where Board A reported both bits on); and it
is upright with the library's own rotation-0 value (`0x48`), the same build
having come up flipped top-to-bottom on it (2026-09-15). That batch is
therefore treated as ILI9341-like: it still gets the scroll fix (the band was
there too) but not the LCMCTRL write, because on an ILI9341 `0xC0` is Power
Control 1 and `0x01` would be an out-of-range GVDD. The boot log prints which
path was taken (`LCMCTRL rewritten` or `LCMCTRL left alone`).

The cup reads its ID bytes at boot and picks the orientation default from
them (`00 00 00` gets `0x48`, anything else `0xC8`); each cup can override
that in NVS. In the serial monitor, `h` mirrors left-right, `v` mirrors
top-bottom, `o` steps through the four combinations (`C8 → 48 → 08 → 88`),
`x` forgets the saved setting and goes back to the ID default; without a
cable, hold BOOT for 15 s to step (the 3 s tare fires on the way, harmless with
an empty cup; the hold is that long because at 6 s a couple of long tare
presses on the bench rotated a cup by accident). If a cup ever comes up turned
round, look at its boot line: `from NVS` means a saved setting is in play and
`x` clears it. Each change redraws at once and is remembered across reflashes.
Landscape has not been tried since the fix.
Horse 15's khaki cloth reads as light grey on this glass; that is the colour
table, not the controller. The two sketches under `tools/` are what found all
this.

### Gateway — plain ESP32 WROOM-32 dev board

Any generic ESP32 WROOM-32 dev board. USB to the DevPi, which is where the
control software lives.

> **Do not use an ESP32-S2.** The S2 has no ESP-NOW support. ESP32 (original)
> or ESP32-S3 only.

## The shared header and the symlink situation

`ddm_common.h` defines the ESP-NOW wire protocol and lives in **this** folder,
one level above both sketches. It is the single source of truth: both ends cast
raw received bytes directly into its structs, so the two builds must compile the
exact same file.

**The Arduino IDE will not follow a relative include that points outside the
sketch folder.** `#include "../ddm_common.h"` does not work. The fix is to
symlink the header into each sketch folder, so both sketches see a local
`ddm_common.h` that is really the one file above.

Git stores symlinks as symlinks, so this is done once and then travels with the
repo — a fresh clone already has them (on Windows, see the note below).

**Windows** — `mklink` needs either an **elevated** Command Prompt or **Developer
Mode** turned on (Settings → System → For developers → Developer Mode). Note
these are `cmd` commands, not PowerShell:

```
cd /d "D:\VSCode\DDM Multimedia\firmware\quiniela\ddm_gateway"
mklink ddm_common.h ..\ddm_common.h

cd /d "D:\VSCode\DDM Multimedia\firmware\quiniela\ddm_cup"
mklink ddm_common.h ..\ddm_common.h
```

For git to *check out* symlinks as real symlinks on Windows (rather than as text
files containing a path), the repo needs `core.symlinks=true`, which also
requires elevation or Developer Mode:

```
git config core.symlinks true
```

**Linux / macOS:**

```bash
cd firmware/quiniela/ddm_gateway && ln -s ../ddm_common.h ddm_common.h
cd ../ddm_cup && ln -s ../ddm_common.h ddm_common.h
```

If symlinks are genuinely unavailable, copying the file works — but then it is
on you to re-copy after every edit, and a stale copy produces silently corrupt
packets rather than a build error.

## `ddm_font.h` is generated — do not hand-edit

> **`ddm_font.h` is machine-generated output.** `tools/tracefont.py` reads a TTF
> with fontTools, flattens and simplifies the outlines of 50 glyphs (`0-9`,
> `A-Z`, space and `% : - . ! ? / + # , ( ) '`) and emits them as polygon data on a
> 1000-unit grid (y = 0 at cap height, 1000 at the baseline) that the cup
> renders directly, anti-aliased. Every string on the cup goes through it, not
> just the digits, so **all cup text is uppercase** and limited to that set; a
> character the font lacks draws as a gap the width of `?`.
>
> **Any hand edit is destroyed the next time the generator runs.** To change the
> face, regenerate:
>
> ```
> pip install fonttools
> python3 tools/tracefont.py IMPACT.TTF ddm_cup/ddm_font.h
> ```
>
> The source TTF is **not** in the repo and must not be committed: Impact is a
> Monotype typeface licensed with Windows (`C:\Windows\Fonts\impact.ttf`); the
> traced coordinate data is the only thing that lives here, and `*.ttf` is in
> `.gitignore`.

## Build settings

Arduino IDE, with the ESP32 board package installed.

- **Board:** `ESP32 Dev Module` (both the gateway and the CYD cups)
- **Serial Monitor:** 115200 baud

Libraries (Library Manager):

- Adafruit GFX Library
- Adafruit ILI9341
- ArduinoJson **7.4.3** (gateway; the v7 `JsonDocument` API, compiled and
  tested against 7.4.3)

## Uploading to the CYD

**Auto-reset on these boards is unreliable.** If Upload hangs on
`Connecting........_____`, put the board into the bootloader by hand:

1. Hold the **BOOT** button down.
2. Click **Upload**.
3. Keep holding until the log prints the chip name (`Chip is ESP32-D0WD-V3`
   or similar).
4. Release. The flash proceeds normally.

## Bench test

The current sketches exist to answer one question: **does the signal reach
from the cabinet to the mantle, with margin?** One gateway, four cups, no
other hardware.

### Flash order

1. Flash the gateway (plain WROOM-32). A default build boots **silent**: it
   broadcasts nothing over ESP-NOW until it is told what to broadcast (see
   [Serial line protocol](#serial-line-protocol)). For the bench, either
   open the serial monitor and type `demo`, or build with `DDM_AUTO_DEMO 1`
   at the top of `ddm_gateway.ino`; either way the gateway then broadcasts
   WINNER with the results walking through the horses every 3 seconds, so
   every cup that hears it shows a WIN / PLACE / SHOW banner in turn. It runs
   standalone off a USB brick, no Pi needed. Never leave a `DDM_AUTO_DEMO 1`
   build on the party gateway; the boot banner prints the value so a serial
   log shows it.
2. Flash each cup (CYD, hold BOOT during upload as above). A cup that has
   never been given a horse shows `NO HORSE` / `HOLD TO SET`.
3. Give each cup its horse: hold the glass 3 s → `HORSE` → tap above the
   number for up, below it for down → `SET`. Or type `n7` in the cup's serial
   monitor. The number is saved in the cup's NVS, survives reflashing, and is
   what the cup reports in every packet. The gateway hands out nothing.
4. Power everything up. Each cup shows its number at once (it needs no
   gateway for that) and appears in the gateway's `cups` table and `status`
   line within a couple of seconds; the banners appear once the broadcast
   starts.

### Reading the gateway output

Serial monitor at 115200. Type `help` for the command list (`state`,
`scratch`, `renum`, `results`, `cups`, `demo`, `debug`, `json`). `cups`
dumps the cup table (MAC, horse, tokens, signal, age). `json` prints the
whole gateway state as one JSON line and `json 1` repeats it every second in
place of the summary table (`json 0` to stop); see `state` under "Up:
gateway → DevPi" below. A default build prints the JSON lines DevPi reads
(one `{"t":"telem",...}` per packet from a cup and a `{"t":"status",...}`
every 5 seconds carrying the cup table, see
[Serial line protocol](#serial-line-protocol)) and prefixes every other line
with `# `. Type `debug on` (or build with `DDM_DEBUG_TEXT 1`) to also get the
human-readable `# TELEM ...` line per packet and, every 5 seconds, the
summary table — this is the range-test readout:

```
# ---- CUPS seq=1234 state=5 demo=on rejects=0 heard=2 ----
#  mac               horse  age_ms   drop  rssi  up_rssi  tok  status
#  A4:CF:12:34:56:78     7     420      0   -58      -55   23  OK
#  A4:CF:12:34:56:9A     3    4200      9   -77      -71    0  STALE
```

- **horse** — the number the cup says it is; `0` = not set on that cup yet.
- **age_ms** — time since that cup was last heard from. Cups report every
  2 s, so a healthy cup sits under ~2100. `STALE` = silent for over 3 s. A
  cup silent for 10 minutes is dropped from the table.
- **drop** — state packets the cup detected as missed (gaps in `seq`),
  cumulative. A slowly rising count at range is packet loss in the
  gateway→cup direction.
- **rssi** — signal strength the *cup* measures on gateway broadcasts
  (downlink). **up_rssi** — signal strength the *gateway* measures on that
  cup's packets (uplink). Both directions matter; they are usually within
  a few dB of each other.
- **tok** — the token count from the cup's last packet.
- **rejects** — packets discarded for a protocol-version mismatch. Nonzero
  means a device is running an old flash.

### What the RSSI figures mean

- **Better than −70 dBm** — comfortable. Real margin; party-night bodies
  and a microwave won't kill it.
- **−70 to −85 dBm** — works on the bench, thin in real conditions. Walk
  the cup around before trusting it.
- **Past −85 dBm** — trouble. Expect drops and STALE cups. Move the
  gateway, or pick a quieter channel in `ddm_common.h` (and reflash
  everything).

A short **BOOT press on any cup** toggles its diagnostic overlay — horse
(and `SCR` when its scratched bit is set), state, RSSI, drop count, whether
it has the gateway's MAC yet, renumbers followed, seq, last-packet age,
tokens — which is what you read while walking a board around the room.

### ESP-NOW, and why it scales to twenty cups

- **Downlink is one broadcast frame** (`DdmStatePacket`, 22 bytes) every
  500 ms to the broadcast address, the gateway's one and only peer. Nothing is
  unicast from the gateway and no peer is added or removed at runtime, so the
  fleet is bounded by the cup table (`DDM_MAX_CUPS`, 24), not by ESP-NOW's
  20-peer limit.
- **Uplink** is one 18-byte frame per cup every 2 s (unicast to the gateway's
  MAC once the cup has heard a state packet; a broadcast `HELLO` with the
  same payload every 1 s before that). A cup registers two peers, broadcast
  and the gateway. Receiving needs no peer entry on either side.
- Twenty cups: 2 packets/s down, 10/s up, under 400 bytes/s of air time.
- Nothing prints or blocks inside a receive callback: the gateway's callback
  queues the packet for `loop()`, the cup's copies it into a buffer and sets
  a flag. Every serial byte is written from `loop()`.

## Serial line protocol

The gateway's USB serial port (115200 8N1) is a machine-readable bridge
between ESP-NOW and DevPi: **one compact JSON object per line** in each
direction, `\n`-terminated, at most 1024 bytes per line down and for most
lines up (the two lines that carry the cup table, `status` and the
on-request up `state`, are sized under their own headings). Every JSON line
has a `"t"` key naming its type. Unknown `"t"` values and unknown keys are
ignored silently, which is how one end can move ahead of the other.

This is line protocol **v2** (`"v":2` in `hello`), which goes with ESP-NOW
protocol v2 (`DDM_PROTO_VERSION 2`): the cup owns its horse number, so every
line is keyed by **MAC** (which cup) and **horse** (what it says it is), and
there are no cup IDs, slots or roster lines anywhere.

Rules that apply to every line:

- **Any line that does not start with `{` is not protocol** and DevPi drops
  it. So every non-JSON line the gateway prints — boot banner, command
  replies, the debug table — starts with `# ` (hash, space). The ESP32 ROM's
  own boot messages cannot be prefixed and fall under the same rule.
- **Horse numbers are 1..24, 0 = none.** 1..20 is the field, 21..24 the
  also-eligibles (`DDM_MAX_HORSE`).
- `phase` / `st` is the numeric `DdmRaceState` value from `ddm_common.h` (0..6).
- MAC addresses are strings, uppercase hex, colon separated:
  `"A0:B7:65:12:34:56"`.
- No timestamps: the gateway has no clock. DevPi stamps lines on receipt.
- Lines never interleave. Everything the gateway prints comes from `loop()`;
  packets arriving in the ESP-NOW receive callback are queued and reported
  from there.
- A rejected downlink line changes nothing and gets an `err` line. An
  applied line gets a `status` line; there is no separate ack.

### Up: gateway → DevPi

#### `telem` — one per packet from a cup, always, whatever the debug flag

```json
{"t":"telem","mac":"A0:B7:65:12:34:56","horse":7,"raw":812345,"count":14,"seq":9021,"drop":2,"rssi":-64,"up":-61}
```

| Key | Source |
| --- | ------ |
| `mac` | ESP-NOW sender address: which cup |
| `horse` | packet `horse`: the number the cup says it is, `0` = none set |
| `raw` | `rawWeight` (HX711 counts, reading minus tare) |
| `count` | `tokenCount` |
| `seq` | packet `seq`: the last state seq the cup saw (not a telemetry counter) |
| `drop` | `dropped` |
| `rssi` | packet `rssi`: the cup's view of the gateway (downlink) |
| `up` | gateway-side RSSI of this packet (uplink, the table's `up_rssi`) |
| `hello` | **Only present, as `1`, when the packet was a `DDM_MSG_HELLO`**: the cup is still broadcasting because it has not heard a state packet yet. Same payload otherwise. |

#### `hello` — gateway boot announcement

```json
{"t":"hello","v":2,"proto":2,"mac":"24:6F:28:AA:BB:CC"}
```

| Key | Meaning |
| --- | ------- |
| `v` | line-protocol version (`DDM_LINE_PROTO_VERSION` in the sketch) |
| `proto` | `DDM_PROTO_VERSION`, the ESP-NOW wire protocol |
| `mac` | the gateway's own MAC |

Sent once at boot, then every 2 seconds **until the first valid `state` line
has been applied**, then never again until the next reboot. A `hello` tells
DevPi the gateway has (re)booted and needs its state again.

#### `status` — heartbeat, acknowledgement, and the cup table

```json
{"t":"status","gseq":10412,"phase":1,"state_rev":42,"cups":[{"mac":"A0:B7:65:12:34:56","horse":7,"tok":23,"rssi":-63,"up":-61,"age":180},{"mac":"A0:B7:65:12:34:57","horse":0,"tok":0,"rssi":-70,"up":-66,"age":900}],"rejects":0,"up_s":5230}
```

| Key | Meaning |
| --- | ------- |
| `gseq` | the gateway's current state broadcast `seq` (stays 0 while silent) |
| `phase` | current `raceState` |
| `state_rev` | `rev` of the last applied `state` line, `0` if none yet |
| `cups` | every cup in the gateway's table, in the order first heard |
| `cups[].mac` | the cup's MAC |
| `cups[].horse` | the horse the cup last claimed, `0` = none set |
| `cups[].tok` | `tokenCount` from its last packet |
| `cups[].rssi` | `rssi` from that packet: the cup's view of the gateway (downlink) |
| `cups[].up` | gateway-side RSSI of that packet (uplink) |
| `cups[].age` | ms since the cup was last heard from |
| `rejects` | packets rejected for a `DDM_PROTO_VERSION` mismatch |
| `up_s` | seconds since boot |

Sent every 5 seconds, **and immediately after any `state` or `debug` line
is applied**. This is the only acknowledgement mechanism. Two cups claiming
the same horse are both listed; DevPi decides what to show. A cup silent
for 10 minutes leaves the table.

**Length.** With the table this line may exceed 1024 bytes: a cup entry is
at most 90 bytes, so 24 cups come to about 2.3 KB. The sketch builds it in
its own 2560-byte buffer (`BIG_LINE_MAX`), and the DevPi bridge reads lines
up to 4096 bytes.

#### `err` — a downlink line was rejected

```json
{"t":"err","msg":"parse","line":"{\"t\":\"sta"}
```

| `msg` | Meaning |
| ----- | ------- |
| `parse` | not valid JSON |
| `invalid` | valid JSON that failed validation (rules under each line type below) |
| `overflow` | the line exceeded 1024 bytes; the rest of it up to the next newline is discarded |

`line` is the first 40 characters of the offending line, JSON-escaped (`"`
and `\` escaped, control characters dropped, bytes above 0x7F as `\u00XX`);
it is omitted for `overflow`. A rejected line changes nothing and no `status`
is sent for it.

#### `state` — full gateway snapshot, only when asked

```json
{"t":"state","seq":1234,"demo":0,"mac":"A4:F0:0F:5E:0B:08","st":1,"scr":[9,15],"renum":[[9,22]],"res":[0,0,0],"cups":[{"mac":"20:50:0D:11:D9:AC","horse":7,"tok":23,"rssi":-63,"up":-61,"age":180}]}
```

| Key | Meaning |
| --- | ------- |
| `seq` | the gateway's current state broadcast `seq` (`gseq` in `status`; stays 0 while silent) |
| `demo` | `1` while demo mode is on, else `0` |
| `mac` | the gateway's own MAC |
| `st` | the broadcast packet's `raceState` (`phase` in `status`) |
| `scr` | the horses whose scratched bit is set, ascending |
| `renum` | the renumber pairs in the packet, `[from, to]` each |
| `res` | the packet's `results`: WIN, PLACE, SHOW horse numbers, `0` = not yet |
| `cups` | the cup table, as in `status` |

Sent only on request, never on its own. Typing `json` prints one line at
once; `json 1` prints one every 1000 ms, and silences the 5-second summary
table while it runs (see "Debug flag" below), until `json 0`. The boot
default is off. Same buffer and length as `status`. The line is written from
`loop()` like every other one, so it never interleaves with a `telem`.

**Naming.** The down-link `state` line (next section) is a different line
that happens to share the type name. DevPi → gateway carries `rev`, `st`,
`scr`, `renum` and `res` and is what the gateway *applies*; this gateway →
DevPi line is a *report* the gateway never parses.

### Down: DevPi → gateway

The gateway reads without blocking, strips a trailing `\r`, ignores empty
lines, and dispatches on the first character: `{` goes to the JSON handler,
anything else to the hand-typed command handler (`help`).

#### `state` — full snapshot, never a delta

```json
{"t":"state","rev":42,"st":1,"scr":[9,15],"renum":[[9,22]],"res":[0,0,0]}
```

All of these must hold or the whole line is rejected with `err` / `invalid`:

- `rev` present, integer ≥ 1
- `st` integer 0..6 (the `DdmRaceState` range)
- `scr` an array (any length, may be empty) of integers 1..`DDM_MAX_HORSE`:
  the horses scratched **with no replacement**; each becomes a bit in the
  packet's `scratched` mask, and a cup whose horse is in it shows the boxed X
- `renum` an array of at most `DDM_RENUM_SLOTS` (4) pairs `[from, to]`, both
  1..`DDM_MAX_HORSE`, `from` ≠ `to`, no `from` twice: a replacement scratch.
  A cup whose horse is `from` adopts `to`, saves it and reports `to` from then
  on (at Churchill an also-eligible that draws in keeps its own program
  number: the cup that was 9 becomes 22). Keep the pair in the line for as
  long as the scratch stands; a cup that has already followed it no longer
  matches, so repeating it is harmless. To undo, send `[to, from]` for a
  while.
- `res` an array of exactly `DDM_RESULT_SLOTS` (3) integers 0..`DDM_MAX_HORSE`:
  WIN, PLACE, SHOW; `0` = not yet. In WINNER (and AFTER_PARTY) a cup whose
  horse is named shows the WIN / PLACE / SHOW frame in gold, silver or bronze.

On a valid line, in this order: `st`, `scr`, `renum` and `res` go into the
broadcast packet in one step (no packet goes out half-applied); `rev` becomes
`state_rev`; demo mode goes **off**; the state broadcast starts if it was not
running yet (see "Silent boot" below); `hello` stops; a `status` is emitted.
The line is idempotent: the same line arriving twice is normal and harmless.
Send the full snapshot on every change, and again whenever a `hello` shows
the gateway has rebooted.

There is **no roster line** any more. A v1 `{"t":"roster",...}` is an unknown
type and is ignored without a word.

#### `debug` — runtime toggle for the human-readable output

```json
{"t":"debug","on":true}
```

`on` must be a boolean. Sets the debug flag (next section), then emits
`status`.

### Debug flag and human-readable output

`#define DDM_DEBUG_TEXT 0` near the top of `ddm_gateway.ino` is the boot
default. The flag gates the **periodic** human output only: the per-packet
`# TELEM ...` / `# HELLO ...` lines and the 5-second `# ---- CUPS` summary
table. Flag off, neither prints; flag on, both print, prefixed with `# `.
JSON lines are emitted regardless. Replies to hand-typed commands (`cups`,
`help`, ...) and the boot banner always print, prefixed with `# `. Flip the
flag with the JSON `debug` line or by typing `debug on` / `debug off`. One
override: while `json 1` is on, the summary table is suppressed whatever the
flag says (the `state` line carries the same numbers every second); the
`# TELEM` lines still follow the flag, and `json 0` hands the table back to
it.

### Silent boot and `DDM_AUTO_DEMO`

`#define DDM_AUTO_DEMO 0` next to `DDM_DEBUG_TEXT` is the boot default, and
the banner prints both values (`# build: DDM_AUTO_DEMO=0 DDM_DEBUG_TEXT=0`).

With `DDM_AUTO_DEMO 0` (the default, the party build):

1. On boot the gateway sends `hello` and **broadcasts nothing** over
   ESP-NOW. Cups show their own number regardless: it lives on the cup.
2. It stays silent **indefinitely**: no timeout, no automatic fallback.
   `hello` repeats every 2 seconds and `status` every 5 the whole time, and
   incoming packets are received, tracked and reported up serial as normal.
   "Silent" means the state broadcast only. A cup that has never heard a
   state packet keeps broadcasting `HELLO` (reported with `"hello":1`) since
   it does not know the gateway's MAC yet; the gateway hears those fine.
3. The 500 ms state broadcast starts only when one of these happens: a valid
   JSON `state` line (broadcast that state, demo stays off); the typed `demo`
   command; a typed `state`, `scratch`, `renum` or `results` command (apply
   it and broadcast the result, demo off).
4. Once started it never stops again until reboot.

The reason: a power blip reboots the gateway in about a second but DevPi
takes most of a minute to boot, and a gateway that started demo mode on its
own would put WIN / PLACE / SHOW banners on cups full of real tokens for
that minute.

With `DDM_AUTO_DEMO 1` (bench builds only) the gateway boots straight into
demo mode and broadcasts immediately. `hello` still repeats, and a valid
JSON `state` line still takes over and turns demo off.

## Scale

Each cup weighs the tokens dropped into it with a 1 kg load cell on an HX711
(gain 128, 10 SPS). The tokens land in an inner sleeve that rides on the load
cell, so nothing that is weighed touches anything fixed. The settled weight is
the source of truth for the count; live drop and remove events, detected as
steps against a slow baseline, are provisional and exist so telemetry and the
splash display react within about 300 ms.

### Wiring

| CN1 pin | HX711 |
| ------- | ----- |
| 3.3V    | VCC   |
| GND     | GND   |
| GPIO27  | DT (data) |
| GPIO22  | SCK (clock) |

3.3V only (see the warning under Hardware). The pins are `HX711_DT` and
`HX711_SCK` at the top of `ddm_cup.ino`; `tools/hx711_calibrate/` uses the same
two. Library: **HX711 Arduino Library** by Bogdan Necula (`bogde/HX711`).

### Calibration constants

```cpp
#define COUNTS_PER_TOKEN   6212L    // mean step, 10 tokens, sd 102 (1.6%)
#define TOKEN_THRESHOLD    3106L    // half a token
```

Measured on the bench with `tools/hx711_calibrate/` in 2026-09: ten tokens of
the current print dropped one at a time. A token lands as a spike about 3% high
and settles over a second; the sketch confirms over three samples and snaps its
baseline to the new reading, so the spike is absorbed rather than counted.

Those are only the defaults. Load cells differ by several percent from unit to
unit, and the 6212 was read 0.8 s after each drop, so it carries a little
overshoot itself. Calibrate each cup on the bench instead: put a known number
of tokens in, wait about 20 s for the `[settle]` line, then type `c` followed
by the number in the serial monitor, for example `c50`. The cup divides the
settled load by that number, stores the result in NVS, and sets its count to
it. `c0` forgets the stored value. The boot line `scale: N counts/token from
NVS` shows what a cup is using.

### Counting accuracy

The settled weight is authoritative; live events are provisional.

A slow baseline tracks the reading while it stays within half a token, so
warm-up drift is never counted. A jump of at least half a token that holds for
three consecutive samples (bump rejection) is a live event: the step is rounded
to whole tokens and the count moves at once, up or down. Then, 3 s after the
last event, once the last 20 samples span less than 1,200 counts, the cup sets
the count to the settled load divided by its counts-per-token, with no cap,
printing `[settle] ok tokens=n load=x.xx` or `[settle] tokens a -> b
load=x.xx`. That corrects a double count, a missed handful and a token lifted
out while nobody was looking, all the same way. A load within a third of a
token of a half is left alone and printed as `[settle] ambiguous ... (left
alone)`, so a slightly mis-calibrated cup cannot flip-flop. `s` over serial
applies the settled load immediately.

Handling rule (`DISTURBED`): a reading lighter than empty by more than half a
token, or a single-sample step of more than `HANDLING_TOKENS` (8) tokens either
way, is a hand, a dump or a bump, never a bet. The cup stops counting, leaves
its baseline alone, skips the settle check, keeps reporting the last good count
and shows `HANDLED` on the overlay until the reading is flat again and no
lighter than empty; then `tokens = round(net / counts-per-token)` (never below
0) with one `[handled] tokens N -> M net=... (re-baselined)` line and no
`[drop]` or `[remove]`. Dumping a cup between races is expected and handled
this way: the count goes to 0 once, with no phantom bets (the sleeve rides on a
cantilever load cell, so a tipped cup reads below its tare and setting it back
down used to look like a 45-token drop). The pi5 reset should still follow a
dump so the ticker clears.

Because the settled weight decides, each cup's counts-per-token has to be its
own (`c<N>` or the menu's `CAL 10`): load cells differ by a few percent per
unit, and at 50 tokens a 2% error is a whole token.

History: until 2026-09-20 the stack leaned on the fixed cup wall and shunted
weight, giving about 5% of hysteresis. An overshoot compensation, a faster
post-event baseline and a one-token cap on settle corrections were added to
survive that; with the sleeve they had turned into the miscount and are gone.

### The sleeve

- The sleeve must clear the shell by 3–4 mm all the way up; anything it touches
  takes weight off the cell.
- Tare with the sleeve installed. The sleeve is part of the empty reading.

### If the token print changes

1. Flash `tools/hx711_calibrate/hx711_calibrate.ino` (same board settings, only
   the HX711 library needed), serial monitor at 115200.
2. Wait 60 s for the HX711 to warm up, then type `t` with the plate empty.
3. Type `c`. Drop one token, type `+`, wait for the `[cal] token N` line.
   Repeat for 8–10 tokens.
4. Type `d`. It prints the two `#define` lines; paste them over the ones at the
   top of `ddm_cup.ino` and reflash every cup.

### Tare

- On boot the cup waits 30 s for the HX711 to settle (the NO HORSE screen says
  `SCALE WARMING UP`, the overlay `WARMUP`), then, once the plate is flat
  (overlay `SETTLING`), averages 30 samples and decides whether that is a fresh
  empty reading or the saved one plus a pile (Brownout, below).
- While the count is 0 the tare follows the slow baseline, so an empty cup keeps
  re-zeroing itself. Once a token is counted the tare freezes.
- **Hold BOOT for 3 s** to re-tare by hand: the count goes back to 0, the
  current reading becomes "empty" and is saved (`[nvs] zero=… saved (tare)`),
  the green LED blinks once and serial prints
  `[tare]`. Keep holding to 15 s and the cup steps its display orientation and
  saves it instead (see the display controller section); `t` over serial also
  tares. A short press still toggles the diagnostic overlay, whose last line
  now reads `TOKENS:n  NET:±counts` plus the scale state. The cup never shows the
  count on a normal screen; the splash display does that.

Serial prints one line per event, `[drop] +1 tokens=7 step=6240 baseline=43512`
or `[remove] -1 ...`, plus `[handled] enter reason=below-empty net=...` /
`[handled] enter reason=step step=... est=...` and `[handled] tokens N -> M
net=... (re-baselined)` around a handled cup, so a bench session can be grepped. If no HX711 answers at
boot the cup runs without counting and the overlay says `no HX711`.

### Brownout: a cup comes back with its count

- The cup keeps its empty reading (`zero`, raw counts) and its settled count
  (`count`) in NVS, in the same `ddmcup` namespace as the horse number.
- Through the 30 s warm-up it reports the saved count, not 0, so the board
  never dips; then it waits for a flat plate, averages 30 samples and decides:
  no saved zero (first boot on this firmware) → tare, count 0; within half a
  token of the saved zero, or lighter → empty, re-zero and save; heavier → the
  pile: keep the saved zero, `count = round((reading − zero) / counts-per-token)`.
  The weight wins over the saved count.
- One `[boot]` line reports the decision with the drift `reading − zero` in
  counts and tokens (the figure the bench test reads for the spec's open
  question 4): `[boot] saved zero=… count=30 | read=… net=+186420 (30.01 tok) →
  keep zero, count 30`, or `… net=-1210 (-0.19 tok) → empty, re-zero (drift
  -1210 counts)`, or `[boot] no saved zero | read=… → tare, count 0`.
- The count is written once it has held `COUNT_SAVE_HOLD_MS` (2 s) with no
  settle pending and the cup not handled (`[nvs] count=30 saved (settled)`),
  never for an unchanged value. An empty, settled cup whose floating tare has
  moved `ZERO_REFRESH_TOKENS` (a quarter token) from the saved zero saves the
  new empty reading, at most once per `ZERO_REFRESH_MS` (10 min). A manual tare
  (BOOT 3 s, the menu's `TARE`, serial `t`) saves the new zero and count 0; a
  tare inside the 30 s warm-up saves a cold reading, so that one is re-saved as
  soon as it has drifted a quarter token, without the 10 min wait. `CAL 10` /
  `c<N>` and a horse change never touch `zero` (the count `c<N>` sets is saved
  like any other settled count). While the boot read waits for a flat plate it
  says so every `BOOT_WAIT_NOTE_MS` (10 s). A plate that is flat but more than
  `HANDLING_TOKENS` (8) tokens below the saved zero is a lifted or tilted cup,
  not an empty one: the cup keeps waiting and says why.
- `z` over serial prints the saved zero and count, a fresh averaged reading
  (the settle ring), the drift between them in counts and tokens, and the
  current count: `[zero] saved zero=… count=30 | reading=… (avg of 20, spread
  310, flat) drift=+186420 counts (+30.01 tok) | tokens=30 tare=… OK`.

Bench test: 30 tokens in, power off, wait, power on. The board never dips and
the count comes back 30. Then with an empty cup: power cycle, count 0. The
drift printed at boot across an empty power cycle decides whether the saved
zero is good enough on its own.

### Serial commands

| Key | Does |
| --- | --- |
| `n<N>` | this cup is horse N (1–24, `n0` = none), saved |
| `o` / `h` / `v` / `x` | next orientation / mirror left-right / mirror top-bottom (all saved) / forget the saved orientation |
| `t` | tare: count 0, the reading becomes "empty", saved (`zero`, `count`) |
| `s` | apply the settled load to the count now |
| `z` | the saved zero and count, a fresh reading, the drift, the current count |
| `c<N>` | N tokens are on the plate: calibrate counts/token and save (`c0` forgets it); does not touch `zero` |
| `p` | print raw and mapped touch coordinates on/off |
| `?` | help |

## Touch menu

The CYD's resistive touch panel (XPT2046, its own SPI bus on GPIO 25/33/32/39,
IRQ 36; library **XPT2046_Touchscreen** by Paul Stoffregen) carries a hidden
maintenance menu, because once the board is inside the cup the BOOT button is
unreachable. It is built so a guest poking the screen sees a cup that ignores
them. Since protocol v2 it is also how a cup is told which horse it is.

- **Open:** press anywhere on the glass and hold for 3 s. A tap does nothing,
  and nothing is drawn until the 3 s are up.
- **Any state:** the menu opens in every race state, linked or not; the 3 s
  hold is the guard against guests, and the destructive actions (`TARE`,
  `CAL 10`) confirm first. `HORSE` alone is locked while betting is open or
  the race is on (states 1–4).
- **Closes** after 5 s without a touch, and after most actions. On close the
  normal screen is redrawn exactly as it was.

Header: `HORSE 7   STATE 1` (or `NO HORSE   STATE 0`) and `V0.7  SEP 29 2026
<MAC>` (firmware version from `FW_VERSION`, build date from `__DATE__`).
Then two pages of full-width bars:

| Page 1 | Does |
| --- | --- |
| `HORSE` | the picker: the number big (or `NONE`), a tap above it counts up, a tap below it counts down (`NONE`, 1..24, wrapping), `SET n` saves it to NVS and shows `HORSE n`, `CANCEL` keeps the old one. The new number is on the screen and in the next packet at once. In states 1–4 the bar reads `HORSE (LOCKED)` and does nothing |
| `TARE` | confirm screen (`TARE?`, `PLATE HAS n TOKENS`, `YES` / `NO`); YES runs the same tare as the 3 s BOOT hold, shows `TARED`, closes |
| `CAL 10` | confirm screen (`PUT EXACTLY 10 TOKENS IN`, `DONE` / `CANCEL`); DONE shows `SETTLING...`, waits up to 6 s for the plate to settle, then does what serial `c10` does (counts/token into NVS, count set to 10), shows the value for 2 s, closes; `NOT SETTLED - TRY AGAIN` returns to the menu |
| `DIAG` | toggles the diagnostic overlay, closes |
| `BRIGHT n%` | cycles 100 → 60 → 30 → 100, saved in NVS as `bright` and applied at boot; stays in the menu |
| `MORE...` | page 2 |
| `CLOSE` | closes |

| Page 2 | Does |
| --- | --- |
| `FLIP 180` | toggles both MADCTL flip bits, saves, redraws, closes |
| `FLIP H` | mirrors left-right (serial `h`), saves, closes |
| `FLIP V` | mirrors top-bottom (serial `v`), saves, closes |
| `ANNOUNCE` | sends one `DDM_MSG_HELLO` now, shows `SENT`, stays on page 2 |
| `BACK` | page 1 |
| `CLOSE` | closes |

A tapped bar lights up amber for 120 ms before its action runs. Serial prints
`[menu] open`, `[horse] 7 saved to NVS (menu HORSE)`, `[menu] tare`,
`[menu] cal cpt=N`, `[menu] flip madctl=0x..`, `[menu] bright N%`,
`[menu] announce`, `[menu] close (...)`. A renumber pair from the gateway
prints `[renum] 9 -> 22 from the gateway` and then the same `[horse]` line.

**Touch mapping.** Hit-testing is on y only: bars are full-width, confirm
screens are top/bottom halves and the picker splits at y 95, so rough
calibration is fine. The defines at the top of `ddm_cup.ino` are
`TOUCH_X_MIN/MAX` and `TOUCH_Y_MIN/MAX` (raw XPT2046 ranges; raw x is the
panel's long axis), `TOUCH_SWAP_XY` (true for portrait: raw x becomes screen
y) and `TOUCH_FLIP_X/Y`. An orientation away from the batch default (FLIP 180,
H or V) is followed automatically. **Both panel batches need their touch axes
verified:** type `p` in the serial monitor, touch the top, middle and bottom
of the glass, and check the printed `screen y` runs 0 → 319 top to bottom; if
it runs the other way set `TOUCH_FLIP_Y`, if it barely changes set
`TOUCH_SWAP_XY` the other way. A press counts only after three consecutive
50 ms polls, because resistive panels chatter at the edge of a press.

## Tools

### `tools/panel_probe/` — display controller probe

Standalone sketch for the "bottom quarter of the panel never clears" symptom
seen on the newer cup boards. Flash it exactly like `ddm_cup.ino` (same board,
same two Adafruit libraries, no `ddm_common.h` needed), open the serial
monitor at 115200, and photograph the glass as it walks through its phases.
It reads the controller ID registers before and after the Adafruit ILI9341
init, walks all four rotations with a border and a labelled ruler, and then
writes raw CASET/PASET windows at several MADCTL values and row offsets,
reading each pixel back out of GRAM. Run it on both board revisions; the
header comment in the sketch says what to report.

### `tools/band_test/` — bottom-band yes/no test

Twelve numbered fills, ~5 s each, at rotation 0. Each says on serial and in
the strip along the top of the glass which colour the bottom quarter should now
be. Report, per step, the colour you actually see there and whether the label
text at the top is upright. Steps 2 and 8 apply the scroll-mode part of the
`panelNormalMode()` fix that `ddm_cup.ino` sends after `tft.begin()`; steps 6
and 7 deliberately put the panel back into scroll mode to show the band
returning; steps 9..12 rewrite LCMCTRL (`0xC0`), the ST7789 register the
ILI9341 init table clobbers with `0x23`, to show whether the picture goes
sideways only once the panel leaves scroll mode and whether clearing XBGR
puts the colour order right.

### `tools/hx711_calibrate/` — HX711 calibration

Standalone: streams raw HX711 counts and walks a token calibration over serial
(`t` tare, `c` start, `+` per token, `d` results). Prints the two `#define`
lines for `ddm_cup.ino`; the values it measured for the current token print are
in its header comment and under Scale above.

### `tools/tracefont.py` — glyph tracer

Generates `ddm_cup/ddm_font.h` from a TTF (see the `ddm_font.h` section
above). Python 3 with `fonttools`; the third argument is the simplification
tolerance in grid units, default 1.4.

## Status

| Component | State |
| --------- | ----- |
| `ddm_common.h` — ESP-NOW protocol | ✅ **v2** (2026-09-27): the cup owns its horse number; `DdmStatePacket` 22 bytes keyed by horse (scratched bits, 4 renumber pairs, 3 results), `DdmTelemetryPacket` 18 bytes with `horse` in place of `cupId`. Every device must be reflashed |
| `ddm_gateway/` — gateway sketch | ✅ implemented (JSON line protocol v2 to DevPi: `telem` by MAC and horse, `status` with the cup table; silent boot; broadcast-only ESP-NOW, no acks, no peers but broadcast; bench commands `state` / `scratch` / `renum` / `results` / `cups`; demo mode walks the results) |
| `ddm_cup/` — cup sketch | ✅ implemented (v0.7: brownout, the cup comes back with its count: `zero` and `count` in NVS, boot read decides empty or pile, serial `z`; v0.6: horse in NVS, touch menu `HORSE` picker locked in states 1–4, `FLIP H` / `FLIP V`, serial `n<N>`, renumber pairs followed and saved, scratched bit, WIN / PLACE / SHOW frame from the results; display + ESP-NOW + HX711 token counting, calibrated for the current token print) |

The v1 protocol gaps are closed by v2: there is no cup ID to assign (so no
hello-ack, and no way for a cup to mistake a neighbour's `HELLO` for one), and
the results ride in the state packet, so a cup shows its own place rather
than cycling all three.

Still open on the cup side: the touch axes must be verified per panel batch
(serial `p`, see the touch menu section), and the scale base has to be proven
under the sleeve before the count is trusted at fifty tokens (see Scale).

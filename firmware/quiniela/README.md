# La Quiniela — cup firmware

Firmware for the La Quiniela betting cups (target: DDM 2027).

Each cup is a self-contained node. It shows a horse number on its TFT, reads a
load cell through an HX711 to count the tokens dropped in it, and talks to a
single gateway over **ESP-NOW**.

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
                  cup 0     cup 1      cup 2  …  cup 19
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
   at the top of `ddm_gateway.ino`; either way the gateway then walks horse
   numbers every 3 seconds on channel 6 and runs standalone off a USB brick,
   no Pi needed. Never leave a `DDM_AUTO_DEMO 1` build on the party gateway;
   the boot banner prints the value so a serial log shows it.
2. Flash each cup (CYD, hold BOOT during upload as above). A cup with no
   assigned ID shows a waiting screen with **its own MAC address in large
   gold text**.
3. Power everything up. Cups hello the gateway and get IDs assigned at
   once, and show horse numbers within a few seconds of the broadcast
   starting (`demo` typed, or a `DDM_AUTO_DEMO 1` build).

### Collecting the four MACs

Cup IDs assigned at runtime are RAM-only and reshuffle when the gateway
reboots. To pin them down:

- Read each MAC off the cup's waiting screen (power cups **without** the
  gateway running and they sit on that screen indefinitely), **or**
- watch the gateway's serial log — every unknown cup produces a `# NEWCUP`
  line with the MAC pre-formatted as a `KNOWN_CUPS[]` table row (only until
  DevPi has sent a roster; after that DevPi owns the IDs, see below).

Paste the four rows into `KNOWN_CUPS[]` at the top of `ddm_gateway.ino`
(table index = cup ID), reflash the gateway, and IDs are stable forever.

### Reading the gateway output

Serial monitor at 115200. Type `help` for the command list (`state`,
`horse`, `scratch`, `roster`, `demo`, `debug`). A default build prints the
JSON lines DevPi reads (one `{"t":"telem",...}` per telemetry packet, a
`{"t":"status",...}` every 5 seconds, see
[Serial line protocol](#serial-line-protocol)) and prefixes every other line
with `# `. Type `debug on` (or build with `DDM_DEBUG_TEXT 1`) to also get the
human-readable `# TELEM ...` line per packet and, every 5 seconds, the
summary table — this is the range-test readout:

```
# ---- CUPS seq=1234 state=1 demo=on rejects=0 ----
#  id mac                age_ms   drop  rssi  up_rssi  status
#   0 A4:CF:12:34:56:78     420      0   -58      -55  OK
#   1 A4:CF:12:34:56:9A    4200      9   -77      -71  STALE
```

- **age_ms** — time since that cup was last heard from. Cups report every
  2 s, so a healthy cup sits under ~2100. `STALE` = silent for over 3 s.
- **drop** — state packets the cup detected as missed (gaps in `seq`),
  cumulative. A slowly rising count at range is packet loss in the
  gateway→cup direction.
- **rssi** — signal strength the *cup* measures on gateway broadcasts
  (downlink). **up_rssi** — signal strength the *gateway* measures on that
  cup's telemetry (uplink). Both directions matter; they are usually within
  a few dB of each other.
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

A short **BOOT press on any cup** toggles its diagnostic overlay — cup ID,
horse, RSSI, drop count, seq, last-packet age — which is what you read
while walking a board around the room.

## Serial line protocol

The gateway's USB serial port (115200 8N1) is a machine-readable bridge
between ESP-NOW and DevPi: **one compact JSON object per line** in each
direction, `\n`-terminated, at most 1024 bytes per line. Every JSON line has
a `"t"` key naming its type. Unknown `"t"` values and unknown keys are
ignored silently, which is how one end can move ahead of the other.

Rules that apply to every line:

- **Any line that does not start with `{` is not protocol** and DevPi drops
  it. So every non-JSON line the gateway prints — boot banner, command
  replies, the debug table — starts with `# ` (hash, space). The ESP32 ROM's
  own boot messages cannot be prefixed and fall under the same rule.
- **Cup IDs are the wire value: 0-based, untranslated.** A cup with
  `cupId == n` reads `horseForCup[n]` and `scratched[n]`, valid IDs are
  0..19, and that is the number in the JSON. `-1` means "MAC not in the
  roster". Any 1-based presentation is DevPi's job; the gateway never
  converts.
- `phase` is the numeric `DdmRaceState` value from `ddm_common.h` (0..6).
- MAC addresses are strings, uppercase hex, colon separated:
  `"A0:B7:65:12:34:56"`. Either case is accepted on input.
- No timestamps: the gateway has no clock. DevPi stamps lines on receipt.
- Lines never interleave. Everything the gateway prints comes from `loop()`;
  packets arriving in the ESP-NOW receive callback are queued and reported
  from there.
- A rejected downlink line changes nothing and gets an `err` line. An
  applied line gets a `status` line; there is no separate ack.

### Up: gateway → DevPi

#### `telem` — one per telemetry packet, always, whatever the debug flag

```json
{"t":"telem","cup":7,"mac":"A0:B7:65:12:34:56","raw":812345,"count":14,"seq":9021,"drop":2,"rssi":-64,"up":-61}
```

| Key | Source |
| --- | ------ |
| `cup` | Roster slot looked up from the **sender MAC**, not from the packet. `-1` if the MAC is not in the roster. |
| `mac` | ESP-NOW sender address |
| `raw` | `rawWeight` (HX711 counts, reading minus tare) |
| `count` | `tokenCount` |
| `seq` | packet `seq`: the last state seq the cup saw (not a telemetry counter) |
| `drop` | `dropped` |
| `rssi` | packet `rssi`: the cup's view of the gateway (downlink) |
| `up` | gateway-side RSSI of this packet (uplink, the table's `up_rssi`) |
| `claim` | **Only present when the packet's `cupId` differs from `cup`.** The packet's `cupId`, so DevPi can spot a cup running on a stale ID. The gateway also re-acks that cup, see "Stale IDs" below. |

#### `cup_hello` — one per `DDM_MSG_HELLO` received

```json
{"t":"cup_hello","cup":7,"mac":"A0:B7:65:12:34:56"}
```

`cup` is the roster slot for that MAC, or `-1`.

#### `hello` — gateway boot announcement

```json
{"t":"hello","v":1,"proto":1,"mac":"24:6F:28:AA:BB:CC"}
```

| Key | Meaning |
| --- | ------- |
| `v` | line-protocol version (`DDM_LINE_PROTO_VERSION` in the sketch) |
| `proto` | `DDM_PROTO_VERSION`, the ESP-NOW wire protocol |
| `mac` | the gateway's own MAC |

Sent once at boot, then every 2 seconds **until the first valid `state` line
has been applied**, then never again until the next reboot. A `hello` tells
DevPi the gateway has (re)booted and needs its state and roster again.

#### `status` — heartbeat and acknowledgement

```json
{"t":"status","gseq":10412,"phase":1,"state_rev":42,"roster_rev":7,"cups":18,"rejects":0,"up_s":5230}
```

| Key | Meaning |
| --- | ------- |
| `gseq` | the gateway's current state broadcast `seq` (stays 0 while silent) |
| `phase` | current `raceState` |
| `state_rev` | `rev` of the last applied `state` line, `0` if none yet |
| `roster_rev` | `rev` of the last applied `roster` line, `0` if none yet |
| `cups` | roster cups heard from within the last 3 seconds (the table's `STALE` threshold) |
| `rejects` | packets rejected for a `DDM_PROTO_VERSION` mismatch |
| `up_s` | seconds since boot |

Sent every 5 seconds, **and immediately after any `state`, `roster` or
`debug` line is applied**. This is the only acknowledgement mechanism.

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

### Down: DevPi → gateway

The gateway reads without blocking, strips a trailing `\r`, ignores empty
lines, and dispatches on the first character: `{` goes to the JSON handler,
anything else to the hand-typed command handler (`help`), which is unchanged.

#### `state` — full snapshot, never a delta

```json
{"t":"state","rev":42,"phase":1,"horse":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20],"scr":[0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0]}
```

All of these must hold or the whole line is rejected with `err` / `invalid`:

- `rev` present, integer ≥ 1
- `phase` integer 0..6 (the `DdmRaceState` range)
- `horse` an array of **exactly** `DDM_MAX_CUPS` (20) integers, each 0..20
  (0 = unassigned), index = cup ID
- `scr` an array of **exactly** `DDM_MAX_CUPS` integers, each 0 or 1

On a valid line, in this order: `phase`, `horse[]` and `scr[]` go into the
broadcast packet in one step (no packet goes out half-applied); `rev` becomes
`state_rev`; demo mode goes **off**; the state broadcast starts if it was not
running yet (see "Silent boot" below); `hello` stops; a `status` is emitted.
The line is idempotent: the same line arriving twice is normal and harmless.
Send the full snapshot on every change, and again whenever a `hello` shows
the gateway has rebooted.

#### `roster` — MAC → cup ID, owned by DevPi

```json
{"t":"roster","rev":7,"macs":["A0:B7:65:12:34:56","A0:B7:65:12:34:57","","","","","","","","","","","","","","","","","",""]}
```

Validation:

- `rev` present, integer ≥ 1
- `macs` an array of **exactly** `DDM_MAX_CUPS` strings; index = cup ID
- each entry is `""` (empty slot) or a well-formed MAC, either case (the
  broadcast address `FF:FF:FF:FF:FF:FF` is refused: it can never be a cup)
- no MAC appears twice

On a valid line: the RAM roster is replaced **entirely** with the new table
(a MAC missing from the new roster becomes unknown, `cup: -1`; per-slot
stats are reset for every slot whose MAC changed); `rev` becomes
`roster_rev`; every MAC whose slot changed compared to the previous roster
is sent a fresh hello-ack carrying
its new ID, which the cup adopts at once (a newly added MAC needs nothing
here: it is still sending HELLO and is acked on the next one); a `status` is
emitted. The gateway also prints one `# [roster] rev N applied: ...` line.

**Roster ownership:**

- **Before any `roster` line** (`roster_rev == 0`) the gateway behaves as it
  always did on the bench: `KNOWN_CUPS[]` seeds the roster, an unknown MAC is
  auto-assigned the next free slot on its HELLO (or on telemetry after a
  gateway reboot) and printed as a `# NEWCUP` line. This keeps the no-Pi
  bench test working.
- **After a `roster` line** (`roster_rev > 0`) DevPi owns identity. The
  gateway stops auto-assigning: a HELLO from an unknown MAC produces a
  `cup_hello` with `cup: -1` and **no ack**, so the cup stays on its MAC
  waiting screen until DevPi sends a roster that includes it.

**Stale IDs.** Every telemetry packet carries the ID the cup believes it has.
Whenever that differs from the sender's roster slot (the `claim` case: a lost
re-ack, an ID from before a gateway reboot, or a cup that took a stray HELLO
for an ack) the gateway re-sends the hello-ack with the slot's ID, so a wrong
ID lasts at most one telemetry period (2 s) once the cup is talking to the
gateway. DevPi still sees the `claim` on that packet.

**Transient ack peers.** ESP-NOW holds 20 peers in all, broadcast included,
and receiving needs no peer entry, so the broadcast address is the gateway's
only permanent peer. A hello-ack is queued (one entry per MAC, newest ID
wins, `DDM_MAX_CUPS + 4` entries, `# ERR ack queue full` if it overflows)
and sent one at a time from `loop()`: the cup's MAC is added as a peer, the
ack is sent, and the peer is deleted once the send callback has reported or
100 ms have passed. Any number of cups can therefore be acked; a dropped or
lost ack is retried by the cup's next HELLO or by the claim-mismatch re-ack.

#### `debug` — runtime toggle for the human-readable output

```json
{"t":"debug","on":true}
```

`on` must be a boolean. Sets the debug flag (next section), then emits
`status`.

### Debug flag and human-readable output

`#define DDM_DEBUG_TEXT 0` near the top of `ddm_gateway.ino` is the boot
default. The flag gates the **periodic** human output only: the per-packet
`# TELEM ...` lines and the 5-second `# ---- CUPS` summary table. Flag off,
neither prints; flag on, both print as they always did, prefixed with `# `.
JSON lines are emitted regardless. Replies to hand-typed commands (`roster`,
`help`, ...) and the boot banner always print, prefixed with `# `. Flip the
flag with the JSON `debug` line or by typing `debug on` / `debug off`.

### Silent boot and `DDM_AUTO_DEMO`

`#define DDM_AUTO_DEMO 0` next to `DDM_DEBUG_TEXT` is the boot default, and
the banner prints both values (`# build: DDM_AUTO_DEMO=0 DDM_DEBUG_TEXT=0`).

With `DDM_AUTO_DEMO 0` (the default, the party build):

1. On boot the gateway sends `hello` and **broadcasts nothing** over
   ESP-NOW. Cups hold their last number on their own.
2. It stays silent **indefinitely**: no timeout, no automatic fallback.
   `hello` repeats every 2 seconds and `status` every 5 the whole time, and
   incoming telemetry and HELLOs are received, acked by the roster rules and
   reported up serial as normal. "Silent" means the state broadcast only.
3. The 500 ms state broadcast starts only when one of these happens: a valid
   JSON `state` line (broadcast that state, demo stays off); the typed `demo`
   command (demo mode, as before); a typed `state`, `horse` or `scratch`
   command (apply it and broadcast the result, demo off).
4. Once started it never stops again until reboot.

The reason: a power blip reboots the gateway in about a second but DevPi
takes most of a minute to boot, and a gateway that started demo mode on its
own would walk wrong horse numbers across 20 cups of real tokens for that
minute.

With `DDM_AUTO_DEMO 1` (bench builds only) the gateway boots straight into
demo mode and broadcasts immediately, exactly as the original bench sketch
did. `hello` still repeats, and a valid JSON `state` line still takes over
and turns demo off.

One caveat while the gateway is silent: a cup that has no gateway MAC yet
*broadcasts* its HELLO, and the current cup firmware takes any
`DDM_MSG_HELLO` frame as its own hello-ack, so cups rebooting together with
the gateway silent can hear each other and adopt cup ID 255 (see the
cup-firmware issues under Status). The gateway cannot prevent that; the
"Stale IDs" re-ack heals it once the state broadcast has started and the cup
is talking to the real gateway again.

## Scale

Each cup weighs the tokens dropped into it with a 1 kg load cell on an HX711
(gain 128, 10 SPS) and counts them by the *step* in the reading, not by
absolute weight: a stack of tokens keeps relaxing for 10–15 s after every
impact, by about 2% of the total load, so absolute weight drifts by roughly one
token in sixty. `ddm_cup.ino` tracks a slow baseline instead and counts a token
when the reading jumps at least half a token above it for three samples in a
row; two tokens dropped together count as two, and a token lifted out counts
down the same way.

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

Two things keep a big stack honest. First, the bench showed that after an
impact the whole stack reads about 2% high and relaxes over 10–15 s, and 2%
of forty tokens is most of a token, so a single drop onto a full cup measured
as 1.8 tokens and rounded to 2 (50 tokens counted as 51–58). The sketch now
takes `OVERSHOOT_PCT` of the load already on the plate off each step before
rounding, with a floor of one token so a gently placed token still counts.
Second, 15 s after the last drop, once the reading has been flat for two
seconds, the cup compares the settled load with its count and corrects it by
at most `SETTLE_MAX_FIX` tokens (1 for now), printing `[settle] tokens 52 ->
50: load ...`; a bigger disagreement is printed as `DISAGREES` and left alone,
and `s` over serial applies it when you know the cup is right; a load within a
third of a token of a half is left alone and reported as ambiguous, so a
mis-calibrated cup cannot flip-flop. Only settled readings are ever used for
that. The `relaxed ... % of load` figure on those lines is the measured
overshoot; it read 1.4–2.5% on the bench, hence `OVERSHOOT_PCT` 2.0.

### The scale has to see all of the weight, every time

Bench, 2026-09-15, second run: with the same 50 tokens in the cup the settled
load read 50.6, then 53.2, then 51.5 tokens as the cup was shaken, and drops
onto a tall stack produced spurious removals of 0.8–1.5 tokens as the stack
shifted. That is a 5% hysteresis in the weighing itself, which no counting
algorithm can remove, and it is why a settle check may not move the count by
more than one token until the base is proven. Things to check, with 50 tokens
in and the reading settled, watching the raw number on the overlay or serial:

- Anything crossing from the weighed cup to the fixed world acts as a spring:
  the HX711 lead if the amplifier is not on the weighed side, the 5V lead into
  the CYD, LED wiring. Flex each one; a lasting change of more than about
  1,000 counts (a sixth of a token) is the culprit. Thin silicone wire with a
  generous loop entering at right angles, or everything on one side.
- The top plate must not touch the enclosure rim or anything else. Press its
  edges lightly, and lift and replace the cup a few times: the settled reading
  should come back to within a few hundred counts every time.
- The load-cell mount must be rigid. The 10–15 s relaxation after every impact
  is plastic creep; a thicker or ribbed mount, or a metal top plate, shortens
  it and shrinks the overshoot.
- Tokens may touch anything that sits on the plate, and nothing that does not.

Once lifting, flexing and shaking no longer move the settled reading, raise
`SETTLE_MAX_FIX` and the settled weight becomes the final word on the count.

### If the token print changes

1. Flash `tools/hx711_calibrate/hx711_calibrate.ino` (same board settings, only
   the HX711 library needed), serial monitor at 115200.
2. Wait 60 s for the HX711 to warm up, then type `t` with the plate empty.
3. Type `c`. Drop one token, type `+`, wait for the `[cal] token N` line.
   Repeat for 8–10 tokens.
4. Type `d`. It prints the two `#define` lines; paste them over the ones at the
   top of `ddm_cup.ino` and reflash every cup.

### Tare

- On boot the cup waits 30 s for the HX711 to settle (the waiting screen says
  `SCALE WARMING UP`), then averages 30 samples as the empty reading.
- While the count is 0 the tare follows the slow baseline, so an empty cup keeps
  re-zeroing itself. Once a token is counted the tare freezes.
- **Hold BOOT for 3 s** to re-tare by hand: the count goes back to 0, the
  current reading becomes "empty", the green LED blinks once and serial prints
  `[tare]`. Keep holding to 15 s and the cup steps its display orientation and
  saves it instead (see the display controller section); `t` over serial also
  tares. A short press still toggles the diagnostic overlay, whose last line
  now reads `TOKENS:n  NET:±counts` plus the scale state. The cup never shows the
  count on a normal screen; the splash display does that.

Serial prints one line per event, `[drop] +1 tokens=7 step=6240 baseline=43512`
or `[remove] -1 ...`, so a bench session can be grepped. If no HX711 answers at
boot the cup runs without counting and the overlay says `no HX711`.

## Touch menu

The CYD's resistive touch panel (XPT2046, its own SPI bus on GPIO 25/33/32/39,
IRQ 36; library **XPT2046_Touchscreen** by Paul Stoffregen) carries a hidden
maintenance menu, because once the board is inside the cup the BOOT button is
unreachable. It is built so a guest poking the screen sees a cup that ignores
them.

- **Open:** press anywhere on the glass and hold for 3 s. A tap does nothing,
  and nothing is drawn until the 3 s are up.
- **Locked during a race:** in `DDM_BETTING_OPEN`, `DDM_FINAL_CALL`,
  `DDM_AT_THE_POST` and `DDM_RUNNING` a completed hold shows `LOCKED DURING
  RACE` for 1.5 s and goes back. The menu opens in `DDM_PRE_RACE`,
  `DDM_WINNER`, `DDM_AFTER_PARTY`, and whenever the cup has heard nothing from
  the gateway for 10 s (a cup with no gateway cannot be in a race).
- **Closes** after 5 s without a touch, and after most actions. On close the
  normal screen is redrawn exactly as it was.

Header: `CUP n   HORSE h` and `V0.5  SEP 17 2026   <MAC>` (firmware version
from `FW_VERSION`, build date from `__DATE__`). Then seven full-width bars:

| Bar | Does |
| --- | --- |
| `TARE` | confirm screen (`TARE?`, `PLATE HAS n TOKENS`, `YES` / `NO`); YES runs the same tare as the 3 s BOOT hold, shows `TARED`, closes |
| `CAL 10` | confirm screen (`PUT EXACTLY 10 TOKENS IN`, `DONE` / `CANCEL`); DONE shows `SETTLING...`, waits up to 6 s for the plate to settle, then does what serial `c10` does (counts/token into NVS, count set to 10), shows the value for 2 s, closes; `NOT SETTLED - TRY AGAIN` returns to the menu |
| `DIAG` | toggles the diagnostic overlay, closes |
| `FLIP 180` | toggles both MADCTL flip bits, saves, redraws, closes |
| `BRIGHT n%` | cycles 100 → 60 → 30 → 100, saved in NVS as `bright` and applied at boot; stays in the menu |
| `ANNOUNCE` | re-sends `DDM_MSG_HELLO` once, shows `SENT`, stays in the menu |
| `CLOSE` | closes |

A tapped bar lights up amber for 120 ms before its action runs. Serial prints
`[menu] open`, `[menu] locked (state N)`, `[menu] tare`, `[menu] cal cpt=N`,
`[menu] flip madctl=0x..`, `[menu] bright N%`, `[menu] announce`,
`[menu] close (...)`.

**Touch mapping.** Hit-testing is on y only: bars are full-width and confirm
screens are top/bottom halves, so rough calibration is fine. The defines at the
top of `ddm_cup.ino` are `TOUCH_X_MIN/MAX` and `TOUCH_Y_MIN/MAX` (raw XPT2046
ranges; raw x is the panel's long axis), `TOUCH_SWAP_XY` (true for portrait:
raw x becomes screen y) and `TOUCH_FLIP_X/Y`. An orientation away from the
batch default (FLIP 180) is followed automatically. **Both panel batches need
their touch axes verified:** type `tc` in the serial monitor (a lone `t` still
tares, after half a second), touch the top, middle and bottom of the glass, and
check the printed `screen y` runs 0 → 319 top to bottom; if it runs the other
way set `TOUCH_FLIP_Y`, if it barely changes set `TOUCH_SWAP_XY` the other way.
A press counts only after three consecutive 50 ms polls, because resistive
panels chatter at the edge of a press.

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
| `ddm_common.h` — ESP-NOW protocol | ✅ defined |
| `ddm_gateway/` — gateway sketch | ✅ implemented (JSON serial line protocol to DevPi, silent boot, roster from DevPi, bench commands, demo mode) |
| `ddm_cup/` — cup sketch | ✅ implemented (bench test: display + ESP-NOW + HX711 token counting, calibrated for the current token print) |

Known protocol gaps, to fix in a v2 of `ddm_common.h` (bump
`DDM_PROTO_VERSION`): no dedicated packet assigns a cup its ID (the
gateway answers `DDM_MSG_HELLO` with the telemetry-struct layout carrying
the assigned ID), and `DdmStatePacket` carries no win/place/show results,
so in `DDM_WINNER` every cup cycles the podium treatment on its own
number.

Known cup-firmware issues, found while the gateway's line protocol was
written (2026-09-18), to fix in `ddm_cup.ino`:

- The cup takes **any** `DDM_MSG_HELLO` frame of the right length as its
  hello-ack (`onDataRecv`), including the HELLOs other cups *broadcast* while
  they have no gateway MAC yet. A cup can therefore adopt `cupId` 0xFF (255)
  and register a neighbouring cup as its gateway; it then stops sending
  HELLO and cannot be acked until it hears a real state broadcast. With the
  gateway silent at boot this is routine rather than rare. Fix: accept an
  ack only when it was unicast to this cup (the receive info's `des_addr`
  is not the broadcast address) and its `cupId` is below `DDM_MAX_CUPS`.
- The acked `cupId` is not range-checked before it indexes `horseForCup[]`
  and `scratched[]` (`computeRendered`, `drawOverlay`, the menu header).

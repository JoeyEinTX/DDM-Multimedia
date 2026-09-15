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

## `ddm_digits.h` is generated — do not hand-edit

> **`ddm_digits.h` is machine-generated output.** It comes from a fontTools
> tracing pipeline that reads a TTF, extracts the outlines of the digit glyphs,
> and emits them as polygon data the cup renders directly.
>
> **Any hand edit is destroyed the next time the generator runs.** To change how
> the digits look, change the source font or the generator settings and
> regenerate. Never patch the header.

## Build settings

Arduino IDE, with the ESP32 board package installed.

- **Board:** `ESP32 Dev Module` (both the gateway and the CYD cups)
- **Serial Monitor:** 115200 baud

Libraries (Library Manager):

- Adafruit GFX Library
- Adafruit ILI9341

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

1. Flash the gateway (plain WROOM-32). It boots straight into **demo mode**
   — broadcasting on channel 6 and walking horse numbers every 3 seconds —
   so it runs standalone off a USB brick, no Pi needed.
2. Flash each cup (CYD, hold BOOT during upload as above). A cup with no
   assigned ID shows a waiting screen with **its own MAC address in large
   gold text**.
3. Power everything up. Cups hello the gateway, get IDs assigned, and start
   showing horse numbers within a few seconds.

### Collecting the four MACs

Cup IDs assigned at runtime are RAM-only and reshuffle when the gateway
reboots. To pin them down:

- Read each MAC off the cup's waiting screen (power cups **without** the
  gateway running and they sit on that screen indefinitely), **or**
- watch the gateway's serial log — every unknown cup produces a `NEWCUP`
  line with the MAC pre-formatted as a `KNOWN_CUPS[]` table row.

Paste the four rows into `KNOWN_CUPS[]` at the top of `ddm_gateway.ino`
(table index = cup ID), reflash the gateway, and IDs are stable forever.

### Reading the gateway output

Serial monitor at 115200. Type `help` for the command list (`state`,
`horse`, `scratch`, `roster`, `demo`). Every telemetry packet prints one
parseable `TELEM ...` line, and every 5 seconds a summary table prints —
this is the range-test readout:

```
---- CUPS seq=1234 state=1 demo=on rejects=0 ----
 id mac                age_ms   drop  rssi  up_rssi  status
  0 A4:CF:12:34:56:78     420      0   -58      -55  OK
  1 A4:CF:12:34:56:9A    4200      9   -77      -71  STALE
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
seconds, the cup compares the settled load with its count and corrects it,
printing `[settle] tokens 52 -> 50: load ...`; a load within a third of a token
of a half is left alone and reported as ambiguous, so a mis-calibrated cup
cannot flip-flop. Only settled readings are ever used for that. The
`relaxed ... % of load` figure on those lines is the measured overshoot; if it
is consistently far from 1.5%, adjust `OVERSHOOT_PCT`.

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

## Status

| Component | State |
| --------- | ----- |
| `ddm_common.h` — ESP-NOW protocol | ✅ defined |
| `ddm_gateway/` — gateway sketch | ✅ implemented (bench test: broadcast, roster, serial commands, demo mode) |
| `ddm_cup/` — cup sketch | ✅ implemented (bench test: display + ESP-NOW + HX711 token counting, calibrated for the current token print) |

Known protocol gaps, to fix in a v2 of `ddm_common.h` (bump
`DDM_PROTO_VERSION`): no dedicated packet assigns a cup its ID (the
gateway answers `DDM_MSG_HELLO` with the telemetry-struct layout carrying
the assigned ID), and `DdmStatePacket` carries no win/place/show results,
so in `DDM_WINNER` every cup cycles the podium treatment on its own
number.

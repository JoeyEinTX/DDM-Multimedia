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

An ESP32 dev board with a 2.8" ILI9341 TFT already wired to it.

**Power: 5V + GND into the P1 connector, and only P1.**

> **CN1 is a 3.3V rail. Feeding 5V into CN1 destroys the board.** These two
> connectors are the same physical part and sit next to each other. Check the
> silkscreen every single time before you plug anything in.

**HX711 → CN1:**

| CN1 pin | HX711 |
| ------- | ----- |
| 3.3V    | VCC   |
| GND     | GND   |
| GPIO22  | DT (data) |
| GPIO27  | SCK (clock) |

Power the HX711 from **3.3V, not 5V**. The HX711 drives its data line at
whatever voltage it is powered from, so a 5V-powered HX711 pushes 5V into a
GPIO that is only rated for 3.3V. It may appear to work for a while and then
kill the pin or the chip.

**Connectors:** both P1 and CN1 are **1.25mm Molex PicoBlade**. They are almost
always sold online as "1.25mm JST", which is technically wrong but is the search
term that finds the right part. They are *not* JST-XH (2.54mm) or JST-PH (2.0mm)
— those will not fit.

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

## Status

| Component | State |
| --------- | ----- |
| `ddm_common.h` — ESP-NOW protocol | ✅ defined |
| `ddm_gateway/` — gateway sketch | ✅ implemented (bench test: broadcast, roster, serial commands, demo mode) |
| `ddm_cup/` — cup sketch | ✅ implemented (bench test: display + ESP-NOW; HX711 not wired in yet, telemetry sends zeros) |

Known protocol gaps, to fix in a v2 of `ddm_common.h` (bump
`DDM_PROTO_VERSION`): no dedicated packet assigns a cup its ID (the
gateway answers `DDM_MSG_HELLO` with the telemetry-struct layout carrying
the assigned ID), and `DdmStatePacket` carries no win/place/show results,
so in `DDM_WINNER` every cup cycles the podium treatment on its own
number.

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

## Status

| Component | State |
| --------- | ----- |
| `ddm_common.h` — ESP-NOW protocol | ✅ defined |
| `ddm_gateway/` — gateway sketch | ⬜ **not yet implemented** (empty folder) |
| `ddm_cup/` — cup sketch | ⬜ **not yet implemented** (empty folder) |

Both sketches are scaffolding only at this point. The protocol header is
complete and is what the sketches will be written against.

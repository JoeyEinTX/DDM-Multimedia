# Parts List (BOM) - DDM Cup Project V3

## Controllers & Computing

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| Raspberry Pi 5 | 1 | 2GB RAM (or 4GB) | Master controller |
| 10" Touchscreen | 1 | Compatible with Pi5 | Official Pi touchscreen recommended |
| ESP32 WROOM-32 DevKit V1 | 1 | WiFi/Bluetooth enabled | LED controller |
| MicroSD Card | 1 | 32GB+ Class 10 | For Raspberry Pi OS |

## LED Components

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| WS2812B LED Strip | ~5m | 144 LEDs/meter, 5V | Cut into 20 × 32-LED segments |
| Level Shifter | 1 | 74AHCT125 or SN74HCT245 | 3.3V → 5V conversion |
| Current Limiting Resistor | 1 | 330Ω-470Ω, 1/4W | Data line protection |
| Filter Capacitors | 3 | 1000µF, 6.3V+ electrolytic | Power injection points |

## Power System

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| Power Supply | 1 | Mean Well LRS-350-5 (5V 60A 300W) | Main PSU |
| DC Distribution Board | 1 | 6-channel fused | With blade fuse holders |
| Blade Fuses | 6 | 10A automotive blade fuses | For LED circuits |
| Power Wire (Red) | 20ft | 14-16 AWG stranded | Main power distribution |
| Power Wire (Black) | 20ft | 14-16 AWG stranded | Main ground distribution |
| AC Power Cable | 1 | IEC C13 for PSU | With appropriate plug for region |

## Display & Indicators

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| OLED Display | 1 | 0.96" or 1.3", I2C, SSD1306 | ESP32 status display |

## Connectors & Wiring

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| JST-VH 2-pin Connectors | 25+ sets | Male + Female | Power connections (5V + GND) |
| JST-SM 2-pin Connectors | 40+ sets | Male + Female | Data jumpers (Data + GND) |
| 22 AWG Wire | 30ft | Stranded, various colors | Data lines, jumpers |
| Heat Shrink Tubing | Assorted | Various sizes | Joint protection |
| Screw Terminals | As needed | For PSU and dist board | Secure connections |

## Enclosures & Mounting

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| Controller Enclosure | 1 | 3D printed or project box | Houses ESP32, shifter, dist board |
| Cup Bases | 20 | 3D printed, ~4" diameter | With LED groove and connector recesses |
| Mounting Hardware | As needed | Screws, standoffs, zip ties | For securing components |

## 3D Printing Materials

| Part | Qty | Specifications | Notes |
|------|-----|----------------|-------|
| 3D Printer Filament | ~500g | Clear/translucent PLA or PETG | For light diffusion |

## Tools & Supplies (Not Consumed)

| Tool | Purpose |
|------|---------|
| Soldering Iron & Solder | LED strip connections |
| Wire Strippers | Wire preparation |
| Crimping Tool | JST connector assembly |
| Multimeter | Testing & troubleshooting |
| Heat Gun | Heat shrink application |
| 3D Printer | Printing cup bases & enclosures |

## Software Requirements

| Software | Purpose | Cost |
|----------|---------|------|
| Arduino IDE | ESP32 programming | Free |
| Raspberry Pi OS | Pi5 operating system | Free |
| Python 3 | Pi5 dashboard | Free |

### Arduino Libraries (Free)
- FastLED (by Daniel Garcia)
- Adafruit GFX Library
- Adafruit SSD1306
- ESP32 Board Support Package

### Python Libraries (Free)
- Flask
- Werkzeug

## Cost Estimate

| Category | Estimated Cost Range |
|----------|---------------------|
| Controllers (Pi5 + ESP32) | $80 - $120 |
| Touchscreen | $60 - $100 |
| LED Strips (5m @ 144/m) | $40 - $80 |
| Power Supply & Distribution | $60 - $100 |
| Connectors & Wiring | $30 - $60 |
| Components (OLED, shifter, etc.) | $20 - $40 |
| 3D Printing Materials | $15 - $30 |
| **Total Estimated Cost** | **$305 - $530** |

*Note: Prices vary by supplier and region. Consider bulk discounts for connectors and wire.*

## Recommended Suppliers

### Electronics
- **Adafruit** - Quality components, good documentation
- **SparkFun** - Reliable electronics supplier
- **Amazon** - Quick shipping for common items
- **AliExpress** - Cost-effective for bulk items (longer shipping)
- **DigiKey/Mouser** - Professional components

### 3D Printing
- **Local printer** - If available
- **Prusa** - High-quality filament
- **Hatchbox** - Good value PLA/PETG

### Power Supply
- **Mean Well** - Direct or through authorized distributors
- Ensure authentic Mean Well products for safety

## Safety Notes

- ⚠️ **High Current System:** 60A @ 5V requires proper fusing and wire gauge
- ⚠️ **AC Voltage:** Use qualified electrician if unsure about AC wiring
- ⚠️ **Ventilation:** Ensure PSU has adequate airflow
- ⚠️ **Fusing:** Each circuit must be properly fused
- ⚠️ **Testing:** Test each section before full assembly
- ⚠️ **Polarity:** Double-check all power connections before powering on

## Optional Upgrades

| Item | Purpose | Notes |
|------|---------|-------|
| Larger Touchscreen | 11.6" or 15.6" | More impressive display |
| Backup Power Supply | Redundancy | In case of primary failure |
| Ethernet Cable | Wired connection | More reliable than WiFi |
| Protective Case | Pi5 protection | Keeps dust out |
| Cable Management | Aesthetics | Cable sleeves, ties, channels |

---

*Last Updated: December 2024*  
*Derby de Mayo Cup Project V3*

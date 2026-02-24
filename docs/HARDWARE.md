# Hardware Documentation - DDM Cup Project

This document describes the hardware architecture, components, wiring, and assembly for the DDM Cup LED display system.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    RASPBERRY PI 5 (Master)                      │
│                    + 10" Touchscreen                            │
│                    Flask Web Interface                          │
│                    Sends commands via WiFi                      │
└─────────────────────────┬───────────────────────────────────────┘
                          │ WiFi Socket (Port 5005)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ESP32 WROOM-32 (LED Controller)              │
│                    WiFi enabled                                 │
│                    Receives commands via socket                 │
│                    Drives WS2812B LEDs via FastLED              │
│                    0.96" OLED status display (I2C)              │
└─────────────────────────┬───────────────────────────────────────┘
                          │ Data Line (GPIO 18)
                          │ 3.3V → 5V Level Shifted
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              20 CUP BASES (Daisy-chained)                       │
│              Cut WS2812B strip segments in groove               │
│              32 LEDs per base = 640 total LEDs                  │
│              Mounted on 112" mantle (~5.9" spacing)             │
└─────────────────────────────────────────────────────────────────┘
```

### Communication Flow

1. **User Input:** Touch dashboard on Pi5 touchscreen
2. **Flask Processing:** Pi5 processes request, sends command to ESP32
3. **Socket Communication:** Command sent over WiFi (port 5005)
4. **LED Control:** ESP32 receives command, updates LED strip via FastLED
5. **Status Display:** OLED shows current state/animation
6. **Feedback:** Pi5 dashboard updates to reflect new state

---

## Parts List (BOM)

### Controllers & Computing

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| Raspberry Pi 5 | 1 | 2GB RAM (or 4GB) | $60-$80 | Master controller |
| 10" Touchscreen | 1 | Compatible with Pi5 | $60-$100 | Official Pi touchscreen recommended |
| ESP32 WROOM-32 DevKit V1 | 1 | WiFi/Bluetooth | $8-$15 | LED controller |
| MicroSD Card | 1 | 32GB+ Class 10 | $10-$15 | For Raspberry Pi OS |

### LED Components

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| WS2812B LED Strip | ~5m | 144 LEDs/meter, 5V | $40-$80 | Cut into 20 × 32-LED segments |
| Level Shifter | 1 | 74AHCT125 or SN74HCT245 | $2-$5 | 3.3V → 5V conversion |
| Current Limiting Resistor | 1 | 330Ω-470Ω, 1/4W | <$1 | Data line protection |
| Filter Capacitors | 3 | 1000µF, 6.3V+ electrolytic | $3-$6 | Power injection points |

### Power System

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| Power Supply | 1 | Mean Well LRS-350-5 (5V 60A 300W) | $40-$60 | Main PSU |
| DC Distribution Board | 1 | 6-channel fused | $15-$25 | With blade fuse holders |
| Blade Fuses | 6 | 10A automotive blade fuses | $5-$10 | For LED circuits |
| Power Wire (Red) | 20ft | 14-16 AWG stranded | $10-$15 | Main power distribution |
| Power Wire (Black) | 20ft | 14-16 AWG stranded | $10-$15 | Main ground distribution |
| AC Power Cable | 1 | IEC C13 for PSU | $5-$10 | With appropriate plug |

### Display & Indicators

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| OLED Display | 1 | 0.96" or 1.3", I2C, SSD1306 | $5-$10 | ESP32 status display |

### Connectors & Wiring

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| JST-VH 2-pin Connectors | 25+ sets | Male + Female | $15-$25 | Power connections (5V + GND) |
| JST-SM 2-pin Connectors | 40+ sets | Male + Female | $10-$20 | Data jumpers (Data + GND) |
| 22 AWG Wire | 30ft | Stranded, various colors | $10-$15 | Data lines, jumpers |
| Heat Shrink Tubing | Assorted | Various sizes | $5-$10 | Joint protection |
| Screw Terminals | As needed | For PSU and dist board | Included | Secure connections |

### Enclosures & Mounting

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| Controller Enclosure | 1 | 3D printed or project box | $10-$20 | Houses ESP32, shifter, dist board |
| Cup Bases | 20 | 3D printed, ~4" diameter | $15-$30 | With LED groove and connector recesses |
| Mounting Hardware | As needed | Screws, standoffs, zip ties | $10-$20 | For securing components |

### 3D Printing Materials

| Part | Qty | Specifications | Approx. Cost | Notes |
|------|-----|----------------|--------------|-------|
| 3D Printer Filament | ~500g | Clear/translucent PLA or PETG | $15-$30 | For light diffusion |

### **Total Estimated Cost: $305 - $530**

---

## ESP32 Pinout Reference

### GPIO Pin Assignments

| ESP32 Pin | Function | Connection | Notes |
|-----------|----------|------------|-------|
| GPIO 18 | LED Data Out | To 74AHCT125 level shifter input (1A) | Primary data output |
| GPIO 21 | I2C SDA | OLED SDA pin | I2C data |
| GPIO 22 | I2C SCL | OLED SCL pin | I2C clock |
| GPIO 2 | Built-in LED | Status indicator (on-board) | Built-in to DevKit |
| VIN | 5V Power In | From fused distribution board (Fuse 6) | Powers ESP32 |
| GND | Ground | Common ground bus | Multiple connections |
| 3V3 | 3.3V Out | OLED VCC, Level shifter LV side | Onboard regulator |

### Power Pins

| ESP32 Pin | Function | Connection |
|-----------|----------|------------|
| VIN | 5V Power In | From fused distribution board (Fuse 6) |
| GND | Ground | Common ground bus |
| 3V3 | 3.3V Out | OLED VCC, Level shifter LV (low voltage side) |

### I2C Bus (OLED Display)

| ESP32 Pin | Function | Connection |
|-----------|----------|------------|
| GPIO 21 | I2C SDA | OLED SDA pin |
| GPIO 22 | I2C SCL | OLED SCL pin |

**OLED Address:** 0x3C (default) or 0x3D (if jumper changed)

---

## Level Shifter Setup

### Purpose
WS2812B LEDs expect 5V data signals, but ESP32 outputs 3.3V. The level shifter converts 3.3V logic to 5V logic.

### Recommended IC
- **74AHCT125** - Quad buffer with 3-state outputs (preferred)
- **SN74HCT245** - Octal bus transceiver (alternative)

### 74AHCT125 Connections

| Shifter Pin | Connection | Notes |
|-------------|------------|-------|
| VCC | 5V from distribution board | High voltage rail |
| GND | Common ground | Shared with ESP32 and LEDs |
| 1A (Input) | ESP32 GPIO 18 (3.3V signal) | Input from ESP32 |
| 1Y (Output) | First cup Data In (5V signal) | Output to LED strip |
| 1OE (Enable) | Tied to GND (always enabled) | Active low enable |

### Additional Protection
- **Resistor:** 330-470Ω between level shifter output and first LED
  - Protects against voltage spikes
  - Reduces signal reflection
- **Capacitor:** 1000µF at first power injection point
  - Smooths power supply
  - Reduces voltage ripple

---

## Power Distribution

### Main Power Supply
**Mean Well LRS-350-5**
- Output: 5V DC
- Current: 60A maximum
- Power: 300W
- Cooling: Forced air (fan included)

### Power Calculations

| Scenario | LEDs Active | Brightness | Current Draw |
|----------|-------------|------------|--------------|
| **Theoretical Max** | 640 | 100% | 38.4A |
| **Realistic Peak** | 640 | 75% (Chaos mode) | 25-30A |
| **Typical Use** | 640 | 50% (Animations) | 15-20A |
| **Idle** | 640 | 25% (Breathing) | 8-12A |
| **Standby** | 640 | 0% (Off) | 0.5A (ESP32 only) |

**Formula:** Current = (LEDs × 60mA) × (Brightness / 100)

### Fuse Allocation

| Fuse | Powers | LED Count | Max Current |
|------|--------|-----------|-------------|
| Fuse 1 | Cups 1-4 + Injection Point 1 | 128 LEDs | 10A fuse, ~8A max |
| Fuse 2 | Cups 5-8 | 128 LEDs | 10A fuse, ~8A max |
| Fuse 3 | Cups 9-12 + Injection Point 2 | 128 LEDs | 10A fuse, ~8A max |
| Fuse 4 | Cups 13-16 | 128 LEDs | 10A fuse, ~8A max |
| Fuse 5 | Cups 17-20 + Injection Point 3 | 128 LEDs | 10A fuse, ~8A max |
| Fuse 6 | ESP32 + OLED | N/A | 2A fuse, <1A typical |

**Why 3 Injection Points?**
- Reduces voltage drop across long LED chain
- Prevents dimming at far end of strip
- Distributes current load more evenly

### Power Injection Points

```
PSU (5V 60A)
    │
    ├─[Fuse 1]─→ Cups 1-4  ←─[Injection Point 1]
    ├─[Fuse 2]─→ Cups 5-8
    ├─[Fuse 3]─→ Cups 9-12 ←─[Injection Point 2]
    ├─[Fuse 4]─→ Cups 13-16
    ├─[Fuse 5]─→ Cups 17-20 ←─[Injection Point 3]
    └─[Fuse 6]─→ ESP32 + OLED
```

---

## LED Strip Connections

### Data Chain
```
ESP32 GPIO 18 → [Level Shifter] → [330Ω Resistor] → Cup 1 → Cup 2 → ... → Cup 20
```

**Key Points:**
- Single data line daisy-chains through all 20 cups
- Each cup has 32 LEDs (LED indices 0-639)
- Ground must be shared at every connection point
- Total distance: ~112 inches (mantle length)

### Each Cup Base Has:

**Power Connections:**
- **Power In:** JST-VH 2-pin connector (5V + GND from distribution board)
- **Total Current per Cup:** Up to 1.92A (32 LEDs × 60mA)

**Data Connections:**
- **Data In:** JST-SM 2-pin connector (Data + GND from previous cup)
- **Data Out:** JST-SM 2-pin connector (Data + GND to next cup)

### Connector Color Code (Recommended)

| Wire | Color | Purpose |
|------|-------|---------|
| +5V | Red | Power positive |
| GND | Black | Ground/return |
| Data | Yellow or White | LED data signal |

---

## Wire Gauge Recommendations

| Application | Wire Gauge | Max Distance | Current | Notes |
|-------------|------------|--------------|---------|-------|
| PSU to Distribution Board | 14 AWG | 6 ft | 60A | Main power trunk |
| Distribution to Injection Points | 16 AWG | 10 ft | 20A | Power drops |
| Cup-to-Cup Power | 18 AWG | 6 inches | 8A | Short runs between cups |
| Data Lines | 22 AWG | 6 inches | <1mA | Twisted pair with ground |

**AWG Ampacity (Chassis Wiring):**
- 14 AWG: 32A
- 16 AWG: 22A
- 18 AWG: 16A
- 22 AWG: 7A

---

## Wiring Diagrams

### Overall System Wiring

```
                    ┌──────────────┐
                    │   AC Mains   │
                    │   110/220V   │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  Mean Well   │
                    │  LRS-350-5   │
                    │  5V 60A PSU  │
                    └──────┬───────┘
                           │ 5V DC
                    ┌──────▼───────────────┐
                    │ 6-Channel Fused      │
                    │ Distribution Board   │
                    └──┬──┬──┬──┬──┬───────┘
                       │  │  │  │  │  │
          ┌────────────┘  │  │  │  │  └──────┐
          │  ┌────────────┘  │  │  │         │
          │  │  ┌────────────┘  │  │         │
          │  │  │  ┌────────────┘  │         │
          │  │  │  │  ┌────────────┘         │
          │  │  │  │  │                      │
          ▼  ▼  ▼  ▼  ▼                      ▼
        [128 LEDs each]                  [ESP32]
        Cups 1-4, 5-8,                   [OLED]
        9-12, 13-16, 17-20               [Level Shifter]
```

### ESP32 to First LED Connection

```
ESP32                Level Shifter         First LED (Cup 1)
                    (74AHCT125)
                    
GPIO 18 ─────────→ 1A (Input)
                    
3V3 ─────────────→ VCC (LV)
                    
                    5V ←──────────── [5V from Fuse 6]
                    
GND ─────────────→ GND ←──────────── [GND from Distribution]
                    
                    1OE → GND
                    
                    1Y (Output) ──→ [330Ω] ──→ DIN (First LED)
                    
                    GND ─────────────────────→ GND (First LED)
```

### OLED Display Connection

```
ESP32          OLED Display
              (SSD1306 I2C)

GPIO 21 ────→ SDA
GPIO 22 ────→ SCL
3V3 ────────→ VCC
GND ────────→ GND
```

---

## Assembly Instructions

### 1. Power System Assembly

1. **Mount PSU** in controller enclosure with adequate ventilation
2. **Wire AC input** (use qualified electrician if unsure)
3. **Connect distribution board** to PSU output:
   - Red wire (14 AWG) to V+ terminal
   - Black wire (14 AWG) to V- terminal
4. **Install fuses** (10A for LED circuits, 2A for ESP32)
5. **Test voltage** before connecting anything

### 2. ESP32 Setup

1. **Mount ESP32** on breadboard or custom PCB
2. **Connect OLED:**
   - SDA to GPIO 21
   - SCL to GPIO 22
   - VCC to 3V3
   - GND to GND
3. **Connect level shifter:**
   - Input (1A) to GPIO 18
   - LV power to ESP32 3V3
   - HV power to 5V from distribution
   - Enable (1OE) to GND
4. **Add resistor** (330-470Ω) between shifter output and first LED data line
5. **Connect ESP32 power:**
   - VIN to Fuse 6 output
   - GND to common ground

### 3. LED Strip Preparation

1. **Measure and mark** LED strip into 20 segments of 32 LEDs each
2. **Cut strip** at designated cut points (copper pads)
3. **Solder wires** to each segment:
   - Power in: Red/Black (5V/GND)
   - Data in: Yellow/Black (Data/GND)
   - Data out: Yellow/Black (Data/GND)
4. **Add JST connectors:**
   - JST-VH for power
   - JST-SM for data
5. **Test each segment** individually before final assembly

### 4. Cup Base Assembly

1. **3D print** 20 cup bases with LED groove and wire channels
2. **Install LED segments** in groove (adhesive backing)
3. **Drill holes** for connectors
4. **Mount connectors** (secure with hot glue if needed)
5. **Label each cup** (1-20) on underside

### 5. Power Distribution Wiring

1. **Cut power wire** to appropriate lengths
2. **Strip and tin** wire ends
3. **Connect to distribution board:**
   - Fuse 1: Cups 1-4 + Injection 1
   - Fuse 2: Cups 5-8
   - Fuse 3: Cups 9-12 + Injection 2
   - Fuse 4: Cups 13-16
   - Fuse 5: Cups 17-20 + Injection 3
   - Fuse 6: ESP32 controller
4. **Add power injection points:**
   - T-tap into power feeds at injection points
   - Add 1000µF capacitor at each point

### 6. Data Chain Wiring

1. **Connect level shifter** output to Cup 1 data in
2. **Daisy-chain cups** 1→2→3→...→20
3. **Use twisted pairs** (data + ground) for noise reduction
4. **Keep data wires short** (~6 inches between cups)
5. **Secure with zip ties** or cable management

### 7. Testing Procedure

**IMPORTANT: Test in stages!**

1. **Power supply test:**
   - Disconnect all loads
   - Power on PSU
   - Verify 5V output with multimeter
   
2. **ESP32 test:**
   - Connect ESP32 only
   - Upload test sketch
   - Verify WiFi connection
   - Check OLED display

3. **Single cup test:**
   - Connect Cup 1 only
   - Send test commands
   - Verify all 32 LEDs light
   - Check colors and brightness

4. **Progressive test:**
   - Add cups one at a time (1→2→3...)
   - Test after each addition
   - Monitor voltage and current
   - Check for dimming at far end

5. **Full system test:**
   - All 20 cups connected
   - Test all animations
   - Verify results modal
   - Check power consumption

---

## Troubleshooting

### LED Issues

| Symptom | Possible Cause | Solution |
|---------|----------------|----------|
| No LEDs light | No power | Check power connections, fuses |
| First LED works, rest don't | Data line broken | Check data connections, level shifter |
| LEDs flicker | Insufficient power | Add power injection, check wire gauge |
| Wrong colors | Incorrect wiring | Verify data pin, check ground connections |
| Dimming at end | Voltage drop | Add power injection points, heavier wire |

### ESP32 Issues

| Symptom | Possible Cause | Solution |
|---------|----------------|----------|
| Won't connect to WiFi | Wrong credentials | Check config.h, verify network |
| No response to commands | Socket error | Check firewall, verify IP address |
| OLED blank | I2C issue | Check SDA/SCL connections, I2C address |
| Freezes/crashes | Power issue | Verify 5V supply to VIN pin |

### Power Issues

| Symptom | Possible Cause | Solution |
|---------|----------------|----------|
| PSU shuts down | Overcurrent | Reduce brightness, check for shorts |
| Blown fuses | Short circuit | Inspect all connections, check polarity |
| PSU noisy/hot | Overload | Reduce load, improve ventilation |

---

## Safety Notes

⚠️ **WARNING - HIGH CURRENT SYSTEM**

- **60A @ 5V can cause fires** if wires are undersized or connections are loose
- **Always use proper wire gauge** for the current load
- **Fuse every circuit** to protect against shorts
- **Double-check polarity** before powering on
- **Never bypass fuses** or use incorrect ratings
- **Ensure adequate ventilation** for PSU (fan must run)
- **Use strain relief** on all cable connections
- **Secure all loose wires** to prevent shorts
- **Test with low brightness first** before full power
- **Have a fire extinguisher nearby** during testing

⚠️ **AC VOLTAGE WARNING**

- **110/220V AC can be lethal**
- **Use qualified electrician** if unsure about AC wiring
- **Disconnect power** before working on PSU
- **Use proper AC-rated components** and enclosures
- **Ground chassis** of metal enclosures

---

## Recommended Suppliers

### Electronics Components
- **Adafruit** - Quality components, excellent documentation
- **SparkFun** - Reliable electronics supplier
- **Amazon** - Quick shipping for common items (Prime)
- **AliExpress** - Cost-effective for bulk items (longer shipping, 2-4 weeks)
- **DigiKey/Mouser** - Professional-grade components, fast shipping

### LED Strips
- **BTF-Lighting** (Amazon/AliExpress) - Good quality WS2812B strips
- **Alitove** (Amazon) - Reliable, fast shipping
- **Adafruit NeoPixel** - Premium quality, higher cost

### Power Supply
- **Mean Well Direct** - Authorized distributors only
- **Digi-Key** - Authorized Mean Well distributor
- **Mouser** - Authorized Mean Well distributor
- ⚠️ **Avoid counterfeit PSUs** - Safety risk!

### 3D Printing
- **Local maker space** - If available, often free or low cost
- **Prusa** - High-quality filament ($25-30/kg)
- **Hatchbox** - Good value PLA/PETG ($20-25/kg)
- **Online printing service** - If no printer available

---

## Maintenance

### Regular Checks
- **Monthly:** Inspect all connections for tightness
- **Monthly:** Check for discolored wires (sign of heat/overload)
- **Monthly:** Clean PSU fan, ensure airflow
- **Yearly:** Replace PSU fan if noisy
- **Yearly:** Test all fuses, replace if questionable

### Storage (Off-Season)
- Disconnect AC power
- Cover to protect from dust
- Store in dry location
- Check for corrosion before next use

---

*Last Updated: December 2024*  
*Derby de Mayo Cup Project V3*

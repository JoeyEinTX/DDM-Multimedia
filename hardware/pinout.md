# ESP32 WROOM-32 Pinout for DDM Cup Project

## Pin Connections

### LED Data Output
| ESP32 Pin | Function | Connection |
|-----------|----------|------------|
| GPIO 18 | LED Data Out | To 74AHCT125 level shifter input (1A) |

### I2C (OLED Display)
| ESP32 Pin | Function | Connection |
|-----------|----------|------------|
| GPIO 21 | I2C SDA | OLED SDA pin |
| GPIO 22 | I2C SCL | OLED SCL pin |

### Power
| ESP32 Pin | Function | Connection |
|-----------|----------|------------|
| VIN | 5V Power In | From fused distribution board (Fuse 6) |
| GND | Ground | Common ground bus |
| 3V3 | 3.3V Out | OLED VCC, Level shifter LV (low voltage side) |

### Status LED
| ESP32 Pin | Function | Connection |
|-----------|----------|------------|
| GPIO 2 | Built-in LED | Status indicator (on-board) |

## Level Shifter (74AHCT125) Connections

| Shifter Pin | Connection |
|-------------|------------|
| VCC | 5V from distribution board |
| GND | Common ground |
| 1A (Input) | ESP32 GPIO 18 (3.3V signal) |
| 1Y (Output) | First cup Data In (5V signal) |
| 1OE (Enable) | Tied to GND (always enabled) |

## OLED Display Connections

| OLED Pin | Connection |
|----------|------------|
| VCC | ESP32 3V3 pin |
| GND | Common ground |
| SDA | ESP32 GPIO 21 |
| SCL | ESP32 GPIO 22 |

## Power Distribution

### Main Power Input
- **Mean Well LRS-350-5:** 5V 60A power supply
- **Output:** Feeds 6-channel fused distribution board

### Fuse Allocation
| Fuse | Powers | Load |
|------|--------|------|
| Fuse 1 | Cups 1-4 (128 LEDs) + Injection Point 1 | ~8A max |
| Fuse 2 | Cups 5-8 (128 LEDs) | ~8A max |
| Fuse 3 | Cups 9-12 (128 LEDs) + Injection Point 2 | ~8A max |
| Fuse 4 | Cups 13-16 (128 LEDs) | ~8A max |
| Fuse 5 | Cups 17-20 (128 LEDs) + Injection Point 3 | ~8A max |
| Fuse 6 | ESP32 + OLED | <1A |

## LED Strip Connections

### Data Chain
```
ESP32 GPIO 18 → Level Shifter → Cup 1 → Cup 2 → ... → Cup 20
```

### Each Cup Base Has:
- **Power In:** JST-VH 2-pin (5V + GND from distribution board)
- **Data In:** JST-SM 2-pin (Data + GND from previous cup)
- **Data Out:** JST-SM 2-pin (Data + GND to next cup)

### Recommended Components
- **Resistor:** 330-470Ω between level shifter output and first LED
- **Capacitors:** 1000µF (6.3V+) at each power injection point (3 total)

## Wire Gauge Recommendations

| Application | Wire Gauge | Notes |
|-------------|------------|-------|
| PSU to Distribution Board | 14 AWG | Main power trunk |
| Distribution to Injection Points | 16 AWG | Power drops |
| Cup-to-Cup Power | 18 AWG | Short runs from dist board |
| Data Lines | 22 AWG | Twisted pair with ground |

## I2C Addresses

| Device | Default Address | Alternative |
|--------|----------------|-------------|
| SSD1306 OLED | 0x3C | 0x3D (if jumper changed) |

## Notes

- **Level Shifter Required:** WS2812B LEDs expect 5V data signals, ESP32 outputs 3.3V
- **Ground Reference:** Data ground and power ground must be connected at each cup
- **Twisted Pairs:** Twist data and ground wires together (3-4 turns per inch) to reduce noise
- **Power Injection:** Three injection points minimize voltage drop across 640 LEDs
- **Fuse Selection:** Use 10A automotive blade fuses in distribution board

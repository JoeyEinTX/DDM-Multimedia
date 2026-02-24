# DDM Cup Project - V3 Changes Summary

## Major Changes from V2 to V3

This document summarizes the key changes made in transitioning from V2 (Pico 2 W) to V3 (ESP32 WROOM-32).

---

## 🔄 Hardware Changes

### LED Controller: Pico 2 W → ESP32 WROOM-32

| Aspect | V2 (Pico 2 W) | V3 (ESP32) |
|--------|---------------|------------|
| **Microcontroller** | RP2350 | ESP32 WROOM-32 |
| **Programming** | MicroPython | Arduino/C++ |
| **LED Library** | NeoPixel (PIO) | FastLED |
| **Data Pin** | GPIO 28 | GPIO 18 |
| **I2C Pins** | GPIO 0 (SDA), GPIO 1 (SCL) | GPIO 21 (SDA), GPIO 22 (SCL) |

### LED System Upgrades

| Component | V2 | V3 | Change |
|-----------|----|----|--------|
| **LEDs per Cup** | 16 | 32 | **+100%** |
| **Total LEDs** | 320 | 640 | **+100%** |
| **Strip Density** | 60 LED/m | 144 LED/m | **+140%** |
| **Strip Length** | ~6m | ~5m | Higher density |

### Power System Upgrades

| Component | V2 | V3 | Reason |
|-----------|----|----|--------|
| **Power Supply** | 5V 30A (LRS-150-5) | 5V 60A (LRS-350-5) | **Double capacity** |
| **Theoretical Max** | 19.2A | 38.4A | Doubled LED count |
| **Realistic Peak** | 10A | 25-30A | Chaos mode with 640 LEDs |
| **Typical Use** | 5-8A | 15-20A | Animated scenes |
| **Injection Points** | 1-2 | 3 required | Voltage drop management |

---

## 💻 Software Changes

### ESP32 Code (New Implementation)

**Language Change:** MicroPython → Arduino/C++

**New Files Created:**
- `esp32/ddm_led_controller/ddm_led_controller.ino` - Main sketch
- `esp32/ddm_led_controller/config.h` - Configuration constants
- `esp32/ddm_led_controller/led_control.h/cpp` - LED control functions
- `esp32/ddm_led_controller/animations.h/cpp` - Animation implementations
- `esp32/ddm_led_controller/oled_display.h/cpp` - OLED display functions

**Key Benefits:**
- FastLED library provides smoother animations
- Better performance with 640 LEDs
- More robust WiFi handling
- Hardware acceleration for LED updates

### Pi5 Dashboard (Complete Rewrite)

**New Implementation:** Flask web application with modern UI

**New Files Created:**
- `pi5/main.py` - Flask application with REST API
- `pi5/config.py` - Configuration settings
- `pi5/communication/esp32_client.py` - Socket client for ESP32
- `pi5/templates/dashboard.html` - Web interface
- `pi5/static/css/ddm_style.css` - DDM-themed styling
- `pi5/static/js/ddm_control.js` - Frontend JavaScript
- `pi5/requirements.txt` - Python dependencies

**Features:**
- Real-time ESP32 status monitoring
- Touch-friendly button interface
- Modal dialog for race results
- Live notifications
- Color-coded status indicators
- Responsive design

---

## 🎨 Animation Improvements

### Enhanced Animations

With FastLED and increased LED count, animations are:
- **Smoother** - Hardware-accelerated updates
- **Brighter** - More LEDs per cup for better visibility
- **More Complex** - FastLED effects (HSV, gradients, nscale8)
- **Synchronized** - Better timing control

### New Animation Features

1. **Breathing Effects** - Smooth sine-wave brightness modulation
2. **Pulse Effects** - Customizable pulse timing
3. **Chaos Mode** - Random rapid color changes on all cups
4. **Results Mode** - Synchronized heartbeat with winner highlighting

---

## 📡 Communication Protocol

### Unchanged Core Protocol

The command protocol remains **backward compatible**:
```
COMMAND:ACTION:VALUE
```

**Same Commands:**
- `PING`, `RESET`
- `LED:ALL_ON`, `LED:ALL_OFF`
- `LED:BRIGHTNESS:XX`
- `LED:COLOR:RRGGBB`
- `LED:CUP:N:RRGGBB`
- `ANIM:*` (all animation commands)
- `RESULTS:W:X:P:Y:S:Z`

### Implementation Changes

| Aspect | V2 | V3 |
|--------|----|----|
| **Server** | Pico 2 W (MicroPython socket) | ESP32 (Arduino WiFi library) |
| **Client** | Pi5 Python socket | Pi5 Flask REST API + socket |
| **Connection** | Direct socket | REST API → Socket client |

---

## 🔌 Pinout Changes

### I2C (OLED)

| Function | Pico 2 W | ESP32 |
|----------|----------|-------|
| SDA | GPIO 0 | GPIO 21 |
| SCL | GPIO 1 | GPIO 22 |

### LED Data

| Function | Pico 2 W | ESP32 |
|----------|----------|-------|
| Data Out | GPIO 28 | GPIO 18 |

### Built-in LED

| Function | Pico 2 W | ESP32 |
|----------|----------|-------|
| Status LED | "LED" pin | GPIO 2 |

---

## 📁 Directory Structure Changes

### Old Structure (V2)
```
pico/
├── main.py
├── config.py
├── led_controller.py
├── oled_display.py
└── lib/
    └── ssd1306.py
```

### New Structure (V3)
```
esp32/
└── ddm_led_controller/
    ├── ddm_led_controller.ino
    ├── config.h
    ├── led_control.h/cpp
    ├── animations.h/cpp
    └── oled_display.h/cpp

pi5/
├── main.py
├── config.py
├── requirements.txt
├── templates/
│   └── dashboard.html
├── static/
│   ├── css/
│   │   └── ddm_style.css
│   └── js/
│       └── ddm_control.js
└── communication/
    └── esp32_client.py

hardware/
├── pinout.md
└── parts_list.md
```

---

## ⚡ Performance Improvements

### LED Update Speed

| Metric | V2 (Pico) | V3 (ESP32) |
|--------|-----------|------------|
| LED Library | NeoPixel | FastLED |
| Update Method | PIO | Hardware SPI-like |
| FPS Target | ~30 | 60 |
| Smoothness | Good | Excellent |

### Power Efficiency

While V3 uses more total power (more LEDs), **per-LED efficiency** is similar:
- Both use WS2812B LEDs (60mA max per LED)
- V3 has better power distribution (3 injection points)
- Reduced voltage drop across longer runs

---

## 🛠️ Setup Changes

### ESP32 Setup (New)

1. Install Arduino IDE
2. Install ESP32 board support
3. Install libraries (FastLED, Adafruit GFX, Adafruit SSD1306)
4. Configure WiFi in `config.h`
5. Upload sketch

### Pi5 Setup (Updated)

1. Install Python dependencies: `pip install -r requirements.txt`
2. Configure ESP32 IP in `config.py`
3. Run: `python main.py`
4. Access dashboard in browser

### Old Pico Setup (V2)

1. Flash MicroPython firmware
2. Upload Python files
3. Configure WiFi in `config.py`
4. Run `main.py`

---

## 🎯 Why These Changes?

### Reasons for Migration to ESP32

1. **Performance** - FastLED handles 640 LEDs better than NeoPixel
2. **Ecosystem** - More Arduino libraries and examples
3. **Stability** - More mature WiFi implementation
4. **Community** - Larger ESP32 community vs Pico 2 W
5. **Scalability** - Easier to add features in C++ vs MicroPython

### Benefits of Increased LED Count

1. **Visibility** - Brighter, more impressive display
2. **Smoothness** - Better gradient/animation effects
3. **Impact** - More engaging for party atmosphere
4. **Flexibility** - Can use lower brightness with more LEDs

---

## 📋 Migration Checklist

If upgrading from V2 to V3:

- [ ] Order new hardware (ESP32, bigger PSU, more LED strip)
- [ ] Install Arduino IDE and libraries
- [ ] Flash ESP32 with new code
- [ ] Update Pi5 dashboard code
- [ ] Redesign/reprint cup bases (32 LEDs vs 16)
- [ ] Rebuild power distribution (60A PSU, 3 injection points)
- [ ] Update wiring (more power drops needed)
- [ ] Test each section before full integration
- [ ] Update documentation with new IP addresses

---

## 🔮 Future Considerations (V4?)

Potential future improvements:
- Wireless power injection (battery-backed cups)?
- Individual cup addressability via ESP32 mesh network?
- Mobile app control (vs web dashboard)?
- Sound-reactive animations (microphone input)?
- Machine learning for automatic race excitement detection?

---

*Document Version: 3.0*  
*Last Updated: December 2024*  
*Derby de Mayo Cup Project*

# Derby de Mayo (DDM) Cup Project — V3

A wireless LED display system for horse racing betting entertainment at an annual Kentucky Derby / Cinco de Mayo party. Features 20 illuminated cup bases on a fireplace mantle, controlled via a Raspberry Pi 5 touchscreen interface, with LED animations synchronized to race events.

## 🏇 Project Overview

**Event Context:** Guests buy $1 tickets, write their name, and place them in cups numbered 1-20 (one per horse). After the race, winners are drawn from the Win/Place/Show cups. Winnings split 70/20/10.

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    RASPBERRY PI 5 (Master)                      │
│                    + 10" Touchscreen                            │
│                    Flask Web Interface                          │
│                    Sends commands via WiFi                      │
└─────────────────────────┬───────────────────────────────────────┘
                          │ WiFi (Socket)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ESP32 WROOM-32 (LED Controller)              │
│                    WiFi enabled                                 │
│                    Receives commands                            │
│                    Drives WS2812B LEDs via FastLED              │
│                    OLED status display                          │
└─────────────────────────┬───────────────────────────────────────┘
                          │ Data Line (3.3V → 5V level shifted)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              20 CUP BASES (Daisy-chained)                       │
│              Cut WS2812B strip segments in groove               │
│              32 LEDs per base = 640 total LEDs                  │
│              Mounted on 112" mantle (~5.9" spacing)             │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Reference

| Spec | Value |
|------|-------|
| Total LEDs | 640 (32 × 20) |
| LED Type | WS2812B strip segments (144/m) |
| Controller | ESP32 WROOM-32 |
| Master | Pi5 + 10" touch |
| Communication | WiFi socket (port 5005) |
| Power | 5V 60A PSU + fused distribution |
| Injection Points | 3 (start, middle, end) |
| Data | Single chain, level-shifted, GPIO 18 |

## Project Structure

```
ddm_cup_v3/
├── README.md                    # This document
│
├── pi5/                         # Raspberry Pi 5 code
│   ├── requirements.txt         # Python dependencies
│   ├── main.py                  # Flask app entry point
│   ├── config.py                # Settings (IP, port, colors)
│   ├── templates/
│   │   └── dashboard.html       # Main control interface
│   ├── static/
│   │   ├── css/
│   │   │   └── ddm_style.css    # DDM-branded styles
│   │   └── js/
│   │       └── ddm_control.js   # Button handlers, socket client
│   └── communication/
│       └── esp32_client.py      # Socket client to send commands
│
├── esp32/                       # ESP32 code (Arduino/C++)
│   └── ddm_led_controller/
│       ├── ddm_led_controller.ino  # Main Arduino sketch
│       ├── config.h             # WiFi creds, LED count, pins
│       ├── led_control.h        # LED strip control functions
│       ├── led_control.cpp
│       ├── animations.h         # Animation functions
│       ├── animations.cpp
│       ├── oled_display.h       # OLED status updates
│       └── oled_display.cpp
│
└── hardware/                    # Reference files
    ├── pinout.md                # ESP32 pinout details
    └── parts_list.md            # BOM with links
```

## Getting Started

### ESP32 Setup

1. **Install Arduino IDE** and the ESP32 board support
2. **Install Required Libraries:**
   - FastLED (by Daniel Garcia)
   - Adafruit GFX Library
   - Adafruit SSD1306
3. **Configure WiFi:**
   - Edit `esp32/ddm_led_controller/config.h`
   - Set `WIFI_SSID` and `WIFI_PASSWORD`
4. **Upload Sketch:**
   - Open `esp32/ddm_led_controller/ddm_led_controller.ino`
   - Select "ESP32 Dev Module" as board
   - Upload to ESP32
5. **Note the IP Address** shown on OLED display or Serial Monitor

### Raspberry Pi 5 Setup

1. **Install Python dependencies:**
   ```bash
   cd pi5
   pip install -r requirements.txt
   ```

2. **Configure ESP32 IP:**
   - Edit `pi5/config.py`
   - Set `ESP32_IP` to your ESP32's IP address

3. **Run the dashboard:**
   ```bash
   python main.py
   ```

4. **Access Dashboard:**
   - Open browser to `http://localhost:5000`
   - Or from another device: `http://<Pi5-IP>:5000`

## Command Protocol

### Command Format
```
COMMAND:ACTION:VALUE
```

### Available Commands

| Command | Description |
|---------|-------------|
| `PING` | Connection test, ESP32 responds `PONG` |
| `LED:ALL_ON` | All LEDs white |
| `LED:ALL_OFF` | All LEDs off |
| `LED:BRIGHTNESS:XX` | Set brightness 0-100 |
| `LED:COLOR:RRGGBB` | Set all LEDs to hex color |
| `LED:CUP:N:RRGGBB` | Set cup N (1-20) to hex color |
| `ANIM:IDLE` | Ambient DDM color breathing |
| `ANIM:WELCOME` | Welcome show |
| `ANIM:BETTING_60` | 60-minute warning |
| `ANIM:BETTING_30` | 30-minute warning |
| `ANIM:FINAL_CALL` | Final call strobe |
| `ANIM:RACE_START` | Green flash, race begins |
| `ANIM:CHAOS` | Maximum intensity final stretch |
| `ANIM:FINISH` | Checkered flag sequence |
| `ANIM:HEARTBEAT` | Synchronized pulse (slows over time) |
| `RESULTS:W:X:P:Y:S:Z` | Set winners (W/P/S cup numbers) |
| `RESET` | Clear all, return to idle |

## Hardware Components

### Master Controller
- Raspberry Pi 5 (2GB RAM)
- 10" Touchscreen
- Raspberry Pi OS (64-bit)

### LED Controller
- ESP32 WROOM-32 DevKit V1
- 0.96" I2C OLED (SSD1306)
- 74AHCT125 Level Shifter (3.3V → 5V)

### LED Display
- WS2812B Strip (144 LED/m) - cut into segments
- 32 LEDs per cup base
- 20 cup bases = 640 total LEDs

### Power System
- Mean Well LRS-350-5 (5V 60A 300W)
- 6-channel fused DC distribution board
- 3 power injection points
- 14-16 AWG main wiring

## DDM Color Palette

```
green:  #228B22 (34, 139, 34)   - Derby forest green
gold:   #FFD700 (255, 215, 0)   - Golden yellow
rose:   #DC143C (220, 20, 60)   - Deep rose/red
white:  #FFFFFF (255, 255, 255) - Clean white
silver: #C0C0C0 (192, 192, 192) - Place
bronze: #CD7F32 (205, 127, 50)  - Show
```

## Development Phases

- [x] **Phase 1:** Set up Pi5 with Flask & ESP32 with Arduino
- [x] **Phase 1:** Establish socket communication (PING/PONG)
- [x] **Phase 1:** Basic LED control (on/off/color)
- [x] **Phase 2:** Build dashboard HTML/CSS/JS
- [x] **Phase 2:** Implement all animations on ESP32
- [ ] **Phase 3:** 3D print cup bases
- [ ] **Phase 3:** Cut and solder LED strip segments
- [ ] **Phase 3:** Build power distribution harness
- [ ] **Phase 4:** Mount all 20 cup bases
- [ ] **Phase 4:** Full system test
- [ ] **Phase 5:** Fine-tune animations and timing
- [ ] **Phase 5:** Party time! 🏇🎉

## Troubleshooting

### ESP32 Not Connecting to WiFi
- Check WiFi credentials in `config.h`
- Verify WiFi signal strength
- Check serial monitor for error messages

### Dashboard Can't Connect to ESP32
- Verify ESP32 IP address in `pi5/config.py`
- Ensure both devices are on same network
- Check firewall settings (port 5005)

### LEDs Not Lighting
- Check power connections
- Verify level shifter wiring
- Test with `LED:ALL_ON` command
- Check data line continuity

### OLED Not Displaying
- Verify I2C connections (SDA GPIO 21, SCL GPIO 22)
- Check OLED address (usually 0x3C or 0x3D)
- Try disabling OLED in `config.h` if needed

## Version History

- **V3.0** (December 2024)
  - Migrated from Pico 2 W to ESP32 WROOM-32
  - Increased to 640 LEDs (32 per cup)
  - Upgraded to 60A power supply
  - Implemented FastLED library for smooth animations
  - Added full Flask web dashboard

- **V2.0** (2024)
  - Pico 2 W with MicroPython
  - 320 LEDs (16 per cup)

- **V1.0** (2023)
  - Initial prototype

## License

This project is for personal/educational use. Feel free to adapt for your own events!

---

*Document Version: 3.0*  
*Last Updated: December 2024*  
*🏇 Derby de Mayo Cup Project 🎉*

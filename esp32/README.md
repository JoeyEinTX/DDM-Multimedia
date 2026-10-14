# ESP32 Starter Code - DDM Cup Project

## Overview

This is a **simplified starter version** of the ESP32 LED controller. It includes:
- ✅ WiFi connection
- ✅ Socket server (port 5005)
- ✅ Basic LED commands (ALL_ON, ALL_OFF, COLOR, BRIGHTNESS, CUP)
- ✅ Single file implementation (no separate headers)

**Animations** and **OLED display** support can be added later.

## Quick Start

### 1. Install Arduino IDE

Download from: https://www.arduino.cc/en/software

### 2. Install ESP32 Board Support

In Arduino IDE:
1. Go to **File → Preferences**
2. Add to "Additional Board Manager URLs":
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Go to **Tools → Board → Boards Manager**
4. Search for "ESP32" and install **"esp32 by Espressif Systems"**

### 3. Install FastLED Library

In Arduino IDE:
1. Go to **Sketch → Include Library → Manage Libraries**
2. Search for **"FastLED"**
3. Install **"FastLED by Daniel Garcia"**

### 4. Configure WiFi

The network goes in `ddm_led_controller/secrets.h`, which is git-ignored: the repo is
public, so the real name and password never go in a tracked file. Copy the example
beside the sketch and fill it in (the sketch won't build without it):

```bash
cp esp32/ddm_led_controller/secrets.example.h esp32/ddm_led_controller/secrets.h
```

```cpp
#define WIFI_SSID "YOUR_WIFI_SSID"        // Your WiFi name
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD" // Your WiFi password
```

### 5. Upload to ESP32

1. Connect ESP32 via USB
2. In Arduino IDE:
   - **Tools → Board → ESP32 Arduino → ESP32 Dev Module**
   - **Tools → Port →** Select your ESP32's port (COM3, COM4, etc.)
   - **Tools → Upload Speed → 115200**
3. Click **Upload** button (→)

### 6. Find ESP32 IP Address

Open **Serial Monitor** (Tools → Serial Monitor, set to 115200 baud):
```
==================================================
   DDM Cup LED Controller - ESP32 STARTER
   Version 3.0 - Basic Commands Only
==================================================

[LED] Initializing 640 LEDs on GPIO 18...
[LED] LED strip initialized
[WiFi] Connecting to: YourWiFiName
..........
[WiFi] Connected!
[WiFi] IP Address: 192.168.1.145    <-- Note this IP!
[WiFi] Signal: -45 dBm
```

**Important:** Write down the IP address!

## Testing Commands

You can test with Python, telnet, or netcat:

### Python Test Script

```python
import socket

ESP32_IP = "192.168.1.145"  # Use your ESP32's IP
ESP32_PORT = 5005

def send_command(cmd):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((ESP32_IP, ESP32_PORT))
    sock.send((cmd + '\n').encode())
    response = sock.recv(1024).decode().strip()
    sock.close()
    print(f"Sent: {cmd} | Response: {response}")
    return response

# Test commands
send_command("PING")                  # Should respond PONG
send_command("LED:ALL_ON")            # All LEDs white
send_command("LED:COLOR:FF0000")      # All LEDs red
send_command("LED:COLOR:00FF00")      # All LEDs green
send_command("LED:COLOR:0000FF")      # All LEDs blue
send_command("LED:BRIGHTNESS:25")     # Dim to 25%
send_command("LED:CUP:1:FFD700")      # Cup 1 gold
send_command("LED:ALL_OFF")           # All LEDs off
```

### Using Telnet (Windows)

```bash
telnet 192.168.1.145 5005
PING
LED:ALL_ON
LED:ALL_OFF
```

## Supported Commands

| Command | Description | Example |
|---------|-------------|---------|
| `PING` | Test connection (responds PONG) | `PING` |
| `LED:ALL_ON` | All LEDs white | `LED:ALL_ON` |
| `LED:ALL_OFF` | All LEDs off | `LED:ALL_OFF` |
| `LED:BRIGHTNESS:XX` | Set brightness (0-100) | `LED:BRIGHTNESS:50` |
| `LED:COLOR:RRGGBB` | All LEDs to hex color | `LED:COLOR:FFD700` |
| `LED:CUP:N:RRGGBB` | Set cup N (1-20) to color | `LED:CUP:5:DC143C` |
| `RESET` | Clear all LEDs | `RESET` |

## LED Configuration

- **Total LEDs:** 640 (32 per cup × 20 cups)
- **Data Pin:** GPIO 18
- **Status LED:** GPIO 2 (built-in)
- **LED Type:** WS2812B (GRB order)
- **Default Brightness:** 128/255 (~50%)

## Troubleshooting

### WiFi Won't Connect
- Check SSID and password are correct
- Ensure WiFi is 2.4GHz (ESP32 doesn't support 5GHz)
- Check router signal strength
- Try moving ESP32 closer to router

### Can't Find Serial Port
- Install CP210x or CH340 USB drivers (depends on your ESP32 board)
- Try different USB cable (some are charge-only)
- Check Device Manager (Windows) for COM ports

### LEDs Don't Light Up
- Check power connections (5V + GND)
- Verify GPIO 18 is connected to data line
- Check level shifter (3.3V → 5V) if using one
- Test with fewer LEDs first (change LED_COUNT to 32)

### Error: "FastLED.h not found"
- Install FastLED library (see step 3 above)
- Restart Arduino IDE

## What's Not Included (Yet)

This starter code intentionally omits:
- ❌ Animations (IDLE, WELCOME, CHAOS, etc.)
- ❌ OLED display support
- ❌ Results mode (Win/Place/Show)
- ❌ Separate config/header files

These can be added later from the full implementation files in this folder.

## Next Steps

1. **Test Basic Commands** - Verify all simple commands work
2. **Connect to Pi5** - Put the ESP32's IP in `pi5/.env` as `DDM_ESP32_IP` (its screen shows it at boot)
3. **Test Dashboard** - Run `python pi5/main.py` and access web interface
4. **Add Animations** - When ready, integrate from `animations.h/cpp`
5. **Add OLED** - When ready, integrate from `oled_display.h/cpp`

## Hardware Connections

### Minimal Working Setup

```
ESP32          →  WS2812B Strip
GPIO 18        →  Data In (via level shifter recommended)
GND            →  GND
VIN (5V)       →  5V power (if using USB power)
```

### Recommended Setup (with Level Shifter)

```
ESP32 GPIO 18  →  74AHCT125 Input (1A)
ESP32 3.3V     →  74AHCT125 LV
ESP32 GND      →  Common GND

5V Supply      →  74AHCT125 VCC
5V Supply      →  LED Strip 5V
Common GND     →  Everything

74AHCT125 Output (1Y)  →  LED Strip Data In
```

## Files in This Folder

| File | Status | Purpose |
|------|--------|---------|
| `ddm_led_controller.ino` | ✅ **START HERE** | Main starter code (single file) |
| `secrets.example.h` | ✅ Copy to `secrets.h` | The Wi-Fi network (`secrets.h` is git-ignored) |
| `config.h` | ⏸️ For later | Configuration constants (future; not included, placeholders only) |
| `led_control.h/cpp` | ⏸️ For later | LED control functions (future) |
| `animations.h/cpp` | ⏸️ For later | Animation implementations (future) |
| `oled_display.h/cpp` | ⏸️ For later | OLED display support (future) |

## Support

- Check Serial Monitor for debug output
- Set baud rate to 115200
- Watch for connection status and error messages

---

**Ready to test!** Upload the code, find your IP, and start sending commands! 🏇✨

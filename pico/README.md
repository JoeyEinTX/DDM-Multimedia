# DDM Cup LED Controller - Pico 2 W

Raspberry Pi Pico 2 W LED controller for the Derby de Mayo Cup Project. This microcontroller receives commands via WiFi from a Raspberry Pi 5 and controls 320 WS2812B LEDs (16 LEDs × 20 cups).

---

## 📁 Project Structure

```
pico/
├── main.py              # Main entry point - WiFi & socket server
├── config.py            # Configuration (WiFi, pins, colors)
├── led_controller.py    # LED strip control class
├── oled_display.py      # OLED status display manager
├── lib/
│   └── ssd1306.py       # OLED driver library
└── README.md            # This file
```

---

## 🔧 Hardware Requirements

| Component | Specification |
|-----------|---------------|
| **Microcontroller** | Raspberry Pi Pico 2 W (RP2350) |
| **LED Strip** | WS2812B (320 LEDs total) |
| **OLED Display** | 0.96" or 1.3" I2C (SSD1306, 128x64) |
| **Level Shifter** | 74AHCT125 or SN74HCT245 |
| **Power Supply** | 5V, 20-30A (Mean Well recommended) |

### Pin Connections

| Pin | Function | Connection |
|-----|----------|------------|
| **GPIO 28** | LED Data Out | To level shifter input |
| **GPIO 0** | I2C SDA | OLED SDA |
| **GPIO 1** | I2C SCL | OLED SCL |
| **VSYS** | 5V Power In | From power supply |
| **GND** | Ground | Common ground |
| **3V3** | 3.3V Out | OLED VCC + level shifter |

---

## 🚀 Quick Start Guide

### Step 1: Install MicroPython on Pico 2 W

1. **Download MicroPython firmware** for Pico 2 W:
   - Visit: https://micropython.org/download/RPI_PICO2/
   - Download the latest `.uf2` file

2. **Flash the Pico**:
   - Hold the **BOOTSEL** button on the Pico
   - While holding it, plug the Pico into your computer via USB
   - Release BOOTSEL - the Pico appears as a USB drive
   - Drag and drop the `.uf2` file onto the drive
   - The Pico will reboot and run MicroPython

### Step 2: Configure WiFi Credentials

1. Open `config.py` in a text editor

2. Update your WiFi credentials:
   ```python
   WIFI_SSID = "YourNetworkName"
   WIFI_PASSWORD = "YourNetworkPassword"
   ```

3. (Optional) Adjust other settings if needed:
   - Change `OLED_ENABLED` to `False` if you don't have an OLED display
   - Adjust LED count if different from 320
   - Modify default brightness

### Step 3: Upload Files to Pico

**Option A: Using Thonny (Recommended for beginners)**

1. Download and install [Thonny IDE](https://thonny.org/)
2. Open Thonny and go to **Tools → Options → Interpreter**
3. Select "MicroPython (Raspberry Pi Pico)"
4. Connect your Pico via USB
5. In Thonny, you'll see the file system in the bottom-right
6. Upload all files:
   - `main.py`
   - `config.py`
   - `led_controller.py`
   - `oled_display.py`
   - Create folder `lib/` and upload `lib/ssd1306.py`

**Option B: Using `ampy` (Command line)**

```bash
# Install ampy
pip install adafruit-ampy

# Find your Pico's port
# Windows: COM3, COM4, etc.
# Linux/Mac: /dev/ttyACM0 or /dev/tty.usbmodem*

# Set port (replace COM3 with your port)
set PORT=COM3

# Upload files
ampy --port %PORT% put config.py
ampy --port %PORT% put led_controller.py
ampy --port %PORT% put oled_display.py
ampy --port %PORT% put main.py
ampy --port %PORT% mkdir lib
ampy --port %PORT% put lib/ssd1306.py lib/ssd1306.py
```

**Option C: Using `mpremote`**

```bash
# Install mpremote
pip install mpremote

# Upload all files
mpremote connect COM3 fs cp config.py :
mpremote connect COM3 fs cp led_controller.py :
mpremote connect COM3 fs cp oled_display.py :
mpremote connect COM3 fs cp main.py :
mpremote connect COM3 fs mkdir lib
mpremote connect COM3 fs cp lib/ssd1306.py :lib/
```

### Step 4: Run the Controller

1. **Disconnect and reconnect** the Pico (power cycle)
2. The Pico will automatically run `main.py` on boot
3. Watch for:
   - Built-in LED blinking during WiFi connection
   - OLED display showing IP address (if connected)
   - Status LED solid on when ready

---

## 🧪 Testing the Controller

### Test 1: Monitor Serial Output

Use Thonny or a serial terminal to watch the boot process:

```
==================================================
DDM Cup LED Controller - Pico 2 W
==================================================
[WiFi] Connecting to 'YourNetwork'...
[WiFi] Connected successfully!
[WiFi] IP Address: 192.168.1.100
==================================================
[Socket] Server listening on 192.168.1.100:5005
[Socket] Ready to receive commands from Pi5
==================================================
```

### Test 2: Send PING Command

From another computer on the same network, test the connection:

**Using Python:**
```python
import socket

# Replace with your Pico's IP address
PICO_IP = "192.168.1.100"
PICO_PORT = 5005

def send_command(cmd):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((PICO_IP, PICO_PORT))
    s.send(cmd.encode('utf-8'))
    response = s.recv(1024).decode('utf-8')
    s.close()
    return response

# Test connection
print(send_command("PING"))  # Should return "PONG"
```

**Using netcat (Linux/Mac):**
```bash
echo "PING" | nc 192.168.1.100 5005
# Should return: PONG
```

### Test 3: Control LEDs

```python
# Turn all LEDs on (white)
send_command("LED:ALL_ON")

# Turn all LEDs off
send_command("LED:ALL_OFF")

# Set brightness to 25%
send_command("LED:BRIGHTNESS:25")

# Set all LEDs to gold
send_command("LED:COLOR:FFD700")

# Set cup 5 to green
send_command("LED:CUP:5:228B22")

# Set cup 10 to rose
send_command("LED:CUP:10:DC143C")

# Reset everything
send_command("RESET")
```

---

## 📡 Command Reference

### Connection Commands
```
PING                    # Test connection (returns "PONG")
RESET                   # Clear all LEDs, return to idle
```

### LED Control Commands
```
LED:ALL_ON              # All LEDs white
LED:ALL_OFF             # All LEDs off
LED:BRIGHTNESS:XX       # Set brightness (0-100)
LED:COLOR:RRGGBB        # Set all LEDs to hex color
LED:CUP:N:RRGGBB        # Set cup N (1-20) to hex color
```

### Animation Commands (Placeholder)
```
ANIM:IDLE               # Green breathing (not fully implemented)
ANIM:WELCOME            # Welcome sequence
```

### Command Response Format
```
OK:ACTION               # Success
PONG                    # Response to PING
ERROR:MESSAGE           # Error occurred
```

---

## 🎨 DDM Color Palette

Pre-defined colors available in `config.py`:

| Color | Hex Code | RGB | Usage |
|-------|----------|-----|-------|
| **Green** | #228B22 | (34, 139, 34) | Derby forest green |
| **Gold** | #FFD700 | (255, 215, 0) | Winner |
| **Rose** | #DC143C | (220, 20, 60) | Deep rose/red |
| **White** | #FFFFFF | (255, 255, 255) | Full brightness |
| **Silver** | #C0C0C0 | (192, 192, 192) | Place |
| **Bronze** | #CD7F32 | (205, 127, 50) | Show |

---

## 🔍 Troubleshooting

### WiFi Connection Fails

**Symptoms:** OLED shows "WiFi Failed" or constant LED blinking

**Solutions:**
1. Double-check WiFi credentials in `config.py`
2. Ensure your WiFi is 2.4GHz (Pico 2 W doesn't support 5GHz)
3. Check WiFi signal strength
4. Try moving Pico closer to router

### OLED Display Not Working

**Symptoms:** Blank OLED screen

**Solutions:**
1. Check I2C connections (GPIO 0 = SDA, GPIO 1 = SCL)
2. Verify OLED address (default: 0x3C)
3. Try different I2C address: modify `ssd1306.py` if your OLED uses 0x3D
4. Set `OLED_ENABLED = False` in `config.py` to disable

### LEDs Not Responding

**Symptoms:** Commands accepted but LEDs don't light

**Solutions:**
1. Check power supply (5V, sufficient amperage)
2. Verify level shifter connections
3. Check LED strip data connection to GPIO 28
4. Test with low brightness: `LED:BRIGHTNESS:10` then `LED:ALL_ON`
5. Measure voltage at LED strip (should be ~5V)

### Socket Connection Refused

**Symptoms:** Can't connect from Pi5 or test computer

**Solutions:**
1. Verify Pico's IP address on serial console or OLED
2. Ensure both devices are on same network
3. Check firewall settings
4. Try pinging Pico's IP address
5. Restart Pico (power cycle)

### USB Connection Issues (During Upload)

**Solutions:**
1. Try a different USB cable (some are power-only)
2. Try a different USB port
3. Reinstall MicroPython firmware
4. Check Device Manager (Windows) or `ls /dev/tty*` (Linux/Mac)

---

## 📊 Performance Notes

- **LED Update Rate:** ~30-60 FPS achievable
- **Socket Response Time:** < 50ms typical
- **Memory Usage:** ~40-50% of Pico's 264KB RAM
- **WiFi Range:** Standard 2.4GHz WiFi range
- **Max Current:** Plan for 10A @ 5V with all LEDs at 50% brightness

---

## 🔜 Next Steps

### Ready for Phase 2? 

Once basic LED control is working:

1. **Add Animation System:** Implement full animation sequences in `animations.py`
2. **Build Pi5 Dashboard:** Create Flask web interface for control
3. **Hardware Assembly:** Build cup bases, wire LED strips, assemble enclosure
4. **Integration Testing:** Test full system end-to-end
5. **Party Time!** 🏇🎉

### Future Enhancements

- [ ] Persistent configuration (save settings to flash)
- [ ] Web-based configuration page (hosted on Pico)
- [ ] OTA firmware updates
- [ ] Race timer synchronization
- [ ] Audio input for music sync
- [ ] Multiple animation layers
- [ ] Error logging and diagnostics

---

## 📚 Additional Resources

- [MicroPython Documentation](https://docs.micropython.org/)
- [Pico 2 W Datasheet](https://datasheets.raspberrypi.com/pico/pico-2-w-datasheet.pdf)
- [WS2812B LED Datasheet](https://cdn-shop.adafruit.com/datasheets/WS2812B.pdf)
- [SSD1306 OLED Guide](https://learn.adafruit.com/monochrome-oled-breakouts)

---

## 📄 License

This project is part of the DDM Cup Project V3.

*Last Updated: December 2024*  
*Version: 3.0 - Foundation/Starter Code*

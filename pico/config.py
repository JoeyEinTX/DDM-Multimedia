# config.py - Configuration settings for DDM Cup LED Controller (Pico 2 W)

# WiFi Credentials
WIFI_SSID = "YOUR_WIFI_SSID"          # Replace with your WiFi network name
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"  # Replace with your WiFi password

# Socket Server Settings
SOCKET_PORT = 5005                     # Port for receiving commands from Pi5
SOCKET_TIMEOUT = 1.0                   # Socket timeout in seconds (non-blocking)

# LED Configuration
LED_PIN = 28                           # GPIO pin for LED data output
LED_COUNT = 320                        # Total LEDs (16 per cup × 20 cups)
LEDS_PER_CUP = 16                      # LEDs in each cup base
NUM_CUPS = 20                          # Total number of cups

# OLED Display Configuration
OLED_ENABLED = True                    # Set to False to disable OLED
OLED_SDA_PIN = 0                       # I2C SDA pin
OLED_SCL_PIN = 1                       # I2C SCL pin
OLED_WIDTH = 128                       # OLED display width in pixels
OLED_HEIGHT = 64                       # OLED display height in pixels (or 32 for 0.96")

# Default LED Settings
DEFAULT_BRIGHTNESS = 50                # Default brightness (0-100)
MAX_BRIGHTNESS = 100                   # Maximum allowed brightness

# DDM Color Palette (RGB tuples)
DDM_COLORS = {
    "green":  (34, 139, 34),           # #228B22 - Derby forest green
    "gold":   (255, 215, 0),           # #FFD700 - Golden yellow
    "rose":   (220, 20, 60),           # #DC143C - Deep rose/red
    "white":  (255, 255, 255),         # #FFFFFF - Clean white
    "black":  (0, 0, 0),               # #000000 - Off
    "silver": (192, 192, 192),         # #C0C0C0 - Place
    "bronze": (205, 127, 50),          # #CD7F32 - Show
}

# Status LED (Built-in)
STATUS_LED_PIN = "LED"                 # Use built-in LED for status indication

# config.example.py - Configuration template for DDM Horse Dashboard (Raspberry Pi 5)
# 
# SETUP INSTRUCTIONS:
# 1. Copy this file to config.py: cp config.example.py config.py
# 2. Edit config.py with your local settings
# 3. config.py is in .gitignore and won't be tracked by git

# ESP32 Connection Settings
ESP32_IP = "192.168.1.100"  # Replace with your ESP32's IP address
ESP32_PORT = 5005
SOCKET_TIMEOUT = 5.0  # Seconds

# Flask Settings
FLASK_HOST = "0.0.0.0"  # Listen on all interfaces
FLASK_PORT = 5000
FLASK_DEBUG = False  # Set to False in production

# DDM Color Palette (RGB tuples)
DDM_COLORS = {
    "green":  (34, 139, 34),    # #228B22 - Derby forest green
    "gold":   (255, 215, 0),    # #FFD700 - Golden yellow
    "rose":   (220, 20, 60),    # #DC143C - Deep rose/red
    "white":  (255, 255, 255),  # #FFFFFF - Clean white
    "black":  (0, 0, 0),        # #000000 - Off
    "silver": (192, 192, 192),  # #C0C0C0 - Place
    "bronze": (205, 127, 50),   # #CD7F32 - Show
}

# System Information
SYSTEM_NAME = "Derby de Mayo Control Center"
VERSION = "3.0.0"
NUM_CUPS = 20
TOTAL_LEDS = 640

# Weather API Settings (WeatherAPI.com)
# Get your free API key at: https://www.weatherapi.com/signup.aspx
WEATHER_API_KEY = "your_api_key_here"  # Replace with your WeatherAPI.com key
WEATHER_LOCATION = "Dallas,TX"  # Change to your location
WEATHER_CACHE_MINUTES = 30

import os

# ---------------------------------------------------------------------------
# La Quiniela gateway bridge (pi5/la_quiniela/, see pi5/LQ_BRIDGE.md)
#
# Every key here can be overridden by an environment variable of the same
# name prefixed DDM_ (for example DDM_LQ_SERIAL_PORT=/dev/pts/3 points the
# app at a simulator without editing this file).
# ---------------------------------------------------------------------------
def _lq_env(name, default):
    raw = os.environ.get('DDM_' + name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(default, int):
        return int(raw)
    return raw

LQ_BRIDGE_ENABLED = _lq_env('LQ_BRIDGE_ENABLED', True)    # master switch
LQ_SERIAL_PORT = _lq_env('LQ_SERIAL_PORT', '')            # empty = bridge idles; on DevPi a /dev/serial/by-id/... path, never /dev/ttyUSB0
LQ_SERIAL_BAUD = _lq_env('LQ_SERIAL_BAUD', 115200)
LQ_SERIAL_LINES = _lq_env('LQ_SERIAL_LINES', 'leave')     # 'leave' never touches DTR/RTS (right for the CP2102 board); 'low' holds both low, which reboots that board
LQ_HEARTBEAT_LOG_S = _lq_env('LQ_HEARTBEAT_LOG_S', 10)    # per-cup heartbeat interval for logging and display refresh
LQ_CUP_OFFLINE_S = _lq_env('LQ_CUP_OFFLINE_S', 6)         # no telemetry for this long = cup offline
LQ_GATEWAY_OFFLINE_S = _lq_env('LQ_GATEWAY_OFFLINE_S', 12) # no line at all for this long = gateway offline
LQ_DEAF_REOPEN_S = _lq_env('LQ_DEAF_REOPEN_S', 20)        # port open but no valid line for this long = close it and open it again
LQ_REOPEN_MIN_GAP_S = _lq_env('LQ_REOPEN_MIN_GAP_S', 30)  # never reopen more often than this
LQ_DEV_ENDPOINTS = _lq_env('LQ_DEV_ENDPOINTS', False)     # enables POST /api/lq/dev/* (testing only)

# The betting board the splash display's TV page renders (GET /api/quiniela,
# see "Betting board" in pi5/LQ_BRIDGE.md). Same names as the splash used;
# DDM_TOKEN_VALUE (float), DDM_QUINIELA_LOG (1/true/yes/on) and
# DDM_QUINIELA_BOARD_STATES (a comma list, "1,2,3,4") override them.
TOKEN_VALUE = 1.00  # dollars per token, for the board's POT
QUINIELA_LOG = True  # pi5/data/quiniela_YYYY-MM-DD.jsonl, one line per token/scratch/state change
QUINIELA_BOARD_STATES = [1, 2, 3, 4]  # race states in which the splash board owns the TV

# How La Quiniela pays ("Betting board" in pi5/LQ_BRIDGE.md): after the race one
# token is drawn from the WIN cup, one from PLACE and one from SHOW, and each
# drawn token's owner takes that cup's whole prize, a fixed fraction of the pot.
# PLACE and SHOW are rounded half up to whole dollars, WIN takes the remainder,
# so the three always sum to the pot. The three fractions should sum to 1 (the
# board warns once if they do not). Overrides: DDM_LQ_SPLIT_WIN / _PLACE / _SHOW
# (floats) and DDM_LQ_CHYRON_LINES (lines separated by |).
LQ_SPLIT_WIN = 0.60
LQ_SPLIT_PLACE = 0.25
LQ_SPLIT_SHOW = 0.15
LQ_CHYRON_LINES = [  # what crawls along the bottom of the TV board
    "TOTALS BASED ON CHEAP CHINESE ELECTRONICS · FINAL RESULTS HAND COUNTED",
    "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS",
]

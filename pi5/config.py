# config.py - Configuration settings for DDM Horse Dashboard (Raspberry Pi 5)

# ESP32 Connection Settings
ESP32_IP = "10.0.0.44"  # DDM ESP32 controller
ESP32_PORT = 5005
SOCKET_TIMEOUT = 5.0  # Seconds

# Tote Board Connection Settings (Interstate75 LED Display)
TOTE_IP = "10.0.0.124"
TOTE_PORT = 80
TOTE_TIMEOUT = 2.0  # Seconds
TOTE_ENABLED = True

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
VERSION = "3.2.0"
NUM_CUPS = 20
TOTAL_LEDS = 640

# Animation Params Cache
import os
PARAMS_FILE = os.path.join(os.path.dirname(__file__), 'animation_params.json')

# Load .env file if present (API keys etc)
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    from dotenv import load_dotenv
    load_dotenv(dotenv_path)

# Anthropic API key for AI horse search
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Race setup data file
RACE_SETUP_FILE = os.path.join(os.path.dirname(__file__), 'data', 'race_setup.json')

# Animation library data files
ANIMATION_REGISTRY_FILE = os.path.join(os.path.dirname(__file__), 'data', 'animation_registry.json')
ANIMATION_ASSIGNMENTS_FILE = os.path.join(os.path.dirname(__file__), 'data', 'animation_assignments.json')

# Weather API Settings (WeatherAPI.com)
WEATHER_API_KEY = "f2296dce2c55403e8bb231111250612"
WEATHER_LOCATION = "Dallas,TX"
WEATHER_CACHE_MINUTES = 30


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
LQ_HEARTBEAT_LOG_S = _lq_env('LQ_HEARTBEAT_LOG_S', 10)    # per-cup heartbeat interval for logging and display refresh
LQ_CUP_OFFLINE_S = _lq_env('LQ_CUP_OFFLINE_S', 6)         # no telemetry for this long = cup offline
LQ_GATEWAY_OFFLINE_S = _lq_env('LQ_GATEWAY_OFFLINE_S', 12) # no line at all for this long = gateway offline
LQ_DEV_ENDPOINTS = _lq_env('LQ_DEV_ENDPOINTS', False)     # enables POST /api/lq/dev/* (testing only)

# La Quiniela betting board (GET /api/quiniela; "Betting board" in pi5/LQ_BRIDGE.md)
TOKEN_VALUE = 1.00  # dollars per token, for the board's POT
QUINIELA_LOG = True  # pi5/data/quiniela_YYYY-MM-DD.jsonl, one line per token/scratch/state change
QUINIELA_BOARD_STATES = [1, 2, 3, 4]  # race states in which the splash board owns the TV

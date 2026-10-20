# config.py - Configuration settings for DDM Horse Dashboard (Raspberry Pi 5)
#
# This file is tracked and the repo is public, so it holds only code and safe
# defaults. Nothing secret and nothing that belongs to one box lives here: those
# come from the environment, and on DevPi from pi5/.env (git-ignored; copy
# pi5/.env.example and fill it in). Every setting read through _env() below is
# the variable of the same name prefixed DDM_ (DDM_ESP32_IP=10.0.0.42), and a
# variable already set in the environment wins over pi5/.env.

import os
import secrets as _secrets

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')


def _read_env_file(path):
    """KEY=VALUE lines of a .env file as a dict: blank lines and # comments
    skipped, an optional leading `export`, a value in matching quotes taken as
    it is, an unquoted one trimmed and cut at ` #`. Standard library only, so
    the tools that read this file (pi5/tools/wipe.py, the installer) need
    nothing installed."""
    values = {}
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        name, sep, value = line.partition('=')
        name = name.strip()
        if not sep or not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        values[name] = value
    return values


def _load_env_file(path):
    """pi5/.env into the environment; a variable already set is left alone."""
    for name, value in _read_env_file(path).items():
        os.environ.setdefault(name, value)


_load_env_file(ENV_FILE)


def _env(name, default):
    """DDM_<name> from the environment, typed like its default (a bool from
    1/true/yes/on, an int, a float), or the default when unset."""
    raw = os.environ.get('DDM_' + name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


_lq_env = _env   # the La Quiniela settings' name for it


# ---------------------------------------------------------------------------
# Secrets: empty unless set in pi5/.env, and the feature they feed is off.
# ---------------------------------------------------------------------------
WEATHER_API_KEY = _env('WEATHER_API_KEY', '')        # WeatherAPI.com; empty = no weather (the dashboard panel, the TV's crawl)
ANTHROPIC_API_KEY = (_env('ANTHROPIC_API_KEY', '')   # the track's odds poller (la_quiniela/odds.py); empty = it can't run
                     or os.environ.get('ANTHROPIC_API_KEY', ''))
FLASK_SECRET_KEY = _env('FLASK_SECRET_KEY', '') or _secrets.token_hex(24)   # Flask's own; empty = a new random one each start (nothing keeps a session)

# ---------------------------------------------------------------------------
# DevPi's own settings: what only one box knows. The defaults work on any dev
# machine and are wrong for no one.
# ---------------------------------------------------------------------------
# ESP32 Connection Settings (the LED controller on the mantle)
ESP32_IP = _env('ESP32_IP', '')        # empty = no LED controller: the LEDs are off, everything else runs
ESP32_PORT = _env('ESP32_PORT', 5005)
SOCKET_TIMEOUT = 5.0  # Seconds

# Tote Board Connection Settings (Interstate75 LED Display)
TOTE_IP = _env('TOTE_IP', '')          # empty = no tote board
TOTE_PORT = _env('TOTE_PORT', 80)
TOTE_TIMEOUT = 2.0  # Seconds
TOTE_ENABLED = _env('TOTE_ENABLED', bool(TOTE_IP))

# Flask Settings
FLASK_HOST = _env('FLASK_HOST', "0.0.0.0")  # Listen on all interfaces
FLASK_PORT = _env('FLASK_PORT', 5000)
FLASK_DEBUG = False  # Set to False in production

# Weather API Settings (WeatherAPI.com; the key is a secret, above)
WEATHER_LOCATION = _env('WEATHER_LOCATION', "Dallas,TX")
WEATHER_CACHE_MINUTES = 30

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
PARAMS_FILE = os.path.join(os.path.dirname(__file__), 'animation_params.json')

# Race setup data file
RACE_SETUP_FILE = os.path.join(os.path.dirname(__file__), 'data', 'race_setup.json')

# Animation library data files
ANIMATION_REGISTRY_FILE = os.path.join(os.path.dirname(__file__), 'data', 'animation_registry.json')
ANIMATION_ASSIGNMENTS_FILE = os.path.join(os.path.dirname(__file__), 'data', 'animation_assignments.json')


# ---------------------------------------------------------------------------
# La Quiniela gateway bridge (pi5/la_quiniela/, see pi5/LQ_BRIDGE.md)
#
# Every key here can be overridden by an environment variable of the same
# name prefixed DDM_ (for example DDM_LQ_SERIAL_PORT=/dev/pts/3 points the
# app at a simulator without editing this file).
# ---------------------------------------------------------------------------
LQ_BRIDGE_ENABLED = _lq_env('LQ_BRIDGE_ENABLED', True)    # master switch
LQ_SERIAL_PORT = _lq_env('LQ_SERIAL_PORT', '')            # empty = bridge idles; on DevPi a /dev/serial/by-id/... path in pi5/.env, never /dev/ttyUSB0
LQ_SERIAL_BAUD = _lq_env('LQ_SERIAL_BAUD', 115200)
LQ_HEARTBEAT_LOG_S = _lq_env('LQ_HEARTBEAT_LOG_S', 10)    # per-cup heartbeat interval for logging and display refresh
LQ_CUP_OFFLINE_S = _lq_env('LQ_CUP_OFFLINE_S', 6)         # no telemetry for this long = cup offline
LQ_GATEWAY_OFFLINE_S = _lq_env('LQ_GATEWAY_OFFLINE_S', 12) # no line at all for this long = gateway offline

# La Quiniela betting board (GET /api/quiniela; "Betting board" in pi5/LQ_BRIDGE.md)
TOKEN_VALUE = 1.00  # dollars per token, for the board's POT
QUINIELA_LOG = True  # pi5/data/quiniela_YYYY-MM-DD.jsonl, one line per token/scratch/state change
QUINIELA_BOARD_STATES = [1, 2, 3, 4, 5]  # the model's board_states; the splash's pages have their own sets and read only that it is not empty (LQ_BRIDGE.md)
# How La Quiniela pays: one token is drawn per cup and takes that cup's whole prize, these fractions of the pot (DDM_LQ_SPLIT_*, DDM_LQ_CHYRON_LINES with | between lines override)
LQ_SPLIT_WIN = 0.60
LQ_SPLIT_PLACE = 0.25
LQ_SPLIT_SHOW = 0.15
LQ_CHYRON_LINES = ["TOTALS BASED ON CHEAP CHINESE ELECTRONICS - FINAL RESULTS HAND COUNTED", "NOT AFFILIATED WITH CHURCHILL DOWNS OR ANYONE WITH LAWYERS"]

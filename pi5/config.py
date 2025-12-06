# config.py - Configuration settings for DDM Horse Dashboard (Raspberry Pi 5)

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

# Weather API Settings (OpenWeatherMap)
OPENWEATHER_API_KEY = ""  # Add your API key here
WEATHER_LOCATION = "Dallas,TX,US"
WEATHER_UNITS = "imperial"  # imperial = Fahrenheit, metric = Celsius
WEATHER_CACHE_MINUTES = 30

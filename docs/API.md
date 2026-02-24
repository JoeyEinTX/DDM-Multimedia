# API Documentation - DDM Cup Project

This document describes the REST API endpoints provided by the Flask backend (`pi5/main.py`).

## Base URL

```
http://<Pi5-IP>:5000
```

Default: `http://localhost:5000` when running locally.

---

## REST API Endpoints

### General Endpoints

#### `GET /`
**Description:** Renders the main dashboard HTML page.

**Response:** HTML page with dashboard interface.

---

#### `GET /api/ping`
**Description:** Test connection to ESP32.

**Response:**
```json
{
  "success": true,
  "status": "ONLINE",
  "response": "PONG"
}
```

---

#### `GET /api/status`
**Description:** Get system status and configuration.

**Response:**
```json
{
  "esp32_connected": true,
  "esp32_ip": "192.168.1.100",
  "esp32_port": 5005,
  "num_cups": 20,
  "total_leds": 640,
  "version": "3.0"
}
```

---

### Command Endpoints

#### `POST /api/command`
**Description:** Send a raw command string to ESP32.

**Request Body:**
```json
{
  "command": "LED:ALL_ON"
}
```

**Response:**
```json
{
  "success": true,
  "command": "LED:ALL_ON",
  "response": "OK"
}
```

**Available Commands:**
- `PING` - Test connection
- `LED:ALL_ON` - All LEDs white
- `LED:ALL_OFF` - All LEDs off
- `LED:BRIGHTNESS:XX` - Set brightness (0-100)
- `LED:COLOR:RRGGBB` - Set all LEDs to hex color
- `LED:CUP:N:RRGGBB` - Set cup N (1-20) to hex color
- `LED:TEST:R,G,B,BRIGHTNESS` - RGB test mode
- `ANIM:*` - Animation commands (see Animation Endpoints)
- `CUP:LOCK:N:R:G:B` - Lock cup to RGB color
- `CUP:UNLOCK:N` or `CUP:UNLOCK:ALL` - Unlock cup(s)
- `RESET` - Reset to idle

---

### LED Control Endpoints

#### `POST /api/led/all_on`
**Description:** Turn all LEDs on (white).

**Response:**
```json
{
  "success": true,
  "response": "OK"
}
```

---

#### `POST /api/led/all_off`
**Description:** Turn all LEDs off.

**Response:**
```json
{
  "success": true,
  "response": "OK"
}
```

---

#### `POST /api/led/brightness`
**Description:** Set LED brightness.

**Request Body:**
```json
{
  "brightness": 75
}
```

**Response:**
```json
{
  "success": true,
  "brightness": 75,
  "response": "OK"
}
```

**Parameters:**
- `brightness` (integer, 0-100): Brightness percentage

---

#### `POST /api/led/color`
**Description:** Set all LEDs to a specific color.

**Request Body:**
```json
{
  "color": "FFD700"
}
```

**Response:**
```json
{
  "success": true,
  "color": "FFD700",
  "response": "OK"
}
```

**Parameters:**
- `color` (string): Hex color code (without #)

---

#### `POST /api/led/cup`
**Description:** Set a specific cup to a color.

**Request Body:**
```json
{
  "cup": 5,
  "color": "FF0000"
}
```

**Response:**
```json
{
  "success": true,
  "horse": 5,
  "color": "FF0000",
  "response": "OK"
}
```

**Parameters:**
- `cup` (integer, 1-20): Cup number
- `color` (string): Hex color code (without #)

---

### Animation Endpoints

#### `POST /api/animation/<anim_name>`
**Description:** Start an animation.

**URL Parameters:**
- `anim_name` (string): Name of the animation

**Available Animations:**
- `IDLE` - Ambient DDM color breathing
- `WELCOME` - Welcome show
- `BETTING_60` - 60-minute warning
- `BETTING_30` - 30-minute warning
- `FINAL_CALL` - Final call strobe
- `RACE_START` - Green flash, race begins
- `CHAOS` - Maximum intensity final stretch
- `FINISH` - Checkered flag sequence
- `HEARTBEAT` - Synchronized pulse (slows over time)
- `HEARTBEAT_FAST` - Fast heartbeat
- `RESULTS_ACTIVE` - Winners + heartbeat on others

**Response:**
```json
{
  "success": true,
  "animation": "CHAOS",
  "response": "OK"
}
```

**Special Case: RESULTS_ACTIVE**

For `RESULTS_ACTIVE`, you can provide winner positions in the request body:

**Request Body:**
```json
{
  "win": 5,
  "place": 12,
  "show": 8
}
```

This will light up the winning cups in gold/silver/bronze with heartbeat on others.

---

### Cup Lock/Unlock Endpoints

#### `POST /api/cup/lock`
**Description:** Lock a cup to a specific color during animations.

**Request Body:**
```json
{
  "cup": 5,
  "r": 255,
  "g": 215,
  "b": 0
}
```

**Response:**
```json
{
  "success": true,
  "cup": 5,
  "color": {"r": 255, "g": 215, "b": 0},
  "response": "OK"
}
```

**Parameters:**
- `cup` (integer, 1-20): Cup number
- `r` (integer, 0-255): Red value
- `g` (integer, 0-255): Green value
- `b` (integer, 0-255): Blue value

---

#### `POST /api/cup/unlock`
**Description:** Unlock cup(s) to return to animation.

**Request Body:**
```json
{
  "cup": 5
}
```
Or unlock all:
```json
{
  "cup": "ALL"
}
```

**Response:**
```json
{
  "success": true,
  "cup": 5,
  "response": "OK"
}
```

---

### Results Endpoints

#### `GET /api/results`
**Description:** Get current race results.

**Response (with results):**
```json
{
  "success": true,
  "results": {
    "win": 5,
    "place": 12,
    "show": 8,
    "timestamp": "2024-12-15T20:30:00.123456"
  }
}
```

**Response (no results):**
```json
{
  "success": false,
  "message": "No results available"
}
```

---

#### `POST /api/results`
**Description:** Set race results.

**Request Body:**
```json
{
  "win": 5,
  "place": 12,
  "show": 8
}
```

**Response:**
```json
{
  "success": true,
  "results": {
    "win": 5,
    "place": 12,
    "show": 8
  },
  "response": "OK"
}
```

**Validation:**
- Win, Place, and Show must be different cup numbers
- Returns 400 error if validation fails

**Side Effects:**
- Saves results to `pi5/data/results.json`
- Sends results to ESP32
- Broadcasts to all connected SSE clients
- Triggers results display animation

---

#### `POST /api/results/clear` or `DELETE /api/results/clear`
**Description:** Clear race results.

**Response:**
```json
{
  "success": true,
  "message": "Results cleared"
}
```

**Side Effects:**
- Deletes `pi5/data/results.json`
- Turns off all LEDs

---

#### `GET /api/results/stream`
**Description:** Server-Sent Events (SSE) endpoint for real-time results notifications.

**Response:** Event stream (text/event-stream)

**Events:**
- `connected` - Initial connection confirmation
- `results` - New results announced

**Example Result Event:**
```
event: results
data: {"win": 5, "place": 12, "show": 8}
```

**Keep-Alive:** Sends comment every 30 seconds to maintain connection.

---

### Weather Endpoint

#### `GET /api/weather`
**Description:** Get weather forecast (12-hour forecast from current hour).

**Response:**
```json
{
  "success": true,
  "hourly": [
    {
      "time": "2024-12-15 20:00",
      "temp_f": 72.5,
      "condition": {
        "text": "Partly cloudy",
        "icon": "//cdn.weatherapi.com/weather/64x64/day/116.png"
      }
    }
  ],
  "current": {
    "temp_f": 72.5,
    "condition": {
      "text": "Partly cloudy",
      "icon": "//cdn.weatherapi.com/weather/64x64/day/116.png"
    }
  },
  "location": "Dallas, TX",
  "cached": false
}
```

**Configuration:**
- API key must be set in `config.py` (`WEATHER_API_KEY`)
- Location set in `config.py` (`WEATHER_LOCATION`)
- Results cached for `WEATHER_CACHE_MINUTES` (default: 15 minutes)

**Error Response:**
```json
{
  "success": false,
  "error": "Weather API key not configured"
}
```

---

### Reset Endpoint

#### `POST /api/reset`
**Description:** Reset system to idle state.

**Response:**
```json
{
  "success": true,
  "response": "OK"
}
```

**Side Effects:**
- Sends `RESET` command to ESP32
- Does NOT clear results (use `/api/results/clear` for that)

---

## Error Responses

All endpoints may return error responses in the format:

```json
{
  "success": false,
  "error": "Error message"
}
```

**Common HTTP Status Codes:**
- `200` - Success
- `400` - Bad request (validation error)
- `500` - Server error
- `503` - Service unavailable (e.g., weather API down)

---

## Command Protocol (ESP32)

Commands sent to ESP32 follow this format:
```
COMMAND:ACTION:VALUE
```

**Examples:**
- `LED:ALL_ON`
- `LED:COLOR:FFD700`
- `LED:CUP:5:FF0000`
- `LED:BRIGHTNESS:75`
- `ANIM:CHAOS`
- `RESULTS:W:5:P:12:S:8`

The Flask API translates REST calls into these command strings.

---

## Real-Time Communication

### Server-Sent Events (SSE)

The dashboard uses SSE for real-time updates:

**Endpoint:** `/api/results/stream`

**Usage:**
```javascript
const eventSource = new EventSource('/api/results/stream');

eventSource.addEventListener('results', function(e) {
    const data = JSON.parse(e.data);
    console.log('Results received:', data);
});
```

**Benefits:**
- Instant notifications when results are set
- All connected devices receive updates simultaneously
- Automatic reconnection on disconnect

---

## Configuration

Key configuration variables in `pi5/config.py`:

```python
# Flask Server
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5000
FLASK_DEBUG = True

# ESP32 Connection
ESP32_IP = '192.168.1.100'
ESP32_PORT = 5005

# System Info
SYSTEM_NAME = "Derby de Mayo Cup Controller"
VERSION = "3.0"
NUM_CUPS = 20
TOTAL_LEDS = 640

# Weather API
WEATHER_API_KEY = 'your_api_key_here'
WEATHER_LOCATION = 'Dallas,TX'
WEATHER_CACHE_MINUTES = 15
```

---

## Testing with curl

### Ping ESP32
```bash
curl http://localhost:5000/api/ping
```

### Turn on all LEDs
```bash
curl -X POST http://localhost:5000/api/led/all_on
```

### Set brightness
```bash
curl -X POST http://localhost:5000/api/led/brightness \
  -H "Content-Type: application/json" \
  -d '{"brightness": 50}'
```

### Set cup color
```bash
curl -X POST http://localhost:5000/api/led/cup \
  -H "Content-Type: application/json" \
  -d '{"cup": 5, "color": "FFD700"}'
```

### Start animation
```bash
curl -X POST http://localhost:5000/api/animation/CHAOS
```

### Set results
```bash
curl -X POST http://localhost:5000/api/results \
  -H "Content-Type: application/json" \
  -d '{"win": 5, "place": 12, "show": 8}'
```

---

*Last Updated: December 2024*  
*Derby de Mayo Cup Project V3*

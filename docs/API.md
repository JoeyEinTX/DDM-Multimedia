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
**Description:** Get current race results (kept across a restart of pi5,
until Reset betting or `/api/results/clear`).

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
  "leds": "ok",
  "response": "OK:RESULTS:FINALIZE",
  "race": {"rev": 42, "state": 5, "state_name": "WINNER", "mode": "RESULTS",
           "source": "dashboard", "gateway_online": true}
}
```

The results are facts about the race, not about the LEDs: they are saved
first, always, and `success` is true once they are. `leds` is what the LED
controller did with them afterwards: `"ok"`, or `"unreachable"` when it did
not answer (`response` then carries the client's `ERROR:...`, and the
dashboard's notification ends `· LEDs unreachable`, in red). The results
stand either way.

`race` is the race state La Quiniela was given (below); `null` when La
Quiniela is not running.

**Response (the file could not be written), 500:**
```json
{
  "success": false,
  "error": "results not saved: pi5 could not write results.json (its console says why)",
  "results": {"win": 5, "place": 12, "show": 8},
  "race": null
}
```
Nothing else happens then: no state line, no LED command, no broadcast.

`win`, `place` and `show` are **horse numbers** (program numbers, 1-24): a
horse standing in for a scratched one is sent under its own number (22, not
the 9 it replaced). The dashboard's pickers send them; La Quiniela reads them
from the saved file for the cups and the TV board.

**Validation:**
- Win, Place, and Show must be different numbers
- Returns 400 error if validation fails

**Side Effects, in this order:**
- Saves results to `pi5/data/results.json` (a temporary file flushed to the
  card and renamed over it). The file is kept when pi5 starts: the results
  live until Reset betting (`POST /api/quiniela/reset`) or the dashboard's
  RESET (`/api/results/clear`), so a restart in WINNER comes back with them
- Sets La Quiniela's race state to WINNER, mode `RESULTS`, in one state line
  with the results: the three cups show WIN / PLACE / SHOW and the TV board
  flips from `OFFICIAL RESULTS COMING` to its results screen (the model's
  `results`, with the figures at the post, the model's `closing`: see "The
  results board" in `pi5/LQ_BRIDGE.md`)
- Broadcasts to all connected SSE clients
- Tells the LED controller: `RESULTS:FINALIZE`, the winners' chase settling
  into the heartbeat (the three cups were locked as they were picked). The
  dashboard used to send it itself through `/api/results/finalize`, which is
  still there; `leds` in the reply says whether the controller answered

---

#### `POST /api/results/clear` or `DELETE /api/results/clear`
**Description:** Clear race results.

**Response:**
```json
{
  "success": true,
  "message": "Results cleared",
  "race": {"rev": 43, "state": 6, "state_name": "AFTER_PARTY", "mode": "RESET",
           "source": "dashboard", "gateway_online": true}
}
```

**Side Effects:**
- Deletes `pi5/data/results.json`
- Sets La Quiniela's race state to AFTER_PARTY, mode `RESET`, with the
  results cleared (the model's `results` is `null` again): the TV board
  hands the screen back to the slideshow
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
- The same cache feeds La Quiniela's model (`weather`: `{"location",
  "temp_f", "condition"}`, the TV crawl's `DALLAS 88°F SUNNY`), so the API is
  asked at most once per `WEATHER_CACHE_MINUTES` for both
- A failed fetch with an expired cache answers the cached `hourly`,
  `current` and `location` with `"cached": true, "stale": true`

**Error Response:**
```json
{
  "success": false,
  "error": "Weather API key not configured"
}
```

---

### Race Endpoint

#### `GET /api/race`
**Description:** The race roster, read-only, for any display that wants one.
Built from La Quiniela's store, the one home of race information (the Race
Setup page, its `data/race_setup.json` and `/api/race-setup` are gone; the race
and its post time are set on La Quiniela's admin page, `/quiniela/admin`).
Always HTTP 200, with `Access-Control-Allow-Origin: *`.

**Response:**
```json
{
  "race_state": "pre-race",
  "post_time": "5:57 PM CDT",
  "post_time_iso": "2027-05-01T17:57:00-05:00",
  "last_updated": "2027-05-01T20:14:07Z",
  "horses": [
    {"number": 1, "name": "Sovereignty", "odds": "5-2", "finish": null},
    {"number": 21, "name": "Great White", "odds": null, "finish": null}
  ],
  "winner": null
}
```

| Field | Where it comes from |
| --- | --- |
| `race_state` | La Quiniela's race state (the dashboard's modes set it): 0-3 `pre-race`, 4 `running`, 5-6 `post-race`; `unknown` while no horse in the field has a name |
| `post_time` | La Quiniela's race info: the post time on the race's clock (`LQ_RACE_TZ`, Central by default); `""` while none is set |
| `post_time_iso` | the same instant, ISO 8601 with that clock's offset; `""` while none is set |
| `last_updated` | the time of the request, UTC |
| `horses` | La Quiniela's field in numeric order, each under its own program number (an also-eligible that drew in keeps its number; a scratched horse is not listed): `name` as typed on the admin page (a horse with no name is left out), `odds` the track's odds for that program number from La Quiniela's odds poller or null, `finish` 1 / 2 / 3 from the results or null |
| `winner` | the WIN horse's program number from the results, or null |

---

### Counted Pot Endpoint

The scales are estimates. After betting closes the host counts the cash box's BETS
compartment and enters the dollars; from then on the pot and all three prizes, on the TV
and on the admin page, come from that number. The rest of La Quiniela's routes
(`/api/quiniela...`) are in `pi5/LQ_BRIDGE.md`; this one is here because race night uses it.

#### `PUT /api/quiniela/counted_pot`
**Description:** Set or clear the hand count.

**Request body:**
```json
{"amount": 152}
```
`amount` is whole dollars, 0 to 10000; entering again overwrites. `{"amount": null}`
clears the count and puts the scales' figures back.

**Response:**
```json
{"ok": true, "pot_counted": 152, "pot_scale": 154.0, "pot": 152.0,
 "prizes": {"win": 91, "place": 38, "show": 23}, "hand_counted": true,
 "race_state": 3, "saved": true}
```
`saved` is false when the database refused the write: the count is held, but a restart of
pi5 would lose it.

**Errors:** `{"ok": false, "error": "..."}` with a plain message.
- 400: the amount is not a whole number of dollars from 0 to 10000 (a float, a string, a
  bool, a negative number), or the body has no `amount`
- 409: the race is not in 3 AT_THE_POST, 4 RUNNING or 5 WINNER, or there are no figures at
  the post yet. In 6 AFTER_PARTY a saved count stays and is read-only

**Side Effects:**
- The count is stored inside the figures at the post, so Reset betting and a state of 0 or 1
  clear it with them, and a restart of pi5 keeps it
- The model is pushed to the event stream at once (the TV does not wait for a poll)

**The model** (`GET /api/quiniela` and its event stream) gains three keys, and its pot and
prizes follow the count:

| Key | Meaning |
|---|---|
| `pot_scale` | the scale pot frozen at the post, or null while there are no figures at the post |
| `pot_counted` | the hand count in whole dollars, or null |
| `hand_counted` | true while the model's `pot` and `prizes` are the count's (a count is held and the race is in 3-6); the TV's pot wears a `HAND COUNTED` tag |
| `pot`, `prizes` | the count's when `hand_counted`, with the same split and whole-dollar rounding as the scale pot's; else the live scale figures |
| `closing.pot`, `closing.prizes` | the figures at the post, the count's when there is one (what the TV paints from the post on); `closing` keeps its five keys |

Bets per horse (`horses[n].tokens`, `total_tokens`, `closing.horses`) are always as the scales
read them.

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

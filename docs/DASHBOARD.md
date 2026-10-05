# Dashboard UI Documentation - DDM Cup Project

This document describes the user interface layout, components, and styling of the DDM Cup Dashboard.

## Overview

The dashboard is a single-page web application built with Flask, HTML, CSS, and JavaScript. It provides touch-friendly controls for managing LED animations, race results, and system settings.

**Access:** `http://<Pi5-IP>:5000`

---

## Layout Structure

### Header Section
```
┌─────────────────────────────────────────────────────────────┐
│ [LOGO]  [WEATHER WIDGET]         [TOTE BOARD CLOCK] [🔍][⛶]│
└─────────────────────────────────────────────────────────────┘
```

**Components:**
- **DDM Logo** - Project branding (left)
- **Weather Widget** - Current weather with icon and temperature (clickable for forecast)
- **Tote Board Clock** - 5×7 dot matrix display showing time in AM/PM format
- **Device Status Button (🔍)** - Shows ESP32 connection status modal
- **Fullscreen Toggle (⛶)** - Enter/exit fullscreen mode

---

### Results Banner
```
┌─────────────────────────────────────────────────────────────┐
│   WIN  [01] HORSE 1                                         │
│  PLACE [05] HORSE 5                                         │
│   SHOW [12] HORSE 12                                        │
└─────────────────────────────────────────────────────────────┘
```

**Layout:**
- **Label** - 5 dot matrix tiles (WIN/PLACE/SHOW)
- **Saddle Cloth** - Color-coded number badge
- **Horse Name** - 15 dot matrix tiles

**Display Logic:**
- Hidden by default
- Appears when results are set
- Persists until results cleared or race reset

---

### Control Panels

The dashboard has 4 main control panels arranged horizontally:

#### 1. PRE-RACE Panel
**Purpose:** Setup and testing before race

**Buttons:**
- `IDLE` - Ambient DDM breathing animation
- `WELCOME` - Welcome show animation
- `TEST` - Opens RGB test modal with color wheel

#### 2. BETTING OPEN Panel
**Purpose:** Betting countdown animations

**Buttons:**
- `60-MIN WARNING` - 60-minute countdown animation
- `30-MIN WARNING` - 30-minute countdown animation
- `FINAL CALL` - Final call strobe effect

#### 3. DURING RACE Panel
**Purpose:** Race in progress

**Buttons:**
- `RACE START` - Green flash, race begins
- `CHAOS MODE` - Maximum intensity (final stretch)
- `FINISH` - Checkered flag sequence

#### 4. RESULTS Panel
**Purpose:** Post-race winner management

**Buttons:**
- `SET WINNERS` - Opens modal to select Win/Place/Show
- `REVEAL!` - (Shows when results exist)
- `RESET RACE` - Clear all results and re-enable controls

---

### Footer Section
```
┌─────────────────────────────────────────────────────────────┐
│ ● ONLINE  |  MODE: IDLE  |  1 Device  |  Powered by JoeyAI │
└─────────────────────────────────────────────────────────────┘
```

**Components:**
- **Connection Status** - Green dot (●) when ESP32 online, red when offline
- **Current Mode** - Shows active animation or state
- **Device Count** - Number of connected ESP32 devices (0 or 1)
- **Branding** - "Powered by JoeyAI"

---

### Emergency Stop Button
```
┌────────┐
│   ⛔   │  (Collapsed state - click to expand)
└────────┘

┌────────────────────────┐
│ EMERGENCY STOP         │
│ [STOP ALL LEDS]        │  (Expanded state)
│ [STANDBY MODE]         │
└────────────────────────┘
```

**Location:** Bottom-right corner (floating)

**Functions:**
- **STOP ALL LEDS** - Immediate LED shutdown (confirmation required)
- **STANDBY MODE** - Turn off LEDs without clearing results

---

## Tote Board Styling

### CSS Variables

```css
--tote-board-bg: #0d4d40;        /* Dark teal background */
--tote-board-border: #1B998B;     /* DDM teal border */
--dot-lit: #D4A000;               /* Amber lit bulbs */
--dot-unlit: rgba(0,0,0,0.5);    /* Dark unlit sockets */
```

### Dot Matrix Characters

**Format:** 5×7 grid (5 columns × 7 rows = 35 dots per character)

**Supported Characters** (`dotPatterns` in `static/js/ddm_control.js`):
- **Numbers:** 0-9 (zero uses slashed style for authenticity)
- **Letters:** A-Z
- **Special:** Space, `:` `|`, and what a horse's name can carry: `' . , - & ! /`
- **For La Quiniela's TV board:** `$ + # % ( ) ? " * = ; < > @ _`, the middle
  dot, the crawl's diamond and arrow. The dashboard prints none of these; the
  TV board's tote look does, and its dot-matrix face
  (`splash_display/static/fonts/DDMTote.ttf`) is built from this table by
  `splash_display/tools/make_tote_font.py`, so the TV's dots are the
  dashboard's. After adding or changing a pattern, run that script and commit
  the face; the splash's tests fail until the two agree.

**Classes:**
- `.dot-digit` - Container for a single character (35 dots)
- `.dot` - Individual dot (unlit by default)
- `.dot.lit` - Lit dot (amber color)

### Tote Board Housing

**Class:** `.tote-board-housing`

**Style:**
```css
.tote-board-housing {
    background: var(--tote-board-bg);
    border: 3px solid var(--tote-board-border);
    border-radius: 8px;
    padding: 12px;
    box-shadow: inset 0 2px 8px rgba(0,0,0,0.3);
}
```

**Usage:** Applied to clock, results banner, and any tote board-style element

---

## Saddle Cloth Colors (1-20)

Official racing colors for horse positions 1-20:

| Position | Background | Text | Color Name |
|----------|-----------|------|------------|
| 1 | #E31837 | #FFFFFF | Red, white |
| 2 | #FFFFFF | #000000 | White, black |
| 3 | #0033A0 | #FFFFFF | Blue, white |
| 4 | #FFCD00 | #000000 | Yellow, black |
| 5 | #00843D | #FFFFFF | Green, white |
| 6 | #000000 | #FFD700 | Black, gold |
| 7 | #FF6600 | #000000 | Orange, black |
| 8 | #FF69B4 | #000000 | Pink, black |
| 9 | #40E0D0 | #000000 | Turquoise, black |
| 10 | #663399 | #FFFFFF | Purple, white |
| 11 | #808080 | #E31837 | Grey, red |
| 12 | #32CD32 | #000000 | Lime, black |
| 13 | #8B4513 | #FFFFFF | Brown, white |
| 14 | #800000 | #FFCD00 | Maroon, yellow |
| 15 | #C4B7A6 | #000000 | Khaki, black |
| 16 | #87CEEB | #E31837 | Light blue, red |
| 17 | #000080 | #FFFFFF | Navy, white |
| 18 | #228B22 | #FFCD00 | Forest green, yellow |
| 19 | #00008B | #E31837 | Dark royal blue, red |
| 20 | #FF00FF | #FFCD00 | Fuchsia, yellow |

**Implementation:**
```javascript
const SADDLE_CLOTHS = {
    1:  { bg: '#E31837', text: '#FFFFFF' },
    2:  { bg: '#FFFFFF', text: '#000000' },
    // ... (see ddm_control.js for complete list)
};
```

**Usage:**
- Results banner number badges
- SET WINNERS modal grid buttons
- Any horse position display

---

## Modals

### 1. Results Modal (SET WINNERS)

**Trigger:** Click "SET WINNERS" button

**Layout:**
```
┌───────────────────────────────────────────────────────────────────┐
│  CHOOSE WINNER                                             [X]    │
│  Tap the 1st place horse                                          │
│                                                                   │
│  [ 1 · DORNOCH ] [ 2 · SIERRA LEONE ] [ 3 · ... ] [ 4 · ... ]   ┌──────────┐
│  [ 5 · ...     ] [ 6 · ...          ] [ 7 · ... ] [ 8 · ... ]   │ WIN  [--]│
│  [22 · OCELLI  ] [10 · ...          ] [11 · ... ] [12 · ... ]   │ PLACE[--]│
│  [13 · ...     ] [14 · ...          ] [15 · ... ] [16 · ... ]   │ SHOW [--]│
│  [17 · ...     ] [18 · ...          ] [19 · ... ] [         ]   └──────────┘
│                                                                   │
│                  [RESET]  [CANCEL]            [CONFIRM RESULTS]   │
└───────────────────────────────────────────────────────────────────┘
```

**Features:**
- **Slot-by-slot selection:** a tap fills the **active slot**: the one the host tapped
  in the sidebar, or, with none chosen, the next empty one in the order WIN → PLACE →
  SHOW. A filled slot is never overwritten by a stray tap: to change a pick, tap its slot
  (it lights up and says `TAP THE NEW HORSE`), then the new horse; tap the slot again to
  leave it as it was
- **One horse, one slot:** a horse already in another slot is refused, not moved, with a
  line under the heading (`7 · HORSE 7 is already WIN. To use it elsewhere, clear WIN
  first.`); picked horses stay in the grid, outlined in their slot's colour and tagged
  WIN / PLACE / SHOW
- **Clear:** the × on a slot empties that slot (its cup unlocks); RESET empties all three
- **Pickers with names:** one per post in mantle order, 4 across and 5 down,
  each showing the horse that runs from that post as `19 · GOLDEN TEMPO`: the
  number on its saddle cloth, then the name from La Quiniela's names store
  (`HORSE n` where it has none). The list comes from `GET /api/quiniela/field`
  every time the modal opens
- **Scratches:** a horse scratched with a replacement shows the replacement's
  number and name in its place (post 9 offers `22 · OCELLI`); a horse scratched
  with no replacement is not offered (its place in the grid stays empty)
- **Post and horse:** a pick lights the LED cup of the **post** (9) and records
  the **horse** (22); the results carry horse numbers
- **NO BETS:** a horse whose cup held no bets at the post is dimmed and tagged `NO BETS`
  (red, top left; a slot's tag is top right). It cannot pay (an empty cup has no token to
  draw), so the host enters the next finisher in its place. The marks come from La
  Quiniela's figures at the post (`closing` in `GET /api/quiniela`), by **horse** number
  (22 running for 9 is horse 22); a horse no cup claimed counts as no bets. Picking one is
  allowed: its slot card shows the tag, and a warning above CONFIRM RESULTS says
  `#7 SO HAPPY: nobody bet this horse, so it can't pay. Enter the next finisher instead.`
  CONFIRM RESULTS stays enabled. With no figures at the post (the race never reached the
  post, betting reopened, pi5 not answering) nothing is marked and nothing says so
- **Sidebar slots:** WIN, PLACE and SHOW, each with its horse's saddle cloth number and
  name in large type (readable from arm's length on an iPad); the active slot is lit. They
  are the check before CONFIRM RESULTS: the picks are not listed a second time. A name
  wraps only between words; a word too wide for the card takes a smaller size until it fits
- **Live LED preview:** Selected cups light up immediately
  - Win: Gold (#FFD700)
  - Place: Silver (#C0C0C0)
  - Show: Bronze (#CD7F32)
- **Heartbeat animation:** Non-selected cups pulse while selecting
- **Navigation:**
  - RESET - Clear all and start over
  - CANCEL - Close without setting anything
  - CONFIRM - Apply results and close modal; it sits one card gap under the SHOW card, with
    a warning above it for any pick nobody bet (the slot cards are the last look)

**Behavior:**
- Which slot a tap fills is decided from the picker's own state, in the moment of the tap;
  the LED commands (lock, unlock) follow it, one after another, and never decide it. A slow
  or unreachable LED controller cannot make a tap land in the wrong slot (it used to: a
  second tap inside the first pick's lock call was read as WIN again and overwrote it)
- Nothing in the background touches the picks while the modal is open: the mode poll and
  the field refresh leave them alone, and a `results` event from another device is held
  (a "Results were set on another device" notice) until the modal is closed
- The figures at the post are read once, with the field, as the modal opens, and the NO
  BETS marks stay as they were while it is open (they are frozen figures; nothing in the
  background re-reads them); opening the modal again reads them again
- A modal taller than the screen (NO BETS warnings on a short screen, Safari's bars)
  scrolls instead of running off the top and bottom, and when the confirm section appears
  CONFIRM RESULTS is scrolled into view
- Header changes color by the active slot (Gold/Silver/Bronze): CHOOSE WINNER / PLACE /
  SHOW, CHANGE ... when replacing one, CHECK THE RESULTS when all three are set
- The confirm section (any NO BETS warnings and CONFIRM RESULTS) appears when all 3 are set
  and no slot is being changed
- Closing without confirming unlocks cups and turns off LEDs; a tap on the dark backdrop
  does not close the modal once a pick is made (CANCEL does)
- Confirming waits at most 3 s for LED commands still in flight, then sets the results,
  saved and sent exactly as before
- Tests: `la_quiniela/test_dashboard.py`, which drives the real picker in headless Chrome
  through `pi5/tools/picker_check.py` (mouse and touch, a slow LED controller, a forced
  refresh, NO BETS marks from figures at the post set up on the server, the right column
  at 1554 x 1116, 1180 x 820, 820 x 1180 and a 740 px tall view, in a wide font too). `python
  tools/picker_check.py --reproduce` from `pi5/` prints the timing table, `--shots DIR`
  takes screenshots (the NO BETS marks among them)

---

### 2. Test Modal (RGB COLOR TEST)

**Trigger:** Click "TEST" button

**Layout:**
```
┌─────────────────────────────────────────────────────┐
│  RGB COLOR TEST                             [X]     │
│                                                      │
│              ┌──────────────┐                       │
│              │              │                       │
│              │ Color Wheel  │                       │
│              │              │                       │
│              └──────────────┘                       │
│                                                      │
│  BRIGHTNESS: ████████░░ 75%                         │
│                                                      │
│  PRESETS:                                           │
│  [ROSE] [GOLD] [TEAL] [WHITE]                      │
│  [RED]  [GREEN] [BLUE] [OFF]                       │
│                                                      │
│                              [CLOSE]                │
└─────────────────────────────────────────────────────┘
```

**Features:**
- **iro.js color wheel** - Interactive HSV color picker
- **Live preview** - All changes sent immediately to LEDs
- **Brightness slider** - 0-100% range
- **8 Preset buttons:**
  - ROSE (#E195AB) - DDM rose pink
  - GOLD (#C9A227) - DDM gold
  - TEAL (#1B998B) - DDM teal
  - WHITE (#FFFFFF)
  - RED (#FF0000)
  - GREEN (#00FF00)
  - BLUE (#0000FF)
  - OFF - Turns off all LEDs

**Command Sent:**
```
LED:TEST:R,G,B,BRIGHTNESS
```

**Behavior:**
- Clicking preset updates color wheel position
- Dragging wheel updates LEDs in real-time
- Close button sends `LED:ALL_OFF` command

---

### 3. Weather Modal (12-HOUR FORECAST)

**Trigger:** Click weather widget in header

**Layout:**
```
┌─────────────────────────────────────────────────────┐
│  Weather Forecast - Dallas, TX              [X]     │
│                                                      │
│  ┌────┬────┬────┬────┬────┬────┬────┬────┬────┐   │
│  │NOW │1PM │2PM │3PM │4PM │5PM │6PM │7PM │8PM │...│
│  │ ☀ │ ☀ │ ⛅ │ ⛅ │ ☁ │ 🌧 │ 🌧 │ ⛈ │ ☁ │   │
│  │72°│73°│74°│75°│76°│74°│72°│70°│68°│   │
│  └────┴────┴────┴────┴────┴────┴────┴────┴────┘   │
│                                                      │
│                              [CLOSE]                │
└─────────────────────────────────────────────────────┘
```

**Features:**
- **12-hour forecast** starting from current hour
- **Grid layout** with 12 time slots
- **Weather icons** from WeatherAPI.com
- **Temperature colors** based on value:
  - < 40°F: Blue (cold)
  - 40-59°F: Light blue (cool)
  - 60-74°F: White (comfortable)
  - 75-89°F: Orange (warm)
  - ≥ 90°F: Red (hot)
- **Current hour** highlighted
- **Auto-refresh** every 30 minutes
- **Caching** for 15 minutes

---

### 4. Device Status Modal

**Trigger:** Click device status button (🔍) in header

**Layout:**
```
┌─────────────────────────────────────────────────────┐
│  Device Status                              [X]     │
│                                                      │
│  🔌 ESP32 LED Controller                   ONLINE   │
│  💡 IP Address                     192.168.1.100    │
│                                                      │
│                              [CLOSE]                │
└─────────────────────────────────────────────────────┘
```

**When Offline:**
```
┌─────────────────────────────────────────────────────┐
│  Device Status                              [X]     │
│                                                      │
│  No devices connected                               │
│                                                      │
│                              [CLOSE]                │
└─────────────────────────────────────────────────────┘
```

**Features:**
- Shows ESP32 connection status
- Displays IP address when online
- Updates automatically every 5 seconds

---

### 5. Results Reveal Modal (Spectator View)

**Trigger:** Automatic when results broadcast via SSE (other devices only)

**Layout:**
```
┌─────────────────────────────────────────────────────┐
│                                                      │
│              🏇 RESULTS ARE IN! 🏇                  │
│                                                      │
│           The winners have been chosen!             │
│                                                      │
│                  [REVEAL WINNERS]                   │
│                                                      │
└─────────────────────────────────────────────────────┘
```

**Features:**
- **Dramatic reveal** for spectator devices
- **NOT shown** on device that submitted results
- **Reveal button** closes modal and shows results banner
- **SSE-triggered** when results are set from another device

---

## Real-time Sync

### Server-Sent Events (SSE)

**Endpoint:** `/api/results/stream`

**Purpose:** Push results to all connected devices simultaneously

**Events:**
- `connected` - Connection established
- `results` - New results available

**Implementation:**
```javascript
const eventSource = new EventSource('/api/results/stream');

eventSource.addEventListener('results', function(e) {
    const data = JSON.parse(e.data);
    if (!justSubmittedResults) {
        showResultsRevealModal();
    }
});
```

**Behavior:**
- Submitting device skips reveal popup (flag: `justSubmittedResults`)
- Other devices show dramatic reveal modal
- Results banner updates on all devices when revealed

---

## Button States

### Active State
**Class:** `.active`
**Style:** Green background, indicates animation is running
**Usage:** Toggle animations (CHAOS, HEARTBEAT, etc.)

### Flash State
**Class:** `.flash`
**Style:** Brief white flash
**Usage:** One-shot commands (IDLE, RESET, etc.)

### Disabled State
**Class:** `.disabled`
**Attribute:** `disabled`
**Style:** Greyed out, no interaction
**Usage:** Race-phase buttons disabled when results set

---

## Loading Indicator

**Element:** `#loader`
**Class:** `.loader.show`

**Behavior:**
- Shows for minimum 2 seconds
- Displays during API calls
- Hides immediately on error
- Prevents multiple rapid clicks

**Style:** Spinning teal circle overlay

---

## Notifications

**Element:** `#notification`
**Classes:** `.notification.show.success` or `.notification.show.error`

**Types:**
- **Success** - Green background, checkmark icon
- **Error** - Red background, X icon

**Duration:** 3 seconds auto-dismiss

**Examples:**
- "Command sent: CHAOS"
- "ESP32 disconnected"
- "Results set: Win=5, Place=12, Show=8"

---

## Splash Screen

**Element:** `#splash-screen`
**Class:** `.fade-out` (triggered after 2 seconds)

**Content:**
```
┌─────────────────────────────────────────┐
│                                         │
│         [DDM LOGO]                      │
│                                         │
│    DERBY DE MAYO CUP CONTROLLER        │
│           VERSION 3.0                   │
│                                         │
└─────────────────────────────────────────┘
```

**Behavior:**
- Shows on page load
- Fades out after 2 seconds
- Removed from DOM after fade completes

---

## Responsive Design

### Breakpoints

- **Desktop:** > 1200px (full layout)
- **Tablet:** 768px - 1200px (adjusted spacing)
- **Mobile:** < 768px (stacked panels)

### Touch-Friendly

- **Minimum button size:** 60px × 60px
- **Touch targets:** Spaced 8px apart
- **Gesture support:** Tap, no hover required
- **Fullscreen mode:** Touch-optimized for Pi5 touchscreen

---

## Color Palette

### DDM Brand Colors

```css
--ddm-teal: #1B998B;         /* Primary brand color */
--ddm-rose: #E195AB;         /* Secondary brand color */
--ddm-gold: #C9A227;         /* Accent color */
--ddm-forest: #228B22;       /* Derby green */
--ddm-dark-teal: #0d4d40;    /* Tote board background */
```

### Winner Colors

```css
--winner-gold: #FFD700;      /* 1st place */
--winner-silver: #C0C0C0;    /* 2nd place */
--winner-bronze: #CD7F32;    /* 3rd place */
```

### Semantic Colors

```css
--success: #00A36C;          /* Success/online */
--error: #D32F2F;            /* Error/offline */
--warning: #FFA000;          /* Warning */
--info: #1976D2;             /* Info */
```

### Background Colors

```css
--bg-primary: #1a1a1a;       /* Main background */
--bg-secondary: #2d2d2d;     /* Panel background */
--bg-tertiary: #3a3a3a;      /* Modal background */
```

---

## Accessibility

### Keyboard Navigation
- Tab through buttons
- Enter to activate
- Escape to close modals

### Screen Reader Support
- ARIA labels on interactive elements
- Role attributes on modals
- Status announcements for state changes

### High Contrast
- 4.5:1 minimum contrast ratio
- Color not sole indicator (icons + text)
- Focus indicators visible

---

## Browser Compatibility

**Tested On:**
- Chrome 90+ ✓
- Firefox 88+ ✓
- Safari 14+ ✓
- Edge 90+ ✓

**Required Features:**
- CSS Grid
- CSS Custom Properties
- Fetch API
- EventSource (SSE)
- Fullscreen API

---

## File Structure

```
pi5/
├── templates/
│   └── dashboard.html          # Main UI template
├── static/
│   ├── css/
│   │   ├── ddm_style.css       # DDM-themed styles
│   │   └── ddm_logo.png        # Logo image
│   └── js/
│       └── ddm_control.js      # UI logic and API calls
└── main.py                     # Flask backend
```

---

## External Dependencies

### JavaScript Libraries

**iro.js** - Color picker
```html
<script src="https://cdn.jsdelivr.net/npm/@jaames/iro@5"></script>
```
- Version: 5.x
- Size: ~9KB
- License: MPL-2.0

---

*Last Updated: December 2024*  
*Derby de Mayo Cup Project V3*

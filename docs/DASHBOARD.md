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
│ [LED wall · online] [Tote board · off]                 [☰] │
│                        [DDM LOGO]                           │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ serape band ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │
│ [ AUTO / MANUAL ]                      [ RACE INFO ]        │
└─────────────────────────────────────────────────────────────┘
```

**Components:**
- **Status bar** - two pills on the left, `LED wall` and `Tote board`, each `online`,
  `unreachable since hh:mm` or `off` (no address set), from `/api/status` every 5 s (tap for
  the device modal); the menu (☰) on the right. One toast when a device goes away (the race
  goes on; the device catches up when it returns), one when it is back; none per tap
- **DDM Logo** - Project branding
- **Serape band**, then the **AUTO / MANUAL** and **RACE INFO** cards
- **No ticker** (Joey, 2026-10-13, gap review OD9): the dot-matrix ticker under the logo
  (time, weather, race state, date) is gone, with its timers and its weather fetch; the
  TV's crawl is the only crawl, and pi5 feeds it the weather itself. The race state is on
  the LQ admin page and in every mode button's reply; the reopen confirm reads it fresh
- **Fullscreen Toggle (⛶)** - bottom left corner

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

#### The race state and the guards (Joey, 2026-10-13)

The panels' buttons are the race's state (`MODE_STATES` in `pi5/la_quiniela/betting.py`):
WELCOME and STANDBY 0, 60 MIN and 30 MIN 1, FINAL CALL 2, AT THE GATE 3, THEY'RE OFF!, CHAOS
and FINISH 4, SET WINNERS (once confirmed) and HEARTBEAT 5, RESET 6.

- **TEST is an LED test only.** It opens the colour wheel and names no mode to pi5, so the
  race state, the figures at the post and the hand count stay as they are (it used to set
  PRE-RACE and drop them; pi5 now refuses `TEST` as a mode, so a page from before cannot
  either).
- **Reopening betting asks first.** At AT THE POST, RUNNING or WINNER (3-5), WELCOME,
  STANDBY, 60 MIN, 30 MIN and FINAL CALL would reopen betting. The tap reads the state from
  pi5 (`GET /api/quiniela/mode`, whose `at_post` carries the scale pot and the hand count at
  the post) and shows the in-page confirm before anything is sent: `This reopens betting.
  The figures at the post (pot $86) and the hand count ($80) will be cleared.` with
  **Reopen betting** and **Cancel**. Cancel sends nothing; Reopen betting sends the mode and
  the LEDs once. pi5 clears the figures and the count when the state arrives (FINAL CALL
  after the post included), and the next AT THE GATE takes new ones. In 0-2, and in 6, the
  buttons send at once. AT THE GATE, THEY'RE OFF!, CHAOS, FINISH and HEARTBEAT never ask.
  With the results set the race-phase buttons stay disabled.
- **AUTO / MANUAL** (the switch above the panels). Race night is MANUAL. AUTO is the mock
  racing service's demo (`/api/racing/*`): a timer walks its own race states and drives the
  LEDs by itself; La Quiniela does not follow it. While AUTO is on every mode button is
  disabled and says `switch to MANUAL` on itself (SET WINNERS, STANDBY and RESET used to do
  nothing silently). Switched on at AT THE POST, RUNNING or WINNER it asks first; MANUAL
  never asks.

---

### Footer Section

The drawer's footer: "Powered by JoeyAI" and the version. The devices' health is in the
status bar's pills, not here.

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
- **Clear:** the × on a slot empties that slot (its cup unlocks); RESET empties every slot.
  With a result already saved on pi5 (CONFIRM RESULTS pressed before), RESET first asks
  `Clear the saved result?` in the page's own ask modal, naming the three, and on
  **Clear the result** clears it through the panel's own route (`/api/results/clear`:
  AFTER PARTY, the TV back to the slideshow, the LEDs off), the picker staying open; the
  field is frozen while a result stands, so this is how the host scratches after the
  results and enters them again. Cancel clears nothing
- **Pickers with names:** one per post in mantle order, 4 across and 5 down,
  each showing the horse that runs from that post as `19 · GOLDEN TEMPO`: the
  number on its saddle cloth, then the name from La Quiniela's names store
  (`HORSE n` where it has none). The list comes from `GET /api/quiniela/field`
  when the modal opens and every 2 s while it is open
- **Scratches:** a horse scratched with a replacement shows the replacement's
  number and name in its place (post 9 offers `22 · OCELLI`); a horse scratched
  with no replacement is not offered (its place in the grid stays empty). A scratch
  made on the LQ admin page while the picker is open reaches it within 2 s: the horse
  leaves the grid, its slot empties (its cup unlocks) and the note under the heading
  says `#3 MYSTIK DAN was scratched: PLACE is empty, tap the horse that finished there.`
  (`#9 ENCINO was scratched.` for a horse not picked); a replacement shows under its
  post. A confirm that races the scratch is refused by pi5 (`place: horse 12 is not in
  the field`), shown in the note, and the field read again at once
- **Post and horse:** a pick lights the LED cup of the **post** (9) and records
  the **horse** (22); the results carry horse numbers
- **The real finish:** the host picks the horses that crossed 1st, 2nd and 3rd, never a
  substitute (Joey, 2026-10-20). A horse whose cup held no bets at the post is dimmed and
  tagged `NO BETS` (red, top left; a slot's tag is top right): La Quiniela skips it for the
  next finisher (its payout computation, `payout_for`; La Subasta pays its owner all the same),
  so once WIN, PLACE and SHOW can no longer hold the horses with bets La Quiniela needs, a
  `4TH` card appears under SHOW asking for the next finisher, then a `5TH` if that one had
  no bets either, until three horses with bets are named (`finishersNeeded`, the same rule
  as pi5's `finishers_needed`, less the empty slots: no card on an empty dialog), and
  CONFIRM RESULTS waits for them. The marks come from La Quiniela's figures at the post (`closing` in
  `GET /api/quiniela`), by **horse** number (22 running for 9 is horse 22); a horse no cup
  claimed counts as no bets. Its slot card shows the tag, and a line above CONFIRM RESULTS
  says `#7 SO HAPPY: no bets at the post, so La Quiniela skips it for the next finisher. La
  Subasta pays its owner all the same.` A next finisher's cup is not lit: the LEDs show the
  three that crossed first. With no figures at the post (the race never reached the post,
  betting reopened, pi5 not answering) nothing is marked, nothing is asked and nothing says so
- **A dead heat:** DEAD HEAT on a filled card ties the card under it to it (`DEAD HEAT · with
  WIN`): the horse there, or the next one tapped, crossed together with it; DEAD HEAT again on
  the second card makes it three deep; a dead heat for 3rd puts the other horse in a `4TH`
  card; UNTIE on the first card undoes it. The record carries the tie (`ties`), and La
  Quiniela's payout pools the positions the tied horses occupy (`RACE_NIGHT.md`, section 8)
- **Reopening while a result stands:** the dialog opens on the saved result, `CHECK THE
  RESULTS` with the picks, the next finishers and any dead heat in their slots and the three
  cups lit again, so a correction is one tap on a slot and the new horse; CANCEL leaves the
  result and puts the mantle back on it (`RESULTS_ACTIVE`). pi5 refuses a result before AT
  THE GATE (no figures at the post to pay from); its line shows under the heading
- **Sidebar slots:** WIN, PLACE and SHOW, and a card per next finisher (`4TH`, `5TH`...)
  while one is needed, each with its horse's saddle cloth number and name in large type
  (readable from arm's length on an iPad); the active slot is lit. They are the check
  before CONFIRM RESULTS: the picks are not listed a second time. A name wraps only between
  words; a word too wide for the card takes a smaller size until it fits
- **Live LED preview:** Selected cups light up immediately
  - Win: Gold (#FFD700)
  - Place: Silver (#C0C0C0)
  - Show: Bronze (#CD7F32)
- **Heartbeat animation:** Non-selected cups pulse while selecting
- **Navigation:**
  - RESET - Clear all and start over; with a result saved on pi5, asks and clears that too
  - CANCEL - Close without setting anything
  - CONFIRM - Apply results and close modal; it sits one card gap under the SHOW card, with
    a warning above it for any pick nobody bet (the slot cards are the last look)

**Behavior:**
- Which slot a tap fills is decided from the picker's own state, in the moment of the tap;
  the LED commands (lock, unlock) follow it, one after another, and never decide it. A slow
  or unreachable LED controller cannot make a tap land in the wrong slot (it used to: a
  second tap inside the first pick's lock call was read as WIN again and overwrote it)
- Nothing in the background touches the picks while the modal is open except the field
  itself: the mode poll leaves them alone, a `results` event from another device is held
  (a "Results were set on another device" notice) until the modal is closed, and the field
  poll (every 2 s) takes out only a horse that has left the field (`pickerFollowsField`),
  the other picks, the chosen slot and the marks staying as they were
- pi5 refuses a result that names a horse not in the field whatever the page shows, so a
  stale dialog cannot save a scratched horse; its line goes in the note under the heading
  and the field is read again. While a result stands the LQ admin page refuses a scratch
  or an undo (409, "the field is frozen"), so the result and the field cannot move under
  each other; RESET in the dialog is the way to clear the result first
- The figures at the post are read once, with the field, as the modal opens, and the NO
  BETS marks stay as they were while it is open (they are frozen figures; nothing in the
  background re-reads them); opening the modal again reads them again
- A modal taller than the screen (NO BETS warnings on a short screen, Safari's bars)
  scrolls instead of running off the top and bottom, and when the confirm section appears
  CONFIRM RESULTS is scrolled into view
- Header changes color by the active slot (Gold/Silver/Bronze): CHOOSE WINNER / PLACE /
  SHOW, CHANGE ... when replacing one, CHECK THE RESULTS when all three are set
- The confirm section (any NO BETS lines and CONFIRM RESULTS) appears when every slot is
  set, the next finishers' cards included, and no slot is being changed
- Closing without confirming unlocks cups and turns off LEDs; a tap on the dark backdrop
  does not close the modal once a pick is made (CANCEL does)
- Confirming waits at most 3 s for LED commands still in flight, then sets the results:
  `{win, place, show, next}` to `POST /api/results`, saved and sent exactly as before
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
- LEDs only: opening, using and closing it never sends a race state (see "The race state and
  the guards")

---

### 3. Weather Modal: removed (2026-10-13)

Nothing on the page opened it any more (the header's weather widget was gone), and the
30-minute `/api/weather` fetch that filled it fed only the ticker; both went with the
ticker. `GET /api/weather` stays on pi5, and the TV's crawl gets the weather from pi5's
own feed.

---

### 4. Device Status Modal

**Trigger:** Tap the pills in the status bar

**Layout:**
```
┌─────────────────────────────────────────────────────┐
│  Devices                                    [X]     │
│                                                      │
│  LED wall      10.0.0.42:5005               online  │
│  Tote board                                    off  │
│                                                      │
│  ESP32 Network   IP Address [10.0.0.42] [Update]    │
└─────────────────────────────────────────────────────┘
```

**Features:**
- One row per device: its name, its address (when set), and `online`, `unreachable since
  hh:mm` or `off`, from the same `/api/status` poll as the pills (every 5 s)
- The LED wall's address can be changed here: saved to `pi5/.env`, tried at once

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

### 6. Ask First (the in-page confirm)

**Trigger:** a tap that would lose something: a reopening button at AT THE POST, RUNNING or
WINNER, or AUTO switched on then (`askFirst()` in `ddm_control.js`)

**Layout:**
```
┌─────────────────────────────────────────────────────┐
│                  Reopen betting?                     │
│                                                      │
│  This reopens betting. The figures at the post       │
│  (pot $86) and the hand count ($80) will be cleared. │
│                                                      │
│        [ REOPEN BETTING ]     [ CANCEL ]             │
└─────────────────────────────────────────────────────┘
```

**Behavior:**
- Nothing is sent until the first button; Cancel sends nothing and leaves the page as it was
- No browser `confirm()`
- An answer keeps the dialog up, its buttons off, for 0.6 s, so the second tap of a double tap
  lands on it and not on the button under it; a double tap on Reopen betting sends once
- While it is up no other mode button takes a tap
- Tests: `la_quiniela/test_dashboard.py` (`test_race_night_guards_in_a_browser`: every
  reopening button in 3, 4 and 5 and in 0-2, TEST at the post, a re-close, AUTO), and the
  two buttons in `tap_checks` (44 px each way)

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
**Usage:** Race-phase buttons disabled when results set; every mode button while AUTO is on
(class `.auto-held`, dashed, with a `switch to MANUAL` line on the button, `updatePanelButtons()`)

---

## Loading Indicator

**Element:** `#loader`
**Class:** `.loader.show`

**Behavior:**
- Shows for minimum 2 seconds
- Displays during API calls
- Hides immediately on error
- Prevents multiple rapid clicks
- Not on the mode buttons (2026-10-07): a mode button sends once a tap, its race state and its LEDs, and takes no
  other tap until those have ended and 2 s have passed (`.is-sending`); no mode button takes one while a race state
  is on its way; a slow LED controller holds only the button tapped, so another mode goes at once

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
- "Results set: Win=5, Place=12, Show=8 · WINNER" (green: the race state the results set)
- "Results set: Win=5, Place=12, Show=8 · race state not set (La Quiniela: bridge not
  initialised)" (red: saved, but La Quiniela refused WINNER; its reason). `POST
  /api/results` takes all three, three different horses in the field, or answers a 400
  that says which and saves nothing

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

- **Minimum touch target:** 44 × 44 px, padding included, for everything the host presses
  on race night, here and on the LQ admin page (an iPad in landscape; a small round × takes
  its taps on an invisible 44 px square), and AT THE GATE and THEY'RE OFF! 8 px or more from
  any other mode button. `la_quiniela/test_dashboard.py` guards it in headless Chrome with
  touch at 1180 × 820 and 1024 × 768 (`tap_checks` in `pi5/tools/picker_check.py`; `python
  tools/picker_check.py --taps` from `pi5/` prints every size at four landscape sizes)
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

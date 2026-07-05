# DDM La Quiniela - Live Betting System Specification

**Version:** 2.3  
**Date:** May 2026  
**Status:** Design Phase - Ready for Prototyping  
**Target Debut:** DDM 2027  
**Author:** Joey + Claude  

---

## Executive Summary

**La Quiniela** is the live betting feature for the Derby de Mayo (DDM) Multimedia Experience, debuting at DDM 2027. It enables party guests to place physical "bets" on their chosen horses using breakable tickets. Guests purchase a numbered ticket, snap it in half, keep one half as their claim stub, and drop the other half into the cup of their selected horse. The system tracks betting activity in real-time across all 20 cups.

La Quiniela uses the **LQ Controller** (dedicated ESP32 for reading scales and driving displays) that communicates with the **LQ Module** (dashboard code on DevPi) for all logic, UI, and control.

---

## Nomenclature

| Name | What It Is | Location |
|------|------------|----------|
| **LQ Controller** | ESP32 - reads 20 scales, drives 20 LED matrices | Near mantle |
| **LQ Module** | Dashboard code - betting UI, logic, horse assignments | DevPi |
| **Cup Base ESP32** | Existing ESP32 - drives 636 LED rings (unchanged) | 10.0.0.44 |

---

## User Experience Flow

### Guest Betting Process

1. Guest pays $1 to receive a numbered ticket (1-500)
2. Guest snaps the ticket in half along the perforated line
3. Guest keeps one half as their claim stub
4. Guest drops the other half into the cup of their chosen horse (cups 1-20)
5. System detects the bet and updates the live betting display
6. Dashboard shows real-time betting activity across all horses

### Post-Race Payout

1. Race results are determined (Win/Place/Show)
2. Staff draws ticket halves from the winning cups
3. Winners match their claim stub to the drawn ticket halves
4. Winners claim their share of the prize pool

---

## System Architecture

### Hardware Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      DDM LA QUINIELA                            │
│              (LQ Controller + LQ Module on DevPi)               │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       LQ CONTROLLER                             │
│                   (ESP32 - Near mantle)                         │
│                                                                 │
│   ┌─────────────────┐         ┌─────────────────┐              │
│   │  Reads 20       │         │  Drives 20      │              │
│   │  HX711 Scales   │         │  LED Matrices   │              │
│   └────────┬────────┘         └────────┬────────┘              │
│            │                           │                        │
└────────────┼───────────────────────────┼────────────────────────┘
             │                           │
             ▼                           ▼
┌────────────────────────┐    ┌────────────────────────┐
│  20x BAR LOAD CELLS    │    │  20x WS2812B 8x8       │
│  (Inside scale bases)  │    │  MATRICES (on cups)   │
└────────────────────────┘    └────────────────────────┘

             │ WebSocket / HTTP
             ▼
┌─────────────────────────────────────────────────────────────────┐
│                         DEVPI                                   │
│                    (Existing DDM Pi)                            │
│                                                                 │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│   │  LQ MODULE   │  │ Bet Tracker  │  │   Database   │         │
│   │ Dashboard UI │  │   Logic      │  │   (SQLite)   │         │
│   └──────────────┘  └──────────────┘  └──────────────┘         │
│                                                                 │
│   - Receives weight data from LQ Controller                    │
│   - Calculates bet counts                                      │
│   - Handles scratches / horse assignments                      │
│   - Sends display commands to LQ Controller                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Component List (Estimated)

| Component | Quantity | Est. Cost | Notes |
|-----------|----------|-----------|-------|
| ESP32-WROOM-32 (LQ Controller) | 1 | $8 | Reads scales + drives displays |
| Bar Load Cells (1kg) | 20 | $40 | Bulk pack, for scale bases |
| HX711 Amplifier Boards | 20 | $30 | Bulk pack |
| WS2812B 8x8 LED Matrix | 20 | $80-100 | 2-packs (~$8-10 for 2) |
| Hookup Wire | 1 lot | $20 | For harness |
| Connectors (JST) | 1 lot | $15 | For modularity |
| **TOTAL** | | **~$190-215** | Plus tokens and printed parts |

**Note:** No additional Raspberry Pi needed - LQ Module runs on DevPi (existing).

### LQ Controller GPIO Assignment

The LQ Controller handles both scales (HX711) and displays (WS2812B):

**HX711 Load Cells (Shared Clock Method):**

| Function | ESP32 GPIO | Notes |
|----------|------------|-------|
| HX711 CLK (shared) | GPIO 17 | Common clock for all HX711s |
| HX711 DATA Scale 1 | GPIO 16 | |
| HX711 DATA Scale 2 | GPIO 4 | |
| HX711 DATA Scale 3 | GPIO 5 | |
| HX711 DATA Scale 4 | GPIO 18 | |
| HX711 DATA Scale 5 | GPIO 19 | |
| HX711 DATA Scale 6 | GPIO 21 | |
| HX711 DATA Scale 7 | GPIO 22 | |
| HX711 DATA Scale 8 | GPIO 23 | |
| HX711 DATA Scale 9 | GPIO 25 | |
| HX711 DATA Scale 10 | GPIO 26 | |
| HX711 DATA Scale 11 | GPIO 27 | |
| HX711 DATA Scale 12 | GPIO 32 | |
| HX711 DATA Scale 13 | GPIO 33 | |
| HX711 DATA Scale 14 | GPIO 34 | Input only |
| HX711 DATA Scale 15 | GPIO 35 | Input only |
| HX711 DATA Scale 16 | GPIO 36 | Input only |
| HX711 DATA Scale 17 | GPIO 39 | Input only |
| HX711 DATA Scale 18 | GPIO 13 | |
| HX711 DATA Scale 19 | GPIO 12 | |
| HX711 DATA Scale 20 | GPIO 14 | |

**WS2812B LED Matrices:**

| Function | ESP32 GPIO | Notes |
|----------|------------|-------|
| WS2812B Data | GPIO 15 | Daisy-chain all 20 matrices |

**Note:** Pin assignments are preliminary and may need adjustment based on testing.

---

## Mechanical Design

### Design Philosophy: Modular Stacking System

**Key Decision:** The weighing system is a **separate scale base** that sits underneath the existing LED cup base. This keeps all three components independent and modular.

The stack from bottom to top:
1. **Scale Base** - low-profile round box with load cell (NEW)
2. **LED Cup Base** - existing design, unchanged
3. **Betting Cup** - taller cup with LED matrix display

```
THE STACK

         ┌───────────────────────┐
         │                       │
         │     BETTING CUP       │
         │                       │
         │    ┌───────────┐      │
         │    │  8x8 LED  │      │  ← WS2812B matrix display
         │    │  MATRIX   │      │
         │    └───────────┘      │
         │                       │
         └───────────┬───────────┘
                     │ sits on
                     ▼
         ┌───────────────────────┐
         │                       │
         │   EXISTING LED BASE   │  ← UNCHANGED
         │                       │
         │   ○ ○ ○ LEDs ○ ○ ○    │
         │                       │
         └───────────┬───────────┘
                     │ sits on
                     ▼
         ┌───────────────────────┐
         │ ┌─────────────────┐   │  ← Top plate
         │ └────────┬────────┘   │
         │          │            │
         │   ═══════●═══════     │  ← Load cell bar
         │   ████████            │
         │   ████████ (fixed)    │
         │                       │
         └───────────────────────┘
              SCALE BASE            ← NEW: Low-profile weighing unit
              (~4.75" × 1")
```

### Component 1: Scale Base (NEW)

Low-profile round box that the entire cup assembly sits on.

**Specifications:**

| Spec | Value | Notes |
|------|-------|-------|
| Diameter | ~4.75" (~121mm) | ~1/8" wider than LED base |
| Height | ~1" (~25mm) | As slim as possible |
| Contents | Bar load cell + HX711 | Weighing mechanism |
| Output | 5V, GND, DATA, CLK | To harness |

**Internal Layout:**

```
CROSS-SECTION - SCALE BASE

┌─────────────────────────────────────────┐
│░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│ ← Top plate (3-4mm)
├─────────────────────────────────────────┤
│                                         │
│         ═══════════●════════════        │ ← Load cell bar (~12mm)
│         ████████████                    │
│         ████████████ (fixed end)        │
│                                         │
├─────────────────────────────────────────┤
│░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│ ← Bottom housing (3-4mm)
└─────────────────────────────────────────┘

Wire exit (toward wall) ───►
```

### Component 2: LED Cup Base (UNCHANGED)

The existing cup base design is preserved. No modifications needed.

| Spec | Current Value | Notes |
|------|---------------|-------|
| Diameter | 4" (101.6mm) | May resize to 4.5" to match betting cups |
| Height | 1" (25.4mm) | |
| LEDs | 32 WS2812B (ring) | Some cups have 31 |

### Component 3: Betting Cup

Taller cup with integrated LED matrix display on the front.

**Specifications:**

| Spec | Value | Notes |
|------|-------|-------|
| Diameter | 4.5" (114.3mm) | Matches LED base |
| Height | 6" (150mm) | Taller for ticket capacity |
| Interior | ~104mm | Room for 25mm ticket halves |
| Display | WS2812B 8x8 matrix | 66×66mm, full RGB |

**Display Integration:**

```
FRONT VIEW - BETTING CUP

         ┌─────────────────────────┐
         │                         │
         │                         │
         │                         │
         │    ┌───────────────┐    │
         │    │ ░░░░███░░░░░░ │    │  ← WS2812B 8x8 Matrix
         │    │ ░░░░░░█░░░░░░ │    │    (~2.6" / 66mm)
         │    │ ░░░░░░█░░░░░░ │    │
         │    │ ░░░░░░█░░░░░░ │    │    Shows: Horse number
         │    │ ░░░░░░█░░░░░░ │    │           or "X" for scratch
         │    │ ░░░███████░░░ │    │           or animations
         │    └───────────────┘    │
         │                         │
         │       [ OPENING ]       │
         │                         │
         └─────────────────────────┘
```

### Dimensions Summary

| Component | Diameter | Height | Notes |
|-----------|----------|--------|-------|
| Betting Cup | 4.5" | 6" | With LED matrix |
| LED Cup Base | 4.5" | 1" | Unchanged |
| Scale Base | 4.75" | 1" | ~1/8" wider |
| **Total Stack** | 4.75" | **8"** | Full assembly |

### Capacity Planning

| Pot Size | Tickets | Avg/Cup | Max/Cup (realistic) |
|----------|---------|---------|---------------------|
| $300 | 300 | 15 | 30-40 |
| $500 | 500 | 25 | 50-60 |
| $800 | 800 | 40 | 80-100 |
| $1000 | 1000 | 50 | 100+ |

With 6" tall cups and 3mm thick ticket halves, each cup can hold ~50 tickets comfortably, supporting growth to a $1000+ pot.

### Benefits of Modular Approach

| Benefit | Description |
|---------|-------------|
| **LED base unchanged** | Existing cup base preserved completely |
| **Independent components** | Scale, base, cup are all separate units |
| **Easy prototyping** | Can develop/test scale base independently |
| **Flexible deployment** | Can use cups without scales if needed |
| **Low profile** | Scale base is only ~1" tall |
| **Hidden wiring** | Scale wires exit toward wall, out of sight |

### Wiring

**Scale Base wiring (per unit):**
- 5V (power)
- GND (ground)
- DATA (HX711 to Pi)
- CLK (shared clock from Pi)

**Betting Cup display wiring (per unit):**
- 5V (power)
- GND (ground)  
- DATA IN (from previous in chain or ESP32)
- DATA OUT (to next in chain)

### Open Questions (To Resolve During Prototyping)

- [ ] Exact load cell dimensions (order and measure)
- [ ] Top plate attachment to load cell (screws, adhesive)
- [ ] Wire exit location and routing
- [ ] Scale base bottom surface (rubber feet for grip?)
- [ ] Display mounting method in cup wall
- [ ] Display controller (separate ESP32 for matrices)

---

## Ticket Design

### Concept

Numbered tickets (1-500) that snap apart along a scored/perforated line. Guest keeps one half as their claim stub while dropping the other half in their chosen cup. Classic betting ticket aesthetic with the reliability of 3D printed consistency.

### Specifications

| Attribute | Target Value | Notes |
|-----------|--------------|-------|
| Material | PLA or PETG | Consistent density for weight |
| Total size | 50mm × 25mm × 3mm | Two 25mm × 25mm halves |
| Thickness | 3mm | Sturdy feel, good weight |
| Weight per half | ~2-2.5 grams | Sufficient for detection |
| Numbering | 1-500 | On BOTH halves |
| Split Method | Scored line / living hinge | Snap apart cleanly |
| Colors | 3-color print (H2D) | Base, DDM text, numbers |

### Design Concept - Breakable Ticket

```
TOP VIEW (intact ticket)

┌─────────────────────┬─────────────────────┐
│                     │                     │
│       KEEP          ┆        DROP         │
│                     ┆                     │
│       #247          ┆        #247         │
│                     ┆                     │
│    🏇 DDM '27       ┆     🏇 DDM '27      │
│                     │                     │
└─────────────────────┴─────────────────────┘
          25mm        ┆        25mm
                      ▲
               Scored line / thin bridge
                  (snap apart)

SIDE VIEW

┌──────────────────────────────────────────┐
│██████████████████████│██████████████████│  ← 3mm thick
└──────────────────────────────────────────┘
                       ▲
                 Thin section (~0.5mm)
                 for clean break
```

### 3-Color Print Layout

| Color | Usage |
|-------|-------|
| **Color 1 (Base)** | Main ticket body |
| **Color 2 (Accent)** | "DDM" text, branding, decorative elements |
| **Color 3 (Contrast)** | Ticket number (high visibility) |

### Print Production

| Metric | Estimate |
|--------|----------|
| Tickets per build plate | ~20-25 |
| Time per sheet (3-color) | ~50-60 min |
| Sheets needed for 500 | ~20-25 sheets |
| **Total print time** | **~20-25 hours** |

Printing can be done in batches over 2-3 weeks with overnight runs.

### Detection Requirements

- Each ticket half must weigh enough to reliably trigger the load cell
- Target: Detectable weight change of ~2-2.5 grams per ticket half
- Load cell resolution with HX711: ~0.1g (more than sufficient)
- At 50 tickets per cup: ~100-125 grams total weight change (easily detected)
- Multiple tickets dropped simultaneously: System calculates count from total weight change

---

## Software Architecture

### Two-Part System

**LQ Controller Firmware (C++/Arduino):**
- Reads 20 HX711 load cells
- Drives 20 WS2812B matrices
- Sends weight data to DevPi via WebSocket
- Receives display commands from DevPi
- Simple, real-time, reliable

**LQ Module (Python/Flask on DevPi):**
- La Quiniela module added to existing DDM dashboard
- Handles all betting logic
- Manages horse assignments and scratches
- Serves La Quiniela UI
- Sends display commands to LQ Controller

### LQ Controller Firmware Structure

```
la_quiniela_esp32/
├── la_quiniela_esp32.ino    # Main firmware
├── config.h                  # Pin assignments, WiFi config
├── hx711_multi.h            # Multi-channel HX711 reading
├── display_matrix.h         # WS2812B matrix rendering
└── websocket_client.h       # Communication with DevPi
```

### LQ Module Code Structure (Added to existing DDM project)

```
pi5/
├── main.py                   # Existing DDM entry point
├── ...                       # Existing DDM modules
│
└── la_quiniela/              # NEW: LQ Module
    ├── __init__.py
    ├── bet_tracker.py        # Betting logic, counts
    ├── horse_manager.py      # Assignments, scratches
    ├── lq_controller.py      # WebSocket to LQ Controller
    ├── models.py             # Data models
    └── routes.py             # La Quiniela API routes

templates/
└── la_quiniela.html          # La Quiniela dashboard UI
```

### Communication Flow

```
LQ CONTROLLER                            LQ MODULE (DevPi)
  │                                        │
  │ ──── WebSocket: weight data ────────►  │
  │      {"scale": 7, "weight": 125.3}     │
  │                                        │
  │                                        ├── Calculate bet delta
  │                                        ├── Update bet counts
  │                                        ├── Update dashboard
  │                                        │
  │ ◄──── WebSocket: display cmd ────────  │
  │       {"cup": 7, "show": "12"}         │
  │       {"cup": 5, "show": "X"}          │
  │                                        │
  └── Render on LED matrix                 │
```

### Software Flow

```
LQ CONTROLLER LOOP (every 100-200ms):
   └── Read all 20 HX711s
   └── Send weight array to DevPi via WebSocket
   └── Check for incoming display commands
   └── Update LED matrices as commanded

LQ MODULE HANDLER (on weight data received):
   └── Calculate weight delta from last reading
   └── If delta > TOKEN_THRESHOLD (~5g):
       └── Calculate token count change
       └── Update bet count for that cup
       └── Log event with timestamp
       └── Broadcast to dashboard via WebSocket
   └── If horse assignment changes:
       └── Send display command to LQ Controller
```

### API Endpoints (LQ Module on DevPi)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/la-quiniela` | GET | La Quiniela dashboard page |
| `/api/la-quiniela/bets` | GET | Current bet counts for all cups |
| `/api/la-quiniela/horses` | GET | Horse assignments per cup |
| `/api/la-quiniela/horses` | POST | Set horse assignments |
| `/api/la-quiniela/scratch/<cup>` | POST | Mark cup as scratched |
| `/api/la-quiniela/tare` | POST | Re-tare all scales |
| `/api/la-quiniela/reset` | POST | Reset all bet counts |
| `/api/la-quiniela/status` | GET | LQ Controller connection status |
| `/ws/la-quiniela` | WebSocket | Real-time updates |

---

## Dashboard UI Concept

### Main Display

```
┌─────────────────────────────────────────────────────────────────┐
│  🏇 LA QUINIELA - DERBY DE MAYO 🏇              POT: $147      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   1  ████████████████░░░░ 18    11 ██████░░░░░░░░░░░░░░  8     │
│   2  ██████████░░░░░░░░░░ 12    12 ████░░░░░░░░░░░░░░░░  5     │
│   3  ████████████████████ 23    13 ██████████░░░░░░░░░░ 11     │
│   4  ██████░░░░░░░░░░░░░░  7    14 ░░░░░░░░░░░░░░░░░░░░  0     │
│   5  ████████████░░░░░░░░ 14    15 ████████░░░░░░░░░░░░  9     │
│   6  ████░░░░░░░░░░░░░░░░  4    16 ██░░░░░░░░░░░░░░░░░░  2     │
│   7  ██████████████░░░░░░ 16    17 ██████████████░░░░░░ 15     │
│   8  ██████████░░░░░░░░░░ 11    18 ████░░░░░░░░░░░░░░░░  5     │
│   9  ████████░░░░░░░░░░░░  9    19 ██████░░░░░░░░░░░░░░  6     │
│  10  ░░░░░░░░░░░░░░░░░░░░  1    20 ████████░░░░░░░░░░░░ 10     │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  TOTAL BETS: 147  │  LEADER: #3 (23 bets)  │  STATUS: BETTING  │
├─────────────────────────────────────────────────────────────────┤
│  RECENT: Cup 7 +1 (3s ago) │ Cup 3 +1 (8s ago) │ Cup 17 +1     │
└─────────────────────────────────────────────────────────────────┘
```

### Features

- **Live bar charts** showing bets per horse
- **Total pot** calculation ($1 × total bets)
- **Leader board** highlighting most-bet horses
- **Recent activity** feed showing latest bets
- **Admin controls** (hidden/authenticated):
  - Open/Close betting
  - Manual adjustments
  - Tare sensors
  - Reset counts
  - Export data

---

## Integration with Main DDM System

### Same Pi, Same Dashboard

LQ Module runs on **DevPi** - the same Pi that runs the main DDM dashboard. This means:

- **No inter-Pi communication needed** - everything is on one device
- **Unified dashboard** - La Quiniela is a section/tab within the existing DDM dashboard
- **Shared database** - bet data lives alongside race results, settings, etc.
- **Coordinated animations** - Easy to trigger LED effects based on bets or results

### Communication with Existing Cup Base ESP32

The existing Cup Base ESP32 (10.0.0.44) controls the cup base LED rings. DevPi can send commands to it for bet-triggered animations:

```
DEVPI
  │
  ├──── WebSocket to LQ Controller ─────► LQ Controller (scales + displays)
  │
  └──── HTTP to Cup Base ESP32 ─────────► Cup Base ESP32 (LED rings)
        (existing API)                    10.0.0.44
```

### Potential Integrations

1. **LED Feedback on Bet:** When a bet is detected, flash that cup's LED ring briefly
2. **Betting Stats Overlay:** Show La Quiniela stats on main dashboard
3. **Odds-Based Animation:** Heavily-bet horses could have different LED behavior
4. **Results Integration:** Auto-highlight winning cups when results are entered

---

## Development Phases

### Phase 1: Hardware Proof of Concept (May-Jul 2026)

- [ ] Order load cells and HX711 boards (2-3 for testing)
- [ ] Test single load cell detection with Pi
- [ ] Design and print prototype betting cup with load cell mount
- [ ] Print test tickets, validate snap-apart and weight
- [ ] Validate ticket weight detection reliability
- [ ] **Goal: 1 working prototype cup for DDM 2026 sneak peek**

### Phase 2: Multi-Cell Prototype (Aug-Oct 2026)

- [ ] Wire up 3-5 load cells with shared clock method
- [ ] Verify no cross-talk or interference
- [ ] Test simultaneous reading performance
- [ ] Refine software detection algorithms
- [ ] Finalize betting cup mechanical design

### Phase 3: Full System Build (Nov 2026 - Jan 2027)

- [ ] Order remaining load cells (20 total)
- [ ] Build complete wiring harness
- [ ] Print all 20 betting cups
- [ ] Integrate sensors into all cups
- [ ] Full system calibration

### Phase 4: Software & UI (Feb-Mar 2027)

- [ ] Build Flask web dashboard (La Quiniela branding)
- [ ] Implement real-time WebSocket updates
- [ ] Add admin controls
- [ ] Test with actual tickets
- [ ] Main DDM system integration (LED feedback on bets)

### Phase 5: Ticket Production (Mar-Apr 2027)

- [ ] Finalize 3-color ticket design
- [ ] Print 500 tickets (~20-25 hours total)
- [ ] Verify numbering and snap-apart quality
- [ ] Package tickets for event

### Phase 6: Polish & Testing (Apr 2027)

- [ ] Full system integration testing
- [ ] Edge case handling
- [ ] UI polish
- [ ] Dry run with test guests

### 🎉 DDM 2027: La Quiniela Debuts (May 2027)

---

## Open Questions & Decisions

| Question | Status | Notes |
|----------|--------|-------|
| Load cell exact model/dimensions | Pending | Need to order and measure |
| Scale base internal geometry | Pending | Design after load cell in hand |
| Scale base top plate attachment | Pending | Screws vs adhesive to load cell |
| Display mounting in cup wall | Pending | Recess, flush, or protruding |
| LED base resizing | Pending | Keep 4" or resize to 4.5" to match cups |
| Ticket scored line thickness | Pending | Test prints to dial in break strength |
| Ticket 3-color layout | Pending | Design in progress |
| Prize pool split ratio | Pending | Win/Place/Show percentages |
| ESP32 WiFi vs wired | Pending | WiFi simpler, wired more reliable |

---

## Reference Links

- HX711 Datasheet: (add when sourced)
- Load Cell Supplier: (add when ordered)
- Main DDM Project Repo: https://github.com/JoeyEinTX/DDM-Multimedia.git

---

## Revision History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | Dec 2025 | Initial specification document |
| 1.1 | Dec 2025 | Updated token design (30mm, 2.5mm thick, snap-fit), cup/base dimensions (4.5" × 6"), capacity planning for growth |
| 1.2 | Dec 2025 | Revised mechanical design: self-contained betting cup with integrated load cell, existing LED base unchanged |
| 2.0 | May 2026 | Rebranded to "La Quiniela", switched from tokens to breakable tickets (50×25×3mm, 3-color), updated timeline for DDM 2027 debut |
| 2.1 | May 2026 | Revised to modular stacking system: separate scale base underneath LED base, added WS2812B 8x8 matrix displays on betting cups |
| 2.2 | May 2026 | Consolidated architecture: single ESP32 handles scales + displays, DevPi handles logic/dashboard (no dedicated La Quiniela Pi), updated GPIO assignments and software architecture |
| 2.3 | May 2026 | Established official nomenclature: LQ Controller (ESP32 hardware) and LQ Module (DevPi software), updated all references throughout document |

---

*This document will be updated as the hardware prototyping progresses and design decisions are finalized.*

# Derby de Mayo's La Subasta — System Specification

**Version:** 1.2 (Added Admin Tunables & Overrides)
**Date:** April 2026
**Status:** Design Phase - Ready for Implementation
**Author:** Joey + Claude
**Target Event:** Derby de Mayo 2026 (May 2, 2026)

---

## Naming & Branding Conventions

| Context | Usage |
|---------|-------|
| **Full event name (signage, splash, headers)** | `Derby de Mayo's` / `La Subasta` (two-line stack) |
| **Short name (body text, nav, UI chrome)** | `La Subasta` |
| **Code directory** | `pi5/la_subasta/` |
| **URL slug** | `/la-subasta` |
| **CSS file prefix** | `la-subasta-*.css` |
| **Verbal reference** | "Go bid in La Subasta" |

**Two-line logo treatment:**
```
Derby de Mayo's
   La Subasta
```
Top line = smaller, serape-beige, sets context (whose event). Bottom line = big, bold, headline (Mistletoe Green or Sunflower Yellow per DDM palette).

---

## Executive Summary

Derby de Mayo's **La Subasta** is an automated, hands-off horse auction for Derby de Mayo guests. Instead of traditional parimutuel betting, each of the 20 Derby horses is auctioned to the highest bidder throughout party day. Guests bid from their phones (or Joey's iPad) on the home wifi, outbid each other in real time, and the final roster locks 15 minutes before post time. After the race, payouts are auto-calculated from the pot using a 60/25/15 win/place/show split.

**Key design principle: zero live management required from Joey.** System opens in the morning, runs itself all day, locks on schedule, and auto-computes payouts when Joey enters race results into the existing dashboard.

This is a **new feature** integrated into the existing Pi 5 Flask dashboard — not a standalone system. It leverages the existing hardware, database, WebSocket infrastructure, and brand styling.

---

## User Experience Flow

### Morning of Party (Joey — 1 action)

1. Joey opens admin view on iPad, hits **"Start Auction"**
2. System loads the 20 horses from the shared horse database
3. Auction goes live at 9:00 AM (configurable)
4. QR code placards around the house point guests to `http://ddm.local/la-subasta` (or the Pi's IP)

### Party Day (Guests)

1. Guest scans QR code on their phone, lands on guest view
2. **First-visit identity:** enters first name + last initial ("Dave K"), picks a festive emoji from a themed grid (🌮🐴🌶️🎺💃🎲🍹🌵🎸⭐)
3. Name+emoji combo must be globally unique (duplicate = "pick another emoji")
4. Optional: tap "Enable alerts" for browser push notifications on outbid events
5. Stored in browser `localStorage` — return visits auto-load
6. Guest browses the field, bids on horses they want (min $1, max $5 raise, max 3 horses owned)
7. When outbid, gets push notification (if opted in) + in-app banner if app open
8. Spectator TV shows rotating leaderboard, hot horses, biggest spenders, recent bid activity

### Lockdown (T-15 min before post time)

1. System auto-closes bidding at hard-stop timestamp (no snipe extensions)
2. All displays flash "BIDDING CLOSED — TIME TO PAY UP" with cheeky messaging
3. Spectator TV transitions to **Ownership Reveal**: rotates through all 20 horses showing owner name + emoji + winning bid
4. Guest phones show their final portfolio ("You own: #3 Magnolia ($45), #14 Carry Back ($67) — Total: $112 — Pay Joey")
5. Admin dashboard shows per-bidder Paid/Unpaid tracker
6. Joey chases payments face-to-face as guests arrive / already at party (Venmo, Zelle, cash)

### Race & Payouts (Automatic)

1. Derby runs. Joey enters W/P/S results into existing dashboard (single source of truth for race results)
2. La Subasta system detects results via shared database, auto-computes payouts
3. **60/25/15 split** applied to total pot:
   - Win: 60% of total pot → Winner's owner
   - Place: 25% of total pot → 2nd place owner
   - Show: 15% of total pot → 3rd place owner
4. Winners get push notification: "🎉 YOU WON! $340 on Magnolia"
5. Spectator TV does victory recap: winner portrait, owner name, payout amounts, biggest winner of the night
6. Non-winners get "Better luck next year" recap with their portfolio summary
7. Admin view shows payout ledger for Joey to settle with winners

---

## System Architecture

### Leverages Existing Infrastructure

This system **does not require new hardware**. It runs as a new Flask blueprint on the existing Pi 5 dashboard.

| Component | Existing or New | Notes |
|-----------|-----------------|-------|
| Raspberry Pi 5 | Existing | Hosts dashboard + La Subasta |
| Flask server | Existing | New `/la-subasta` blueprint |
| SocketIO | Existing | Real-time bid updates |
| SQLite database | Existing | New La Subasta tables |
| Horse roster | Existing | Shared with dashboard |
| DDM branding CSS | Existing | Reused across views |
| LED cup array | Existing | Optional visual tie-in |

### Three Views, One Flask App

```
┌─────────────────────────────────────────────────────────────┐
│                      PI 5 FLASK SERVER                      │
│                                                             │
│   ┌───────────────────────────────────────────────────┐    │
│   │  EXISTING DASHBOARD BLUEPRINT (unchanged)         │    │
│   └───────────────────────────────────────────────────┘    │
│                                                             │
│   ┌───────────────────────────────────────────────────┐    │
│   │  NEW: /la-subasta BLUEPRINT                         │    │
│   │                                                   │    │
│   │   ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │    │
│   │   │   /guest    │  │   /admin    │  │/spectator│  │    │
│   │   │  (phones)   │  │   (iPad)    │  │   (TV)  │  │    │
│   │   └─────────────┘  └─────────────┘  └─────────┘  │    │
│   │          │                │              │        │    │
│   │          └────────────────┴──────────────┘        │    │
│   │                    │                              │    │
│   │              SocketIO broadcasts                  │    │
│   │              (all views update live)              │    │
│   └───────────────────────────────────────────────────┘    │
│                                                             │
│   ┌───────────────────────────────────────────────────┐    │
│   │  SQLite Database                                  │    │
│   │  - bidders, horses (shared), bids, payouts        │    │
│   └───────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### Data Model (SQLite)

```sql
-- Shared with existing dashboard, not created here
-- horses: id, saddle_cloth, name, jockey, program_number, scratched

-- NEW tables for La Subasta:

bidders (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,           -- "Dave K"
  emoji TEXT NOT NULL,          -- "🌮"
  identity TEXT UNIQUE NOT NULL,-- "Dave K 🌮" (name + emoji)
  push_endpoint TEXT,            -- browser push subscription JSON (optional)
  created_at TIMESTAMP,
  paid BOOLEAN DEFAULT FALSE,    -- admin-marked
  paid_at TIMESTAMP,
  paid_amount REAL
);

bids (
  id INTEGER PRIMARY KEY,
  bidder_id INTEGER REFERENCES bidders,
  horse_id INTEGER REFERENCES horses,
  amount REAL NOT NULL,
  bid_time TIMESTAMP NOT NULL,
  voided BOOLEAN DEFAULT FALSE,  -- admin can void
  voided_reason TEXT
);
-- Append-only log; current high bid is MAX(amount) WHERE horse_id = X AND voided = FALSE

ownership (
  id INTEGER PRIMARY KEY,
  horse_id INTEGER UNIQUE REFERENCES horses,
  bidder_id INTEGER REFERENCES bidders,
  winning_bid REAL NOT NULL,
  locked_at TIMESTAMP
);
-- Populated when auction closes

payouts (
  id INTEGER PRIMARY KEY,
  bidder_id INTEGER REFERENCES bidders,
  horse_id INTEGER REFERENCES horses,
  finish TEXT NOT NULL,          -- 'win', 'place', 'show'
  amount REAL NOT NULL,
  paid_out BOOLEAN DEFAULT FALSE
);

auction_state (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,           -- 'NOT_STARTED','OPEN','FINAL_HOUR','LOCKED','RACE_COMPLETE','SETTLED'
  opens_at TIMESTAMP,
  closes_at TIMESTAMP,
  total_pot REAL DEFAULT 0
);
```

---

## Bidding Mechanics

### Identity System

- **Name format:** First name + last initial ("Dave K", "Sarah M")
- **Emoji palette (themed, 16 options):**
  🌮 🐴 🌶️ 🎺 💃 🎲 🍹 🌵 🎸 ⭐ 🎩 🍀 🏇 🌶️ 🥭 🪅
- **Uniqueness enforcement:** "Dave K 🌮" is a unique ID. If taken, user prompted to pick different emoji.
- **Persistence:** Stored in browser `localStorage`. Return visits auto-load without re-entry.
- **No password / login.** If someone else picks up their phone, they're that person. Accept this tradeoff for zero-friction UX.

### Bid Rules

| Rule | Value | Rationale |
|------|-------|-----------|
| Minimum opening bid | $1 | Accessible |
| Minimum raise | $1 | Fine-tuning allowed |
| **Maximum raise** | **$5** | Prevents runaway bidding, keeps pot reasonable |
| Maximum horses per bidder | 3 | Spreads ownership across crowd |
| Bid close | Hard stop at T-15 before Derby post | No snipe extensions |
| Tiebreaker | Earliest timestamp wins | Standard |
| Bid retraction | 10-second undo window | Fat-finger protection |
| Scratched horse | Bid refunded, horse removed | Standard |

### Enforcement Logic (server-side validation)

```python
def validate_bid(bidder, horse, amount):
    # Identity and state checks
    if auction_state not in ('OPEN', 'FINAL_HOUR'): reject("Auction closed")
    if horse.scratched: reject("Horse scratched")

    # Max 3 horses owned
    currently_leading = count_horses_where_top_bidder(bidder)
    if currently_leading >= 3 and not outbid_existing: reject("Max 3 horses")

    # Bid range
    current = horse.current_high_bid or 0
    if amount < current + 1: reject("Must bid at least $1 more")
    if amount > current + 5: reject("Max raise is $5")

    # Can't outbid yourself
    if horse.current_high_bidder == bidder: reject("You're already leading")

    accept()
```

### Anti-Snipe Policy

**Hard stop at T-15.** No extensions. Joey's explicit call. Snipes are part of the game — the max-3-horses cap naturally limits sniper damage (a last-minute sniper can grab at most 3 horses anyway).

### Welch / Void Policy

If a guest refuses to pay or bids in bad faith:

1. Admin clicks **"Void bid"** on that bidder's winning bid for a specific horse
2. System automatically re-awards horse to **2nd highest bidder at their bid amount**
3. If no 2nd bid exists, horse goes to House (see below)
4. Audit trail preserved in `voided_reason` column

---

## Payment Tracking (Honor System)

### Flow

1. Auction closes → every bidder has a "total owed" (sum of winning bids)
2. Admin view shows all bidders with Paid/Unpaid status
3. As guests arrive / mingle, Joey hits them up: "Dave, you owe $112"
4. Guest Venmos/Zelles/hands cash → Joey taps **"Mark Paid"** in admin
5. Post-race, winners receive payouts minus any unpaid balance

### Admin Bidder View

```
┌────────────────────────────────────────────────────────────┐
│  BIDDERS                                  Unpaid: $347     │
├────────────────────────────────────────────────────────────┤
│  Dave K 🌮      $112   [3 horses]  [ Mark Paid ]   [Void] │
│  Sarah M 💃     $87    [2 horses]  ✓ PAID                  │
│  Mike T 🎺      $145   [3 horses]  [ Mark Paid ]   [Void] │
│  ...                                                       │
└────────────────────────────────────────────────────────────┘
```

### Edge Case: Unpaid Bidder Wins

If an unpaid bidder wins a payout, admin sees flag: "Mike T 🎺 owes $145 but won $340 on Magnolia — net $195 owed to them." Joey settles net.

---

## Payouts

### Formula

```
Total Pot = sum of all winning bids across 20 horses

Win payout  = Total Pot × 0.60 → horse finishing 1st
Place payout = Total Pot × 0.25 → horse finishing 2nd
Show payout  = Total Pot × 0.15 → horse finishing 3rd
```

### Example

Total pot = $500
- Winner owner: $300
- Place owner: $125
- Show owner: $75

### "The House" Safety Net

Joey's encouraging a bid on all cups, so this shouldn't trigger. But as a safety net:

- Any horse with **zero bids** at lockdown goes to "The House" at $0
- If The House wins a payout, it rolls to the **DDM 2027 Build Fund** (displayed on spectator TV as: "The House wins $45 → DDM 2027 Fund 🛠️")

### Settlement

1. Race results entered by Joey into existing dashboard
2. La Subasta system reads results, computes payouts
3. Push notifications fire to winners
4. Spectator TV runs victory sequence (see below)
5. Admin view shows payout ledger: "Pay Dave K 🌮 $300 for Magnolia (Win)"
6. Joey Venmos out winnings, taps **"Mark Paid Out"** per row

---

## Real-Time Updates

### WebSocket (SocketIO) Events

Reuses existing SocketIO infrastructure. New event types:

| Event | Payload | Broadcast To |
|-------|---------|--------------|
| `bid_placed` | `{horse, bidder, amount, prev_bidder}` | All views |
| `outbid` | `{horse, old_bidder, new_bidder, amount}` | Old bidder's push |
| `auction_locked` | `{timestamp}` | All views |
| `horse_scratched` | `{horse, refund_count}` | All views |
| `results_entered` | `{win, place, show}` | All views |
| `payout_computed` | `{bidder, amount, horse, finish}` | Winner push |
| `paid_marked` | `{bidder}` | Admin view |

### Browser Push Notifications

**Tech:** Web Push API + service worker. Free, works over local wifi, no SMS service needed.

**Flow:**
1. First-time visit → "Enable alerts?" one-tap prompt
2. Browser generates push subscription endpoint → stored in `bidders.push_endpoint`
3. Server fires push via Pi's internal service when outbid / win events occur
4. Works on iOS 16.4+ and all Android/desktop Chrome/Firefox
5. Notifications fire even when app is closed or phone locked

**Payload example:**
```json
{
  "title": "You were outbid on Magnolia!",
  "body": "Dave K 🌮 just bid $42. Tap to counter.",
  "icon": "/static/ddm-logo.png",
  "click_action": "/la-subasta#horse-3"
}
```

### Graceful Failure

Payouts and ownership are stored in DB — **never dependent on notifications landing**. If push fails:
- Guest sees celebration when they next open the app
- Spectator TV shows winner regardless
- Admin can manually trigger a celebration notification for any bidder

---

## UI / UX — Three Views

### 1. Guest Phone View (`/la-subasta`)

**Mobile-first, portrait orientation, single column, large tap targets.**

```
┌─────────────────────┐
│ 🏇 LA SUBASTA       │
│ Dave K 🌮  $112     │
│ 3 horses owned      │
├─────────────────────┤
│ ⏰ 2h 14m until     │
│    bidding closes   │
├─────────────────────┤
│ ┌─────────────────┐ │
│ │ #3 MAGNOLIA     │ │
│ │ [horse photo]   │ │
│ │ Current: $42    │ │
│ │ Leader: Dave K🌮│ │ ← you!
│ │ [  BID $43  ]   │ │
│ │ [  BID $47  ]   │ │
│ │ [ CUSTOM BID ]  │ │
│ └─────────────────┘ │
│                     │
│ ┌─────────────────┐ │
│ │ #4 CARRY BACK   │ │
│ │ Current: $28    │ │
│ │ Leader: Sarah M💃│ │
│ │ [  BID $29  ]   │ │
│ │ ...             │ │
│ └─────────────────┘ │
│                     │
│ [ MY PORTFOLIO ]    │
│ [ LEADERBOARD ]     │
└─────────────────────┘
```

**Features:**
- Pull-to-refresh
- Sticky header with identity + countdown
- Horse cards with photo, current bid, leader, quick-bid buttons (+$1, +$5, custom)
- "My Portfolio" tab: horses currently leading on
- Outbid banner slides in when notified
- 10-second undo toast after placing bid

### 2. Admin iPad View (`/la_subasta/admin`)

**Landscape iPad layout, denser info, admin controls.**

```
┌──────────────────────────────────────────────────────────────┐
│ 🏇 LA SUBASTA ADMIN        State: OPEN  │  Pot: $412         │
├──────────────┬───────────────────────────────────────────────┤
│              │                                               │
│  BIDDERS     │  HORSES                                       │
│  (sortable)  │  # │ Name      │ High │ Leader   │ Actions   │
│              │  1 │ Secretariat│ $45 │ Dave K🌮 │ [Void][...]│
│  Dave K🌮    │  2 │ Magnolia  │ $42 │ Dave K🌮 │ [Void][...]│
│  $112  UNPAID│  3 │ ...       │ ... │ ...      │ ...        │
│  [Pay][Void] │  ...                                          │
│              │                                               │
│  Sarah M💃   ├───────────────────────────────────────────────┤
│  $87  PAID✓  │  LIVE BID FEED                                │
│              │  [10:42] Dave K🌮 bid $42 on Magnolia         │
│  ...         │  [10:41] Sarah M💃 bid $28 on Carry Back      │
│              │  ...                                          │
├──────────────┴───────────────────────────────────────────────┤
│ [START AUCTION] [LOCK NOW] [ENTER RESULTS] [TESTING MODE]    │
└──────────────────────────────────────────────────────────────┘
```

**Admin powers:**
- Mark any bidder Paid/Unpaid
- Void any bid (triggers re-award)
- Manually edit a bid (fix fat-finger)
- Mark horse scratched
- Force lock auction early
- Enter race results
- Mark payouts as settled
- Toggle Testing/Sandbox Mode
- Export full data as CSV/JSON

### 3. Spectator TV View (`/la_subasta/spectator`)

**Landscape, big TV, 1080p or 4K, auto-rotating panels.**

Rotating content every 15 sec:

**Panel A — Live Bid Activity (default)**
```
┌────────────────────────────────────────────────────┐
│  🏇 DERBY DE MAYO'S LA SUBASTA                     │
│                                                    │
│  POT: $412     ⏰ 2h 14m LEFT                      │
│                                                    │
│  🔥 HOT HORSE                                      │
│  ┌──────────────────────┐                          │
│  │  #3 MAGNOLIA          │                         │
│  │  $42                  │                         │
│  │  Dave K 🌮 leading    │                         │
│  └──────────────────────┘                          │
│                                                    │
│  RECENT BIDS:                                      │
│  ● Dave K🌮 → Magnolia $42  (just now)             │
│  ● Sarah M💃 → Carry Back $28  (8s ago)            │
│  ● Mike T🎺 → Secretariat $45  (22s ago)           │
└────────────────────────────────────────────────────┘
```

**Panel B — Full Field**
All 20 horses with current bid and owner emoji.

**Panel C — Leaderboard**
Biggest spender, most horses owned, biggest single bid.

**Panel D (post-lockdown) — Ownership Reveal**
Slow rotation through all 20 horses: photo, saddle cloth, owner name + emoji, winning bid. Dramatic reveal pacing (~3 sec per horse).

**Panel E (post-race) — Victory Sequence**
1. "🏆 WINNER: MAGNOLIA — Owner: Dave K 🌮 — Payout: $300"
2. "🥈 PLACE: CARRY BACK — Owner: Sarah M 💃 — Payout: $125"
3. "🥉 SHOW: LUCKY STAR — Owner: Mike T 🎺 — Payout: $75"
4. "💰 BIGGEST WINNER: Dave K 🌮 — $300 on $45 bid (6.7x return)"

---

## Integration with Existing Dashboard

### Shared Horse Data

La Subasta **reads from the existing `horses` table.** Joey enters the 20 Derby horses once into the dashboard, and La Subasta picks them up automatically.

### Race State Machine Integration

The existing dashboard drives race state (PRE-RACE → BETTING_OPEN → FINAL_CALL → AT_THE_POST → RUNNING → WINNER). La Subasta has its **own parallel state** but listens to race state events:

| Existing Race State | La Subasta State | Behavior |
|---------------------|---------------|----------|
| PRE-RACE | NOT_STARTED or OPEN | Auction runs independently all morning |
| BETTING_OPEN | OPEN | Normal bidding |
| FINAL_CALL | FINAL_HOUR (last 15 min) | Urgency UI, red flashes |
| AT_THE_POST | LOCKED | "Time to Pay Up" messaging |
| RUNNING | LOCKED | Race in progress |
| WINNER | RACE_COMPLETE → SETTLED | Payouts computed, celebration |

### Optional LED Cup Feedback

When a bid is placed on horse #3, cup #3 briefly flashes gold (matches saddle cloth). When auction locks, all cups pulse with their owner's assigned emoji color. Victory sequence pulses winning cups gold/silver/bronze (reuses existing animation framework).

This reuses the existing `RESULTS:FINALIZE` animation pattern — minimal new firmware.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/la_subasta/` | GET | Guest view |
| `/la_subasta/admin` | GET | Admin view (iPad) |
| `/la_subasta/spectator` | GET | Spectator TV view |
| `/la_subasta/api/state` | GET | Current auction state, pot, countdown |
| `/la_subasta/api/horses` | GET | All horses with current high bids |
| `/la_subasta/api/bidders` | GET | All bidders with totals (admin-only) |
| `/la_subasta/api/bid` | POST | Place a bid `{horse_id, amount, bidder_id}` |
| `/la_subasta/api/bid/undo` | POST | Undo most recent bid (10-sec window) |
| `/la_subasta/api/register` | POST | Create bidder `{name, emoji}` |
| `/la_subasta/api/check-identity` | POST | Check if name+emoji available |
| `/la_subasta/api/push/subscribe` | POST | Register browser push endpoint |
| `/la_subasta/api/admin/start` | POST | Start auction (admin) |
| `/la_subasta/api/admin/lock` | POST | Force-lock auction (admin) |
| `/la_subasta/api/admin/void` | POST | Void a bid `{bid_id, reason}` (admin) |
| `/la_subasta/api/admin/paid` | POST | Mark bidder paid `{bidder_id}` (admin) |
| `/la_subasta/api/admin/scratch` | POST | Scratch horse `{horse_id}` (admin) |
| `/la_subasta/api/admin/testing` | POST | Toggle sandbox mode (admin) |
| `/la_subasta/api/admin/export` | GET | Export all data as JSON/CSV |
| `/la_subasta/api/history` | GET | Past years' winners (year-over-year) |
| `/la_subasta/socket` | WebSocket | Real-time events |

---

## Testing / Sandbox Mode

### Auto-Simulation

Admin toggles "Testing Mode" — system spawns **12 fake bidders** with random names/emojis and runs a realistic simulation:

- Fake bidders place bids at random intervals (5-30 sec apart)
- Bid amounts within normal range ($1-$5 raises)
- Random distribution across horses (some hot, some cold)
- Max 3 horse rule enforced
- Occasional outbid wars on favorites
- Runs compressed: **15 min real time = full 8-hour auction simulated**

### Purpose

- Verify UI flow works under load
- Stress-test WebSocket updates (12 concurrent bidders)
- Confirm payout math with complex ownership
- Test lockdown, scratched-horse handling, void flow
- See what spectator TV looks like during peak activity

### Reset Between Tests

Admin button **"Reset Sandbox"** wipes all fake bidders/bids without touching real data. Real auction data lives in separate tables and is untouched.

### Pre-Event Dry Run

Recommend running a full sandbox simulation 1-2 days before the party to confirm everything works end-to-end.

---

## Year-Over-Year Data

All bid history, ownership, and payouts **persist forever** in SQLite. Enables:

### Hall of Fame Features (Spectator TV panel)

- **Past Champions:** "DDM 2026 Winner: Magnolia (Dave K 🌮)"
- **All-Time Biggest Winner:** cumulative winnings across years
- **All-Time Biggest Spender:** cumulative bids placed
- **Return Rivalries:** "Dave and Sarah have clashed in 3 straight DDMs"
- **The House Fund:** cumulative rollover for the 2027 Build Fund

### Data Table

```sql
event_years (
  year INTEGER PRIMARY KEY,
  derby_date DATE,
  total_pot REAL,
  num_bidders INTEGER,
  winner_horse_name TEXT,
  winner_owner TEXT,
  biggest_spender TEXT
);
```

Auction data tagged with `event_year` on every row for clean year-filtering.

---

## DDM Branding (Mobile-First)

### Color Palette (matches dashboard)

| Use | Hex | DDM Filament |
|-----|-----|--------------|
| Primary background | `#0a1f1a` | — |
| Secondary background | `#1a3a30` | — |
| Card background | warm beige gradient | — |
| Primary accent | `#3F8E43` | Mistletoe Green |
| Urgency / FINAL_HOUR | `#DE4343` | Scarlet Red Matte |
| Winner gold | `#FEC600` | Sunflower Yellow |
| Secondary accent | `#00B1B7` | Turquoise |
| Special highlights | `#F5547C` | Hot Pink |
| Text | `#FFFFFF` | Jade White |

### Mobile UX Principles

- **Minimum 44px tap targets** (iOS standard)
- **Big bid buttons** — thumb-friendly, high contrast
- **Minimal scrolling** — horse cards collapse/expand
- **No tiny text** — 16px minimum
- **Instant feedback** — every tap triggers visual/haptic response
- **Offline-graceful** — if wifi drops, queued bids sync on reconnect

### Protected Components

The **dot-matrix ticker strip** from the existing dashboard header should NOT be reused on mobile (too cramped). Instead, mobile uses a simplified countdown timer with the same DDM color treatment.

### Serape Stripes

Subtle horizontal serape band across the top of guest view. Scaled-down version of dashboard pattern (thinner stripes for mobile).

---

## Development Phases

### Phase 1: Core Backend (Week 1)
- [ ] SQLite schema + migrations
- [ ] Flask blueprint `/la-subasta` scaffolding
- [ ] Bidder registration + identity uniqueness
- [ ] Bid placement API with full validation
- [ ] SocketIO events for bid updates
- [ ] Auction state machine (NOT_STARTED → OPEN → FINAL_HOUR → LOCKED → RACE_COMPLETE → SETTLED)
- [ ] Payout computation logic

### Phase 2: Guest Phone UI (Week 2)
- [ ] Identity setup flow (name + emoji picker)
- [ ] Horse list view with current bids
- [ ] Bid placement with quick-bid buttons + custom input
- [ ] Portfolio view
- [ ] 10-second undo toast
- [ ] Outbid banner notifications
- [ ] DDM mobile branding

### Phase 3: Admin iPad UI (Week 3)
- [ ] Bidder list with Paid/Unpaid toggles
- [ ] Horse list with void / scratch actions
- [ ] Live bid feed
- [ ] Start auction / lock now controls
- [ ] Race results entry (shared with dashboard)
- [ ] Payout ledger

### Phase 4: Spectator TV UI (Week 4)
- [ ] Rotating panel system
- [ ] Live bid activity panel
- [ ] Full field view
- [ ] Leaderboard panel
- [ ] Ownership reveal sequence
- [ ] Victory sequence
- [ ] Hall of Fame panel (for DDM 2027+)

### Phase 5: Push Notifications (Week 4)
- [ ] Service worker registration
- [ ] Push subscription + VAPID keys
- [ ] Outbid push trigger
- [ ] Winner push trigger
- [ ] Graceful failure handling

### Phase 6: Sandbox / Testing Mode (Week 5)
- [ ] Fake bidder generation
- [ ] Auto-simulation engine
- [ ] Compressed time mode
- [ ] Reset functionality

### Phase 7: LED Integration (Optional, Week 5)
- [ ] Cup flash on bid (firmware endpoint exists)
- [ ] Owner-colored pulse at lockdown
- [ ] Victory sequence LED trigger

### Phase 8: Pre-Event Dress Rehearsal
- [ ] Full sandbox run with all phones/iPads/TV connected
- [ ] Verify push notifications on multiple devices
- [ ] Payout math verification with sandbox data
- [ ] Network stress test (20+ concurrent guests)

---

## Open Questions & Future Enhancements

### Deferred to DDM 2027

- **Syndicates:** Allow 2-3 guests to co-own a horse, split payouts
- **Photo upload:** Let guests attach a profile photo in addition to emoji
- **Audio reactions:** Guests can send emoji reactions that play sound effects on spectator TV
- **Commentary bot:** AI-generated play-by-play of auction on spectator TV ("Dave K has just destroyed Sarah M's dreams with a $42 snipe on Magnolia!")

### Open Questions

| Question | Status | Notes |
|----------|--------|-------|
| Exact auction open time (9 AM? 10 AM?) | Configurable | Default 9 AM |
| Should LED cups flash on bids? | Nice-to-have | Firmware endpoint likely already exists |
| QR code placard design | Pending | Integrate with existing DDM signage |
| Backup plan if Pi wifi fails | Pending | Cache bids in browser, sync on reconnect |
| Should admin see current pot on dashboard header? | Recommended | Yes — adds excitement |

---

## File Structure (Proposed)

```
/home/pi/DDM-Multimedia/
├── pi5/
│   ├── la_subasta/
│   │   ├── __init__.py
│   │   ├── blueprint.py           # Flask routes
│   │   ├── models.py               # SQLAlchemy or raw SQL
│   │   ├── bidding.py              # Bid validation + state
│   │   ├── payouts.py              # Payout math
│   │   ├── notifications.py        # Web Push logic
│   │   ├── sandbox.py              # Fake bidder simulation
│   │   ├── state_machine.py        # Auction states
│   │   ├── templates/
│   │   │   ├── guest.html
│   │   │   ├── admin.html
│   │   │   └── spectator.html
│   │   ├── static/
│   │   │   ├── js/
│   │   │   │   ├── guest.js
│   │   │   │   ├── admin.js
│   │   │   │   ├── spectator.js
│   │   │   │   └── service-worker.js
│   │   │   └── css/
│   │   │       ├── la-subasta-mobile.css
│   │   │       ├── la-subasta-admin.css
│   │   │       └── la-subasta-spectator.css
│   │   └── config.py               # Tunables: open time, caps, etc.
```

---

## Configuration (Modular, Per DDM Preference)

All tunables in `la_subasta/config.py`:

```python
# Timing
AUCTION_OPEN_TIME = "09:00"        # Morning of Derby
DERBY_POST_TIME = "17:57"           # Official Derby post (ET equivalent)
LOCKDOWN_MINUTES_BEFORE_POST = 15  # Hard close T-15
FINAL_HOUR_MINUTES = 15             # Last X min = urgency mode

# Bidding rules
MIN_BID = 1
MIN_RAISE = 1
MAX_RAISE = 5
MAX_HORSES_PER_BIDDER = 3
BID_UNDO_WINDOW_SECONDS = 10

# Payouts
PAYOUT_WIN_PCT = 0.60
PAYOUT_PLACE_PCT = 0.25
PAYOUT_SHOW_PCT = 0.15

# House
HOUSE_FUND_LABEL = "DDM 2027 Build Fund"

# Sandbox
SANDBOX_FAKE_BIDDER_COUNT = 12
SANDBOX_TIME_COMPRESSION = 32  # 32x real-time (8 hrs in 15 min)

# Spectator TV rotation
SPECTATOR_PANEL_SECONDS = 15
SPECTATOR_OWNERSHIP_REVEAL_PER_HORSE = 3

# Emoji palette
EMOJI_PALETTE = ["🌮","🐴","🌶️","🎺","💃","🎲","🍹","🌵",
                 "🎸","⭐","🎩","🍀","🏇","🪅","🥭","🌶️"]
```

---

## Admin Tunables & Overrides (Live-Adjustable)

Most config values in `config.py` are set-and-forget. However, **7 specific settings** are exposed in the admin UI as live-adjustable overrides. These are the values Joey is most likely to tweak year-over-year based on what he learned the previous DDM.

### The 7 Tunables

| # | Setting | Default | Range | Notes |
|---|---------|---------|-------|-------|
| 1 | **Max raise** | $5 | $1 – $50 | Caps how much higher than current bid someone can go |
| 2 | **Max horses per bidder** | 3 | 1 – 10 | Spreads ownership across crowd |
| 3 | **Min opening bid** | $1 | $1 – $20 | Floor for first bid on any horse |
| 4 | **Lockdown minutes before post** | 15 | 5 – 60 | When auction hard-closes |
| 5 | **Payout split** | 60/25/15 | Preset dropdown | Options: `60/25/15` (classic), `70/20/10` (top-heavy), `50/30/20` (flatter) |
| 6 | **House fund label** | "DDM 2027 Build Fund" | Free text, ≤40 chars | Yearly text change |
| 7 | **Auction open time** | 09:00 | HH:MM | Morning-of start time |

### Lock-When-Open Guardrail

Once `auction_state == OPEN` (or any state past OPEN), **certain settings cannot change** because doing so would retroactively invalidate guest portfolios or bidding strategy. These settings are **greyed out in the UI** with a tooltip:

> 🔒 *Locked while auction is OPEN. Lock the auction or reset to change.*

**Locked-when-open settings:**
- Max horses per bidder (would invalidate existing portfolios — e.g., changing from 3 → 2 leaves someone "over-quota")
- Payout split (people bid based on expected payout structure; changing mid-auction is unfair)
- Min/max raise limits (last-minute changes could be gamed by sniping admin)
- Auction open time (irrelevant once auction has opened)

**Unlocked when OPEN (always editable):**
- House fund label (cosmetic only)
- Lockdown minutes before post (changes the close time, but acceptable — Joey may want to extend if guests are slow)

### Storage Pattern

```python
# config.py = DEFAULTS (committed to repo, never modified at runtime)
DEFAULTS = {
    "MAX_RAISE": 5,
    "MAX_HORSES_PER_BIDDER": 3,
    "MIN_BID": 1,
    "LOCKDOWN_MINUTES_BEFORE_POST": 15,
    "PAYOUT_PRESET": "60/25/15",
    "HOUSE_FUND_LABEL": "DDM 2027 Build Fund",
    "AUCTION_OPEN_TIME": "09:00",
}
```

```sql
-- New table: admin overrides only
auction_overrides (
  setting_key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  changed_by TEXT,
  changed_at TIMESTAMP
);
```

**Resolution logic:** Load defaults from `config.py`, overlay any rows from `auction_overrides` table at startup AND on every change. "Reset to defaults" button truncates the overrides table.

### Audit Log

Every settings change is appended to a separate audit table — never overwritten:

```sql
settings_audit_log (
  id INTEGER PRIMARY KEY,
  setting_key TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT NOT NULL,
  changed_at TIMESTAMP NOT NULL,
  auction_state_at_change TEXT     -- 'NOT_STARTED', 'OPEN', etc.
);
```

**Why this matters on race day:** If something weird happens ("wait, why did Dave's bid get rejected?"), the audit log shows exactly when settings changed. Saves debugging headaches when adrenaline is high and 30 guests are watching. Audit log is admin-viewable but not exposed to guests.

### Admin UI — Settings Panel

Settings live behind a gear icon in the admin view, separate from the always-visible operational controls (Start Auction / Lock Now / Mark Paid / Void Bid / Toggle Testing).

```
┌─────────────────────────────────────────────┐
│  ⚙ LA SUBASTA — SETTINGS                    │
├─────────────────────────────────────────────┤
│  Max raise:           [ $5      ]   🔒      │
│  Max horses:          [ 3       ]   🔒      │
│  Min bid:             [ $1      ]   🔒      │
│  Lockdown at T-:      [ 15  ] min           │
│  Payout split:        [ 60/25/15  ▾ ] 🔒    │
│  House fund label:    [ DDM 2027 Build...  ]│
│  Auction open time:   [ 09:00   ]   🔒      │
│                                             │
│  🔒 = Locked while auction is OPEN          │
│                                             │
│  [ SAVE CHANGES ]      [ RESET DEFAULTS ]   │
│                                             │
│  ▸ View change history (audit log)          │
└─────────────────────────────────────────────┘
```

**Save behavior:**
- Confirmation dialog before applying changes ("Change Max Raise from $5 to $10? This affects all future bids.")
- Each save writes to `auction_overrides` table AND appends to `settings_audit_log`
- Changes apply immediately (no restart needed)
- WebSocket broadcasts a `settings_changed` event so all connected clients refresh

### Everything Else Stays in `config.py`

These remain code-only (rare to change, edit the file directly if needed):
- Min raise, bid undo window, final hour duration
- Sandbox params (fake bidder count, time compression)
- Spectator TV rotation timing
- Emoji palette
- Push notification cooldowns
- Anti-snipe behavior (currently disabled, hard stop)

---

## Revision History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | April 2026 | Initial specification document (as "Calcutta Auction") |
| 1.1 | April 2026 | Renamed to **Derby de Mayo's La Subasta**; added Naming & Branding Conventions section; updated all directory paths, URL slugs, CSS file names, and ASCII mockups |
| 1.2 | April 2026 | Added **Admin Tunables & Overrides** section: 7 live-adjustable settings, lock-when-open guardrail, override storage pattern, audit log table, settings panel UI |

---

## Appendix: Origin of the Format

**Derby de Mayo's La Subasta** is DDM's take on the classic **Calcutta Auction** format — a horse-racing pool betting tradition that originated in 19th-century Calcutta (now Kolkata), India, at the Royal Calcutta Turf Club under British colonial rule. Expats wanted a more social, higher-stakes betting format than traditional bookmaking, so they invented the auction-pool: bidders own horses, pot splits by finish. The format spread through British social clubs and became a fixture of golf member-guest tournaments throughout the 20th century. It's technically illegal gambling under most US state laws, which is why you rarely see it run officially at tournaments anymore — but at a house party with friends and pesos, nobody cares.

**La Subasta** (Spanish for "The Auction") reframes this tradition for the Derby de Mayo spirit: a Kentucky Derby / Cinco de Mayo mashup with full DDM branding, real-time mobile bidding, push notifications, spectator displays, and year-over-year hall of fame. Same social drama, zero live management, freshly bilingual.

---

*This document will evolve through Phase 1 implementation. Spec updates tracked in Revision History.*

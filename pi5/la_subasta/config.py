# la_subasta/config.py - All tunables for La Subasta
#
# Per spec § Configuration. Edit here to change auction rules without
# touching logic. All values are per-event and reload on server restart.

import os

# -----------------------------------------------------------------------------
# Admin-tunable DEFAULTS (Phase 1.5)
# -----------------------------------------------------------------------------
#
# These 7 settings are the canonical defaults. The `settings` module reads
# from auction_overrides first and falls back here. The plain module-level
# constants below (MAX_RAISE, MIN_BID, etc.) mirror these values for
# backwards compatibility — older imports keep working, but any code that
# needs the *current* value at runtime must call settings.get_setting().
DEFAULTS = {
    "MAX_RAISE": 5,
    "MAX_HORSES_PER_BIDDER": 3,
    "MIN_BID": 1,
    "LOCKDOWN_MINUTES_BEFORE_POST": 15,
    "PAYOUT_PRESET": "60/25/15",
    "HOUSE_FUND_LABEL": "DDM 2027 Build Fund",
    "AUCTION_OPEN_TIME": "09:00",
}

# Valid payout preset strings (spec § Admin Tunables). Any value not in this
# list is rejected at set_setting() time.
PAYOUT_PRESET_OPTIONS = ("60/25/15", "70/20/10", "50/30/20")

# Settings that can't be changed once the auction is running (OPEN, FINAL_HOUR,
# LOCKED, RACE_COMPLETE, SETTLED). HOUSE_FUND_LABEL + LOCKDOWN_MINUTES_BEFORE_POST
# are always editable — the first is display-only and the second affects a
# future auto-lock timer, not in-flight bid validation.
LOCKED_WHEN_OPEN = frozenset({
    "MAX_RAISE",
    "MAX_HORSES_PER_BIDDER",
    "MIN_BID",
    "PAYOUT_PRESET",
    "AUCTION_OPEN_TIME",
})

# -----------------------------------------------------------------------------
# Timing
# -----------------------------------------------------------------------------
AUCTION_OPEN_TIME = DEFAULTS["AUCTION_OPEN_TIME"]
DERBY_POST_TIME = "17:57"              # Official Derby post (local time)
LOCKDOWN_MINUTES_BEFORE_POST = DEFAULTS["LOCKDOWN_MINUTES_BEFORE_POST"]
FINAL_HOUR_MINUTES = 15                # Last X min = urgency mode

# -----------------------------------------------------------------------------
# Bidding rules
# -----------------------------------------------------------------------------
MIN_BID = DEFAULTS["MIN_BID"]
MIN_RAISE = 1                          # Code-only, not a runtime tunable
MAX_RAISE = DEFAULTS["MAX_RAISE"]
MAX_HORSES_PER_BIDDER = DEFAULTS["MAX_HORSES_PER_BIDDER"]
BID_UNDO_WINDOW_SECONDS = 10           # Code-only, not a runtime tunable

# -----------------------------------------------------------------------------
# Payouts (default — overridable via PAYOUT_PRESET at runtime)
# -----------------------------------------------------------------------------
# Kept as plain constants for backcompat. The payouts module calls
# settings.get_setting("PAYOUT_PRESET") at compute time, so these are only
# used if settings lookup fails at startup.
PAYOUT_WIN_PCT = 0.60
PAYOUT_PLACE_PCT = 0.25
PAYOUT_SHOW_PCT = 0.15

# -----------------------------------------------------------------------------
# House / rollover
# -----------------------------------------------------------------------------
HOUSE_FUND_LABEL = DEFAULTS["HOUSE_FUND_LABEL"]
# Sentinel identity for un-bid horses. Top-hat (🎩) is in the general emoji
# palette (iOS/Android/desktop push notifications all render it identically)
# and fits the auctioneer vibe better than 🏠, which ships as a few different
# house glyphs depending on platform.
HOUSE_BIDDER_NAME = "The House"
HOUSE_BIDDER_EMOJI = "🎩"
HOUSE_BIDDER_IDENTITY = f"{HOUSE_BIDDER_NAME} {HOUSE_BIDDER_EMOJI}"

# -----------------------------------------------------------------------------
# Sandbox / testing
# -----------------------------------------------------------------------------
SANDBOX_FAKE_BIDDER_COUNT = 12
SANDBOX_TIME_COMPRESSION = 32          # 32x real-time (8 hrs in 15 min)

# -----------------------------------------------------------------------------
# Spectator TV rotation
# -----------------------------------------------------------------------------
SPECTATOR_PANEL_SECONDS = 15
SPECTATOR_OWNERSHIP_REVEAL_PER_HORSE = 3

# -----------------------------------------------------------------------------
# Emoji palette (themed, 16 options)
# -----------------------------------------------------------------------------
EMOJI_PALETTE = [
    "🌮", "🐴", "🌶️", "🎺", "💃", "🎲", "🍹", "🌵",
    "🎸", "⭐", "🎩", "🍀", "🏇", "🪅", "🥭", "🎊",
]

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
# Resolve relative to pi5/data/ so it lives next to the dashboard data files.
_PI5_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PI5_DIR, "data", "la_subasta.db")

# Current event year (used for tagging rows for year-over-year history)
EVENT_YEAR = 2026

# Total horses in the field
NUM_HORSES = 20

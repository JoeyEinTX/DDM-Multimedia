"""
DDM Splash Display — Phase 1 configuration

All tunables (timing, weights, paths, server) live here. Edit and restart the
service (or the dev server) to take effect.

Phase 2 will add upload-related config (auth, content paths, etc) — keep this
file focused on Phase 1 concerns for now.
"""

# ---------------------------------------------------------------------------
# Timing — fixed durations
# Used for slides where reading-time adaptive scaling doesn't apply.
# ---------------------------------------------------------------------------
DEFAULT_SPLASH_DURATION_MS = 12000
# Phase 1.9: bumped from 15000 to 18000 to compensate for the 3-second
# cinematic intro on the countdown splash — leaves ~15s in the resting
# state where the digits actually tick. The runtime value is read from
# splash_pages.json's per-entry duration_ms; this constant is the canonical
# default for any new splash that doesn't specify its own duration.
COUNTDOWN_DURATION_MS = 18000
TRANSITION_FADE_MS = 600

# ---------------------------------------------------------------------------
# Timing — adaptive durations (Phase 1.7)
#
# Trivia dwell times scale with word count so short cards aren't held forever
# and long cards aren't cut off. All formulas are scaled by the global
# READING_SPEED_MULTIPLIER so the whole show can be slowed/sped uniformly.
#
# Per-card overrides take precedence: a card may carry an explicit
# `duration_ms` (fact) or `question_ms` / `answer_ms` (Q&A) field in
# trivia.json and that value is used verbatim instead of the adaptive calc.
#
# Fact cards: word count = headline + body. Q&A: question and answer scored
# independently. Q&A answers use a faster curve than fact cards because the
# reader has been primed by the question phase.
# ---------------------------------------------------------------------------

# Global read-speed knob — applies to fact cards AND both Q&A phases.
# 1.0 is baseline; >1.0 holds slides longer; <1.0 speeds them up.
READING_SPEED_MULTIPLIER = 1.0

# Fact cards (headline + body word count)
MIN_DURATION_MS = 8000
MAX_DURATION_MS = 18000
MS_PER_WORD = 250

# Q&A questions
QA_QUESTION_BASE_MS = 6000
QA_QUESTION_PER_WORD_MS = 200
QA_QUESTION_MAX_MS = 10000

# Q&A answers — faster than fact cards (reader primed by the question)
QA_ANSWER_MIN_MS = 6000
QA_ANSWER_PER_WORD_MS = 200
QA_ANSWER_MAX_MS = 13000

# ---------------------------------------------------------------------------
# Trivia category weights (relative; normalized internally)
# Keys MUST match the top-level keys in content/trivia.json. Categories present
# in the JSON but absent here default to weight 0 (effectively muted).
#
# Phase 1.5 rebalance: previous split was 70% racing-themed, drowning out the
# Cinco de Mayo / Mexican cultural content. This split totals to 100 (clean
# mental math) and lands roughly 50/50 racing-vs-Mexican, with attractions
# and "did you know" filler at lower weight.
# ---------------------------------------------------------------------------
TRIVIA_WEIGHTS = {
    "chapel_downs": 18,
    "carry_back": 8,
    "cinco_de_mayo": 18,
    "mexican_culture": 15,
    "mexican_horse_racing": 12,
    "classic_derby": 12,
    "did_you_know": 10,
    # ddm_attractions removed in Phase 1.8 — its three cards duplicated the
    # dedicated splash pages (la_subasta / la_quiniela / derby_dash) verbatim.
    # Sums to 93 now; internal normalization handles the actual distribution.
}

# ---------------------------------------------------------------------------
# Splash page weights — source of truth.
#
# These OVERRIDE any `weight` field set in content/splash_pages.json.
# splash_pages.json may still carry a `weight` for human reference / future
# tooling, but the runtime always reads from this dict by `id`.
#
# Keys MUST match the `id` field of an entry in splash_pages.json. Any id not
# listed here defaults to weight 0 (effectively disabled).
# ---------------------------------------------------------------------------
SPLASH_WEIGHTS = {
    "countdown":     30,
    "horse_roster":  30,   # Phase 1.14 — high weight; auto-skipped when no race data
    "ddm_brand":     15,
    "la_subasta":     8,
    "la_quiniela":    8,
    "derby_dash":     8,
}

# ---------------------------------------------------------------------------
# Playlist construction
# ---------------------------------------------------------------------------
# Approximate number of slides per shuffle cycle. The frontend will refetch
# /api/slides when the playlist runs out, so this is just "how often we
# reshuffle" not a hard cap on session length.
TARGET_PLAYLIST_LENGTH = 35

# Roughly the trivia-vs-splash mix in the playlist. 0.7 means ~70% of slides
# are trivia, ~30% are splash pages. Tune to taste.
TRIVIA_SPLASH_RATIO = 0.7

# ---------------------------------------------------------------------------
# Slide transitions (Phase 1.5)
#
# Each slide picks a random transition type from TRANSITION_TYPES, biased by
# TRANSITION_WEIGHTS. All transitions respect TRANSITION_FADE_MS and are
# CSS-driven (transform / opacity / clip-path) — no JS animation libraries.
#
# The countdown splash always uses crossfade regardless of the random pick
# (other transitions distract from the live-updating digits). Individual
# splash pages can also force a transition by adding a `force_transition`
# field to their entry in content/splash_pages.json.
#
# To disable a transition for stutter reasons, drop its weight to 0 here.
# ---------------------------------------------------------------------------
TRANSITION_TYPES = [
    "crossfade",
    "slide_left",
    "slide_up",
    "zoom_in",
    "wipe_horizontal",
]

TRANSITION_WEIGHTS = {
    "crossfade":       50,   # Most common — the "default" feel
    "slide_left":      15,   # Slide in from right
    "slide_up":        15,   # Slide up from bottom
    "zoom_in":         10,   # Zoom in from center — good for splash hero moments
    "wipe_horizontal": 10,   # Horizontal wipe — card-deal feel
}

# ---------------------------------------------------------------------------
# DDM 2026 race time (for live countdown calculation)
# Kentucky Derby 2026: Saturday May 2, post time ~6:57 PM ET.
# Stored as ISO 8601 with timezone offset; the countdown JS parses this.
# ---------------------------------------------------------------------------
DDM_2026_POST_TIME_ISO = "2026-05-02T18:57:00-04:00"

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5000
DEBUG = False

# ---------------------------------------------------------------------------
# Dashboard Pi integration (Phase 1.14)
# Live race roster is fetched from the dashboard Pi every POLL_INTERVAL_S
# (race_poller.py). If unreachable, the horse_roster splash silently drops
# from rotation until the next successful poll.
# ---------------------------------------------------------------------------
DASHBOARD_RACE_URL = "http://joeydevpi.local:5000/api/race"
RACE_DATA_STALENESS_S = 300  # 5 minutes — past this, slide shows "Last updated N min ago"

"""
DDM Splash Display — Phase 1 configuration

All tunables (timing, weights, paths, server) live here. Edit and restart the
service (or the dev server) to take effect.

Phase 2 will add upload-related config (auth, content paths, etc) — keep this
file focused on Phase 1 concerns for now.
"""

# ---------------------------------------------------------------------------
# Timing defaults (overridable per-slide in JSON)
# All values are milliseconds.
# ---------------------------------------------------------------------------
DEFAULT_FACT_DURATION_MS = 10000
DEFAULT_QA_QUESTION_MS = 8000
DEFAULT_QA_ANSWER_MS = 8000
DEFAULT_SPLASH_DURATION_MS = 12000
COUNTDOWN_DURATION_MS = 15000
TRANSITION_FADE_MS = 600

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
    "ddm_attractions": 7,
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
    "countdown": 25,
    "la_subasta": 15,
    "la_quiniela": 15,
    "derby_dash": 15,
    "ddm_brand": 10,
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

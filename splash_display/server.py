"""
DDM Splash Display — Phase 1 Flask app.

Routes:
    GET /                    → 302 redirect to /display
    GET /display             → renders the master slideshow page (kiosk URL)
    GET /api/slides          → returns a freshly shuffled JSON playlist
    GET /api/slide/<id>      → returns the rendered HTML fragment for one slide

Phase 2 will add a /upload endpoint and a back-channel into the Pi 5 dashboard.
The structure here keeps content loading and playlist building isolated so a
remote-content source can be plugged in without touching the route layer.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import Flask, abort, jsonify, redirect, render_template, url_for

import config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "content"
TRIVIA_PATH = CONTENT_DIR / "trivia.json"
SPLASH_PAGES_PATH = CONTENT_DIR / "splash_pages.json"

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "static"),
    template_folder=str(BASE_DIR / "templates"),
)

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("splash_display")


# ---------------------------------------------------------------------------
# Content loading
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_trivia() -> Dict[str, List[Dict[str, Any]]]:
    """Load and lightly validate trivia.json. Returns {category: [card,...]}."""
    raw = _load_json(TRIVIA_PATH)
    if not isinstance(raw, dict):
        raise ValueError("trivia.json must be an object keyed by category")
    return raw


def load_splash_pages() -> List[Dict[str, Any]]:
    """Load and lightly validate splash_pages.json."""
    raw = _load_json(SPLASH_PAGES_PATH)
    if not isinstance(raw, list):
        raise ValueError("splash_pages.json must be an array")
    return raw


# ---------------------------------------------------------------------------
# Playlist construction
# ---------------------------------------------------------------------------
def _trivia_card_to_slide(category: str, card: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw trivia card dict to a playlist slide entry."""
    card_type = card.get("type", "fact")
    if card_type == "qa":
        return {
            "id": f"trivia:{category}:{card.get('id', 'unknown')}",
            "type": "trivia_qa",
            "category": category,
            "data": {
                "question": card.get("question", ""),
                "answer": card.get("answer", ""),
            },
            "question_ms": int(card.get("question_ms", config.DEFAULT_QA_QUESTION_MS)),
            "answer_ms": int(card.get("answer_ms", config.DEFAULT_QA_ANSWER_MS)),
        }
    # default to fact
    return {
        "id": f"trivia:{category}:{card.get('id', 'unknown')}",
        "type": "trivia_fact",
        "category": category,
        "data": {
            "headline": card.get("headline", ""),
            "body": card.get("body", ""),
        },
        "duration_ms": int(card.get("duration_ms", config.DEFAULT_FACT_DURATION_MS)),
    }


def _splash_page_to_slide(page: Dict[str, Any]) -> Dict[str, Any]:
    slide = {
        "id": f"splash:{page['id']}",
        "type": "splash",
        "template": page["template"],
        "splash_id": page["id"],
        "duration_ms": int(page.get("duration_ms", config.DEFAULT_SPLASH_DURATION_MS)),
    }
    # Optional per-splash transition override (Phase 1.5). If set, the
    # frontend uses this transition type instead of the random pick.
    if "force_transition" in page:
        slide["force_transition"] = page["force_transition"]
    return slide


def _weighted_choice_no_immediate_repeat(
    items: List[Tuple[Any, float]], previous: Any = None
) -> Any:
    """
    Pick one item by weight. If the chosen item equals `previous`, retry up to
    a few times so back-to-back repeats are unlikely (best-effort, not a hard
    guarantee — single-item categories will still repeat eventually).
    """
    if not items:
        return None
    for _ in range(6):
        choice = random.choices(
            [it for it, _ in items], weights=[w for _, w in items], k=1
        )[0]
        if choice != previous:
            return choice
    return choice  # last attempt; accept the repeat


def _weighted_sample_no_replace(
    pool: List[Tuple[Any, float]], k: int
) -> List[Any]:
    """
    Sample up to k items from `pool` (list of (item, weight)) WITHOUT
    replacement. Each item appears at most once in the returned list.

    If the pool is exhausted before k items are picked, the pool is refilled
    once and sampling continues from a fresh shuffle. With ~60 trivia cards
    and a 35-slide target this should virtually never happen, but the refill
    keeps the playlist length stable in degenerate cases (e.g. someone
    truncates trivia.json).
    """
    selected: List[Any] = []
    if not pool or k <= 0:
        return selected

    while len(selected) < k:
        # One full pass through a freshly-shuffled copy of the pool, with
        # weighted-without-replacement picks until either we have enough or
        # the local pool is empty (then we loop and refill).
        local = list(pool)
        while local and len(selected) < k:
            weights = [w for _, w in local]
            idx = random.choices(range(len(local)), weights=weights, k=1)[0]
            selected.append(local[idx][0])
            local.pop(idx)
    return selected


def build_playlist() -> List[Dict[str, Any]]:
    """
    Build a fresh, weighted, shuffled playlist of approximately
    config.TARGET_PLAYLIST_LENGTH slides.

    Trivia cards are sampled WITHOUT replacement within a single playlist
    cycle — no individual card repeats. Category weights bias which
    categories get drawn from more often by assigning each card a per-card
    weight of (category_weight / category_size), so the sum of card weights
    in each category equals the configured category weight.

    Splash slides CAN repeat across the playlist (we want to see the
    countdown frequently), and are picked independently and then interleaved
    with the trivia run so splashes don't clump.

    Categories are read dynamically from trivia.json — no hard-coded list.
    """
    trivia_data = load_trivia()
    splash_pages = load_splash_pages()

    # --- Trivia pool: per-card weight = category_weight / category_size ----
    trivia_pool: List[Tuple[Dict[str, Any], float]] = []
    for cat, cards in trivia_data.items():
        if not cards:
            continue
        cat_weight = float(config.TRIVIA_WEIGHTS.get(cat, 0))
        if cat_weight <= 0:
            continue
        per_card_weight = cat_weight / len(cards)
        for card in cards:
            trivia_pool.append(
                (_trivia_card_to_slide(cat, card), per_card_weight)
            )

    # Splash page lookup by id (config weights are source of truth)
    splash_by_id: Dict[str, Dict[str, Any]] = {p["id"]: p for p in splash_pages}
    splash_weights: List[Tuple[str, float]] = [
        (sid, float(weight))
        for sid, weight in config.SPLASH_WEIGHTS.items()
        if sid in splash_by_id and weight > 0
    ]

    target = max(1, int(config.TARGET_PLAYLIST_LENGTH))
    trivia_count = max(1, int(round(target * config.TRIVIA_SPLASH_RATIO)))
    splash_count = max(1, target - trivia_count)

    # --- Trivia: weighted sample WITHOUT replacement -----------------------
    trivia_slides = _weighted_sample_no_replace(trivia_pool, trivia_count)

    # --- Splash: weighted choice WITH replacement (splashes can repeat) ---
    splash_slides: List[Dict[str, Any]] = []
    last_splash_id: str = ""
    for _ in range(splash_count):
        if not splash_weights:
            break
        sid = _weighted_choice_no_immediate_repeat(splash_weights, last_splash_id)
        splash_slides.append(_splash_page_to_slide(splash_by_id[sid]))
        last_splash_id = sid

    # --- interleave trivia & splash so splashes are roughly evenly spaced ---
    random.shuffle(trivia_slides)
    random.shuffle(splash_slides)
    playlist: List[Dict[str, Any]] = []
    if splash_slides:
        # Compute a target gap so splashes are evenly distributed.
        gap = max(1, len(trivia_slides) // len(splash_slides) or 1)
        splash_iter = iter(splash_slides)
        next_splash = next(splash_iter, None)
        for i, trivia in enumerate(trivia_slides):
            playlist.append(trivia)
            # Insert a splash roughly every `gap` trivia slides
            if next_splash is not None and (i + 1) % gap == 0:
                playlist.append(next_splash)
                next_splash = next(splash_iter, None)
        # Append any leftover splashes at the end
        while next_splash is not None:
            playlist.append(next_splash)
            next_splash = next(splash_iter, None)
    else:
        playlist = trivia_slides

    return playlist


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def root():
    return redirect(url_for("display"))


@app.route("/display")
def display():
    # Build the transition pool the slideshow JS will pick from. We strip out
    # any zero-weight or unknown-named entries here so the frontend doesn't
    # have to defend against bad config.
    transitions = [
        {"name": name, "weight": int(config.TRANSITION_WEIGHTS.get(name, 0))}
        for name in config.TRANSITION_TYPES
        if int(config.TRANSITION_WEIGHTS.get(name, 0)) > 0
    ]
    return render_template(
        "slideshow.html",
        transition_fade_ms=config.TRANSITION_FADE_MS,
        transitions_json=json.dumps(transitions),
    )


@app.route("/api/slides")
def api_slides():
    try:
        playlist = build_playlist()
    except Exception as exc:
        log.exception("Failed to build playlist: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify(playlist)


@app.route("/api/slide/<path:slide_id>")
def api_slide(slide_id: str):
    """
    Render a single slide as an HTML fragment.

    The slideshow frontend pre-renders every slide from the /api/slides
    payload directly in the browser, so this endpoint is a fallback /
    debugging aid (and a hook for Phase 2). slide_id is the same `id` field
    returned by /api/slides — e.g. "splash:countdown" or
    "trivia:chapel_downs:cd_carry_back".
    """
    try:
        kind, _, rest = slide_id.partition(":")
        if kind == "splash":
            splash_pages = load_splash_pages()
            page = next((p for p in splash_pages if p["id"] == rest), None)
            if page is None:
                abort(404)
            return render_template(
                page["template"],
                ddm_post_time_iso=config.DDM_2026_POST_TIME_ISO,
            )
        if kind == "trivia":
            cat, _, card_id = rest.partition(":")
            trivia = load_trivia()
            cards = trivia.get(cat, [])
            card = next((c for c in cards if c.get("id") == card_id), None)
            if card is None:
                abort(404)
            if card.get("type") == "qa":
                return render_template(
                    "trivia/qa_reveal.html",
                    question=card.get("question", ""),
                    answer=card.get("answer", ""),
                )
            return render_template(
                "trivia/fact_card.html",
                headline=card.get("headline", ""),
                body=card.get("body", ""),
            )
        abort(404)
    except Exception as exc:
        log.exception("Failed to render slide %s: %s", slide_id, exc)
        abort(500)


@app.context_processor
def inject_brand_globals():
    """Make config values available in every template."""
    return {
        "ddm_post_time_iso": config.DDM_2026_POST_TIME_ISO,
        "transition_fade_ms": config.TRANSITION_FADE_MS,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.DEBUG,
        use_reloader=config.DEBUG,
    )

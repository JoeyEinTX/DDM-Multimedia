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
    return {
        "id": f"splash:{page['id']}",
        "type": "splash",
        "template": page["template"],
        "splash_id": page["id"],
        "duration_ms": int(page.get("duration_ms", config.DEFAULT_SPLASH_DURATION_MS)),
    }


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


def build_playlist() -> List[Dict[str, Any]]:
    """
    Build a fresh, weighted, shuffled playlist of approximately
    config.TARGET_PLAYLIST_LENGTH slides.

    The playlist is interleaved: trivia and splash slides are picked
    independently and then woven together so splashes don't all clump up.
    """
    trivia_data = load_trivia()
    splash_pages = load_splash_pages()

    # Build category -> [slides] for trivia
    trivia_by_category: Dict[str, List[Dict[str, Any]]] = {
        cat: [_trivia_card_to_slide(cat, card) for card in cards]
        for cat, cards in trivia_data.items()
        if cards  # skip empty categories
    }

    # Filter category weights to only categories that actually have content.
    cat_weights = [
        (cat, float(weight))
        for cat, weight in config.TRIVIA_WEIGHTS.items()
        if cat in trivia_by_category and weight > 0
    ]

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

    # --- pick trivia slides by weighted category, then random card in cat ---
    trivia_slides: List[Dict[str, Any]] = []
    last_card_id: str = ""
    for _ in range(trivia_count):
        if not cat_weights:
            break
        category = _weighted_choice_no_immediate_repeat(cat_weights)
        cards_in_cat = trivia_by_category[category]
        # random card; try to avoid back-to-back repeat of same card id
        for _try in range(5):
            card = random.choice(cards_in_cat)
            if card["id"] != last_card_id:
                break
        trivia_slides.append(card)
        last_card_id = card["id"]

    # --- pick splash slides by weight ---
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
    return render_template(
        "slideshow.html",
        transition_fade_ms=config.TRANSITION_FADE_MS,
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

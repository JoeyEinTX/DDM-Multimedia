"""
DDM Splash Display — Phase 1 Flask app.

Routes:
    GET /                    → 302 redirect to /display
    GET /display             → renders the master slideshow page (kiosk URL)
    GET /api/slides          → returns a freshly shuffled JSON playlist
    GET /api/slide/<id>      → returns the rendered HTML fragment for one slide
    GET /api/quiniela        → La Quiniela betting model, relayed from pi5 (tokens per horse, pot, link state)
    GET /api/quiniela/stream → the same model as Server-Sent Events, on every change
    POST /api/quiniela/cmd   → forwards the body to pi5's /api/quiniela/cmd and relays its answer

Phase 2 will add a /upload endpoint and a back-channel into the Pi 5 dashboard.
The structure here keeps content loading and playlist building isolated so a
remote-content source can be plugged in without touching the route layer.
"""

from __future__ import annotations

# The splash runs as the ddm-splash service (deploy/), never alongside it: a copy
# started by hand refuses while the service is active, before the link to pi5 starts.
import service_mode
if __name__ == "__main__":
    service_mode.refuse_alongside_the_service("ddm-splash")

import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import HTTPException

import config
import quiniela

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


def static_url(filename: str) -> str:
    """url_for('static') with ?v=<the file's modification time>. Flask sends
    Last-Modified and no max-age, and Chromium then keeps a file for a tenth
    of its age without asking, so after a pull the kiosk could keep running
    the old script. A changed file is a new URL: a pull and a restart always
    serve the new code, no hard reload."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        version = 0
    return url_for("static", filename=filename, v=version)


app.jinja_env.globals["static_url"] = static_url


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
# Adaptive durations (Phase 1.7)
#
# Trivia dwell scales with word count. Per-card overrides in trivia.json
# (`duration_ms`, `question_ms`, `answer_ms`) take precedence over these.
# All formulas are scaled by config.READING_SPEED_MULTIPLIER.
# ---------------------------------------------------------------------------
def _word_count(text: str) -> int:
    return len(text.split()) if text else 0


def adaptive_duration(word_count: int) -> int:
    """Fact-card dwell. Word count is over headline + body."""
    raw = (
        config.MIN_DURATION_MS
        + word_count * config.MS_PER_WORD * config.READING_SPEED_MULTIPLIER
    )
    return max(config.MIN_DURATION_MS, min(config.MAX_DURATION_MS, int(raw)))


def adaptive_qa_question_duration(word_count: int) -> int:
    """Q&A question dwell — shown before the answer reveal."""
    raw = (
        config.QA_QUESTION_BASE_MS
        + word_count * config.QA_QUESTION_PER_WORD_MS * config.READING_SPEED_MULTIPLIER
    )
    return max(config.QA_QUESTION_BASE_MS, min(config.QA_QUESTION_MAX_MS, int(raw)))


def adaptive_qa_answer_duration(word_count: int) -> int:
    """Q&A answer dwell — faster than fact cards; reader primed by question."""
    raw = (
        config.QA_ANSWER_MIN_MS
        + word_count * config.QA_ANSWER_PER_WORD_MS * config.READING_SPEED_MULTIPLIER
    )
    return max(config.QA_ANSWER_MIN_MS, min(config.QA_ANSWER_MAX_MS, int(raw)))


# ---------------------------------------------------------------------------
# Playlist construction
# ---------------------------------------------------------------------------
def _trivia_card_to_slide(category: str, card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a raw trivia card dict to a playlist slide entry.

    Per-card timing overrides (`duration_ms`, `question_ms`, `answer_ms`)
    in trivia.json beat the adaptive calculation when present. None of the
    current cards carry these fields, but the override mechanism exists so
    individual cards can be hand-tuned without touching code.
    """
    card_type = card.get("type", "fact")
    card_id = card.get("id", "unknown")

    if card_type == "qa":
        question = card.get("question", "")
        answer = card.get("answer", "")
        question_ms = (
            int(card["question_ms"])
            if "question_ms" in card
            else adaptive_qa_question_duration(_word_count(question))
        )
        answer_ms = (
            int(card["answer_ms"])
            if "answer_ms" in card
            else adaptive_qa_answer_duration(_word_count(answer))
        )
        return {
            "id": f"trivia:{category}:{card_id}",
            "type": "trivia_qa",
            "category": category,
            "data": {"question": question, "answer": answer},
            "question_ms": question_ms,
            "answer_ms": answer_ms,
        }

    # default to fact
    headline = card.get("headline", "")
    body = card.get("body", "")
    duration_ms = (
        int(card["duration_ms"])
        if "duration_ms" in card
        else adaptive_duration(_word_count(headline) + _word_count(body))
    )
    return {
        "id": f"trivia:{category}:{card_id}",
        "type": "trivia_fact",
        "category": category,
        "data": {"headline": headline, "body": body},
        "duration_ms": duration_ms,
    }


# The slides built from the race: both read La Quiniela's model as the splash
# relays it (quiniela.board), the one home of race information on pi5. The
# countdown needs its post time; the roster ("field and odds") needs a field
# with at least one name. Each is left out of the playlist without, and the
# page fills them from the live model (static/js/quiniela_board.js's
# window.ddmQuiniela), so a post time or a name set on the admin page shows
# the next time the slide comes round.
def race_post_at(model: Dict[str, Any] | None = None) -> float | None:
    """The race's post time (unix seconds) from the relayed model, or None."""
    m = model if model is not None else quiniela.board.model()
    race = m.get("race") if isinstance(m, dict) else None
    at = race.get("post_at") if isinstance(race, dict) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return float(at)


def roster_ready(model: Dict[str, Any] | None = None) -> bool:
    """A field to list: some horse in the field (in_field) has a name."""
    m = model if model is not None else quiniela.board.model()
    horses = m.get("horses") if isinstance(m, dict) else None
    if not isinstance(horses, dict):
        return False
    return any(isinstance(h, dict) and h.get("in_field") and str(h.get("name") or "").strip()
               for h in horses.values())


def _splash_page_to_slide(page: Dict[str, Any]) -> Dict[str, Any] | None:
    """Build a playlist entry for a splash page, or None for a race slide
    with nothing to show (the countdown without a post time, the roster
    without a named field): callers leave it out of the playlist."""
    if page["id"] == "countdown" and race_post_at() is None:
        return None
    if page["id"] == "horse_roster" and not roster_ready():
        return None
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
        slide = _splash_page_to_slide(splash_by_id[sid])
        if slide is None:
            # a race slide with nothing to show (no post time, no named
            # field) — skip silently.
            continue
        splash_slides.append(slide)
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
# The looks of the La Quiniela board (config.QUINIELA_LOOK, ?look=).
QUINIELA_LOOKS = ("impact", "dots", "numbers")
DEFAULT_LOOK = "dots"          # the tote look, when config names none
_bad_looks: set = set()


def board_look(requested: Any = None) -> str:
    """The look the board page is served with: ?look= when it names one,
    else config.QUINIELA_LOOK, else DEFAULT_LOOK ("dots"). Case does not
    matter. A config value that names no look is logged once."""
    text = str(requested).strip().lower() if requested is not None else ""
    if text in QUINIELA_LOOKS:
        return text
    configured = getattr(config, "QUINIELA_LOOK", DEFAULT_LOOK)
    text = str(configured).strip().lower() if configured is not None else ""
    if text in QUINIELA_LOOKS:
        return text
    if text not in _bad_looks:
        _bad_looks.add(text)
        log.warning("config.QUINIELA_LOOK %r is not one of %s: using %s", configured,
                    ", ".join(QUINIELA_LOOKS), DEFAULT_LOOK)
    return DEFAULT_LOOK


@app.route("/")
def root():
    # The query goes along: /?look=dots is /display?look=dots
    return redirect(url_for("display", **request.args.to_dict()))


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
        quiniela_look=board_look(request.args.get("look")),
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
            # The race slides are shells the page fills from the live model.
            return render_template(page["template"])
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
                    category=cat,
                )
            return render_template(
                "trivia/fact_card.html",
                headline=card.get("headline", ""),
                body=card.get("body", ""),
                category=cat,
            )
        abort(404)
    except HTTPException:
        raise  # abort(404) above: not a render failure, pass it through unchanged
    except Exception as exc:
        log.exception("Failed to render slide %s: %s", slide_id, exc)
        abort(500)


# ---------------------------------------------------------------------------
# La Quiniela live board — pi5's betting model, relayed (quiniela.py)
# ---------------------------------------------------------------------------
@app.route("/api/quiniela")
def api_quiniela():
    """The betting model as last received from pi5: tokens per horse, pot,
    leader, recent events, link_ok (pi5's own, and pi5 heard within 5 s),
    and board_states (the race states in which the board owns the TV) so
    the frontend does not hard-code them."""
    return jsonify(quiniela.board.model())


@app.route("/api/quiniela/stream")
def api_quiniela_stream():
    """Server-Sent Events. The current model is sent immediately as the
    first `data:` event, then the full model again whenever it changes. After
    every SSE_HEARTBEAT_S of silence a `: heartbeat` comment and an
    `event: ping` go out so the browser can tell a quiet link from a dead
    connection."""
    return Response(
        quiniela.sse_events(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/quiniela/cmd", methods=["POST"])
def api_quiniela_cmd():
    """Forward the JSON body (e.g. {"cmd": "state 1"}) to pi5's
    /api/quiniela/cmd and relay its status and answer unchanged: pi5
    validates the command (single source of truth). pi5 unreachable ->
    503 {"ok": false, "error": "pi5 not reachable: ..."}."""
    status, payload = quiniela.link.forward_cmd(request.get_json(silent=True))
    return jsonify(payload), status


@app.context_processor
def inject_brand_globals():
    """Make config values available in every template."""
    return {
        "transition_fade_ms": config.TRANSITION_FADE_MS,
    }


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
# La Quiniela pi5 link: a daemon thread following pi5's /api/quiniela/stream
# (polling /api/quiniela while the stream is down) plus a 1 Hz ticker for
# link_ok. Retries harmlessly forever when pi5 is unreachable.
def _serves_requests() -> bool:
    """False in the werkzeug reloader's parent process (DEBUG = True), which
    only watches files and re-spawns the child that serves requests: if it
    started the link too, pi5 would carry a second, useless SSE client (each
    one holds a request thread open on pi5) and its log would show two
    splashes. werkzeug marks the child with WERKZEUG_RUN_MAIN=true (the test
    behind its is_running_from_reloader()). Under systemd (DEBUG = False, no
    reloader) there is one process."""
    return not config.DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true"


if _serves_requests():
    quiniela.start_link()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    service_mode.quiet_access_log()   # no line per request in the journal (DDM_ACCESS_LOG=1 brings them back)
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.DEBUG,
        use_reloader=config.DEBUG,
    )

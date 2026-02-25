# racing_routes.py - Flask API endpoints for the DDM racing data service
#
# Provides REST endpoints for managing race state, horse entries,
# auto-progression, and race results.

import logging
from flask import Blueprint, jsonify, request

from services.racing_data_service import RacingDataService, RaceState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

racing_bp = Blueprint("racing", __name__, url_prefix="/api/racing")

# Module-level service instance (set via init_racing_service)
_service: RacingDataService = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Service initialisation
# ---------------------------------------------------------------------------

def init_racing_service(socketio=None, use_mock: bool = True) -> RacingDataService:
    """
    Create and store the RacingDataService instance.

    Call this once at application startup before registering the blueprint
    so the routes have access to the service.

    Args:
        socketio: Flask-SocketIO instance (or None for headless operation).
        use_mock: If True, populate with mock horse data on init.

    Returns:
        The initialised RacingDataService instance.
    """
    global _service
    _service = RacingDataService(socketio=socketio, use_mock=use_mock)
    logger.info("Racing service initialised via init_racing_service()")
    return _service


def _get_service() -> RacingDataService:
    """Return the service instance or raise if not initialised."""
    if _service is None:
        raise RuntimeError(
            "RacingDataService not initialised. "
            "Call init_racing_service() before using racing routes."
        )
    return _service


# ---------------------------------------------------------------------------
# State name → RaceState mapping helper
# ---------------------------------------------------------------------------

_STATE_ALIASES = {
    "dormant": RaceState.DORMANT,
    "entries_loaded": RaceState.ENTRIES_LOADED,
    "entries": RaceState.ENTRIES_LOADED,
    "betting_open": RaceState.BETTING_OPEN,
    "betting": RaceState.BETTING_OPEN,
    "betting_closing": RaceState.BETTING_CLOSING,
    "closing": RaceState.BETTING_CLOSING,
    "at_the_post": RaceState.AT_THE_POST,
    "post": RaceState.AT_THE_POST,
    "running": RaceState.RUNNING,
    "race": RaceState.RUNNING,
    "finished": RaceState.FINISHED,
    "finish": RaceState.FINISHED,
    "official": RaceState.OFFICIAL,
}


def _resolve_state(name: str) -> RaceState:
    """
    Resolve a user-supplied state name to a RaceState enum value.

    Accepts the enum value itself (e.g. "BETTING_OPEN") or a short alias
    (e.g. "betting"). Case-insensitive.

    Raises:
        ValueError: If the name does not match any known state.
    """
    key = name.strip().lower()

    # Try alias map first
    state = _STATE_ALIASES.get(key)
    if state is not None:
        return state

    # Try matching enum value directly (e.g. "BETTING_OPEN")
    try:
        return RaceState(name.strip().upper())
    except ValueError:
        pass

    valid = ", ".join(sorted(_STATE_ALIASES.keys()))
    raise ValueError(
        f"Unknown state '{name}'. Valid names: {valid}"
    )


# ---------------------------------------------------------------------------
# Routes — Horses
# ---------------------------------------------------------------------------

@racing_bp.route("/horses", methods=["GET"])
def get_horses():
    """Return JSON list of all horses with current data."""
    try:
        svc = _get_service()
        return jsonify({
            "success": True,
            "horses": svc.get_horses(),
            "count": len(svc.horses),
        })
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in get_horses")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Routes — State
# ---------------------------------------------------------------------------

@racing_bp.route("/state", methods=["GET"])
def get_state():
    """Return current race state, elapsed time, and remaining time."""
    try:
        svc = _get_service()
        return jsonify({
            "success": True,
            **svc.get_state(),
        })
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in get_state")
        return jsonify({"success": False, "error": str(exc)}), 500


@racing_bp.route("/state", methods=["POST"])
def set_state():
    """
    Manually set the race state (admin override).

    Body JSON: {"state": "betting"}
    Accepts enum values or short aliases (case-insensitive).
    """
    try:
        svc = _get_service()
        data = request.get_json(silent=True) or {}
        state_name = data.get("state")

        if not state_name:
            return jsonify({
                "success": False,
                "error": "Missing required field: state",
            }), 400

        try:
            new_state = _resolve_state(state_name)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        svc.set_state(new_state)

        return jsonify({
            "success": True,
            "message": f"State set to {new_state.value}",
            **svc.get_state(),
        })

    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in set_state")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Routes — Auto Progression
# ---------------------------------------------------------------------------

@racing_bp.route("/start", methods=["POST"])
def start_auto():
    """Start the auto-progression timer through race states."""
    try:
        svc = _get_service()
        svc.start_auto_progression()
        return jsonify({
            "success": True,
            "status": "started",
            **svc.get_state(),
        })
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in start_auto")
        return jsonify({"success": False, "error": str(exc)}), 500


@racing_bp.route("/stop", methods=["POST"])
def stop_auto():
    """Stop the auto-progression timer."""
    try:
        svc = _get_service()
        svc.stop_auto_progression()
        return jsonify({
            "success": True,
            "status": "stopped",
            **svc.get_state(),
        })
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in stop_auto")
        return jsonify({"success": False, "error": str(exc)}), 500


@racing_bp.route("/reset", methods=["POST"])
def reset_race():
    """Reset to DORMANT state with fresh horses."""
    try:
        svc = _get_service()
        svc.reset()
        return jsonify({
            "success": True,
            "status": "reset",
            **svc.get_state(),
        })
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in reset_race")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Routes — Results / Winners
# ---------------------------------------------------------------------------

@racing_bp.route("/winners", methods=["POST"])
def set_winners():
    """
    Set the finishing positions and transition to OFFICIAL state.

    Body JSON: {"win": 7, "place": 3, "show": 12}
    Values are post position numbers (1–20).
    """
    try:
        svc = _get_service()
        data = request.get_json(silent=True) or {}

        win = data.get("win")
        place = data.get("place")
        show = data.get("show")

        # Validate presence
        missing = []
        if win is None:
            missing.append("win")
        if place is None:
            missing.append("place")
        if show is None:
            missing.append("show")
        if missing:
            return jsonify({
                "success": False,
                "error": f"Missing required fields: {', '.join(missing)}",
            }), 400

        # Validate types
        try:
            win = int(win)
            place = int(place)
            show = int(show)
        except (TypeError, ValueError):
            return jsonify({
                "success": False,
                "error": "win, place, and show must be integers",
            }), 400

        # Set winners (may raise ValueError for invalid positions)
        try:
            svc.set_winners(win, place, show)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        # Transition to OFFICIAL
        svc.set_state(RaceState.OFFICIAL)

        return jsonify({
            "success": True,
            "results": {
                "win": svc.get_horse(win),
                "place": svc.get_horse(place),
                "show": svc.get_horse(show),
            },
            **svc.get_state(),
        })

    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in set_winners")
        return jsonify({"success": False, "error": str(exc)}), 500


@racing_bp.route("/results", methods=["GET"])
def get_results():
    """
    Return win/place/show horse data if results have been set.

    Returns null results if no winners have been declared yet.
    """
    try:
        svc = _get_service()
        state_info = svc.get_state()

        win_pos = state_info.get("win")
        place_pos = state_info.get("place")
        show_pos = state_info.get("show")

        if win_pos is None or place_pos is None or show_pos is None:
            return jsonify({
                "success": True,
                "results": None,
                "message": "No results available",
                "state": state_info["state"],
            })

        return jsonify({
            "success": True,
            "results": {
                "win": svc.get_horse(win_pos),
                "place": svc.get_horse(place_pos),
                "show": svc.get_horse(show_pos),
            },
            "state": state_info["state"],
        })

    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Error in get_results")
        return jsonify({"success": False, "error": str(exc)}), 500

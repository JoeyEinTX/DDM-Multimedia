# guest.py - Guest / spectator routes for DDM Horse Dashboard
#
# Serves public-facing pages that do not require authentication,
# such as the full-screen tote board spectator display.

from flask import Blueprint, render_template

guest_ui = Blueprint("guest", __name__, url_prefix="/guest")


@guest_ui.route("/spectator")
def spectator():
    """Full-screen tote board display for Kentucky Derby spectator TV."""
    try:
        from routes.racing_routes import _service as racing_service
        state_data = racing_service.get_current_state_data()
        initial_state = state_data.get("state", "DORMANT")
        horses = state_data.get("horses", [])
        race_name = state_data.get("race_name", "Derby de Mayo")
    except Exception:
        initial_state = "DORMANT"
        horses = []
        race_name = "Derby de Mayo"

    return render_template(
        "spectator.html",
        initial_state=initial_state,
        horses=horses,
        race_name=race_name,
    )

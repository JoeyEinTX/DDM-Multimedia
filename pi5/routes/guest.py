# guest.py - Guest / spectator routes for DDM Horse Dashboard
#
# Serves public-facing pages that do not require authentication,
# such as the full-screen tote board spectator display.

from flask import Blueprint, render_template

guest_ui = Blueprint("guest", __name__, url_prefix="/guest")


@guest_ui.route("/spectator")
def spectator():
    """Full-screen tote board display for Kentucky Derby spectator TV."""
    return render_template("spectator.html")

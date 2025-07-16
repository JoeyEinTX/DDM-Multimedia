"""
Guest UI Blueprint - TEMPORARILY DISABLED

This will be implemented in Module 4: User Interfaces
Currently disabled to focus on Admin Dashboard development.
"""

from flask import Blueprint, jsonify
import logging

guest_ui = Blueprint('guest_ui', __name__)

@guest_ui.route('/')
def dashboard():
    """Guest dashboard - TEMPORARILY DISABLED."""
    logging.warning("Guest interface accessed but is temporarily disabled for admin development")
    return jsonify({
        "status": "disabled",
        "message": "Guest interface is temporarily disabled. Please use /admin for development.",
        "redirect": "/admin"
    }), 503

# Additional routes will be added as we build the modules

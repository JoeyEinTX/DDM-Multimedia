"""
Guest UI Blueprint - Placeholder

This will be implemented in Module 4: User Interfaces
"""

from flask import Blueprint, render_template

guest_ui = Blueprint('guest_ui', __name__)

@guest_ui.route('/')
def dashboard():
    """Guest dashboard - placeholder."""
    return "<h1>DDM Racing System - Guest Dashboard</h1><p>Coming soon in Module 4!</p>"

# Additional routes will be added as we build the modules

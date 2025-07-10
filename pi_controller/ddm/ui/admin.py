"""
Admin UI Blueprint - Placeholder

This will be implemented in Module 4: User Interfaces
"""

from flask import Blueprint, render_template

admin_ui = Blueprint('admin_ui', __name__)

@admin_ui.route('/')
def dashboard():
    """Admin dashboard - placeholder."""
    return "<h1>DDM Racing System - Admin Dashboard</h1><p>Coming soon in Module 4!</p>"

# Additional routes will be added as we build the modules

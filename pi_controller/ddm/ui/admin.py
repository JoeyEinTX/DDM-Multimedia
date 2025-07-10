"""
Admin UI Blueprint - Voice-Activated LED Control Dashboard
"""

from flask import Blueprint, render_template, session, redirect, url_for

admin_ui = Blueprint('admin_ui', __name__)

@admin_ui.route('/')
def dashboard():
    """Admin dashboard with voice prompt feature."""
    return render_template('admin/dashboard.html')

@admin_ui.route('/login')
def login():
    """Admin login page - placeholder."""
    return "<h1>Admin Login</h1><p>Login functionality coming soon!</p>"

@admin_ui.route('/logout')
def logout():
    """Admin logout."""
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_ui.login'))

# Additional routes will be added as we build the modules

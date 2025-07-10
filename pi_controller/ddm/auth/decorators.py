"""
Simple authentication decorators for DDM Racing System.
"""

from functools import wraps
from flask import session, request, jsonify


def require_admin_auth(f):
    """Decorator to require admin authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # For now, just check if admin is logged in via session
        # In production, you'd want more robust authentication
        if not session.get('admin_logged_in', False):
            return jsonify({'error': 'Admin authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function


def require_guest_auth(f):
    """Decorator to require guest authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # For now, just check if guest is logged in via session
        if not session.get('guest_logged_in', False):
            return jsonify({'error': 'Guest authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function

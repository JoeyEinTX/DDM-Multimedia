"""
Error handling utilities for DDM Racing System
"""

from flask import jsonify, request, current_app
from werkzeug.exceptions import HTTPException
import traceback
from .logger import get_logger

logger = get_logger(__name__)

class DDMError(Exception):
    """Base exception class for DDM Racing System."""
    
    def __init__(self, message, status_code=500, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}

    def to_dict(self):
        """Convert exception to dictionary for JSON response."""
        return {
            'error': self.message,
            'status_code': self.status_code,
            **self.payload
        }


class DeviceError(DDMError):
    """Exception for ESP32 device-related errors."""
    
    def __init__(self, message, device_id=None, status_code=500):
        payload = {'device_id': device_id} if device_id else {}
        super().__init__(message, status_code, payload)


class AuthenticationError(DDMError):
    """Exception for authentication-related errors."""
    
    def __init__(self, message="Authentication required", status_code=401):
        super().__init__(message, status_code)


class AuthorizationError(DDMError):
    """Exception for authorization-related errors."""
    
    def __init__(self, message="Insufficient permissions", status_code=403):
        super().__init__(message, status_code)


class ValidationError(DDMError):
    """Exception for input validation errors."""
    
    def __init__(self, message, field=None, status_code=400):
        payload = {'field': field} if field else {}
        super().__init__(message, status_code, payload)


class ModeError(DDMError):
    """Exception for mode-related errors."""
    
    def __init__(self, message, current_mode=None, status_code=400):
        payload = {'current_mode': current_mode} if current_mode else {}
        super().__init__(message, status_code, payload)


def register_error_handlers(app):
    """Register error handlers with the Flask application."""
    
    @app.errorhandler(DDMError)
    def handle_ddm_error(error):
        """Handle DDM-specific errors."""
        logger.error(f"DDM Error: {error.message}")
        response = jsonify(error.to_dict())
        response.status_code = error.status_code
        return response
    
    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        """Handle HTTP errors."""
        logger.warning(f"HTTP Error {error.code}: {error.description}")
        return jsonify({
            'error': error.description,
            'status_code': error.code
        }), error.code
    
    @app.errorhandler(Exception)
    def handle_general_error(error):
        """Handle general exceptions."""
        logger.error(f"Unexpected error: {str(error)}")
        logger.debug(traceback.format_exc())
        
        # Don't expose internal errors in production
        if current_app.debug:
            return jsonify({
                'error': str(error),
                'status_code': 500,
                'traceback': traceback.format_exc()
            }), 500
        else:
            return jsonify({
                'error': 'Internal server error',
                'status_code': 500
            }), 500
    
    @app.errorhandler(404)
    def handle_not_found(error):
        """Handle 404 errors."""
        logger.warning(f"404 Error: {request.url}")
        return jsonify({
            'error': 'Resource not found',
            'status_code': 404
        }), 404
    
    @app.errorhandler(500)
    def handle_internal_error(error):
        """Handle 500 errors."""
        logger.error(f"Internal server error: {str(error)}")
        return jsonify({
            'error': 'Internal server error',
            'status_code': 500
        }), 500

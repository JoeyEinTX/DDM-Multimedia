"""
Display API Blueprint - LED Display Control Endpoints
"""

from flask import Blueprint, jsonify
from ddm.utils.logger import get_logger

logger = get_logger(__name__)

# Create blueprint
display_bp = Blueprint('display_api', __name__)


@display_bp.route('/status', methods=['GET'])
def get_status():
    """Get display status - simple status check."""
    return jsonify({"status": "ok"})


@display_bp.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for display system."""
    return jsonify({
        "status": "healthy",
        "service": "display",
        "timestamp": "2025-07-10T00:00:00Z"
    })


# Additional display routes can be added here as needed

"""
Admin API Blueprint - Placeholder

This will be implemented in Module 2: Device Management
"""

from flask import Blueprint, jsonify
from ddm.utils.helpers import create_response

admin_api = Blueprint('admin_api', __name__)

@admin_api.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify(create_response(message="Admin API is running"))

# Additional endpoints will be added as we build the modules

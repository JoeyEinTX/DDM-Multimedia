"""
Status API Blueprint - Placeholder

This will be implemented in Module 2: Device Management
"""

from flask import Blueprint, jsonify
from ddm.utils.helpers import create_response, get_system_info

status_api = Blueprint('status_api', __name__)

@status_api.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify(create_response(message="Status API is running"))

@status_api.route('/system', methods=['GET'])
def system_status():
    """Get system status information."""
    system_info = get_system_info()
    return jsonify(create_response(data=system_info, message="System status retrieved"))

# Additional endpoints will be added as we build the modules

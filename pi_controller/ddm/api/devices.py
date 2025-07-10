"""
Devices API Blueprint - Placeholder

This will be implemented in Module 2: Device Management
"""

from flask import Blueprint, jsonify
from ddm.utils.helpers import create_response

devices_api = Blueprint('devices_api', __name__)

@devices_api.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify(create_response(message="Devices API is running"))

# Additional endpoints will be added as we build the modules

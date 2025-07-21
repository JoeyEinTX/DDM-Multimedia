"""
Display API Blueprint - LED Display Control Endpoints
"""

from flask import Blueprint, jsonify, request
from ddm.utils.logger import get_logger
from flask import current_app

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


@display_bp.route('/mode-animations', methods=['GET'])
def get_mode_animations():
    """Get available animations for a specific mode."""
    try:
        mode = request.args.get('mode')
        
        if not mode:
            return jsonify({
                'success': False,
                'error': 'Mode parameter is required'
            }), 400
        
        # Get UI_MODE_MAPPING from config
        ui_mode_mapping = current_app.config.get('UI_MODE_MAPPING', {})
        
        if mode not in ui_mode_mapping:
            return jsonify({
                'success': False,
                'error': f'Invalid mode: {mode}. Available modes: {list(ui_mode_mapping.keys())}'
            }), 400
        
        animations = ui_mode_mapping[mode]
        
        return jsonify({
            'success': True,
            'mode': mode,
            'animations': animations
        })
        
    except Exception as e:
        logger.error(f"Error getting mode animations: {e}")
        return jsonify({
            'success': False,
            'error': 'Internal server error'
        }), 500


@display_bp.route('/command', methods=['POST'])
def send_command():
    """Send animation command with mode validation."""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                'success': False,
                'error': 'No JSON data provided'
            }), 400
        
        animation = data.get('animation')
        mode = data.get('mode')
        
        if not animation:
            return jsonify({
                'success': False,
                'error': 'Animation parameter is required'
            }), 400
        
        if not mode:
            return jsonify({
                'success': False,
                'error': 'Mode parameter is required'
            }), 400
        
        # Get UI_MODE_MAPPING from config
        ui_mode_mapping = current_app.config.get('UI_MODE_MAPPING', {})
        
        # Validate mode
        if mode not in ui_mode_mapping:
            return jsonify({
                'success': False,
                'error': f'Invalid mode: {mode}. Available modes: {list(ui_mode_mapping.keys())}'
            }), 400
        
        # Validate animation for the mode
        allowed_animations = ui_mode_mapping[mode]
        if animation not in allowed_animations:
            return jsonify({
                'success': False,
                'error': f'Animation "{animation}" is not allowed for mode "{mode}". Allowed animations: {allowed_animations}'
            }), 400
        
        # Handle special case for race results with horse selections
        if animation == 'race_results' and 'results' in data:
            results = data['results']
            logger.info(f"Race results finalized: Win #{results.get('win')}, Place #{results.get('place')}, Show #{results.get('show')}")
            
            # TODO: Send results to LED controller with horse numbers
            # For now, just log the results
            
            return jsonify({
                'success': True,
                'message': f'Race results finalized with Win #{results.get("win")}, Place #{results.get("place")}, Show #{results.get("show")}',
                'animation': animation,
                'mode': mode,
                'results': results
            })
        
        # TODO: Send command to LED controller
        # For now, just log the command
        logger.info(f"Sending animation command: {animation} for mode: {mode}")
        
        return jsonify({
            'success': True,
            'message': f'Animation "{animation}" sent for mode "{mode}"',
            'animation': animation,
            'mode': mode
        })
        
    except Exception as e:
        logger.error(f"Error sending display command: {e}")
        return jsonify({
            'success': False,
            'error': 'Internal server error'
        }), 500


@display_bp.route('/test-animation', methods=['POST'])
def test_animation():
    """Send test animation to verify ESP32 connectivity and LED control."""
    try:
        data = request.get_json() or {}
        animation = data.get('animation', 'connectivity_test')
        duration = data.get('duration', 5000)
        
        logger.info(f"Sending test animation: {animation} for {duration}ms")
        
        # TODO: Send test command to ESP32 devices
        # This would typically:
        # 1. Check ESP32 connectivity
        # 2. Send test pattern (rainbow, chase, flash, etc.)
        # 3. Return success/failure based on ESP32 response
        
        # For now, simulate the test
        # In real implementation, this would communicate with ESP32
        esp32_connected = False  # This would be actual ESP32 status check
        
        if esp32_connected:
            return jsonify({
                'success': True,
                'message': f'Test animation "{animation}" sent to ESP32 devices',
                'animation': animation,
                'duration': duration,
                'devices_reached': 0  # Would be actual count
            })
        else:
            return jsonify({
                'success': False,
                'error': 'No ESP32 devices connected',
                'message': 'Test animation prepared but no devices available'
            })
        
    except Exception as e:
        logger.error(f"Error sending test animation: {e}")
        return jsonify({
            'success': False,
            'error': 'Internal server error'
        }), 500


@display_bp.route('/clear-animations', methods=['POST'])
def clear_animations():
    """Clear all animations and reset ESP32 devices."""
    try:
        logger.info("Clearing all animations and resetting ESP32 devices")
        
        # TODO: Send clear/reset command to ESP32 devices
        # This would typically:
        # 1. Send stop command to all ESP32 devices
        # 2. Reset LEDs to off/default state
        # 3. Clear any queued animations
        
        # For now, simulate the clear
        esp32_connected = False  # This would be actual ESP32 status check
        
        if esp32_connected:
            return jsonify({
                'success': True,
                'message': 'All animations cleared, ESP32 devices reset',
                'devices_reset': 0  # Would be actual count
            })
        else:
            return jsonify({
                'success': False,
                'error': 'No ESP32 devices connected',
                'message': 'Clear command prepared but no devices available'
            })
        
    except Exception as e:
        logger.error(f"Error clearing animations: {e}")
        return jsonify({
            'success': False,
            'error': 'Internal server error'
        }), 500


# Additional display routes can be added here as needed

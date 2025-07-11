"""
Race API Blueprint - Simple Race State Management Endpoints

Provides REST API endpoints for controlling performative race phases:
- Pre-Race, During Race, After Race
"""

from flask import Blueprint, request, jsonify, current_app
from ddm.models.race_state import RaceConfig, RacePhase, RaceStatus
from ddm.controllers.race_controller import RaceController
from ddm.utils.logger import get_logger
from ddm.auth import require_admin_auth
import uuid

logger = get_logger(__name__)

# Create blueprint
race_api = Blueprint('race_api', __name__)

# Global race controller instance
_race_controller: RaceController = None


def get_race_controller():
    """Get or create the global race controller instance."""
    global _race_controller
    
    if _race_controller is None:
        from ddm import socketio
        _race_controller = RaceController(socketio=socketio)
    
    return _race_controller


@race_api.route('/status', methods=['GET'])
def get_race_status():
    """Get current race status."""
    try:
        controller = get_race_controller()
        status = controller.get_race_status()
        
        return jsonify({
            'success': True,
            'race_state': status
        })
    
    except Exception as e:
        logger.error(f"Error getting race status: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@race_api.route('/create', methods=['POST'])
@require_admin_auth
def create_race():
    """Create a new race with specified configuration."""
    try:
        data = request.get_json() or {}
        
        # Create race config
        config = RaceConfig(
            race_id=str(uuid.uuid4()),
            race_name=data.get('race_name', 'Derby de Mayo Race'),
            pre_race_duration=data.get('pre_race_duration', 30),
            race_duration=data.get('race_duration', 45),
            after_race_duration=data.get('after_race_duration', 30),
            animation_speed=data.get('animation_speed', 1.0)
        )
        
        controller = get_race_controller()
        race_state = controller.create_race(config)
        
        return jsonify({
            'success': True,
            'race_id': config.race_id,
            'race_state': race_state.to_dict()
        })
    
    except Exception as e:
        logger.error(f"Error creating race: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@race_api.route('/start', methods=['POST'])
@require_admin_auth
def start_race():
    """Start the race sequence."""
    try:
        controller = get_race_controller()
        
        if controller.start_race():
            return jsonify({
                'success': True,
                'message': 'Race started successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to start race. Is a race configured?'
            }), 400
    
    except Exception as e:
        logger.error(f"Error starting race: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@race_api.route('/stop', methods=['POST'])
@require_admin_auth
def stop_race():
    """Stop the current race."""
    try:
        controller = get_race_controller()
        
        if controller.stop_race():
            return jsonify({
                'success': True,
                'message': 'Race stopped successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'No race running'
            }), 400
    
    except Exception as e:
        logger.error(f"Error stopping race: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@race_api.route('/pause', methods=['POST'])
@require_admin_auth
def pause_race():
    """Pause the current race."""
    try:
        controller = get_race_controller()
        
        if controller.pause_race():
            return jsonify({
                'success': True,
                'message': 'Race paused successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Cannot pause race'
            }), 400
    
    except Exception as e:
        logger.error(f"Error pausing race: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@race_api.route('/resume', methods=['POST'])
@require_admin_auth
def resume_race():
    """Resume a paused race."""
    try:
        controller = get_race_controller()
        
        if controller.resume_race():
            return jsonify({
                'success': True,
                'message': 'Race resumed successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Cannot resume race'
            }), 400
    
    except Exception as e:
        logger.error(f"Error resuming race: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@race_api.route('/quick-race', methods=['POST'])
@require_admin_auth
def quick_race():
    """Start a quick race with default settings."""
    try:
        # Create quick race config
        config = RaceConfig(
            race_id=str(uuid.uuid4()),
            race_name="Quick Race",
            pre_race_duration=10,
            race_duration=30,
            after_race_duration=10,
            animation_speed=1.5
        )
        
        controller = get_race_controller()
        race_state = controller.create_race(config)
        
        # Start immediately
        if controller.start_race():
            return jsonify({
                'success': True,
                'message': 'Quick race started!',
                'race_id': config.race_id,
                'race_state': race_state.to_dict()
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to start quick race'
            }), 500
    
    except Exception as e:
        logger.error(f"Error starting quick race: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

"""
Animation Generation API for DDM Racing System

This module provides API endpoints for generating racing animations using AI.
"""

from flask import Blueprint, jsonify, request
from functools import wraps
import threading
from datetime import datetime

from ddm.utils.logger import get_logger
from ddm.utils.ai_animation_engine import generate_racing_animations
from ddm.auth.decorators import require_admin_auth

logger = get_logger(__name__)

# Create the blueprint
animation_bp = Blueprint('animation', __name__, url_prefix='/api/animation')


def async_task(f):
    """Decorator to run a task asynchronously."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=f, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper


# Global generation status
generation_status = {
    'is_generating': False,
    'progress': 0,
    'total': 22,
    'current_animation': None,
    'started_at': None,
    'completed_at': None,
    'error': None
}


@async_task
def generate_animations_async(config=None):
    """Generate animations asynchronously."""
    global generation_status
    
    try:
        generation_status.update({
            'is_generating': True,
            'progress': 0,
            'current_animation': 'Initializing...',
            'started_at': datetime.now().isoformat(),
            'error': None
        })
        
        logger.info("Starting asynchronous animation generation")
        
        # Generate the complete library
        result = generate_racing_animations(config)
        
        generation_status.update({
            'is_generating': False,
            'progress': generation_status['total'],
            'current_animation': 'Complete',
            'completed_at': datetime.now().isoformat()
        })
        
        logger.info(f"Animation generation completed successfully. "
                   f"Generated {len(result.get('animations', {}))} animations")
        
    except Exception as e:
        logger.error(f"Animation generation failed: {e}")
        generation_status.update({
            'is_generating': False,
            'error': str(e),
            'completed_at': datetime.now().isoformat()
        })


@animation_bp.route('/generate', methods=['POST'])
@require_admin_auth
def generate_animations():
    """
    Start generating the complete racing animation library.
    
    Returns:
        JSON response with generation status
    """
    global generation_status
    
    if generation_status['is_generating']:
        return jsonify({
            'success': False,
            'message': 'Animation generation already in progress',
            'status': generation_status
        }), 409
    
    try:
        # Reset status
        generation_status.update({
            'is_generating': False,
            'progress': 0,
            'current_animation': None,
            'started_at': None,
            'completed_at': None,
            'error': None
        })
        
        # Start async generation
        config = request.json if request.is_json else {}
        generate_animations_async(config)
        
        return jsonify({
            'success': True,
            'message': 'Animation generation started',
            'status': generation_status
        })
        
    except Exception as e:
        logger.error(f"Failed to start animation generation: {e}")
        return jsonify({
            'success': False,
            'message': f'Failed to start generation: {str(e)}'
        }), 500


@animation_bp.route('/status', methods=['GET'])
@require_admin_auth
def get_generation_status():
    """
    Get the current status of animation generation.
    
    Returns:
        JSON response with current generation status
    """
    return jsonify({
        'success': True,
        'status': generation_status
    })


@animation_bp.route('/library', methods=['GET'])
@require_admin_auth
def get_animation_library():
    """
    Get the latest generated animation library.
    
    Returns:
        JSON response with animation library
    """
    try:
        import json
        import os
        
        # Try to load the latest animation library
        library_path = "/workspaces/DDM-Multimedia/pi_controller/ddm/static/data/racing_animations_latest.json"
        
        if not os.path.exists(library_path):
            return jsonify({
                'success': False,
                'message': 'No animation library found. Generate animations first.',
                'library': None
            }), 404
        
        with open(library_path, 'r') as f:
            library = json.load(f)
        
        return jsonify({
            'success': True,
            'message': f'Found {len(library.get("animations", {}))} animations',
            'library': library
        })
        
    except Exception as e:
        logger.error(f"Failed to load animation library: {e}")
        return jsonify({
            'success': False,
            'message': f'Failed to load library: {str(e)}'
        }), 500


@animation_bp.route('/preview/<animation_name>', methods=['GET'])
@require_admin_auth
def preview_animation(animation_name):
    """
    Get preview data for a specific animation.
    
    Args:
        animation_name: Name of the animation to preview
        
    Returns:
        JSON response with animation preview data
    """
    try:
        import json
        import os
        
        library_path = "/workspaces/DDM-Multimedia/pi_controller/ddm/static/data/racing_animations_latest.json"
        
        if not os.path.exists(library_path):
            return jsonify({
                'success': False,
                'message': 'No animation library found'
            }), 404
        
        with open(library_path, 'r') as f:
            library = json.load(f)
        
        animations = library.get('animations', {})
        
        if animation_name not in animations:
            return jsonify({
                'success': False,
                'message': f'Animation "{animation_name}" not found',
                'available': list(animations.keys())
            }), 404
        
        animation = animations[animation_name]
        
        # Create preview data (first few frames)
        preview_data = {
            'name': animation.get('name'),
            'phase': animation.get('phase'),
            'description': animation.get('description'),
            'duration_ms': animation.get('duration_ms'),
            'led_count': animation.get('led_count'),
            'intensity': animation.get('intensity'),
            'colors': animation.get('suggested_colors', []),
            'preview_frames': []
        }
        
        # Get first 10 frames for preview
        animation_data = animation.get('animation_data', {})
        frames = animation_data.get('frames', [])
        preview_data['preview_frames'] = frames[:10] if frames else []
        
        return jsonify({
            'success': True,
            'animation': preview_data
        })
        
    except Exception as e:
        logger.error(f"Failed to preview animation {animation_name}: {e}")
        return jsonify({
            'success': False,
            'message': f'Failed to preview animation: {str(e)}'
        }), 500


@animation_bp.route('/phases', methods=['GET'])
def get_racing_phases():
    """
    Get all available racing phases and their animations.
    
    Returns:
        JSON response with phase information
    """
    phases = {
        'pre_race': {
            'name': 'Pre-Race',
            'description': 'Warm-up and preparation animations',
            'animations': ['warm_up_glow', 'anticipation_pulse', 'countdown_sequence']
        },
        'betting_open': {
            'name': 'Betting Open',
            'description': 'Betting period animations',
            'animations': ['betting_open', 'betting_warning', 'betting_last_call']
        },
        'race_start': {
            'name': 'Race Start',
            'description': 'Race beginning animations',
            'animations': ['gate_burst', 'theyre_off', 'speed_burst']
        },
        'during_race': {
            'name': 'During Race',
            'description': 'Active racing animations',
            'animations': ['galloping_rhythm', 'down_the_stretch', 'neck_and_neck']
        },
        'photo_finish': {
            'name': 'Photo Finish',
            'description': 'Close finish animations',
            'animations': ['photo_finish_suspense', 'winner_revealed']
        },
        'victory': {
            'name': 'Victory',
            'description': 'Winner celebration animations',
            'animations': ['victory_celebration', 'podium_ceremony', 'winner_circle']
        },
        'celebration': {
            'name': 'Celebration',
            'description': 'Party and celebration animations',
            'animations': ['fireworks_display', 'party_lights', 'victory_lap_parade']
        },
        'cool_down': {
            'name': 'Cool Down',
            'description': 'End of event animations',
            'animations': ['gentle_fade_out', 'reset_sequence']
        }
    }
    
    return jsonify({
        'success': True,
        'phases': phases
    })


# Error handlers
@animation_bp.errorhandler(404)
def not_found(error):
    return jsonify({
        'success': False,
        'message': 'Animation endpoint not found'
    }), 404


@animation_bp.errorhandler(500)
def internal_error(error):
    return jsonify({
        'success': False,
        'message': 'Internal server error in animation API'
    }), 500

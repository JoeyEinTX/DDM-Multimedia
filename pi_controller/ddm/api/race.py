"""
Race API Blueprint - Race State Management Endpoints

Provides REST API endpoints for controlling Derby de Mayo race phases:
- Pre-Race, Betting Open, During Race, After Race
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
            'race_status': status
        })
        
    except Exception as e:
        logger.error(f"Error getting race status: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@race_api.route('/create', methods=['POST'])
@require_admin_auth
def create_race():
    """Create a new race."""
    try:
        data = request.get_json() or {}
        
        # Create race configuration
        config = RaceConfig(
            race_id=str(uuid.uuid4()),
            race_name=data.get('race_name', 'Derby de Mayo Race'),
            pre_race_duration=data.get('pre_race_duration', 30),
            betting_duration=data.get('betting_duration', 60),
            race_duration=data.get('race_duration', 45),
            after_race_duration=data.get('after_race_duration', 30),
            num_horses=data.get('num_horses', 6),
            randomness=data.get('randomness', 0.3)
        )
        
        # Create race
        controller = get_race_controller()
        race_state = controller.create_race(config)
        
        return jsonify({
            'success': True,
            'message': 'Race created successfully',
            'race_state': race_state.to_dict()
        })
        
    except Exception as e:
        logger.error(f"Error creating race: {e}")
        return jsonify({'error': str(e)}), 500


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
                'error': 'Failed to start race. Make sure a race is configured.'
            }), 400
        
    except Exception as e:
        logger.error(f"Error starting race: {e}")
        return jsonify({'error': str(e)}), 500


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
                'error': 'No race is currently running'
            }), 400
        
    except Exception as e:
        logger.error(f"Error stopping race: {e}")
        return jsonify({'error': str(e)}), 500


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
                'error': 'Cannot pause race in current state'
            }), 400
        
    except Exception as e:
        logger.error(f"Error pausing race: {e}")
        return jsonify({'error': str(e)}), 500


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
                'error': 'Cannot resume race in current state'
            }), 400
        
    except Exception as e:
        logger.error(f"Error resuming race: {e}")
        return jsonify({'error': str(e)}), 500


@race_api.route('/bet', methods=['POST'])
def place_bet():
    """Place a bet on a horse (available to guests during betting phase)."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Request body required'}), 400
        
        guest_name = data.get('guest_name')
        horse_id = data.get('horse_id')
        amount = data.get('amount', 10.0)
        
        if not guest_name or not horse_id:
            return jsonify({
                'error': 'guest_name and horse_id are required'
            }), 400
        
        controller = get_race_controller()
        
        if controller.place_bet(guest_name, horse_id, amount):
            return jsonify({
                'success': True,
                'message': f'Bet placed: ${amount} on horse {horse_id}'
            })
        else:
            return jsonify({
                'error': 'Cannot place bet. Betting may not be open or horse not found.'
            }), 400
        
    except Exception as e:
        logger.error(f"Error placing bet: {e}")
        return jsonify({'error': str(e)}), 500


@race_api.route('/horses', methods=['GET'])
def get_horses():
    """Get list of horses in the current race."""
    try:
        controller = get_race_controller()
        status = controller.get_race_status()
        
        if not status.get('race_configured'):
            return jsonify({
                'error': 'No race is currently configured'
            }), 400
        
        race_state = status['race_status']['race_state']
        horses = race_state.get('horses', [])
        
        return jsonify({
            'success': True,
            'horses': horses
        })
        
    except Exception as e:
        logger.error(f"Error getting horses: {e}")
        return jsonify({'error': str(e)}), 500


@race_api.route('/bets', methods=['GET'])
@require_admin_auth
def get_bets():
    """Get all bets placed in the current race."""
    try:
        controller = get_race_controller()
        status = controller.get_race_status()
        
        if not status.get('race_configured'):
            return jsonify({
                'error': 'No race is currently configured'
            }), 400
        
        race_state = status['race_status']['race_state']
        bets = race_state.get('bets', [])
        
        # Calculate bet totals by horse
        bet_totals = {}
        for bet in bets:
            horse_id = bet['horse_id']
            if horse_id not in bet_totals:
                bet_totals[horse_id] = {'total_amount': 0, 'bet_count': 0}
            bet_totals[horse_id]['total_amount'] += bet['amount']
            bet_totals[horse_id]['bet_count'] += 1
        
        return jsonify({
            'success': True,
            'bets': bets,
            'bet_totals': bet_totals
        })
        
    except Exception as e:
        logger.error(f"Error getting bets: {e}")
        return jsonify({'error': str(e)}), 500


@race_api.route('/results', methods=['GET'])
def get_race_results():
    """Get race results if race is completed."""
    try:
        controller = get_race_controller()
        status = controller.get_race_status()
        
        if not status.get('race_configured'):
            return jsonify({
                'error': 'No race is currently configured'
            }), 400
        
        race_state = status['race_status']['race_state']
        
        if race_state.get('race_status') != 'completed':
            return jsonify({
                'error': 'Race is not yet completed'
            }), 400
        
        return jsonify({
            'success': True,
            'winner': race_state.get('winner'),
            'results': race_state.get('race_results', []),
            'bets': race_state.get('bets', [])
        })
        
    except Exception as e:
        logger.error(f"Error getting race results: {e}")
        return jsonify({'error': str(e)}), 500


@race_api.route('/quick-race', methods=['POST'])
@require_admin_auth
def quick_race():
    """Create and start a race with default settings (for quick testing)."""
    try:
        # Create default race config
        config = RaceConfig(
            race_id=str(uuid.uuid4()),
            race_name="Quick Derby de Mayo Race",
            pre_race_duration=10,    # Shorter for testing
            betting_duration=20,     # Shorter for testing
            race_duration=15,        # Shorter for testing
            after_race_duration=10,  # Shorter for testing
            num_horses=4,           # Fewer horses for testing
            randomness=0.4
        )
        
        controller = get_race_controller()
        
        # Create and start race
        race_state = controller.create_race(config)
        
        if controller.start_race():
            return jsonify({
                'success': True,
                'message': 'Quick race started successfully',
                'race_state': race_state.to_dict()
            })
        else:
            return jsonify({
                'error': 'Failed to start quick race'
            }), 500
        
    except Exception as e:
        logger.error(f"Error starting quick race: {e}")
        return jsonify({'error': str(e)}), 500

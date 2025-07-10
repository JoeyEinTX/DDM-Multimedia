"""
OpenAI API endpoints for DDM Racing System.
"""

from flask import Blueprint, request, jsonify, current_app
from ddm.utils.openai_client import get_openai_client
from ddm.utils.logger import get_logger
from ddm.auth import require_admin_auth
import json

logger = get_logger(__name__)

# Create blueprint
openai_api = Blueprint('openai_api', __name__)


@openai_api.route('/generate-led-sequence', methods=['POST'])
@require_admin_auth
def generate_led_sequence():
    """Generate LED sequence using OpenAI."""
    try:
        data = request.get_json()
        if not data or 'description' not in data:
            return jsonify({'error': 'Description is required'}), 400
        
        description = data['description']
        
        # Get OpenAI client
        client = get_openai_client(current_app.config)
        
        # Generate LED sequence
        result = client.generate_led_sequence(description)
        
        if result:
            try:
                # Try to parse as JSON
                sequence_data = json.loads(result)
                return jsonify({
                    'success': True,
                    'sequence': sequence_data,
                    'description': description
                })
            except json.JSONDecodeError:
                # Return as text if not valid JSON
                return jsonify({
                    'success': True,
                    'sequence': result,
                    'description': description
                })
        else:
            return jsonify({'error': 'Failed to generate LED sequence'}), 500
            
    except Exception as e:
        logger.error(f"Error generating LED sequence: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@openai_api.route('/suggest-racing-mode', methods=['POST'])
@require_admin_auth
def suggest_racing_mode():
    """Suggest racing mode based on preferences."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Preferences data is required'}), 400
        
        # Get OpenAI client
        client = get_openai_client(current_app.config)
        
        # Get mode suggestion
        result = client.suggest_racing_mode(data)
        
        if result:
            try:
                # Try to parse as JSON
                mode_data = json.loads(result)
                return jsonify({
                    'success': True,
                    'suggestion': mode_data,
                    'preferences': data
                })
            except json.JSONDecodeError:
                # Return as text if not valid JSON
                return jsonify({
                    'success': True,
                    'suggestion': result,
                    'preferences': data
                })
        else:
            return jsonify({'error': 'Failed to generate racing mode suggestion'}), 500
            
    except Exception as e:
        logger.error(f"Error suggesting racing mode: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@openai_api.route('/analyze-race-data', methods=['POST'])
@require_admin_auth
def analyze_race_data():
    """Analyze race data and provide insights."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Race data is required'}), 400
        
        # Get OpenAI client
        client = get_openai_client(current_app.config)
        
        # Analyze race data
        result = client.analyze_race_data(data)
        
        if result:
            return jsonify({
                'success': True,
                'analysis': result,
                'race_data': data
            })
        else:
            return jsonify({'error': 'Failed to analyze race data'}), 500
            
    except Exception as e:
        logger.error(f"Error analyzing race data: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@openai_api.route('/chat', methods=['POST'])
@require_admin_auth
def chat():
    """General chat endpoint for OpenAI interactions."""
    try:
        data = request.get_json()
        if not data or 'messages' not in data:
            return jsonify({'error': 'Messages array is required'}), 400
        
        messages = data['messages']
        if not isinstance(messages, list) or not messages:
            return jsonify({'error': 'Messages must be a non-empty array'}), 400
        
        # Get OpenAI client
        client = get_openai_client(current_app.config)
        
        # Get chat completion
        result = client.chat_completion(
            messages=messages,
            **{k: v for k, v in data.items() if k != 'messages'}
        )
        
        if result:
            return jsonify({
                'success': True,
                'response': result,
                'messages': messages
            })
        else:
            return jsonify({'error': 'Failed to get chat response'}), 500
            
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@openai_api.route('/status', methods=['GET'])
@require_admin_auth
def openai_status():
    """Get OpenAI integration status."""
    try:
        # Check if OpenAI is configured
        api_key = current_app.config.get('OPENAI_API_KEY')
        model = current_app.config.get('OPENAI_MODEL')
        
        if not api_key:
            return jsonify({
                'configured': False,
                'message': 'OpenAI API key not configured'
            })
        
        # Try to get client
        try:
            client = get_openai_client(current_app.config)
            return jsonify({
                'configured': True,
                'model': model,
                'api_key_set': bool(api_key),
                'message': 'OpenAI integration is ready'
            })
        except Exception as e:
            return jsonify({
                'configured': False,
                'message': f'OpenAI client error: {str(e)}'
            })
            
    except Exception as e:
        logger.error(f"Error checking OpenAI status: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# main.py - Flask app entry point for DDM Cup Dashboard

from flask import Flask, render_template, jsonify, request
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG, SYSTEM_NAME, VERSION, NUM_CUPS, TOTAL_LEDS
from communication.esp32_client import esp32, check_esp32_connection

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ddm-cup-controller-2025'


@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template('dashboard.html', 
                         system_name=SYSTEM_NAME,
                         version=VERSION,
                         num_cups=NUM_CUPS,
                         total_leds=TOTAL_LEDS)


@app.route('/api/ping', methods=['GET'])
def api_ping():
    """Test connection to ESP32"""
    is_connected = check_esp32_connection()
    return jsonify({
        'success': is_connected,
        'status': 'ONLINE' if is_connected else 'OFFLINE',
        'response': esp32.get_last_response()
    })


@app.route('/api/command', methods=['POST'])
def api_command():
    """Send a command to ESP32"""
    data = request.get_json()
    command = data.get('command', '')
    
    if not command:
        return jsonify({
            'success': False,
            'error': 'No command provided'
        }), 400
    
    response = esp32.send_command(command)
    success = not response.startswith('ERROR')
    
    return jsonify({
        'success': success,
        'command': command,
        'response': response
    })


@app.route('/api/led/all_on', methods=['POST'])
def api_led_all_on():
    """Turn all LEDs on"""
    response = esp32.all_on()
    return jsonify({
        'success': not response.startswith('ERROR'),
        'response': response
    })


@app.route('/api/led/all_off', methods=['POST'])
def api_led_all_off():
    """Turn all LEDs off"""
    response = esp32.all_off()
    return jsonify({
        'success': not response.startswith('ERROR'),
        'response': response
    })


@app.route('/api/led/brightness', methods=['POST'])
def api_led_brightness():
    """Set LED brightness"""
    data = request.get_json()
    brightness = data.get('brightness', 50)
    
    response = esp32.set_brightness(brightness)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'brightness': brightness,
        'response': response
    })


@app.route('/api/led/color', methods=['POST'])
def api_led_color():
    """Set all LEDs to a color"""
    data = request.get_json()
    color = data.get('color', 'FFFFFF')
    
    response = esp32.set_color(color)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'color': color,
        'response': response
    })


@app.route('/api/led/cup', methods=['POST'])
def api_led_cup():
    """Set a specific cup to a color"""
    data = request.get_json()
    cup_number = data.get('cup', 1)
    color = data.get('color', 'FFFFFF')
    
    response = esp32.set_cup(cup_number, color)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'cup': cup_number,
        'color': color,
        'response': response
    })


@app.route('/api/animation/<anim_name>', methods=['POST'])
def api_animation(anim_name):
    """Start an animation"""
    response = esp32.start_animation(anim_name)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'animation': anim_name,
        'response': response
    })


@app.route('/api/results', methods=['POST'])
def api_results():
    """Set race results"""
    data = request.get_json()
    win = data.get('win', 1)
    place = data.get('place', 2)
    show = data.get('show', 3)
    
    # Validate unique cups
    if len(set([win, place, show])) != 3:
        return jsonify({
            'success': False,
            'error': 'Win, Place, and Show must be different cups'
        }), 400
    
    response = esp32.set_results(win, place, show)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'results': {
            'win': win,
            'place': place,
            'show': show
        },
        'response': response
    })


@app.route('/api/reset', methods=['POST'])
def api_reset():
    """Reset to idle state"""
    response = esp32.reset()
    return jsonify({
        'success': not response.startswith('ERROR'),
        'response': response
    })


@app.route('/api/status', methods=['GET'])
def api_status():
    """Get system status"""
    is_connected = esp32.is_connected()
    
    return jsonify({
        'esp32_connected': is_connected,
        'esp32_ip': esp32.ip,
        'esp32_port': esp32.port,
        'num_cups': NUM_CUPS,
        'total_leds': TOTAL_LEDS,
        'version': VERSION
    })


if __name__ == '__main__':
    print("\n" + "="*60)
    print(f"  {SYSTEM_NAME}")
    print(f"  Version {VERSION}")
    print("="*60)
    print(f"\n  Dashboard: http://{FLASK_HOST}:{FLASK_PORT}")
    print(f"  ESP32 Target: {esp32.ip}:{esp32.port}")
    print(f"  Debug Mode: {FLASK_DEBUG}\n")
    print("="*60 + "\n")
    
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

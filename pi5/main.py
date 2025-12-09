# main.py - Flask app entry point for DDM Horse Dashboard

from flask import Flask, render_template, jsonify, request
import sys
import os
import requests
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (FLASK_HOST, FLASK_PORT, FLASK_DEBUG, SYSTEM_NAME, VERSION, NUM_CUPS, TOTAL_LEDS,
                   WEATHER_API_KEY, WEATHER_LOCATION, WEATHER_CACHE_MINUTES)
from communication.esp32_client import esp32, check_esp32_connection

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ddm-horse-controller-2025'

# Weather cache
weather_cache = {
    'data': None,
    'timestamp': None
}


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
    """Set a specific horse to a color"""
    data = request.get_json()
    cup_number = data.get('cup', 1)
    color = data.get('color', 'FFFFFF')
    
    response = esp32.set_cup(cup_number, color)
    return jsonify({
        'success': not response.startswith('ERROR'),
        'horse': cup_number,
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


@app.route('/api/weather', methods=['GET'])
def api_weather():
    """Get weather forecast with caching - returns 12 hours starting from current hour"""
    global weather_cache
    
    # Check if API key is configured
    if not WEATHER_API_KEY:
        return jsonify({
            'success': False,
            'error': 'Weather API key not configured'
        }), 503
    
    # Check cache
    now = datetime.now()
    if weather_cache['data'] and weather_cache['timestamp']:
        cache_age = (now - weather_cache['timestamp']).total_seconds() / 60
        if cache_age < WEATHER_CACHE_MINUTES:
            cached_data = weather_cache['data']
            return jsonify({
                'success': True,
                'hourly': cached_data.get('hourly', []),
                'current': cached_data.get('current', {}),
                'location': cached_data.get('location', 'Dallas, TX'),
                'cached': True
            })
    
    # Fetch fresh data from WeatherAPI.com
    try:
        url = "http://api.weatherapi.com/v1/forecast.json"
        params = {
            'key': WEATHER_API_KEY,
            'q': WEATHER_LOCATION,
            'days': 2  # Request 2 days to ensure we have tomorrow's hours
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        # Get current hour from API's location time
        localtime = data.get('location', {}).get('localtime', '')  # Format: "2025-12-07 20:35"
        current_hour = int(localtime.split(' ')[1].split(':')[0]) if localtime else datetime.now().hour
        
        # Get forecast days
        forecast_days = data.get('forecast', {}).get('forecastday', [])
        
        # Collect hourly data starting from current hour
        all_hours = []
        
        # Get today's hours (from current hour onwards)
        if len(forecast_days) > 0:
            today_hours = forecast_days[0].get('hour', [])
            for hour_data in today_hours:
                hour_time_str = hour_data.get('time', '')  # Format: "2025-12-07 20:00"
                hour = int(hour_time_str.split(' ')[1].split(':')[0]) if hour_time_str else 0
                if hour >= current_hour:
                    all_hours.append(hour_data)
        
        # Get tomorrow's hours if we need more to reach 12 hours
        if len(forecast_days) > 1 and len(all_hours) < 12:
            tomorrow_hours = forecast_days[1].get('hour', [])
            needed_hours = 12 - len(all_hours)
            all_hours.extend(tomorrow_hours[:needed_hours])
        
        # Take exactly 12 hours
        hourly_data = all_hours[:12]
        
        # Extract current conditions
        current_data = data.get('current', {})
        
        # Cache the results
        weather_cache['data'] = {
            'hourly': hourly_data,
            'current': current_data,
            'location': data.get('location', {}).get('name', WEATHER_LOCATION)
        }
        weather_cache['timestamp'] = now
        
        return jsonify({
            'success': True,
            'hourly': hourly_data,
            'current': current_data,
            'location': data.get('location', {}).get('name', WEATHER_LOCATION),
            'cached': False
        })
        
    except requests.RequestException as e:
        print(f"Error fetching weather: {e}")
        # Return cached data if available, even if expired
        if weather_cache['data']:
            return jsonify({
                'success': True,
                'hourly': weather_cache['data'],
                'cached': True,
                'stale': True
            })
        
        return jsonify({
            'success': False,
            'error': 'Failed to fetch weather data'
        }), 503


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

"""Weather API for outdoor event information using National Weather Service"""

import requests
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
import os

# Create Blueprint
weather_bp = Blueprint('weather', __name__)

# Setup logging
logger = logging.getLogger(__name__)

# Weather configuration for National Weather Service
WEATHER_CONFIG = {
    # Dallas, TX 75229 - Your event location
    'latitude': 32.8668,  # Dallas, TX 75229
    'longitude': -96.7836,
    'city': 'Dallas, TX (75229)',
    
    # National Weather Service API (completely free, no API key needed!)
    'base_url': 'https://api.weather.gov',
    'user_agent': 'DDM-Racing-System/1.0 (contact@ddm-racing.com)'  # Required by NWS API
}


@weather_bp.route('/current', methods=['GET'])
def get_current_weather():
    """Get current weather conditions from National Weather Service"""
    try:
        # Step 1: Get the weather station grid point
        grid_url = f"{WEATHER_CONFIG['base_url']}/points/{WEATHER_CONFIG['latitude']},{WEATHER_CONFIG['longitude']}"
        headers = {'User-Agent': WEATHER_CONFIG['user_agent']}
        
        grid_response = requests.get(grid_url, headers=headers, timeout=10)
        grid_response.raise_for_status()
        grid_data = grid_response.json()
        
        # Step 2: Get current observations from nearest station
        stations_url = grid_data['properties']['observationStations']
        stations_response = requests.get(stations_url, headers=headers, timeout=10)
        stations_response.raise_for_status()
        stations_data = stations_response.json()
        
        if not stations_data['features']:
            raise Exception("No weather stations found")
        
        # Get observations from the first station
        station_id = stations_data['features'][0]['properties']['stationIdentifier']
        obs_url = f"{WEATHER_CONFIG['base_url']}/stations/{station_id}/observations/latest"
        
        obs_response = requests.get(obs_url, headers=headers, timeout=10)
        obs_response.raise_for_status()
        obs_data = obs_response.json()
        
        # Extract weather data
        properties = obs_data['properties']
        
        # Convert Celsius to Fahrenheit
        temp_c = properties.get('temperature', {}).get('value')
        temp_f = round((temp_c * 9/5) + 32) if temp_c else None
        
        # Convert wind speed from m/s to mph
        wind_ms = properties.get('windSpeed', {}).get('value')
        wind_mph = round(wind_ms * 2.237) if wind_ms else 0
        
        weather_data = {
            'success': True,
            'location': WEATHER_CONFIG['city'],
            'current': {
                'temperature': temp_f or 70,  # Fallback temperature
                'feels_like': temp_f or 70,  # NWS doesn't provide feels like
                'humidity': round(properties.get('relativeHumidity', {}).get('value', 50)),
                'description': properties.get('textDescription', 'Clear').lower(),
                'icon': 'clear',  # We'll use generic icons
                'wind_speed': wind_mph,
                'wind_direction': properties.get('windDirection', {}).get('value', 0),
                'pressure': round(properties.get('barometricPressure', {}).get('value', 1013) / 100, 1),  # Convert Pa to mb
                'visibility': round(properties.get('visibility', {}).get('value', 16000) / 1609.34, 1)  # Convert m to miles
            },
            'updated': datetime.now().strftime('%I:%M %p'),
            'source': 'National Weather Service'
        }
        
        return jsonify(weather_data)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"NWS API request failed: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to fetch weather data from National Weather Service',
            'message': str(e)
        }), 500
    except Exception as e:
        logger.error(f"Weather processing error: {e}")
        return jsonify({
            'success': False,
            'error': 'Weather processing failed',
            'message': str(e)
        }), 500


@weather_bp.route('/hourly', methods=['GET'])
def get_hourly_forecast():
    """Get hourly forecast from National Weather Service"""
    try:
        # Step 1: Get the weather station grid point  
        grid_url = f"{WEATHER_CONFIG['base_url']}/points/{WEATHER_CONFIG['latitude']},{WEATHER_CONFIG['longitude']}"
        headers = {'User-Agent': WEATHER_CONFIG['user_agent']}
        
        grid_response = requests.get(grid_url, headers=headers, timeout=10)
        grid_response.raise_for_status()
        grid_data = grid_response.json()
        
        # Step 2: Get hourly forecast
        forecast_url = grid_data['properties']['forecastHourly']
        forecast_response = requests.get(forecast_url, headers=headers, timeout=10)
        forecast_response.raise_for_status()
        forecast_data = forecast_response.json()
        
        # Process hourly data (next 8 hours for outdoor events)
        hourly_forecast = []
        periods = forecast_data['properties']['periods'][:8]  # Next 8 hours
        
        for period in periods:
            # Parse the time
            start_time = datetime.fromisoformat(period['startTime'].replace('Z', '+00:00'))
            
            hourly_forecast.append({
                'time': start_time.strftime('%I %p'),
                'temperature': period['temperature'],
                'feels_like': period['temperature'],  # NWS doesn't provide feels like
                'description': period['shortForecast'].lower(),
                'icon': 'generic',
                'rain_probability': 0,  # NWS provides this in detailed forecast
                'rain_amount': 0,
                'wind_speed': int(period.get('windSpeed', '0 mph').split()[0]) if period.get('windSpeed') else 0,
                'humidity': 50  # NWS doesn't provide hourly humidity
            })
        
        return jsonify({
            'success': True,
            'location': WEATHER_CONFIG['city'],
            'hourly': hourly_forecast,
            'updated': datetime.now().strftime('%I:%M %p'),
            'source': 'National Weather Service'
        })
        
    except Exception as e:
        logger.error(f"Hourly forecast error: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to fetch hourly forecast',
            'message': str(e)
        }), 500


@weather_bp.route('/alerts', methods=['GET'])
def get_weather_alerts():
    """Get weather alerts from National Weather Service"""
    try:
        # Get active weather alerts for the area
        alerts_url = f"{WEATHER_CONFIG['base_url']}/alerts/active"
        params = {
            'point': f"{WEATHER_CONFIG['latitude']},{WEATHER_CONFIG['longitude']}"
        }
        headers = {'User-Agent': WEATHER_CONFIG['user_agent']}
        
        response = requests.get(alerts_url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        alerts = []
        if 'features' in data:
            for alert in data['features'][:3]:  # Limit to 3 most recent alerts
                properties = alert['properties']
                alerts.append({
                    'title': properties.get('event', 'Weather Alert'),
                    'description': properties.get('description', '')[:200] + '...',  # Truncate
                    'severity': properties.get('severity', 'Unknown').lower(),
                    'urgency': properties.get('urgency', 'Unknown').lower(),
                    'start': properties.get('onset', ''),
                    'end': properties.get('ends', '')
                })
        
        return jsonify({
            'success': True,
            'alerts': alerts,
            'updated': datetime.now().strftime('%I:%M %p'),
            'source': 'National Weather Service'
        })
        
    except Exception as e:
        logger.error(f"Weather alerts error: {e}")
        return jsonify({
            'success': True,  # Don't fail for alerts
            'alerts': [],
            'message': 'Weather alerts unavailable'
        })

@weather_bp.route('/config', methods=['GET', 'POST'])
def weather_config():
    """Get or update weather location configuration"""
    global WEATHER_CONFIG
    
    if request.method == 'GET':
        return jsonify({
            'success': True,
            'config': {
                'latitude': WEATHER_CONFIG['latitude'],
                'longitude': WEATHER_CONFIG['longitude'],
                'city': WEATHER_CONFIG['city'],
                'api_source': 'National Weather Service (FREE)'
            }
        })
    
    # POST - update configuration
    try:
        data = request.get_json()
        
        if 'latitude' in data:
            WEATHER_CONFIG['latitude'] = float(data['latitude'])
        if 'longitude' in data:
            WEATHER_CONFIG['longitude'] = float(data['longitude'])
        if 'city' in data:
            WEATHER_CONFIG['city'] = str(data['city'])
        
        return jsonify({
            'success': True,
            'message': 'Weather location updated',
            'config': WEATHER_CONFIG
        })
        
    except Exception as e:
        logger.error(f"Weather config update error: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to update weather configuration',
            'message': str(e)
        }), 500

# Utility function to get comprehensive weather data
def get_event_weather_summary():
    """Get a comprehensive weather summary perfect for outdoor events"""
    try:
        # This combines current weather, hourly forecast, and alerts
        # Could be called by other parts of the application
        
        current_response = get_current_weather()
        hourly_response = get_hourly_forecast()
        alerts_response = get_weather_alerts()
        
        if current_response[1] != 200:  # Error in current weather
            return None
            
        current_data = current_response[0].get_json()
        hourly_data = hourly_response[0].get_json() if hourly_response[1] == 200 else {'hourly': []}
        alerts_data = alerts_response[0].get_json() if alerts_response[1] == 200 else {'alerts': []}
        
        return {
            'current': current_data.get('current', {}),
            'hourly': hourly_data.get('hourly', [])[:6],  # Next 6 hours
            'alerts': alerts_data.get('alerts', []),
            'location': current_data.get('location', 'Unknown'),
            'updated': datetime.now().strftime('%I:%M %p')
        }
        
    except Exception as e:
        logger.error(f"Weather summary error: {e}")
        return None

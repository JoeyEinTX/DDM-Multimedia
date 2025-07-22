# Weather API Configuration Guide

## Option 1: National Weather Service (FREE - US Only)
- **No signup required**: Completely free
- **Best for**: US outdoor events
- **Coverage**: All US locations
- **Setup**: Just need to set your latitude/longitude

### Setup Steps:
1. Find your event coordinates: https://www.latlong.net/
2. Update weather.py with your coordinates
3. Change API from OpenWeatherMap to NWS

## Option 2: OpenWeatherMap (FREE Tier)
- **Sign up**: https://openweathermap.org/api
- **Free tier**: 1,000 calls/day
- **Best for**: International events
- **Features**: Current weather, 5-day forecast, alerts

### Setup Steps:
1. Create account at OpenWeatherMap
2. Get your free API key from dashboard
3. Set environment variable: OPENWEATHER_API_KEY=your_key_here
4. Restart the Flask app

## Option 3: WeatherAPI.com (GENEROUS Free Tier)
- **Sign up**: https://www.weatherapi.com/
- **Free tier**: 1 million calls/month (!!)
- **Best for**: Heavy usage
- **Features**: 3-day hourly forecast, air quality

### Setup Steps:
1. Create account at WeatherAPI.com
2. Get your free API key
3. Update weather.py to use WeatherAPI instead of OpenWeatherMap

## Current Status
✅ Demo weather is working
⏳ Live weather requires API setup above

## Environment Variable Setup
Create a .env file in your project root:

```bash
# Weather API Configuration
OPENWEATHER_API_KEY=your_api_key_here

# Optional: Set your event location
WEATHER_LATITUDE=40.7128
WEATHER_LONGITUDE=-74.0060
WEATHER_CITY="Your Event Location"
```

## Which Option Should You Choose?

**For US events**: National Weather Service (no signup needed)
**For international events**: WeatherAPI.com (generous free tier)
**For simple setup**: OpenWeatherMap (most common)

Would you like me to implement any of these options?

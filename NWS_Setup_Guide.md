# National Weather Service Setup for Your Event

## ✅ What's Already Working
- **No API key needed** - completely free!
- **Weather card is live** with demo mode
- **National Weather Service integration** ready

## 🎯 Set Your Event Location

### Quick Setup
1. **Find your coordinates**: Go to https://www.latlong.net/
2. **Enter your event address** and get latitude/longitude
3. **Update the coordinates** in the weather config

### Current Location
- **Default**: Austin, TX (30.2672, -97.7431)
- **Change this** to your actual event location

### Example Event Locations
```
Dallas, TX: 32.7767, -96.7970
Houston, TX: 29.7604, -95.3698
San Antonio, TX: 29.4241, -98.4936
Fort Worth, TX: 32.7555, -97.3308
Austin, TX: 30.2672, -97.7431
```

## 🔧 How to Update Location

### Option 1: Edit the Code (Recommended)
Open `/workspaces/DDM-Multimedia/pi_controller/ddm/api/weather.py` and update lines 17-19:

```python
'latitude': YOUR_EVENT_LATITUDE,   # Replace with your coordinates
'longitude': YOUR_EVENT_LONGITUDE, # Replace with your coordinates  
'city': 'Your Event City, State',  # Replace with your city
```

### Option 2: API Call (Advanced)
```bash
curl -X POST http://localhost:5000/api/weather/config \
  -H "Content-Type: application/json" \
  -d '{
    "latitude": 32.7767,
    "longitude": -96.7970,
    "city": "Dallas, TX"
  }'
```

## 🌟 National Weather Service Features
✅ **Current conditions** (temperature, humidity, wind)
✅ **Hourly forecast** (next 8 hours)
✅ **Weather alerts** (severe weather warnings)
✅ **Government data** (very reliable)
✅ **No rate limits** (completely free)
✅ **No signup required**

## 🚀 Ready to Test
1. Update your coordinates in the weather.py file
2. Restart the Flask app
3. Refresh your dashboard
4. You should see live weather data!

The weather card will automatically switch from demo mode to live National Weather Service data once the location is set.

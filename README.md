# DDM Racing System 🏇

**Event-Focused LED Lighting Control Platform for Horse Racing-Themed Parties**

A professional Flask-based system that provides admin and guest interfaces for controlling ESP32-based LED displays in real-time. Perfect for Derby de Mayo events and horse racing parties!

## 🎯 **Project Overview**

The DDM Racing System provides:
- **Admin Touchscreen Interface** on Raspberry Pi 4B for complete control
- **Mobile Guest Interface** for limited user interaction
- **ESP32 Controllers** that drive WS2812B LED displays
- **Real-time WebSocket Communication** for synchronized animations
- **Modular Architecture** for easy expansion and maintenance

## 🚀 **Current Status: Module 1 Complete!**

### ✅ **Core Infrastructure (COMPLETE)**
- Flask application factory with configuration management
- Comprehensive logging and error handling
- Input validation and utility functions
- WebSocket support for real-time communication
- Modular API blueprint structure
- Development environment with hot reload

### 📋 **Ready for Implementation**
- **Module 2**: ESP32 Device Management
- **Module 3**: Racing Mode System
- **Module 4**: User Interfaces
- **Module 5**: Integration & Polish

## 🔗 **Live System**

**Server running on**: http://localhost:5002

### User Interfaces
- **Admin Dashboard**: `/admin/` - Full control interface
- **Guest Dashboard**: `/guest/` - Limited user interface

### API Endpoints
- **System Status**: `/api/status/system` - Server health and metrics
- **Device Management**: `/api/devices/health` - ESP32 device control
- **Admin API**: `/api/admin/health` - Admin-only endpoints
- **Guest API**: `/api/guest/health` - Guest-accessible endpoints

## 🛠️ **Quick Start**

```bash
# Navigate to project
cd DDM-Multimedia/pi_controller

# Install dependencies
pip install -r requirements.txt

# Start development server
python app.py
```

## 📈 **Next Steps**

**Ready to implement Module 2: Device Management?** 

This will add:
- ESP32 device discovery and registration
- Command sending and status monitoring
- Device health checking
- Network scanning utilities

---

**🎉 Derby de Mayo Ready!** Built for professional horse racing event control!

# oled_display.py - OLED Status Display for DDM Cup Project

from machine import Pin, I2C
import time
from config import OLED_ENABLED, OLED_SDA_PIN, OLED_SCL_PIN, OLED_WIDTH, OLED_HEIGHT

# Try to import the SSD1306 driver
try:
    from lib.ssd1306 import SSD1306_I2C
    OLED_AVAILABLE = True
except ImportError:
    OLED_AVAILABLE = False
    print("[OLED] Warning: ssd1306 library not found. OLED will be disabled.")


class OLEDDisplay:
    """Manages the OLED status display (SSD1306)"""
    
    def __init__(self):
        """Initialize the OLED display"""
        self.display = None
        self.enabled = OLED_ENABLED and OLED_AVAILABLE
        
        if not self.enabled:
            print("[OLED] Display disabled")
            return
        
        try:
            # Initialize I2C
            i2c = I2C(0, sda=Pin(OLED_SDA_PIN), scl=Pin(OLED_SCL_PIN), freq=400000)
            
            # Initialize OLED
            self.display = SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c)
            self.display.fill(0)
            self.display.show()
            
            print(f"[OLED] Initialized {OLED_WIDTH}x{OLED_HEIGHT} display on I2C0")
            
            # Display startup message
            self.show_startup()
            
        except Exception as e:
            print(f"[OLED] Error initializing display: {e}")
            self.enabled = False
            self.display = None
    
    def show_startup(self):
        """Show startup/boot message"""
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            self.display.text("DDM Cup v3", 20, 10)
            self.display.text("Booting...", 25, 30)
            self.display.show()
            time.sleep(1)
        except Exception as e:
            print(f"[OLED] Error showing startup: {e}")
    
    def update_status(self, ip="", status="", animation="", last_cmd=""):
        """
        Update the OLED display with current status
        
        Args:
            ip: IP address string (e.g., "192.168.1.100")
            status: Connection status (e.g., "Connected", "Waiting")
            animation: Current animation name
            last_cmd: Last command received
        """
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            
            # Line 1: IP Address (or "No IP")
            ip_text = ip if ip else "No IP"
            self.display.text(ip_text, 0, 0)
            
            # Line 2: Connection Status
            status_text = status if status else "Initializing"
            self.display.text(status_text, 0, 16)
            
            # Line 3: Current Animation
            if animation:
                # Truncate if too long (max ~16 chars for 128px width)
                anim_text = animation[:16]
                self.display.text(anim_text, 0, 32)
            
            # Line 4: Last Command (if space allows and height is 64)
            if last_cmd and OLED_HEIGHT >= 64:
                cmd_text = last_cmd[:16]
                self.display.text(cmd_text, 0, 48)
            
            self.display.show()
            
        except Exception as e:
            print(f"[OLED] Error updating display: {e}")
    
    def show_message(self, line1="", line2="", line3="", line4=""):
        """
        Display a custom 4-line message
        
        Args:
            line1-line4: Text strings for each line (max ~16 chars each)
        """
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            
            if line1:
                self.display.text(line1[:16], 0, 0)
            if line2:
                self.display.text(line2[:16], 0, 16)
            if line3:
                self.display.text(line3[:16], 0, 32)
            if line4 and OLED_HEIGHT >= 64:
                self.display.text(line4[:16], 0, 48)
            
            self.display.show()
            
        except Exception as e:
            print(f"[OLED] Error showing message: {e}")
    
    def show_connecting(self, ssid):
        """Show WiFi connecting message"""
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            self.display.text("Connecting to:", 0, 10)
            # Truncate SSID if too long
            ssid_text = ssid[:16] if len(ssid) <= 16 else ssid[:13] + "..."
            self.display.text(ssid_text, 0, 25)
            self.display.show()
        except Exception as e:
            print(f"[OLED] Error showing connecting: {e}")
    
    def show_connected(self, ip):
        """Show WiFi connected message with IP"""
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            self.display.text("WiFi Connected!", 0, 10)
            self.display.text("IP:", 0, 25)
            self.display.text(ip, 20, 25)
            self.display.show()
            time.sleep(2)
        except Exception as e:
            print(f"[OLED] Error showing connected: {e}")
    
    def show_error(self, error_msg):
        """Show an error message"""
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            self.display.text("ERROR:", 0, 10)
            # Split error message across lines if needed
            if len(error_msg) <= 16:
                self.display.text(error_msg, 0, 25)
            else:
                self.display.text(error_msg[:16], 0, 25)
                if len(error_msg) > 16:
                    self.display.text(error_msg[16:32], 0, 40)
            self.display.show()
        except Exception as e:
            print(f"[OLED] Error showing error: {e}")
    
    def clear(self):
        """Clear the display"""
        if not self.enabled:
            return
        
        try:
            self.display.fill(0)
            self.display.show()
        except Exception as e:
            print(f"[OLED] Error clearing display: {e}")
    
    def is_enabled(self):
        """Check if OLED is enabled and working"""
        return self.enabled

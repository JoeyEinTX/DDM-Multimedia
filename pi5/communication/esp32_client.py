# esp32_client.py - Socket client to communicate with ESP32

import socket
import threading
import time
from config import ESP32_IP, ESP32_PORT, SOCKET_TIMEOUT

# While the LED controller stays unreachable, one line says so at most this often:
# the dashboard asks it for STATUS every 5 s, and a line each time buried the journal.
DOWN_LOG_EVERY_S = 300.0


class ESP32Client:
    """Client for sending commands to ESP32 LED controller via socket"""
    
    def __init__(self, ip=ESP32_IP, port=ESP32_PORT, timeout=SOCKET_TIMEOUT, clock=time.monotonic):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.connected = False
        self.last_response = ""
        # One line when the controller becomes unreachable (with the address tried), one
        # when it answers again, and while it stays down one every DOWN_LOG_EVERY_S.
        self._clock = clock
        self._log_lock = threading.Lock()
        self._down_logged_at = None      # when the last "unreachable" line went out; None while it answers
        self._failed_since_log = 0
    
    def send_command(self, command):
        """
        Send a command to the ESP32 and return the response
        
        Args:
            command: Command string to send (e.g., "PING", "LED:ALL_ON")
        
        Returns:
            Response string from ESP32, or error message
        """
        try:
            # Create socket connection
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.ip, self.port))
                
                # Send command (add newline)
                message = command + '\n'
                sock.sendall(message.encode('utf-8'))
                
                # Receive response
                response = sock.recv(1024).decode('utf-8').strip()
                
                self.last_response = response
                self.connected = True
                
                self._answered()
                print(f"[ESP32] Sent: {command} | Received: {response}")
                return response
        
        except socket.timeout:
            self.connected = False
            error = "ERROR:TIMEOUT"
            self._failed(command, "timed out")
            return error
        
        except ConnectionRefusedError:
            self.connected = False
            error = "ERROR:CONNECTION_REFUSED"
            self._failed(command, "connection refused")
            return error
        
        except Exception as e:
            self.connected = False
            error = f"ERROR:EXCEPTION:{str(e)}"
            self._failed(command, str(e))
            return error
    
    def _failed(self, command, reason):
        """A command that got no answer: say so once, then at most every DOWN_LOG_EVERY_S."""
        with self._log_lock:
            now = self._clock()
            if self._down_logged_at is None:
                print(f"[ESP32] LED controller unreachable at {self.ip}:{self.port} ({reason}, sending {command})")
            elif now - self._down_logged_at >= DOWN_LOG_EVERY_S:
                print(f"[ESP32] LED controller still unreachable at {self.ip}:{self.port} ({reason}); "
                      f"{self._failed_since_log} more command(s) failed since the last line")
            else:
                self._failed_since_log += 1
                return
            self._down_logged_at = now
            self._failed_since_log = 0

    def _answered(self):
        """A command answered: one line if the controller had been reported unreachable."""
        with self._log_lock:
            if self._down_logged_at is not None:
                print(f"[ESP32] LED controller reachable again at {self.ip}:{self.port}")
                self._down_logged_at = None
                self._failed_since_log = 0

    def ping(self):
        """Test connection to ESP32"""
        response = self.send_command("PING")
        return response == "PONG"
    
    def reset(self):
        """Reset ESP32 to idle state"""
        return self.send_command("RESET")
    
    def all_on(self):
        """Turn all LEDs on (white)"""
        return self.send_command("LED:ALL_ON")
    
    def all_off(self):
        """Turn all LEDs off"""
        return self.send_command("LED:ALL_OFF")
    
    def set_brightness(self, brightness):
        """
        Set LED brightness
        
        Args:
            brightness: Brightness level 0-100
        """
        brightness = max(0, min(100, int(brightness)))
        return self.send_command(f"LED:BRIGHTNESS:{brightness}")
    
    def set_color(self, hex_color):
        """
        Set all LEDs to a specific color
        
        Args:
            hex_color: Hex color string (e.g., "FFD700" or "#FFD700")
        """
        hex_color = hex_color.lstrip('#')
        return self.send_command(f"LED:COLOR:{hex_color}")
    
    def set_cup(self, cup_number, hex_color):
        """
        Set a specific horse to a color

        Args:
            cup_number: Horse number (1-20)
            hex_color: Hex color string
        """
        hex_color = hex_color.lstrip('#')
        return self.send_command(f"LED:CUP:{cup_number}:{hex_color}")
    
    def start_animation(self, anim_name):
        """
        Start an animation
        
        Args:
            anim_name: Animation name (IDLE, WELCOME, BETTING_60, etc.)
        """
        return self.send_command(f"ANIM:{anim_name.upper()}")
    
    def is_connected(self):
        """Check if ESP32 is reachable"""
        return self.connected
    
    def get_last_response(self):
        """Get the last response received from ESP32"""
        return self.last_response

    def get_power_status(self):
        """Get software-estimated power draw from ESP32"""
        response = self.send_command("STATUS")
        if response.startswith("STATUS:"):
            parts = response.split(":")
            if len(parts) == 4:
                return {
                    'current_ma': int(parts[1]),
                    'peak_ma': int(parts[2]),
                    'min_ma': int(parts[3])
                }
        return None


# Global client instance
esp32 = ESP32Client()


# Convenience functions for Flask routes
def send_to_esp32(command):
    """Send command to ESP32 and return response"""
    return esp32.send_command(command)


def check_esp32_connection():
    """Check if ESP32 is connected"""
    return esp32.ping()

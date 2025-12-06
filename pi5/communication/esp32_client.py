# esp32_client.py - Socket client to communicate with ESP32

import socket
import time
from config import ESP32_IP, ESP32_PORT, SOCKET_TIMEOUT


class ESP32Client:
    """Client for sending commands to ESP32 LED controller via socket"""
    
    def __init__(self, ip=ESP32_IP, port=ESP32_PORT, timeout=SOCKET_TIMEOUT):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.connected = False
        self.last_response = ""
    
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
                
                print(f"[ESP32] Sent: {command} | Received: {response}")
                return response
        
        except socket.timeout:
            self.connected = False
            error = "ERROR:TIMEOUT"
            print(f"[ESP32] Timeout sending command: {command}")
            return error
        
        except ConnectionRefusedError:
            self.connected = False
            error = "ERROR:CONNECTION_REFUSED"
            print(f"[ESP32] Connection refused to {self.ip}:{self.port}")
            return error
        
        except Exception as e:
            self.connected = False
            error = f"ERROR:EXCEPTION:{str(e)}"
            print(f"[ESP32] Exception: {e}")
            return error
    
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
    
    def set_results(self, win_cup, place_cup, show_cup):
        """
        Set race results
        
        Args:
            win_cup: Horse number for Win position (1-20)
            place_cup: Horse number for Place position (1-20)
            show_cup: Horse number for Show position (1-20)
        """
        command = f"RESULTS:W:{win_cup}:P:{place_cup}:S:{show_cup}"
        return self.send_command(command)
    
    def is_connected(self):
        """Check if ESP32 is reachable"""
        return self.connected
    
    def get_last_response(self):
        """Get the last response received from ESP32"""
        return self.last_response


# Global client instance
esp32 = ESP32Client()


# Convenience functions for Flask routes
def send_to_esp32(command):
    """Send command to ESP32 and return response"""
    return esp32.send_command(command)


def check_esp32_connection():
    """Check if ESP32 is connected"""
    return esp32.ping()

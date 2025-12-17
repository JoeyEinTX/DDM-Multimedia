# tote_client.py - HTTP client to communicate with Interstate75 LED tote board

import requests
import time
from urllib.parse import urlencode


class ToteClient:
    """Client for sending commands to Interstate75 LED tote board via HTTP"""
    
    def __init__(self, ip, port=80, timeout=2.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.connected = False
        self.last_response = ""
        self.base_url = f"http://{ip}:{port}"
    
    def _send(self, endpoint, params=None):
        """
        Send HTTP GET request to tote board
        
        Args:
            endpoint: URL endpoint (e.g., "/status", "/tote")
            params: Dictionary of query parameters
        
        Returns:
            Response text, or error message
        """
        try:
            url = f"{self.base_url}{endpoint}"
            
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            self.last_response = response.text
            self.connected = True
            
            param_str = f"?{urlencode(params)}" if params else ""
            print(f"[TOTE] GET {endpoint}{param_str} | Status: {response.status_code}")
            
            return response.text
        
        except requests.Timeout:
            self.connected = False
            error = "ERROR:TIMEOUT"
            print(f"[TOTE] Timeout on {endpoint}")
            return error
        
        except requests.ConnectionError:
            self.connected = False
            error = "ERROR:CONNECTION_REFUSED"
            print(f"[TOTE] Connection error to {self.base_url}")
            return error
        
        except requests.HTTPError as e:
            self.connected = False
            error = f"ERROR:HTTP:{e.response.status_code}"
            print(f"[TOTE] HTTP error: {e}")
            return error
        
        except Exception as e:
            self.connected = False
            error = f"ERROR:EXCEPTION:{str(e)}"
            print(f"[TOTE] Exception: {e}")
            return error
    
    def ping(self):
        """Test connection to tote board"""
        response = self._send("/status")
        return not response.startswith("ERROR")
    
    @property
    def is_connected(self):
        """Check if tote board is reachable"""
        return self.connected
    
    def welcome(self):
        """Display welcome animation"""
        return self._send("/tote", {"mode": "welcome"})
    
    def scroll(self, message, color="gold", speed=3):
        """
        Display scrolling message
        
        Args:
            message: Text to scroll
            color: Color name (e.g., "gold", "green", "red")
            speed: Scroll speed 1-10 (default 3)
        """
        params = {
            "mode": "scroll",
            "msg": message,
            "color": color,
            "speed": speed
        }
        return self._send("/tote", params)
    
    def betting_open(self, minutes=60):
        """
        Display betting countdown
        
        Args:
            minutes: Minutes until betting closes (default 60)
        """
        params = {
            "mode": "betting",
            "mins": minutes
        }
        return self._send("/tote", params)
    
    def final_call(self):
        """Display final call for bets"""
        return self._send("/tote", {"mode": "finalcall"})
    
    def race_start(self):
        """Display race start animation"""
        return self._send("/tote", {"mode": "racestart"})
    
    def official(self, win, place, show):
        """
        Display official race results
        
        Args:
            win: Horse number for Win position (1-20)
            place: Horse number for Place position (1-20)
            show: Horse number for Show position (1-20)
        """
        params = {
            "mode": "official",
            "win": win,
            "place": place,
            "show": show
        }
        return self._send("/tote", params)
    
    def test(self):
        """Display test pattern"""
        return self._send("/tote", {"mode": "test"})
    
    def stop(self):
        """Stop all animations and clear display"""
        return self._send("/tote", {"mode": "stop"})
    
    def get_last_response(self):
        """Get the last response received from tote board"""
        return self.last_response


# Global client instance
_tote_client = None


def init_tote_client(ip, port=80, timeout=2.0):
    """Initialize the global tote client instance"""
    global _tote_client
    _tote_client = ToteClient(ip, port, timeout)
    return _tote_client


def get_tote_client():
    """Get the global tote client instance"""
    return _tote_client

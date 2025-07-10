"""
Helper utilities for DDM Racing System
"""

import time
import socket
import subprocess
import psutil
from typing import Dict, Any, List, Optional
from functools import wraps
from .logger import get_logger

logger = get_logger(__name__)

def timing_decorator(func):
    """
    Decorator to measure and log function execution time.
    
    Args:
        func: Function to decorate
    
    Returns:
        Wrapped function that logs execution time
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        
        duration_ms = (end_time - start_time) * 1000
        logger.debug(f"{func.__name__} executed in {duration_ms:.2f}ms")
        
        return result
    return wrapper


def get_local_ip() -> str:
    """
    Get the local IP address of the Pi.
    
    Returns:
        str: Local IP address
    """
    try:
        # Connect to a remote address to determine local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def is_port_open(host: str, port: int, timeout: int = 3) -> bool:
    """
    Check if a port is open on a host.
    
    Args:
        host: Host address
        port: Port number
        timeout: Connection timeout in seconds
    
    Returns:
        bool: True if port is open, False otherwise
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            result = s.connect_ex((host, port))
            return result == 0
    except Exception:
        return False


def scan_network_for_devices(network_range: str, port: int, timeout: int = 1) -> List[str]:
    """
    Scan network range for devices responding on a specific port.
    
    Args:
        network_range: Network range (e.g., "192.168.1.0/24")
        port: Port to scan
        timeout: Connection timeout in seconds
    
    Returns:
        List[str]: List of responding IP addresses
    """
    import ipaddress
    
    devices = []
    
    try:
        network = ipaddress.ip_network(network_range, strict=False)
        
        for ip in network.hosts():
            ip_str = str(ip)
            if is_port_open(ip_str, port, timeout):
                devices.append(ip_str)
                logger.debug(f"Found device at {ip_str}:{port}")
    
    except Exception as e:
        logger.error(f"Error scanning network {network_range}: {e}")
    
    return devices


def get_system_info() -> Dict[str, Any]:
    """
    Get system information.
    
    Returns:
        Dict[str, Any]: System information
    """
    try:
        return {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
            'temperature': get_cpu_temperature(),
            'uptime': get_uptime(),
            'local_ip': get_local_ip()
        }
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        return {}


def get_cpu_temperature() -> Optional[float]:
    """
    Get CPU temperature (Raspberry Pi specific).
    
    Returns:
        Optional[float]: CPU temperature in Celsius, None if not available
    """
    try:
        # Try Raspberry Pi method first
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = float(f.read().strip()) / 1000.0
            return temp
    except (FileNotFoundError, ValueError):
        try:
            # Try alternative method
            result = subprocess.run(['vcgencmd', 'measure_temp'], 
                                  capture_output=True, text=True)
            if result.returncode == 0:
                temp_str = result.stdout.strip()
                temp = float(temp_str.split('=')[1].split('\'')[0])
                return temp
        except Exception:
            pass
    
    return None


def get_uptime() -> Optional[str]:
    """
    Get system uptime.
    
    Returns:
        Optional[str]: Uptime string, None if not available
    """
    try:
        uptime_seconds = time.time() - psutil.boot_time()
        uptime_hours = int(uptime_seconds // 3600)
        uptime_minutes = int((uptime_seconds % 3600) // 60)
        
        return f"{uptime_hours}h {uptime_minutes}m"
    except Exception:
        return None


def hex_to_rgb(hex_color: str) -> tuple:
    """
    Convert hex color to RGB tuple.
    
    Args:
        hex_color: Hex color string (e.g., "#FF0000" or "FF0000")
    
    Returns:
        tuple: RGB tuple (r, g, b)
    """
    # Remove # if present
    if hex_color.startswith('#'):
        hex_color = hex_color[1:]
    
    # Convert to RGB
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    
    return (r, g, b)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """
    Convert RGB tuple to hex color.
    
    Args:
        r: Red value (0-255)
        g: Green value (0-255)
        b: Blue value (0-255)
    
    Returns:
        str: Hex color string with # prefix
    """
    return f"#{r:02X}{g:02X}{b:02X}"


def clamp(value: float, min_val: float, max_val: float) -> float:
    """
    Clamp a value between minimum and maximum.
    
    Args:
        value: Value to clamp
        min_val: Minimum value
        max_val: Maximum value
    
    Returns:
        float: Clamped value
    """
    return max(min_val, min(value, max_val))


def format_duration(duration_ms: int) -> str:
    """
    Format duration in milliseconds to human-readable string.
    
    Args:
        duration_ms: Duration in milliseconds
    
    Returns:
        str: Formatted duration string
    """
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    elif duration_ms < 60000:
        return f"{duration_ms / 1000:.1f}s"
    else:
        minutes = duration_ms // 60000
        seconds = (duration_ms % 60000) // 1000
        return f"{minutes}m {seconds}s"


def create_response(data: Any = None, message: str = None, status: str = "success") -> Dict[str, Any]:
    """
    Create a standardized API response.
    
    Args:
        data: Response data
        message: Response message
        status: Response status ("success" or "error")
    
    Returns:
        Dict[str, Any]: Standardized response
    """
    response = {
        "status": status,
        "timestamp": time.time()
    }
    
    if data is not None:
        response["data"] = data
    
    if message is not None:
        response["message"] = message
    
    return response

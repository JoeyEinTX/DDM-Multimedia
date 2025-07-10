"""
Input validation utilities for DDM Racing System
"""

import re
from typing import Dict, Any, List, Optional
from .errors import ValidationError

def validate_required_fields(data: Dict[str, Any], required_fields: List[str]) -> None:
    """
    Validate that all required fields are present in the data.
    
    Args:
        data: Dictionary to validate
        required_fields: List of required field names
    
    Raises:
        ValidationError: If any required field is missing
    """
    missing_fields = [field for field in required_fields if field not in data or data[field] is None]
    
    if missing_fields:
        raise ValidationError(
            f"Missing required fields: {', '.join(missing_fields)}",
            field=missing_fields[0]
        )


def validate_device_id(device_id: str) -> str:
    """
    Validate ESP32 device ID format.
    
    Args:
        device_id: Device ID to validate
    
    Returns:
        str: Validated device ID
    
    Raises:
        ValidationError: If device ID is invalid
    """
    if not device_id or not isinstance(device_id, str):
        raise ValidationError("Device ID must be a non-empty string", field="device_id")
    
    # Device ID should be alphanumeric with hyphens/underscores
    if not re.match(r'^[a-zA-Z0-9_-]+$', device_id):
        raise ValidationError(
            "Device ID must contain only alphanumeric characters, hyphens, and underscores",
            field="device_id"
        )
    
    if len(device_id) > 32:
        raise ValidationError("Device ID must be 32 characters or less", field="device_id")
    
    return device_id


def validate_ip_address(ip_address: str) -> str:
    """
    Validate IP address format.
    
    Args:
        ip_address: IP address to validate
    
    Returns:
        str: Validated IP address
    
    Raises:
        ValidationError: If IP address is invalid
    """
    if not ip_address or not isinstance(ip_address, str):
        raise ValidationError("IP address must be a non-empty string", field="ip_address")
    
    # Simple IP address validation
    ip_pattern = r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
    if not re.match(ip_pattern, ip_address):
        raise ValidationError("Invalid IP address format", field="ip_address")
    
    return ip_address


def validate_color_value(color: str) -> str:
    """
    Validate color value (hex format).
    
    Args:
        color: Color value to validate (e.g., "#FF0000" or "FF0000")
    
    Returns:
        str: Validated color value with # prefix
    
    Raises:
        ValidationError: If color value is invalid
    """
    if not color or not isinstance(color, str):
        raise ValidationError("Color must be a non-empty string", field="color")
    
    # Remove # if present
    if color.startswith('#'):
        color = color[1:]
    
    # Validate hex format
    if not re.match(r'^[0-9A-Fa-f]{6}$', color):
        raise ValidationError("Color must be a valid hex color (e.g., FF0000)", field="color")
    
    return f"#{color.upper()}"


def validate_brightness(brightness: int) -> int:
    """
    Validate brightness value (0-255).
    
    Args:
        brightness: Brightness value to validate
    
    Returns:
        int: Validated brightness value
    
    Raises:
        ValidationError: If brightness is invalid
    """
    if not isinstance(brightness, int):
        raise ValidationError("Brightness must be an integer", field="brightness")
    
    if brightness < 0 or brightness > 255:
        raise ValidationError("Brightness must be between 0 and 255", field="brightness")
    
    return brightness


def validate_led_count(led_count: int) -> int:
    """
    Validate LED count value.
    
    Args:
        led_count: LED count to validate
    
    Returns:
        int: Validated LED count
    
    Raises:
        ValidationError: If LED count is invalid
    """
    if not isinstance(led_count, int):
        raise ValidationError("LED count must be an integer", field="led_count")
    
    if led_count < 1 or led_count > 1000:
        raise ValidationError("LED count must be between 1 and 1000", field="led_count")
    
    return led_count


def validate_speed(speed: int) -> int:
    """
    Validate animation speed value.
    
    Args:
        speed: Speed value to validate (1-100)
    
    Returns:
        int: Validated speed value
    
    Raises:
        ValidationError: If speed is invalid
    """
    if not isinstance(speed, int):
        raise ValidationError("Speed must be an integer", field="speed")
    
    if speed < 1 or speed > 100:
        raise ValidationError("Speed must be between 1 and 100", field="speed")
    
    return speed


def validate_duration(duration: int) -> int:
    """
    Validate duration value in milliseconds.
    
    Args:
        duration: Duration to validate
    
    Returns:
        int: Validated duration
    
    Raises:
        ValidationError: If duration is invalid
    """
    if not isinstance(duration, int):
        raise ValidationError("Duration must be an integer", field="duration")
    
    if duration < 0:
        raise ValidationError("Duration must be non-negative", field="duration")
    
    if duration > 3600000:  # 1 hour max
        raise ValidationError("Duration must be less than 1 hour", field="duration")
    
    return duration


def validate_mode_name(mode_name: str) -> str:
    """
    Validate mode name.
    
    Args:
        mode_name: Mode name to validate
    
    Returns:
        str: Validated mode name
    
    Raises:
        ValidationError: If mode name is invalid
    """
    if not mode_name or not isinstance(mode_name, str):
        raise ValidationError("Mode name must be a non-empty string", field="mode_name")
    
    # Mode names should be alphanumeric with underscores
    if not re.match(r'^[a-zA-Z0-9_]+$', mode_name):
        raise ValidationError(
            "Mode name must contain only alphanumeric characters and underscores",
            field="mode_name"
        )
    
    if len(mode_name) > 50:
        raise ValidationError("Mode name must be 50 characters or less", field="mode_name")
    
    return mode_name.lower()


def validate_command_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate command payload structure.
    
    Args:
        payload: Command payload to validate
    
    Returns:
        Dict[str, Any]: Validated payload
    
    Raises:
        ValidationError: If payload is invalid
    """
    if not isinstance(payload, dict):
        raise ValidationError("Payload must be a dictionary")
    
    # Validate required fields
    validate_required_fields(payload, ['command'])
    
    # Validate command name
    payload['command'] = validate_mode_name(payload['command'])
    
    # Validate parameters if present
    if 'parameters' in payload:
        if not isinstance(payload['parameters'], dict):
            raise ValidationError("Parameters must be a dictionary", field="parameters")
    
    return payload

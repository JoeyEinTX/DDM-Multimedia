"""
ESP32 Device Models for DDM Racing System

This module defines the data structures for managing ESP32 LED controllers,
including device registration, status tracking, and command management.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum
import json

from ddm.utils.logger import get_logger
from ddm.utils.validators import validate_device_id, validate_ip_address

logger = get_logger(__name__)


class DeviceStatus(Enum):
    """ESP32 device status enumeration."""
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"
    UPDATING = "updating"


class DeviceType(Enum):
    """ESP32 device type enumeration."""
    CUP_BASE = "cup_base"
    MATRIX_WALL = "matrix_wall"
    STRIP_CONTROLLER = "strip_controller"
    UNKNOWN = "unknown"


@dataclass
class DeviceCommand:
    """
    Represents a command to be sent to an ESP32 device.
    
    Attributes:
        command: Command name (e.g., "chaos_mode", "solid_color")
        parameters: Command parameters dictionary
        timestamp: When the command was created
        timeout: Command timeout in seconds
        retries: Number of retry attempts
        priority: Command priority (0-10, higher is more important)
    """
    command: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    timeout: int = 5
    retries: int = 3
    priority: int = 5
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert command to dictionary for JSON serialization."""
        return {
            'command': self.command,
            'parameters': self.parameters,
            'timestamp': self.timestamp.isoformat(),
            'timeout': self.timeout,
            'retries': self.retries,
            'priority': self.priority
        }
    
    def to_json(self) -> str:
        """Convert command to JSON string."""
        return json.dumps(self.to_dict())


@dataclass
class Device:
    """
    Represents an ESP32 LED controller device.
    
    This class manages device information, status, and command queuing
    for ESP32 controllers in the DDM Racing System.
    """
    device_id: str
    ip_address: str
    device_type: DeviceType = DeviceType.UNKNOWN
    status: DeviceStatus = DeviceStatus.UNKNOWN
    
    # Device specifications
    led_count: int = 50
    led_type: str = "WS2812B"
    color_order: str = "RGB"
    max_brightness: int = 255
    current_brightness: int = 128
    
    # Network and communication
    port: int = 80
    firmware_version: str = "unknown"
    mac_address: str = "unknown"
    
    # Status and health
    last_seen: Optional[datetime] = None
    last_command: Optional[str] = None
    last_response: Optional[str] = None
    uptime: int = 0
    temperature: Optional[float] = None
    free_memory: int = 0
    
    # Command queue and performance
    command_queue: List[DeviceCommand] = field(default_factory=list)
    commands_sent: int = 0
    commands_successful: int = 0
    commands_failed: int = 0
    avg_response_time: float = 0.0
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Validate device data after initialization."""
        self.device_id = validate_device_id(self.device_id)
        self.ip_address = validate_ip_address(self.ip_address)
        logger.info(f"Created device: {self.device_id} at {self.ip_address}")
    
    @property
    def is_online(self) -> bool:
        """Check if device is currently online."""
        return self.status == DeviceStatus.ONLINE
    
    @property
    def is_responsive(self) -> bool:
        """Check if device is responsive (online and not busy)."""
        return self.status in [DeviceStatus.ONLINE]
    
    @property
    def success_rate(self) -> float:
        """Calculate command success rate percentage."""
        if self.commands_sent == 0:
            return 0.0
        return (self.commands_successful / self.commands_sent) * 100
    
    @property
    def base_url(self) -> str:
        """Get the base URL for HTTP requests to this device."""
        return f"http://{self.ip_address}:{self.port}"
    
    def update_status(self, status: DeviceStatus, message: str = None):
        """
        Update device status and log the change.
        
        Args:
            status: New device status
            message: Optional status message
        """
        old_status = self.status
        self.status = status
        self.updated_at = datetime.now()
        
        if status == DeviceStatus.ONLINE:
            self.last_seen = datetime.now()
        
        logger.info(f"Device {self.device_id} status: {old_status} -> {status}")
        if message:
            logger.debug(f"Device {self.device_id} status message: {message}")
    
    def add_command(self, command: DeviceCommand):
        """
        Add a command to the device's command queue.
        
        Args:
            command: Command to add to queue
        """
        self.command_queue.append(command)
        # Sort queue by priority (highest first)
        self.command_queue.sort(key=lambda cmd: cmd.priority, reverse=True)
        logger.debug(f"Added command {command.command} to device {self.device_id} queue")
    
    def get_next_command(self) -> Optional[DeviceCommand]:
        """
        Get the next command from the queue.
        
        Returns:
            Next command to execute or None if queue is empty
        """
        if self.command_queue:
            return self.command_queue.pop(0)
        return None
    
    def clear_command_queue(self):
        """Clear all commands from the queue."""
        cleared_count = len(self.command_queue)
        self.command_queue.clear()
        logger.info(f"Cleared {cleared_count} commands from device {self.device_id} queue")
    
    def record_command_result(self, success: bool, response_time: float = None):
        """
        Record the result of a command execution.
        
        Args:
            success: Whether the command was successful
            response_time: Command response time in seconds
        """
        self.commands_sent += 1
        
        if success:
            self.commands_successful += 1
        else:
            self.commands_failed += 1
        
        # Update average response time
        if response_time is not None:
            if self.avg_response_time == 0:
                self.avg_response_time = response_time
            else:
                # Simple exponential moving average
                self.avg_response_time = (self.avg_response_time * 0.8) + (response_time * 0.2)
        
        self.updated_at = datetime.now()
        logger.debug(f"Device {self.device_id} command result: {'SUCCESS' if success else 'FAILED'}")
    
    def update_health_info(self, uptime: int = None, temperature: float = None, 
                          free_memory: int = None, firmware_version: str = None):
        """
        Update device health and system information.
        
        Args:
            uptime: Device uptime in seconds
            temperature: Device temperature in Celsius
            free_memory: Free memory in bytes
            firmware_version: Firmware version string
        """
        if uptime is not None:
            self.uptime = uptime
        if temperature is not None:
            self.temperature = temperature
        if free_memory is not None:
            self.free_memory = free_memory
        if firmware_version is not None:
            self.firmware_version = firmware_version
        
        self.updated_at = datetime.now()
        logger.debug(f"Updated health info for device {self.device_id}")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert device to dictionary for JSON serialization."""
        return {
            'device_id': self.device_id,
            'ip_address': self.ip_address,
            'device_type': self.device_type.value,
            'status': self.status.value,
            'led_count': self.led_count,
            'led_type': self.led_type,
            'color_order': self.color_order,
            'max_brightness': self.max_brightness,
            'current_brightness': self.current_brightness,
            'port': self.port,
            'firmware_version': self.firmware_version,
            'mac_address': self.mac_address,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'last_command': self.last_command,
            'last_response': self.last_response,
            'uptime': self.uptime,
            'temperature': self.temperature,
            'free_memory': self.free_memory,
            'commands_sent': self.commands_sent,
            'commands_successful': self.commands_successful,
            'commands_failed': self.commands_failed,
            'success_rate': self.success_rate,
            'avg_response_time': self.avg_response_time,
            'queue_length': len(self.command_queue),
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat()
        }
    
    def to_json(self) -> str:
        """Convert device to JSON string."""
        return json.dumps(self.to_dict())
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Device':
        """
        Create device instance from dictionary.
        
        Args:
            data: Device data dictionary
            
        Returns:
            Device instance
        """
        # Convert enum values
        device_type = DeviceType(data.get('device_type', DeviceType.UNKNOWN.value))
        status = DeviceStatus(data.get('status', DeviceStatus.UNKNOWN.value))
        
        # Convert datetime strings
        created_at = datetime.fromisoformat(data['created_at']) if data.get('created_at') else datetime.now()
        updated_at = datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else datetime.now()
        last_seen = datetime.fromisoformat(data['last_seen']) if data.get('last_seen') else None
        
        device = cls(
            device_id=data['device_id'],
            ip_address=data['ip_address'],
            device_type=device_type,
            status=status,
            led_count=data.get('led_count', 50),
            led_type=data.get('led_type', 'WS2812B'),
            color_order=data.get('color_order', 'RGB'),
            max_brightness=data.get('max_brightness', 255),
            current_brightness=data.get('current_brightness', 128),
            port=data.get('port', 80),
            firmware_version=data.get('firmware_version', 'unknown'),
            mac_address=data.get('mac_address', 'unknown'),
            last_seen=last_seen,
            last_command=data.get('last_command'),
            last_response=data.get('last_response'),
            uptime=data.get('uptime', 0),
            temperature=data.get('temperature'),
            free_memory=data.get('free_memory', 0),
            commands_sent=data.get('commands_sent', 0),
            commands_successful=data.get('commands_successful', 0),
            commands_failed=data.get('commands_failed', 0),
            avg_response_time=data.get('avg_response_time', 0.0),
            created_at=created_at,
            updated_at=updated_at
        )
        
        return device
    
    def __str__(self) -> str:
        """String representation of device."""
        return f"Device({self.device_id}@{self.ip_address}:{self.port}, {self.status.value})"
    
    def __repr__(self) -> str:
        """Developer representation of device."""
        return (f"Device(device_id='{self.device_id}', ip_address='{self.ip_address}', "
                f"status={self.status}, type={self.device_type})")

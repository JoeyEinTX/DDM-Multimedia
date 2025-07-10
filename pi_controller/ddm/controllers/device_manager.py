"""
Device Manager for DDM Racing System

This module handles ESP32 device discovery, registration, and management.
It provides network scanning, device registration, and device lifecycle management.
"""

import asyncio
import threading
import time
from typing import Dict, List, Optional, Set
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import current_app

from ddm.models.device import Device, DeviceStatus, DeviceType
from ddm.utils.logger import get_logger, log_device_command, log_system_event
from ddm.utils.helpers import scan_network_for_devices, is_port_open, timing_decorator
from ddm.utils.errors import DeviceError

logger = get_logger(__name__)


class DeviceManager:
    """
    Manages ESP32 device discovery, registration, and lifecycle.
    
    This class handles:
    - Network scanning for ESP32 devices
    - Device registration and deregistration
    - Device status tracking
    - Device information storage
    """
    
    def __init__(self, network_range: str = "192.168.1.0/24", discovery_port: int = 8080):
        """
        Initialize the device manager.
        
        Args:
            network_range: Network range to scan for devices
            discovery_port: Port to scan for ESP32 devices
        """
        self.network_range = network_range
        self.discovery_port = discovery_port
        self.devices: Dict[str, Device] = {}
        self.scanning = False
        self.scan_thread: Optional[threading.Thread] = None
        self.last_scan_time: Optional[float] = None
        self.scan_interval = 30  # seconds
        self.device_timeout = 60  # seconds
        
        # Thread pool for parallel operations
        self.executor = ThreadPoolExecutor(max_workers=10)
        
        logger.info(f"DeviceManager initialized: network={network_range}, port={discovery_port}")
    
    def start_auto_discovery(self):
        """Start automatic device discovery in background thread."""
        if self.scanning:
            logger.warning("Auto-discovery already running")
            return
        
        self.scanning = True
        self.scan_thread = threading.Thread(target=self._auto_discovery_loop, daemon=True)
        self.scan_thread.start()
        log_system_event("device_discovery", "Auto-discovery started")
    
    def stop_auto_discovery(self):
        """Stop automatic device discovery."""
        if not self.scanning:
            return
        
        self.scanning = False
        if self.scan_thread and self.scan_thread.is_alive():
            self.scan_thread.join(timeout=5)
        
        log_system_event("device_discovery", "Auto-discovery stopped")
    
    def _auto_discovery_loop(self):
        """Background loop for automatic device discovery."""
        while self.scanning:
            try:
                self.discover_devices()
                self._check_device_timeouts()
                time.sleep(self.scan_interval)
            except Exception as e:
                logger.error(f"Error in auto-discovery loop: {e}")
                time.sleep(5)  # Wait before retrying
    
    @timing_decorator
    def discover_devices(self) -> List[Device]:
        """
        Discover ESP32 devices on the network.
        
        Returns:
            List of discovered devices
        """
        logger.info(f"Starting device discovery scan: {self.network_range}")
        
        # Scan network for devices
        responding_ips = scan_network_for_devices(
            self.network_range, 
            self.discovery_port, 
            timeout=2
        )
        
        discovered_devices = []
        
        # Check each responding IP
        for ip in responding_ips:
            try:
                device = self._probe_device(ip)
                if device:
                    discovered_devices.append(device)
                    self._register_or_update_device(device)
            except Exception as e:
                logger.error(f"Error probing device at {ip}: {e}")
        
        self.last_scan_time = time.time()
        logger.info(f"Discovery scan complete: found {len(discovered_devices)} devices")
        
        return discovered_devices
    
    def _probe_device(self, ip_address: str) -> Optional[Device]:
        """
        Probe a device to get its information.
        
        Args:
            ip_address: IP address to probe
            
        Returns:
            Device object if valid ESP32 device, None otherwise
        """
        try:
            # Try to get device info
            response = requests.get(
                f"http://{ip_address}:{self.discovery_port}/info",
                timeout=3
            )
            
            if response.status_code == 200:
                device_info = response.json()
                
                # Validate this is a DDM ESP32 device
                if device_info.get('system') == 'DDM-ESP32':
                    device = Device(
                        device_id=device_info.get('device_id', f"esp32-{ip_address.replace('.', '-')}"),
                        ip_address=ip_address,
                        device_type=DeviceType(device_info.get('type', 'unknown')),
                        status=DeviceStatus.ONLINE,
                        led_count=device_info.get('led_count', 50),
                        led_type=device_info.get('led_type', 'WS2812B'),
                        color_order=device_info.get('color_order', 'RGB'),
                        max_brightness=device_info.get('max_brightness', 255),
                        current_brightness=device_info.get('current_brightness', 128),
                        port=self.discovery_port,
                        firmware_version=device_info.get('firmware_version', 'unknown'),
                        mac_address=device_info.get('mac_address', 'unknown'),
                        uptime=device_info.get('uptime', 0),
                        temperature=device_info.get('temperature'),
                        free_memory=device_info.get('free_memory', 0)
                    )
                    
                    logger.info(f"Discovered ESP32 device: {device.device_id} at {ip_address}")
                    return device
                else:
                    logger.debug(f"Device at {ip_address} is not a DDM ESP32 device")
            else:
                logger.debug(f"Device at {ip_address} returned status {response.status_code}")
                
        except requests.exceptions.Timeout:
            logger.debug(f"Device at {ip_address} timed out")
        except requests.exceptions.ConnectionError:
            logger.debug(f"Could not connect to device at {ip_address}")
        except Exception as e:
            logger.error(f"Error probing device at {ip_address}: {e}")
        
        return None
    
    def _register_or_update_device(self, device: Device):
        """
        Register a new device or update an existing one.
        
        Args:
            device: Device to register or update
        """
        existing_device = self.devices.get(device.device_id)
        
        if existing_device:
            # Update existing device
            existing_device.ip_address = device.ip_address
            existing_device.status = DeviceStatus.ONLINE
            existing_device.last_seen = device.last_seen
            existing_device.update_health_info(
                uptime=device.uptime,
                temperature=device.temperature,
                free_memory=device.free_memory,
                firmware_version=device.firmware_version
            )
            logger.debug(f"Updated existing device: {device.device_id}")
        else:
            # Register new device
            self.devices[device.device_id] = device
            logger.info(f"Registered new device: {device.device_id}")
            log_device_command(device.device_id, "device_registered", {"ip": device.ip_address})
    
    def _check_device_timeouts(self):
        """Check for devices that haven't been seen recently and mark them offline."""
        current_time = time.time()
        timeout_threshold = current_time - self.device_timeout
        
        for device in self.devices.values():
            if device.last_seen and device.last_seen.timestamp() < timeout_threshold:
                if device.status == DeviceStatus.ONLINE:
                    device.update_status(DeviceStatus.OFFLINE, "Device timeout")
                    logger.warning(f"Device {device.device_id} timed out")
    
    def register_device(self, device_id: str, ip_address: str, device_type: str = "unknown") -> Device:
        """
        Manually register a device.
        
        Args:
            device_id: Unique device identifier
            ip_address: Device IP address
            device_type: Type of device
            
        Returns:
            Registered device
            
        Raises:
            DeviceError: If device registration fails
        """
        try:
            device = Device(
                device_id=device_id,
                ip_address=ip_address,
                device_type=DeviceType(device_type),
                status=DeviceStatus.UNKNOWN
            )
            
            # Try to probe the device for more information
            probed_device = self._probe_device(ip_address)
            if probed_device:
                # Use probed information but keep the specified device_id
                probed_device.device_id = device_id
                device = probed_device
            
            self.devices[device_id] = device
            logger.info(f"Manually registered device: {device_id}")
            log_device_command(device_id, "device_registered", {"ip": ip_address, "manual": True})
            
            return device
            
        except Exception as e:
            raise DeviceError(f"Failed to register device {device_id}: {e}", device_id=device_id)
    
    def unregister_device(self, device_id: str) -> bool:
        """
        Unregister a device.
        
        Args:
            device_id: Device identifier to unregister
            
        Returns:
            True if device was unregistered, False if not found
        """
        if device_id in self.devices:
            device = self.devices.pop(device_id)
            logger.info(f"Unregistered device: {device_id}")
            log_device_command(device_id, "device_unregistered", {"ip": device.ip_address})
            return True
        return False
    
    def get_device(self, device_id: str) -> Optional[Device]:
        """
        Get a device by ID.
        
        Args:
            device_id: Device identifier
            
        Returns:
            Device object or None if not found
        """
        return self.devices.get(device_id)
    
    def get_all_devices(self) -> List[Device]:
        """
        Get all registered devices.
        
        Returns:
            List of all devices
        """
        return list(self.devices.values())
    
    def get_online_devices(self) -> List[Device]:
        """
        Get all online devices.
        
        Returns:
            List of online devices
        """
        return [device for device in self.devices.values() if device.is_online]
    
    def get_devices_by_type(self, device_type: DeviceType) -> List[Device]:
        """
        Get all devices of a specific type.
        
        Args:
            device_type: Type of devices to return
            
        Returns:
            List of devices of the specified type
        """
        return [device for device in self.devices.values() if device.device_type == device_type]
    
    def get_device_count(self) -> int:
        """Get total number of registered devices."""
        return len(self.devices)
    
    def get_online_device_count(self) -> int:
        """Get number of online devices."""
        return len(self.get_online_devices())
    
    def ping_device(self, device_id: str) -> bool:
        """
        Ping a device to check if it's responsive.
        
        Args:
            device_id: Device identifier
            
        Returns:
            True if device responds, False otherwise
        """
        device = self.get_device(device_id)
        if not device:
            return False
        
        try:
            response = requests.get(
                f"{device.base_url}/ping",
                timeout=3
            )
            
            if response.status_code == 200:
                device.update_status(DeviceStatus.ONLINE)
                return True
            else:
                device.update_status(DeviceStatus.OFFLINE, f"HTTP {response.status_code}")
                return False
                
        except Exception as e:
            device.update_status(DeviceStatus.OFFLINE, str(e))
            return False
    
    def refresh_device_info(self, device_id: str) -> bool:
        """
        Refresh device information by probing it.
        
        Args:
            device_id: Device identifier
            
        Returns:
            True if refresh was successful, False otherwise
        """
        device = self.get_device(device_id)
        if not device:
            return False
        
        updated_device = self._probe_device(device.ip_address)
        if updated_device:
            # Update existing device with new information
            device.update_health_info(
                uptime=updated_device.uptime,
                temperature=updated_device.temperature,
                free_memory=updated_device.free_memory,
                firmware_version=updated_device.firmware_version
            )
            device.update_status(DeviceStatus.ONLINE)
            return True
        else:
            device.update_status(DeviceStatus.OFFLINE, "Failed to refresh info")
            return False
    
    def get_discovery_stats(self) -> Dict[str, any]:
        """
        Get device discovery statistics.
        
        Returns:
            Dictionary of discovery statistics
        """
        online_devices = self.get_online_devices()
        
        return {
            'total_devices': self.get_device_count(),
            'online_devices': len(online_devices),
            'offline_devices': self.get_device_count() - len(online_devices),
            'last_scan_time': self.last_scan_time,
            'scanning': self.scanning,
            'network_range': self.network_range,
            'discovery_port': self.discovery_port,
            'device_types': {
                device_type.value: len(self.get_devices_by_type(device_type))
                for device_type in DeviceType
            }
        }
    
    def export_devices(self) -> List[Dict[str, any]]:
        """
        Export all devices to a list of dictionaries.
        
        Returns:
            List of device dictionaries
        """
        return [device.to_dict() for device in self.devices.values()]
    
    def import_devices(self, devices_data: List[Dict[str, any]]) -> int:
        """
        Import devices from a list of dictionaries.
        
        Args:
            devices_data: List of device dictionaries
            
        Returns:
            Number of devices imported
        """
        imported_count = 0
        
        for device_data in devices_data:
            try:
                device = Device.from_dict(device_data)
                self.devices[device.device_id] = device
                imported_count += 1
            except Exception as e:
                logger.error(f"Failed to import device {device_data.get('device_id', 'unknown')}: {e}")
        
        logger.info(f"Imported {imported_count} devices")
        return imported_count
    
    def shutdown(self):
        """Shutdown the device manager and clean up resources."""
        logger.info("Shutting down DeviceManager")
        self.stop_auto_discovery()
        self.executor.shutdown(wait=True)
        log_system_event("device_manager", "DeviceManager shutdown complete")

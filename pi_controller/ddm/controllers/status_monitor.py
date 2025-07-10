"""
Status Monitor for DDM Racing System

This module provides real-time monitoring of ESP32 device health and status.
It handles periodic health checks, status updates, and WebSocket notifications.
"""

import threading
import time
from typing import Dict, List, Optional, Callable
from datetime import datetime, timedelta
import requests
from dataclasses import dataclass
from enum import Enum

from ddm.models.device import Device, DeviceStatus
from ddm.utils.logger import get_logger, log_system_event, log_performance
from ddm.utils.helpers import timing_decorator

logger = get_logger(__name__)


class HealthCheckResult(Enum):
    """Health check result types."""
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    OFFLINE = "offline"


@dataclass
class HealthMetrics:
    """
    Health metrics for a device.
    
    Attributes:
        device_id: Device identifier
        status: Current device status
        response_time: Response time in seconds
        uptime: Device uptime in seconds
        temperature: Device temperature in Celsius
        free_memory: Free memory in bytes
        cpu_usage: CPU usage percentage
        wifi_strength: WiFi signal strength in dBm
        last_command_time: Time of last command execution
        error_count: Number of recent errors
        timestamp: When metrics were collected
    """
    device_id: str
    status: DeviceStatus
    response_time: float = 0.0
    uptime: int = 0
    temperature: Optional[float] = None
    free_memory: int = 0
    cpu_usage: float = 0.0
    wifi_strength: int = 0
    last_command_time: Optional[datetime] = None
    error_count: int = 0
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
    
    def get_health_status(self) -> HealthCheckResult:
        """Determine overall health status based on metrics."""
        if self.status == DeviceStatus.OFFLINE:
            return HealthCheckResult.OFFLINE
        
        # Check for critical conditions
        if self.temperature and self.temperature > 85:  # ESP32 critical temp
            return HealthCheckResult.CRITICAL
        
        if self.free_memory < 1024:  # Less than 1KB free
            return HealthCheckResult.CRITICAL
        
        if self.response_time > 5.0:  # Very slow response
            return HealthCheckResult.CRITICAL
        
        # Check for warning conditions
        if self.temperature and self.temperature > 70:  # ESP32 warning temp
            return HealthCheckResult.WARNING
        
        if self.free_memory < 5120:  # Less than 5KB free
            return HealthCheckResult.WARNING
        
        if self.response_time > 2.0:  # Slow response
            return HealthCheckResult.WARNING
        
        if self.wifi_strength < -80:  # Weak WiFi signal
            return HealthCheckResult.WARNING
        
        if self.error_count > 5:  # Many recent errors
            return HealthCheckResult.WARNING
        
        return HealthCheckResult.HEALTHY
    
    def to_dict(self) -> Dict[str, any]:
        """Convert health metrics to dictionary."""
        return {
            'device_id': self.device_id,
            'status': self.status.value,
            'response_time': self.response_time,
            'uptime': self.uptime,
            'temperature': self.temperature,
            'free_memory': self.free_memory,
            'cpu_usage': self.cpu_usage,
            'wifi_strength': self.wifi_strength,
            'last_command_time': self.last_command_time.isoformat() if self.last_command_time else None,
            'error_count': self.error_count,
            'health_status': self.get_health_status().value,
            'timestamp': self.timestamp.isoformat()
        }


class StatusMonitor:
    """
    Monitors ESP32 device health and status.
    
    This class provides:
    - Periodic health checks for all devices
    - Real-time status monitoring
    - Performance metrics collection
    - Alert generation for critical conditions
    - WebSocket notifications for status changes
    """
    
    def __init__(self, device_manager, socketio=None):
        """
        Initialize the status monitor.
        
        Args:
            device_manager: DeviceManager instance
            socketio: SocketIO instance for real-time notifications
        """
        self.device_manager = device_manager
        self.socketio = socketio
        self.monitoring = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.health_check_interval = 30  # seconds
        self.ping_interval = 10  # seconds
        self.metrics_history: Dict[str, List[HealthMetrics]] = {}
        self.max_history_size = 100
        self.alert_callbacks: List[Callable] = []
        self.last_health_check = None
        self.last_ping_check = None
        
        # Statistics
        self.health_checks_performed = 0
        self.alerts_generated = 0
        self.status_changes_detected = 0
        
        logger.info("StatusMonitor initialized")
    
    def start_monitoring(self):
        """Start the status monitoring in a background thread."""
        if self.monitoring:
            logger.warning("Status monitoring already running")
            return
        
        self.monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitor_thread.start()
        log_system_event("status_monitor", "Status monitoring started")
    
    def stop_monitoring(self):
        """Stop the status monitoring."""
        if not self.monitoring:
            return
        
        self.monitoring = False
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=5)
        
        log_system_event("status_monitor", "Status monitoring stopped")
    
    def _monitoring_loop(self):
        """Main monitoring loop."""
        while self.monitoring:
            try:
                current_time = time.time()
                
                # Perform health checks
                if (self.last_health_check is None or 
                    current_time - self.last_health_check >= self.health_check_interval):
                    self._perform_health_checks()
                    self.last_health_check = current_time
                
                # Perform ping checks
                if (self.last_ping_check is None or 
                    current_time - self.last_ping_check >= self.ping_interval):
                    self._perform_ping_checks()
                    self.last_ping_check = current_time
                
                # Clean up old metrics
                self._cleanup_old_metrics()
                
                # Short sleep to prevent busy waiting
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in status monitoring loop: {e}")
                time.sleep(5)
    
    @timing_decorator
    def _perform_health_checks(self):
        """Perform comprehensive health checks on all devices."""
        devices = self.device_manager.get_all_devices()
        if not devices:
            return
        
        logger.debug(f"Performing health checks on {len(devices)} devices")
        
        for device in devices:
            try:
                metrics = self._check_device_health(device)
                self._process_health_metrics(device, metrics)
                
            except Exception as e:
                logger.error(f"Error checking health for device {device.device_id}: {e}")
        
        self.health_checks_performed += 1
    
    def _check_device_health(self, device: Device) -> HealthMetrics:
        """
        Perform detailed health check on a single device.
        
        Args:
            device: Device to check
            
        Returns:
            HealthMetrics object
        """
        start_time = time.time()
        
        try:
            # Get device status and metrics
            response = requests.get(
                f"{device.base_url}/status",
                timeout=5
            )
            
            response_time = time.time() - start_time
            
            if response.status_code == 200:
                status_data = response.json()
                
                # Extract metrics from response
                metrics = HealthMetrics(
                    device_id=device.device_id,
                    status=DeviceStatus.ONLINE,
                    response_time=response_time,
                    uptime=status_data.get('uptime', 0),
                    temperature=status_data.get('temperature'),
                    free_memory=status_data.get('free_memory', 0),
                    cpu_usage=status_data.get('cpu_usage', 0.0),
                    wifi_strength=status_data.get('wifi_strength', 0),
                    error_count=status_data.get('error_count', 0)
                )
                
                # Update device with health info
                device.update_health_info(
                    uptime=metrics.uptime,
                    temperature=metrics.temperature,
                    free_memory=metrics.free_memory,
                    firmware_version=status_data.get('firmware_version')
                )
                
                # Update device status if it changed
                if device.status != DeviceStatus.ONLINE:
                    device.update_status(DeviceStatus.ONLINE)
                    self.status_changes_detected += 1
                
                return metrics
                
            else:
                # Device responded but with error
                metrics = HealthMetrics(
                    device_id=device.device_id,
                    status=DeviceStatus.ERROR,
                    response_time=response_time,
                    error_count=1
                )
                
                device.update_status(DeviceStatus.ERROR, f"HTTP {response.status_code}")
                if device.status != DeviceStatus.ERROR:
                    self.status_changes_detected += 1
                
                return metrics
                
        except requests.exceptions.Timeout:
            metrics = HealthMetrics(
                device_id=device.device_id,
                status=DeviceStatus.OFFLINE,
                response_time=time.time() - start_time,
                error_count=1
            )
            
            device.update_status(DeviceStatus.OFFLINE, "Health check timeout")
            if device.status != DeviceStatus.OFFLINE:
                self.status_changes_detected += 1
            
            return metrics
            
        except requests.exceptions.ConnectionError:
            metrics = HealthMetrics(
                device_id=device.device_id,
                status=DeviceStatus.OFFLINE,
                response_time=time.time() - start_time,
                error_count=1
            )
            
            device.update_status(DeviceStatus.OFFLINE, "Connection error")
            if device.status != DeviceStatus.OFFLINE:
                self.status_changes_detected += 1
            
            return metrics
            
        except Exception as e:
            metrics = HealthMetrics(
                device_id=device.device_id,
                status=DeviceStatus.ERROR,
                response_time=time.time() - start_time,
                error_count=1
            )
            
            device.update_status(DeviceStatus.ERROR, str(e))
            if device.status != DeviceStatus.ERROR:
                self.status_changes_detected += 1
            
            return metrics
    
    def _perform_ping_checks(self):
        """Perform quick ping checks on all devices."""
        devices = self.device_manager.get_all_devices()
        
        for device in devices:
            try:
                is_reachable = self.device_manager.ping_device(device.device_id)
                if not is_reachable and device.status == DeviceStatus.ONLINE:
                    device.update_status(DeviceStatus.OFFLINE, "Ping failed")
                    self.status_changes_detected += 1
                    
            except Exception as e:
                logger.error(f"Error pinging device {device.device_id}: {e}")
    
    def _process_health_metrics(self, device: Device, metrics: HealthMetrics):
        """
        Process health metrics and generate alerts if needed.
        
        Args:
            device: Device that was checked
            metrics: Health metrics collected
        """
        # Store metrics in history
        if device.device_id not in self.metrics_history:
            self.metrics_history[device.device_id] = []
        
        self.metrics_history[device.device_id].append(metrics)
        
        # Check for alerts
        health_status = metrics.get_health_status()
        if health_status in [HealthCheckResult.WARNING, HealthCheckResult.CRITICAL]:
            self._generate_alert(device, metrics, health_status)
        
        # Send WebSocket notification
        if self.socketio:
            self.socketio.emit('device_health_update', {
                'device_id': device.device_id,
                'metrics': metrics.to_dict(),
                'health_status': health_status.value
            })
        
        # Log performance metrics
        log_performance(
            "health_check",
            metrics.response_time * 1000,  # Convert to milliseconds
            {
                "device_id": device.device_id,
                "status": metrics.status.value,
                "health": health_status.value
            }
        )
    
    def _generate_alert(self, device: Device, metrics: HealthMetrics, health_status: HealthCheckResult):
        """
        Generate an alert for a device health issue.
        
        Args:
            device: Device with health issue
            metrics: Health metrics
            health_status: Health status level
        """
        alert_data = {
            'device_id': device.device_id,
            'device_ip': device.ip_address,
            'health_status': health_status.value,
            'metrics': metrics.to_dict(),
            'timestamp': datetime.now().isoformat()
        }
        
        # Log the alert
        logger.warning(f"Health alert for device {device.device_id}: {health_status.value}")
        
        # Call alert callbacks
        for callback in self.alert_callbacks:
            try:
                callback(alert_data)
            except Exception as e:
                logger.error(f"Error calling alert callback: {e}")
        
        # Send WebSocket notification
        if self.socketio:
            self.socketio.emit('device_alert', alert_data)
        
        self.alerts_generated += 1
    
    def _cleanup_old_metrics(self):
        """Clean up old metrics to prevent memory growth."""
        for device_id, metrics_list in self.metrics_history.items():
            if len(metrics_list) > self.max_history_size:
                # Remove oldest metrics
                metrics_list[:] = metrics_list[-self.max_history_size:]
    
    def add_alert_callback(self, callback: Callable):
        """
        Add a callback function to be called when alerts are generated.
        
        Args:
            callback: Function to call with alert data
        """
        self.alert_callbacks.append(callback)
    
    def remove_alert_callback(self, callback: Callable):
        """
        Remove an alert callback.
        
        Args:
            callback: Function to remove
        """
        if callback in self.alert_callbacks:
            self.alert_callbacks.remove(callback)
    
    def get_device_health(self, device_id: str) -> Optional[HealthMetrics]:
        """
        Get the latest health metrics for a device.
        
        Args:
            device_id: Device identifier
            
        Returns:
            Latest HealthMetrics or None if not found
        """
        metrics_list = self.metrics_history.get(device_id, [])
        return metrics_list[-1] if metrics_list else None
    
    def get_device_health_history(self, device_id: str, limit: int = 50) -> List[HealthMetrics]:
        """
        Get health metrics history for a device.
        
        Args:
            device_id: Device identifier
            limit: Maximum number of metrics to return
            
        Returns:
            List of HealthMetrics (most recent first)
        """
        metrics_list = self.metrics_history.get(device_id, [])
        return metrics_list[-limit:] if metrics_list else []
    
    def get_system_health_summary(self) -> Dict[str, any]:
        """
        Get a summary of system health status.
        
        Returns:
            Dictionary with system health summary
        """
        devices = self.device_manager.get_all_devices()
        
        health_counts = {
            'healthy': 0,
            'warning': 0,
            'critical': 0,
            'offline': 0
        }
        
        total_devices = len(devices)
        avg_response_time = 0.0
        total_uptime = 0
        
        for device in devices:
            latest_metrics = self.get_device_health(device.device_id)
            if latest_metrics:
                health_status = latest_metrics.get_health_status()
                health_counts[health_status.value] += 1
                avg_response_time += latest_metrics.response_time
                total_uptime += latest_metrics.uptime
            else:
                health_counts['offline'] += 1
        
        if total_devices > 0:
            avg_response_time /= total_devices
            total_uptime /= total_devices
        
        return {
            'total_devices': total_devices,
            'health_distribution': health_counts,
            'avg_response_time': avg_response_time,
            'avg_uptime': total_uptime,
            'monitoring_active': self.monitoring,
            'health_checks_performed': self.health_checks_performed,
            'alerts_generated': self.alerts_generated,
            'status_changes_detected': self.status_changes_detected,
            'last_health_check': self.last_health_check,
            'last_ping_check': self.last_ping_check
        }
    
    def force_health_check(self, device_id: str = None) -> Dict[str, any]:
        """
        Force an immediate health check.
        
        Args:
            device_id: Specific device to check, or None for all devices
            
        Returns:
            Dictionary with check results
        """
        if device_id:
            device = self.device_manager.get_device(device_id)
            if device:
                metrics = self._check_device_health(device)
                self._process_health_metrics(device, metrics)
                return {
                    'device_id': device_id,
                    'metrics': metrics.to_dict(),
                    'health_status': metrics.get_health_status().value
                }
            else:
                return {'error': f'Device {device_id} not found'}
        else:
            self._perform_health_checks()
            return {'message': 'Health check performed on all devices'}
    
    def get_monitoring_statistics(self) -> Dict[str, any]:
        """
        Get status monitoring statistics.
        
        Returns:
            Dictionary of monitoring statistics
        """
        return {
            'monitoring_active': self.monitoring,
            'health_check_interval': self.health_check_interval,
            'ping_interval': self.ping_interval,
            'health_checks_performed': self.health_checks_performed,
            'alerts_generated': self.alerts_generated,
            'status_changes_detected': self.status_changes_detected,
            'devices_monitored': len(self.metrics_history),
            'total_metrics_stored': sum(len(metrics) for metrics in self.metrics_history.values()),
            'alert_callbacks_registered': len(self.alert_callbacks)
        }
    
    def shutdown(self):
        """Shutdown the status monitor and clean up resources."""
        logger.info("Shutting down StatusMonitor")
        self.stop_monitoring()
        self.metrics_history.clear()
        self.alert_callbacks.clear()
        logger.info("StatusMonitor shutdown complete")

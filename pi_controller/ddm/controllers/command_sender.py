"""
Command Sender for DDM Racing System

This module handles sending commands to ESP32 devices via HTTP requests.
It provides command queuing, retry logic, and response handling.
"""

import asyncio
import threading
import time
from typing import Dict, List, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor, Future
import requests
from dataclasses import dataclass
from enum import Enum

from ddm.models.device import Device, DeviceStatus, DeviceCommand
from ddm.utils.logger import get_logger, log_device_command, log_performance
from ddm.utils.helpers import timing_decorator, create_response
from ddm.utils.errors import DeviceError
from ddm.utils.validators import validate_command_payload

logger = get_logger(__name__)


class CommandResult(Enum):
    """Command execution result types."""
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DEVICE_OFFLINE = "device_offline"
    INVALID_COMMAND = "invalid_command"


@dataclass
class CommandResponse:
    """
    Response from a command execution.
    
    Attributes:
        result: Command execution result
        message: Response message
        data: Response data
        response_time: Command execution time in seconds
        http_status: HTTP status code
        device_id: Target device ID
        command: Original command
    """
    result: CommandResult
    message: str
    data: Optional[Dict[str, Any]] = None
    response_time: float = 0.0
    http_status: int = 0
    device_id: str = ""
    command: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert response to dictionary."""
        return {
            'result': self.result.value,
            'message': self.message,
            'data': self.data,
            'response_time': self.response_time,
            'http_status': self.http_status,
            'device_id': self.device_id,
            'command': self.command
        }


class CommandSender:
    """
    Handles sending commands to ESP32 devices.
    
    This class provides:
    - HTTP command dispatch to ESP32 devices
    - Command queuing and retry logic
    - Response handling and validation
    - Performance monitoring
    """
    
    def __init__(self, device_manager, max_workers: int = 5):
        """
        Initialize the command sender.
        
        Args:
            device_manager: DeviceManager instance
            max_workers: Maximum number of concurrent command threads
        """
        self.device_manager = device_manager
        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.command_queue: List[tuple] = []  # (device_id, command, callback)
        self.processing_queue = False
        self.queue_thread: Optional[threading.Thread] = None
        self.active_commands: Dict[str, Future] = {}  # device_id -> Future
        
        # Statistics
        self.commands_sent = 0
        self.commands_successful = 0
        self.commands_failed = 0
        self.total_response_time = 0.0
        
        logger.info(f"CommandSender initialized with {max_workers} workers")
    
    def start_queue_processor(self):
        """Start the command queue processor."""
        if self.processing_queue:
            logger.warning("Queue processor already running")
            return
        
        self.processing_queue = True
        self.queue_thread = threading.Thread(target=self._process_queue_loop, daemon=True)
        self.queue_thread.start()
        logger.info("Command queue processor started")
    
    def stop_queue_processor(self):
        """Stop the command queue processor."""
        if not self.processing_queue:
            return
        
        self.processing_queue = False
        if self.queue_thread and self.queue_thread.is_alive():
            self.queue_thread.join(timeout=5)
        
        logger.info("Command queue processor stopped")
    
    def _process_queue_loop(self):
        """Background loop for processing the command queue."""
        while self.processing_queue:
            try:
                if self.command_queue:
                    device_id, command, callback = self.command_queue.pop(0)
                    
                    # Check if device is already processing a command
                    if device_id not in self.active_commands:
                        future = self.executor.submit(self._execute_command, device_id, command)
                        self.active_commands[device_id] = future
                        
                        # Add callback to handle completion
                        future.add_done_callback(
                            lambda f, dev_id=device_id, cb=callback: self._handle_command_completion(f, dev_id, cb)
                        )
                else:
                    time.sleep(0.1)  # Short sleep when queue is empty
                    
            except Exception as e:
                logger.error(f"Error in command queue processor: {e}")
                time.sleep(1)
    
    def _handle_command_completion(self, future: Future, device_id: str, callback: Optional[Callable]):
        """Handle command completion and cleanup."""
        try:
            # Remove from active commands
            if device_id in self.active_commands:
                del self.active_commands[device_id]
            
            # Get result and call callback if provided
            result = future.result()
            if callback:
                callback(result)
                
        except Exception as e:
            logger.error(f"Error handling command completion for device {device_id}: {e}")
    
    @timing_decorator
    def send_command(self, device_id: str, command: str, parameters: Dict[str, Any] = None, 
                     callback: Optional[Callable] = None) -> CommandResponse:
        """
        Send a command to a device immediately.
        
        Args:
            device_id: Target device ID
            command: Command to send
            parameters: Command parameters
            callback: Optional callback function for async response
            
        Returns:
            CommandResponse object
        """
        if parameters is None:
            parameters = {}
        
        device_command = DeviceCommand(
            command=command,
            parameters=parameters
        )
        
        return self._execute_command(device_id, device_command)
    
    def queue_command(self, device_id: str, command: str, parameters: Dict[str, Any] = None,
                     callback: Optional[Callable] = None):
        """
        Queue a command for execution.
        
        Args:
            device_id: Target device ID
            command: Command to send
            parameters: Command parameters
            callback: Optional callback function for async response
        """
        if parameters is None:
            parameters = {}
        
        device_command = DeviceCommand(
            command=command,
            parameters=parameters
        )
        
        # Add to device's command queue
        device = self.device_manager.get_device(device_id)
        if device:
            device.add_command(device_command)
        
        # Add to global command queue
        self.command_queue.append((device_id, device_command, callback))
        logger.debug(f"Queued command {command} for device {device_id}")
    
    def _execute_command(self, device_id: str, command: DeviceCommand) -> CommandResponse:
        """
        Execute a command on a device.
        
        Args:
            device_id: Target device ID
            command: Command to execute
            
        Returns:
            CommandResponse object
        """
        start_time = time.time()
        device = self.device_manager.get_device(device_id)
        
        if not device:
            return CommandResponse(
                result=CommandResult.DEVICE_OFFLINE,
                message=f"Device {device_id} not found",
                device_id=device_id,
                command=command.command
            )
        
        if not device.is_responsive:
            return CommandResponse(
                result=CommandResult.DEVICE_OFFLINE,
                message=f"Device {device_id} is not responsive (status: {device.status})",
                device_id=device_id,
                command=command.command
            )
        
        try:
            # Validate command payload
            payload = validate_command_payload({
                'command': command.command,
                'parameters': command.parameters
            })
            
            # Mark device as busy
            device.update_status(DeviceStatus.BUSY)
            
            # Send HTTP request
            response = requests.post(
                f"{device.base_url}/command",
                json=payload,
                timeout=command.timeout,
                headers={'Content-Type': 'application/json'}
            )
            
            response_time = time.time() - start_time
            
            # Process response
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    result = CommandResult.SUCCESS
                    message = response_data.get('message', 'Command executed successfully')
                    data = response_data.get('data')
                except ValueError:
                    # Response is not JSON
                    result = CommandResult.SUCCESS
                    message = "Command executed successfully"
                    data = {'raw_response': response.text}
                
                # Update device status
                device.update_status(DeviceStatus.ONLINE)
                device.record_command_result(True, response_time)
                device.last_command = command.command
                device.last_response = message
                
                self.commands_successful += 1
                
            else:
                result = CommandResult.FAILED
                message = f"HTTP {response.status_code}: {response.text}"
                data = None
                
                device.update_status(DeviceStatus.ERROR, message)
                device.record_command_result(False, response_time)
                
                self.commands_failed += 1
            
            # Log command execution
            log_device_command(
                device_id, 
                command.command, 
                command.parameters, 
                result == CommandResult.SUCCESS
            )
            
            log_performance(
                f"command_{command.command}",
                response_time * 1000,  # Convert to milliseconds
                {"device_id": device_id, "success": result == CommandResult.SUCCESS}
            )
            
            self.commands_sent += 1
            self.total_response_time += response_time
            
            return CommandResponse(
                result=result,
                message=message,
                data=data,
                response_time=response_time,
                http_status=response.status_code,
                device_id=device_id,
                command=command.command
            )
            
        except requests.exceptions.Timeout:
            message = f"Command timeout after {command.timeout} seconds"
            device.update_status(DeviceStatus.OFFLINE, message)
            device.record_command_result(False)
            self.commands_failed += 1
            
            return CommandResponse(
                result=CommandResult.TIMEOUT,
                message=message,
                response_time=time.time() - start_time,
                device_id=device_id,
                command=command.command
            )
            
        except requests.exceptions.ConnectionError:
            message = f"Could not connect to device {device_id}"
            device.update_status(DeviceStatus.OFFLINE, message)
            device.record_command_result(False)
            self.commands_failed += 1
            
            return CommandResponse(
                result=CommandResult.DEVICE_OFFLINE,
                message=message,
                response_time=time.time() - start_time,
                device_id=device_id,
                command=command.command
            )
            
        except Exception as e:
            message = f"Command execution error: {str(e)}"
            device.update_status(DeviceStatus.ERROR, message)
            device.record_command_result(False)
            self.commands_failed += 1
            
            logger.error(f"Command execution error for device {device_id}: {e}")
            
            return CommandResponse(
                result=CommandResult.FAILED,
                message=message,
                response_time=time.time() - start_time,
                device_id=device_id,
                command=command.command
            )
    
    def broadcast_command(self, command: str, parameters: Dict[str, Any] = None, 
                         device_filter: Optional[Callable] = None) -> List[CommandResponse]:
        """
        Broadcast a command to multiple devices.
        
        Args:
            command: Command to broadcast
            parameters: Command parameters
            device_filter: Optional function to filter devices
            
        Returns:
            List of CommandResponse objects
        """
        if parameters is None:
            parameters = {}
        
        # Get target devices
        devices = self.device_manager.get_online_devices()
        if device_filter:
            devices = [device for device in devices if device_filter(device)]
        
        logger.info(f"Broadcasting command {command} to {len(devices)} devices")
        
        # Send commands in parallel
        futures = []
        for device in devices:
            device_command = DeviceCommand(command=command, parameters=parameters)
            future = self.executor.submit(self._execute_command, device.device_id, device_command)
            futures.append(future)
        
        # Wait for all commands to complete
        results = []
        for future in futures:
            try:
                result = future.result(timeout=30)  # 30 second timeout for broadcast
                results.append(result)
            except Exception as e:
                logger.error(f"Broadcast command error: {e}")
                results.append(CommandResponse(
                    result=CommandResult.FAILED,
                    message=f"Broadcast error: {str(e)}",
                    command=command
                ))
        
        return results
    
    def get_command_statistics(self) -> Dict[str, Any]:
        """
        Get command execution statistics.
        
        Returns:
            Dictionary of command statistics
        """
        avg_response_time = 0.0
        if self.commands_sent > 0:
            avg_response_time = self.total_response_time / self.commands_sent
        
        success_rate = 0.0
        if self.commands_sent > 0:
            success_rate = (self.commands_successful / self.commands_sent) * 100
        
        return {
            'commands_sent': self.commands_sent,
            'commands_successful': self.commands_successful,
            'commands_failed': self.commands_failed,
            'success_rate': success_rate,
            'avg_response_time': avg_response_time,
            'queue_length': len(self.command_queue),
            'active_commands': len(self.active_commands),
            'processing_queue': self.processing_queue
        }
    
    def clear_device_queue(self, device_id: str):
        """
        Clear the command queue for a specific device.
        
        Args:
            device_id: Device ID to clear queue for
        """
        device = self.device_manager.get_device(device_id)
        if device:
            device.clear_command_queue()
        
        # Remove from global queue
        self.command_queue = [
            (dev_id, cmd, callback) for dev_id, cmd, callback in self.command_queue
            if dev_id != device_id
        ]
        
        logger.info(f"Cleared command queue for device {device_id}")
    
    def clear_all_queues(self):
        """Clear all command queues."""
        # Clear device queues
        for device in self.device_manager.get_all_devices():
            device.clear_command_queue()
        
        # Clear global queue
        self.command_queue.clear()
        
        logger.info("Cleared all command queues")
    
    def emergency_stop(self):
        """Send emergency stop command to all devices."""
        logger.warning("Emergency stop initiated")
        
        # Clear all queues first
        self.clear_all_queues()
        
        # Send stop command to all online devices
        results = self.broadcast_command("emergency_stop", {"immediate": True})
        
        # Log results
        successful_stops = len([r for r in results if r.result == CommandResult.SUCCESS])
        logger.info(f"Emergency stop: {successful_stops}/{len(results)} devices stopped")
        
        return results
    
    def shutdown(self):
        """Shutdown the command sender and clean up resources."""
        logger.info("Shutting down CommandSender")
        self.stop_queue_processor()
        self.executor.shutdown(wait=True)
        logger.info("CommandSender shutdown complete")

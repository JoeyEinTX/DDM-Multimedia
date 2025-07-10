"""
Logging utilities for DDM Racing System
"""

import logging
import os
from datetime import datetime
from flask import current_app

def get_logger(name=None):
    """
    Get a logger instance.
    
    Args:
        name (str): Logger name. If None, uses the calling module name.
    
    Returns:
        logging.Logger: Configured logger instance
    """
    if name is None:
        # Get the caller's module name
        import inspect
        frame = inspect.currentframe().f_back
        name = frame.f_globals.get('__name__', 'ddm')
    
    logger = logging.getLogger(name)
    
    # If we're in a Flask app context, use app logger level
    try:
        if current_app:
            logger.setLevel(current_app.logger.level)
    except RuntimeError:
        # Not in app context, use default INFO level
        logger.setLevel(logging.INFO)
    
    return logger


def log_device_command(device_id, command, parameters=None, success=True):
    """
    Log device command execution.
    
    Args:
        device_id (str): ESP32 device identifier
        command (str): Command that was executed
        parameters (dict): Command parameters
        success (bool): Whether command was successful
    """
    logger = get_logger('ddm.device_commands')
    
    status = "SUCCESS" if success else "FAILED"
    param_str = f" with {parameters}" if parameters else ""
    
    logger.info(f"Device {device_id}: {command}{param_str} - {status}")


def log_user_action(user_role, action, details=None):
    """
    Log user action for audit trail.
    
    Args:
        user_role (str): 'admin' or 'guest'
        action (str): Action description
        details (dict): Additional details
    """
    logger = get_logger('ddm.user_actions')
    
    detail_str = f" - {details}" if details else ""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    logger.info(f"[{timestamp}] {user_role.upper()}: {action}{detail_str}")


def log_system_event(event_type, message, level='info'):
    """
    Log system-level events.
    
    Args:
        event_type (str): Type of event (startup, shutdown, error, etc.)
        message (str): Event message
        level (str): Log level ('debug', 'info', 'warning', 'error', 'critical')
    """
    logger = get_logger('ddm.system')
    
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(f"[{event_type.upper()}] {message}")


def log_performance(operation, duration_ms, details=None):
    """
    Log performance metrics.
    
    Args:
        operation (str): Operation name
        duration_ms (float): Duration in milliseconds
        details (dict): Additional details
    """
    logger = get_logger('ddm.performance')
    
    detail_str = f" - {details}" if details else ""
    logger.info(f"Performance: {operation} took {duration_ms:.2f}ms{detail_str}")

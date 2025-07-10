"""
DDM Racing System

A Flask-based LED lighting control platform for horse racing-themed events.
Provides admin and guest interfaces for controlling ESP32-based LED displays.
"""

from flask import Flask
from flask_socketio import SocketIO
import os
import logging
from logging.handlers import RotatingFileHandler

# Initialize extensions
socketio = SocketIO()

def create_app(config_name=None):
    """
    Application factory pattern.
    
    Args:
        config_name (str): Configuration name ('development', 'production', 'testing')
    
    Returns:
        Flask: Configured Flask application
    """
    app = Flask(__name__)
    
    # Load configuration
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')
    
    from config import config
    app.config.from_object(config[config_name])
    
    # Configure logging
    configure_logging(app)
    
    # Initialize OpenAI client
    initialize_openai_client(app)
    
    # Initialize extensions
    socketio.init_app(
        app,
        cors_allowed_origins=app.config['SOCKETIO_CORS_ALLOWED_ORIGINS'],
        logger=app.config['SOCKETIO_LOGGER'],
        engineio_logger=app.config['SOCKETIO_LOGGER'],
        async_mode='threading'  # Use threading instead of eventlet for Python 3.12 compatibility
    )
    
    # Register blueprints
    register_blueprints(app)
    
    # Register error handlers
    register_error_handlers(app)
    
    # Log startup
    app.logger.info(f"DDM Racing System starting in {config_name} mode")
    
    return app


def configure_logging(app):
    """Configure application logging."""
    if not app.debug and not app.testing:
        # Create logs directory if it doesn't exist
        log_dir = os.path.dirname(app.config['LOG_FILE'])
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # Set up file handler
        file_handler = RotatingFileHandler(
            app.config['LOG_FILE'],
            maxBytes=10240000,  # 10MB
            backupCount=10
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(getattr(logging, app.config['LOG_LEVEL']))
        app.logger.addHandler(file_handler)
    
    app.logger.setLevel(getattr(logging, app.config['LOG_LEVEL']))


def register_blueprints(app):
    """Register Flask blueprints."""
    # Import blueprints
    from ddm.ui.admin import admin_ui
    from ddm.ui.guest import guest_ui
    from ddm.api.admin import admin_api
    from ddm.api.guest import guest_api
    from ddm.api.devices import devices_api
    from ddm.api.status import status_api
    
    # Register UI blueprints
    app.register_blueprint(admin_ui, url_prefix='/admin')
    app.register_blueprint(guest_ui, url_prefix='/guest')
    
    # Register API blueprints
    app.register_blueprint(admin_api, url_prefix='/api/admin')
    app.register_blueprint(guest_api, url_prefix='/api/guest')
    app.register_blueprint(devices_api, url_prefix='/api/devices')
    app.register_blueprint(status_api, url_prefix='/api/status')
    
    # Register WebSocket events
    from ddm.websocket import events


def register_error_handlers(app):
    """Register error handlers."""
    from ddm.utils.errors import register_error_handlers as register_handlers
    register_handlers(app)

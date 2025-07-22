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


def initialize_openai_client(app):
    """Initialize OpenAI client with app configuration."""
    try:
        from ddm.utils.openai_client import initialize_openai
        
        # Extract OpenAI configuration from app config
        openai_config = {
            'OPENAI_API_KEY': app.config.get('OPENAI_API_KEY'),
            'OPENAI_MODEL': app.config.get('OPENAI_MODEL'),
            'OPENAI_MAX_TOKENS': app.config.get('OPENAI_MAX_TOKENS'),
            'OPENAI_TEMPERATURE': app.config.get('OPENAI_TEMPERATURE')
        }
        
        if openai_config['OPENAI_API_KEY']:
            success = initialize_openai(openai_config)
            if success:
                app.logger.info("OpenAI client initialized successfully")
            else:
                app.logger.warning("Failed to initialize OpenAI client")
        else:
            app.logger.warning("OpenAI API key not found - AI features disabled")
            
    except ImportError as e:
        app.logger.warning(f"OpenAI dependencies not installed: {e}")
    except Exception as e:
        app.logger.error(f"Error initializing OpenAI client: {e}")


def register_blueprints(app):
    """Register Flask blueprints."""
    # Import blueprints
    from ddm.ui.admin import admin_ui
    from ddm.ui.guest import guest_ui
    from ddm.api.admin import admin_api
    from ddm.api.guest import guest_api
    from ddm.api.devices import devices_api
    from ddm.api.status import status_api
    from ddm.api.openai_api import openai_api
    from ddm.api.display import display_bp
    from ddm.api.race import race_api
    from ddm.api.weather import weather_bp
    
    # Add root route - redirect to admin for development
    from flask import redirect, url_for
    
    @app.route('/')
    def index():
        return redirect(url_for('admin_ui.dashboard'))
    
    # Register UI blueprints
    app.register_blueprint(admin_ui, url_prefix='/admin')
    app.register_blueprint(guest_ui, url_prefix='/guest')
    
    # Register API blueprints
    app.register_blueprint(admin_api, url_prefix='/api/admin')
    app.register_blueprint(guest_api, url_prefix='/api/guest')
    app.register_blueprint(devices_api, url_prefix='/api/devices')
    app.register_blueprint(status_api, url_prefix='/api/status')
    app.register_blueprint(openai_api, url_prefix='/api/openai')
    app.register_blueprint(display_bp, url_prefix='/api/display')
    app.register_blueprint(race_api, url_prefix='/api/race')
    app.register_blueprint(weather_bp, url_prefix='/api/weather')
    
    # Register WebSocket events
    from ddm.websocket import events


def register_error_handlers(app):
    """Register error handlers."""
    from ddm.utils.errors import register_error_handlers as register_handlers
    register_handlers(app)

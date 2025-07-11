import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    """Base configuration class."""
    
    # Flask configuration
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    
    # Server configuration
    HOST = os.environ.get('HOST', '0.0.0.0')
    PORT = int(os.environ.get('PORT', 5000))
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 'yes']
    
    # SocketIO configuration
    SOCKETIO_CORS_ALLOWED_ORIGINS = os.environ.get('SOCKETIO_CORS_ALLOWED_ORIGINS', '*')
    SOCKETIO_LOGGER = os.environ.get('SOCKETIO_LOGGER', 'False').lower() in ['true', '1', 'yes']
    
    # ESP32 configuration
    ESP32_NETWORK_RANGE = os.environ.get('ESP32_NETWORK_RANGE', '192.168.1.0/24')
    ESP32_DISCOVERY_PORT = int(os.environ.get('ESP32_DISCOVERY_PORT', 8080))
    ESP32_COMMAND_TIMEOUT = int(os.environ.get('ESP32_COMMAND_TIMEOUT', 5))
    
    # Security configuration
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
    SESSION_TIMEOUT = int(os.environ.get('SESSION_TIMEOUT', 3600))
    
    # LED configuration
    DEFAULT_LED_COUNT = int(os.environ.get('DEFAULT_LED_COUNT', 50))
    DEFAULT_BRIGHTNESS = int(os.environ.get('DEFAULT_BRIGHTNESS', 128))
    DEFAULT_COLOR_ORDER = os.environ.get('DEFAULT_COLOR_ORDER', 'RGB')
    
    # Logging configuration
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = os.environ.get('LOG_FILE', 'logs/ddm_racing.log')
    
    # Database configuration (future)
    DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///ddm_racing.db')
    
    # OpenAI configuration
    OPENAI_ENABLED = os.environ.get('OPENAI_ENABLED', 'False').lower() in ['true', '1', 'yes']
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-3.5-turbo')
    OPENAI_MAX_TOKENS = int(os.environ.get('OPENAI_MAX_TOKENS', 1000))
    OPENAI_TEMPERATURE = float(os.environ.get('OPENAI_TEMPERATURE', 0.7))
    
    # Credit protection settings
    OPENAI_MAX_REQUESTS_PER_HOUR = int(os.environ.get('OPENAI_MAX_REQUESTS_PER_HOUR', 50))
    OPENAI_MAX_REQUESTS_PER_DAY = int(os.environ.get('OPENAI_MAX_REQUESTS_PER_DAY', 200))
    OPENAI_DEMO_MODE = os.environ.get('OPENAI_DEMO_MODE', 'False').lower() in ['true', '1', 'yes']
    
    # UI Mode → Animation Mapping
    UI_MODE_MAPPING = {
        "pre_race": [
            "welcome_animation",
            "system_test",
            "standby_mode",
            "demo_mode"
        ],
        "betting_open": [
            "sixty_min_warning",
            "thirty_min_urgency",
            "final_call"
        ],
        "during_race": [
            "theyre_off",
            "down_the_stretch",
            "finish_race"
        ],
        "results": [
            "after_race_party",
            "race_results"
        ]
    }


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True
    LOG_LEVEL = 'DEBUG'


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False
    LOG_LEVEL = 'INFO'


class TestingConfig(Config):
    """Testing configuration."""
    TESTING = True
    DEBUG = True
    LOG_LEVEL = 'DEBUG'


# Configuration dictionary
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}

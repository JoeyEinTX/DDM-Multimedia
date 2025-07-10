"""OpenAI integration utilities for DDM Racing System."""

import os
import logging
import time
from typing import Optional, Dict, Any, List
from collections import defaultdict
from datetime import datetime, timedelta
from openai import OpenAI
from ddm.utils.logger import get_logger

logger = get_logger(__name__)


class OpenAIClient:
    """OpenAI client for the DDM Racing System with rate limiting and toggle control."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize OpenAI client with configuration."""
        self.config = config or {}
        self.enabled = self.config.get('OPENAI_ENABLED', False)
        self.demo_mode = self.config.get('OPENAI_DEMO_MODE', False)
        
        # Rate limiting
        self.max_requests_per_hour = self.config.get('OPENAI_MAX_REQUESTS_PER_HOUR', 50)
        self.max_requests_per_day = self.config.get('OPENAI_MAX_REQUESTS_PER_DAY', 200)
        self.request_counts = defaultdict(int)
        self.last_reset = datetime.now()
        
        if not self.enabled:
            logger.info("OpenAI integration is DISABLED")
            return
            
        self.api_key = self.config.get('OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
        
        if not self.api_key:
            logger.warning("OpenAI API key not found. Features will be disabled.")
            self.enabled = False
            return
        
        try:
            self.client = OpenAI(api_key=self.api_key)
            self.model = self.config.get('OPENAI_MODEL', 'gpt-3.5-turbo')
            self.max_tokens = self.config.get('OPENAI_MAX_TOKENS', 1000)
            self.temperature = self.config.get('OPENAI_TEMPERATURE', 0.7)
            
            logger.info(f"OpenAI client initialized with model: {self.model}")
            logger.info(f"Rate limits: {self.max_requests_per_hour}/hour, {self.max_requests_per_day}/day")
            
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")
            self.enabled = False
    
    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = datetime.now()
        
        # Reset counters if needed
        if now - self.last_reset > timedelta(hours=1):
            self.request_counts.clear()
            self.last_reset = now
        
        # Check limits
        hourly_key = now.strftime('%Y-%m-%d-%H')
        daily_key = now.strftime('%Y-%m-%d')
        
        if self.request_counts[hourly_key] >= self.max_requests_per_hour:
            logger.warning(f"Hourly rate limit reached: {self.max_requests_per_hour}")
            return False
            
        if self.request_counts[daily_key] >= self.max_requests_per_day:
            logger.warning(f"Daily rate limit reached: {self.max_requests_per_day}")
            return False
            
        return True
    
    def _increment_usage(self):
        """Increment usage counters."""
        now = datetime.now()
        hourly_key = now.strftime('%Y-%m-%d-%H')
        daily_key = now.strftime('%Y-%m-%d')
        
        self.request_counts[hourly_key] += 1
        self.request_counts[daily_key] += 1
        
        logger.info(f"OpenAI usage: {self.request_counts[hourly_key]}/{self.max_requests_per_hour} hourly, "
                   f"{self.request_counts[daily_key]}/{self.max_requests_per_day} daily")
    
    def _get_demo_response(self, request_type: str) -> str:
        """Return demo responses when in demo mode."""
        demo_responses = {
            'led_sequence': '{"pattern": "rainbow_chase", "colors": ["#FF0000", "#00FF00", "#0000FF"], "timing": 100, "brightness": 128}',
            'racing_mode': '{"mode": "Sprint Race", "difficulty": "medium", "duration": 60, "features": ["lap_counter", "speed_boost"]}',
            'race_analysis': 'Great race! Your lap times are improving. Consider taking turns 3-4 more aggressively for better times.',
            'chat': 'This is a demo response. OpenAI is currently disabled to save credits.'
        }
        return demo_responses.get(request_type, 'Demo mode active - OpenAI disabled')
    
    def chat_completion(self, messages: List[Dict[str, Any]], **kwargs) -> Optional[str]:
        """Get a chat completion from OpenAI with rate limiting and toggle control."""
        if not self.enabled:
            logger.info("OpenAI is disabled - returning demo response")
            return self._get_demo_response('chat')
        
        if self.demo_mode:
            logger.info("Demo mode active - returning demo response")
            return self._get_demo_response('chat')
        
        if not self._check_rate_limit():
            logger.warning("Rate limit exceeded - returning demo response")
            return self._get_demo_response('chat')
        
        try:
            self._increment_usage()
            
            response = self.client.chat.completions.create(
                model=kwargs.get('model', self.model),
                messages=messages,
                max_tokens=kwargs.get('max_tokens', self.max_tokens),
                temperature=kwargs.get('temperature', self.temperature),
                **{k: v for k, v in kwargs.items() if k not in ['model', 'max_tokens', 'temperature']}
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return None
    
    def generate_led_sequence(self, description: str) -> Optional[str]:
        """Generate LED sequence code based on description."""
        if not self.enabled:
            logger.info("OpenAI is disabled - returning demo LED sequence")
            return self._get_demo_response('led_sequence')
        
        if self.demo_mode:
            logger.info("Demo mode active - returning demo LED sequence")
            return self._get_demo_response('led_sequence')
        
        if not self._check_rate_limit():
            logger.warning("Rate limit exceeded - returning demo LED sequence")
            return self._get_demo_response('led_sequence')
        
        messages = [
            {
                "role": "system",
                "content": "You are an expert in LED programming for racing systems. Generate JSON code for LED sequences based on descriptions. Return only valid JSON with 'pattern', 'colors', 'timing', and 'brightness' fields."
            },
            {
                "role": "user",
                "content": f"Generate an LED sequence for: {description}"
            }
        ]
        
        return self.chat_completion(messages, max_tokens=500)
    
    def suggest_racing_mode(self, preferences: Dict[str, Any]) -> Optional[str]:
        """Suggest racing mode based on user preferences."""
        if not self.enabled:
            logger.info("OpenAI is disabled - returning demo racing mode")
            return self._get_demo_response('racing_mode')
        
        if self.demo_mode:
            logger.info("Demo mode active - returning demo racing mode")
            return self._get_demo_response('racing_mode')
        
        if not self._check_rate_limit():
            logger.warning("Rate limit exceeded - returning demo racing mode")
            return self._get_demo_response('racing_mode')
        
        messages = [
            {
                "role": "system",
                "content": "You are a racing game expert. Suggest optimal racing modes based on user preferences. Return JSON with 'mode', 'difficulty', 'duration', and 'features' fields."
            },
            {
                "role": "user",
                "content": f"Suggest a racing mode for preferences: {preferences}"
            }
        ]
        
        return self.chat_completion(messages, max_tokens=300)
    
    def analyze_race_data(self, race_data: Dict[str, Any]) -> Optional[str]:
        """Analyze race data and provide insights."""
        if not self.enabled:
            logger.info("OpenAI is disabled - returning demo race analysis")
            return self._get_demo_response('race_analysis')
        
        if self.demo_mode:
            logger.info("Demo mode active - returning demo race analysis")
            return self._get_demo_response('race_analysis')
        
        if not self._check_rate_limit():
            logger.warning("Rate limit exceeded - returning demo race analysis")
            return self._get_demo_response('race_analysis')
        
        messages = [
            {
                "role": "system",
                "content": "You are a racing performance analyst. Analyze race data and provide insights, recommendations, and performance tips."
            },
            {
                "role": "user",
                "content": f"Analyze this race data: {race_data}"
            }
        ]
        
        return self.chat_completion(messages, max_tokens=800)
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Get current usage statistics."""
        now = datetime.now()
        hourly_key = now.strftime('%Y-%m-%d-%H')
        daily_key = now.strftime('%Y-%m-%d')
        
        return {
            'enabled': self.enabled,
            'demo_mode': self.demo_mode,
            'hourly_usage': self.request_counts[hourly_key],
            'daily_usage': self.request_counts[daily_key],
            'hourly_limit': self.max_requests_per_hour,
            'daily_limit': self.max_requests_per_day,
            'hourly_remaining': max(0, self.max_requests_per_hour - self.request_counts[hourly_key]),
            'daily_remaining': max(0, self.max_requests_per_day - self.request_counts[daily_key])
        }


# Global OpenAI client instance
_openai_client: Optional[OpenAIClient] = None


def get_openai_client(config: Optional[Dict[str, Any]] = None) -> OpenAIClient:
    """Get or create the global OpenAI client instance."""
    global _openai_client
    
    if _openai_client is None:
        _openai_client = OpenAIClient(config)
    
    return _openai_client


def initialize_openai(config: Dict[str, Any]) -> bool:
    """Initialize OpenAI client with configuration."""
    try:
        global _openai_client
        _openai_client = OpenAIClient(config)
        logger.info("OpenAI client initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize OpenAI client: {e}")
        return False


def toggle_openai(enabled: bool) -> bool:
    """Toggle OpenAI on/off at runtime."""
    global _openai_client
    
    if _openai_client:
        _openai_client.enabled = enabled
        logger.info(f"OpenAI {'enabled' if enabled else 'disabled'}")
        return True
    
    return False


def set_demo_mode(demo_mode: bool) -> bool:
    """Enable/disable demo mode."""
    global _openai_client
    
    if _openai_client:
        _openai_client.demo_mode = demo_mode
        logger.info(f"Demo mode {'enabled' if demo_mode else 'disabled'}")
        return True
    
    return False

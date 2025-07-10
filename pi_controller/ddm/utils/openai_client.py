"""OpenAI integration utilities for DDM Racing System."""

import os
import logging
from typing import Optional, Dict, Any, List
from openai import OpenAI
from ddm.utils.logger import get_logger

logger = get_logger(__name__)


class OpenAIClient:
    """OpenAI client for the DDM Racing System."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize OpenAI client with configuration."""
        self.config = config or {}
        self.api_key = self.config.get('OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY')
        
        if not self.api_key:
            raise ValueError("OpenAI API key not found. Please set OPENAI_API_KEY in your environment variables.")
        
        self.client = OpenAI(api_key=self.api_key)
        self.model = self.config.get('OPENAI_MODEL', 'gpt-3.5-turbo')
        self.max_tokens = self.config.get('OPENAI_MAX_TOKENS', 1000)
        self.temperature = self.config.get('OPENAI_TEMPERATURE', 0.7)
        
        logger.info(f"OpenAI client initialized with model: {self.model}")
    
    def chat_completion(self, messages: List[Dict[str, str]], **kwargs) -> Optional[str]:
        """
        Get a chat completion from OpenAI.
        
        Args:
            messages: List of message dictionaries with 'role' and 'content'
            **kwargs: Additional parameters for the API call
            
        Returns:
            The assistant's response or None if failed
        """
        try:
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
        """
        Generate LED sequence code based on description.
        
        Args:
            description: Natural language description of the LED effect
            
        Returns:
            Generated LED sequence code or None if failed
        """
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
        """
        Suggest racing mode based on user preferences.
        
        Args:
            preferences: Dictionary of user preferences
            
        Returns:
            Suggested racing mode configuration or None if failed
        """
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
        """
        Analyze race data and provide insights.
        
        Args:
            race_data: Dictionary containing race performance data
            
        Returns:
            Analysis and recommendations or None if failed
        """
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


# Global OpenAI client instance
_openai_client: Optional[OpenAIClient] = None


def get_openai_client(config: Optional[Dict[str, Any]] = None) -> OpenAIClient:
    """Get or create the global OpenAI client instance."""
    global _openai_client
    
    if _openai_client is None:
        _openai_client = OpenAIClient(config)
    
    return _openai_client


def initialize_openai(config: Dict[str, Any]) -> bool:
    """
    Initialize OpenAI client with configuration.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        True if initialization successful, False otherwise
    """
    try:
        global _openai_client
        _openai_client = OpenAIClient(config)
        logger.info("OpenAI client initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize OpenAI client: {e}")
        return False

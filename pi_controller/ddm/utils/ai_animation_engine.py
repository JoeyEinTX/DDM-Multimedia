"""
AI-Powered Racing Animation Generator for DDM Racing System
"""

import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum
from dataclasses import dataclass, field

from ddm.utils.logger import get_logger
from ddm.utils.openai_client import get_openai_client

logger = get_logger(__name__)


class RacingPhase(Enum):
    """Racing event phases for animation generation."""
    PRE_RACE = "pre_race"
    BETTING_OPEN = "betting_open"
    RACE_START = "race_start"
    DURING_RACE = "during_race"
    PHOTO_FINISH = "photo_finish"
    VICTORY = "victory"
    CELEBRATION = "celebration"
    COOL_DOWN = "cool_down"


@dataclass
class AnimationSpec:
    """Specification for generating an animation."""
    name: str
    phase: RacingPhase
    description: str
    duration_ms: int = 5000
    led_count: int = 50
    intensity: str = "medium"
    colors: List[str] = field(default_factory=list)


class RacingAnimationGenerator:
    """AI-powered generator for racing LED animations."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the animation generator."""
        self.config = config or {}
        self.openai_client = get_openai_client(config)
        self.generated_animations = {}
        
        logger.info("Racing Animation Generator initialized")
    
    def generate_all_racing_animations(self) -> Dict[str, Any]:
        """Generate a complete library of racing animations."""
        logger.info("Starting generation of complete racing animation library...")
        
        animation_library = {
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "led_type": "WS2812B",
                "color_order": "RGB",
                "default_brightness": 200,
                "frame_rate": 60
            },
            "animations": {}
        }
        
        animation_specs = self._get_animation_specifications()
        
        for spec in animation_specs:
            logger.info(f"Generating animation: {spec.name}")
            animation = self._generate_single_animation(spec)
            if animation:
                animation_library["animations"][spec.name] = animation
                time.sleep(1)
        
        self._save_animation_library(animation_library)
        
        logger.info(f"Generated {len(animation_library['animations'])} racing animations")
        return animation_library
    
    def _get_animation_specifications(self) -> List[AnimationSpec]:
        """Define all the racing animations we want to generate."""
        return [
            AnimationSpec(
                name="warm_up_glow",
                phase=RacingPhase.PRE_RACE,
                description="Gentle warm-up glow that gradually increases in brightness",
                duration_ms=8000,
                intensity="low",
                colors=["#4A90E2", "#7ED321", "#F5A623"]
            ),
            AnimationSpec(
                name="anticipation_pulse",
                phase=RacingPhase.PRE_RACE,
                description="Building anticipation with synchronized pulsing",
                duration_ms=10000,
                intensity="medium",
                colors=["#9013FE", "#FF6B6B", "#4ECDC4"]
            ),
            AnimationSpec(
                name="countdown_sequence",
                phase=RacingPhase.PRE_RACE,
                description="Dramatic countdown from 10 to 1 with color changes",
                duration_ms=12000,
                intensity="high",
                colors=["#FF3B30", "#FF9500", "#FFCC02", "#30D158"]
            ),
            AnimationSpec(
                name="betting_open",
                phase=RacingPhase.BETTING_OPEN,
                description="Inviting betting open animation with flowing colors",
                duration_ms=6000,
                intensity="medium",
                colors=["#32D74B", "#007AFF", "#FF9F0A"]
            ),
            AnimationSpec(
                name="betting_warning",
                phase=RacingPhase.BETTING_OPEN,
                description="Urgent flashing to indicate betting closing soon",
                duration_ms=5000,
                intensity="high",
                colors=["#FF9500", "#FF3B30"]
            ),
            AnimationSpec(
                name="betting_last_call",
                phase=RacingPhase.BETTING_OPEN,
                description="Final dramatic call for bets with intense flashing",
                duration_ms=4000,
                intensity="high",
                colors=["#FF3B30", "#FFFFFF"]
            )
        ]
    
    def _generate_single_animation(self, spec: AnimationSpec) -> Optional[Dict[str, Any]]:
        """Generate a single animation using AI."""
        prompt = self._build_animation_prompt(spec)
        
        try:
            result = self.openai_client.generate_led_sequence(prompt)
            if result:
                animation_data = json.loads(result) if isinstance(result, str) else result
                
                enhanced_animation = {
                    "name": spec.name,
                    "phase": spec.phase.value,
                    "description": spec.description,
                    "duration_ms": spec.duration_ms,
                    "led_count": spec.led_count,
                    "intensity": spec.intensity,
                    "suggested_colors": spec.colors,
                    "generated_at": datetime.now().isoformat(),
                    "animation_data": animation_data
                }
                
                return enhanced_animation
                
        except Exception as e:
            logger.error(f"Failed to generate animation {spec.name}: {e}")
        
        return None
    
    def _build_animation_prompt(self, spec: AnimationSpec) -> str:
        """Build a detailed prompt for AI animation generation."""
        color_list = ", ".join(spec.colors) if spec.colors else "racing colors"
        
        prompt = f"""
Create a professional LED animation for a horse racing event.

Animation Details:
- Name: {spec.name}
- Phase: {spec.phase.value}
- Description: {spec.description}
- Duration: {spec.duration_ms}ms
- LED Count: {spec.led_count}
- Intensity: {spec.intensity}
- Colors to use: {color_list}

Technical Requirements:
- LED Type: WS2812B (RGB)
- Frame Rate: 60 FPS
- Brightness Range: 0-255
- Color Format: Hex (#RRGGBB)

Return a JSON structure for the animation pattern.
        """
        
        return prompt.strip()
    
    def _save_animation_library(self, library: Dict[str, Any]):
        """Save the generated animation library to a file."""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"racing_animations_{timestamp}.json"
            base_path = "/workspaces/DDM-Multimedia/pi_controller/ddm/static/data"
            filepath = f"{base_path}/{filename}"
            
            import os
            os.makedirs(base_path, exist_ok=True)
            
            with open(filepath, 'w') as f:
                json.dump(library, f, indent=2)
            
            logger.info(f"Animation library saved to: {filepath}")
            
            latest_filepath = f"{base_path}/racing_animations_latest.json"
            with open(latest_filepath, 'w') as f:
                json.dump(library, f, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save animation library: {e}")


def generate_racing_animations(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Generate the complete racing animation library."""
    generator = RacingAnimationGenerator(config)
    return generator.generate_all_racing_animations()

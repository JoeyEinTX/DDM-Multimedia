"""
Race State Models for DDM Racing System

Simple performative race state management for party LED displays.
Handles basic race phases: Pre-Race, During Race, After Race.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Any, Callable
from enum import Enum

from ddm.utils.logger import get_logger

logger = get_logger(__name__)


class RacePhase(Enum):
    """Race phase enumeration for party flow."""
    IDLE = "idle"
    PRE_RACE = "pre_race"
    DURING_RACE = "during_race"
    AFTER_RACE = "after_race"
    PAUSED = "paused"


class RaceStatus(Enum):
    """Overall race status."""
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


@dataclass
class RaceConfig:
    """Configuration for a race."""
    race_id: str
    race_name: str = "Derby de Mayo Race"
    
    # Phase durations (in seconds)
    pre_race_duration: int = 30
    race_duration: int = 45
    after_race_duration: int = 30
    
    # LED settings
    animation_speed: float = 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "race_id": self.race_id,
            "race_name": self.race_name,
            "pre_race_duration": self.pre_race_duration,
            "race_duration": self.race_duration,
            "after_race_duration": self.after_race_duration,
            "animation_speed": self.animation_speed
        }


@dataclass
class RaceState:
    """Current state of the race system."""
    config: RaceConfig
    current_phase: RacePhase = RacePhase.IDLE
    race_status: RaceStatus = RaceStatus.STOPPED
    
    # Timing
    phase_start_time: Optional[datetime] = None
    phase_duration: int = 0
    total_race_time: float = 0.0
    
    # Callbacks for LED animations
    phase_change_callbacks: list[Callable] = field(default_factory=list)
    
    def get_phase_remaining_time(self) -> float:
        """Get remaining time in current phase."""
        if not self.phase_start_time:
            return 0.0
        
        elapsed = (datetime.now() - self.phase_start_time).total_seconds()
        return max(0, self.phase_duration - elapsed)
    
    def get_phase_progress(self) -> float:
        """Get progress through current phase (0-1)."""
        if not self.phase_start_time or self.phase_duration == 0:
            return 0.0
        
        elapsed = (datetime.now() - self.phase_start_time).total_seconds()
        return min(1.0, elapsed / self.phase_duration)
    
    def add_phase_change_callback(self, callback: Callable):
        """Add callback for phase changes."""
        self.phase_change_callbacks.append(callback)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert race state to dictionary."""
        return {
            "config": self.config.to_dict(),
            "current_phase": self.current_phase.value,
            "race_status": self.race_status.value,
            "phase_start_time": self.phase_start_time.isoformat() if self.phase_start_time else None,
            "phase_duration": self.phase_duration,
            "phase_remaining_time": self.get_phase_remaining_time(),
            "phase_progress": self.get_phase_progress(),
            "total_race_time": self.total_race_time
        }

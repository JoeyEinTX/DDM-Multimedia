"""
Race State Models for DDM Racing System

This module defines the race state management system for Derby de Mayo party events.
Handles Pre-Race, Betting Open, During Race, and After Race phases.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
import json
import time

from ddm.utils.logger import get_logger

logger = get_logger(__name__)


class RacePhase(Enum):
    """Race phase enumeration for party flow."""
    IDLE = "idle"
    PRE_RACE = "pre_race"
    BETTING_OPEN = "betting_open"
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
class Horse:
    """Represents a horse in the race."""
    horse_id: str
    name: str
    number: int
    color: str = "#FF0000"  # LED color for this horse
    odds: float = 1.0  # Betting odds
    position: float = 0.0  # Current race position (0-100%)
    speed: float = 1.0  # Base speed multiplier
    stamina: float = 1.0  # Stamina affects speed over time
    current_speed: float = 1.0  # Current speed
    lane: int = 0  # LED lane/strip assignment
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert horse to dictionary."""
        return {
            "horse_id": self.horse_id,
            "name": self.name,
            "number": self.number,
            "color": self.color,
            "odds": self.odds,
            "position": self.position,
            "speed": self.speed,
            "stamina": self.stamina,
            "current_speed": self.current_speed,
            "lane": self.lane
        }


@dataclass
class Bet:
    """Represents a guest bet on a horse."""
    bet_id: str
    guest_name: str
    horse_id: str
    amount: float = 0.0  # Fun money, not real
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert bet to dictionary."""
        return {
            "bet_id": self.bet_id,
            "guest_name": self.guest_name,
            "horse_id": self.horse_id,
            "amount": self.amount,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class RaceConfig:
    """Configuration for a race."""
    race_id: str
    race_name: str = "Derby de Mayo Race"
    
    # Phase durations (in seconds)
    pre_race_duration: int = 30
    betting_duration: int = 60
    race_duration: int = 45
    after_race_duration: int = 30
    
    # Race settings
    num_horses: int = 6
    track_length: int = 100  # Conceptual length
    randomness: float = 0.3  # How much randomness in race
    
    # LED settings
    update_interval: float = 0.1  # How often to update LED positions
    animation_speed: float = 1.0  # Speed multiplier for animations
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "race_id": self.race_id,
            "race_name": self.race_name,
            "pre_race_duration": self.pre_race_duration,
            "betting_duration": self.betting_duration,
            "race_duration": self.race_duration,
            "after_race_duration": self.after_race_duration,
            "num_horses": self.num_horses,
            "track_length": self.track_length,
            "randomness": self.randomness,
            "update_interval": self.update_interval,
            "animation_speed": self.animation_speed
        }


@dataclass
class RaceState:
    """Current state of the race system."""
    config: RaceConfig
    current_phase: RacePhase = RacePhase.IDLE
    race_status: RaceStatus = RaceStatus.STOPPED
    horses: List[Horse] = field(default_factory=list)
    bets: List[Bet] = field(default_factory=list)
    
    # Timing
    phase_start_time: Optional[datetime] = None
    phase_duration: int = 0
    total_race_time: float = 0.0
    
    # Results
    winner: Optional[Horse] = None
    race_results: List[Horse] = field(default_factory=list)
    
    # Callbacks
    phase_change_callbacks: List[Callable] = field(default_factory=list)
    position_update_callbacks: List[Callable] = field(default_factory=list)
    
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
    
    def add_position_update_callback(self, callback: Callable):
        """Add callback for position updates."""
        self.position_update_callbacks.append(callback)
    
    def get_horse_by_id(self, horse_id: str) -> Optional[Horse]:
        """Get horse by ID."""
        return next((h for h in self.horses if h.horse_id == horse_id), None)
    
    def get_horse_by_number(self, number: int) -> Optional[Horse]:
        """Get horse by number."""
        return next((h for h in self.horses if h.number == number), None)
    
    def get_bets_for_horse(self, horse_id: str) -> List[Bet]:
        """Get all bets for a specific horse."""
        return [bet for bet in self.bets if bet.horse_id == horse_id]
    
    def get_total_bets_for_horse(self, horse_id: str) -> float:
        """Get total bet amount for a horse."""
        return sum(bet.amount for bet in self.get_bets_for_horse(horse_id))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert race state to dictionary."""
        return {
            "config": self.config.to_dict(),
            "current_phase": self.current_phase.value,
            "race_status": self.race_status.value,
            "horses": [horse.to_dict() for horse in self.horses],
            "bets": [bet.to_dict() for bet in self.bets],
            "phase_start_time": self.phase_start_time.isoformat() if self.phase_start_time else None,
            "phase_duration": self.phase_duration,
            "phase_remaining_time": self.get_phase_remaining_time(),
            "phase_progress": self.get_phase_progress(),
            "total_race_time": self.total_race_time,
            "winner": self.winner.to_dict() if self.winner else None,
            "race_results": [horse.to_dict() for horse in self.race_results]
        }

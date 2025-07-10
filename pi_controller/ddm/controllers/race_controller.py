"""
Race Controller for DDM Racing System

This module manages the race state machine, timing, and transitions between
Pre-Race, Betting Open, During Race, and After Race phases.
"""

import threading
import time
import random
import math
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime, timedelta

from ddm.models.race_state import RaceState, RaceConfig, RacePhase, RaceStatus, Horse, Bet
from ddm.utils.logger import get_logger, log_system_event
from ddm.utils.helpers import timing_decorator

logger = get_logger(__name__)


class RaceController:
    """
    Controls the race state machine and timing for Derby de Mayo party events.
    
    Manages transitions between:
    - Pre-Race: Setup and anticipation
    - Betting Open: Guests place bets
    - During Race: Simulated race with LED animations
    - After Race: Results and celebration
    """
    
    def __init__(self, socketio=None, device_manager=None):
        """Initialize the race controller."""
        self.socketio = socketio
        self.device_manager = device_manager
        self.race_state: Optional[RaceState] = None
        self.race_thread: Optional[threading.Thread] = None
        self.position_thread: Optional[threading.Thread] = None
        self.running = False
        
        # Default horse names for Derby de Mayo theme
        self.default_horse_names = [
            "Cinco Lightning", "Taco Thunder", "Margarita Magic",
            "Sombrero Storm", "Fiesta Flash", "Piñata Power",
            "Salsa Speed", "Mariachi Might", "Corona Comet", "Jalapeño Jet"
        ]
        
        # Default horse colors
        self.default_horse_colors = [
            "#FF0000", "#00FF00", "#0000FF", "#FFFF00", 
            "#FF00FF", "#00FFFF", "#FF8000", "#8000FF"
        ]
        
        logger.info("Race controller initialized")
    
    def create_race(self, config: RaceConfig) -> RaceState:
        """Create a new race with the given configuration."""
        # Generate horses
        horses = self._generate_horses(config.num_horses)
        
        # Create race state
        self.race_state = RaceState(
            config=config,
            horses=horses,
            current_phase=RacePhase.IDLE,
            race_status=RaceStatus.STOPPED
        )
        
        # Add default callbacks
        self.race_state.add_phase_change_callback(self._on_phase_change)
        self.race_state.add_position_update_callback(self._on_position_update)
        
        logger.info(f"Created race: {config.race_name} with {len(horses)} horses")
        self._emit_race_update()
        
        return self.race_state
    
    def start_race(self) -> bool:
        """Start the race sequence from Pre-Race phase."""
        if not self.race_state:
            logger.error("Cannot start race: no race configured")
            return False
        
        if self.running:
            logger.warning("Race is already running")
            return False
        
        self.running = True
        self.race_state.race_status = RaceStatus.RUNNING
        
        # Start race thread
        self.race_thread = threading.Thread(target=self._race_sequence, daemon=True)
        self.race_thread.start()
        
        logger.info("Started race sequence")
        self._emit_race_update()
        return True
    
    def stop_race(self) -> bool:
        """Stop the current race."""
        if not self.running:
            return False
        
        self.running = False
        
        if self.race_state:
            self.race_state.race_status = RaceStatus.STOPPED
            self.race_state.current_phase = RacePhase.IDLE
        
        logger.info("Stopped race")
        self._emit_race_update()
        return True
    
    def pause_race(self) -> bool:
        """Pause the current race."""
        if not self.race_state or not self.running:
            return False
        
        self.race_state.race_status = RaceStatus.PAUSED
        self.race_state.current_phase = RacePhase.PAUSED
        
        logger.info("Paused race")
        self._emit_race_update()
        return True
    
    def resume_race(self) -> bool:
        """Resume a paused race."""
        if not self.race_state or self.race_state.race_status != RaceStatus.PAUSED:
            return False
        
        self.race_state.race_status = RaceStatus.RUNNING
        # Resume from previous phase logic would go here
        
        logger.info("Resumed race")
        self._emit_race_update()
        return True
    
    def place_bet(self, guest_name: str, horse_id: str, amount: float = 10.0) -> bool:
        """Place a bet on a horse."""
        if not self.race_state:
            return False
        
        if self.race_state.current_phase != RacePhase.BETTING_OPEN:
            logger.warning(f"Cannot place bet: betting not open (current phase: {self.race_state.current_phase})")
            return False
        
        # Check if horse exists
        horse = self.race_state.get_horse_by_id(horse_id)
        if not horse:
            logger.error(f"Cannot place bet: horse {horse_id} not found")
            return False
        
        # Create bet
        bet = Bet(
            bet_id=f"bet_{len(self.race_state.bets) + 1}",
            guest_name=guest_name,
            horse_id=horse_id,
            amount=amount
        )
        
        self.race_state.bets.append(bet)
        
        logger.info(f"Bet placed: {guest_name} bet ${amount} on {horse.name}")
        self._emit_race_update()
        return True
    
    def get_race_status(self) -> Dict[str, Any]:
        """Get current race status."""
        if not self.race_state:
            return {
                "race_configured": False,
                "race_status": "no_race"
            }
        
        return {
            "race_configured": True,
            "race_state": self.race_state.to_dict(),
            "running": self.running
        }
    
    def _generate_horses(self, count: int) -> List[Horse]:
        """Generate horses for the race."""
        horses = []
        
        for i in range(count):
            horse = Horse(
                horse_id=f"horse_{i + 1}",
                name=self.default_horse_names[i % len(self.default_horse_names)],
                number=i + 1,
                color=self.default_horse_colors[i % len(self.default_horse_colors)],
                odds=round(random.uniform(1.5, 8.0), 1),
                speed=random.uniform(0.8, 1.2),
                stamina=random.uniform(0.8, 1.2),
                current_speed=1.0,
                lane=i
            )
            horses.append(horse)
        
        return horses
    
    def _race_sequence(self):
        """Main race sequence thread."""
        if not self.race_state:
            logger.error("Cannot run race sequence: no race state")
            return
            
        try:
            # Phase 1: Pre-Race
            self._enter_phase(RacePhase.PRE_RACE, self.race_state.config.pre_race_duration)
            self._run_pre_race_phase()
            
            # Phase 2: Betting Open
            self._enter_phase(RacePhase.BETTING_OPEN, self.race_state.config.betting_duration)
            self._run_betting_phase()
            
            # Phase 3: During Race
            self._enter_phase(RacePhase.DURING_RACE, self.race_state.config.race_duration)
            self._run_race_phase()
            
            # Phase 4: After Race
            self._enter_phase(RacePhase.AFTER_RACE, self.race_state.config.after_race_duration)
            self._run_after_race_phase()
            
            # Complete
            self.race_state.race_status = RaceStatus.COMPLETED
            self.race_state.current_phase = RacePhase.IDLE
            self.running = False
            
            logger.info("Race sequence completed")
            self._emit_race_update()
            
        except Exception as e:
            logger.error(f"Error in race sequence: {e}")
            self.stop_race()
    
    def _enter_phase(self, phase: RacePhase, duration: int):
        """Enter a new race phase."""
        if not self.race_state:
            return
            
        self.race_state.current_phase = phase
        self.race_state.phase_start_time = datetime.now()
        self.race_state.phase_duration = duration
        
        logger.info(f"Entered phase: {phase.value} (duration: {duration}s)")
        
        # Trigger callbacks
        for callback in self.race_state.phase_change_callbacks:
            try:
                callback(phase, self.race_state)
            except Exception as e:
                logger.error(f"Error in phase change callback: {e}")
        
        self._emit_race_update()
    
    def _run_pre_race_phase(self):
        """Run the pre-race phase."""
        if not self.race_state:
            return
            
        logger.info("Running pre-race phase: building anticipation")
        
        # Send pre-race animation commands
        self._send_animation_command("pre_race_buildup", {
            "horses": [horse.to_dict() for horse in self.race_state.horses]
        })
        
        # Wait for phase duration
        self._wait_for_phase_completion()
    
    def _run_betting_phase(self):
        """Run the betting phase."""
        if not self.race_state:
            return
            
        logger.info("Running betting phase: accepting bets")
        
        # Send betting animation commands
        self._send_animation_command("betting_open", {
            "horses": [horse.to_dict() for horse in self.race_state.horses]
        })
        
        # Wait for phase duration (bets can be placed during this time)
        self._wait_for_phase_completion()
    
    def _run_race_phase(self):
        """Run the actual race phase."""
        if not self.race_state:
            return
            
        logger.info("Running race phase: horses are racing!")
        
        # Start position update thread
        self.position_thread = threading.Thread(target=self._update_race_positions, daemon=True)
        self.position_thread.start()
        
        # Send race start animation
        self._send_animation_command("race_start", {
            "horses": [horse.to_dict() for horse in self.race_state.horses]
        })
        
        # Wait for race to complete
        self._wait_for_phase_completion()
        
        # Determine winner
        self._determine_race_results()
    
    def _run_after_race_phase(self):
        """Run the after-race phase."""
        if not self.race_state:
            return
            
        logger.info("Running after-race phase: celebrating results")
        
        # Send victory animation
        self._send_animation_command("race_finish", {
            "winner": self.race_state.winner.to_dict() if self.race_state.winner else None,
            "results": [horse.to_dict() for horse in self.race_state.race_results]
        })
        
        # Wait for phase duration
        self._wait_for_phase_completion()
    
    def _update_race_positions(self):
        """Update horse positions during the race."""
        if not self.race_state:
            return
            
        start_time = time.time()
        race_duration = self.race_state.config.race_duration
        update_interval = self.race_state.config.update_interval
        
        while (self.running and 
               self.race_state.current_phase == RacePhase.DURING_RACE and
               time.time() - start_time < race_duration):
            
            elapsed = time.time() - start_time
            progress = elapsed / race_duration
            
            # Update each horse position
            for horse in self.race_state.horses:
                # Add some randomness to the race
                randomness = random.uniform(-self.race_state.config.randomness, 
                                          self.race_state.config.randomness)
                
                # Calculate position with speed, stamina, and randomness
                base_progress = progress * horse.speed
                stamina_effect = horse.stamina * (1 - progress * 0.5)  # Stamina decreases over time
                random_effect = randomness * 0.1
                
                horse.position = min(100, max(0, (base_progress + random_effect) * 100 * stamina_effect))
                horse.current_speed = horse.speed * stamina_effect
            
            # Sort horses by position for leaderboard
            self.race_state.horses.sort(key=lambda h: h.position, reverse=True)
            
            # Trigger position update callbacks
            for callback in self.race_state.position_update_callbacks:
                try:
                    callback(self.race_state.horses)
                except Exception as e:
                    logger.error(f"Error in position update callback: {e}")
            
            # Send position update to LEDs
            self._send_position_update()
            
            time.sleep(update_interval)
    
    def _determine_race_results(self):
        """Determine the race winner and final results."""
        if not self.race_state:
            return
            
        # Sort by final position
        self.race_state.race_results = sorted(self.race_state.horses, 
                                            key=lambda h: h.position, 
                                            reverse=True)
        
        # Set winner
        if self.race_state.race_results:
            self.race_state.winner = self.race_state.race_results[0]
            logger.info(f"Race winner: {self.race_state.winner.name}")
        
        self._emit_race_update()
    
    def _wait_for_phase_completion(self):
        """Wait for the current phase to complete."""
        while (self.running and self.race_state and
               self.race_state.get_phase_remaining_time() > 0):
            time.sleep(0.5)
    
    def _send_animation_command(self, animation_type: str, parameters: Dict[str, Any]):
        """Send animation command to LED devices."""
        if not self.device_manager:
            logger.debug(f"No device manager: would send {animation_type} animation")
            return
        
        # This would send commands to all ESP32 devices
        command_data = {
            "command": animation_type,
            "parameters": parameters
        }
        
        logger.debug(f"Sending animation command: {animation_type}")
        # Implementation would go here when device manager is connected
    
    def _send_position_update(self):
        """Send horse position updates to LED devices."""
        if not self.device_manager or not self.race_state:
            return
        
        positions = [
            {
                "horse_id": horse.horse_id,
                "position": horse.position,
                "color": horse.color,
                "lane": horse.lane
            }
            for horse in self.race_state.horses
        ]
        
        self._send_animation_command("position_update", {"positions": positions})
    
    def _on_phase_change(self, phase: RacePhase, race_state: RaceState):
        """Handle phase change events."""
        logger.info(f"Phase changed to: {phase.value}")
        log_system_event(f"race_phase_change", {"phase": phase.value})
    
    def _on_position_update(self, horses: List[Horse]):
        """Handle position update events."""
        # Emit WebSocket update for real-time position tracking
        self._emit_position_update(horses)
    
    def _emit_race_update(self):
        """Emit race state update via WebSocket."""
        if self.socketio and self.race_state:
            self.socketio.emit('race_update', self.race_state.to_dict())
    
    def _emit_position_update(self, horses: List[Horse]):
        """Emit position update via WebSocket."""
        if self.socketio:
            positions = [
                {
                    "horse_id": horse.horse_id,
                    "name": horse.name,
                    "position": horse.position,
                    "color": horse.color,
                    "current_speed": horse.current_speed
                }
                for horse in horses
            ]
            self.socketio.emit('position_update', {"positions": positions})

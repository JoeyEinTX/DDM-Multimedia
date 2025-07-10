"""
Race Controller for DDM Racing System

Simple performative race controller for party LED displays.
Manages phase transitions: Pre-Race, During Race, After Race.
"""

import threading
import time
from typing import Dict, Optional, Any
from datetime import datetime

from ddm.models.race_state import RaceState, RaceConfig, RacePhase, RaceStatus
from ddm.utils.logger import get_logger

logger = get_logger(__name__)


class RaceController:
    """
    Simple race controller for performative party events.
    
    Manages transitions between:
    - Pre-Race: Setup and anticipation
    - During Race: Simulated race with LED animations
    - After Race: Results and celebration
    """
    
    def __init__(self, socketio=None, device_manager=None):
        """Initialize the race controller."""
        self.socketio = socketio
        self.device_manager = device_manager
        self.race_state: Optional[RaceState] = None
        self.race_thread: Optional[threading.Thread] = None
        self.running = False
        
        logger.info("Race controller initialized")
    
    def create_race(self, config: RaceConfig) -> RaceState:
        """Create a new race with the given configuration."""
        self.race_state = RaceState(
            config=config,
            current_phase=RacePhase.IDLE,
            race_status=RaceStatus.STOPPED
        )
        
        # Add default callbacks
        self.race_state.add_phase_change_callback(self._on_phase_change)
        
        logger.info(f"Created race: {config.race_name}")
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
        
        logger.info("Resumed race")
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
    
    def _race_sequence(self):
        """Main race sequence thread."""
        if not self.race_state:
            logger.error("Cannot run race sequence: no race state")
            return
            
        try:
            # Phase 1: Pre-Race
            self._enter_phase(RacePhase.PRE_RACE, self.race_state.config.pre_race_duration)
            self._run_pre_race_phase()
            
            # Phase 2: During Race
            self._enter_phase(RacePhase.DURING_RACE, self.race_state.config.race_duration)
            self._run_race_phase()
            
            # Phase 3: After Race
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
        self._send_animation_command("pre_race_buildup")
        
        # Wait for phase duration
        self._wait_for_phase_completion()
    
    def _run_race_phase(self):
        """Run the actual race phase."""
        if not self.race_state:
            return
            
        logger.info("Running race phase: simulated race!")
        
        # Send race animation
        self._send_animation_command("race_running")
        
        # Wait for phase duration
        self._wait_for_phase_completion()
    
    def _run_after_race_phase(self):
        """Run the after-race phase."""
        if not self.race_state:
            return
            
        logger.info("Running after-race phase: celebration")
        
        # Send celebration animation
        self._send_animation_command("race_celebration")
        
        # Wait for phase duration
        self._wait_for_phase_completion()
    
    def _wait_for_phase_completion(self):
        """Wait for the current phase to complete."""
        if not self.race_state:
            return
        
        start_time = time.time()
        while self.running and self.race_state.get_phase_remaining_time() > 0:
            time.sleep(0.1)
            
            # Update total race time
            self.race_state.total_race_time = time.time() - start_time
            
            # Emit updates periodically
            if int(time.time() * 10) % 10 == 0:  # Every second
                self._emit_race_update()
    
    def _send_animation_command(self, animation_type: str):
        """Send animation command to device manager."""
        if not self.device_manager:
            return
        
        try:
            # Simple animation commands based on phase
            if animation_type == "pre_race_buildup":
                self.device_manager.send_command("animation", {"type": "buildup", "speed": 1.0})
            elif animation_type == "race_running":
                self.device_manager.send_command("animation", {"type": "race", "speed": 2.0})
            elif animation_type == "race_celebration":
                self.device_manager.send_command("animation", {"type": "celebration", "speed": 1.5})
                
        except Exception as e:
            logger.error(f"Error sending animation command: {e}")
    
    def _on_phase_change(self, phase: RacePhase, race_state: RaceState):
        """Handle phase changes."""
        logger.info(f"Phase changed to: {phase.value}")
        
        # Emit WebSocket event
        self._emit_phase_change(phase)
    
    def _emit_race_update(self):
        """Emit race update via WebSocket."""
        if not self.socketio:
            return
        
        try:
            self.socketio.emit('race_update', self.get_race_status())
        except Exception as e:
            logger.error(f"Error emitting race update: {e}")
    
    def _emit_phase_change(self, phase: RacePhase):
        """Emit phase change via WebSocket."""
        if not self.socketio:
            return
        
        try:
            self.socketio.emit('race_phase_change', {
                'phase': phase.value,
                'timestamp': datetime.now().isoformat()
            })
        except Exception as e:
            logger.error(f"Error emitting phase change: {e}")

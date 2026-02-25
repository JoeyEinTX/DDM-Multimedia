# racing_data_service.py - Racing data management service for DDM Horse Dashboard
#
# Manages race state machine, horse entries, odds drifting, and LED/socketio
# broadcasting for the Derby de Mayo Cup system.

import random
import time
import logging
import threading
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


# =============================================================================
# Race State Machine
# =============================================================================

class RaceState(Enum):
    """States in the race lifecycle, from dormant through official results."""
    DORMANT = "DORMANT"
    ENTRIES_LOADED = "ENTRIES_LOADED"
    BETTING_OPEN = "BETTING_OPEN"
    BETTING_CLOSING = "BETTING_CLOSING"
    AT_THE_POST = "AT_THE_POST"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    OFFICIAL = "OFFICIAL"


# Ordered state progression for auto mode
STATE_ORDER = [
    RaceState.DORMANT,
    RaceState.ENTRIES_LOADED,
    RaceState.BETTING_OPEN,
    RaceState.BETTING_CLOSING,
    RaceState.AT_THE_POST,
    RaceState.RUNNING,
    RaceState.FINISHED,
    RaceState.OFFICIAL,
]

# Duration in seconds each state lasts before advancing (auto mode)
STATE_DURATIONS = {
    RaceState.ENTRIES_LOADED: 30,
    RaceState.BETTING_OPEN: 60,
    RaceState.BETTING_CLOSING: 30,
    RaceState.AT_THE_POST: 15,
    RaceState.RUNNING: 10,
    RaceState.FINISHED: 5,
    # OFFICIAL has no timer — it stays until manually reset
}

# LED animation commands mapped to each race state
STATE_LED_COMMANDS = {
    RaceState.ENTRIES_LOADED: "ANIMATION:WELCOME",
    RaceState.BETTING_OPEN: "ANIMATION:BETTING_60",
    RaceState.BETTING_CLOSING: "ANIMATION:FINAL_CALL",
    RaceState.AT_THE_POST: "ANIMATION:IDLE",
    RaceState.RUNNING: "ANIMATION:RACE_START",
    RaceState.FINISHED: "ANIMATION:FINISH",
    # OFFICIAL is handled specially — triggers winner spotlight
}


# =============================================================================
# Saddle Cloth Colors (Official Racing Colors, positions 1–20)
# =============================================================================

SADDLE_CLOTH_COLORS: Dict[int, str] = {
    1:  "#E31837",   # Red
    2:  "#FFFFFF",   # White
    3:  "#0033A0",   # Blue
    4:  "#FFCD00",   # Yellow
    5:  "#00843D",   # Green
    6:  "#000000",   # Black
    7:  "#FF6600",   # Orange
    8:  "#FF69B4",   # Pink
    9:  "#40E0D0",   # Turquoise
    10: "#663399",   # Purple
    11: "#808080",   # Grey
    12: "#32CD32",   # Lime
    13: "#8B4513",   # Brown
    14: "#800000",   # Maroon
    15: "#C4B7A6",   # Khaki
    16: "#87CEEB",   # Light Blue
    17: "#000080",   # Navy
    18: "#228B22",   # Forest Green
    19: "#00008B",   # Dark Royal Blue
    20: "#FF00FF",   # Fuchsia
}


# =============================================================================
# Horse Data
# =============================================================================

@dataclass
class DerbyHorse:
    """Represents a single horse entry in a Derby de Mayo race."""
    post_position: int
    horse_name: str
    jockey: str
    trainer: str
    morning_line_odds: str          # e.g. "5/2"
    current_odds: float             # decimal odds, e.g. 2.5
    saddle_cloth_color: str         # hex color, e.g. "#E31837"
    finish_position: Optional[int] = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# =============================================================================
# Mock Data Pools
# =============================================================================

MOCK_HORSE_NAMES = [
    "Sovereignty", "Journalism", "Midnight Thunder", "Golden Conquest",
    "Silver Lining", "Desert Storm", "Iron Will", "Wildfire",
    "Noble Quest", "Dark Horizon", "Blazing Trail", "Crown Royal",
    "Phantom Rider", "Lucky Strike", "Bold Venture", "Storm Chaser",
    "Diamond Edge", "Crimson Tide", "Celtic Pride", "Maverick Run",
]

MOCK_JOCKEYS = [
    "J. Velazquez", "I. Ortiz Jr.", "F. Prat", "J. Rosario",
    "L. Saez", "T. Gaffalione", "J. Castellano", "M. Smith",
    "R. Santana Jr.", "J. Leparoux", "C. Landeros", "D. Davis",
    "K. Carmouche", "A. Cedillo", "B. Hernandez Jr.", "E. Cancel",
    "J. Lezcano", "P. Lopez", "S. Bridgmohan", "R. Bejarano",
]

MOCK_TRAINERS = [
    "B. Baffert", "T. Pletcher", "C. Brown", "S. Asmussen",
    "B. Cox", "M. Maker", "W. Mott", "D. O'Neill",
    "K. McPeek", "J. Sadler", "D. Romans", "M. Casse",
    "H. Motion", "R. Mandella", "J. Sharp", "I. Wilkes",
    "M. Stidham", "D. Stewart", "G. Weaver", "P. Gallagher",
]

MOCK_MORNING_LINE_ODDS = [
    "3/1", "5/2", "4/1", "6/1", "8/1", "10/1", "12/1", "15/1",
    "20/1", "7/2", "9/2", "5/1", "7/1", "30/1", "50/1", "2/1",
    "3/2", "6/5", "8/5", "9/1",
]


def _fraction_to_float(fraction_str: str) -> float:
    """Convert a fractional odds string like '5/2' to a float like 2.5."""
    try:
        num, den = fraction_str.split("/")
        return round(float(num) / float(den), 2)
    except (ValueError, ZeroDivisionError):
        return 3.0


# =============================================================================
# Racing Data Service
# =============================================================================

class RacingDataService:
    """
    Central service for managing race state, horse data, odds, and
    broadcasting updates via Socket.IO and LED commands.

    Args:
        socketio: Flask-SocketIO instance (or None for headless operation).
        use_mock: If True, generate mock horse entries on init.
    """

    def __init__(self, socketio=None, use_mock: bool = True):
        self.socketio = socketio
        self.use_mock = use_mock

        # Race state
        self.current_state: RaceState = RaceState.DORMANT
        self.state_changed_at: float = time.time()

        # Horse entries keyed by post position (1–20)
        self.horses: Dict[int, DerbyHorse] = {}

        # Result positions
        self._win: Optional[int] = None
        self._place: Optional[int] = None
        self._show: Optional[int] = None

        # Background threads
        self._auto_thread: Optional[threading.Thread] = None
        self._odds_thread: Optional[threading.Thread] = None
        self._running = False

        # Thread lock for state changes
        self._lock = threading.Lock()

        # Generate horses if mock mode
        if self.use_mock:
            self.generate_mock_horses()

        logger.info("RacingDataService initialised (mock=%s)", use_mock)

    # -----------------------------------------------------------------
    # Horse generation
    # -----------------------------------------------------------------

    def generate_mock_horses(self) -> None:
        """
        Populate self.horses with 20 realistic mock entries.
        Uses the MOCK_* pools and assigns official saddle cloth colours.
        """
        names = list(MOCK_HORSE_NAMES)
        jockeys = list(MOCK_JOCKEYS)
        trainers = list(MOCK_TRAINERS)
        odds_pool = list(MOCK_MORNING_LINE_ODDS)

        random.shuffle(names)
        random.shuffle(jockeys)
        random.shuffle(trainers)
        random.shuffle(odds_pool)

        self.horses.clear()

        for pos in range(1, 21):
            ml_odds = odds_pool[pos - 1]
            horse = DerbyHorse(
                post_position=pos,
                horse_name=names[pos - 1],
                jockey=jockeys[pos - 1],
                trainer=trainers[pos - 1],
                morning_line_odds=ml_odds,
                current_odds=_fraction_to_float(ml_odds),
                saddle_cloth_color=SADDLE_CLOTH_COLORS.get(pos, "#808080"),
                finish_position=None,
            )
            self.horses[pos] = horse

        logger.info("Generated %d mock horses", len(self.horses))

    # -----------------------------------------------------------------
    # State management
    # -----------------------------------------------------------------

    def get_state(self) -> dict:
        """Return current state information as a dict."""
        elapsed = round(time.time() - self.state_changed_at, 1)
        duration = STATE_DURATIONS.get(self.current_state)
        remaining = max(0, round(duration - elapsed, 1)) if duration else None

        return {
            "state": self.current_state.value,
            "elapsed_seconds": elapsed,
            "duration_seconds": duration,
            "remaining_seconds": remaining,
            "win": self._win,
            "place": self._place,
            "show": self._show,
        }

    def set_state(self, state: RaceState) -> None:
        """
        Manually set the race state (override). Broadcasts the change.

        Args:
            state: The RaceState to transition to.
        """
        with self._lock:
            old_state = self.current_state
            self.current_state = state
            self.state_changed_at = time.time()

        logger.info("State changed: %s → %s (manual)", old_state.value, state.value)
        self.emit_state_change(old_state, state)
        self.emit_led_command(state)

    def _advance_state(self) -> Optional[RaceState]:
        """
        Advance to the next state in the lifecycle.
        Returns the new state, or None if already at OFFICIAL.
        """
        try:
            idx = STATE_ORDER.index(self.current_state)
        except ValueError:
            return None

        if idx >= len(STATE_ORDER) - 1:
            return None  # Already at OFFICIAL

        next_state = STATE_ORDER[idx + 1]
        self.set_state(next_state)
        return next_state

    # -----------------------------------------------------------------
    # Auto progression
    # -----------------------------------------------------------------

    def start_auto_progression(self) -> None:
        """
        Start a background thread that automatically advances through
        race states on the defined timers.
        """
        if self._running:
            logger.warning("Auto progression already running")
            return

        self._running = True

        # Start state progression thread
        self._auto_thread = threading.Thread(
            target=self._auto_progression_loop,
            name="RaceAutoProgression",
            daemon=True,
        )
        self._auto_thread.start()

        # Start odds drifting thread
        self._odds_thread = threading.Thread(
            target=self._odds_drift_loop,
            name="OddsDrift",
            daemon=True,
        )
        self._odds_thread.start()

        logger.info("Auto progression started")

    def stop_auto_progression(self) -> None:
        """Stop auto progression and odds drifting."""
        self._running = False
        logger.info("Auto progression stopped")

    def _auto_progression_loop(self) -> None:
        """Background loop that advances state after each duration expires."""
        # Start from ENTRIES_LOADED
        if self.current_state == RaceState.DORMANT:
            self.set_state(RaceState.ENTRIES_LOADED)

        while self._running:
            duration = STATE_DURATIONS.get(self.current_state)
            if duration is None:
                # OFFICIAL or DORMANT — no auto-advance
                break

            # Sleep in small increments so we can stop quickly
            slept = 0.0
            while slept < duration and self._running:
                time.sleep(0.5)
                slept += 0.5

            if not self._running:
                break

            next_state = self._advance_state()
            if next_state is None:
                break

        logger.info("Auto progression loop ended (state=%s)", self.current_state.value)

    # -----------------------------------------------------------------
    # Odds drifting
    # -----------------------------------------------------------------

    def drift_odds(self) -> None:
        """
        Randomly adjust current_odds for all horses by a small amount.
        Simulates tote board odds movement during betting periods.
        """
        with self._lock:
            for horse in self.horses.values():
                # Drift by ±5%
                drift_pct = random.uniform(-0.05, 0.05)
                new_odds = horse.current_odds * (1.0 + drift_pct)
                # Clamp to a reasonable range (minimum 1.1)
                horse.current_odds = round(max(1.1, new_odds), 2)

        # Broadcast updated odds
        if self.socketio:
            self.socketio.emit("odds_update", {
                "horses": self.get_horses(),
            })

    def _odds_drift_loop(self) -> None:
        """Background loop that drifts odds every few seconds during betting."""
        while self._running:
            if self.current_state in (RaceState.BETTING_OPEN, RaceState.BETTING_CLOSING):
                self.drift_odds()
            time.sleep(random.uniform(3.0, 6.0))

    # -----------------------------------------------------------------
    # Socket.IO broadcasting
    # -----------------------------------------------------------------

    def emit_state_change(self, old_state: RaceState, new_state: RaceState) -> None:
        """
        Broadcast a state change event via Socket.IO.

        Args:
            old_state: The previous RaceState.
            new_state: The new RaceState.
        """
        payload = {
            "old_state": old_state.value,
            "new_state": new_state.value,
            "timestamp": time.time(),
            "state_info": self.get_state(),
            "horses": self.get_horses(),
        }

        if self.socketio:
            self.socketio.emit("race_state_change", payload)
            logger.debug("Emitted race_state_change: %s → %s", old_state.value, new_state.value)
        else:
            logger.debug("No socketio — skipping emit for %s → %s", old_state.value, new_state.value)

    def emit_led_command(self, state: RaceState) -> None:
        """
        Send the appropriate LED animation command for the given state.

        For OFFICIAL state, sends a winner spotlight command using the
        winning post position. For all other states, sends the mapped
        animation command.

        Args:
            state: The current RaceState to send LED command for.
        """
        if state == RaceState.OFFICIAL and self._win is not None:
            # Winner spotlight — highlight the winning post position
            command = f"ANIMATION:WINNER_SPOTLIGHT:{self._win}"
        else:
            command = STATE_LED_COMMANDS.get(state)

        if command is None:
            logger.debug("No LED command for state %s", state.value)
            return

        if self.socketio:
            self.socketio.emit("led_command", {"command": command})
            logger.info("LED command emitted: %s", command)
        else:
            logger.info("LED command (no socketio): %s", command)

    # -----------------------------------------------------------------
    # Horse data access
    # -----------------------------------------------------------------

    def get_horses(self) -> List[dict]:
        """
        Return a list of horse dicts sorted by post position.

        Returns:
            List of horse dictionaries, ordered by post_position 1–20.
        """
        return [
            self.horses[pos].to_dict()
            for pos in sorted(self.horses.keys())
        ]

    def get_horse(self, post_position: int) -> Optional[dict]:
        """
        Return a single horse dict by post position.

        Args:
            post_position: Post position number (1–20).

        Returns:
            Horse dict or None if not found.
        """
        horse = self.horses.get(post_position)
        return horse.to_dict() if horse else None

    # -----------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------

    def set_winners(self, win: int, place: int, show: int) -> None:
        """
        Set the finish positions for win, place, and show.

        Updates the corresponding DerbyHorse finish_position fields
        and broadcasts the update.

        Args:
            win: Post position of the winning horse (1st place).
            place: Post position of the place horse (2nd place).
            show: Post position of the show horse (3rd place).

        Raises:
            ValueError: If any post position is invalid or duplicated.
        """
        # Validate
        positions = [win, place, show]
        if len(set(positions)) != 3:
            raise ValueError("Win, place, and show must be different post positions")
        for pos in positions:
            if pos not in self.horses:
                raise ValueError(f"Invalid post position: {pos}")

        with self._lock:
            # Clear any previous finish positions
            for horse in self.horses.values():
                horse.finish_position = None

            # Set new results
            self.horses[win].finish_position = 1
            self.horses[place].finish_position = 2
            self.horses[show].finish_position = 3

            self._win = win
            self._place = place
            self._show = show

        logger.info(
            "Results set — WIN: #%d %s, PLACE: #%d %s, SHOW: #%d %s",
            win, self.horses[win].horse_name,
            place, self.horses[place].horse_name,
            show, self.horses[show].horse_name,
        )

        # Broadcast results
        if self.socketio:
            self.socketio.emit("race_results", {
                "win": self.horses[win].to_dict(),
                "place": self.horses[place].to_dict(),
                "show": self.horses[show].to_dict(),
            })

    def clear_results(self) -> None:
        """Clear finish positions and reset result tracking."""
        with self._lock:
            for horse in self.horses.values():
                horse.finish_position = None
            self._win = None
            self._place = None
            self._show = None

        logger.info("Results cleared")

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def reset(self) -> None:
        """
        Full reset: stop auto progression, clear results, return to
        DORMANT, and optionally regenerate mock horses.
        """
        self.stop_auto_progression()
        self.clear_results()

        with self._lock:
            self.current_state = RaceState.DORMANT
            self.state_changed_at = time.time()

        if self.use_mock:
            self.generate_mock_horses()

        logger.info("RacingDataService reset to DORMANT")

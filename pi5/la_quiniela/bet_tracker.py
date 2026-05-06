# la_quiniela/bet_tracker.py - In-memory state for the La Quiniela POC
#
# Tracks per-cup weight, ticket count, and assigned horse number. Thread-safe
# via a single lock around all mutations. No persistence yet (POC).

import threading
import time
from typing import Optional


# Tickets weigh ~2-2.5g per half (DDM_La_Quiniela_Spec.md). Below this
# threshold, weight changes are noise to ignore.
DEFAULT_TICKET_WEIGHT_G = 2.5
DEFAULT_NOISE_THRESHOLD_G = 2.0


class BetTracker:
    def __init__(
        self,
        num_cups: int = 20,
        ticket_weight: float = DEFAULT_TICKET_WEIGHT_G,
        noise_threshold: float = DEFAULT_NOISE_THRESHOLD_G,
    ):
        self._num_cups = num_cups
        self._ticket_weight = ticket_weight
        self._noise_threshold = noise_threshold
        self._lock = threading.Lock()
        self._weights: dict[int, float] = {i: 0.0 for i in range(1, num_cups + 1)}
        self._bets: dict[int, int] = {i: 0 for i in range(1, num_cups + 1)}
        # Horse assignment per cup: a digit string "1".."20", "X" for scratch,
        # or None if not yet assigned.
        self._horses: dict[int, Optional[str]] = {i: None for i in range(1, num_cups + 1)}
        self._last_event_ts: Optional[float] = None

    # ---- ingest ----
    def handle_weight_update(self, payload: dict) -> dict:
        """Process one {"scale": N, "weight": g, "delta": g} message.

        Returns a result dict describing what changed (for logging / push).
        """
        scale = payload.get("scale")
        weight = payload.get("weight")
        delta = payload.get("delta")

        if not isinstance(scale, int) or scale < 1 or scale > self._num_cups:
            return {"ok": False, "error": "invalid scale id"}
        if not isinstance(weight, (int, float)):
            return {"ok": False, "error": "invalid weight"}

        # Trust the ESP32-provided delta when present, else compute from
        # last known weight.
        with self._lock:
            previous = self._weights[scale]
            d = float(delta) if isinstance(delta, (int, float)) else float(weight) - previous
            self._weights[scale] = float(weight)

            tickets_delta = 0
            if abs(d) >= self._noise_threshold:
                # Round to nearest ticket count, preserve sign.
                tickets_delta = int(round(d / self._ticket_weight))
                if tickets_delta != 0:
                    new_count = max(0, self._bets[scale] + tickets_delta)
                    self._bets[scale] = new_count
                    self._last_event_ts = time.time()

            return {
                "ok": True,
                "cup": scale,
                "weight": self._weights[scale],
                "delta": d,
                "tickets_delta": tickets_delta,
                "bet_count": self._bets[scale],
            }

    # ---- assignments ----
    def set_horse(self, cup: int, horse: str) -> None:
        """Assign a horse number ('1'..'20') or 'X' (scratch) to a cup."""
        if cup < 1 or cup > self._num_cups:
            raise ValueError(f"cup {cup} out of range")
        h = horse.strip().upper()
        if h != "X":
            if not h.isdigit() or not (1 <= int(h) <= 20):
                raise ValueError(f"invalid horse '{horse}'")
        with self._lock:
            self._horses[cup] = h

    def get_horse(self, cup: int) -> Optional[str]:
        with self._lock:
            return self._horses.get(cup)

    # ---- read ----
    def get_bets(self) -> list[dict]:
        """Return a list of {cup, horse, bets, weight} for all cups."""
        with self._lock:
            return [
                {
                    "cup": i,
                    "horse": self._horses[i],
                    "bets": self._bets[i],
                    "weight": round(self._weights[i], 2),
                }
                for i in range(1, self._num_cups + 1)
            ]

    def get_horses(self) -> dict[int, Optional[str]]:
        with self._lock:
            return dict(self._horses)

    def total_bets(self) -> int:
        with self._lock:
            return sum(self._bets.values())

    # ---- admin ----
    def reset(self) -> None:
        with self._lock:
            for i in range(1, self._num_cups + 1):
                self._weights[i] = 0.0
                self._bets[i] = 0
            self._last_event_ts = None

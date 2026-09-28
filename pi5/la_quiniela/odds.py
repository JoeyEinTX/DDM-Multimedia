# la_quiniela/odds.py - The real track's odds, for the slideshow
#
# La Quiniela pays no odds (a token is a raffle ticket for a fixed prize), but
# the TV's roster slide shows the real track's morning line / live odds next
# to each horse, as the Race Setup page used to. The poller that fetched them
# lived in main.py and wrote them into data/race_setup.json; it lives here
# now and hands them to the board (BettingBoard.set_odds), which serves them
# as horses[n].odds, keyed by PROGRAM number (21..24 included: an
# also-eligible that draws in keeps its number, and its odds come with it).
#
# One background thread, started and stopped by hand (POST
# /api/quiniela/odds/start, /stop; GET /api/quiniela/odds for its status),
# exactly as before: nothing fetches unless asked. Each round asks Claude
# (the Anthropic API with web search) for the current odds of the race the
# store names (its year and name); a round that fails, or a machine with no
# API key or no internet, leaves the odds as they were, and a horse with no
# odds is null in the model. Nothing here raises into a request.

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("la_quiniela.odds")

DEFAULT_INTERVAL_S = 300
MIN_INTERVAL_S = 60                   # never more often than once a minute
MODEL = "claude-sonnet-4-6"

_poller: Optional["OddsPoller"] = None


def extract_json(text: str) -> Optional[Any]:
    """A JSON object out of a model's reply: markdown fences stripped, prose
    around it ignored, the outermost {...} tried when the whole does not
    parse. None when nothing parses."""
    if not text:
        return None
    cleaned = text.strip()
    for fence in ("```json", "```"):
        if fence in cleaned:
            try:
                start = cleaned.index(fence) + len(fence)
                cleaned = cleaned[start:cleaned.index("```", start)].strip()
            except ValueError:
                pass
            break
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        first, last = cleaned.find("{"), cleaned.rfind("}")
        if first != -1 and last > first:
            try:
                return json.loads(cleaned[first:last + 1])
            except json.JSONDecodeError:
                return None
    return None


def odds_from_reply(parsed: Any) -> Optional[Dict[str, str]]:
    """{"odds": {"1": "5-2", ...}} or a bare {"1": "5-2", ...} -> the dict as
    given (keys and values as strings); None for any other shape. The board
    decides what counts as odds (betting.clean_odds)."""
    odds = parsed.get("odds") if isinstance(parsed, dict) and "odds" in parsed else parsed
    if not isinstance(odds, dict):
        return None
    return {str(k).strip(): ("" if v is None else str(v).strip()) for k, v in odds.items()}


def anthropic_fetcher(api_key: Optional[str]) -> Callable[[Dict[str, Any]], Optional[Dict[str, str]]]:
    """A fetch(race) for OddsPoller: one question to Claude with web search.
    race is the model's race ({"name", "year", ...})."""
    def fetch(race: Dict[str, Any]) -> Optional[Dict[str, str]]:
        if not api_key:
            return None
        name = str(race.get("name") or "KENTUCKY DERBY").title()
        year = race.get("year") or datetime.now().year
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search for the latest {year} {name} odds. "
                        "Return ONLY a JSON object with no other text. "
                        'Format: {"odds": {"1": "5-2", "2": "8-1", "21": "30-1", ...}} '
                        "where the keys are the horses' PROGRAM numbers (1-20, and "
                        "21-24 for an also-eligible that drew in, under its own "
                        "number) and the values are the current odds as strings. "
                        'Use "" for scratched or unknown horses.'
                    ),
                }],
            )
            text = "".join(block.text for block in response.content if getattr(block, "text", None))
            return odds_from_reply(extract_json(text))
        except Exception as exc:
            log.warning("odds: the Anthropic call failed: %s", exc)
            return None
    return fetch


class OddsPoller:
    """Fetches the odds every `interval` seconds on a daemon thread and hands
    them to `sink` (the board's set_odds). `race` supplies the race to ask
    about; `emit`, if given, is called with {"odds", "last_update"} after
    each successful round (the spectator page's odds_update)."""

    def __init__(self, fetch: Callable[[Dict[str, Any]], Optional[Dict[str, str]]],
                 sink: Callable[[Any], Any], race: Callable[[], Dict[str, Any]],
                 emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 enabled: bool = True) -> None:
        self._fetch = fetch
        self._sink = sink
        self._race = race
        self._emit = emit
        self.enabled = enabled
        self.interval = DEFAULT_INTERVAL_S
        self.last_update: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def polling(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, interval: Any = None) -> Dict[str, Any]:
        """{"ok": True, "interval": s} or {"ok": False, "error", "status"}."""
        if not self.enabled:
            return {"ok": False, "error": "ANTHROPIC_API_KEY not configured", "status": 503}
        with self._lock:
            if self.polling():
                return {"ok": False, "error": "odds polling already running", "status": 409}
            try:
                seconds = int(interval) if interval is not None else DEFAULT_INTERVAL_S
            except (TypeError, ValueError):
                seconds = DEFAULT_INTERVAL_S
            self.interval = max(MIN_INTERVAL_S, seconds)
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="lq-odds", daemon=True)
            self._thread.start()
        log.info("odds: polling every %d s", self.interval)
        return {"ok": True, "interval": self.interval}

    def stop(self) -> Dict[str, Any]:
        was = self.polling()
        self._stop.set()
        return {"ok": True, "stopped": was}

    def status(self) -> Dict[str, Any]:
        polling = self.polling()
        next_update = None
        if polling and self.last_update:
            try:
                last = datetime.strptime(self.last_update, "%Y-%m-%dT%H:%M:%SZ")
                next_update = (last + timedelta(seconds=self.interval)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
        return {"polling": polling, "interval": self.interval, "last_update": self.last_update,
                "next_update": next_update}

    def poll_once(self) -> bool:
        """One round: fetch, hand over, emit. Returns whether odds came back."""
        try:
            odds = self._fetch(self._race())
        except Exception as exc:
            log.warning("odds: fetch failed: %s", exc)
            odds = None
        if not odds:
            log.info("odds: nothing came back this round; the odds stay as they were")
            return False
        kept = self._sink(odds)
        self.last_update = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("odds: %d horses' odds at %s", len(kept) if isinstance(kept, dict) else len(odds),
                 self.last_update)
        if self._emit is not None:
            try:
                self._emit({"odds": {str(k): v for k, v in (kept if isinstance(kept, dict) else odds).items()},
                            "last_update": self.last_update})
            except Exception as exc:
                log.debug("odds: emit failed: %s", exc)
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)
        log.info("odds: polling stopped")


def init_odds(api_key: Optional[str], sink: Callable[[Any], Any], race: Callable[[], Dict[str, Any]],
              emit: Optional[Callable[[Dict[str, Any]], Any]] = None) -> OddsPoller:
    """The app's poller (no thread until start())."""
    global _poller
    if _poller is not None:
        _poller.stop()
    _poller = OddsPoller(anthropic_fetcher(api_key), sink, race, emit=emit, enabled=bool(api_key))
    return _poller


def get_odds_poller() -> OddsPoller:
    if _poller is None:
        raise RuntimeError("odds poller not initialised")
    return _poller


def set_odds_poller(poller: Optional[OddsPoller]) -> None:
    """Tests: put a poller of their own in place (or None)."""
    global _poller
    _poller = poller

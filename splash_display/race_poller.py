"""
Background poller for the dashboard Pi's /api/race endpoint.

A daemon thread fetches the race roster every POLL_INTERVAL_S and caches the
parsed JSON in module-global state. Splash routes call get_race_data() to read
the latest snapshot synchronously. On fetch failure the previous snapshot is
retained so transient network blips don't cause the horse_roster slide to
disappear from rotation.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import config

log = logging.getLogger("splash_display.race_poller")

POLL_INTERVAL_S = 30
FETCH_TIMEOUT_S = 5

_lock = threading.Lock()
_cached: Optional[Dict[str, Any]] = None
_thread: Optional[threading.Thread] = None
_started = False


def get_race_data() -> Optional[Dict[str, Any]]:
    """Return the most recent successfully fetched race payload, or None."""
    with _lock:
        return _cached


def _fetch_once() -> Optional[Dict[str, Any]]:
    url = config.DASHBOARD_RACE_URL
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.warning("race poll failed (%s): %s", type(e).__name__, e)
    except json.JSONDecodeError as e:
        log.warning("race poll: invalid JSON: %s", e)
    return None


def _run() -> None:
    global _cached
    while True:
        data = _fetch_once()
        if data is not None:
            with _lock:
                _cached = data
        time.sleep(POLL_INTERVAL_S)


def start_poller() -> None:
    """Idempotent — safe to call from server startup."""
    global _thread, _started
    if _started:
        return
    _started = True
    _thread = threading.Thread(target=_run, name="race-poller", daemon=True)
    _thread.start()
    log.info("race poller started (url=%s, interval=%ss)",
             config.DASHBOARD_RACE_URL, POLL_INTERVAL_S)

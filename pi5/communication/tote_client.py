# tote_client.py - The tote board (Interstate75 LED display), through one queued sender
#
# HTTP GET to the board, TOTE_TIMEOUT to answer. Nothing here waits for it on
# a request thread: every display call hands its path to the Sender
# (communication/sender.py) and returns; ping() asks and waits. One key for
# the display (/tote): the newest figures replace older ones waiting, and the
# last figures go out again when the board returns. The sender's probe,
# /status every 5 s idle, keeps the board's health fresh for GET /api/status.

import time
from typing import Any, Dict
from urllib.parse import urlencode

import requests

from communication.sender import Sender, Unreachable, logs_probes


def key_of(path: str) -> str:
    return path.partition("?")[0]


def is_state(path: str) -> bool:
    return path.startswith("/tote")


class ToteClient:
    def __init__(self, ip, port=80, timeout=2.0, clock=time.time, log=print):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{ip}:{port}"
        self.sender = Sender("Tote board", "[TOTE]", f"{ip}:{port}", self, timeout, key_of, is_state,
                             probe="/status", clock=clock, log=log)

    def exchange(self, path: str) -> str:
        """One GET to the board, on the sender's thread. Raises Unreachable when it did not answer."""
        try:
            response = requests.get(self.base_url + path, timeout=self.timeout)
            response.raise_for_status()
        except requests.Timeout:
            raise Unreachable("TIMEOUT", "timed out")
        except requests.ConnectionError:
            raise Unreachable("CONNECTION_REFUSED", "connection refused")
        except requests.HTTPError as e:
            raise Unreachable(f"HTTP:{e.response.status_code}", f"HTTP {e.response.status_code}")
        if path != "/status" or logs_probes():
            print(f"[TOTE] GET {path} | Status: {response.status_code}")
        return response.text

    def _display(self, params: Dict[str, Any]) -> None:
        self.sender.send("/tote?" + urlencode(params))

    def ping(self) -> bool:
        return not self.sender.ask("/status").startswith("ERROR")

    def health(self) -> Dict[str, Any]:
        h = self.sender.health()
        h["enabled"] = True
        return h

    def welcome(self) -> None:
        self._display({"mode": "welcome"})

    def scroll(self, message, color="gold", speed=3) -> None:
        self._display({"mode": "scroll", "msg": message, "color": color, "speed": speed})

    def betting_open(self, minutes=60) -> None:
        self._display({"mode": "betting", "mins": minutes})

    def final_call(self) -> None:
        self._display({"mode": "finalcall"})

    def race_start(self) -> None:
        self._display({"mode": "racestart"})

    def official(self, win, place, show) -> None:
        self._display({"mode": "official", "win": win, "place": place, "show": show})

    def test(self) -> None:
        self._display({"mode": "test"})

    def stop(self) -> None:
        self._display({"mode": "stop"})

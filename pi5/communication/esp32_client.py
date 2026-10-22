# esp32_client.py - The LED wall (the ESP32 LED controller), through one queued sender
#
# TCP to the controller, one connection per command: a line out, a line back,
# SOCKET_TIMEOUT to answer. Nothing here waits for it on a request thread:
# send() hands the command to the Sender (communication/sender.py) and
# returns it; ask() waits for the reply, for the few callers that need one
# (PING, PARAM). The wall's health is the sender's, kept fresh by its STATUS
# probe: GET /api/status serves it and the Control Center shows it. With no
# address (DDM_ESP32_IP empty) the LEDs are off: nothing is sent or probed, a
# send is dropped, an ask answers ERROR:NO_LED_CONTROLLER at once, and main.py
# says so once at start.

import socket
import time
from typing import Any, Dict, Optional

from config import ESP32_IP, ESP32_PORT, SOCKET_TIMEOUT
from communication.sender import Sender, Unreachable, logs_probes


def key_of(command: str) -> str:
    """What a command replaces while it waits: the newest animation, the newest
    whole-wall state (off, on, a colour, a reset), each cup's newest, the
    brightness, the results' finalize, STATUS and PING once each; anything
    else (PARAM, a raw command) only an identical one."""
    parts = command.split(":")
    head = parts[0]
    if head == "ANIM":
        return "ANIM"
    if head == "RESET" or (head == "LED" and len(parts) > 1 and parts[1] in ("ALL_OFF", "ALL_ON", "COLOR")):
        return "LED"
    if head == "LED" and len(parts) > 1 and parts[1] == "BRIGHTNESS":
        return "LED:BRIGHTNESS"
    if head == "LED" and len(parts) > 2 and parts[1] == "CUP":
        return "CUP:" + parts[2]
    if head == "CUP" and len(parts) > 2:
        return "CUP:" + parts[2]
    if head == "RESULTS":
        return "RESULTS"
    return command


def is_state(command: str) -> bool:
    """A command the wall should show again when it returns: everything but
    a query (STATUS, PING) and the tuning modal's PARAM commands."""
    return command not in ("STATUS", "PING") and not command.startswith("PARAM:")


class ESP32Client:
    def __init__(self, ip=ESP32_IP, port=ESP32_PORT, timeout=SOCKET_TIMEOUT, clock=time.time, log=print):
        self._ip = ip
        self.port = port
        self.timeout = timeout
        self.power: Optional[Dict[str, int]] = None        # the last STATUS answer: current_ma, peak_ma, min_ma
        self.sender = Sender("LED wall", "[ESP32]", self.address, self, timeout, key_of, is_state,
                             probe="STATUS" if ip else None, clock=clock, log=log)

    @property
    def ip(self) -> str:
        return self._ip

    @ip.setter
    def ip(self, value: str) -> None:
        self._ip = value
        self.sender.address = self.address
        self.sender.probe = "STATUS"

    @property
    def address(self) -> str:
        return f"{self._ip}:{self.port}"

    def exchange(self, command: str) -> str:
        """One command to the controller, on the sender's thread: connect, the
        line out, the reply back. Raises Unreachable when it did not answer."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self._ip, self.port))
                sock.sendall((command + '\n').encode('utf-8'))
                response = sock.recv(1024).decode('utf-8').strip()
        except socket.timeout:
            raise Unreachable("TIMEOUT", "timed out")
        except ConnectionRefusedError:
            raise Unreachable("CONNECTION_REFUSED", "connection refused")
        except OSError as e:
            raise Unreachable("EXCEPTION:" + str(e), str(e))
        if response.startswith("STATUS:"):
            parts = response.split(":")
            if len(parts) == 4:
                self.power = {'current_ma': int(parts[1]), 'peak_ma': int(parts[2]), 'min_ma': int(parts[3])}
        if command != "STATUS" or logs_probes():
            print(f"[ESP32] Sent: {command} | Received: {response}")
        return response

    def send(self, command: str) -> str:
        """Hand a command to the wall's worker (dropped with no address) and return it."""
        if self._ip:
            self.sender.send(command)
        return command

    def ask(self, command: str, wait: Optional[float] = None) -> str:
        if not self._ip:
            return "ERROR:NO_LED_CONTROLLER"
        return self.sender.ask(command, wait)

    def health(self) -> Dict[str, Any]:
        h = self.sender.health()
        h["enabled"] = bool(self._ip)
        h["power"] = self.power
        return h

    def ping(self) -> bool:
        return self.ask("PING") == "PONG"

    def reset(self) -> str:
        return self.send("RESET")

    def all_on(self) -> str:
        return self.send("LED:ALL_ON")

    def all_off(self) -> str:
        return self.send("LED:ALL_OFF")

    def set_brightness(self, brightness) -> str:
        brightness = max(0, min(100, int(brightness)))
        return self.send(f"LED:BRIGHTNESS:{brightness}")

    def set_color(self, hex_color: str) -> str:
        return self.send(f"LED:COLOR:{hex_color.lstrip('#')}")

    def set_cup(self, cup_number, hex_color: str) -> str:
        return self.send(f"LED:CUP:{cup_number}:{hex_color.lstrip('#')}")

    def start_animation(self, anim_name: str) -> str:
        return self.send(f"ANIM:{anim_name.upper()}")


# Global client instance
esp32 = ESP32Client()

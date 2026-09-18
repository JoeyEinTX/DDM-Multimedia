# la_quiniela/sim/link.py - the virtual serial port
#
# A pty pair from os.openpty(). The simulator keeps the master end; the
# bridge opens the slave end like any serial port. A stable symlink
# (/tmp/ddm-lq-sim by default) points at the slave so the app can always be
# started with DDM_LQ_SERIAL_PORT=/tmp/ddm-lq-sim. Nothing here blocks: the
# bridge may connect late, disconnect or restart at any time, and output
# written while nobody is listening is discarded.

import errno
import os
import queue
import select
import termios
import threading
import tty
from typing import List, Optional

DEFAULT_LINK = "/tmp/ddm-lq-sim"


class PtyLink:

    def __init__(self, link_path: Optional[str] = DEFAULT_LINK):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)                   # no echo, no line discipline, from the start
        os.set_blocking(self.master, False)
        self.slave_path = os.ttyname(self.slave)
        self.link_path = link_path
        if link_path:
            try:
                if os.path.islink(link_path) or os.path.exists(link_path):
                    os.remove(link_path)
            except OSError:
                pass
            os.symlink(self.slave_path, link_path)
        self.rx: "queue.Queue[bytes]" = queue.Queue()
        self.dropped_writes = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="lq-sim-pty", daemon=True)
        self._reader.start()

    # -- reading (bridge -> simulator) -----------------------------------------

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.master], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(self.master, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:       # slave side closed: wait for a new reader
                    self._stop.wait(0.1)
                    continue
                break
            if not data:
                continue
            self.bytes_in += len(data)
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.rx.put(line.rstrip(b"\r"))
            if len(buf) > 8192:                  # no newline for 8 KB: not a line, drop it
                buf = b""

    def drain(self) -> List[bytes]:
        lines = []
        while True:
            try:
                lines.append(self.rx.get_nowait())
            except queue.Empty:
                return lines

    # -- writing (simulator -> bridge) -----------------------------------------

    def write_line(self, text: str) -> bool:
        """Write one line. False, and the line is discarded, when nobody is
        reading and the pty buffer is full; the stale buffer is flushed so a
        bridge that connects later does not start on old lines."""
        data = (text + "\n").encode("utf-8")
        sent = 0
        tries = 0
        while sent < len(data):
            try:
                n = os.write(self.master, data[sent:])
            except BlockingIOError:
                n = 0
            except OSError:
                self.dropped_writes += 1
                return False
            sent += n
            if sent < len(data):
                tries += 1
                if tries > 5:
                    self.dropped_writes += 1
                    try:
                        termios.tcflush(self.slave, termios.TCIFLUSH)
                    except (OSError, termios.error):
                        pass
                    return False
                try:
                    select.select([], [self.master], [], 0.02)
                except (OSError, ValueError):
                    self.dropped_writes += 1
                    return False
        self.bytes_out += len(data)
        return True

    # -- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        self._stop.set()
        self._reader.join(1.0)
        if self.link_path:
            try:
                if os.path.islink(self.link_path) and os.readlink(self.link_path) == self.slave_path:
                    os.remove(self.link_path)
            except OSError:
                pass
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

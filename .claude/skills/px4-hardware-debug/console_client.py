#!/usr/bin/env python3
"""Skill-side helper for the PX4 shared console broker (see console.py).

Usage from the skill:
    from console_client import Console
    c = Console()                      # connects to a running broker
    c.send("ver all")                  # fire-and-forget
    print(c.read(timeout=2.0))         # drain output seen so far
    print(c.read_until("nsh>", 5.0))   # convenience for quick commands
"""

import queue
import socket
import sys
import threading
import time

DEFAULT_TCP_PORT = 8765


class Console:
    def __init__(self, host="127.0.0.1", port=DEFAULT_TCP_PORT, connect_timeout=2.0):
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as e:
            raise ConnectionError(
                "no console broker on %s:%s - start it with "
                "console.py [--headless]" % (host, port)) from e
        # create_connection leaves connect_timeout on the socket; clear it so the
        # reader thread blocks indefinitely on readline() instead of dying with
        # socket.timeout during idle gaps (e.g. between flash progress bursts, or
        # a quiet board). read()/read_until() have their own queue timeouts.
        self._sock.settimeout(None)
        self.statuses = []
        self.flash_status = []     # inbound FLASH status lines, in arrival order
        self.board = None          # last board name the broker detected, or None
        self._q = queue.Queue()
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        f = self._sock.makefile("r")
        while self._running:
            try:
                line = f.readline()
            except OSError:
                break
            if not line:
                break
            line = line.rstrip("\n")
            self._classify(line)

    def _classify(self, line):
        if line.startswith("OUT "):
            self._q.put(line[4:])
        elif line.startswith("FLASH "):
            self.flash_status.append(line[6:])
        elif line.startswith("STATUS "):
            self.statuses.append(line[7:])
        elif line.startswith("BOARD "):
            name = line[6:]
            self.board = None if name == "?" else name
        # PONG and anything else are ignored

    def send(self, text):
        self._sock.sendall(("SEND " + text + "\n").encode("utf-8", "replace"))

    def read(self, timeout=1.0):
        lines = []
        end = time.time() + timeout
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            try:
                lines.append(self._q.get(timeout=remaining))
            except queue.Empty:
                break
        return "\n".join(lines)

    def read_until(self, marker, timeout=5.0):
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self._q.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                break
            lines.append(line)
            if marker in line:
                break
        return "\n".join(lines)

    def flash(self, target):
        # Raw FLASH line (NOT via send(), which prepends "SEND " and would make
        # the broker type "FLASH <target>" into NSH instead of dispatching it).
        self._sock.sendall(("FLASH " + target + "\n").encode("utf-8", "replace"))

    def wait_flash_done(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            if self.flash_status:
                last = self.flash_status[-1]
                if last == "done" or last.startswith("error"):
                    return last
            time.sleep(0.2)
        return None

    def close(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass

    @staticmethod
    def ping(host="127.0.0.1", port=DEFAULT_TCP_PORT, timeout=1.0):
        # The broker greets every client with a STATUS line before it answers
        # PING, so read until PONG appears (or timeout) rather than assuming the
        # first packet contains it.
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.sendall(b"PING\n")
                s.settimeout(timeout)
                buf = b""
                end = time.time() + timeout
                while time.time() < end:
                    try:
                        chunk = s.recv(256)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if b"PONG" in buf:
                        return True
                return False
        except OSError:
            return False


if __name__ == "__main__":
    # Tiny CLI: `console_client.py ver all` sends and prints ~2s of output.
    # `console_client.py --flash <target>` triggers a flash and streams status.
    c = Console()
    if len(sys.argv) > 2 and sys.argv[1] == "--flash":
        c.flash(sys.argv[2])
        print("flashing", sys.argv[2], "...")
        result = c.wait_flash_done()
        for s in c.flash_status:
            print("  ", s)
        print("result:", result)
    elif len(sys.argv) > 1:
        c.send(" ".join(sys.argv[1:]))
        print(c.read(timeout=2.0))
    c.close()

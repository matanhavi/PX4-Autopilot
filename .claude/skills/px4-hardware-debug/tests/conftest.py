import os
import sys
import time

# Make console.py / console_client.py importable from the skill dir.
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

import pytest


class PtyFakeSerial:
    """A serial-like object backed by a pseudo-terminal.

    Implements the read/write/close contract Broker depends on:
      - read(n) -> bytes (b"" when no data is available, like a serial timeout)
      - write(b) -> int
      - close()
    Plus test helpers to act as the device on the other end of the wire:
      - feed_device(b): bytes the 'device' sends to the host (broker reads them)
      - read_host(n):   bytes the broker wrote toward the device
    """

    def __init__(self):
        self.master, self.slave = os.openpty()
        os.set_blocking(self.slave, False)
        os.set_blocking(self.master, False)
        self.closed = False

    def read(self, n):
        try:
            return os.read(self.slave, n)
        except BlockingIOError:
            return b""

    def write(self, b):
        return os.write(self.slave, b)

    def feed_device(self, b):
        os.write(self.master, b)

    def read_host(self, n=4096):
        # Give the broker a moment to flush its write.
        for _ in range(50):
            try:
                data = os.read(self.master, n)
                if data:
                    return data
            except BlockingIOError:
                pass
            time.sleep(0.01)
        return b""

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.master)
            os.close(self.slave)


@pytest.fixture
def pty_serial():
    s = PtyFakeSerial()
    yield s
    try:
        s.close()
    except OSError:
        pass


def wait_for(predicate, timeout=2.0, interval=0.01):
    """Poll predicate() until truthy or timeout; return its last value."""
    end = time.time() + timeout
    val = predicate()
    while not val and time.time() < end:
        time.sleep(interval)
        val = predicate()
    return val

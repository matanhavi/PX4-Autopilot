"""MAVLink NSH shell as a pyserial-lookalike transport.

Some boards have no UART/FTDI console -- their only shell is the USB VCP, reached
over MAVLink SERIAL_CONTROL (the "nsh over mavlink" path). This adapter presents
the read()/write()/close() surface that console.Broker expects from a pyserial
object, so the broker can drive such a board (e.g. the Aerium Apex) exactly like
an FTDI console, via the broker's serial_factory injection point.

All MAVLink I/O runs in ONE background thread (poll for output + flush queued
writes), so the underlying serial port is never touched concurrently. read()
drains a byte buffer with a short timeout, mimicking pyserial's timeout=0.2.

Pass a stable /dev/serial/by-id/... path so a reconnect after the board
re-enumerates (its ttyACM number shifts across reboots) re-opens the right device.
"""

import queue
import threading
import time

try:
    from pymavlink import mavutil
except ImportError:                       # pragma: no cover - optional dep
    mavutil = None

_CHUNK = 70                               # SERIAL_CONTROL data field size


class MavlinkSerial:
    def __init__(self, port, baud=57600, heartbeat_timeout=8.0):
        if mavutil is None:
            raise RuntimeError("pymavlink is not installed")
        self._mav = mavutil.mavlink_connection(port, baud=baud)
        if not self._mav.wait_heartbeat(timeout=heartbeat_timeout):
            try:
                self._mav.close()
            except Exception:
                pass
            raise RuntimeError("no MAVLink heartbeat on %s" % port)

        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self._wq = queue.Queue()
        self._closed = False
        self._dead = False
        self._io = threading.Thread(target=self._io_loop, daemon=True)
        self._io.start()

    # -- MAVLink SERIAL_CONTROL helpers (called only from the io thread) --
    def _send_sc(self, data, exclusive):
        m = mavutil.mavlink
        flags = m.SERIAL_CONTROL_FLAG_RESPOND
        if exclusive:
            flags |= m.SERIAL_CONTROL_FLAG_EXCLUSIVE
        n = min(len(data), _CHUNK)
        payload = bytearray(data[:n]) + bytearray(_CHUNK - n)
        self._mav.mav.serial_control_send(
            m.SERIAL_CONTROL_DEV_SHELL, flags, 0, 0, n, payload)

    def _io_loop(self):
        while not self._closed:
            try:
                # flush queued writes (command lines from the broker)
                try:
                    while True:
                        data = self._wq.get_nowait()
                        for i in range(0, len(data), _CHUNK):
                            self._send_sc(data[i:i + _CHUNK], exclusive=True)
                except queue.Empty:
                    pass
                # poll the board for buffered output
                self._send_sc(b"", exclusive=False)
                for _ in range(50):
                    r = self._mav.recv_match(type="SERIAL_CONTROL",
                                             blocking=True, timeout=0.05)
                    if not r:
                        break
                    if r.count:
                        with self._buf_lock:
                            self._buf += bytes(r.data[:r.count])
            except Exception:
                # Device removed / port error -> mark dead so read() surfaces it
                # and the broker reconnects (re-resolving the by-id path).
                if not self._closed:
                    self._dead = True
                return
            time.sleep(0.02)

    # -- pyserial-lookalike surface -------------------------------------
    def read(self, size=4096):
        if self._dead:
            raise OSError("mavlink link lost")
        deadline = time.time() + 0.2
        while time.time() < deadline:
            with self._buf_lock:
                if self._buf:
                    out = bytes(self._buf[:size])
                    del self._buf[:size]
                    return out
            if self._dead:
                raise OSError("mavlink link lost")
            time.sleep(0.01)
        return b""

    def write(self, data):
        if self._dead:
            raise OSError("mavlink link lost")
        self._wq.put(bytes(data))
        return len(data)

    @property
    def in_waiting(self):
        with self._buf_lock:
            return len(self._buf)

    @property
    def closed(self):
        return self._closed

    def close(self):
        self._closed = True
        try:
            self._io.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._mav.close()
        except Exception:
            pass


def open_mavlink(port, baud):
    """serial_factory for console.Broker: a board's NSH over MAVLink."""
    return MavlinkSerial(port, baud)

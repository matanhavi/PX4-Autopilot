# PX4 Shared Console Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared NSH serial console to the `px4-hardware-debug` skill that both the user (via a tkinter window) and the skill (via a localhost TCP client) can drive, with timestamped logging and auto-reconnect, in headed or headless mode.

**Architecture:** A single port-owner process (`console.py`) opens `/dev/ttyUSB0`, runs a localhost TCP server, and logs everything. Headed mode also opens a tkinter window in the same process; headless skips it. The skill always talks over TCP via `console_client.py`, so its code path is identical in both modes. Pure logic (line buffering, logging, the broker, the client) lives in importable classes so it can be unit-tested without hardware using `os.openpty()` fakes.

**Tech Stack:** Python 3, `pyserial` (already installed), `tkinter` (stdlib, imported lazily), `socket`/`threading`/`queue` (stdlib), `pytest` for tests.

---

## File Structure

All paths under `/home/matan/src/PX4-Autopilot/.claude/skills/px4-hardware-debug/`:

- `console.py` — entry point + `LineBuffer`, `SessionLog`, `Broker`, `ConsoleGUI` classes and `main()`. The single owner of the serial port. `tkinter` is imported lazily inside `ConsoleGUI` so the module is importable in headless/test environments.
- `console_client.py` — `Console` class: the skill-side TCP helper (`send`/`read`/`read_until`/`ping`).
- `.gitignore` — ignores `logs/`.
- `logs/` — git-ignored session logs (created at runtime).
- `tests/conftest.py` — adds the skill dir to `sys.path`, provides the `PtyFakeSerial` fixture.
- `tests/test_linebuffer.py`, `tests/test_session_log.py`, `tests/test_broker.py`, `tests/test_console_client.py`.
- `SKILL.md` — updated with the headed/headless workflow and how to drive the console.

**Class interfaces (locked here so later tasks reference real names):**

```python
# console.py
class LineBuffer:
    def feed(self, text: str) -> list[str]: ...   # complete lines, '\r' stripped
    def flush(self) -> str | None: ...            # pending partial line or None

class SessionLog:
    def __init__(self, log_dir: str, now=datetime.now): ...
    path: str
    def log_input(self, source: str, text: str) -> None: ...   # source "USER"/"SKILL"
    def log_output(self, line: str) -> None: ...
    def log_status(self, msg: str) -> None: ...
    def close(self) -> None: ...

def open_serial(port: str, baud: int): ...        # production serial factory

class Broker:
    def __init__(self, port, baud, tcp_port, session_log,
                 serial_factory=open_serial, on_event=None): ...
    tcp_port: int                                 # actual bound port (after start)
    on_event: callable | None                     # on_event(kind, payload)
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def send(self, text: str, source: str) -> None: ...

class ConsoleGUI:
    def __init__(self, broker: "Broker", log_path: str): ...
    def run(self) -> None: ...

def main(argv=None) -> int: ...

# console_client.py
class Console:
    def __init__(self, host="127.0.0.1", port=8765, connect_timeout=2.0): ...
    statuses: list[str]
    def send(self, text: str) -> None: ...
    def read(self, timeout=1.0) -> str: ...
    def read_until(self, marker: str, timeout=5.0) -> str: ...
    def close(self) -> None: ...
    @staticmethod
    def ping(host="127.0.0.1", port=8765, timeout=1.0) -> bool: ...
```

`on_event` kinds: `"input"` → payload `(source, text)`; `"output"` → payload `line`; `"status"` → payload `msg`.

---

## Task 1: Scaffolding

**Files:**
- Create: `.claude/skills/px4-hardware-debug/.gitignore`
- Create: `.claude/skills/px4-hardware-debug/tests/conftest.py`

- [ ] **Step 1: Create the `.gitignore`**

Create `.claude/skills/px4-hardware-debug/.gitignore`:

```gitignore
logs/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 2: Create the test conftest with the PTY fake-serial fixture**

Create `.claude/skills/px4-hardware-debug/tests/conftest.py`:

```python
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
```

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/px4-hardware-debug/.gitignore .claude/skills/px4-hardware-debug/tests/conftest.py
git commit -m "chore(console): scaffold console feature (gitignore, test fixtures)"
```

---

## Task 2: LineBuffer

**Files:**
- Create: `.claude/skills/px4-hardware-debug/console.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_linebuffer.py`

- [ ] **Step 1: Write the failing tests**

Create `.claude/skills/px4-hardware-debug/tests/test_linebuffer.py`:

```python
from console import LineBuffer


def test_no_newline_buffers():
    lb = LineBuffer()
    assert lb.feed("abc") == []
    assert lb.flush() == "abc"
    assert lb.flush() is None


def test_single_line():
    lb = LineBuffer()
    assert lb.feed("abc\n") == ["abc"]
    assert lb.flush() is None


def test_multiple_lines_with_remainder():
    lb = LineBuffer()
    assert lb.feed("a\nb\nc") == ["a", "b"]
    assert lb.flush() == "c"


def test_crlf_is_stripped():
    lb = LineBuffer()
    assert lb.feed("abc\r\ndef") == ["abc"]
    assert lb.flush() == "def"


def test_split_across_feeds():
    lb = LineBuffer()
    assert lb.feed("ab") == []
    assert lb.feed("c\nde") == ["abc"]
    assert lb.flush() == "de"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_linebuffer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'console'` (or import error).

- [ ] **Step 3: Create console.py with the module header and LineBuffer**

Create `.claude/skills/px4-hardware-debug/console.py` with exactly this content for now:

```python
#!/usr/bin/env python3
"""PX4 shared NSH console: single serial-port owner with a localhost TCP
interface and an optional tkinter GUI. See the px4-hardware-debug SKILL.md."""

import argparse
import os
import queue
import signal
import socket
import sys
import threading
from datetime import datetime

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - serial only needed at runtime
    serial = None

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
DEFAULT_TCP_PORT = 8765


class LineBuffer:
    """Accumulates text and yields complete lines (trailing '\\r' stripped)."""

    def __init__(self):
        self._buf = ""

    def feed(self, text):
        self._buf += text
        lines = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            lines.append(line.rstrip("\r"))
        return lines

    def flush(self):
        if self._buf == "":
            return None
        pending = self._buf.rstrip("\r")
        self._buf = ""
        return pending
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_linebuffer.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py .claude/skills/px4-hardware-debug/tests/test_linebuffer.py
git commit -m "feat(console): add LineBuffer for serial line framing"
```

---

## Task 3: SessionLog

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_session_log.py`

- [ ] **Step 1: Write the failing tests**

Create `.claude/skills/px4-hardware-debug/tests/test_session_log.py`:

```python
from datetime import datetime

from console import SessionLog


def _fixed_now():
    return datetime(2026, 6, 8, 14, 3, 21, 412000)


def test_creates_timestamped_file(tmp_path):
    log = SessionLog(str(tmp_path), now=_fixed_now)
    assert log.path.endswith("session-20260608-140321.log")
    assert (tmp_path / "session-20260608-140321.log").exists()
    log.close()


def test_input_format(tmp_path):
    log = SessionLog(str(tmp_path), now=_fixed_now)
    log.log_input("USER", "ver all")
    log.close()
    content = (tmp_path / "session-20260608-140321.log").read_text()
    assert content == "2026-06-08T14:03:21.412  USER  > ver all\n"


def test_output_and_status_format(tmp_path):
    log = SessionLog(str(tmp_path), now=_fixed_now)
    log.log_output("HW arch: AERIUM_RADIAN_H7_REV_B")
    log.log_status("connected ttyUSB0@57600")
    log.close()
    lines = (tmp_path / "session-20260608-140321.log").read_text().splitlines()
    assert lines[0] == "2026-06-08T14:03:21.412  OUT   HW arch: AERIUM_RADIAN_H7_REV_B"
    assert lines[1] == "2026-06-08T14:03:21.412  STAT  connected ttyUSB0@57600"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_session_log.py -v`
Expected: FAIL — `ImportError: cannot import name 'SessionLog'`.

- [ ] **Step 3: Add SessionLog to console.py**

In `console.py`, add after the `LineBuffer` class:

```python
class SessionLog:
    """Writes a timestamped transcript: one event per line, source-tagged."""

    def __init__(self, log_dir, now=datetime.now):
        self._now = now
        os.makedirs(log_dir, exist_ok=True)
        stamp = now().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(log_dir, "session-%s.log" % stamp)
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")
        self._lock = threading.Lock()

    def _write(self, tag, rest):
        ts = self._now().isoformat(timespec="milliseconds")
        with self._lock:
            self._fh.write("%s  %-5s %s\n" % (ts, tag, rest))

    def log_input(self, source, text):
        self._write(source, "> " + text)

    def log_output(self, line):
        self._write("OUT", line)

    def log_status(self, msg):
        self._write("STAT", msg)

    def close(self):
        with self._lock:
            if not self._fh.closed:
                self._fh.close()
```

Note: `"%-5s"` pads the tag to 5 chars, so `USER`→`USER ` then a literal space gives `USER  >` and `OUT`→`OUT  ` gives `OUT   line`, matching the spec format.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_session_log.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py .claude/skills/px4-hardware-debug/tests/test_session_log.py
git commit -m "feat(console): add SessionLog timestamped transcript writer"
```

---

## Task 4: Broker — output fan-out + logging

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_broker.py`

- [ ] **Step 1: Write the failing tests**

Create `.claude/skills/px4-hardware-debug/tests/test_broker.py`:

```python
import socket

from console import Broker, SessionLog
from conftest import wait_for


def _client(broker):
    # Wait for the serial link so the greeting is "connected", not "disconnected".
    assert wait_for(lambda: broker.is_connected, timeout=2.0)
    s = socket.create_connection(("127.0.0.1", broker.tcp_port), timeout=2.0)
    s.settimeout(2.0)
    return s


def _readlines(sock, count, timeout=2.0):
    sock.settimeout(timeout)
    f = sock.makefile("r")
    out = []
    for _ in range(count):
        line = f.readline()
        if not line:
            break
        out.append(line.rstrip("\n"))
    return out


def test_device_output_fans_out_to_clients_and_log(tmp_path, pty_serial):
    log = SessionLog(str(tmp_path))
    events = []
    broker = Broker("ignored", 57600, 0, log,
                    serial_factory=lambda *_: pty_serial,
                    on_event=lambda kind, payload: events.append((kind, payload)))
    broker.start()
    try:
        client = _client(broker)
        # Drain the initial STATUS connected line.
        first = _readlines(client, 1)
        assert first == ["STATUS connected ignored 57600"]
        pty_serial.feed_device(b"hello world\r\n")
        assert _readlines(client, 1) == ["OUT hello world"]
        assert wait_for(lambda: ("output", "hello world") in events)
        client.close()
    finally:
        broker.stop()
        log.close()
    assert "OUT   hello world" in (tmp_path / log.path.split("/")[-1]).read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_broker.py -v`
Expected: FAIL — `ImportError: cannot import name 'Broker'`.

- [ ] **Step 3: Add `open_serial` and the Broker (serial read + TCP fan-out) to console.py**

In `console.py`, add after `SessionLog`:

```python
def open_serial(port, baud):
    if serial is None:
        raise RuntimeError("pyserial is not installed")
    return serial.Serial(port, baud, timeout=0.2)


class Broker:
    """Owns the serial port; fans output out to TCP clients, the log, and a
    GUI callback; accepts input from TCP clients (and the GUI via send())."""

    def __init__(self, port, baud, tcp_port, session_log,
                 serial_factory=open_serial, on_event=None):
        self._port = port
        self._baud = baud
        self._log = session_log
        self._serial_factory = serial_factory
        self.on_event = on_event

        self._serial = None
        self._serial_lock = threading.Lock()
        self._linebuf = LineBuffer()

        self._clients = set()
        self._clients_lock = threading.Lock()

        self._running = False
        self._connected = False
        self._disc_announced = False
        self._threads = []

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", tcp_port))
        self._srv.listen(8)
        self.tcp_port = self._srv.getsockname()[1]

    # ---- lifecycle ----------------------------------------------------
    def start(self):
        self._running = True
        for target in (self._serial_loop, self._accept_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    @property
    def is_connected(self):
        return self._connected

    def stop(self):
        self._running = False
        try:
            self._srv.close()
        except OSError:
            pass
        with self._serial_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except OSError:
                    pass
                self._serial = None
        with self._clients_lock:
            for c in list(self._clients):
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()

    # ---- fan-out helpers ----------------------------------------------
    def _emit(self, kind, payload):
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # pragma: no cover - GUI callback must not kill us
                pass

    def _broadcast(self, message):
        data = (message + "\n").encode("utf-8", "replace")
        with self._clients_lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(data)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._clients.discard(c)

    def _set_status(self, msg):
        self._log.log_status(msg)
        self._broadcast("STATUS " + msg)
        self._emit("status", msg)

    # ---- serial side --------------------------------------------------
    def _serial_loop(self):
        while self._running:
            with self._serial_lock:
                have = self._serial is not None
            if not have:
                self._try_open()
                continue
            try:
                data = self._serial.read(4096)
            except Exception:  # device removed mid-read
                self._handle_drop()
                continue
            if data:
                for line in self._linebuf.feed(data.decode("utf-8", "replace")):
                    self._log.log_output(line)
                    self._broadcast("OUT " + line)
                    self._emit("output", line)
            else:
                threading.Event().wait(0.02)

    def _announce_disconnected(self):
        if not self._disc_announced:
            self._disc_announced = True
            self._connected = False
            self._set_status("disconnected")

    def _try_open(self):
        try:
            ser = self._serial_factory(self._port, self._baud)
        except Exception:
            self._announce_disconnected()
            threading.Event().wait(1.0)
            return
        with self._serial_lock:
            self._serial = ser
        self._connected = True
        self._disc_announced = False
        self._set_status("connected %s %s" % (self._port, self._baud))

    def _handle_drop(self):
        with self._serial_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except OSError:
                    pass
                self._serial = None
        self._announce_disconnected()

    # ---- TCP side -----------------------------------------------------
    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            with self._clients_lock:
                self._clients.add(conn)
            t = threading.Thread(target=self._client_loop, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _client_loop(self, conn):
        # Greet new client with current connection status (under the clients
        # lock so it can't interleave with a concurrent broadcast on this sock).
        status = ("connected %s %s" % (self._port, self._baud)
                  if self._connected else "disconnected")
        with self._clients_lock:
            try:
                conn.sendall(("STATUS " + status + "\n").encode())
            except OSError:
                return
        f = conn.makefile("r")
        try:
            while self._running:
                line = f.readline()
                if not line:
                    break
                line = line.rstrip("\n")
                if line.startswith("SEND "):
                    self.send(line[5:], "SKILL")
                elif line == "PING":
                    with self._clients_lock:
                        try:
                            conn.sendall(b"PONG\n")
                        except OSError:
                            break
        finally:
            with self._clients_lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    # ---- input --------------------------------------------------------
    def send(self, text, source):
        with self._serial_lock:
            if self._serial is not None:
                try:
                    self._serial.write((text + "\r").encode("utf-8", "replace"))
                except OSError:
                    pass
        self._log.log_input(source, text)
        self._emit("input", (source, text))
```

Note: input events are logged and sent to the GUI callback but are **not** broadcast to TCP clients — the device echoes typed input back as `OUT` lines, which is what the skill reads.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_broker.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py .claude/skills/px4-hardware-debug/tests/test_broker.py
git commit -m "feat(console): add Broker serial read, TCP fan-out, and logging"
```

---

## Task 5: Broker — SEND input and PING/PONG

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/tests/test_broker.py`

(The implementation already exists from Task 4; these tests lock the behavior in.)

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_broker.py`:

```python
def test_send_writes_to_device_and_logs(tmp_path, pty_serial):
    log = SessionLog(str(tmp_path))
    broker = Broker("ignored", 57600, 0, log,
                    serial_factory=lambda *_: pty_serial)
    broker.start()
    try:
        client = _client(broker)
        _readlines(client, 1)  # drain STATUS
        client.sendall(b"SEND ver all\n")
        assert wait_for(lambda: pty_serial.read_host() == b"ver all\r")
    finally:
        broker.stop()
        log.close()
    assert "SKILL  > ver all" in (tmp_path / log.path.split("/")[-1]).read_text()


def test_ping_pong(tmp_path, pty_serial):
    log = SessionLog(str(tmp_path))
    broker = Broker("ignored", 57600, 0, log,
                    serial_factory=lambda *_: pty_serial)
    broker.start()
    try:
        client = _client(broker)
        _readlines(client, 1)  # drain STATUS
        client.sendall(b"PING\n")
        assert _readlines(client, 1) == ["PONG"]
    finally:
        broker.stop()
        log.close()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_broker.py -v`
Expected: PASS (3 passed). If `test_send_writes_to_device_and_logs` fails on the read, confirm `Broker.send` writes `text + "\r"`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/px4-hardware-debug/tests/test_broker.py
git commit -m "test(console): cover Broker SEND input and PING/PONG"
```

---

## Task 6: Broker — auto-reconnect / status transitions

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/tests/test_broker.py`

(Implementation exists from Task 4; this verifies the reconnect path.)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_broker.py`:

```python
def test_reconnect_after_initial_failure(tmp_path, pty_serial):
    log = SessionLog(str(tmp_path))
    calls = {"n": 0}

    def flaky_factory(port, baud):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("device not ready")
        return pty_serial

    statuses = []
    broker = Broker("ttyUSB0", 57600, 0, log,
                    serial_factory=flaky_factory,
                    on_event=lambda kind, payload: (
                        statuses.append(payload) if kind == "status" else None))
    broker.start()
    try:
        assert wait_for(lambda: "disconnected" in statuses, timeout=3.0)
        assert wait_for(lambda: any(s.startswith("connected") for s in statuses),
                        timeout=3.0)
    finally:
        broker.stop()
        log.close()
    # disconnected must appear before connected
    assert statuses.index("disconnected") < next(
        i for i, s in enumerate(statuses) if s.startswith("connected"))
```

- [ ] **Step 2: Run the test**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_broker.py::test_reconnect_after_initial_failure -v`
Expected: PASS. The factory raises once → broker emits `disconnected`, waits ~1s, retries → succeeds → emits `connected`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/px4-hardware-debug/tests/test_broker.py
git commit -m "test(console): cover Broker auto-reconnect status transitions"
```

---

## Task 7: Console client (skill-side TCP helper)

**Files:**
- Create: `.claude/skills/px4-hardware-debug/console_client.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_console_client.py`

- [ ] **Step 1: Write the failing tests**

Create `.claude/skills/px4-hardware-debug/tests/test_console_client.py`:

```python
import pytest

from console import Broker, SessionLog
from console_client import Console
from conftest import wait_for


def _broker(tmp_path, pty_serial):
    log = SessionLog(str(tmp_path))
    broker = Broker("ttyUSB0", 57600, 0, log,
                    serial_factory=lambda *_: pty_serial)
    broker.start()
    assert wait_for(lambda: broker.is_connected, timeout=2.0)
    return broker, log


def test_ping_false_when_nothing_listening():
    assert Console.ping(port=59999, timeout=0.5) is False


def test_send_and_read_roundtrip(tmp_path, pty_serial):
    broker, log = _broker(tmp_path, pty_serial)
    try:
        c = Console(port=broker.tcp_port)
        # The skill sends a command; the 'device' echoes a reply.
        c.send("ver all")
        assert wait_for(lambda: pty_serial.read_host() == b"ver all\r")
        pty_serial.feed_device(b"HW arch: AERIUM_RADIAN_H7_REV_B\r\n")
        out = c.read_until("AERIUM", timeout=2.0)
        assert "HW arch: AERIUM_RADIAN_H7_REV_B" in out
        c.close()
    finally:
        broker.stop()
        log.close()


def test_ping_true_against_running_broker(tmp_path, pty_serial):
    broker, log = _broker(tmp_path, pty_serial)
    try:
        assert Console.ping(port=broker.tcp_port, timeout=1.0) is True
    finally:
        broker.stop()
        log.close()


def test_connect_error_is_clear():
    with pytest.raises(ConnectionError) as exc:
        Console(port=59999, connect_timeout=0.5)
    assert "no console broker" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_console_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'console_client'`.

- [ ] **Step 3: Implement console_client.py**

Create `.claude/skills/px4-hardware-debug/console_client.py`:

```python
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
        self.statuses = []
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
            if line.startswith("OUT "):
                self._q.put(line[4:])
            elif line.startswith("STATUS "):
                self.statuses.append(line[7:])
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

    def close(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass

    @staticmethod
    def ping(host="127.0.0.1", port=DEFAULT_TCP_PORT, timeout=1.0):
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.sendall(b"PING\n")
                s.settimeout(timeout)
                return b"PONG" in s.recv(64)
        except OSError:
            return False


if __name__ == "__main__":
    # Tiny CLI: `console_client.py ver all` sends and prints ~2s of output.
    c = Console()
    if len(sys.argv) > 1:
        c.send(" ".join(sys.argv[1:]))
    print(c.read(timeout=2.0))
    c.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_console_client.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console_client.py .claude/skills/px4-hardware-debug/tests/test_console_client.py
git commit -m "feat(console): add console_client skill-side TCP helper"
```

---

## Task 8: Entry point — already-running guard + main()

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_broker.py`

- [ ] **Step 1: Write the failing test for the already-running guard**

Append to `tests/test_broker.py`:

```python
from console import main


def test_main_refuses_when_broker_already_running(tmp_path, pty_serial, capsys, monkeypatch):
    log = SessionLog(str(tmp_path))
    running = Broker("ttyUSB0", 57600, 0, log,
                     serial_factory=lambda *_: pty_serial)
    running.start()
    try:
        rc = main(["--tcp-port", str(running.tcp_port), "--headless"])
        assert rc == 1
        assert "already running" in capsys.readouterr().err
    finally:
        running.stop()
        log.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_broker.py::test_main_refuses_when_broker_already_running -v`
Expected: FAIL — `ImportError: cannot import name 'main'`.

- [ ] **Step 3: Add argument parsing and main() to console.py**

Append to the end of `console.py` (before any `if __name__` block; add the block in this step):

```python
def _parse_args(argv):
    p = argparse.ArgumentParser(description="PX4 shared NSH console broker")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=57600)
    p.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    p.add_argument("--headless", action="store_true",
                   help="run without the GUI window")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    # Import here so the module stays importable where console_client isn't on
    # the path during isolated unit tests of LineBuffer/SessionLog.
    from console_client import Console as _C
    if _C.ping(port=args.tcp_port, timeout=0.5):
        sys.stderr.write(
            "A console broker is already running on 127.0.0.1:%d; refusing to "
            "start a second owner of %s.\n" % (args.tcp_port, args.port))
        return 1

    log = SessionLog(LOG_DIR)
    broker = Broker(args.port, args.baud, args.tcp_port, log)
    broker.start()

    if args.headless:
        sys.stdout.write(
            "[console] headless broker on 127.0.0.1:%d, logging to %s\n"
            % (broker.tcp_port, log.path))
        sys.stdout.flush()
        stop = threading.Event()

        def _stop(*_):
            stop.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        stop.wait()
    else:
        gui = ConsoleGUI(broker, log.path)
        gui.run()

    broker.stop()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: `main` references `ConsoleGUI`, which is added in Task 9. The headless test path in Step 1 never reaches the `else` branch, so this test passes before `ConsoleGUI` exists **only if** `ConsoleGUI` is at least defined. To keep things runnable, add a minimal placeholder now and flesh it out in Task 9. Add this stub above `_parse_args`:

```python
class ConsoleGUI:
    def __init__(self, broker, log_path):
        self._broker = broker
        self._log_path = log_path

    def run(self):  # pragma: no cover - replaced in Task 9
        raise NotImplementedError("GUI implemented in Task 9")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_broker.py::test_main_refuses_when_broker_already_running -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py .claude/skills/px4-hardware-debug/tests/test_broker.py
git commit -m "feat(console): add main() entry point with already-running guard"
```

---

## Task 9: tkinter GUI

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_console_client.py` (GUI smoke test, skip-guarded)

- [ ] **Step 1: Write the skip-guarded smoke test**

Append to `tests/test_console_client.py`:

```python
def test_gui_instantiates_and_shows_events(tmp_path, pty_serial):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.destroy()

    from console import Broker, SessionLog, ConsoleGUI
    log = SessionLog(str(tmp_path))
    broker = Broker("ttyUSB0", 57600, 0, log,
                    serial_factory=lambda *_: pty_serial)
    broker.start()
    try:
        gui = ConsoleGUI(broker, log.path)
        gui.build()  # create widgets without entering mainloop
        gui._on_event("output", "hello from device")
        gui._drain_events()
        text = gui._text.get("1.0", "end")
        assert "hello from device" in text
        gui.destroy()
    finally:
        broker.stop()
        log.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_console_client.py::test_gui_instantiates_and_shows_events -v`
Expected: FAIL — `AttributeError` (`build`/`_on_event`/`_drain_events` not defined) or skip if no display. If it skips, temporarily note that and proceed; the implementation must still be written.

- [ ] **Step 3: Replace the ConsoleGUI stub with the full implementation**

In `console.py`, replace the `ConsoleGUI` stub (from Task 8) with:

```python
class ConsoleGUI:
    """tkinter window: read-only output pane, input box, Clear button, status.

    The broker calls on_event() from background threads, so events are pushed
    onto a thread-safe queue and drained on the tkinter main thread via after().
    """

    COLORS = {
        "USER": "#5fd75f",    # green
        "SKILL": "#5fd7ff",   # cyan
        "OUT": "#d0d0d0",     # grey
        "STAT": "#ffd75f",    # yellow
    }

    def __init__(self, broker, log_path):
        self._broker = broker
        self._log_path = log_path
        self._events = queue.Queue()
        self._history = []
        self._hist_idx = None
        self._autoscroll = True
        broker.on_event = self._on_event

    # Called from broker threads — only enqueue, never touch tkinter here.
    def _on_event(self, kind, payload):
        self._events.put((kind, payload))

    def build(self):
        import tkinter as tk
        self._tk = tk
        self._root = tk.Tk()
        self._root.title("PX4 Console - %s @ %d"
                         % (self._broker_port(), self._broker._baud))
        self._root.configure(bg="#1c1c1c")

        top = tk.Frame(self._root, bg="#1c1c1c")
        top.pack(fill="x")
        self._status = tk.Label(top, text="● connecting",
                                fg="#ffd75f", bg="#1c1c1c", anchor="e")
        self._status.pack(side="right", padx=6, pady=2)

        self._text = tk.Text(self._root, bg="#101010", fg="#d0d0d0",
                             insertbackground="#d0d0d0",
                             font=("monospace", 11), state="disabled", wrap="char")
        self._text.pack(fill="both", expand=True)
        for tag, color in self.COLORS.items():
            self._text.tag_configure(tag, foreground=color)
        self._text.bind("<MouseWheel>", self._pause_autoscroll)
        self._text.bind("<Button-4>", self._pause_autoscroll)
        self._text.bind("<Button-5>", self._pause_autoscroll)

        bottom = tk.Frame(self._root, bg="#1c1c1c")
        bottom.pack(fill="x")
        tk.Label(bottom, text=">", fg="#d0d0d0", bg="#1c1c1c").pack(side="left")
        self._entry = tk.Entry(bottom, bg="#101010", fg="#d0d0d0",
                               insertbackground="#d0d0d0")
        self._entry.pack(side="left", fill="x", expand=True, padx=4, pady=4)
        self._entry.bind("<Return>", self._on_enter)
        self._entry.bind("<Up>", self._history_prev)
        self._entry.bind("<Down>", self._history_next)
        tk.Button(bottom, text="Clear", command=self._clear).pack(side="right", padx=4)
        self._entry.focus_set()

    def _broker_port(self):
        return getattr(self._broker, "_port", "ttyUSB0")

    def _append(self, tag, text):
        self._text.configure(state="normal")
        self._text.insert("end", text + "\n", tag)
        self._text.configure(state="disabled")
        if self._autoscroll:
            self._text.see("end")

    def _drain_events(self):
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "output":
                self._append("OUT", payload)
            elif kind == "input":
                source, text = payload
                self._append(source, "nsh> " + text)
            elif kind == "status":
                self._append("STAT", "[" + payload + "]")
                self._update_status(payload)

    def _update_status(self, payload):
        if payload.startswith("connected"):
            self._status.configure(text="● connected", fg="#5fd75f")
        else:
            self._status.configure(text="● disconnected - reconnecting",
                                   fg="#ff5f5f")

    def _pump(self):
        self._drain_events()
        self._root.after(50, self._pump)

    # ---- input handlers ----
    def _on_enter(self, _event):
        text = self._entry.get()
        if text:
            self._history.append(text)
        self._hist_idx = None
        self._entry.delete(0, "end")
        self._broker.send(text, "USER")
        return "break"

    def _history_prev(self, _event):
        if not self._history:
            return "break"
        self._hist_idx = (len(self._history) - 1 if self._hist_idx is None
                          else max(0, self._hist_idx - 1))
        self._set_entry(self._history[self._hist_idx])
        return "break"

    def _history_next(self, _event):
        if self._hist_idx is None:
            return "break"
        if self._hist_idx >= len(self._history) - 1:
            self._hist_idx = None
            self._set_entry("")
        else:
            self._hist_idx += 1
            self._set_entry(self._history[self._hist_idx])
        return "break"

    def _set_entry(self, value):
        self._entry.delete(0, "end")
        self._entry.insert(0, value)

    def _pause_autoscroll(self, _event):
        # Re-enable autoscroll only when the view is at the bottom.
        try:
            self._autoscroll = self._text.yview()[1] >= 0.999
        except Exception:
            pass

    def _clear(self):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def destroy(self):
        try:
            self._root.destroy()
        except Exception:
            pass

    def run(self):
        self.build()
        self._pump()
        self._root.mainloop()
```

Note: `_drain_events` is called directly by the smoke test; `_pump` is only used
under `run()` to poll the queue on the tkinter main thread every 50 ms.

- [ ] **Step 4: Run the GUI smoke test**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest tests/test_console_client.py::test_gui_instantiates_and_shows_events -v`
Expected: PASS, or SKIP if no display/tkinter. If it runs, the assertion on pane text must pass.

- [ ] **Step 5: Run the whole suite**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest -v`
Expected: all pass (GUI test may skip).

- [ ] **Step 6: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py .claude/skills/px4-hardware-debug/tests/test_console_client.py
git commit -m "feat(console): add tkinter GUI with colors, history, clear, reconnect status"
```

---

## Task 10: SKILL.md documentation

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/SKILL.md`

- [ ] **Step 1: Add the headed/headless question to §0 (Session setup)**

In SKILL.md §0, after the existing question 4 ("Is this a new board bring-up..."), add:

```markdown
5. **Headed or headless console?** Headed opens a live terminal window
   (`console.py`) that both you and the skill share in real time; headless runs
   the same broker without a window. Either way every byte is logged to
   `logs/session-*.log`. Default to **headed** unless the user has no display.
```

- [ ] **Step 2: Add a "Shared console" subsection to §2 (NSH shell access)**

In SKILL.md §2, immediately under the "### Preferred: Physical UART console"
heading's intro, add this subsection before the `picocom`/`minicom` block:

````markdown
### Shared console (preferred for agent + user together)

`console.py` owns `/dev/ttyUSB0` and exposes a localhost TCP interface so the
user (GUI window) and the skill can drive the same console at once, with full
logging and auto-reconnect across reboot/flash.

Start it once at the beginning of the session (pick the mode from §0 Q5):

```bash
# headed (opens the window):
python3 .claude/skills/px4-hardware-debug/console.py &
# headless (no window, same logging + TCP interface):
python3 .claude/skills/px4-hardware-debug/console.py --headless &
```

Drive it from the skill (identical in both modes):

```python
import sys
sys.path.insert(0, ".claude/skills/px4-hardware-debug")
from console_client import Console

c = Console()                 # connects to the running broker
c.send("ver all")            # fire-and-forget
print(c.read_until("nsh>", timeout=5.0))   # quick command
c.send("uorb top")           # streaming: keep reading, then ^C to stop
# ... loop c.read() as needed ...
c.send("\x03")
c.close()
```

The broker refuses to start if one is already running on its TCP port, so it is
safe to attempt a start at session begin. Session logs land in
`.claude/skills/px4-hardware-debug/logs/` (git-ignored).
````

- [ ] **Step 3: Verify the docs render and reference real commands**

Run: `cd .claude/skills/px4-hardware-debug && python3 -c "import console, console_client; print('imports OK')"`
Expected: `imports OK` (confirms the files referenced in the docs import cleanly).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/px4-hardware-debug/SKILL.md
git commit -m "docs(skill): document headed/headless shared console workflow"
```

---

## Final verification

- [ ] **Run the full suite once more**

Run: `cd .claude/skills/px4-hardware-debug && python3 -m pytest -v`
Expected: all tests pass (GUI smoke test may skip if no display).

- [ ] **Confirm logs are git-ignored**

Run: `cd /home/matan/src/PX4-Autopilot && python3 .claude/skills/px4-hardware-debug/console.py --headless --tcp-port 8799 & sleep 1; git status --porcelain .claude/skills/px4-hardware-debug/logs/; kill %1`
Expected: no output from `git status` for `logs/` (the directory and its `session-*.log` are ignored).

- [ ] **Manual hardware check (when a board is attached)**

Run headed against the real board, type a command in the window, send one from
the skill via `console_client`, flash the board and confirm the status flips to
`disconnected - reconnecting` then back to `connected` without restarting the
window, and confirm `logs/session-*.log` contains USER/SKILL/OUT/STAT lines.

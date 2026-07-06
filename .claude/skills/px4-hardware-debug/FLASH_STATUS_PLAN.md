# Console Flash-Status Feature — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the shared console (`console.py`) trigger a firmware flash itself and show live flash status as a line in its header banner (and over the TCP/log stream), so the flasher and the console are one tool — no gap where status has to be pushed in manually.

**Architecture:** Add a `FLASH <target>` verb to the broker's existing line-based TCP protocol. The broker runs `make <target> upload` as a subprocess with `PYTHONUNBUFFERED=1`, splits its output on CR/LF (px_uploader draws progress bars with `\r`), parses each line into a normalized status string, and pushes that string through the *existing* fan-out (`_broadcast` → TCP, `_emit` → GUI, `SessionLog` → file) exactly like the `board`/`status` headers already do. The GUI adds a `Flash:` label updated from a new `"flash"` event. Because the broker owns the FTDI, the post-flash boot log lands in the same pane right after the flash line.

**Tech Stack:** Python 3 stdlib only (`subprocess`, `re`, `threading`, `os`), tkinter (GUI, optional — headless must still work), PX4 `make <target> upload` (`Tools/px_uploader.py`).

## Global Constraints

- **Stdlib only** — no new pip dependencies (matches current `console.py`).
- **Style:** `%`-formatting (not f-strings), 4-space indent, `daemon=True` threads — match the existing file.
- **Headless parity:** every feature must work with `--headless` (no tkinter). Flash status still goes to the log + TCP; only the GUI label is GUI-gated.
- **Additive protocol only:** do not change the existing `SEND`/`PING`/`OUT`/`STATUS`/`BOARD`/`PONG` lines. Add `FLASH <...>` as a new line kind.
- **No port conflict:** the console owns `/dev/ttyUSB0` (FTDI); the flash goes over the board USB (`/dev/ttyACM*`) via `make upload`. Never open the board USB from `console.py`.
- **Repo root** is resolved from the skill file location: `<repo>/.claude/skills/px4-hardware-debug/console.py` → repo root is `os.path.join(dirname(__file__), "..", "..", "..")`.

---

## File Structure

- **Create `flash_status.py`** (skill folder) — pure, importable, testable: `parse_flash_line(line)`, `build_flash_cmd(target, repo_root)`, `run_flash(cmd, on_status, on_done, cwd, env)`. No tkinter, no broker imports.
- **Create `tests/test_flash_status.py`** — unit tests for the above (runnable with plain `python3`, no pytest needed).
- **Modify `console.py`** — `Broker`: add `_set_flash_status`, `flash`, and a `FLASH` case in `_client_loop`. `ConsoleGUI`: add a `Flash:` label, a `"flash"` case in `_drain_events`, and `_update_flash`.
- **Modify `console_client.py`** — `Console.flash(target)`, capture `FLASH` lines, `wait_flash_done()`, and a `--flash` CLI path.
- **Modify `SKILL.md`** — document the flash-from-console workflow + WSL caveat.

Keeping the parser/runner in `flash_status.py` (not inside `console.py`) is what makes Tasks 1–2 unit-testable without constructing a `Broker` or a tkinter root.

---

### Task 1: Flash line parser

**Files:**
- Create: `.claude/skills/px4-hardware-debug/flash_status.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_flash_status.py`

**Interfaces:**
- Produces: `parse_flash_line(line: str) -> Optional[str]` — maps one raw uploader output token to a normalized status like `"erasing 43%"`, `"programming 78%"`, `"verifying 30%"`, `"waiting for bootloader"`, `"found board 1803"`, `"loaded firmware"`, `"rebooting"`, or `"error: <msg>"`. Returns `None` for lines that carry no status.

- [ ] **Step 1: Write the failing test**

Create `tests/test_flash_status.py`:

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flash_status import parse_flash_line


def test_parse_flash_line():
    assert parse_flash_line("Erase  : [==        ] 20.0%") == "erasing 20%"
    assert parse_flash_line("Program: [========  ] 80.0%") == "programming 80%"
    assert parse_flash_line("Verify : [===       ] 30.0%") == "verifying 30%"
    assert parse_flash_line("Waiting for bootloader...") == "waiting for bootloader"
    assert parse_flash_line("Found board id: 1803") == "found board 1803"
    assert parse_flash_line("Loaded firmware for 9,0") == "loaded firmware"
    assert parse_flash_line("Rebooting. Elapsed Time 27.5") == "rebooting"
    assert parse_flash_line("ERROR: Board not responding").startswith("error:")
    assert parse_flash_line("[100%] Built target foo") is None
    assert parse_flash_line("") is None


if __name__ == "__main__":
    test_parse_flash_line()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_flash_status.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'flash_status'`.

- [ ] **Step 3: Write minimal implementation**

Create `flash_status.py`:

```python
#!/usr/bin/env python3
"""Flash-status helpers for the PX4 console broker (see console.py / SKILL.md).

Pure and importable: no tkinter, no broker. Parses `make <target> upload`
output into short status strings and runs the flash subprocess.
"""

import os
import re
import subprocess

_PCT = re.compile(r"^(Erase|Program|Verify)\s*:.*?(\d+)(?:\.\d+)?%")
_VERB = {"Erase": "erasing", "Program": "programming", "Verify": "verifying"}
_BOARD = re.compile(r"Found board id[: ]+(\w+)", re.IGNORECASE)


def parse_flash_line(line):
    """Map one raw uploader output token to a short status, or None."""
    line = line.strip()
    if not line:
        return None
    m = _PCT.match(line)
    if m:
        return "%s %s%%" % (_VERB[m.group(1)], m.group(2))
    low = line.lower()
    if "waiting for bootloader" in low:
        return "waiting for bootloader"
    m = _BOARD.search(line)
    if m:
        return "found board %s" % m.group(1)
    if low.startswith("loaded firmware"):
        return "loaded firmware"
    if low.startswith("rebooting"):
        return "rebooting"
    if low.startswith("error") or "not responding" in low or "no such file" in low:
        return "error: " + line
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_flash_status.py`
Expected: prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/flash_status.py \
        .claude/skills/px4-hardware-debug/tests/test_flash_status.py
git commit -m "feat(px4-hardware-debug): add flash-output line parser"
```

---

### Task 2: Flash command builder + subprocess runner

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/flash_status.py`
- Test: `.claude/skills/px4-hardware-debug/tests/test_flash_status.py`

**Interfaces:**
- Consumes: `parse_flash_line` (Task 1).
- Produces:
  - `build_flash_cmd(target: str, repo_root: str) -> list[str]` → `["make", "-C", repo_root, target, "upload"]`.
  - `run_flash(cmd, on_status, on_done, cwd=None, env=None) -> None` — runs `cmd`, calls `on_status(str)` for every non-None `parse_flash_line`, then `on_done(str)` once (`"done"` on rc 0, `"error: exit N"` otherwise, `"error: <ex>"` if spawn failed). Sets `PYTHONUNBUFFERED=1` and splits stdout on CR **and** LF so `\r` progress bars are seen live.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flash_status.py` (and add the two calls to `__main__`):

```python
from flash_status import build_flash_cmd, run_flash


def test_build_flash_cmd():
    assert build_flash_cmd("aerium_apex_h7_rev_a_default", "/repo") == \
        ["make", "-C", "/repo", "aerium_apex_h7_rev_a_default", "upload"]


def test_run_flash_parses_stream_and_finishes():
    script = (
        "import sys\n"
        "sys.stdout.write('Loaded firmware for 9,0\\n')\n"
        "sys.stdout.write('Erase  : [==        ] 20.0%\\r')\n"
        "sys.stdout.write('Program: [========  ] 80.0%\\r')\n"
        "sys.stdout.write('Rebooting. Elapsed Time 27\\n')\n"
    )
    got = []
    done = []
    run_flash([sys.executable, "-c", script], got.append, done.append)
    assert "loaded firmware" in got
    assert "erasing 20%" in got
    assert "programming 80%" in got
    assert "rebooting" in got
    assert done == ["done"]


def test_run_flash_reports_nonzero_exit():
    got, done = [], []
    run_flash([sys.executable, "-c", "import sys; sys.exit(3)"], got.append, done.append)
    assert done and done[0].startswith("error: exit 3")
```

Update `__main__`:

```python
if __name__ == "__main__":
    test_parse_flash_line()
    test_build_flash_cmd()
    test_run_flash_parses_stream_and_finishes()
    test_run_flash_reports_nonzero_exit()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_flash_status.py`
Expected: FAIL — `ImportError: cannot import name 'build_flash_cmd'`.

- [ ] **Step 3: Write minimal implementation**

Append to `flash_status.py`:

```python
def build_flash_cmd(target, repo_root):
    return ["make", "-C", repo_root, target, "upload"]


def run_flash(cmd, on_status, on_done, cwd=None, env=None):
    """Run `cmd`, streaming parsed statuses to on_status, then on_done once."""
    e = dict(os.environ)
    e["PYTHONUNBUFFERED"] = "1"          # force px_uploader progress to flush
    if env:
        e.update(env)
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=e, bufsize=0,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as ex:
        on_done("error: %s" % ex)
        return
    buf = ""
    try:
        while True:
            chunk = p.stdout.read(256)   # raw stream (bufsize=0): returns what's available
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()            # keep the incomplete tail
            for part in parts:
                st = parse_flash_line(part)
                if st:
                    on_status(st)
    finally:
        p.wait()
        tail = parse_flash_line(buf)
        if tail:
            on_status(tail)
        on_done("done" if p.returncode == 0 else "error: exit %d" % p.returncode)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_flash_status.py`
Expected: prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/flash_status.py \
        .claude/skills/px4-hardware-debug/tests/test_flash_status.py
git commit -m "feat(px4-hardware-debug): add flash command builder + streaming runner"
```

---

### Task 3: Broker — FLASH verb + status fan-out

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py` (imports at top; `Broker._client_loop` ~line 414; add methods after `Broker._set_status` ~line 298)
- Test: `.claude/skills/px4-hardware-debug/tests/test_broker_flash.py`

**Interfaces:**
- Consumes: `flash_status.build_flash_cmd`, `flash_status.run_flash`, `Broker._broadcast`, `Broker._emit`, `Broker._log.log_status`.
- Produces on `Broker`:
  - `_set_flash_status(self, msg)` — `log_status("flash "+msg)`, `_broadcast("FLASH "+msg)`, `_emit("flash", msg)`.
  - `flash(self, target)` — if a flash is already running, `_set_flash_status("error: busy")` and return; else spawn a daemon thread running `run_flash(build_flash_cmd(target, REPO_ROOT), self._set_flash_status, self._on_flash_done, cwd=REPO_ROOT)`; emit `_set_flash_status("starting " + target)`.
  - `_on_flash_done(self, msg)` — clear the busy flag, `_set_flash_status(msg)`.
- Protocol: TCP clients may send `FLASH <target>`; broker replies with `FLASH <status>` broadcast lines.

- [ ] **Step 1: Write the failing test**

Create `tests/test_broker_flash.py`:

```python
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import console


def _make_broker(events):
    b = console.Broker.__new__(console.Broker)      # bypass serial __init__
    b._clients = set(); b._clients_lock = __import__("threading").Lock()
    b._running = True
    b._log = type("L", (), {"log_status": lambda self, m: None})()
    b.on_event = lambda kind, payload: events.append((kind, payload))
    b._flashing = False
    return b


def test_set_flash_status_emits_event():
    events = []
    b = _make_broker(events)
    b._set_flash_status("erasing 20%")
    assert ("flash", "erasing 20%") in events


def test_flash_runs_and_reports_done():
    events = []
    b = _make_broker(events)
    # override the command to a trivial fake so no real board is needed
    orig = console.build_flash_cmd
    console.build_flash_cmd = lambda target, root: [
        sys.executable, "-c", "print('Rebooting. Elapsed Time 1')"]
    try:
        b.flash("fake_target")
        for _ in range(50):
            if any(p == "done" for k, p in events if k == "flash"):
                break
            time.sleep(0.1)
    finally:
        console.build_flash_cmd = orig
    kinds = [p for k, p in events if k == "flash"]
    assert any(s.startswith("starting") for s in kinds)
    assert "rebooting" in kinds
    assert "done" in kinds


if __name__ == "__main__":
    test_set_flash_status_emits_event()
    test_flash_runs_and_reports_done()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_broker_flash.py`
Expected: FAIL — `AttributeError: 'Broker' object has no attribute '_set_flash_status'` (or `console` has no `build_flash_cmd`).

- [ ] **Step 3: Write minimal implementation**

In `console.py`, near the top imports add:

```python
from flash_status import parse_flash_line, build_flash_cmd, run_flash

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
```

In `class Broker`, immediately after `_set_status` (ends ~line 298) add:

```python
    def _set_flash_status(self, msg):
        self._log.log_status("flash " + msg)
        self._broadcast("FLASH " + msg)
        self._emit("flash", msg)

    def _on_flash_done(self, msg):
        self._flashing = False
        self._set_flash_status(msg)

    def flash(self, target):
        if getattr(self, "_flashing", False):
            self._set_flash_status("error: busy")
            return
        self._flashing = True
        self._set_flash_status("starting " + target)
        cmd = build_flash_cmd(target, _REPO_ROOT)
        t = threading.Thread(
            target=run_flash,
            args=(cmd, self._set_flash_status, self._on_flash_done),
            kwargs={"cwd": _REPO_ROOT},
            daemon=True)
        t.start()
```

In `_client_loop`, extend the command dispatch (after the `PING` branch, ~line 421):

```python
                elif line.startswith("FLASH "):
                    self.flash(line[6:].strip())
```

In `Broker.__init__`, add `self._flashing = False` next to the other instance attributes (near `self._board = None`, ~line 226).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_broker_flash.py`
Expected: prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py \
        .claude/skills/px4-hardware-debug/tests/test_broker_flash.py
git commit -m "feat(px4-hardware-debug): broker FLASH verb + status fan-out"
```

---

### Task 4: GUI — Flash header line

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py` (`ConsoleGUI.build` ~line 494; `_drain_events` ~line 574; add `_update_flash` near `_update_board` ~line 583)

**Interfaces:**
- Consumes: the `"flash"` event emitted by `Broker._emit` (Task 3).
- Produces: a `Flash:` label in the header, refreshed on every `"flash"` event. Amber while running, green on `done`, red on `error`.

*(No unit test — tkinter needs a display. Verified manually in Step 3 via `--headless` parity + a live window in Task 6's verification.)*

- [ ] **Step 1: Add the label in `build()`**

In `ConsoleGUI.build`, inside the `hdr` frame block (after the `_board_label` pack, ~line 502) add:

```python
        tk.Label(hdr, text="  Flash:", fg="#8a8a8a", bg="#141414").pack(side="left", padx=(12, 2))
        self._flash_label = tk.Label(hdr, text="idle", fg="#8a8a8a",
                                     bg="#141414", font=("monospace", 11),
                                     anchor="w")
        self._flash_label.pack(side="left")
```

- [ ] **Step 2: Handle the event in `_drain_events`**

In `_drain_events`, after the `elif kind == "board":` branch (~line 575) add:

```python
            elif kind == "flash":
                self._update_flash(payload)
```

- [ ] **Step 3: Add `_update_flash`**

After `_update_board` (~line 583) add:

```python
    def _update_flash(self, msg):
        if msg.startswith("error"):
            color = "#ff5f5f"
        elif msg == "done":
            color = "#5fd75f"
        else:
            color = "#ffd75f"
        self._flash_label.configure(text=msg, fg=color)
```

- [ ] **Step 4: Manual verification (no board needed)**

Run: `python3 .claude/skills/px4-hardware-debug/console.py --headless &` then
`python3 -c "import sys; sys.path.insert(0,'.claude/skills/px4-hardware-debug'); from console_client import Console; c=Console(); c._sock.sendall(b'FLASH nonexistent_target_xyz\n'); import time; time.sleep(3); print([l for l in [] ])"`
Expected: the session log shows `flash starting nonexistent_target_xyz` then `flash error: exit 2` (make fails fast on a bad target). Confirms the wire path without a board. (`grep -a FLASH`/`flash` in the newest `logs/session-*.log`.)

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py
git commit -m "feat(px4-hardware-debug): show live Flash: status in console header"
```

---

### Task 5: Client helper + CLI

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console_client.py` (`_read_loop` ~line 46; add methods after `read_until` ~line 82; `__main__` ~line 117)
- Test: `.claude/skills/px4-hardware-debug/tests/test_client_flash.py`

**Interfaces:**
- Produces on `Console`:
  - captures inbound `FLASH <msg>` lines into `self.flash_status` (list) as they arrive.
  - `flash(self, target)` — sends `FLASH <target>`.
  - `wait_flash_done(self, timeout=180)` — polls `self.flash_status` until the last entry is `"done"` or starts with `"error"`; returns that final string (or `None` on timeout).
- CLI: `console_client.py --flash <target>` triggers a flash and streams status to stdout.

- [ ] **Step 1: Write the failing test**

Create `tests/test_client_flash.py`:

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import console_client


def test_flash_lines_are_captured():
    c = console_client.Console.__new__(console_client.Console)  # no socket
    c.flash_status = []
    # simulate the reader loop classifying an inbound FLASH line
    console_client.Console._classify(c, "FLASH erasing 20%")
    console_client.Console._classify(c, "FLASH done")
    assert c.flash_status == ["erasing 20%", "done"]


if __name__ == "__main__":
    test_flash_lines_are_captured()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_client_flash.py`
Expected: FAIL — `AttributeError: type object 'Console' has no attribute '_classify'`.

- [ ] **Step 3: Write minimal implementation**

In `console_client.py`, add `self.flash_status = []` in `__init__` (next to `self.statuses = []`, ~line 29). Refactor the line classification in `_read_loop` into a `_classify` method so it is testable, and add a `FLASH ` case:

```python
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
```

and change `_read_loop`'s body to call `self._classify(line)` in place of the inline `if/elif` chain (lines 46–53).

Add after `read_until` (~line 82):

```python
    def flash(self, target):
        self.send("FLASH " + target)

    def wait_flash_done(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            if self.flash_status:
                last = self.flash_status[-1]
                if last == "done" or last.startswith("error"):
                    return last
            time.sleep(0.2)
        return None
```

Extend `__main__` (~line 117) to support `--flash`:

```python
if __name__ == "__main__":
    c = Console()
    if len(sys.argv) > 2 and sys.argv[1] == "--flash":
        c.flash(sys.argv[2])
        print("flashing", sys.argv[2], "...")
        seen = 0
        result = c.wait_flash_done()
        for s in c.flash_status:
            print("  ", s)
        print("result:", result)
    elif len(sys.argv) > 1:
        c.send(" ".join(sys.argv[1:]))
        print(c.read(timeout=2.0))
    c.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 .claude/skills/px4-hardware-debug/tests/test_client_flash.py`
Expected: prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console_client.py \
        .claude/skills/px4-hardware-debug/tests/test_client_flash.py
git commit -m "feat(px4-hardware-debug): client flash() + wait_flash_done + --flash CLI"
```

---

### Task 6: Docs + end-to-end on-board verification

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/SKILL.md` (§5 Build and flash)

**Interfaces:** none (documentation + real-hardware smoke test).

- [ ] **Step 1: Document the workflow**

In `SKILL.md` §5, after "Step 2 — Flash via USB bootloader", add a subsection:

```markdown
### Flash from the console (live status in the header)

If the shared console broker is running, you can flash **through it** — the
`Flash:` header line then shows `starting → found board → erasing X% →
programming X% → rebooting → done`, the whole flash is timestamped in the
session log, and the post-flash boot log lands in the same pane.

    from console_client import Console
    c = Console()
    c.flash("aerium_apex_h7_rev_a_default")
    print(c.wait_flash_done())   # "done" or "error: ..."

Or from the CLI: `python3 console_client.py --flash aerium_apex_h7_rev_a_default`.

> **WSL:** the board still re-enumerates into the bootloader (different VID:PID),
> so the `usbipd ... --auto-attach` loop from §5 Step 2 must be running first.
> (Auto-managing it from the console is a future add — see FLASH_STATUS_PLAN.md
> Task 7.)
```

- [ ] **Step 2: Run the full unit suite**

Run: `for t in .claude/skills/px4-hardware-debug/tests/test_*.py; do python3 "$t" || exit 1; done`
Expected: each prints `OK`.

- [ ] **Step 3: On-board end-to-end (needs the board + a real build)**

With a built target and the broker running (and the WSL auto-attach loop up):
`python3 .claude/skills/px4-hardware-debug/console_client.py --flash aerium_apex_h7_rev_a_default`
Expected: status lines progress through `found board … → programming …% → rebooting → done`; the `Flash:` header (headed mode) tracks them; the newest `logs/session-*.log` contains matching `flash …` lines followed by the fresh boot banner. If it stalls at `waiting for bootloader`, tap RESET (same as manual flashing).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/px4-hardware-debug/SKILL.md
git commit -m "docs(px4-hardware-debug): document flash-from-console workflow"
```

---

### Task 7 (optional / v2): WSL auto-attach wrapper

**Files:**
- Modify: `.claude/skills/px4-hardware-debug/console.py` (arg parsing in `main`; `Broker.flash`)

**Interfaces:**
- Adds a `--wsl-busid <BUSID>` CLI option. When set, `Broker.flash` starts
  `powershell.exe -Command "usbipd attach --wsl --busid <BUSID> --auto-attach"`
  as a subprocess before `run_flash` and terminates it after `_on_flash_done`,
  removing the "loop must already be running" caveat on WSL.

- [ ] **Step 1: Add the option and lifecycle**

Store `self._wsl_busid` on the broker from the parsed arg (default `None`). In `flash`, before starting `run_flash`, if `self._wsl_busid`:

```python
        self._wsl_proc = subprocess.Popen(
            ["powershell.exe", "-Command",
             "usbipd attach --wsl --busid %s --auto-attach" % self._wsl_busid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
```

and in `_on_flash_done`, if `getattr(self, "_wsl_proc", None)`: `self._wsl_proc.terminate()` then clear it.

- [ ] **Step 2: Manual verification on WSL**

Run the flash from the console with `--wsl-busid 2-7` and no separate auto-attach loop; confirm the flash still lands (the board re-attaches on re-enumeration automatically).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/px4-hardware-debug/console.py
git commit -m "feat(px4-hardware-debug): optional --wsl-busid auto-attach around flash"
```

---

## Self-Review Notes

- **Coverage:** header line (T4), flash from the tool (T3/T5), progress parsing incl. buffered `\r` (T1/T2 + `PYTHONUNBUFFERED`), headless parity (status via log/TCP, GUI label GUI-gated), WSL caveat (T6 doc, T7 optional fix). All requested behavior mapped.
- **Type consistency:** `parse_flash_line`/`build_flash_cmd`/`run_flash` signatures match between `flash_status.py`, the broker (T3), and tests. Event kind is `"flash"` everywhere; wire prefix is `FLASH ` everywhere.
- **Risk:** the only nontrivial risk is uploader output buffering — mitigated by `PYTHONUNBUFFERED=1` + splitting on `\r`; T2's fake-stream test exercises the `\r` path without a board.
- **Scope split:** Tasks 1–6 are the MVP (self-contained, testable, useful). Task 7 is an independent WSL convenience that can ship later.

# PX4 Hardware Debug — Shared Console Window

**Date:** 2026-06-08
**Skill:** `px4-hardware-debug`
**Status:** Approved design, pending implementation plan

## Goal

Extend the `px4-hardware-debug` skill so the NSH serial console can be driven by
both the **user** and the **skill (agent)** through a single shared connection,
with everything logged to timestamped files for later review. A GUI window shows
the console in real time and lets the user type alongside the agent.

The feature has two modes, chosen at the start of every debugging session:

- **Headed** — a tkinter window opens; user and skill share the live console.
- **Headless** — no window; the skill drives the console and everything is logged.

The skill's programmatic interface and the logging are **byte-for-byte identical**
in both modes. The only difference is whether a window is shown.

## Scope

In scope:
- NSH text console over the UART adapter (`/dev/ttyUSB0` @ 57600) **only**.
  "Device data" (e.g. `listener`, `uorb top` output) is text through this same
  console.
- Single owner of the serial port (a broker) with a localhost TCP interface.
- tkinter GUI (headed mode).
- Timestamped session logs in the skill folder, git-ignored.
- Auto-reconnect across board reboot/flash.
- Color-coding USER vs SKILL input in the GUI and transcript.
- Input history (up/down) + clear-screen in the GUI.

Out of scope (YAGNI for v1):
- MAVLink binary channel (`/dev/ttyACM0`) — the transport is kept simple enough
  that a second channel could be added later, but it is not built now.
- Annotate/bookmark button.
- Remote (non-localhost) access; multi-machine GUI.

## Architecture (Approach A: single port-owner with optional GUI)

One script, `console.py`, becomes the **single owner** of the serial port and
runs a localhost TCP server for the skill. Headed mode additionally opens a
tkinter window in the same process. The skill always talks over TCP, so its code
path is identical headed vs headless. The GUI reads the shared buffer in-process
(it is not itself a TCP client).

```
                         console.py  (single owner of /dev/ttyUSB0)
   ┌───────────────────────────────────────────────────────────────┐
   │  serial reader thread                                          │
   │     reads ttyUSB0 → ring buffer → log file → GUI pane (headed) │
   │     → fans out to all TCP clients                              │
   │                                                                │
   │  TCP server thread (127.0.0.1:<tcp-port>)                      │
   │     each client: live tail of output; may SEND input          │
   │                                                                │
   │  main thread                                                   │
   │     headed   → tkinter event loop                              │
   │     headless → block until SIGTERM/Ctrl-C                      │
   └───────────────────────────────────────────────────────────────┘
            ▲ TCP                         ▲ in-process
            │                             │
   console_client.py                tkinter GUI
   (the skill: send/read)           (the user: type/read)
```

### Shared state and write path
- Shared state = one ring buffer + the serial handle, guarded by a lock.
- Writes to the serial port come from two sources (GUI input box, TCP clients)
  and are funneled through a single `write_serial()` guarded by a lock so they
  never interleave mid-line. Each write is tagged with its source (USER / SKILL).

### Threading notes
- tkinter must run on the main thread; serial reader and TCP server run on
  background threads.
- Background-thread exceptions are caught and surfaced as `STAT error: …` lines
  to the pane/log instead of killing the process.

## TCP protocol

Localhost only, line-framed, deliberately minimal.

- **On connect**, the client immediately begins receiving the live output stream
  from connect-time onward (no history replay — fire-and-forget fits a live tail).
- **Client → broker** (newline-framed control lines):
  - `SEND <text>` → broker writes `<text>\r` to the serial port, tagged SKILL.
  - `PING` → broker replies `PONG` (used to detect an already-running broker).
- **Broker → client** (newline-framed, one event per line):
  - `OUT <line>` — a single line of console output. Output is line-buffered the
    same way as the log (a partial line at a read boundary is held until the next
    newline or a short idle), so `OUT` payloads never contain embedded newlines
    and the framing is unambiguous.
  - `STATUS connected <port> <baud>` / `STATUS disconnected` — link-state changes.

Control-prefixing (rather than raw pass-through) lets the skill distinguish
console output from status events (e.g. serial dropped during flash) without
guessing, and the shared line-buffering keeps `OUT`, the log, and the GUI pane
consistent.

## Skill-side helper (`console_client.py`)

Importable/runnable helper used by the skill in both modes:

```python
c = Console()                              # connects to the running broker
c.send("ver all")                          # fire-and-forget
out = c.read(timeout=2.0)                   # drain output seen so far → text
out = c.read_until("nsh>", timeout=5.0)     # convenience for quick commands
# streaming (e.g. top): loop c.read() until the skill decides to stop, then:
c.send("\x03")                              # ^C to stop a rolling command
```

`Console()` **only connects**; it does not auto-start a broker. If no broker is
running it raises a clear error ("no console broker — start it with
`console.py [--headless]`"). This keeps the headed/headless choice honest: the
mode is decided by how the broker was launched, not silently by the client.

## GUI layout (headed mode)

Plain dark terminal-style window:

```
┌─ PX4 Console — ttyUSB0 @ 57600 ──────────── ● connected ─┐
│  [scrolling output pane — monospace, dark bg]            │
│  nsh> ver all                          (USER input: grn) │
│  HW arch: AERIUM_RADIAN_H7_REV_B                         │
│  nsh> sensors status                   (SKILL input: cyan)│
│  ...                                                     │
├──────────────────────────────────────────────────────────┤
│ > [input box.......................]  [Clear]            │
└──────────────────────────────────────────────────────────┘
```

- **Output pane** — read-only, autoscrolls; autoscroll pauses while the user
  scrolls up. USER input green, SKILL input cyan, device output grey/default,
  STATUS lines yellow.
- **Input box** — Enter sends to serial (tagged USER). Up/Down arrows recall
  input history.
- **Clear button** — wipes the on-screen pane only; the log file is untouched.
- **Status indicator** — top-right dot + text, driven by the STATUS stream.

## Auto-reconnect

The serial reader detects port loss (read error / device gone), emits
`STATUS disconnected`, then retries opening `/dev/ttyUSB0` every ~1 s until it is
back, then emits `STATUS connected`. This survives board reboots and flashing
without restarting the window.

Note: flashing uploads over `/dev/ttyACM0` (a different device), so the broker on
`/dev/ttyUSB0` simply rides through the reboot drop and reconnects.

## Logging & file layout

```
.claude/skills/px4-hardware-debug/
├── SKILL.md            # updated: headed/headless workflow + driving the console
├── console.py          # broker + optional GUI (port owner)
├── console_client.py   # skill-side helper: Console().send / read
├── .gitignore          # contains: logs/
└── logs/               # git-ignored
    └── session-YYYYMMDD-HHMMSS.log
```

- One log file per broker session, named by start timestamp; a new run = a new
  file (no clobbering, naturally ordered for review).
- Transcript format: one event per line, ISO-ish timestamp + source tag:

  ```
  2026-06-08T14:03:21.412  USER  > ver all
  2026-06-08T14:03:21.840  OUT   HW arch: AERIUM_RADIAN_H7_REV_B
  2026-06-08T14:03:25.110  SKILL > sensors status
  2026-06-08T14:03:25.900  OUT   selected gyro: 2490386 (1)
  2026-06-08T14:03:30.001  STAT  disconnected — reconnecting
  2026-06-08T14:03:34.220  STAT  connected ttyUSB0@57600
  ```

  Output lines are logged as they arrive (line-buffered; a partial line at a read
  boundary is flushed on the next newline or after a short idle).
- The skill folder `.gitignore` contains just `logs/`, so the script and docs are
  committed but session logs never are.

## Lifecycle & mode selection

- Mode selection becomes question 5 in SKILL.md §0 ("Session setup"):
  *"Headed or headless console?"*
- Headed: skill launches `python3 console.py` (background; window appears).
  Headless: `python3 console.py --headless`.
- Defaults baked in (`--port /dev/ttyUSB0 --baud 57600 --tcp-port 8765`),
  overridable by flag. `8765` is chosen to avoid PX4's known ports (4560
  simulator, 5760 MAVLink-TCP, 14540/14550 UDP).
- On startup the broker `PING`s its TCP port; if a broker already answers, it
  refuses to start a second port-owner and exits with a clear message.
- The skill drives the console via `console_client.py` over TCP. At session end
  the skill terminates the broker process, which closes the serial port and
  flushes the log.

## Error handling

- **Serial port busy** (stray `picocom` / old broker) → broker reports where to
  look and exits non-zero; skill surfaces it.
- **Port missing at startup** (e.g. WSL not attached) → broker starts in
  `disconnected` state and auto-connects when the device appears (ties into the
  existing WSL `usbipd` auto-attach guidance in the skill).
- **Broker not running when skill connects** → `console_client` raises a clear
  error rather than silently opening the port directly.
- **Background-thread errors** → caught and pushed to pane/log as `STAT error: …`
  instead of killing the window.

## Testing strategy

- **Unit (no hardware):** point the broker at a PTY pair (pseudo-terminal) acting
  as a fake device. Verify: serial→clients fan-out, `SEND` write path with source
  tagging, `PING`/`PONG`, log file format/line-flush behavior, the
  already-running-broker refusal, and `console_client` send/read/read_until.
- **Auto-reconnect (no hardware):** close the fake PTY mid-session, assert
  `STATUS disconnected` then `connected` after the PTY is recreated.
- **GUI smoke (no hardware):** instantiate the tkinter app against the fake PTY,
  drive a couple of inputs, assert pane/coloring updates (or skip cleanly when no
  display is available).
- **Manual (hardware):** run headed against the real board, confirm shared typing,
  run a flash and confirm ride-through reconnect, confirm log contents.

## Documentation updates

- SKILL.md §0: add the headed/headless question.
- SKILL.md §2: document driving the shared console via `console_client.py` as the
  preferred non-interactive path (superseding the standalone pyserial helper when
  the broker is in use), and how to launch the broker in each mode.

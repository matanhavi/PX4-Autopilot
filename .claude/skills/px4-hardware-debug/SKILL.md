---
name: px4-hardware-debug
description: >
  Guide for debugging PX4 firmware on real hardware. Use this skill whenever the
  user needs to access the NSH shell, check if a serial port is alive, diagnose
  unexpected sensor or driver behavior, check MAVLink status, read or reset
  parameters, investigate boot issues, or troubleshoot any hardware problem on a
  PX4 flight controller. Always consider hardware as a potential factor.
---

# PX4 Hardware Debug

Assume the board is already flashed. Focus on health checks and diagnosing what's wrong, not on setting things up.

---

## 0. Session setup — ask before doing anything

**Before touching any tools, ask the user these questions in a single message:**

1. **Which board and revision are you working with?** (e.g. `aerium_radian_h7` rev_b). This determines the correct build target and which board_id to expect from the bootloader.
2. **Which connections are available?**
   - USB (board shows up as `/dev/ttyACM*`)
   - UART serial adapter (FTDI/CP210x, shows up as `/dev/ttyUSB*`)
   - Both
3. **Is an SD card inserted?** (Affects dataman, logging, and mission storage diagnostics.)
4. **Is this a new board bring-up or a regression on a known-working board?** (Affects which errors are expected vs. surprising.)
5. **Headed or headless console?** Headed opens a live terminal window
   (`console.py`) that both you and the skill share in real time; headless runs
   the same broker without a window. Either way every byte is logged to
   `logs/session-*.log`. Default to **headed** unless the user has no display.

Use the answers to guide the session — don't probe for connections the user said aren't present, and don't flag missing-SD errors as critical if the user said there's no SD card.

If a device that the user said is connected does **not** appear in `/dev/`, ask immediately: "Is the cable plugged in / USB attached?" before proceeding.

---

## 1. Check physical connections

```bash
ls -l /dev/serial/by-id/
```

Expected devices:
- `usb-..._<board_name>_...-if00` → board USB (MAVLink / bootloader)
- `usb-FTDI_...` or `usb-Silicon_Labs_...` → serial adapter for UART console

If a device the user expects is missing, stop and ask them to check the cable/USB before continuing.

On WSL, USB devices must be attached from Windows first. List them and find the board's busid:
```powershell
usbipd list   # run via: powershell.exe -Command "usbipd list"
```
Devices show `Shared` (available) or `Attached` (already on WSL). Attach the board and the
FTDI adapter by busid:
```powershell
usbipd attach --wsl --busid 2-1   # board USB
usbipd attach --wsl --busid 2-7   # FTDI UART adapter
```
After attaching, the device appears under `/dev/serial/by-id/` and `/dev/ttyACM*` / `/dev/ttyUSB*`.

> **For flashing on WSL, a plain attach is not enough — see §5 Step 2.** The board re-enumerates
> with a different VID:PID when it drops into the bootloader, and usbipd loses it mid-flash.

---

## 2. NSH shell access

### Shared console (preferred for agent + user together)

`console.py` owns `/dev/ttyUSB0` and exposes a localhost TCP interface so the
user (GUI window) and the skill can drive the same console at once, with full
logging and auto-reconnect across reboot/flash.

Start it once at the beginning of the session (pick the mode from §0 Q5):

```bash
# headed (opens the window; needs python3-tk + a display):
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
`.claude/skills/px4-hardware-debug/logs/` (git-ignored). The headed GUI needs
`python3-tk` installed (`sudo apt install python3-tk`); without it, use
`--headless`.

### Manual UART console (single user)

Connect a serial adapter to the board's designated debug UART (TX→RX, RX→TX, GND→GND) at 57600 baud. Open with any terminal:

```bash
picocom -b 57600 /dev/ttyUSB0
# or
minicom -b 57600 -D /dev/ttyUSB0
```

You'll see the full boot log and an `nsh>` prompt. Press Enter if the prompt doesn't appear — NSH is waiting for input.

`picocom`/`minicom` are **interactive** and can't be driven non-interactively. To run commands
and capture output programmatically (e.g. from an agent), use a pyserial helper:

```python
import serial, time
ser = serial.Serial('/dev/ttyUSB0', 57600, timeout=0.4)

def run(cmd, wait=2.0):
    ser.reset_input_buffer()
    ser.write((cmd + '\r\n').encode())
    out, t = b'', time.time()
    while time.time() - t < wait:
        n = ser.in_waiting
        if n: out += ser.read(n); t = time.time()
        else: time.sleep(0.1)
    return out.decode('utf-8', errors='replace')

ser.write(b'\r\n'); time.sleep(0.4); ser.read(ser.in_waiting or 1)  # wake prompt
print(run('ver all'))
print(run('dmesg', 2.5))
```

Output may contain `nsh> [K` prompt-redraw noise and, right after boot, stray null bytes
(`^@`) — strip/ignore them. `dmesg` replays the full boot log including driver init lines.

Reading raw bytes to check what's on the port:
```bash
stty -F /dev/ttyUSB0 57600 raw -echo
dd if=/dev/ttyUSB0 bs=1 count=50 2>/dev/null | xxd
```
- `nsh>` text → console working
- MAVLink binary (`fe`/`fd` bytes) → MAVLink is on this port, not the console
- Silence → wrong connector, wrong baud, or port not configured

### Fallback: MAVLink shell (USB only)

Use when no serial adapter is available. Gives text shell access but no boot log.

```python
from pymavlink import mavutil
import time

m = mavutil.mavlink_connection('/dev/ttyACM0', baud=57600, dialect='ardupilotmega')
m.wait_heartbeat()

def shell(cmd, wait=1.5):
    data = (cmd + '\n').encode()
    m.mav.serial_control_send(
        mavutil.mavlink.SERIAL_CONTROL_DEV_SHELL,
        mavutil.mavlink.SERIAL_CONTROL_FLAG_EXCLUSIVE |
        mavutil.mavlink.SERIAL_CONTROL_FLAG_RESPOND,
        0, 0, len(data), bytearray(data) + bytearray(70 - len(data)))
    time.sleep(wait)
    out = b''
    while True:
        r = m.recv_match(type='SERIAL_CONTROL', blocking=True, timeout=0.5)
        if not r:
            break
        out += bytes(r.data[:r.count])
    return out.decode('utf-8', errors='replace')

print(shell('ver hw'))
```

> Note: MAVLink shell stops working if the board has a physical UART set as NuttX
> console. In that case, use the UART console above.

---

## 3. Health check commands

### Verify firmware version first

Before diagnosing anything, confirm the firmware on the board matches what the user expects:

```bash
ver all
```

This prints board type, git hash, build date, and branch. Cross-check:
- **HW arch** matches the expected board revision (e.g. `AERIUM_RADIAN_H7_REV_B`, not `REV_A`)
- **git-hash** matches the commit the user just built/flashed
- **Build datetime** is recent — if it's stale, the board may still be running old firmware

If anything looks wrong, flash the correct firmware before continuing (see section 5).

### Overall picture

```bash
uorb top                  # live message rates — spot silent drivers
top                       # CPU load per task
sensors status            # sensor pipeline state
commander status          # arming state, preflight checks
mavlink status            # active MAVLink links, rates, errors
```

Sensor-specific:
```bash
listener sensor_combined  # IMU data live
listener battery_status   # battery voltage/current
bmi270 status             # replace with your IMU driver name
gps status                # GPS fix, satellites
```

Parameters:
```bash
param show SYS_AUTOSTART  # airframe selection
param show MAV_0_CONFIG   # which port MAVLink instance 0 is on
param show MAV_1_CONFIG   # which port MAVLink instance 1 is on
param reset_all && param save  # factory reset if params are suspect
```

---

## 4. MAVLink keeps starting on a port unexpectedly

Saved flash parameters override `px4board` defaults. Check:

```python
for param in ['MAV_0_CONFIG', 'MAV_1_CONFIG', 'MAV_2_CONFIG']:
    m.mav.param_request_read_send(m.target_system, m.target_component, param.encode(), -1)
    p = m.recv_match(type='PARAM_VALUE', blocking=True, timeout=3)
    print(f'{param}: {p.param_value if p else "N/A"}')
```

Fix via shell: `param reset_all` + `param save`.

---

## 5. Build and flash

### Determine the board target

The board target is `<manufacturer>_<board>_<variant>`. Match it exactly to the board revision — e.g. `AERIUM_RADIAN_H7_REV_B` → `aerium_radian_h7_rev_b_default`, not `aerium_radian_h7_default`.

Find all available targets:
```bash
find boards/ -name "*.px4board" | sed 's|boards/||;s|/[^/]*\.px4board||' | sort
```

### Step 1 — Build only (verify it compiles before touching the board)

```bash
make <board>_default
# example: make aerium_radian_h7_rev_b_default
```

Wait for a successful build before proceeding. The first build for a new target takes 3–5 minutes (compiling ~1100 files). Subsequent incremental builds take ~10 seconds.

### Step 2 — Flash via USB bootloader

**On WSL — start an auto-attach loop FIRST (required).** When the board reboots from app
mode into the bootloader it re-enumerates with a *different* VID:PID on the same busid
(e.g. app `1b8c:0036` → bootloader `3162:0709`). A one-shot `usbipd attach` is dropped at
that moment, and the ~5 s bootloader window closes before you can manually re-attach — so the
flash never lands. Run the auto-attach loop in the background before uploading; it re-grabs
the device the instant it re-enumerates:

```bash
# background, stays running across the whole flash:
powershell.exe -Command "usbipd attach --wsl --busid 2-1 --auto-attach" &
```

Then upload:

```bash
make <board>_default upload
# example: make aerium_radian_h7_rev_b_default upload
```

> **Don't pipe `make upload` through `tail`/`head`** — the uploader's progress bars are
> buffered and you'll see nothing until it exits. Run it in the background and read the raw
> output file, or rely on the exit code (0 = success).

**If it stalls at "Waiting for bootloader...":**

The uploader is waiting for the board to enter bootloader mode. Try in order:
1. The uploader auto-triggers reboot via MAVLink — wait up to 10 s
2. On WSL: confirm the auto-attach loop is running and that busid re-attaches when the board reboots (`ls /dev/serial/by-id/` should show the board name reappear)
3. If still waiting: press the board's RESET button once (the uploader retries on reconnect)
4. If still waiting: hold BOOT button, press RESET, release BOOT — this forces bootloader mode on power-up

After a successful flash the board reboots automatically. Wait ~5 s, then reconnect.
Kill the background auto-attach loop once you're done if you don't want it persisting.

### Step 3 — Verify

Run `dmesg` via the shell and confirm the previously failing drivers now appear without errors.

### Board ID mismatch

Upload fails with "Firmware not suitable for this board" when `board_id` in `firmware.prototype` ≠ the board_id reported by the bootloader (`Found board id: XXXX`). Fix by matching the number in `firmware.prototype` and `BOARD_TYPE` in `src/hw_config.h` to what the bootloader reports, then rebuild.

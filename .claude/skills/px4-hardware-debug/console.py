#!/usr/bin/env python3
"""PX4 shared NSH console: single serial-port owner with a localhost TCP
interface and an optional tkinter GUI. See the px4-hardware-debug SKILL.md."""

import argparse
import glob
import json
import os
import queue
import re
import signal
import socket
import sys
import threading
from datetime import datetime

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - serial only needed at runtime
    serial = None

from flash_status import parse_flash_line, build_flash_cmd, run_flash, valid_target

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
DEFAULT_TCP_PORT = 8765
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

# ANSI escape sequences: CSI (\x1b[ ... final) plus two-character escapes.
# Screen-redraw commands like `top`/`uorb top` emit these (clear-screen,
# cursor-home, erase-line, colors); the tkinter Text widget would otherwise
# render them as literal `[K`/`[2J`.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text):
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


# PX4 'ver all' prints "HW arch: <BOARD>" -- used to identify which board is on
# this console, so a swapped adapter / multi-board setup can't be confused.
_HWARCH_RE = re.compile(r"HW arch:\s*(\S+)")


def usb_fields(port):
    """Best-effort structured USB identity for a serial device.

    Returns a dict with keys 'path', 'bus', 'by_path', 'name', 'ids' (vid:pid)
    and 'serial'; each missing field is ''. The physical bus path ('1-2') and
    by-path ('0:2') are what actually tell two adapters apart -- the ttyUSBx
    number can swap on replug, and cheap FTDIs often share one serial. Never
    raises; falls back to just the resolved device path on a non-USB port.
    """
    real = os.path.realpath(port)
    f = {"path": real, "bus": "", "by_path": "", "name": "",
         "ids": "", "serial": ""}
    try:
        sysdev = "/sys/class/tty/%s/device" % os.path.basename(real)
        node = os.path.realpath(sysdev)
        while (node and node != "/"
               and not os.path.exists(os.path.join(node, "idVendor"))):
            node = os.path.dirname(node)

        def _read(attr):
            try:
                with open(os.path.join(node, attr)) as fh:
                    return fh.read().strip()
            except OSError:
                return None

        if node and node != "/":
            f["bus"] = os.path.basename(node)                  # e.g. 1-2
        bp_dir = "/dev/serial/by-path"
        if os.path.isdir(bp_dir):
            for link in os.listdir(bp_dir):
                if os.path.realpath(os.path.join(bp_dir, link)) == real:
                    m = re.search(r"usb-(\d+:\d+)", link)      # ...-usb-0:2:1.0-port0
                    f["by_path"] = m.group(1) if m else link
                    break
        f["name"] = " ".join(x for x in (_read("manufacturer"),
                                         _read("product")) if x)
        vid, pid = _read("idVendor"), _read("idProduct")
        if vid and pid:
            f["ids"] = "%s:%s" % (vid, pid)
        f["serial"] = _read("serial") or ""
    except Exception:
        pass
    return f


def usb_tech_line(f):
    """The technical identifiers from usb_fields() as one string, excluding the
    human name: path · bus · by-path · vid:pid · SN. Used for the header table."""
    parts = [f["path"]]
    if f["bus"]:
        parts.append("bus " + f["bus"])
    if f["by_path"]:
        parts.append("by-path " + f["by_path"])
    if f["ids"]:
        parts.append(f["ids"])
    if f["serial"]:
        parts.append("SN " + f["serial"])
    return "  ·  ".join(parts)


def usb_info(port):
    """One-line USB identity (path · bus · by-path · name · ids · SN) for the
    session log. See usb_fields() for the structured form used by the header."""
    f = usb_fields(port)
    parts = [f["path"]]
    if f["bus"]:
        parts.append("bus " + f["bus"])
    if f["by_path"]:
        parts.append("by-path " + f["by_path"])
    if f["name"]:
        parts.append(f["name"])
    if f["ids"]:
        parts.append(f["ids"])
    if f["serial"]:
        parts.append("SN " + f["serial"])
    return "  ·  ".join(parts)


def board_usb_port():
    """The board's own USB CDC/ACM tty (/dev/ttyACM*), or None if not attached.

    The FTDI console adapter enumerates as ttyUSB*, so the board's direct USB
    (MAVLink / bootloader) is the ACM device. Used for the GUI board-USB row.
    """
    acms = sorted(glob.glob("/dev/ttyACM*"))
    return acms[0] if acms else None


def endpoint_info(path):
    """(name, tech) for an endpoint device currently present, else None ('n/a').

    Accepts a stable /dev/serial/by-id/... path (or bare tty); resolves it and
    returns None when the device isn't attached, so the header can show 'n/a'.
    """
    if not path:
        return None
    if not os.path.exists(os.path.realpath(path)):
        return None
    f = usb_fields(path)
    return (f["name"], usb_tech_line(f))


# External, user-editable keyword highlighting config (JSON: color -> [keywords]).
COLOR_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "console_colors.json")


def load_keyword_colors(path):
    """Read a JSON {color: [keywords]} file and return a flat list of
    (keyword, color) rules. Returns [] if the file is missing or malformed."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rules = []
    for color, keywords in data.items():
        for kw in keywords:
            rules.append((kw, color))
    return rules


def find_keyword_spans(text, rules):
    """For each (keyword, color) rule, find whole-word, case-insensitive matches
    in text. Return a list of (start, end, color) character spans."""
    spans = []
    for kw, color in rules:
        if not kw:
            continue
        for m in re.finditer(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
            spans.append((m.start(), m.end(), color))
    return spans


class LineBuffer:
    """Accumulates text and yields complete lines.

    A line ends on LF ('\\n'), CR ('\\r'), or CRLF ('\\r\\n') -- all three are
    treated as one line break. Handling a bare CR matters for two cases the
    old LF-only logic silently swallowed: devices that terminate with CR only,
    and a TX<->RX loopback, which echoes back only the CR the broker writes
    (see Broker.send) and so would otherwise never surface. CRLF is collapsed
    to a single break -- including when the CR and LF land in separate reads --
    so normal NuttX output ('\\r\\n') does not yield spurious blank lines.
    """

    def __init__(self):
        self._buf = ""
        self._swallow_lf = False   # true right after a CR, to absorb its LF

    def feed(self, text):
        lines = []
        for ch in text:
            if ch == "\r":
                lines.append(self._buf)
                self._buf = ""
                self._swallow_lf = True
            elif ch == "\n":
                if self._swallow_lf:
                    self._swallow_lf = False   # CRLF: LF already closed the line
                else:
                    lines.append(self._buf)
                    self._buf = ""
            else:
                self._swallow_lf = False
                self._buf += ch
        return lines

    def flush(self):
        if self._buf == "":
            return None
        pending = self._buf
        self._buf = ""
        return pending


class SessionLog:
    """Writes a timestamped transcript: one event per line, source-tagged."""

    def __init__(self, log_dir, now=datetime.now, label=None):
        self._now = now
        os.makedirs(log_dir, exist_ok=True)
        stamp = now().strftime("%Y%m%d-%H%M%S")
        # Label (e.g. the broker's TCP port) keeps two consoles started in the
        # same second from colliding on one interleaved log file.
        suffix = ("-%s" % label) if label else ""
        self.path = os.path.join(log_dir, "session-%s%s.log" % (stamp, suffix))
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")
        self._lock = threading.Lock()

    def _write(self, tag, rest):
        ts = self._now().isoformat(timespec="milliseconds")
        with self._lock:
            # A straggler broker thread may try to log after close(); the lock
            # makes this check race-free against close().
            if self._fh.closed:
                return
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


def open_serial(port, baud):
    if serial is None:
        raise RuntimeError("pyserial is not installed")
    return serial.Serial(port, baud, timeout=0.2)


class Broker:
    """Owns the serial port; fans output out to TCP clients, the log, and a
    GUI callback; accepts input from TCP clients (and the GUI via send())."""

    def __init__(self, port, baud, tcp_port, session_log,
                 serial_factory=open_serial, on_event=None,
                 owns_board_usb=False):
        self._port = port
        self._baud = baud
        self._log = session_log
        self._serial_factory = serial_factory
        self.on_event = on_event
        # True when this console owns the board's own USB VCP (MAVLink transport).
        # A flash (make upload) needs that same port, so we release it during the
        # flash and let the reconnect loop re-acquire it afterwards.
        self._owns_board_usb = owns_board_usb
        self._flash_release = False

        self._serial = None
        self._serial_lock = threading.Lock()
        self._linebuf = LineBuffer()
        self._board = None          # detected 'HW arch' board name, or None
        self._flashing = False      # True while a FLASH subprocess is running

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

    # ---- flash --------------------------------------------------------
    def _set_flash_status(self, msg):
        self._log.log_status("flash " + msg)
        self._broadcast("FLASH " + msg)
        self._emit("flash", msg)

    def _on_flash_done(self, msg):
        self._flashing = False
        self._flash_release = False      # resume reconnect (re-acquire the VCP)
        self._set_flash_status(msg)

    def flash(self, target):
        if getattr(self, "_flashing", False):
            self._set_flash_status("error: busy")
            return
        target = target.strip()
        if not valid_target(target):
            # Reject flag smuggling / metacharacters before they reach make.
            self._set_flash_status("error: invalid target")
            return
        self._flashing = True
        if getattr(self, "_owns_board_usb", False):
            # Release our USB VCP so make upload can drive the bootloader.
            self._flash_release = True
        self._set_flash_status("starting " + target)
        cmd = build_flash_cmd(target, _REPO_ROOT)
        t = threading.Thread(
            target=run_flash,
            args=(cmd, self._set_flash_status, self._on_flash_done),
            kwargs={"cwd": _REPO_ROOT},
            daemon=True)
        t.start()

    # ---- serial side --------------------------------------------------
    def _serial_loop(self):
        while self._running:
            if getattr(self, "_flash_release", False):
                # A flash needs our port (MAVLink console). Release and hold until
                # the flash finishes; the board also re-enumerates during upload.
                with self._serial_lock:
                    if self._serial is not None:
                        try:
                            self._serial.close()
                        except OSError:
                            pass
                        self._serial = None
                self._announce_disconnected()
                threading.Event().wait(0.3)
                continue
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
                    self._detect_board(line)
            else:
                threading.Event().wait(0.02)

    # ---- board identity ----------------------------------------------
    def _detect_board(self, line):
        """Update the detected board name from any 'HW arch:' line that flows
        past (boot banner or a 'ver all' reply)."""
        m = _HWARCH_RE.search(line)
        if m and m.group(1) != self._board:
            self._board = m.group(1)
            self._log.log_status("board " + self._board)
            self._broadcast("BOARD " + self._board)
            self._emit("board", self._board)

    def probe_board(self):
        """Ask the device to identify itself. The 'HW arch:' line in the reply
        is picked up by _detect_board and updates the board header. No-op if the
        port is not currently open."""
        self.send("ver all", "AUTO")

    def _announce_disconnected(self):
        if not self._disc_announced:
            self._disc_announced = True
            self._connected = False
            # Forget the board so a reconnect (possibly a different adapter/board)
            # re-identifies instead of showing a stale name.
            self._board = None
            self._broadcast("BOARD ?")
            self._emit("board", None)
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
        # Identify the board shortly after connecting (delay lets a booting
        # board reach its prompt; a fresh boot banner also self-identifies).
        threading.Timer(1.5, self.probe_board).start()

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
                # Tell the new client the board we've already identified, so it
                # doesn't have to wait for the next change to broadcast.
                if self._board:
                    conn.sendall(("BOARD " + self._board + "\n").encode())
            except OSError:
                return
        f = conn.makefile("r")
        try:
            while self._running:
                try:
                    line = f.readline()
                except OSError:
                    # Client closed/reset the connection abruptly.
                    break
                if not line:
                    break
                line = line.rstrip("\n")
                if line.startswith("SEND "):
                    self.send(line[5:], "SKILL")
                elif line.startswith("FLASH "):
                    self.flash(line[6:].strip())
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
        "AUTO": "#6a6a6a",    # dim - console's own auto-probe (ver all)
    }

    def __init__(self, broker, log_path, color_rules=None,
                 endpoints=None, owned="serial", board_name=None):
        self._broker = broker
        self._log_path = log_path
        self._rules = (color_rules if color_rules is not None
                       else load_keyword_colors(COLOR_CONFIG))
        # Three fixed header lines. The 'owned' role is the transport this broker
        # actually drives (and whose connection status we show); the others are
        # informational. A None endpoint renders as 'n/a'.
        self._endpoints = endpoints or {"serial": broker._port,
                                        "usb": None, "jtag": None}
        self._owned = owned
        self._board_name = board_name
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
        self._root.title("PX4 Console - %s" % (self._board_name or "(detecting…)"))
        self._root.configure(bg="#1c1c1c")

        # Board identity ON TOP: the HW arch reported by 'ver all' (or --name), so
        # you always know WHICH board this console is. Refresh re-runs the probe.
        hdr = tk.Frame(self._root, bg="#141414")
        hdr.pack(fill="x")
        tk.Button(hdr, text="↻ Refresh",
                  command=self._refresh_board).pack(side="right", padx=4, pady=2)
        tk.Label(hdr, text="Board:", fg="#8a8a8a", bg="#141414").pack(side="left", padx=(6, 2))
        self._board_label = tk.Label(
            hdr, text=self._board_name or "(detecting…)",
            fg="#5fd7ff" if self._board_name else "#ffd75f",
            bg="#141414", font=("monospace", 11, "bold"), anchor="w")
        self._board_label.pack(side="left")
        tk.Label(hdr, text="  Flash:", fg="#8a8a8a", bg="#141414").pack(side="left", padx=(12, 2))
        self._flash_label = tk.Label(hdr, text="idle", fg="#8a8a8a",
                                     bg="#141414", font=("monospace", 11),
                                     anchor="w")
        self._flash_label.pack(side="left")

        # Three fixed connection lines: serial / usb / jtag. Each shows the
        # device identity or 'n/a'. The link/log status sits on the line for the
        # transport this broker owns (serial for an FTDI console, usb for a
        # MAVLink-shell console). Devices are polled since they come and go.
        conn = tk.Frame(self._root, bg="#1c1c1c")
        conn.pack(fill="x")
        conn.columnconfigure(3, weight=1)          # push status to the far right

        def _tag(text, fg):
            return tk.Label(conn, text=text, fg=fg, bg="#2a2a2a",
                            font=("monospace", 9, "bold"), padx=6)

        def _cell(text, fg="#8a8a8a", weight="normal"):
            return tk.Label(conn, text=text, fg=fg, bg="#1c1c1c", anchor="w",
                            font=("monospace", 9, weight))

        role_colors = {"serial": "#5fd7ff", "usb": "#5fd75f", "jtag": "#d78fff"}
        self._ep_name = {}
        self._ep_tech = {}
        self._status = None
        for row, role in enumerate(("serial", "usb", "jtag")):
            _tag(role, role_colors[role]).grid(row=row, column=0, sticky="w",
                                               padx=(6, 8), pady=(2, 2))
            self._ep_name[role] = _cell("", fg="#d0d0d0", weight="bold")
            self._ep_name[role].grid(row=row, column=1, sticky="w", padx=(0, 12))
            self._ep_tech[role] = _cell("")
            self._ep_tech[role].grid(row=row, column=2, sticky="w")
            if role == self._owned:
                self._status = tk.Label(
                    conn, text="● connecting  ·  %s"
                    % os.path.basename(self._log_path),
                    fg="#ffd75f", bg="#1c1c1c", anchor="e")
                self._status.grid(row=row, column=3, sticky="e", padx=6)
        self._refresh_endpoints()

        text_frame = tk.Frame(self._root, bg="#101010")
        text_frame.pack(fill="both", expand=True)
        self._scroll = tk.Scrollbar(text_frame, command=self._on_scroll)
        self._scroll.pack(side="right", fill="y")
        self._text = tk.Text(text_frame, bg="#101010", fg="#d0d0d0",
                             insertbackground="#d0d0d0",
                             font=("monospace", 11), state="disabled", wrap="char",
                             yscrollcommand=self._scroll.set)
        self._text.pack(side="left", fill="both", expand=True)
        for tag, color in self.COLORS.items():
            self._text.tag_configure(tag, foreground=color)
        # Keyword highlight tags (raised above the base OUT tag so they win).
        for color in sorted(set(c for _, c in self._rules)):
            self._text.tag_configure("kw_" + color, foreground=color)
            self._text.tag_raise("kw_" + color)
        # Visible selection highlight on the dark background (a disabled Text
        # can still be mouse-selected and copied).
        self._text.tag_configure("sel", background="#2b4a6f")
        self._text.bind("<MouseWheel>", self._pause_autoscroll)
        self._text.bind("<Button-4>", self._pause_autoscroll)
        self._text.bind("<Button-5>", self._pause_autoscroll)
        # Right-click menu on the output: copy the current selection.
        self._out_menu = tk.Menu(self._root, tearoff=0)
        self._out_menu.add_command(label="Copy", command=self._copy_output)
        self._out_menu.add_command(label="Select all", command=self._select_all_output)
        self._out_menu.add_separator()
        self._out_menu.add_command(label="Clear", command=self._clear)
        self._text.bind("<Button-3>", self._show_out_menu)
        self._text.bind("<Control-c>", self._copy_output_evt)

        bottom = tk.Frame(self._root, bg="#1c1c1c")
        bottom.pack(fill="x")
        tk.Label(bottom, text=">", fg="#d0d0d0", bg="#1c1c1c").pack(side="left")
        self._entry = tk.Entry(bottom, bg="#101010", fg="#d0d0d0",
                               insertbackground="#d0d0d0")
        self._entry.pack(side="left", fill="x", expand=True, padx=4, pady=4)
        self._entry.bind("<Return>", self._on_enter)
        self._entry.bind("<Up>", self._history_prev)
        self._entry.bind("<Down>", self._history_next)
        # Right-click menu on the command line: paste (newlines collapsed so a
        # command copied out of the output pastes as one line), copy, cut.
        self._entry_menu = tk.Menu(self._root, tearoff=0)
        self._entry_menu.add_command(label="Paste", command=self._paste_entry)
        self._entry_menu.add_command(label="Copy", command=self._copy_entry)
        self._entry_menu.add_command(label="Cut", command=self._cut_entry)
        self._entry.bind("<Button-3>", self._show_entry_menu)
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

    def _append_output(self, line):
        # Insert with the base OUT tag, then overlay a color tag on each matched
        # keyword token (just the token, not the whole line).
        self._text.configure(state="normal")
        start = self._text.index("end-1c")
        self._text.insert("end", line + "\n", "OUT")
        for s, e, color in find_keyword_spans(line, self._rules):
            self._text.tag_add("kw_" + color,
                               "%s+%dc" % (start, s),
                               "%s+%dc" % (start, e))
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
                # Device redraw commands (top/uorb top) emit ANSI codes that the
                # Text widget can't interpret; strip them for display. The raw
                # log and TCP stream keep the original bytes.
                self._append_output(strip_ansi(payload))
            elif kind == "input":
                # Show what we sent, tagged by source (USER/SKILL). Don't prefix
                # a fake "nsh> " prompt -- it's not from the device and reads as
                # a real prompt even on a loopback or a board that never replied.
                source, text = payload
                self._append(source, text)
            elif kind == "status":
                self._append("STAT", "[" + payload + "]")
                self._update_status(payload)
            elif kind == "board":
                self._update_board(payload)
            elif kind == "flash":
                self._update_flash(payload)

    def _update_board(self, name):
        if name:
            self._board_label.configure(text=name, fg="#5fd7ff")
            self._root.title("PX4 Console - %s" % name)
        elif self._board_name:
            self._board_label.configure(text=self._board_name, fg="#5fd7ff")
        else:
            self._board_label.configure(text="(detecting…)", fg="#ffd75f")

    def _update_flash(self, msg):
        if msg.startswith("error"):
            color = "#ff5f5f"
        elif msg == "done":
            color = "#5fd75f"
        else:
            color = "#ffd75f"
        self._flash_label.configure(text=msg, fg=color)

    def _refresh_board(self):
        self._board_label.configure(text="(detecting…)", fg="#ffd75f")
        self._broker.probe_board()

    def _refresh_endpoints(self):
        for role in ("serial", "usb", "jtag"):
            info = endpoint_info(self._endpoints.get(role))
            if info is None:
                self._ep_name[role].configure(text="n/a", fg="#6a6a6a")
                self._ep_tech[role].configure(text="", fg="#6a6a6a")
            else:
                name, tech = info
                name = name or "(unknown)"
                if role == "usb" and self._owned == "usb":
                    name += "  [MAVLink shell]"
                self._ep_name[role].configure(text=name, fg="#d0d0d0")
                self._ep_tech[role].configure(text=tech, fg="#8a8a8a")
        self._root.after(2000, self._refresh_endpoints)

    def _update_status(self, payload):
        if self._status is None:
            return
        logname = os.path.basename(self._log_path)
        if payload.startswith("connected"):
            self._status.configure(text="● connected  ·  %s" % logname,
                                   fg="#5fd75f")
        else:
            self._status.configure(
                text="● disconnected - reconnecting  ·  %s" % logname,
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

    def _on_scroll(self, *args):
        # Scrollbar drag/click: scroll the text, then reassess autoscroll (stays
        # on only while the view is parked at the bottom).
        self._text.yview(*args)
        self._pause_autoscroll(None)

    # ---- copy / paste -------------------------------------------------
    def _show_out_menu(self, event):
        try:
            self._out_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._out_menu.grab_release()

    def _copy_output(self):
        try:
            sel = self._text.get("sel.first", "sel.last")
        except Exception:
            return                       # nothing selected
        if sel:
            self._root.clipboard_clear()
            self._root.clipboard_append(sel)

    def _copy_output_evt(self, _event):
        self._copy_output()
        return "break"

    def _select_all_output(self):
        self._text.tag_add("sel", "1.0", "end-1c")

    def _show_entry_menu(self, event):
        try:
            self._entry_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._entry_menu.grab_release()

    def _paste_entry(self):
        try:
            text = self._root.clipboard_get()
        except Exception:
            return                       # empty / non-text clipboard
        # The command line is single-line; collapse newlines so a multi-line
        # copy (e.g. a wrapped command from the output) pastes as one command.
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if self._entry.selection_present():
            self._entry.delete("sel.first", "sel.last")
        self._entry.insert("insert", text)

    def _copy_entry(self):
        self._entry.event_generate("<<Copy>>")

    def _cut_entry(self):
        self._entry.event_generate("<<Cut>>")

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


def _parse_args(argv):
    p = argparse.ArgumentParser(description="PX4 shared NSH console broker")
    p.add_argument("--serial",
                   help="FTDI/UART console device (path or /dev/serial/by-id/...)")
    p.add_argument("--usb",
                   help="board USB VCP, driven over MAVLink SERIAL_CONTROL "
                        "(path or by-id); use for boards with no UART console")
    p.add_argument("--jtag",
                   help="SWD/JTAG probe device, shown in the header (informational)")
    p.add_argument("--name",
                   help="board name shown on top (else auto-detected from HW arch)")
    p.add_argument("--port", help="alias for --serial (back-compat)")
    p.add_argument("--baud", type=int, default=57600)
    p.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    p.add_argument("--headless", action="store_true",
                   help="run without the GUI window")
    args = p.parse_args(argv)
    if args.serial is None and args.port is not None:
        args.serial = args.port
    if args.serial is None and args.usb is None:
        args.serial = "/dev/ttyUSB0"           # back-compat default
    return args


def main(argv=None):
    args = _parse_args(argv)

    # Import here so the module stays importable where console_client isn't on
    # the path during isolated unit tests of LineBuffer/SessionLog.
    from console_client import Console as _C
    if _C.ping(port=args.tcp_port, timeout=0.5):
        sys.stderr.write(
            "A console broker is already running on 127.0.0.1:%d; refusing to "
            "start a second broker on this port.\n" % args.tcp_port)
        return 1

    # The transport this broker drives: an FTDI/UART serial console when --serial
    # is set, otherwise the board's USB VCP over MAVLink (--usb). The one it owns
    # carries the connection status; the others are informational header lines.
    owned = "serial" if args.serial else "usb"
    primary = args.serial if owned == "serial" else args.usb
    if owned == "serial":
        factory = open_serial
    else:
        from mavlink_serial import open_mavlink
        factory = open_mavlink

    log = SessionLog(LOG_DIR, label=str(args.tcp_port))
    # Record which physical USB adapter we're bound to at the top of the log
    # and on stdout, so a session transcript is self-describing even headless.
    usb = usb_info(primary)
    log.log_status("usb " + usb)
    broker = Broker(primary, args.baud, args.tcp_port, log, serial_factory=factory,
                    owns_board_usb=(owned == "usb"))
    broker.start()

    endpoints = {"serial": args.serial, "usb": args.usb, "jtag": args.jtag}

    if args.headless:
        sys.stdout.write(
            "[console] headless broker on 127.0.0.1:%d\n"
            "[console] %s: %s\n"
            "[console] logging to %s\n"
            % (broker.tcp_port, owned, usb, log.path))
        sys.stdout.flush()
        stop = threading.Event()

        def _stop(*_):
            stop.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        stop.wait()
    else:
        gui = ConsoleGUI(broker, log.path, endpoints=endpoints,
                         owned=owned, board_name=args.name)
        gui.run()

    broker.stop()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""PX4 shared NSH console: single serial-port owner with a localhost TCP
interface and an optional tkinter GUI. See the px4-hardware-debug SKILL.md."""

import argparse
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

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
DEFAULT_TCP_PORT = 8765

# ANSI escape sequences: CSI (\x1b[ ... final) plus two-character escapes.
# Screen-redraw commands like `top`/`uorb top` emit these (clear-screen,
# cursor-home, erase-line, colors); the tkinter Text widget would otherwise
# render them as literal `[K`/`[2J`.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text):
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


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
    }

    def __init__(self, broker, log_path, color_rules=None):
        self._broker = broker
        self._log_path = log_path
        self._rules = (color_rules if color_rules is not None
                       else load_keyword_colors(COLOR_CONFIG))
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
        self._status = tk.Label(top, text="● connecting  ·  %s"
                                % os.path.basename(self._log_path),
                                fg="#ffd75f", bg="#1c1c1c", anchor="e")
        self._status.pack(side="right", padx=6, pady=2)

        self._text = tk.Text(self._root, bg="#101010", fg="#d0d0d0",
                             insertbackground="#d0d0d0",
                             font=("monospace", 11), state="disabled", wrap="char")
        self._text.pack(fill="both", expand=True)
        for tag, color in self.COLORS.items():
            self._text.tag_configure(tag, foreground=color)
        # Keyword highlight tags (raised above the base OUT tag so they win).
        for color in sorted(set(c for _, c in self._rules)):
            self._text.tag_configure("kw_" + color, foreground=color)
            self._text.tag_raise("kw_" + color)
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

    def _update_status(self, payload):
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

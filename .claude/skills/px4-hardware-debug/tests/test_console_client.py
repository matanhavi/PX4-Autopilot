import os

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


def test_gui_strips_ansi_from_output(tmp_path, pty_serial):
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
        gui.build()
        gui._on_event("output", "\x1b[K 523 gps  fix")
        gui._drain_events()
        text = gui._text.get("1.0", "end")
        assert " 523 gps  fix" in text
        assert "\x1b" not in text and "[K" not in text
        gui.destroy()
    finally:
        broker.stop()
        log.close()


def test_gui_keyword_coloring_and_log_name(tmp_path, pty_serial):
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
        gui = ConsoleGUI(broker, log.path,
                         color_rules=[("ERROR", "red"), ("WRN", "orange")])
        gui.build()
        gui._on_event("output", "ERROR motor failed")
        gui._on_event("status", "connected ttyUSB0 57600")
        gui._drain_events()
        # Only the keyword token is tagged, not the whole line.
        r = gui._text.tag_nextrange("kw_red", "1.0")
        assert r and gui._text.get(*r) == "ERROR"
        # Log file name appears in the info line next to the status.
        assert os.path.basename(log.path) in gui._status.cget("text")
        gui.destroy()
    finally:
        broker.stop()
        log.close()

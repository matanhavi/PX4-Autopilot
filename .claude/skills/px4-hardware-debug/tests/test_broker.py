import socket

from console import Broker, SessionLog, main
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
    # "SKILL" fills the 5-char tag column exactly, so a single separator space.
    assert "SKILL > ver all" in (tmp_path / log.path.split("/")[-1]).read_text()


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


def test_main_refuses_when_broker_already_running(tmp_path, pty_serial, capsys):
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

import os, socket, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import console_client


def test_flash_lines_are_captured():
    c = console_client.Console.__new__(console_client.Console)  # no socket
    c.flash_status = []
    # simulate the reader loop classifying an inbound FLASH line
    console_client.Console._classify(c, "FLASH erasing 20%")
    console_client.Console._classify(c, "FLASH done")
    assert c.flash_status == ["erasing 20%", "done"]


def test_reader_survives_idle_gap_longer_than_connect_timeout():
    # Regression: the connect timeout used to persist on the socket, so the
    # reader thread died with socket.timeout during any idle gap (e.g. the ~20s
    # between "flash starting" and the first uploader progress line), capturing
    # only the first status. A real flash then reported wait_flash_done -> None.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"FLASH starting foo\n")
        time.sleep(0.6)                  # idle gap > the 0.3s connect timeout
        conn.sendall(b"FLASH done\n")
        time.sleep(0.3)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    c = console_client.Console(port=port, connect_timeout=0.3)
    result = c.wait_flash_done(timeout=5)
    c.close()
    srv.close()
    assert c.flash_status == ["starting foo", "done"]
    assert result == "done"


if __name__ == "__main__":
    test_flash_lines_are_captured()
    test_reader_survives_idle_gap_longer_than_connect_timeout()
    print("OK")

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


def test_flash_rejects_flag_target():
    events = []
    b = _make_broker(events)
    b.flash("--eval=$(shell touch pwned)")
    kinds = [p for k, p in events if k == "flash"]
    assert any(s.startswith("error: invalid target") for s in kinds)
    assert not any(s.startswith("starting") for s in kinds)
    assert b._flashing is False


if __name__ == "__main__":
    test_set_flash_status_emits_event()
    test_flash_runs_and_reports_done()
    test_flash_rejects_flag_target()
    print("OK")

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

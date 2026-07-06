import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flash_status import parse_flash_line
from flash_status import build_flash_cmd, run_flash, valid_target


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


def test_build_flash_cmd():
    assert build_flash_cmd("aerium_apex_h7_rev_a_default", "/repo") == \
        ["make", "-C", "/repo", "--", "aerium_apex_h7_rev_a_default", "upload"]


def test_build_flash_cmd_rejects_flag_injection():
    # Argument injection: a target starting with '-' or carrying shell/whitespace
    # metacharacters must not reach make (e.g. --eval=$(shell ...) is RCE).
    for bad in ["-C/tmp", "--eval=$(shell touch pwned)", "-j4", "",
                "a b", "a;b", "$(pwd)", "../etc"]:
        try:
            build_flash_cmd(bad, "/repo")
        except ValueError:
            continue
        assert False, "expected ValueError for %r" % (bad,)
    assert valid_target("aerium_apex_h7_rev_a_default")


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


if __name__ == "__main__":
    test_parse_flash_line()
    test_build_flash_cmd()
    test_build_flash_cmd_rejects_flag_injection()
    test_run_flash_parses_stream_and_finishes()
    test_run_flash_reports_nonzero_exit()
    print("OK")

from console import strip_ansi


def test_plain_text_unchanged():
    assert strip_ansi("plain text") == "plain text"


def test_erase_line_prefix():
    assert strip_ansi("\x1b[Kver hw") == "ver hw"


def test_clear_screen_becomes_empty():
    assert strip_ansi("\x1b[2J") == ""


def test_cursor_home_and_erase():
    assert strip_ansi("\x1b[H\x1b[K 523 gps") == " 523 gps"


def test_color_codes_removed_text_kept():
    assert strip_ansi("\x1b[31mWARN\x1b[0m") == "WARN"


def test_prompt_redraw():
    assert strip_ansi("nsh> \x1b[K") == "nsh> "

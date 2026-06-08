from console import LineBuffer


def test_no_newline_buffers():
    lb = LineBuffer()
    assert lb.feed("abc") == []
    assert lb.flush() == "abc"
    assert lb.flush() is None


def test_single_line():
    lb = LineBuffer()
    assert lb.feed("abc\n") == ["abc"]
    assert lb.flush() is None


def test_multiple_lines_with_remainder():
    lb = LineBuffer()
    assert lb.feed("a\nb\nc") == ["a", "b"]
    assert lb.flush() == "c"


def test_crlf_is_stripped():
    lb = LineBuffer()
    assert lb.feed("abc\r\ndef") == ["abc"]
    assert lb.flush() == "def"


def test_split_across_feeds():
    lb = LineBuffer()
    assert lb.feed("ab") == []
    assert lb.feed("c\nde") == ["abc"]
    assert lb.flush() == "de"

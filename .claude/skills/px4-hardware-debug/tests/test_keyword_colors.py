import json

from console import load_keyword_colors, find_keyword_spans


def test_load_returns_keyword_color_pairs(tmp_path):
    p = tmp_path / "colors.json"
    p.write_text(json.dumps({"red": ["ERROR", "ERR"], "orange": ["Warning", "WRN"]}))
    rules = load_keyword_colors(str(p))
    assert set(rules) == {("ERROR", "red"), ("ERR", "red"),
                          ("Warning", "orange"), ("WRN", "orange")}


def test_load_missing_file_returns_empty():
    assert load_keyword_colors("/no/such/file.json") == []


def test_load_malformed_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert load_keyword_colors(str(p)) == []


def test_span_matches_keyword():
    assert find_keyword_spans("ERROR [dataman] failed", [("ERROR", "red")]) \
        == [(0, 5, "red")]


def test_span_is_case_insensitive():
    assert find_keyword_spans("warning: low battery", [("Warning", "orange")]) \
        == [(0, 7, "orange")]


def test_span_respects_word_boundaries():
    # "ERR" must not match inside "errors"
    assert find_keyword_spans("no errors here", [("ERR", "red")]) == []
    # ...and must not match inside "ERROR"
    assert find_keyword_spans("ERROR happened", [("ERR", "red")]) == []


def test_span_multiple_keywords():
    spans = find_keyword_spans("ERROR and WRN seen",
                               [("ERROR", "red"), ("WRN", "orange")])
    assert (0, 5, "red") in spans
    assert (10, 13, "orange") in spans

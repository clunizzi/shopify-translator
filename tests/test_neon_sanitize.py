from src.state import neon


def test_sanitize_text_removes_nul():
    assert neon.sanitize_text("ab\x00cd") == "abcd"


def test_sanitize_json_value_removes_nul_recursively():
    payload = {
        "a": "x\x00y",
        "b": ["z\x00", {"c": "q\x00w"}],
    }
    assert neon.sanitize_json_value(payload) == {
        "a": "xy",
        "b": ["z", {"c": "qw"}],
    }


def test_make_source_hash_ignores_nul():
    assert neon.make_source_hash("abc\x00def") == neon.make_source_hash("abcdef")

from src.htmlmap.extract import extract_text_segments
from src.htmlmap.reinject import reinject_text


def test_html_extract_reinject_roundtrip():
    html = "<p>Ciao <strong>mondo</strong>!</p>"
    mapped, segments = extract_text_segments(html)
    assert segments and "[[T0]]" in mapped
    reinjected = reinject_text(mapped, [seg.upper() for seg in segments])
    assert "<strong>" in reinjected
    assert "CIAO" in reinjected

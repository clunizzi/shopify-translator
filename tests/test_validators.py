from src.translate.validators import (
    enforce_meta_title_format,
    make_handle_from_title,
    validate_handle,
    validate_meta_length,
)


def test_handle_generation_and_validation():
    h = make_handle_from_title("Scarpe Da Corsa Leggere")
    assert validate_handle(h)


def test_meta_helpers():
    s = enforce_meta_title_format("Prodotto X", "Brand", "Benefit")
    assert " | " in s
    ok, cut = validate_meta_length("x" * 200, 60)
    assert not ok
    assert len(cut) <= 60

import asyncio

import pytest

from src.translate.cache import TranslationCache
from src.translate.translator import (
    DoNotTranslateConfig,
    TranslationError,
    Translator,
    json_translation_output_issue,
    meta_shape_issue,
    translation_output_issue,
)


def _translator() -> Translator:
    return Translator(cache=TranslationCache(":memory:"), model="test-model")


def test_plain_translation_error_never_falls_back_to_source(monkeypatch):
    translator = _translator()

    def _raise(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(translator, "_call_openai", _raise)

    with pytest.raises(TranslationError):
        translator._translate_and_validate(
            "title",
            "Motocoltivatore professionale",
            "de",
            [],
            "translate",
        )


def test_html_structure_failure_never_returns_source(monkeypatch):
    translator = _translator()
    monkeypatch.setattr(
        translator,
        "_call_openai",
        lambda *args, **kwargs: ("<div>Professioneller Einachsschlepper</div>", {}),
    )

    with pytest.raises(TranslationError):
        translator.translate_html_document(
            "PRODUCT",
            "body_html",
            "<p>Motocoltivatore professionale</p>",
            "de",
            DoNotTranslateConfig(brands=[], units=[], tokens=[]),
            [],
        )


def test_html_structure_failure_falls_back_to_text_nodes(monkeypatch):
    translator = _translator()
    outputs = iter(
        [
            ("<div>Hallo Welt</div>", {}),
            ("Hallo", {}),
            ("Welt", {}),
        ]
    )
    monkeypatch.setattr(
        translator,
        "_call_openai",
        lambda *args, **kwargs: next(outputs),
    )

    result = translator.translate_html_document(
        "PRODUCT",
        "body_html",
        "<p>Ciao<br/>• <strong>mondo</strong></p>",
        "de",
        DoNotTranslateConfig(brands=[], units=[], tokens=[]),
        [],
    )

    assert result == "<p>Hallo<br/>• <strong>Welt</strong></p>"


def test_json_parse_failure_never_returns_empty_payload(monkeypatch):
    translator = _translator()
    monkeypatch.setattr(
        translator,
        "_call_openai",
        lambda *args, **kwargs: ("not-json", {}),
    )

    with pytest.raises(TranslationError):
        translator.translate_json_value(
            "METAFIELD",
            "value",
            '{"description":"Motocoltivatore professionale"}',
            "de",
            DoNotTranslateConfig(brands=[], units=[], tokens=[]),
            [],
        )


@pytest.mark.parametrize(
    ("source", "translated", "reason"),
    [
        ("Titolo", '```json\n["Titre"]\n```', "unexpected_code_fence"),
        ("Titolo", '["Titre"]', "unexpected_embedded_json"),
        ("Titolo", "x" * 400, "extreme_length_inflation"),
    ],
)
def test_suspicious_model_output_is_rejected(source, translated, reason):
    assert translation_output_issue(source, translated) == reason


def test_domain_glossary_rejects_wrong_agricultural_term():
    dnt = DoNotTranslateConfig(
        brands=[],
        units=[],
        tokens=[],
        glossary={"fr": {"zappe": ["houe"], "fresa": ["fraiseuse", "fraise rotative"]}},
    )

    assert (
        translation_output_issue(
            "Fresa fissa con 54 zappe",
            "Fraiseuse fixe avec 54 sabots",
            target_locale="fr",
            dnt=dnt,
        )
        == "glossary_mismatch:zappe"
    )
    assert (
        translation_output_issue(
            "Fresa fissa con 54 zappe",
            "Fraiseuse fixe avec 54 houes",
            target_locale="fr-FR",
            dnt=dnt,
        )
        is None
    )


def test_domain_glossary_rejects_untranslated_motozappa():
    dnt = DoNotTranslateConfig(
        brands=[],
        units=[],
        tokens=[],
        glossary={
            "de": {"motozappa": ["Motorhacke"]},
            "fr": {"motozappa": ["motobineuse"]},
        },
    )

    assert (
        translation_output_issue(
            "Motozappa potente per piccoli giardini",
            "Leistungsstarke Motozappa für kleine Gärten",
            target_locale="de",
            dnt=dnt,
        )
        == "glossary_mismatch:motozappa"
    )
    assert (
        translation_output_issue(
            "Motozappa potente per piccoli giardini",
            "Leistungsstarke Motorhacke für kleine Gärten",
            target_locale="de",
            dnt=dnt,
        )
        is None
    )


def test_specific_glossary_phrase_overrides_overlapping_generic_term():
    dnt = DoNotTranslateConfig(
        brands=[],
        units=[],
        tokens=[],
        glossary={
            "fr": {
                "fresa": ["fraiseuse"],
                "fresa per terreno": ["sarcleuse"],
            }
        },
    )

    assert (
        translation_output_issue(
            "Stihl - Fresa per terreno BF",
            "Stihl - Sarcleuse BF",
            target_locale="fr",
            dnt=dnt,
        )
        is None
    )


def test_exact_glossary_match_is_deterministic_for_plain_and_json(monkeypatch):
    translator = _translator()
    dnt = DoNotTranslateConfig(
        brands=[],
        units=[],
        tokens=[],
        glossary={
            "fr": {
                "Stihl - Fresa Per Terreno BF": ["Stihl - Sarcleuse BF"],
                "fresa": ["fraiseuse"],
            }
        },
    )

    def _unexpected_openai_call(*args, **kwargs):
        raise AssertionError("an exact glossary match must not call OpenAI")

    monkeypatch.setattr(translator, "_call_openai", _unexpected_openai_call)

    assert (
        translator.translate_plain(
            "METAFIELD",
            "value",
            "  STIHL - FRESA PER TERRENO BF  ",
            "fr-FR",
            dnt,
            [],
        )
        == "Stihl - Sarcleuse BF"
    )
    assert (
        translator.translate_json_value(
            "METAFIELD",
            "value",
            '{"title":"Stihl - Fresa Per Terreno BF"}',
            "fr",
            dnt,
            [],
        )
        == '{"title": "Stihl - Sarcleuse BF"}'
    )


@pytest.mark.parametrize(
    ("field", "text", "locale", "reason"),
    [
        ("meta_title", "Stihl | Leistungsstarker Sauger |", "de", "trailing_separator"),
        ("meta_description", "Leistungsstarker Sauger. Ideal für", "de", "dangling_word"),
        ("meta_description", "Aspirateur professionnel conçu pour", "fr", "dangling_word"),
        ("meta_title", "Stihl Nass-/Trockensauger mit Drehzahlregelung", "de", None),
    ],
)
def test_meta_shape_issue_rejects_mechanical_truncation(field, text, locale, reason):
    assert meta_shape_issue(field, text, locale) == reason


def test_meta_fit_rewrites_instead_of_hard_cut(monkeypatch):
    translator = _translator()
    calls = []

    def _rewrite(*args, **kwargs):
        calls.append((args, kwargs))
        return (
            "Stihl Industriesauger mit Katalysator und Drehzahlregelung.",
            {},
        )

    monkeypatch.setattr(translator, "_call_openai", _rewrite)
    result = translator._fit_meta_translation(
        type_name="PRODUCT",
        field="meta_description",
        source_text="Potente aspiratore con catalizzatore e regolazione giri.",
        draft="Leistungsstarker Sauger mit Katalysator. Ideal für",
        target_locale="de",
        dnt=DoNotTranslateConfig(brands=["Stihl"], units=[], tokens=[]),
    )

    assert result == "Stihl Industriesauger mit Katalysator und Drehzahlregelung."
    assert len(calls) == 1


def test_async_meta_fit_rechecks_glossary_after_rewrite(monkeypatch):
    translator = _translator()
    dnt = DoNotTranslateConfig(
        brands=["Grillo"],
        units=["cm"],
        tokens=[],
        glossary={"de": {"fresa": ["Fräse", "Bodenfräse"]}},
    )

    async def _rewrite(*args, **kwargs):
        return (
            "Grillo MAX3 Diesel-Motorhacke mit 441 cc, 9,1 HP und verstellbarer 68-cm-Fräse.",
            {},
        )

    monkeypatch.setattr(translator, "_call_openai_async", _rewrite)
    result = asyncio.run(
        translator._fit_meta_translation_async(
            type_name="PRODUCT",
            field="meta_description",
            source_text="Grillo MAX3 con motore 441 cc, 9,1 HP e fresa registrabile da 68 cm.",
            draft="Grillo MAX3 Diesel-Motorhacke mit 441 cc und 9,1 HP.",
            target_locale="de",
            dnt=dnt,
        )
    )

    assert "Fräse" in result


def test_json_cache_validator_preserves_blank_and_blocked_leaves():
    dnt = DoNotTranslateConfig(brands=[], units=[], tokens=[])
    source = (
        '{"disclaimer":"","rows":[{"title":"Titolo",' '"image_url":"https://example.com/a.jpg"}]}'
    )
    def leaf_filter(path, _value):
        return path[-1] in {"disclaimer", "title"}

    valid = (
        '{"disclaimer":"","rows":[{"title":"Titel",' '"image_url":"https://example.com/a.jpg"}]}'
    )
    changed_blank = (
        '{"disclaimer":"Hinweis","rows":[{"title":"Titel",'
        '"image_url":"https://example.com/a.jpg"}]}'
    )
    changed_url = (
        '{"disclaimer":"","rows":[{"title":"Titel",' '"image_url":"https://example.com/b.jpg"}]}'
    )

    assert (
        json_translation_output_issue(
            source,
            valid,
            target_locale="de",
            dnt=dnt,
            should_translate_leaf=leaf_filter,
        )
        is None
    )
    assert (
        json_translation_output_issue(
            source,
            changed_blank,
            target_locale="de",
            dnt=dnt,
            should_translate_leaf=leaf_filter,
        )
        == "blank_json_leaf_changed"
    )
    assert (
        json_translation_output_issue(
            source,
            changed_url,
            target_locale="de",
            dnt=dnt,
            should_translate_leaf=leaf_filter,
        )
        == "blocked_json_leaf_changed"
    )

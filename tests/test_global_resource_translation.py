from types import SimpleNamespace

from src.bootstrap.resources import (
    collection_entry_is_enabled,
    deterministic_collection_title,
    resource_entry_needs_sync,
    should_translate_resource_entry,
    translate_resource_document,
)


class _Translator:
    def translate_plain(self, resource_type, key, value, target_locale, dnt, exclude_tokens):
        if key == "title":
            return "Olivenrüttler"
        raise AssertionError(f"Unexpected plain translation: {key}")

    def translate_html_document(self, *args, **kwargs):
        raise AssertionError("HTML translation not expected")


def _source_document():
    return {
        "shop_domain": "example.myshopify.com",
        "resource_group": "global",
        "resource_type": "PAGE",
        "resource_id": "gid://shopify/Page/1",
        "source_locale": "it",
        "entries": {
            "title": {
                "resource_id": "gid://shopify/Page/1",
                "key": "title",
                "value": "Abbacchiatori",
                "digest": "title-digest",
                "content_kind": "plain",
            },
            "handle": {
                "resource_id": "gid://shopify/Page/1",
                "key": "handle",
                "value": "abbacchiatori",
                "digest": "handle-digest",
                "content_kind": "plain",
            },
        },
    }


def test_missing_handle_is_slugged_from_localized_title(monkeypatch):
    monkeypatch.setattr(
        "src.bootstrap.resources.load_do_not_translate",
        lambda path: SimpleNamespace(brands=[], units=[], tokens=[]),
    )
    document, _hashes, payloads, sources = translate_resource_document(
        source_document=_source_document(),
        changed_sections={"PAGE.title", "PAGE.handle"},
        target_locale="de",
        translator=_Translator(),
        current_translations={},
        reserved_handles=set(),
    )

    assert document["entries"]["handle"] == "olivenruttler"
    assert next(item for item in payloads if item["key"] == "handle")["value"] == "olivenruttler"
    assert sources["PAGE.handle"] == "handle_from_localized_title"


def test_outdated_handle_value_is_preserved(monkeypatch):
    monkeypatch.setattr(
        "src.bootstrap.resources.load_do_not_translate",
        lambda path: SimpleNamespace(brands=[], units=[], tokens=[]),
    )
    document, _hashes, payloads, sources = translate_resource_document(
        source_document=_source_document(),
        changed_sections={"PAGE.handle"},
        target_locale="de",
        translator=_Translator(),
        current_translations={"handle": {"value": "historische-seite", "outdated": True}},
        reserved_handles={"historische-seite"},
    )

    assert document["entries"]["handle"] == "historische-seite"
    assert (
        next(item for item in payloads if item["key"] == "handle")["value"] == "historische-seite"
    )
    assert sources["PAGE.handle"] == "shopify_handle_preserved"


def test_missing_handle_avoids_source_and_localized_collisions(monkeypatch):
    monkeypatch.setattr(
        "src.bootstrap.resources.load_do_not_translate",
        lambda path: SimpleNamespace(brands=[], units=[], tokens=[]),
    )
    document, _hashes, _payloads, _sources = translate_resource_document(
        source_document=_source_document(),
        changed_sections={"PAGE.title", "PAGE.handle"},
        target_locale="de",
        translator=_Translator(),
        current_translations={},
        reserved_handles={"olivenruttler"},
    )

    assert document["entries"]["handle"] == "olivenruttler-2"


def test_html_without_visible_text_is_not_sent_for_translation():
    assert not should_translate_resource_entry(
        resource_type="PAGE",
        key="body_html",
        value='<div id="revy-bundle-bundles-page"></div>',
    )


def test_current_html_translation_gets_localized_links_without_ai(monkeypatch):
    monkeypatch.setattr(
        "src.bootstrap.resources.load_do_not_translate",
        lambda path: SimpleNamespace(brands=[], units=[], tokens=[]),
    )
    source = _source_document()
    source["entries"] = {
        "body": {
            "resource_id": "gid://shopify/Page/1",
            "key": "body",
            "value": '<p><a href="/pages/negozio">Negozio</a></p>',
            "digest": "digest-body",
            "locale": "it",
            "content_kind": "html",
        }
    }

    document, _hashes, _payloads, sources = translate_resource_document(
        source_document=source,
        changed_sections={"PAGE.body"},
        target_locale="de",
        translator=_Translator(),
        current_translations={
            "body": {
                "value": '<p><a href="/pages/negozio">Zum Shop</a></p>',
                "outdated": False,
            }
        },
        route_prefixes={"de": "/de-de"},
        localized_handle_maps={"de": {"pages": {"negozio": "shop"}}},
    )

    assert 'href="/de-de/pages/shop"' in document["entries"]["body"]
    assert sources["PAGE.body"] == "current_shopify_translation_link_rewrite"


def test_spare_parts_collection_titles_are_deterministic_and_keep_model_verbatim():
    assert deterministic_collection_title("Ricambi MS 194 T", "de") == (
        "Ersatzteile für MS 194 T"
    )
    assert deterministic_collection_title("Ricambi MS 194 T", "fr") == (
        "Pièces détachées pour MS 194 T"
    )
    assert deterministic_collection_title("Abbacchiatori", "de") is None


def test_generic_spare_parts_collection_titles_use_curated_phrasing():
    assert deterministic_collection_title("Ricambi e manutenzione", "de") == (
        "Ersatzteile und Wartung"
    )
    assert deterministic_collection_title("Ricambi e manutenzione", "fr") == (
        "Pièces détachées et entretien"
    )


def test_missing_handle_is_valid_when_localized_title_keeps_canonical_slug():
    source = _source_document()
    source["entries"]["title"]["value"] = "Honda"
    source["entries"]["handle"]["value"] = "honda"

    assert not resource_entry_needs_sync(
        source_document=source,
        section_name="COLLECTION.handle",
        remote={"title": {"value": "Honda", "outdated": False}},
    )


def test_missing_handle_is_pending_when_localized_title_changes_slug():
    source = _source_document()

    assert resource_entry_needs_sync(
        source_document=source,
        section_name="COLLECTION.handle",
        remote={"title": {"value": "Pièces détachées", "outdated": False}},
    )


def test_collection_ai_gate_allows_pattern_but_blocks_freeform_seo(monkeypatch):
    source = _source_document()
    source["resource_type"] = "COLLECTION"
    source["entries"]["title"]["value"] = "Ricambi MS 194 T"

    assert collection_entry_is_enabled(
        source_document=source,
        section_name="COLLECTION.title",
        target_locale="de",
        remote={},
    )
    assert not collection_entry_is_enabled(
        source_document=source,
        section_name="COLLECTION.meta_description",
        target_locale="de",
        remote={},
    )

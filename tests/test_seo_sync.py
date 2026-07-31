import asyncio

from src.bootstrap import seo
from src.state.neon import make_source_hash

PRODUCT_GID = "gid://shopify/Product/123"


class _FakeStore:
    def __init__(self):
        self.memory = {}
        self.writes = []

    def get_translation_memory(
        self,
        *,
        source_hash,
        field_key,
        source_locale,
        target_locale,
    ):
        return self.memory.get((source_hash, field_key, source_locale, target_locale))

    def upsert_translation_memory(self, **kwargs):
        key = (
            kwargs["source_hash"],
            kwargs["field_key"],
            kwargs["source_locale"],
            kwargs["target_locale"],
        )
        self.memory[key] = kwargs["translated_value"]
        self.writes.append(kwargs)


class _FakeTranslator:
    model = "test-model"
    dry_run = False

    def __init__(self, translations=None):
        self.translations = translations or {}
        self.calls = []

    def translate_field(
        self,
        type_name,
        field,
        default_content,
        target_locale,
        dnt,
        exclude_tokens,
    ):
        self.calls.append((field, target_locale, default_content))
        return self.translations[(field, target_locale)]


def _live_map():
    return {
        PRODUCT_GID: [
            {
                "key": "title",
                "value": "Decespugliatore FS 120",
                "digest": "title-digest",
                "locale": "it",
            },
            {
                "key": "meta_title",
                "value": "Decespugliatore FS 120 Stihl",
                "digest": "meta-title-digest",
                "locale": "it",
            },
            {
                "key": "meta_description",
                "value": "Decespugliatore professionale Stihl per erba resistente e lavori intensivi.",
                "digest": "meta-description-digest",
                "locale": "it",
            },
        ]
    }


def test_current_seo_is_preserved_without_openai_or_shopify(monkeypatch):
    calls = []
    translator = _FakeTranslator()

    async def _unexpected_register(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(seo, "register_translations", _unexpected_register)
    result = asyncio.run(
        seo.process_product_seo_bundle(
            store=_FakeStore(),
            translator=translator,
            product_id=123,
            product_gid=PRODUCT_GID,
            live_map=_live_map(),
            existing_translations={
                "de": {
                    PRODUCT_GID: {
                        "meta_title": {
                            "value": "Freischneider FS 120 Stihl",
                            "outdated": False,
                        },
                        "meta_description": {
                            "value": "Professioneller Stihl Freischneider für widerstandsfähiges Gras und intensive Arbeiten.",
                            "outdated": False,
                        },
                    }
                }
            },
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
        )
    )

    assert result["status"] == "current"
    assert result["locales"]["de"]["current_fields"] == [
        "meta_title",
        "meta_description",
    ]
    assert translator.calls == []
    assert calls == []


def test_only_missing_seo_field_is_translated_and_registered(monkeypatch):
    calls = []
    store = _FakeStore()
    translator = _FakeTranslator(
        {
            (
                "meta_description",
                "de",
            ): "Professioneller Stihl Freischneider für widerstandsfähiges Gras und intensive Arbeiten.",
        }
    )

    async def _register(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    monkeypatch.setattr(seo, "register_translations", _register)
    result = asyncio.run(
        seo.process_product_seo_bundle(
            store=store,
            translator=translator,
            product_id=123,
            product_gid=PRODUCT_GID,
            live_map=_live_map(),
            existing_translations={
                "de": {
                    PRODUCT_GID: {
                        "meta_title": {
                            "value": "Freischneider FS 120 Stihl",
                            "outdated": False,
                        }
                    }
                }
            },
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
        )
    )

    assert result["status"] == "synced"
    assert translator.calls == [
        (
            "meta_description",
            "de",
            "Decespugliatore professionale Stihl per erba resistente e lavori intensivi.",
        )
    ]
    assert len(store.writes) == 1
    assert calls[0][0] == PRODUCT_GID
    assert calls[0][1] == [
        {
            "key": "meta_description",
            "locale": "de",
            "value": "Professioneller Stihl Freischneider für widerstandsfähiges Gras und intensive Arbeiten.",
            "translatableContentDigest": "meta-description-digest",
        }
    ]


def test_seo_dry_run_only_plans_missing_fields(monkeypatch):
    translator = _FakeTranslator()

    async def _unexpected_register(*args, **kwargs):
        raise AssertionError("dry-run must not register translations")

    monkeypatch.setattr(seo, "register_translations", _unexpected_register)
    result = asyncio.run(
        seo.process_product_seo_bundle(
            store=_FakeStore(),
            translator=translator,
            product_id=123,
            product_gid=PRODUCT_GID,
            live_map=_live_map(),
            existing_translations={"fr": {PRODUCT_GID: {}}},
            target_locales=["fr"],
            source_locale="it",
            apply_translations=True,
            dry_run=True,
        )
    )

    assert result["status"] == "planned"
    assert result["locales"]["fr"]["planned_fields"] == [
        "meta_title",
        "meta_description",
    ]
    assert translator.calls == []


def test_seo_audit_counts_missing_outdated_and_current(monkeypatch):
    async def _list_resources(*, resource_type, first, after):
        assert resource_type == "PRODUCT"
        assert after is None
        return (
            [
                {
                    "resourceId": PRODUCT_GID,
                    "translatableContent": _live_map()[PRODUCT_GID],
                }
            ],
            {"hasNextPage": False, "endCursor": None},
        )

    async def _translations(resource_ids, locale):
        assert resource_ids == [PRODUCT_GID]
        if locale == "de":
            return {
                PRODUCT_GID: {
                    "meta_title": {
                        "value": "Freischneider FS 120 Stihl",
                        "outdated": False,
                    },
                    "meta_description": {"value": "", "outdated": False},
                }
            }
        return {
            PRODUCT_GID: {
                "meta_title": {
                    "value": "Débroussailleuse FS 120 Stihl",
                    "outdated": False,
                },
                "meta_description": {
                    "value": "Débroussailleuse professionnelle Stihl pour herbe résistante et travaux intensifs.",
                    "outdated": True,
                },
            }
        }

    monkeypatch.setattr(seo, "list_translatable_resources", _list_resources)
    monkeypatch.setattr(seo, "get_resource_translations_by_ids", _translations)

    report = asyncio.run(seo.audit_product_seo(target_locales=["de", "fr"]))

    assert report["products_total"] == 1
    assert report["products_with_custom_seo"] == 1
    assert report["source_fields"] == 2
    assert report["candidate_product_ids"] == [123]
    assert report["locales"]["de"]["fields_current"] == 1
    assert report["locales"]["de"]["fields_missing"] == 1
    assert report["locales"]["fr"]["fields_current"] == 1
    assert report["locales"]["fr"]["fields_outdated"] == 1


def test_seo_translation_memory_is_reused(monkeypatch):
    calls = []
    store = _FakeStore()
    source_value = "Decespugliatore FS 120 Stihl"
    store.memory[
        (
            make_source_hash(source_value),
            "product.meta_title",
            "it",
            "de",
        )
    ] = "Freischneider FS 120 Stihl"
    translator = _FakeTranslator()

    async def _register(resource_id, payloads):
        calls.append(payloads)
        return []

    monkeypatch.setattr(seo, "register_translations", _register)
    result = asyncio.run(
        seo.process_product_seo_bundle(
            store=store,
            translator=translator,
            product_id=123,
            product_gid=PRODUCT_GID,
            live_map=_live_map(),
            existing_translations={
                "de": {
                    PRODUCT_GID: {
                        "meta_description": {
                            "value": "Professioneller Stihl Freischneider für widerstandsfähiges Gras und intensive Arbeiten.",
                            "outdated": False,
                        }
                    }
                }
            },
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
        )
    )

    assert result["locales"]["de"]["section_sources"]["meta_title"] == "memory"
    assert translator.calls == []
    assert calls[0][0]["value"] == "Freischneider FS 120 Stihl"

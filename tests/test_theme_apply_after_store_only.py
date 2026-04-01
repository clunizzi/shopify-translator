import asyncio

from src.bootstrap import theme
from src.state.neon import make_source_hash
from src.state.neon import ThemeTranslationState


class _FakeTranslator:
    model = "test-model"


class _FakeStore:
    def __init__(self):
        self.sources = {}
        self.translations = {}
        self.memory = {}

    def ensure_schema(self):
        return None

    def close(self):
        return None

    def get_theme_source_hashes(self, *, shop_domain, theme_id, resource_type, resource_id, source_locale):
        record = self.sources.get((shop_domain, theme_id, resource_type, resource_id, source_locale))
        return dict(record["section_hashes"]) if record else {}

    def upsert_theme_source(self, record):
        self.sources[
            (record.shop_domain, record.theme_id, record.resource_type, record.resource_id, record.source_locale)
        ] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "metadata": dict(record.metadata or {}),
        }

    def get_theme_translation_state(self, *, shop_domain, theme_id, resource_type, resource_id, target_locale):
        record = self.translations.get((shop_domain, theme_id, resource_type, resource_id, target_locale))
        if not record:
            return None
        return ThemeTranslationState(
            document=dict(record["document"]),
            section_hashes=dict(record["section_hashes"]),
            status=record["status"],
            metadata=dict(record["metadata"]),
        )

    def upsert_translation_memory(
        self,
        *,
        source_hash,
        field_key,
        source_locale,
        target_locale,
        source_value,
        translated_value,
        model,
        metadata=None,
    ):
        self.memory[(source_hash, field_key, source_locale, target_locale)] = translated_value

    def upsert_theme_translation(self, record):
        self.translations[
            (record.shop_domain, record.theme_id, record.resource_type, record.resource_id, record.target_locale)
        ] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "status": record.status,
            "metadata": dict(record.metadata or {}),
            "model": record.model,
        }


def test_theme_apply_translations_after_store_only_pushes_even_when_source_is_unchanged(monkeypatch):
    calls = []
    shop_domain = theme.SETTINGS.shopify_domain

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    def _fake_translate_theme_document(**kwargs):
        source_document = kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        changed_sections = kwargs["changed_sections"]
        translated_document = {
            "shop_domain": source_document["shop_domain"],
            "theme_id": source_document["theme_id"],
            "resource_type": source_document["resource_type"],
            "resource_id": source_document["resource_id"],
            "source_locale": source_document["source_locale"],
            "target_locale": target_locale,
            "entries": {},
        }
        translated_hashes = {}
        payloads = []
        section_sources = {}
        for section_name in changed_sections:
            _, key = section_name.split(".", 1)
            entry = source_document["entries"][key]
            translated_value = f"{entry['value']} [{target_locale}]"
            translated_document["entries"][key] = translated_value
            translated_hashes[section_name] = f"translated:{target_locale}:{key}"
            section_sources[section_name] = "translator"
            payloads.append(
                {
                    "resource_id": entry["resource_id"],
                    "key": key,
                    "locale": target_locale,
                    "value": translated_value,
                    "translatableContentDigest": entry["digest"],
                }
            )
        return translated_document, translated_hashes, payloads, section_sources

    monkeypatch.setattr(theme, "register_translations", _fake_register_translations)
    monkeypatch.setattr(theme, "translate_theme_document", _fake_translate_theme_document)
    async def _fake_fetch_theme_source_bundle(**kwargs):
        return [
                {
                    "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                    "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1",
                    "translatableContent": [
                        {"key": "section.home.heading:abc", "value": "Benvenuti", "digest": "d1", "locale": "it"},
                        {"key": "section.home.text:def", "value": "<p>Testo hero</p>", "digest": "d2", "locale": "it"},
                    ],
                }
            ]

    monkeypatch.setattr(theme, "fetch_theme_source_bundle", _fake_fetch_theme_source_bundle)

    store = _FakeStore()

    class _FakeNeonStoreFactory:
        def __call__(self):
            return store

    monkeypatch.setattr(theme, "NeonTranslationStore", _FakeNeonStoreFactory())

    first = asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de", "fr"],
            source_locale="it",
            apply_translations=False,
            dry_run=False,
            resource_types=["ONLINE_STORE_THEME_JSON_TEMPLATE"],
        )
    )
    assert first["items"][-1]["status"] == "translated"
    assert calls == []

    second = asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de", "fr"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            resource_types=["ONLINE_STORE_THEME_JSON_TEMPLATE"],
        )
    )

    assert second["items"][-1]["status"] == "synced"
    assert len(calls) == 2
    assert store.get_theme_translation_state(
        shop_domain=shop_domain,
        theme_id="111",
        resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
        resource_id="gid://shopify/OnlineStoreThemeJsonTemplate/1",
        target_locale="de",
    ).status == "synced"


def test_theme_apply_after_store_only_reuses_stored_translation_state(monkeypatch):
    calls = []

    async def _fake_fetch_theme_source_bundle(**kwargs):
        return [
            {
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1",
                "translatableContent": [
                    {"key": "section.home.heading:abc", "value": "Benvenuti", "digest": "d1", "locale": "it"},
                ],
            }
        ]

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    translate_calls = {"count": 0}

    def _fake_translate_theme_document(**kwargs):
        translate_calls["count"] += 1
        source_document = kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        return (
            {
                "shop_domain": source_document["shop_domain"],
                "theme_id": source_document["theme_id"],
                "resource_type": source_document["resource_type"],
                "resource_id": source_document["resource_id"],
                "source_locale": source_document["source_locale"],
                "target_locale": target_locale,
                "entries": {"section.home.heading:abc": f"Benvenuti [{target_locale}]"},
            },
            {"ONLINE_STORE_THEME_JSON_TEMPLATE.section.home.heading:abc": make_source_hash("Benvenuti")},
            [
                {
                    "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1",
                    "key": "section.home.heading:abc",
                    "locale": target_locale,
                    "value": f"Benvenuti [{target_locale}]",
                    "translatableContentDigest": "d1",
                }
            ],
            {"ONLINE_STORE_THEME_JSON_TEMPLATE.section.home.heading:abc": "translator"},
        )

    monkeypatch.setattr(theme, "fetch_theme_source_bundle", _fake_fetch_theme_source_bundle)
    monkeypatch.setattr(theme, "register_translations", _fake_register_translations)
    monkeypatch.setattr(theme, "translate_theme_document", _fake_translate_theme_document)

    store = _FakeStore()
    monkeypatch.setattr(theme, "NeonTranslationStore", lambda: store)

    asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de"],
            source_locale="it",
            apply_translations=False,
            dry_run=False,
            resource_types=["ONLINE_STORE_THEME_JSON_TEMPLATE"],
        )
    )
    assert translate_calls["count"] == 1

    asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            resource_types=["ONLINE_STORE_THEME_JSON_TEMPLATE"],
        )
    )

    assert translate_calls["count"] == 1
    assert calls == [
        (
            "gid://shopify/OnlineStoreThemeJsonTemplate/1",
            [
                {
                    "key": "section.home.heading:abc",
                    "locale": "de",
                    "value": "Benvenuti [de]",
                    "translatableContentDigest": "d1",
                }
            ],
        )
    ]

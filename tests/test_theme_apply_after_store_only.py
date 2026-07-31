import asyncio

from src.bootstrap import theme
from src.state.neon import ThemeTranslationState, make_source_hash


class _FakeTranslator:
    model = "test-model"


async def _missing_remote_translations(resource_ids, _locale):
    return {resource_id: {} for resource_id in resource_ids}


class _FakeStore:
    def __init__(self):
        self.sources = {}
        self.translations = {}
        self.memory = {}

    def ensure_schema(self):
        return None

    def close(self):
        return None

    def get_theme_source_hashes(
        self, *, shop_domain, theme_id, resource_type, resource_id, source_locale
    ):
        record = self.sources.get(
            (shop_domain, theme_id, resource_type, resource_id, source_locale)
        )
        return dict(record["section_hashes"]) if record else {}

    def upsert_theme_source(self, record):
        self.sources[
            (
                record.shop_domain,
                record.theme_id,
                record.resource_type,
                record.resource_id,
                record.source_locale,
            )
        ] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "metadata": dict(record.metadata or {}),
        }

    def get_theme_translation_state(
        self, *, shop_domain, theme_id, resource_type, resource_id, target_locale
    ):
        record = self.translations.get(
            (shop_domain, theme_id, resource_type, resource_id, target_locale)
        )
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
            (
                record.shop_domain,
                record.theme_id,
                record.resource_type,
                record.resource_id,
                record.target_locale,
            )
        ] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "status": record.status,
            "metadata": dict(record.metadata or {}),
            "model": record.model,
        }


def test_theme_apply_translations_after_store_only_pushes_even_when_source_is_unchanged(
    monkeypatch,
):
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
    monkeypatch.setattr(
        theme,
        "get_resource_translations_by_ids",
        _missing_remote_translations,
    )
    monkeypatch.setattr(theme, "translate_theme_document", _fake_translate_theme_document)

    async def _fake_fetch_theme_source_bundle(**kwargs):
        return [
            {
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
                "translatableContent": [
                    {
                        "key": "section.home.heading:abc",
                        "value": "Benvenuti",
                        "digest": "d1",
                        "locale": "it",
                    },
                    {
                        "key": "section.home.text:def",
                        "value": "<p>Testo hero</p>",
                        "digest": "d2",
                        "locale": "it",
                    },
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
            require_main_theme=False,
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
            require_main_theme=False,
        )
    )

    assert second["items"][-1]["status"] == "synced"
    assert len(calls) == 2
    assert (
        store.get_theme_translation_state(
            shop_domain=shop_domain,
            theme_id="111",
            resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
            resource_id="gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
            target_locale="de",
        ).status
        == "synced"
    )


def test_theme_apply_after_store_only_reuses_stored_translation_state(monkeypatch):
    calls = []

    async def _fake_fetch_theme_source_bundle(**kwargs):
        return [
            {
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
                "translatableContent": [
                    {
                        "key": "section.home.heading:abc",
                        "value": "Benvenuti",
                        "digest": "d1",
                        "locale": "it",
                    },
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
            {
                "ONLINE_STORE_THEME_JSON_TEMPLATE.section.home.heading:abc": make_source_hash(
                    "Benvenuti"
                )
            },
            [
                {
                    "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
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
    monkeypatch.setattr(
        theme,
        "get_resource_translations_by_ids",
        _missing_remote_translations,
    )
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
            require_main_theme=False,
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
            require_main_theme=False,
        )
    )

    assert translate_calls["count"] == 1
    assert calls == [
        (
            "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
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


def test_theme_apply_preserves_current_shopify_translation(monkeypatch):
    resource_id = "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111"
    calls = []

    async def _bundle(**_kwargs):
        return [
            {
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": resource_id,
                "translatableContent": [
                    {
                        "key": "section.home.heading:abc",
                        "value": "Benvenuti",
                        "digest": "d1",
                        "locale": "it",
                    }
                ],
            }
        ]

    async def _remote(_resource_ids, _locale):
        return {
            resource_id: {
                "section.home.heading:abc": {
                    "value": "Willkommen",
                    "outdated": False,
                }
            }
        }

    async def _register(*args):
        calls.append(args)
        return []

    monkeypatch.setattr(theme, "fetch_theme_source_bundle", _bundle)
    monkeypatch.setattr(theme, "get_resource_translations_by_ids", _remote)
    monkeypatch.setattr(theme, "register_translations", _register)
    monkeypatch.setattr(theme, "NeonTranslationStore", lambda: _FakeStore())

    out = asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            require_main_theme=False,
        )
    )

    assert calls == []
    assert out["registered"] == 0
    assert out["items"][0]["status"] == "unchanged"

    def _forced_translation(**kwargs):
        source_document = kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        section_name = next(iter(kwargs["changed_sections"]))
        key = section_name.split(".", 1)[1]
        return (
            {
                "shop_domain": source_document["shop_domain"],
                "theme_id": source_document["theme_id"],
                "resource_type": source_document["resource_type"],
                "resource_id": source_document["resource_id"],
                "source_locale": source_document["source_locale"],
                "target_locale": target_locale,
                "entries": {key: "Neu"},
            },
            {section_name: make_source_hash("Benvenuti")},
            [
                {
                    "resource_id": resource_id,
                    "key": key,
                    "locale": target_locale,
                    "value": "Neu",
                    "translatableContentDigest": "d1",
                }
            ],
            {section_name: "translator"},
        )

    monkeypatch.setattr(theme, "translate_theme_document", _forced_translation)
    forced = asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            require_main_theme=False,
            force_key_fragments=["heading"],
        )
    )

    assert forced["registered"] == 1
    assert len(calls) == 1


def test_theme_dry_run_does_not_persist_or_register(monkeypatch):
    resource_id = "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111"
    store = _FakeStore()

    async def _bundle(**_kwargs):
        return [
            {
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": resource_id,
                "translatableContent": [
                    {
                        "key": "section.home.heading:abc",
                        "value": "Benvenuti",
                        "digest": "d1",
                        "locale": "it",
                    }
                ],
            }
        ]

    async def _unexpected_register(*_args):
        raise AssertionError("dry-run must not call translationsRegister")

    monkeypatch.setattr(theme, "fetch_theme_source_bundle", _bundle)
    monkeypatch.setattr(
        theme,
        "get_resource_translations_by_ids",
        _missing_remote_translations,
    )
    monkeypatch.setattr(theme, "register_translations", _unexpected_register)
    monkeypatch.setattr(theme, "NeonTranslationStore", lambda: store)

    out = asyncio.run(
        theme.bootstrap_theme(
            theme_id="111",
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=True,
            require_main_theme=False,
            max_translations=1,
        )
    )

    assert out["would_register"] == 1
    assert out["registered"] == 0
    assert out["state_persisted"] is False
    assert store.sources == {}
    assert store.translations == {}
    assert store.memory == {}


def test_theme_audit_is_read_only_and_reports_missing_and_outdated(monkeypatch):
    resource_id = "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111"

    async def _main(_theme_id):
        return {
            "id": "gid://shopify/OnlineStoreTheme/111",
            "name": "Example Store Live",
            "role": "MAIN",
            "updated_at": "now",
        }

    async def _bundle(**_kwargs):
        return [
            {
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": resource_id,
                "translatableContent": [
                    {
                        "key": "section.home.heading:abc",
                        "value": "Benvenuti",
                        "digest": "d1",
                        "locale": "it",
                    },
                    {
                        "key": "section.home.text:def",
                        "value": "Testo",
                        "digest": "d2",
                        "locale": "it",
                    },
                ],
            }
        ]

    async def _remote(_resource_ids, _locale):
        return {
            resource_id: {
                "section.home.heading:abc": {
                    "value": "Willkommen",
                    "outdated": True,
                }
            }
        }

    monkeypatch.setattr(theme, "assert_approved_main_theme", _main)
    monkeypatch.setattr(theme, "fetch_theme_source_bundle", _bundle)
    monkeypatch.setattr(theme, "get_resource_translations_by_ids", _remote)
    monkeypatch.setattr(
        theme,
        "NeonTranslationStore",
        lambda: (_ for _ in ()).throw(AssertionError("audit must not open Neon")),
    )

    out = asyncio.run(
        theme.audit_theme_translations(
            theme_id="111",
            target_locales=["de"],
            source_locale="it",
            include_items=True,
        )
    )

    assert out["read_only"] is True
    assert out["locales"]["de"]["missing"] == 1
    assert out["locales"]["de"]["outdated"] == 1
    assert len(out["locales"]["de"]["items"]) == 2


def test_theme_translation_reuses_same_semantic_shopify_value():
    class _UnexpectedTranslator:
        model = "test-model"

        def translate_plain(self, *_args, **_kwargs):
            raise AssertionError("a current equivalent Shopify translation must be reused")

    resource_id = "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111"
    source_document = {
        "shop_domain": theme.SETTINGS.shopify_domain,
        "theme_id": "111",
        "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
        "resource_id": resource_id,
        "source_locale": "it",
        "entries": {
            "section.page.one.expiry_text:a": {
                "resource_id": resource_id,
                "key": "section.page.one.expiry_text:a",
                "value": "9 agosto",
                "digest": "d1",
                "locale": "it",
                "content_kind": "plain",
            }
        },
    }
    section_name = "ONLINE_STORE_THEME_JSON_TEMPLATE.section.page.one.expiry_text:a"
    reuse = {
        ("de", "expiry_text", "plain", "9 agosto"): "9. August",
    }

    translated, _hashes, payloads, sources = theme.translate_theme_document(
        source_document=source_document,
        changed_sections={section_name},
        target_locale="de",
        translator=_UnexpectedTranslator(),
        translation_reuse=reuse,
    )

    assert translated["entries"]["section.page.one.expiry_text:a"] == "9. August"
    assert payloads[0]["value"] == "9. August"
    assert sources[section_name] == "current_shopify_translation_reuse"

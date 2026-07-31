import asyncio

from src.bootstrap import theme
from src.state.neon import ThemeTranslationState


class _FakeStore:
    def __init__(self):
        self.sources = {}
        self.translations = {}

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

    def upsert_translation_memory(self, **kwargs):
        return None

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
            "document": dict(record.document),
            "section_hashes": dict(record.section_hashes),
            "status": record.status,
            "metadata": dict(record.metadata or {}),
        }


def test_theme_bootstrap_marks_failed_when_register_returns_graphql_errors(monkeypatch):
    store = _FakeStore()

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
        return [{"field": ["translationsRegister"], "message": "invalid value"}]

    def _fake_translate_theme_document(**kwargs):
        return (
            {
                "shop_domain": theme.SETTINGS.shopify_domain,
                "theme_id": "111",
                "resource_type": "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
                "source_locale": "it",
                "target_locale": kwargs["target_locale"],
                "entries": {"section.home.heading:abc": f"Willkommen [{kwargs['target_locale']}]"},
            },
            {"ONLINE_STORE_THEME_JSON_TEMPLATE.section.home.heading:abc": "h1"},
            [
                {
                    "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
                    "key": "section.home.heading:abc",
                    "locale": kwargs["target_locale"],
                    "value": f"Willkommen [{kwargs['target_locale']}]",
                    "translatableContentDigest": "d1",
                }
            ],
            {"ONLINE_STORE_THEME_JSON_TEMPLATE.section.home.heading:abc": "translator"},
        )

    monkeypatch.setattr(theme, "fetch_theme_source_bundle", _fake_fetch_theme_source_bundle)
    monkeypatch.setattr(theme, "register_translations", _fake_register_translations)

    async def _missing_remote_translations(resource_ids, _locale):
        return {resource_id: {} for resource_id in resource_ids}

    monkeypatch.setattr(
        theme,
        "get_resource_translations_by_ids",
        _missing_remote_translations,
    )
    monkeypatch.setattr(theme, "translate_theme_document", _fake_translate_theme_document)
    monkeypatch.setattr(theme, "NeonTranslationStore", lambda: store)

    result = asyncio.run(
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

    assert result["items"][0]["status"] == "failed"
    assert (
        store.get_theme_translation_state(
            shop_domain=theme.SETTINGS.shopify_domain,
            theme_id="111",
            resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
            resource_id="gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
            target_locale="de",
        ).status
        == "failed"
    )

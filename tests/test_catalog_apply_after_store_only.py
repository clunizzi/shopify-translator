import asyncio

from src.bootstrap import catalog
from src.state.neon import PDPTranslationState


class _FakeTranslator:
    model = "test-model"


class _FakeStore:
    def __init__(self):
        self.sources = {}
        self.translations = {}
        self.memory = {}

    def has_pdp_source(self, *, shop_domain, product_gid, source_locale):
        return (shop_domain, product_gid, source_locale) in self.sources

    def get_pdp_source_hashes(self, *, shop_domain, product_gid, source_locale):
        record = self.sources.get((shop_domain, product_gid, source_locale))
        return dict(record["section_hashes"]) if record else {}

    def upsert_pdp_source(self, record):
        self.sources[(record.shop_domain, record.product_gid, record.source_locale)] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "metadata": dict(record.metadata or {}),
        }

    def get_pdp_translation_state(self, *, shop_domain, product_gid, target_locale):
        record = self.translations.get((shop_domain, product_gid, target_locale))
        if not record:
            return None
        return PDPTranslationState(
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

    def upsert_pdp_translation(self, record):
        self.translations[(record.shop_domain, record.product_gid, record.target_locale)] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "status": record.status,
            "metadata": dict(record.metadata or {}),
            "model": record.model,
        }


def test_apply_translations_after_store_only_pushes_even_when_source_is_unchanged(monkeypatch):
    calls = []
    shop_domain = catalog.SETTINGS.shopify_domain

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    def _fake_translate_pdp_document(**kwargs):
        source_document = kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        changed_sections = kwargs["changed_sections"]
        translated_document = {"product": {}, "metafields": {}}
        translated_hashes = {}
        payloads = []
        section_sources = {}
        resource_id = source_document["product"]["title"]["resource_id"]
        for section_name in changed_sections:
            if not section_name.startswith("product."):
                continue
            key = section_name.split(".", 1)[1]
            source_value = source_document["product"][key]["value"]
            translated_value = f"{source_value} [{target_locale}]"
            translated_document["product"][key] = translated_value
            translated_hashes[section_name] = f"translated:{target_locale}:{key}"
            section_sources[section_name] = "translator"
            payloads.append(
                {
                    "resource_id": resource_id,
                    "key": key,
                    "locale": target_locale,
                    "value": translated_value,
                    "translatableContentDigest": source_document["product"][key]["digest"],
                }
            )
        return translated_document, translated_hashes, payloads, section_sources

    monkeypatch.setattr(catalog, "register_translations", _fake_register_translations)
    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)

    store = _FakeStore()
    summary = {"products": 0, "changed_products": 0, "changed_sections": 0, "registered": 0, "items": []}
    common_kwargs = dict(
        store=store,
        translator=_FakeTranslator(),
        product_id=123,
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Motozappa Honda", "digest": "d1", "locale": "it"},
                {"key": "body_html", "value": "<p>Test</p>", "digest": "d2", "locale": "it"},
            ]
        },
        existing_translations={"de": {}, "fr": {}},
        target_locales=["de", "fr"],
        source_locale="it",
        dry_run=False,
        existing_products=True,
        is_create=False,
        summary=summary,
    )

    first = asyncio.run(
        catalog.process_product_bundle(
            apply_translations=False,
            **common_kwargs,
        )
    )
    assert first["items"][-1]["status"] == "translated"
    assert calls == []

    second = asyncio.run(
        catalog.process_product_bundle(
            apply_translations=True,
            **common_kwargs,
        )
    )

    assert second["items"][-1]["status"] == "synced"
    assert len(calls) == 2
    assert all(call[0] == "gid://shopify/Product/123" for call in calls)
    assert store.get_pdp_translation_state(
        shop_domain=shop_domain,
        product_gid="gid://shopify/Product/123",
        target_locale="de",
    ).status == "synced"

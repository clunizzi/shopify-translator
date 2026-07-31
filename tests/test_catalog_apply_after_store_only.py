import asyncio

from src.bootstrap import catalog
from src.state.neon import PDPTranslationState, make_source_hash
from src.translate.cache import TranslationCache
from src.translate.translator import Translator


class _FakeTranslator:
    model = "test-model"


class _FakeStore:
    def __init__(self):
        self.sources = {}
        self.translations = {}
        self.memory = {}
        self.source_writes = 0

    def has_pdp_source(self, *, shop_domain, product_gid, source_locale):
        return (shop_domain, product_gid, source_locale) in self.sources

    def get_pdp_source_hashes(self, *, shop_domain, product_gid, source_locale):
        record = self.sources.get((shop_domain, product_gid, source_locale))
        return dict(record["section_hashes"]) if record else {}

    def upsert_pdp_source(self, record):
        self.source_writes += 1
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

    def get_translation_memory(self, *, source_hash, field_key, source_locale, target_locale):
        return self.memory.get((source_hash, field_key, source_locale, target_locale))

    def upsert_pdp_translation(self, record):
        self.translations[(record.shop_domain, record.product_gid, record.target_locale)] = {
            "document": record.document,
            "section_hashes": dict(record.section_hashes),
            "status": record.status,
            "metadata": dict(record.metadata or {}),
            "model": record.model,
        }


def test_content_only_event_skips_inventory_noise_without_repairing_drift(monkeypatch):
    calls = []
    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    product_gid = "gid://shopify/Product/123"
    source_hash = make_source_hash("Motozappa Honda")

    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={"product.title": source_hash},
            metadata={},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="de",
            document={"product": {"title": "Honda-Motorhacke"}, "metafields": {}, "options": {}},
            section_hashes={"product.title": source_hash},
            status="translated",
            model="test-model",
            metadata={},
        )
    )
    source_writes_before = store.source_writes

    async def _unexpected_register(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(catalog, "register_translations", _unexpected_register)

    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {
                        "key": "title",
                        "value": "Motozappa Honda",
                        "digest": "new-digest-from-inventory-event",
                        "locale": "it",
                    }
                ]
            },
            existing_translations={"de": {}},
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            content_changes_only=True,
        )
    )

    item = result["items"][-1]
    assert item["status"] == "unchanged"
    assert item["skip_reason"] == "no_translatable_content_change"
    assert item["changed_sections"] == []
    assert calls == []
    assert store.source_writes == source_writes_before
    assert (
        store.get_pdp_translation_state(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="de",
        ).status
        == "translated"
    )


def test_content_only_event_translates_only_the_changed_section(monkeypatch):
    calls = []
    translated_requests = []
    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    product_gid = "gid://shopify/Product/123"
    old_title_hash = make_source_hash("Motozappa Honda")
    body_hash = make_source_hash("<p>Descrizione invariata</p>")

    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={
                "product.title": old_title_hash,
                "product.body_html": body_hash,
            },
            metadata={},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="de",
            document={"product": {"title": "Honda-Motorhacke"}, "metafields": {}, "options": {}},
            section_hashes={"product.title": old_title_hash},
            status="translated",
            model="test-model",
            metadata={},
        )
    )

    def _fake_translate_pdp_document(**kwargs):
        changed_sections = set(kwargs["changed_sections"])
        translated_requests.append(changed_sections)
        assert changed_sections == {"product.title"}
        return (
            {"product": {"title": "Honda-Motorhacke Pro"}, "metafields": {}, "options": {}},
            {"product.title": make_source_hash("Motozappa Honda Pro")},
            [
                {
                    "resource_id": product_gid,
                    "key": "title",
                    "locale": "de",
                    "value": "Honda-Motorhacke Pro",
                    "translatableContentDigest": "title-digest",
                }
            ],
            {"product.title": "translator"},
        )

    async def _fake_register(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)
    monkeypatch.setattr(catalog, "register_translations", _fake_register)

    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {
                        "key": "title",
                        "value": "Motozappa Honda Pro",
                        "digest": "title-digest",
                        "locale": "it",
                    },
                    {
                        "key": "body_html",
                        "value": "<p>Descrizione invariata</p>",
                        "digest": "body-digest",
                        "locale": "it",
                    },
                ]
            },
            existing_translations={"de": {}},
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            content_changes_only=True,
        )
    )

    item = result["items"][-1]
    assert item["changed_sections"] == ["product.title"]
    assert translated_requests == [{"product.title"}]
    assert len(calls) == 1
    assert calls[0][0] == product_gid
    assert calls[0][1][0]["key"] == "title"


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
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }
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
    assert (
        store.get_pdp_translation_state(
            shop_domain=shop_domain,
            product_gid="gid://shopify/Product/123",
            target_locale="de",
        ).status
        == "synced"
    )


def test_apply_after_store_only_reuses_stored_product_translation(monkeypatch):
    calls = []
    translate_calls = {"count": 0}

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    def _fake_translate_pdp_document(**kwargs):
        translate_calls["count"] += 1
        kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        translated_document = {
            "product": {"title": f"Motorhacke Honda [{target_locale}]"},
            "metafields": {},
            "options": {},
        }
        return (
            translated_document,
            {"product.title": make_source_hash("Motozappa Honda")},
            [
                {
                    "resource_id": "gid://shopify/Product/123",
                    "key": "title",
                    "locale": target_locale,
                    "value": f"Motorhacke Honda [{target_locale}]",
                    "translatableContentDigest": "d1",
                }
            ],
            {"product.title": "translator"},
        )

    monkeypatch.setattr(catalog, "register_translations", _fake_register_translations)
    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)

    store = _FakeStore()
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }
    common_kwargs = dict(
        store=store,
        translator=_FakeTranslator(),
        product_id=123,
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Motozappa Honda", "digest": "d1", "locale": "it"},
            ]
        },
        existing_translations={"de": {}},
        target_locales=["de"],
        source_locale="it",
        dry_run=False,
        existing_products=True,
        is_create=False,
        summary=summary,
    )

    asyncio.run(catalog.process_product_bundle(apply_translations=False, **common_kwargs))
    assert translate_calls["count"] == 1
    asyncio.run(catalog.process_product_bundle(apply_translations=True, **common_kwargs))
    assert translate_calls["count"] == 1
    assert calls == [
        (
            "gid://shopify/Product/123",
            [
                {
                    "key": "title",
                    "locale": "de",
                    "value": "Motorhacke Honda [de]",
                    "translatableContentDigest": "d1",
                }
            ],
        )
    ]


def test_store_only_completes_only_missing_locales_when_source_is_unchanged(monkeypatch):
    translate_calls: list[str] = []

    def _fake_translate_pdp_document(**kwargs):
        target_locale = kwargs["target_locale"]
        translate_calls.append(target_locale)
        return (
            {
                "product": {"title": f"Motozappa Honda [{target_locale}]"},
                "metafields": {},
                "options": {},
            },
            {"product.title": make_source_hash("Motozappa Honda")},
            [
                {
                    "resource_id": "gid://shopify/Product/123",
                    "key": "title",
                    "locale": target_locale,
                    "value": f"Motozappa Honda [{target_locale}]",
                    "translatableContentDigest": "d1",
                }
            ],
            {"product.title": "translator"},
        )

    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)

    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid="gid://shopify/Product/123",
            source_locale="it",
            document={},
            section_hashes={"product.title": make_source_hash("Motozappa Honda")},
            metadata={"product_id": 123},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid="gid://shopify/Product/123",
            target_locale="de",
            document={
                "product": {"title": "Motozappa Honda [de]"},
                "metafields": {},
                "options": {},
            },
            section_hashes={"product.title": make_source_hash("Motozappa Honda")},
            status="translated",
            model="test-model",
            metadata={},
        )
    )
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }
    common_kwargs = dict(
        store=store,
        translator=_FakeTranslator(),
        product_id=123,
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Motozappa Honda", "digest": "d1", "locale": "it"},
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

    first = asyncio.run(catalog.process_product_bundle(apply_translations=False, **common_kwargs))
    assert translate_calls == ["fr"]
    assert first["items"][-1]["locales"]["de"]["status"] == "translated"
    assert first["items"][-1]["locales"]["fr"]["status"] == "translated"

    second = asyncio.run(catalog.process_product_bundle(apply_translations=False, **common_kwargs))
    assert translate_calls == ["fr"]
    assert second["items"][-1]["status"] == "unchanged"


def test_build_payloads_from_stored_translation_uses_value_key_for_metafields():
    source_document = {
        "product_gid": "gid://shopify/Product/123",
        "shop_domain": "example-store.myshopify.com",
        "source_locale": "it",
        "target_locale": "de",
        "product": {},
        "metafields": {
            "custom.technical_datas": {
                "key": "custom.technical_datas",
                "resource_id": "gid://shopify/Metafield/1",
                "value": '{"foo":"bar"}',
                "digest": "d1",
            }
        },
        "options": {},
    }
    stored_document = {
        "product": {},
        "metafields": {
            "custom.technical_datas": '{"foo":"baz"}',
        },
        "options": {},
    }

    reused = catalog.build_pdp_payloads_from_stored_translation(
        source_document=source_document,
        stored_document=stored_document,
        target_locale="de",
        changed_sections={"metafield.custom.technical_datas"},
    )

    assert reused is not None
    _, _, payloads, section_sources = reused
    assert payloads == [
        {
            "resource_id": "gid://shopify/Metafield/1",
            "key": "value",
            "locale": "de",
            "value": '{"foo":"baz"}',
            "translatableContentDigest": "d1",
        }
    ]
    assert section_sources["metafield.custom.technical_datas"] == "stored_translation_state"


def test_partial_update_preserves_existing_translation_state(monkeypatch):
    async def _fake_register_translations(resource_id, payloads):
        return []

    def _fake_translate_pdp_document(**kwargs):
        kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        changed_sections = kwargs["changed_sections"]
        assert changed_sections == {"product.body_html"}
        return (
            {
                "product": {"body_html": f"<p>Corps mis a jour [{target_locale}]</p>"},
                "metafields": {},
                "options": {},
            },
            {"product.body_html": make_source_hash("<p>Corpo aggiornato</p>")},
            [
                {
                    "resource_id": "gid://shopify/Product/123",
                    "key": "body_html",
                    "locale": target_locale,
                    "value": f"<p>Corps mis a jour [{target_locale}]</p>",
                    "translatableContentDigest": "d2",
                }
            ],
            {"product.body_html": "translator_html_document"},
        )

    monkeypatch.setattr(catalog, "register_translations", _fake_register_translations)
    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)

    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    product_gid = "gid://shopify/Product/123"
    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={
                "product.title": make_source_hash("Titolo"),
                "product.body_html": make_source_hash("<p>Corpo vecchio</p>"),
            },
            metadata={"product_id": 123},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="fr",
            document={
                "product": {
                    "title": "Titre",
                    "body_html": "<p>Corps ancien</p>",
                },
                "metafields": {},
                "options": {},
            },
            section_hashes={
                "product.title": make_source_hash("Titolo"),
                "product.body_html": make_source_hash("<p>Corpo vecchio</p>"),
            },
            status="synced",
            model="test-model",
            metadata={},
        )
    )
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }

    asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                    {
                        "key": "body_html",
                        "value": "<p>Corpo aggiornato</p>",
                        "digest": "d2",
                        "locale": "it",
                    },
                ]
            },
            existing_translations={"fr": {}},
            target_locales=["fr"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            summary=summary,
        )
    )

    state = store.get_pdp_translation_state(
        shop_domain=shop_domain,
        product_gid=product_gid,
        target_locale="fr",
    )
    assert state.status == "synced"
    assert state.document["product"]["title"] == "Titre"
    assert state.document["product"]["body_html"] == "<p>Corps mis a jour [fr]</p>"
    assert state.section_hashes["product.title"] == make_source_hash("Titolo")
    assert state.section_hashes["product.body_html"] == make_source_hash("<p>Corpo aggiornato</p>")


def test_apply_translations_rebuilds_missing_stored_sections_even_when_synced(monkeypatch):
    calls = []
    translate_calls = {"count": 0}

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    def _fake_translate_pdp_document(**kwargs):
        translate_calls["count"] += 1
        source_document = kwargs["source_document"]
        target_locale = kwargs["target_locale"]
        changed_sections = kwargs["changed_sections"]
        assert changed_sections == {"product.title"}
        source_value = source_document["product"]["title"]["value"]
        return (
            {
                "product": {"title": f"{source_value} [{target_locale}]"},
                "metafields": {},
                "options": {},
            },
            {"product.title": make_source_hash(source_value)},
            [
                {
                    "resource_id": "gid://shopify/Product/123",
                    "key": "title",
                    "locale": target_locale,
                    "value": f"{source_value} [{target_locale}]",
                    "translatableContentDigest": "d1",
                }
            ],
            {"product.title": "translator"},
        )

    monkeypatch.setattr(catalog, "register_translations", _fake_register_translations)
    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)

    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    product_gid = "gid://shopify/Product/123"
    source_title = "Grillo - Max3 Motocoltivatore Fresa 68 cm inclusa"
    source_hash = make_source_hash(source_title)
    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={"product.title": source_hash},
            metadata={"product_id": 123},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="de",
            document={"product": {}, "metafields": {}, "options": {}},
            section_hashes={},
            status="synced",
            model="test-model",
            metadata={},
        )
    )
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }

    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": source_title, "digest": "d1", "locale": "it"},
                ]
            },
            existing_translations={"de": {}},
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            summary=summary,
        )
    )

    assert result["items"][-1]["status"] == "synced"
    assert translate_calls["count"] == 1
    assert calls == [
        (
            product_gid,
            [
                {
                    "key": "title",
                    "locale": "de",
                    "value": f"{source_title} [de]",
                    "translatableContentDigest": "d1",
                }
            ],
        )
    ]


def test_apply_translations_reuses_stored_sections_for_failed_locale(monkeypatch):
    calls = []
    translate_calls = {"count": 0}

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    def _fake_translate_pdp_document(**kwargs):
        translate_calls["count"] += 1
        assert kwargs["changed_sections"] == {"product.title"}
        return (
            {
                "product": {"title": "Titolo [de]"},
                "metafields": {},
                "options": {},
            },
            {"product.title": make_source_hash("Titolo")},
            [
                {
                    "resource_id": "gid://shopify/Product/123",
                    "key": "title",
                    "locale": "de",
                    "value": "Titolo [de]",
                    "translatableContentDigest": "d1",
                }
            ],
            {"product.title": "translator"},
        )

    monkeypatch.setattr(catalog, "register_translations", _fake_register_translations)
    monkeypatch.setattr(catalog, "translate_pdp_document", _fake_translate_pdp_document)

    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    product_gid = "gid://shopify/Product/123"
    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={
                "product.title": make_source_hash("Titolo"),
                "product.body_html": make_source_hash("<p>Corpo</p>"),
            },
            metadata={"product_id": 123},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="de",
            document={
                "product": {"body_html": "<p>Body [de]</p>"},
                "metafields": {},
                "options": {},
            },
            section_hashes={"product.body_html": make_source_hash("<p>Corpo</p>")},
            status="failed",
            model="test-model",
            metadata={},
        )
    )

    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }
    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                    {"key": "body_html", "value": "<p>Corpo</p>", "digest": "d2", "locale": "it"},
                ]
            },
            existing_translations={"de": {}},
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            summary=summary,
        )
    )

    assert result["items"][-1]["status"] == "synced"
    assert translate_calls["count"] == 1
    assert calls == [
        (
            product_gid,
            [
                {
                    "key": "title",
                    "locale": "de",
                    "value": "Titolo [de]",
                    "translatableContentDigest": "d1",
                },
                {
                    "key": "body_html",
                    "locale": "de",
                    "value": "<p>Body [de]</p>",
                    "translatableContentDigest": "d2",
                },
            ],
        )
    ]


def test_product_title_uses_neon_memory_before_translator(monkeypatch):
    def _raise_translate_plain(*args, **kwargs):
        raise AssertionError("translate_plain should not be called when memory is available")

    monkeypatch.setattr(catalog.Translator, "translate_plain", _raise_translate_plain)

    store = _FakeStore()
    store.upsert_translation_memory(
        source_hash=make_source_hash("Motozappa Honda"),
        field_key="product.title",
        source_locale="it",
        target_locale="de",
        source_value="Motozappa Honda",
        translated_value="Motorhacke Honda",
        model="test-model",
        metadata={},
    )

    translated, source_kind = catalog._translate_product_title(
        store,
        _FakeTranslator(),
        "Motozappa Honda",
        source_locale="it",
        target_locale="de",
        dnt=catalog.load_do_not_translate(None),
        exclude_tokens=[],
    )

    assert translated == "Motorhacke Honda"
    assert source_kind == "memory"


def test_product_title_rejects_italian_memory_and_falls_back_to_translator(monkeypatch):
    class _MemoryFallbackTranslator(_FakeTranslator):
        def translate_plain(
            self, type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
        ):
            assert type_name == "PRODUCT"
            assert field == "title"
            assert target_locale == "de"
            return "Motorhacke Honda"

    store = _FakeStore()
    store.upsert_translation_memory(
        source_hash=make_source_hash("Grillo - Max3 Motocoltivatore Fresa 68 cm inclusa"),
        field_key="product.title",
        source_locale="it",
        target_locale="de",
        source_value="Grillo - Max3 Motocoltivatore Fresa 68 cm inclusa",
        translated_value="Grillo - Max3 Motocoltivatore Fresa 68 cm inclusa",
        model="test-model",
        metadata={},
    )

    translated, source_kind = catalog._translate_product_title(
        store,
        _MemoryFallbackTranslator(),
        "Grillo - Max3 Motocoltivatore Fresa 68 cm inclusa",
        source_locale="it",
        target_locale="de",
        dnt=catalog.load_do_not_translate(None),
        exclude_tokens=[],
    )

    assert translated == "Motorhacke Honda"
    assert source_kind == "translator"


def test_build_pdp_document_includes_option_names_and_values():
    document, section_hashes = catalog.build_pdp_document(
        shop_domain="example-store.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/ProductOption/1": [
                {"key": "name", "value": "Colore", "digest": "d1", "locale": "it"}
            ],
            "gid://shopify/ProductOptionValue/10": [
                {"key": "name", "value": "Rosso", "digest": "d2", "locale": "it"}
            ],
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
    )

    assert document["options"]["option_name::gid://shopify/ProductOption/1"]["value"] == "Colore"
    assert (
        document["options"]["option_value::gid://shopify/ProductOptionValue/10"]["value"] == "Rosso"
    )
    assert "option.option_name::gid://shopify/ProductOption/1" in section_hashes
    assert "option.option_value::gid://shopify/ProductOptionValue/10" in section_hashes


def test_build_pdp_document_skips_default_title_and_title_option():
    document, section_hashes = catalog.build_pdp_document(
        shop_domain="example-store.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/ProductOption/1": [
                {"key": "name", "value": "Title", "digest": "d1", "locale": "it"}
            ],
            "gid://shopify/ProductOptionValue/10": [
                {"key": "name", "value": "Default Title", "digest": "d2", "locale": "it"}
            ],
            "gid://shopify/ProductOptionValue/11": [
                {"key": "name", "value": "Rosso", "digest": "d3", "locale": "it"}
            ],
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
    )

    assert "option_name::gid://shopify/ProductOption/1" not in document["options"]
    assert "option_value::gid://shopify/ProductOptionValue/10" not in document["options"]
    assert (
        document["options"]["option_value::gid://shopify/ProductOptionValue/11"]["value"] == "Rosso"
    )
    assert "option.option_value::gid://shopify/ProductOptionValue/10" not in section_hashes


def test_build_pdp_document_skips_unit_only_option_values(tmp_path, monkeypatch):
    dnt_path = tmp_path / "dnt.yaml"
    dnt_path.write_text("units:\\n  - L\\n", encoding="utf-8")

    document, section_hashes = catalog.build_pdp_document(
        shop_domain="example-store.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/ProductOptionValue/10": [
                {"key": "name", "value": "1L", "digest": "d1", "locale": "it"}
            ],
            "gid://shopify/ProductOptionValue/11": [
                {"key": "name", "value": "Quantità", "digest": "d2", "locale": "it"}
            ],
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
        units=["L"],
    )

    assert "option_value::gid://shopify/ProductOptionValue/10" not in document["options"]
    assert "option_value::gid://shopify/ProductOptionValue/11" in document["options"]
    assert "option.option_value::gid://shopify/ProductOptionValue/10" not in section_hashes


def test_build_pdp_document_skips_empty_product_type():
    document, section_hashes = catalog.build_pdp_document(
        shop_domain="example-store.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                {"key": "product_type", "value": "", "digest": "d2", "locale": "it"},
            ]
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
    )

    assert "product_type" not in document["product"]
    assert "product.product_type" not in section_hashes


def test_build_pdp_document_skips_all_blank_source_values():
    metafield_id = "gid://shopify/Metafield/1"
    document, section_hashes = catalog.build_pdp_document(
        shop_domain="example-store.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[
            {
                "id": metafield_id,
                "namespace": "custom",
                "key": "in_breve",
                "type": "multi_line_text_field",
            }
        ],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                {"key": "body_html", "value": "  ", "digest": "d2", "locale": "it"},
            ],
            metafield_id: [
                {"key": "value", "value": "\n", "digest": "d3", "locale": "it"},
            ],
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
    )

    assert set(document["product"]) == {"title"}
    assert document["metafields"] == {}
    assert set(section_hashes) == {"product.title"}


def test_blank_stored_translation_is_missing_and_never_reused():
    source_document = {
        "product_gid": "gid://shopify/Product/123",
        "shop_domain": "example-store.myshopify.com",
        "source_locale": "it",
        "product": {
            "title": {
                "resource_id": "gid://shopify/Product/123",
                "key": "title",
                "value": "Titolo",
                "digest": "d1",
            }
        },
        "metafields": {},
        "options": {},
    }
    state = PDPTranslationState(
        document={"product": {"title": "  "}, "metafields": {}, "options": {}},
        section_hashes={"product.title": make_source_hash("Titolo")},
        status="failed",
        metadata={},
    )

    missing = catalog._missing_pdp_translation_sections(
        previous_translation=state,
        source_document=source_document,
        section_hashes={"product.title": make_source_hash("Titolo")},
    )
    reused = catalog.build_pdp_payloads_from_stored_translation(
        source_document=source_document,
        stored_document=state.document or {},
        target_locale="de",
        changed_sections={"product.title"},
    )

    assert missing == {"product.title"}
    assert reused is None


def test_shopify_existing_translation_reuse_ignores_outdated_and_blank_values():
    source_document = {
        "product_gid": "gid://shopify/Product/123",
        "shop_domain": "example-store.myshopify.com",
        "source_locale": "it",
        "product": {
            "title": {
                "resource_id": "gid://shopify/Product/123",
                "key": "title",
                "value": "Titolo",
                "digest": "d1",
            },
            "body_html": {
                "resource_id": "gid://shopify/Product/123",
                "key": "body_html",
                "value": "<p>Corpo</p>",
                "digest": "d2",
            },
        },
        "metafields": {},
        "options": {},
    }
    partial, reused = catalog.build_pdp_payloads_from_shopify_translations(
        source_document=source_document,
        shopify_translations={
            "gid://shopify/Product/123": {
                "title": {"value": "Titel", "outdated": False},
                "body_html": {"value": "<p>Alt</p>", "outdated": True},
            }
        },
        target_locale="de",
        candidate_sections={"product.title", "product.body_html"},
    )

    assert reused == {"product.title"}
    assert partial is not None
    assert partial[0]["product"] == {"title": "Titel"}
    assert partial[2] == []


def test_dry_run_is_side_effect_free_and_reconciles_current_shopify_translation():
    store = _FakeStore()
    product_gid = "gid://shopify/Product/123"
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }

    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                ]
            },
            existing_translations={
                "de": {
                    product_gid: {
                        "title": {"value": "Titel", "outdated": False},
                    }
                }
            },
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=True,
            existing_products=True,
            is_create=False,
            summary=summary,
        )
    )

    assert result["items"][-1]["status"] == "synced"
    assert result["items"][-1]["locales"]["de"]["reconciled_from_shopify"] is True
    assert store.sources == {}
    assert store.translations == {}
    assert store.memory == {}


def test_blank_translator_output_fails_before_shopify_registration(monkeypatch):
    class _BlankTranslator(_FakeTranslator):
        def translate_plain(self, *args, **kwargs):
            return "  "

    async def _unexpected_register(*args, **kwargs):
        raise AssertionError("Shopify registration must not run for a blank translation")

    monkeypatch.setattr(catalog, "register_translations", _unexpected_register)
    product_gid = "gid://shopify/Product/123"

    try:
        asyncio.run(
            catalog.process_product_bundle(
                store=_FakeStore(),
                translator=_BlankTranslator(),
                product_id=123,
                product_gid=product_gid,
                metafields=[],
                live_map={
                    product_gid: [
                        {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                    ]
                },
                existing_translations={"de": {}},
                target_locales=["de"],
                source_locale="it",
                apply_translations=True,
                dry_run=False,
                existing_products=True,
                is_create=False,
            )
        )
    except RuntimeError as exc:
        assert "blank product title" in str(exc)
    else:
        raise AssertionError("Expected blank translation to fail closed")


def test_json_translation_validation_rejects_changed_technical_values():
    source_entry = {
        "namespace": "custom",
        "key": "technical_datas",
        "metafield_type": "json",
        "content_kind": "json",
        "value": '{"rows":[{"label":"Potenza trattore","value":"70 ▶ 120"}]}',
    }

    assert catalog._is_valid_json_translation(
        source_entry,
        '{"rows":[{"label":"Puissance du tracteur","value":"70 ▶ 120"}]}',
    )
    assert not catalog._is_valid_json_translation(
        source_entry,
        """{"rows":[{"label":"Puissance du tracteur","value":"['70 ▶ 120']"}]}""",
    )


def test_rich_text_validation_preserves_ast_structure_fields():
    source_entry = {
        "namespace": "custom",
        "key": "description",
        "metafield_type": "rich_text_field",
        "content_kind": "json",
        "value": '{"type":"root","children":[{"type":"text","value":"Ciao"}]}',
    }

    assert catalog._is_valid_json_translation(
        source_entry,
        '{"type":"root","children":[{"type":"text","value":"Bonjour"}]}',
    )
    assert not catalog._is_valid_json_translation(
        source_entry,
        '{"type":"racine","children":[{"type":"texte","value":"Bonjour"}]}',
    )


def test_json_validation_rejects_model_response_embedded_in_leaf():
    source_entry = {
        "namespace": "custom",
        "key": "collapsible_rows",
        "metafield_type": "json",
        "content_kind": "json",
        "value": '{"rows":[{"title":"Caratteristiche"}]}',
    }

    assert not catalog._is_valid_json_translation(
        source_entry,
        '{"rows":[{"title":"```json\\n[{\\"title\\":\\"Caractéristiques\\"}]\\n```"}]}',
    )


def test_dry_run_placeholder_is_not_rejected_by_domain_glossary():
    cache = TranslationCache(":memory:")
    translator = Translator(cache=cache, model="test-model", dry_run=True)
    try:
        translated_document, _, _, _ = catalog.translate_pdp_document(
            store=_FakeStore(),
            source_document={
                "product_gid": "gid://shopify/Product/123",
                "shop_domain": "example-store.myshopify.com",
                "product": {},
                "metafields": {
                    "custom.accessori": {
                        "resource_id": "gid://shopify/Metafield/1",
                        "namespace": "custom",
                        "key": "accessori",
                        "full_key": "custom.accessori",
                        "metafield_type": "json",
                        "content_kind": "json",
                        "value": (
                            '{"items":[{"product_handle":"fresa-test",'
                            '"title":"Fresa di prova"}]}'
                        ),
                        "digest": "digest-1",
                    }
                },
                "options": {},
            },
            changed_sections={"metafield.custom.accessori"},
            target_locale="fr",
            source_locale="it",
            translator=translator,
            existing_product=True,
        )
    finally:
        cache.close()

    assert "[fr] Fresa di prova" in translated_document["metafields"]["custom.accessori"]


def test_dry_run_detects_shopify_drift_even_when_neon_state_is_synced():
    store = _FakeStore()
    product_gid = "gid://shopify/Product/123"
    source_hash = make_source_hash("Titolo")
    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=catalog.SETTINGS.shopify_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={"product.title": source_hash},
            metadata={},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=catalog.SETTINGS.shopify_domain,
            product_gid=product_gid,
            target_locale="de",
            document={"product": {"title": "Titel"}, "metafields": {}, "options": {}},
            section_hashes={"product.title": source_hash},
            status="synced",
            model="test-model",
            metadata={},
        )
    )

    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": "Titolo", "digest": "d1", "locale": "it"},
                ]
            },
            existing_translations={
                "de": {
                    product_gid: {
                        "title": {"value": "Alter Titel", "outdated": True},
                    }
                }
            },
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=True,
            existing_products=True,
            is_create=False,
        )
    )

    locale = result["items"][-1]["locales"]["de"]
    assert locale["would_register"] == 1
    assert locale["section_sources"]["product.title"] == "stored_translation_state"
    assert (
        store.get_pdp_translation_state(
            shop_domain=catalog.SETTINGS.shopify_domain,
            product_gid=product_gid,
            target_locale="de",
        ).status
        == "synced"
    )


def test_handle_only_generates_handle_from_stored_title(monkeypatch):
    calls = []

    async def _fake_register_translations(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    monkeypatch.setattr(catalog, "register_translations", _fake_register_translations)

    store = _FakeStore()
    shop_domain = catalog.SETTINGS.shopify_domain
    product_gid = "gid://shopify/Product/123"
    store.upsert_pdp_source(
        catalog.PDPSourceRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            source_locale="it",
            document={},
            section_hashes={},
            metadata={"product_id": 123},
        )
    )
    store.upsert_pdp_translation(
        catalog.PDPTranslationRecord(
            shop_domain=shop_domain,
            product_gid=product_gid,
            target_locale="de",
            document={"product": {"title": "Kompakttraktor"}, "metafields": {}, "options": {}},
            section_hashes={"product.title": make_source_hash("Trattore compatto")},
            status="synced",
            model="test-model",
            metadata={},
        )
    )

    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }
    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=123,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": "Trattore compatto", "digest": "d1", "locale": "it"},
                    {"key": "handle", "value": "trattore-compatto", "digest": "d2", "locale": "it"},
                ]
            },
            existing_translations={"de": {}},
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            handle_only=True,
            summary=summary,
        )
    )

    state = store.get_pdp_translation_state(
        shop_domain=shop_domain,
        product_gid=product_gid,
        target_locale="de",
    )
    assert result["items"][-1]["status"] == "synced"
    assert state.document["product"]["handle"] == "kompakttraktor"
    assert calls == [
        (
            product_gid,
            [
                {
                    "key": "handle",
                    "locale": "de",
                    "value": "kompakttraktor",
                    "translatableContentDigest": "d2",
                }
            ],
        )
    ]


def test_handle_only_never_rewrites_current_shopify_handle(monkeypatch):
    async def _unexpected_register(*_args, **_kwargs):
        raise AssertionError("current localized handle must be preserved")

    monkeypatch.setattr(catalog, "register_translations", _unexpected_register)
    store = _FakeStore()
    product_gid = "gid://shopify/Product/321"
    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "items": [],
    }

    result = asyncio.run(
        catalog.process_product_bundle(
            store=store,
            translator=_FakeTranslator(),
            product_id=321,
            product_gid=product_gid,
            metafields=[],
            live_map={
                product_gid: [
                    {"key": "title", "value": "Titolo nuovo", "digest": "d1", "locale": "it"},
                    {"key": "handle", "value": "titolo-nuovo", "digest": "d2", "locale": "it"},
                ]
            },
            existing_translations={
                "de": {
                    product_gid: {
                        "title": {"value": "Neuer Titel", "outdated": False},
                        "handle": {"value": "historische-url", "outdated": False},
                    }
                }
            },
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            existing_products=True,
            is_create=False,
            handle_only=True,
            summary=summary,
        )
    )

    assert result["items"][-1]["locales"]["de"]["status"] == "current_preserved"
    assert result["registered"] == 0


def test_json_validator_preserves_blank_and_blocked_leaves():
    source_entry = {
        "namespace": "custom",
        "key": "collapsible_rows",
        "content_kind": "json",
        "metafield_type": "json",
        "value": (
            '{"disclaimer":"","rows":[{"title":"Titolo",'
            '"description":"Descrizione","image_url":"https://example.com/a.jpg"}]}'
        ),
    }

    assert catalog._is_valid_json_translation(
        source_entry,
        (
            '{"disclaimer":"","rows":[{"title":"Titel",'
            '"description":"Beschreibung","image_url":"https://example.com/a.jpg"}]}'
        ),
        target_locale="de",
    )
    assert not catalog._is_valid_json_translation(
        source_entry,
        (
            '{"disclaimer":"inventato","rows":[{"title":"Titel",'
            '"description":"Beschreibung","image_url":"https://example.com/a.jpg"}]}'
        ),
        target_locale="de",
    )
    assert not catalog._is_valid_json_translation(
        source_entry,
        (
            '{"disclaimer":"","rows":[{"title":"Titel",'
            '"description":"Beschreibung","image_url":"https://example.com/b.jpg"}]}'
        ),
        target_locale="de",
    )

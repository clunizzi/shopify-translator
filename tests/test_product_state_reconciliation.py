import asyncio

from src.bootstrap import reconcile


class _FakeStore:
    def __init__(self):
        self.records = []
        self.sources = []
        self.candidates = [
            {
                "product_gid": "gid://shopify/Product/123",
                "source_document": {
                    "product_gid": "gid://shopify/Product/123",
                    "shop_domain": "example-store.myshopify.com",
                    "source_locale": "it",
                    "product": {
                        "title": {
                            "resource_id": "gid://shopify/Product/123",
                            "key": "title",
                            "value": "Motosega",
                            "content_kind": "plain",
                        },
                        "body_html": {
                            "resource_id": "gid://shopify/Product/123",
                            "key": "body_html",
                            "value": "<p>Descrizione</p>",
                            "content_kind": "html",
                        },
                        "product_type": {
                            "resource_id": "gid://shopify/Product/123",
                            "key": "product_type",
                            "value": "",
                            "content_kind": "plain",
                        },
                    },
                    "metafields": {},
                    "options": {},
                },
                "source_hashes": {
                    "product.title": "title-hash",
                    "product.body_html": "body-hash",
                    "product.product_type": "empty-hash",
                },
                "translations": {
                    "de": {
                        "status": "failed",
                        "model": "gpt-old",
                        "metadata": {},
                    },
                    "fr": {
                        "status": "failed",
                        "model": "gpt-old",
                        "metadata": {},
                    },
                },
            }
        ]

    def ensure_schema(self):
        return None

    def count_pdp_translation_statuses(self, **_kwargs):
        return {
            "de": {"failed": 1},
            "fr": {"failed": 1},
        }

    def list_pdp_reconciliation_candidates(self, **kwargs):
        if kwargs["after_product_gid"]:
            return []
        return self.candidates[: kwargs["limit"]]

    def upsert_pdp_translation(self, record):
        self.records.append(record)

    def upsert_pdp_source(self, record):
        self.sources.append(record)


def test_reconcile_uses_shopify_reads_only_and_classifies_live_state(monkeypatch):
    async def fake_bundle(product_id, mf_include=None, target_locales=None):
        assert product_id == "123"
        assert mf_include is None
        assert target_locales == ["de", "fr"]
        product_gid = "gid://shopify/Product/123"
        live_map = {
            product_gid: [
                {
                    "key": "title",
                    "value": "Motosega",
                    "digest": "title-digest",
                    "locale": "it",
                },
                {
                    "key": "body_html",
                    "value": "<p>Descrizione</p>",
                    "digest": "body-digest",
                    "locale": "it",
                },
            ]
        }
        translations = {
            "de": {
                product_gid: {
                    "title": {
                        "value": "Motorsäge",
                        "outdated": False,
                    },
                    "body_html": {
                        "value": "<p>Beschreibung</p>",
                        "outdated": False,
                    },
                }
            },
            "fr": {
                product_gid: {
                    "title": {
                        "value": "Tronçonneuse",
                        "outdated": False,
                    },
                }
            },
        }
        return product_gid, [], live_map, translations

    monkeypatch.setattr(reconcile, "fetch_product_source_bundle", fake_bundle)
    store = _FakeStore()

    result = asyncio.run(
        reconcile.reconcile_product_translation_states(
            target_locales=["de", "fr"],
            max_products=1,
            batch_size=1,
            concurrency=1,
            dry_run=False,
            store=store,
        )
    )

    assert result["shopify_writes"] == 0
    assert result["openai_calls"] == 0
    assert result["reconciled_synced"] == 1
    assert result["reconciled_partial"] == 1
    assert len(store.sources) == 1
    assert [(record.target_locale, record.status) for record in store.records] == [
        ("de", "synced"),
        ("fr", "partial"),
    ]
    assert store.records[0].metadata["read_only_shopify_audit"] is True
    assert store.records[1].metadata["shopify_missing_sections"] == ["product.body_html"]


def test_reconcile_dry_run_does_not_write_neon(monkeypatch):
    async def fake_bundle(_product_id, mf_include=None, target_locales=None):
        product_gid = "gid://shopify/Product/123"
        live_map = {
            product_gid: [
                {
                    "key": "title",
                    "value": "Motosega",
                    "digest": "title-digest",
                    "locale": "it",
                },
                {
                    "key": "body_html",
                    "value": "<p>Descrizione</p>",
                    "digest": "body-digest",
                    "locale": "it",
                },
            ]
        }
        translations = {
            "de": {
                product_gid: {
                    "title": {"value": "Motorsäge", "outdated": False},
                    "body_html": {
                        "value": "<p>Beschreibung</p>",
                        "outdated": False,
                    },
                }
            },
            "fr": {
                product_gid: {
                    "title": {
                        "value": "Tronçonneuse",
                        "outdated": False,
                    },
                    "body_html": {
                        "value": "<p>Description</p>",
                        "outdated": False,
                    },
                }
            },
        }
        return product_gid, [], live_map, translations

    monkeypatch.setattr(reconcile, "fetch_product_source_bundle", fake_bundle)
    store = _FakeStore()

    result = asyncio.run(
        reconcile.reconcile_product_translation_states(
            target_locales=["de", "fr"],
            max_products=1,
            dry_run=True,
            store=store,
        )
    )

    assert result["reconciled_synced"] == 2
    assert store.records == []
    assert store.sources == []


def test_prune_blank_product_type_checks_live_source_before_updating_neon(
    monkeypatch,
):
    product_gid = "gid://shopify/Product/123"
    store = _FakeStore()
    store.candidates = [
        {
            "product_gid": product_gid,
            "source_document": {
                "product_gid": product_gid,
                "shop_domain": "example-store.myshopify.com",
                "source_locale": "it",
                "product": {
                    "title": {
                        "resource_id": product_gid,
                        "key": "title",
                        "value": "Motosega",
                    },
                    "product_type": {
                        "resource_id": product_gid,
                        "key": "product_type",
                        "value": "",
                    },
                },
                "metafields": {},
                "options": {},
            },
            "source_hashes": {
                "product.title": "title-hash",
                "product.product_type": "empty-hash",
            },
            "translations": {
                "fr": {
                    "status": "partial",
                    "model": "gpt-old",
                    "metadata": {"shopify_missing_sections": ["product.product_type"]},
                    "document": {
                        "product": {
                            "title": "Tronçonneuse",
                            "product_type": "Ancienne valeur",
                        },
                        "metafields": {},
                        "options": {},
                    },
                    "section_hashes": {
                        "product.title": "title-hash",
                        "product.product_type": "empty-hash",
                    },
                }
            },
        }
    ]

    async def fake_live_map(resource_ids):
        assert resource_ids == [product_gid]
        return {
            product_gid: [
                {
                    "key": "title",
                    "value": "Motosega",
                    "digest": "title-digest",
                    "locale": "it",
                }
            ]
        }

    monkeypatch.setattr(reconcile, "get_translatable_by_ids", fake_live_map)

    result = asyncio.run(
        reconcile.prune_blank_product_type_states(
            target_locales=["fr"],
            max_products=1,
            dry_run=False,
            store=store,
        )
    )

    assert result["shopify_writes"] == 0
    assert result["openai_calls"] == 0
    assert result["states_reconciled_synced"] == 1
    assert "product_type" not in store.sources[0].document["product"]
    assert store.records[0].status == "synced"
    assert "product_type" not in store.records[0].document["product"]

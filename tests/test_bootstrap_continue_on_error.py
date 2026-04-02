import asyncio

from src.bootstrap import catalog


def test_bootstrap_products_continues_after_single_failure(monkeypatch):
    processed = []

    async def _fake_fetch_product_source_bundle(product_id, mf_include, target_locales=None):
        if int(product_id) == 2:
            raise RuntimeError("boom")
        return (
            f"gid://shopify/Product/{int(product_id)}",
            [],
            {},
            {},
        )

    async def _fake_process_product_bundle(**kwargs):
        processed.append(kwargs["product_id"])
        kwargs["summary"]["products"] += 1
        return kwargs["summary"]

    class _FakeStore:
        def ensure_schema(self):
            return None

        def close(self):
            return None

    class _FakeCache:
        def close(self):
            return None

    monkeypatch.setattr(catalog, "fetch_product_source_bundle", _fake_fetch_product_source_bundle)
    monkeypatch.setattr(catalog, "process_product_bundle", _fake_process_product_bundle)
    monkeypatch.setattr(catalog, "NeonTranslationStore", lambda: _FakeStore())
    monkeypatch.setattr(catalog, "TranslationCache", lambda db_path=None: _FakeCache())

    result = asyncio.run(
        catalog.bootstrap_products(
            product_ids=[1, 2, 3],
            target_locales=["de", "fr"],
            mf_include=None,
            source_locale="it",
            apply_translations=False,
            dry_run=False,
            existing_products=True,
            is_create=False,
            continue_on_error=True,
        )
    )

    assert processed == [1, 3]
    assert result["failed_products"] == 1
    assert result["failed_product_ids"] == [2]


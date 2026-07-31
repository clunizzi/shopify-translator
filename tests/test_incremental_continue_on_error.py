import asyncio

import pytest

from src.bootstrap import incremental


class _Closable:
    def ensure_schema(self):
        return None

    def close(self):
        return None


def _patch_runtime(monkeypatch):
    monkeypatch.setattr(incremental, "NeonTranslationStore", _Closable)
    monkeypatch.setattr(
        incremental,
        "TranslationCache",
        lambda db_path=None: _Closable(),
    )
    monkeypatch.setattr(
        incremental,
        "Translator",
        lambda **_kwargs: object(),
    )

    async def fetch(product_id, *_args, **_kwargs):
        return f"gid://shopify/Product/{product_id}", [], {}, {}

    async def process(*, product_id, summary, **_kwargs):
        if product_id == 2:
            raise RuntimeError("bad product")
        summary["products"] += 1
        summary["items"].append({"product_id": product_id, "status": "synced"})

    monkeypatch.setattr(incremental, "fetch_product_source_bundle", fetch)
    monkeypatch.setattr(incremental, "process_product_bundle", process)


def test_incremental_cli_batch_can_continue_after_product_error(monkeypatch):
    _patch_runtime(monkeypatch)

    result = asyncio.run(
        incremental.sync_products_incremental(
            product_ids=[1, 2, 3],
            target_locales=["de"],
            source_locale="it",
            apply_translations=True,
            dry_run=False,
            continue_on_error=True,
        )
    )

    assert result["failed_products"] == 1
    assert result["failed_product_ids"] == [2]
    assert [item["product_id"] for item in result["items"]] == [1, 2, 3]
    assert result["items"][1]["status"] == "failed"


def test_incremental_worker_default_remains_fail_fast(monkeypatch):
    _patch_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match="bad product"):
        asyncio.run(
            incremental.sync_products_incremental(
                product_ids=[1, 2, 3],
                target_locales=["de"],
                source_locale="it",
                apply_translations=True,
                dry_run=False,
            )
        )

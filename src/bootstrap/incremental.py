from __future__ import annotations

from src.bootstrap.catalog import fetch_product_source_bundle, process_product_bundle
from src.bootstrap.seo import process_product_seo_bundle
from src.config.settings import SETTINGS
from src.state.neon import NeonTranslationStore
from src.translate.cache import TranslationCache
from src.translate.translator import Translator


async def sync_products_incremental(
    *,
    product_ids: list[int],
    target_locales: list[str],
    mf_include: list[tuple[str, str]] | None = None,
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    is_create: bool = False,
    handle_only: bool = False,
    reconcile_shopify_drift: bool = False,
    content_changes_only: bool = False,
    recover_incomplete_state: bool = True,
    sync_seo: bool = False,
    continue_on_error: bool = False,
) -> dict:
    store = NeonTranslationStore()
    if not dry_run:
        store.ensure_schema()
    cache = TranslationCache(db_path=":memory:" if dry_run else None)
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)

    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "skipped_products": 0,
        "registered": 0,
        "failed_products": 0,
        "failed_product_ids": [],
        "target_locales": target_locales,
        "items": [],
        "dry_run": dry_run,
        "state_persisted": not dry_run,
        "seo": {
            "enabled": sync_seo,
            "products": 0,
            "registered_fields": 0,
            "items": [],
        },
    }

    try:
        for product_id in product_ids:
            try:
                (
                    product_gid,
                    metafields,
                    live_map,
                    _existing_translations,
                ) = await fetch_product_source_bundle(
                    product_id,
                    mf_include,
                    target_locales=target_locales,
                )
                await process_product_bundle(
                    store=store,
                    translator=translator,
                    product_id=int(product_id),
                    product_gid=product_gid,
                    metafields=metafields,
                    live_map=live_map,
                    existing_translations=_existing_translations,
                    target_locales=target_locales,
                    source_locale=source_locale,
                    apply_translations=apply_translations,
                    dry_run=dry_run,
                    existing_products=not is_create,
                    is_create=is_create,
                    handle_only=handle_only,
                    summary=summary,
                    persist_state=not dry_run,
                    reconcile_shopify_drift=reconcile_shopify_drift,
                    content_changes_only=content_changes_only,
                    recover_incomplete_state=recover_incomplete_state,
                )
                product_item = summary["items"][-1] if summary["items"] else {}
                if sync_seo and not (
                    content_changes_only and product_item.get("status") == "unchanged"
                ):
                    seo_item = await process_product_seo_bundle(
                        store=store,
                        translator=translator,
                        product_id=int(product_id),
                        product_gid=product_gid,
                        live_map=live_map,
                        existing_translations=_existing_translations,
                        target_locales=target_locales,
                        source_locale=source_locale,
                        apply_translations=apply_translations,
                        dry_run=dry_run,
                    )
                    summary["seo"]["products"] += 1
                    summary["seo"]["registered_fields"] += sum(
                        len(locale_item.get("registered_fields") or [])
                        for locale_item in seo_item.get("locales", {}).values()
                    )
                    summary["seo"]["items"].append(seo_item)
            except Exception as exc:
                summary["failed_products"] += 1
                summary["failed_product_ids"].append(int(product_id))
                summary["items"].append(
                    {
                        "product_id": int(product_id),
                        "status": "failed",
                        "error": str(exc)[:500],
                    }
                )
                if not continue_on_error:
                    raise
                continue
            if summary["items"] and summary["items"][-1].get("status") == "unchanged":
                summary["skipped_products"] += 1
    finally:
        cache.close()
        store.close()

    return summary

from __future__ import annotations

from src.bootstrap.catalog import build_pdp_document, fetch_product_source_bundle, process_product_bundle
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
) -> dict:
    store = NeonTranslationStore()
    store.ensure_schema()
    cache = TranslationCache(db_path="/tmp/bootstrap-cache.sqlite" if dry_run else None)
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)

    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "skipped_products": 0,
    }

    try:
        for product_id in product_ids:
            product_gid, metafields, live_map, _existing_translations = await fetch_product_source_bundle(
                product_id,
                mf_include,
                target_locales=target_locales,
            )
            existing = store.has_pdp_source(
                shop_domain=SETTINGS.shopify_domain,
                product_gid=product_gid,
                source_locale=source_locale,
            )
            previous_hashes = store.get_pdp_source_hashes(
                shop_domain=SETTINGS.shopify_domain,
                product_gid=product_gid,
                source_locale=source_locale,
            )
            _doc, section_hashes = build_pdp_document(
                shop_domain=SETTINGS.shopify_domain,
                product_gid=product_gid,
                metafields=metafields,
                live_map=live_map,
                source_locale=source_locale,
                is_create=not existing,
                existing_product=existing,
            )
            changed = {
                name for name, source_hash in section_hashes.items()
                if previous_hashes.get(name) != source_hash
            }
            summary["products"] += 1
            if not changed:
                summary["skipped_products"] += 1
                continue
            summary["changed_products"] += 1
            summary["changed_sections"] += len(changed)

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
                summary=summary,
            )
    finally:
        cache.close()
        store.close()

    return summary

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from src.bootstrap.catalog import (
    build_pdp_document,
    build_pdp_payloads_from_shopify_translations,
    fetch_product_source_bundle,
)
from src.config.settings import SETTINGS
from src.shopify.graphql import get_translatable_by_ids
from src.state.neon import (
    NeonTranslationStore,
    PDPSourceRecord,
    PDPTranslationRecord,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _empty_translation_document(
    source_document: dict[str, Any],
    *,
    target_locale: str,
) -> dict[str, Any]:
    return {
        "product_gid": source_document.get("product_gid"),
        "shop_domain": source_document.get("shop_domain"),
        "source_locale": source_document.get("source_locale"),
        "target_locale": target_locale,
        "product": {},
        "metafields": {},
        "options": {},
    }


async def _audit_candidate(
    candidate: dict[str, Any],
    *,
    concurrency: asyncio.Semaphore,
) -> dict[str, Any]:
    locale_states = dict(candidate.get("translations") or {})
    locales = sorted(locale_states)
    product_gid = str(candidate.get("product_gid") or "")
    product_id = product_gid.rsplit("/", 1)[-1]

    async with concurrency:
        (
            live_product_gid,
            metafields,
            live_map,
            existing_translations,
        ) = await fetch_product_source_bundle(
            product_id,
            target_locales=locales,
        )
    source_document, source_hashes = build_pdp_document(
        shop_domain=SETTINGS.shopify_domain,
        product_gid=live_product_gid,
        metafields=metafields,
        live_map=live_map,
        source_locale=SETTINGS.source_locale,
        is_create=False,
        existing_product=True,
    )
    required_sections = set(source_hashes)

    if not required_sections:
        return {
            "product_gid": live_product_gid,
            "product_id": product_id,
            "source_document": source_document,
            "source_hashes": source_hashes,
            "not_translatable": True,
            "locales": {
                locale: {
                    "status": "not_translatable",
                    "previous_status": str((previous or {}).get("status") or ""),
                    "model": str((previous or {}).get("model") or "shopify-live-reconcile"),
                    "document": _empty_translation_document(
                        source_document,
                        target_locale=locale,
                    ),
                    "section_hashes": {},
                    "current_sections": [],
                    "missing_sections": [],
                }
                for locale, previous in locale_states.items()
            },
        }

    locale_results: dict[str, dict[str, Any]] = {}
    for locale, previous in locale_states.items():
        shopify_translations = existing_translations.get(locale) or {}
        partial, current_sections = build_pdp_payloads_from_shopify_translations(
            source_document=source_document,
            shopify_translations=shopify_translations,
            target_locale=locale,
            candidate_sections=required_sections,
        )
        if partial is None:
            live_document = _empty_translation_document(
                source_document,
                target_locale=locale,
            )
            live_hashes: dict[str, str] = {}
        else:
            live_document, live_hashes, _payloads, _sources = partial

        missing_sections = required_sections - current_sections
        status = "synced" if not missing_sections else "partial"
        previous_data = dict(previous or {})
        locale_results[locale] = {
            "status": status,
            "previous_status": str(previous_data.get("status") or ""),
            "model": str(previous_data.get("model") or "shopify-live-reconcile"),
            "document": live_document,
            "section_hashes": live_hashes,
            "current_sections": sorted(current_sections),
            "missing_sections": sorted(missing_sections),
        }

    return {
        "product_gid": live_product_gid,
        "product_id": product_id,
        "source_document": source_document,
        "source_hashes": source_hashes,
        "locales": locale_results,
    }


async def reconcile_product_translation_states(
    *,
    target_locales: list[str],
    candidate_statuses: list[str] | None = None,
    max_products: int = 25,
    batch_size: int = 25,
    concurrency: int = 3,
    dry_run: bool = True,
    store: NeonTranslationStore | None = None,
) -> dict[str, Any]:
    statuses = candidate_statuses or ["failed"]
    owned_store = store is None
    state_store = store or NeonTranslationStore()
    safe_max = max(1, int(max_products))
    safe_batch = min(max(1, int(batch_size)), safe_max)
    semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 6)))
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "shopify_writes": 0,
        "openai_calls": 0,
        "candidate_statuses": statuses,
        "target_locales": target_locales,
        "products_checked": 0,
        "locale_states_checked": 0,
        "reconciled_synced": 0,
        "reconciled_partial": 0,
        "not_translatable": 0,
        "audit_errors": 0,
        "samples": [],
    }
    after_product_gid = ""

    try:
        if not dry_run:
            state_store.ensure_schema()
        summary["before"] = state_store.count_pdp_translation_statuses(
            shop_domain=SETTINGS.shopify_domain,
            target_locales=target_locales,
        )
        while summary["products_checked"] < safe_max:
            remaining = safe_max - summary["products_checked"]
            candidates = state_store.list_pdp_reconciliation_candidates(
                shop_domain=SETTINGS.shopify_domain,
                source_locale=SETTINGS.source_locale,
                target_locales=target_locales,
                statuses=statuses,
                limit=min(safe_batch, remaining),
                after_product_gid=after_product_gid,
            )
            if not candidates:
                break
            after_product_gid = str(candidates[-1].get("product_gid") or "")
            results = await asyncio.gather(
                *[_audit_candidate(candidate, concurrency=semaphore) for candidate in candidates],
                return_exceptions=True,
            )
            summary["products_checked"] += len(candidates)

            for result in results:
                if isinstance(result, Exception):
                    summary["audit_errors"] += 1
                    if len(summary["samples"]) < 20:
                        summary["samples"].append(
                            {"status": "audit_error", "error": str(result)[:300]}
                        )
                    continue
                if result.get("error"):
                    summary["audit_errors"] += 1
                    if len(summary["samples"]) < 20:
                        summary["samples"].append(
                            {
                                "product_gid": result.get("product_gid"),
                                "status": "audit_error",
                                "error": result.get("error"),
                            }
                        )
                    continue

                if not dry_run:
                    state_store.upsert_pdp_source(
                        PDPSourceRecord(
                            shop_domain=SETTINGS.shopify_domain,
                            product_gid=str(result.get("product_gid") or ""),
                            source_locale=SETTINGS.source_locale,
                            document=dict(result.get("source_document") or {}),
                            section_hashes=dict(result.get("source_hashes") or {}),
                            metadata={
                                "product_id": int(result.get("product_id") or 0),
                                "refreshed_by_reconciliation": True,
                                "reconciled_at": _utc_now(),
                            },
                        )
                    )
                for locale, locale_result in (result.get("locales") or {}).items():
                    summary["locale_states_checked"] += 1
                    status = str(locale_result.get("status") or "partial")
                    if status == "synced":
                        summary["reconciled_synced"] += 1
                    elif status == "not_translatable":
                        summary["not_translatable"] += 1
                    else:
                        summary["reconciled_partial"] += 1
                    if not dry_run:
                        state_store.upsert_pdp_translation(
                            PDPTranslationRecord(
                                shop_domain=SETTINGS.shopify_domain,
                                product_gid=str(result.get("product_gid") or ""),
                                target_locale=locale,
                                document=dict(locale_result.get("document") or {}),
                                section_hashes=dict(locale_result.get("section_hashes") or {}),
                                status=status,
                                model=str(locale_result.get("model") or ""),
                                metadata={
                                    "reconciled_from_shopify": True,
                                    "read_only_shopify_audit": True,
                                    "reconciled_at": _utc_now(),
                                    "previous_status": locale_result.get("previous_status"),
                                    "shopify_current_sections": locale_result.get(
                                        "current_sections"
                                    ),
                                    "shopify_missing_sections": locale_result.get(
                                        "missing_sections"
                                    ),
                                },
                            )
                        )
                    if len(summary["samples"]) < 20:
                        summary["samples"].append(
                            {
                                "product_gid": result.get("product_gid"),
                                "locale": locale,
                                "status": status,
                                "current_sections": len(
                                    locale_result.get("current_sections") or []
                                ),
                                "missing_sections": locale_result.get("missing_sections"),
                            }
                        )

        summary["after"] = (
            summary["before"]
            if dry_run
            else state_store.count_pdp_translation_statuses(
                shop_domain=SETTINGS.shopify_domain,
                target_locales=target_locales,
            )
        )
        return summary
    finally:
        if owned_store:
            state_store.close()


async def prune_blank_product_type_states(
    *,
    target_locales: list[str],
    max_products: int = 3000,
    batch_size: int = 250,
    dry_run: bool = True,
    store: NeonTranslationStore | None = None,
) -> dict[str, Any]:
    owned_store = store is None
    state_store = store or NeonTranslationStore()
    safe_max = max(1, int(max_products))
    safe_batch = min(max(1, int(batch_size)), 250, safe_max)
    after_product_gid = ""
    section_name = "product.product_type"
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "shopify_writes": 0,
        "openai_calls": 0,
        "products_checked": 0,
        "source_sections_pruned": 0,
        "states_reconciled_synced": 0,
        "requires_full_audit": 0,
        "skipped_nonmatching": 0,
        "samples": [],
    }

    try:
        if not dry_run:
            state_store.ensure_schema()
        summary["before"] = state_store.count_pdp_translation_statuses(
            shop_domain=SETTINGS.shopify_domain,
            target_locales=target_locales,
        )
        while summary["products_checked"] < safe_max:
            remaining = safe_max - summary["products_checked"]
            candidates = state_store.list_pdp_reconciliation_candidates(
                shop_domain=SETTINGS.shopify_domain,
                source_locale=SETTINGS.source_locale,
                target_locales=target_locales,
                statuses=["partial"],
                limit=min(safe_batch, remaining),
                after_product_gid=after_product_gid,
            )
            if not candidates:
                break
            after_product_gid = str(candidates[-1].get("product_gid") or "")
            product_gids = [
                str(candidate.get("product_gid") or "")
                for candidate in candidates
                if candidate.get("product_gid")
            ]
            live_map = await get_translatable_by_ids(product_gids)
            summary["products_checked"] += len(candidates)

            for candidate in candidates:
                product_gid = str(candidate.get("product_gid") or "")
                source_document = dict(candidate.get("source_document") or {})
                stored_product_type = (
                    (source_document.get("product") or {}).get("product_type") or {}
                ).get("value")
                matching_locales = {
                    locale: dict(previous or {})
                    for locale, previous in (dict(candidate.get("translations") or {}).items())
                    if list(
                        ((previous or {}).get("metadata") or {}).get("shopify_missing_sections")
                        or []
                    )
                    == [section_name]
                }
                if stored_product_type not in {"", None} or not matching_locales:
                    summary["skipped_nonmatching"] += 1
                    continue

                live_product_type = next(
                    (
                        str(entry.get("value") or "")
                        for entry in (live_map.get(product_gid) or [])
                        if str(entry.get("key") or "") == "product_type"
                    ),
                    "",
                )
                if live_product_type.strip():
                    summary["requires_full_audit"] += 1
                    if len(summary["samples"]) < 20:
                        summary["samples"].append(
                            {
                                "product_gid": product_gid,
                                "status": "live_source_nonblank",
                            }
                        )
                    continue

                refreshed_source = {
                    **source_document,
                    "product": dict(source_document.get("product") or {}),
                }
                refreshed_source["product"].pop("product_type", None)
                refreshed_source_hashes = dict(candidate.get("source_hashes") or {})
                refreshed_source_hashes.pop(section_name, None)
                if not dry_run:
                    state_store.upsert_pdp_source(
                        PDPSourceRecord(
                            shop_domain=SETTINGS.shopify_domain,
                            product_gid=product_gid,
                            source_locale=SETTINGS.source_locale,
                            document=refreshed_source,
                            section_hashes=refreshed_source_hashes,
                            metadata={
                                "blank_product_type_pruned": True,
                                "reconciled_at": _utc_now(),
                            },
                        )
                    )
                summary["source_sections_pruned"] += 1

                for locale, previous in matching_locales.items():
                    previous_document = dict(previous.get("document") or {})
                    refreshed_translation = {
                        **previous_document,
                        "product": dict(previous_document.get("product") or {}),
                    }
                    refreshed_translation["product"].pop("product_type", None)
                    refreshed_translation_hashes = dict(previous.get("section_hashes") or {})
                    refreshed_translation_hashes.pop(section_name, None)
                    if not dry_run:
                        state_store.upsert_pdp_translation(
                            PDPTranslationRecord(
                                shop_domain=SETTINGS.shopify_domain,
                                product_gid=product_gid,
                                target_locale=locale,
                                document=refreshed_translation,
                                section_hashes=refreshed_translation_hashes,
                                status="synced",
                                model=str(previous.get("model") or "shopify-live-reconcile"),
                                metadata={
                                    "reconciled_from_shopify": True,
                                    "read_only_shopify_audit": True,
                                    "blank_source_section_pruned": section_name,
                                    "reconciled_at": _utc_now(),
                                    "previous_status": previous.get("status"),
                                    "shopify_missing_sections": [],
                                },
                            )
                        )
                    summary["states_reconciled_synced"] += 1
                    if len(summary["samples"]) < 20:
                        summary["samples"].append(
                            {
                                "product_gid": product_gid,
                                "locale": locale,
                                "status": "synced_after_blank_source_prune",
                            }
                        )

        summary["after"] = (
            summary["before"]
            if dry_run
            else state_store.count_pdp_translation_statuses(
                shop_domain=SETTINGS.shopify_domain,
                target_locales=target_locales,
            )
        )
        return summary
    finally:
        if owned_store:
            state_store.close()

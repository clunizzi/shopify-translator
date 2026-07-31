from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import structlog

from src.bootstrap.catalog import _translate_product_title
from src.config.dnt_loader import load_do_not_translate
from src.config.settings import SETTINGS
from src.shopify.graphql import (
    get_resource_translations_by_ids,
    list_translatable_resources,
    register_translations,
)
from src.state.neon import NeonTranslationStore, make_source_hash
from src.translate.cache import TranslationCache
from src.translate.translator import Translator
from src.translate.validators import make_handle_from_title, validate_handle

logger = structlog.get_logger()


def _numeric_product_id(product_gid: str) -> int:
    return int(product_gid.rsplit("/", 1)[-1])


def _allocate_unique_handle(
    desired: str,
    *,
    reserved: set[str],
    max_len: int = 255,
) -> str:
    base = desired[:max_len].rstrip("-")
    if base and base not in reserved and validate_handle(base, max_len=max_len):
        reserved.add(base)
        return base

    for suffix_number in range(2, 10_000):
        suffix = f"-{suffix_number}"
        candidate = f"{base[: max_len - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in reserved and validate_handle(candidate, max_len=max_len):
            reserved.add(candidate)
            return candidate
    raise RuntimeError(f"Could not allocate a unique localized handle for {desired!r}")


def _source_fields(resource: dict[str, Any]) -> dict[str, dict[str, str]]:
    resource_id = str(resource.get("resourceId") or "")
    return {
        str(item.get("key") or ""): {
            "value": str(item.get("value") or "").strip(),
            "digest": str(item.get("digest") or ""),
            "resource_id": resource_id,
        }
        for item in resource.get("translatableContent") or []
        if str(item.get("key") or "") in {"title", "handle"}
    }


async def _fetch_product_resources() -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page, page_info = await list_translatable_resources(
            resource_type="PRODUCT",
            first=250,
            after=cursor,
        )
        resources.extend(page)
        if not page_info.get("hasNextPage"):
            return resources
        cursor = str(page_info.get("endCursor") or "")
        if not cursor:
            raise RuntimeError("Shopify product handle pagination returned no endCursor")


def _plan_locale(
    *,
    locale: str,
    sources: dict[str, dict[str, dict[str, str]]],
    translations: dict[str, dict[str, dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    stats: dict[str, Any] = {
        "products_expected": len(sources),
        "handles_current": 0,
        "handles_missing": 0,
        "handles_outdated": 0,
        "candidates": 0,
        "blocked_missing_title": 0,
        "blocked_outdated_title": 0,
        "blocked_collisions": 0,
    }
    plan: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []

    handle_owners: dict[str, set[str]] = defaultdict(set)
    for product_gid, fields in translations.items():
        handle = str((fields.get("handle") or {}).get("value") or "").strip()
        if handle:
            handle_owners[handle].add(product_gid)

    reserved = {handle: set(owners) for handle, owners in handle_owners.items()}
    for product_gid in sorted(sources, key=_numeric_product_id):
        source_handle = sources[product_gid].get("handle") or {}
        source_title_value = str((sources[product_gid].get("title") or {}).get("value") or "")
        current = translations.get(product_gid) or {}
        current_handle = current.get("handle") or {}
        current_value = str(current_handle.get("value") or "").strip()
        outdated = bool(current_handle.get("outdated"))

        if current_value and not outdated:
            stats["handles_current"] += 1
            continue

        if current_value and outdated:
            stats["handles_outdated"] += 1
            # Preserve the localized URL byte-for-byte and only bind it to the
            # current source digest. This clears Shopify's outdated flag without
            # silently changing a historical URL.
            plan.append(
                {
                    "product_id": _numeric_product_id(product_gid),
                    "product_gid": product_gid,
                    "locale": locale,
                    "action": "refresh_digest",
                    "value": current_value,
                    "source_digest": str(source_handle.get("digest") or ""),
                }
            )
            continue

        stats["handles_missing"] += 1
        title = current.get("title") or {}
        title_value = str(title.get("value") or "").strip()
        if not title_value:
            stats["blocked_missing_title"] += 1
            blocked_items.append(
                {
                    "product_id": _numeric_product_id(product_gid),
                    "product_gid": product_gid,
                    "locale": locale,
                    "reason": "missing_localized_title",
                    "source_title": source_title_value,
                    "source_handle": str(source_handle.get("value") or ""),
                    "localized_title": "",
                    "desired_handle": "",
                    "collision_owner_ids": [],
                }
            )
            continue
        if bool(title.get("outdated")):
            stats["blocked_outdated_title"] += 1
            blocked_items.append(
                {
                    "product_id": _numeric_product_id(product_gid),
                    "product_gid": product_gid,
                    "locale": locale,
                    "reason": "outdated_localized_title",
                    "source_title": source_title_value,
                    "source_handle": str(source_handle.get("value") or ""),
                    "localized_title": title_value,
                    "desired_handle": "",
                    "collision_owner_ids": [],
                }
            )
            continue

        desired = make_handle_from_title(title_value)
        if not desired:
            stats["blocked_missing_title"] += 1
            blocked_items.append(
                {
                    "product_id": _numeric_product_id(product_gid),
                    "product_gid": product_gid,
                    "locale": locale,
                    "reason": "unusable_localized_title",
                    "source_title": source_title_value,
                    "source_handle": str(source_handle.get("value") or ""),
                    "localized_title": title_value,
                    "desired_handle": "",
                    "collision_owner_ids": [],
                }
            )
            continue
        other_owners = reserved.get(desired, set()) - {product_gid}
        if other_owners:
            stats["blocked_collisions"] += 1
            blocked_items.append(
                {
                    "product_id": _numeric_product_id(product_gid),
                    "product_gid": product_gid,
                    "locale": locale,
                    "reason": "localized_handle_collision",
                    "source_title": source_title_value,
                    "source_handle": str(source_handle.get("value") or ""),
                    "localized_title": title_value,
                    "desired_handle": desired,
                    "collision_owner_ids": sorted(
                        _numeric_product_id(owner) for owner in other_owners
                    ),
                }
            )
            continue

        reserved.setdefault(desired, set()).add(product_gid)
        plan.append(
            {
                "product_id": _numeric_product_id(product_gid),
                "product_gid": product_gid,
                "locale": locale,
                "action": "create_missing",
                "value": desired,
                "source_digest": str(source_handle.get("digest") or ""),
            }
        )

    stats["candidates"] = len(plan)
    stats["blocked_items"] = len(blocked_items)
    return stats, plan, blocked_items


async def audit_product_handles(*, target_locales: list[str]) -> dict[str, Any]:
    resources = await _fetch_product_resources()
    sources = {
        str(resource.get("resourceId") or ""): _source_fields(resource)
        for resource in resources
        if str(resource.get("resourceId") or "") and _source_fields(resource).get("handle")
    }
    resource_ids = sorted(sources)
    report: dict[str, Any] = {
        "products_total": len(resources),
        "products_with_handle": len(sources),
        "target_locales": target_locales,
        "locales": {},
        "plan": [],
        "blocked_items": [],
    }

    async def _audit_locale(
        locale: str,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        translations = await get_resource_translations_by_ids(resource_ids, locale)
        stats, plan, blocked_items = _plan_locale(
            locale=locale,
            sources=sources,
            translations=translations,
        )
        return locale, stats, plan, blocked_items

    # Each locale fetch already uses a four-request semaphore. Keep locales
    # sequential so the aggregate never doubles to eight concurrent reads.
    locale_results = []
    for locale in target_locales:
        locale_results.append(await _audit_locale(locale))
    for locale, stats, plan, blocked_items in locale_results:
        report["locales"][locale] = stats
        report["plan"].extend(plan)
        report["blocked_items"].extend(blocked_items)
    report["candidates"] = len(report["plan"])
    report["blocked"] = len(report["blocked_items"])
    return report


async def repair_product_handles(
    *,
    target_locales: list[str],
    apply_translations: bool,
    dry_run: bool,
    max_items: int | None = None,
    continue_on_error: bool = True,
    concurrency: int = 1,
) -> dict[str, Any]:
    audit = await audit_product_handles(target_locales=target_locales)
    plan = list(audit["plan"])
    if max_items is not None:
        plan = plan[: max(0, int(max_items))]

    summary: dict[str, Any] = {
        "audit": {
            key: value for key, value in audit.items() if key not in {"plan", "blocked_items"}
        },
        "selected_items": len(plan),
        "registered_items": 0,
        "failed_items": 0,
        "failures": [],
        "apply_translations": apply_translations,
        "dry_run": dry_run,
        "concurrency": max(1, min(int(concurrency), 4)),
        "items": plan,
    }
    if dry_run or not apply_translations:
        return summary

    semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 4)))

    async def _register_one(item: dict[str, Any]) -> tuple[dict[str, Any], Exception | None]:
        payload = {
            "key": "handle",
            "locale": item["locale"],
            "value": item["value"],
            "translatableContentDigest": item["source_digest"],
        }
        async with semaphore:
            try:
                user_errors = await register_translations(item["product_gid"], [payload])
                if user_errors:
                    raise RuntimeError(str(user_errors))
                return item, None
            except Exception as exc:
                return item, exc

    results = await asyncio.gather(*[_register_one(item) for item in plan])
    for item, exc in results:
        if exc is None:
            summary["registered_items"] += 1
        else:
            summary["failed_items"] += 1
            summary["failures"].append(
                {
                    "product_id": item["product_id"],
                    "locale": item["locale"],
                    "action": item["action"],
                    "error": str(exc),
                }
            )
            logger.error(
                "product_handle_repair_failed",
                product_id=item["product_id"],
                locale=item["locale"],
                action=item["action"],
                error=str(exc),
            )
            if not continue_on_error:
                raise exc
    return summary


async def complete_blocked_product_handles(
    *,
    target_locales: list[str],
    apply_translations: bool,
    dry_run: bool,
    max_items: int | None = None,
    continue_on_error: bool = True,
) -> dict[str, Any]:
    """Complete missing localized titles and collision-blocked handles.

    Current localized titles and handles are never rewritten. A missing title is
    translated first, then its handle is generated. Collisions receive the first
    available deterministic numeric suffix, matching Shopify's familiar URL style.
    """

    audit = await audit_product_handles(target_locales=target_locales)
    blocked_items = [
        item
        for item in audit["blocked_items"]
        if item["reason"] in {"missing_localized_title", "localized_handle_collision"}
    ]
    if max_items is not None:
        blocked_items = blocked_items[: max(0, int(max_items))]

    summary: dict[str, Any] = {
        "audit": {
            key: value for key, value in audit.items() if key not in {"plan", "blocked_items"}
        },
        "selected_items": len(blocked_items),
        "translated_titles": 0,
        "registered_handles": 0,
        "failed_items": 0,
        "failures": [],
        "apply_translations": apply_translations,
        "dry_run": dry_run,
        "items": [],
    }
    if dry_run:
        summary["items"] = blocked_items
        return summary

    resources = await _fetch_product_resources()
    sources = {
        str(resource.get("resourceId") or ""): _source_fields(resource)
        for resource in resources
        if str(resource.get("resourceId") or "") and _source_fields(resource).get("handle")
    }
    translations_by_locale: dict[str, dict[str, dict[str, Any]]] = {}
    resource_ids = sorted(sources)
    for locale in target_locales:
        translations_by_locale[locale] = await get_resource_translations_by_ids(
            resource_ids,
            locale,
        )

    reserved_by_locale: dict[str, set[str]] = {}
    for locale, translations in translations_by_locale.items():
        reserved_by_locale[locale] = {
            str((fields.get("handle") or {}).get("value") or "").strip()
            for fields in translations.values()
            if str((fields.get("handle") or {}).get("value") or "").strip()
        }

    store = NeonTranslationStore()
    store.ensure_schema()
    cache = TranslationCache()
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=False)
    dnt = load_do_not_translate(
        SETTINGS.do_not_translate_path if SETTINGS.do_not_translate_path else None
    )

    try:
        for item in blocked_items:
            product_gid = item["product_gid"]
            locale = item["locale"]
            source_fields = sources.get(product_gid) or {}
            title_source = source_fields.get("title") or {}
            handle_source = source_fields.get("handle") or {}
            existing = translations_by_locale.get(locale, {}).get(product_gid, {})
            existing_title = str((existing.get("title") or {}).get("value") or "").strip()
            existing_handle = str((existing.get("handle") or {}).get("value") or "").strip()

            item_result: dict[str, Any] = {
                "product_id": item["product_id"],
                "product_gid": product_gid,
                "locale": locale,
                "reason": item["reason"],
                "source_title": item["source_title"],
                "title": existing_title,
                "handle": existing_handle,
                "title_source": "shopify_current" if existing_title else "",
                "status": "planned",
            }
            try:
                if existing_handle:
                    item_result["status"] = "skipped_current_handle"
                    summary["items"].append(item_result)
                    continue

                translated_title = existing_title
                title_source_kind = "shopify_current"
                needs_title_registration = not translated_title
                if needs_title_registration:
                    translated_title, title_source_kind = _translate_product_title(
                        store,
                        translator,
                        str(title_source.get("value") or ""),
                        source_locale=SETTINGS.source_locale,
                        target_locale=locale,
                        dnt=dnt,
                        exclude_tokens=[],
                    )

                desired_handle = make_handle_from_title(translated_title)
                if not desired_handle:
                    raise RuntimeError("Translated title produced an empty handle")
                unique_handle = _allocate_unique_handle(
                    desired_handle,
                    reserved=reserved_by_locale[locale],
                )

                payloads = []
                if needs_title_registration:
                    payloads.append(
                        {
                            "key": "title",
                            "locale": locale,
                            "value": translated_title,
                            "translatableContentDigest": str(title_source.get("digest") or ""),
                        }
                    )
                payloads.append(
                    {
                        "key": "handle",
                        "locale": locale,
                        "value": unique_handle,
                        "translatableContentDigest": str(handle_source.get("digest") or ""),
                    }
                )

                item_result.update(
                    {
                        "title": translated_title,
                        "handle": unique_handle,
                        "title_source": title_source_kind,
                        "status": "translated",
                    }
                )
                if apply_translations:
                    user_errors = await register_translations(product_gid, payloads)
                    if user_errors:
                        raise RuntimeError(str(user_errors))
                    item_result["status"] = "synced"
                    summary["registered_handles"] += 1
                    if needs_title_registration:
                        summary["translated_titles"] += 1

                    if needs_title_registration:
                        store.upsert_translation_memory(
                            source_hash=make_source_hash(str(title_source.get("value") or "")),
                            field_key="product.title",
                            source_locale=SETTINGS.source_locale,
                            target_locale=locale,
                            source_value=str(title_source.get("value") or ""),
                            translated_value=translated_title,
                            model=translator.model,
                            metadata={
                                "product_gid": product_gid,
                                "origin": "blocked_handle_completion",
                            },
                        )
                    store.upsert_translation_memory(
                        source_hash=make_source_hash(str(handle_source.get("value") or "")),
                        field_key="product.handle",
                        source_locale=SETTINGS.source_locale,
                        target_locale=locale,
                        source_value=str(handle_source.get("value") or ""),
                        translated_value=unique_handle,
                        model=translator.model,
                        metadata={
                            "product_gid": product_gid,
                            "origin": "blocked_handle_completion",
                        },
                    )
                summary["items"].append(item_result)
            except Exception as exc:
                summary["failed_items"] += 1
                summary["failures"].append(
                    {
                        "product_id": item["product_id"],
                        "locale": locale,
                        "reason": item["reason"],
                        "error": str(exc),
                    }
                )
                item_result["status"] = "failed"
                item_result["error"] = str(exc)
                summary["items"].append(item_result)
                logger.exception(
                    "blocked_product_handle_completion_failed",
                    product_id=item["product_id"],
                    locale=locale,
                    reason=item["reason"],
                    error=str(exc),
                )
                if not continue_on_error:
                    raise
    finally:
        cache.close()
        store.close()

    return summary

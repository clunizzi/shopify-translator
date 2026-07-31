from __future__ import annotations

import asyncio
from typing import Any

import structlog

from src.bootstrap.dictionary import resolve_memory_second
from src.config.dnt_loader import load_do_not_translate
from src.config.settings import SETTINGS
from src.shopify.graphql import (
    get_resource_translations_by_ids,
    get_translatable_by_ids,
    list_translatable_resources,
    make_product_gid,
    register_translations,
)
from src.state.neon import NeonTranslationStore, make_source_hash
from src.translate.cache import TranslationCache
from src.translate.translator import Translator, meta_shape_issue, translation_output_issue
from src.translate.validators import MetaRules

SEO_KEYS = ("meta_title", "meta_description")
logger = structlog.get_logger("seo")


def _nonblank(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_entries(
    product_gid: str,
    live_map: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for item in live_map.get(product_gid, []):
        key = str(item.get("key") or "")
        value = _nonblank(item.get("value"))
        if key not in SEO_KEYS or value is None:
            continue
        entries[key] = {
            "resource_id": product_gid,
            "key": key,
            "value": value,
            "digest": str(item.get("digest") or ""),
            "locale": str(item.get("locale") or SETTINGS.source_locale),
        }
    return entries


def _translation_item(
    existing_translations: dict[str, dict[str, dict[str, dict[str, Any]]]],
    *,
    target_locale: str,
    product_gid: str,
    key: str,
) -> dict[str, Any]:
    locale_data = existing_translations.get(target_locale) or {}
    resource_data = locale_data.get(product_gid) or {}
    item = resource_data.get(key) or {}
    return dict(item) if isinstance(item, dict) else {"value": str(item or ""), "outdated": False}


def _translation_issue(
    *,
    source_value: str,
    translated_value: str,
    target_locale: str,
    dnt: Any,
) -> str | None:
    translated = _nonblank(translated_value)
    if translated is None:
        return "missing"
    return translation_output_issue(
        source_value,
        translated,
        target_locale=target_locale,
        dnt=dnt,
    )


def _field_reason(
    *,
    translation: dict[str, Any],
) -> str | None:
    if bool(translation.get("outdated")):
        return "outdated"
    if _nonblank(translation.get("value")) is None:
        return "missing"
    return None


def _validate_seo_output(
    *,
    key: str,
    source_value: str,
    translated_value: str,
    target_locale: str,
    dnt: Any,
) -> str:
    translated = _nonblank(translated_value)
    if translated is None:
        raise RuntimeError(f"Blank SEO translation for {key}")
    issue = _translation_issue(
        source_value=source_value,
        translated_value=translated,
        target_locale=target_locale,
        dnt=dnt,
    )
    if issue:
        raise RuntimeError(f"Unsafe SEO translation for {key}: {issue}")
    limits = MetaRules()
    max_len = limits.max_title_len if key == "meta_title" else limits.max_desc_len
    if len(translated) > max_len:
        raise RuntimeError(f"SEO translation for {key} exceeds {max_len} characters")
    shape_issue = meta_shape_issue(key, translated, target_locale)
    if shape_issue:
        raise RuntimeError(f"Incomplete SEO translation for {key}: {shape_issue}")
    return translated


async def _translate_seo_field(
    *,
    store: NeonTranslationStore,
    translator: Translator,
    key: str,
    source_value: str,
    source_locale: str,
    target_locale: str,
    dnt: Any,
    exclude_tokens: list[str],
) -> tuple[str, str]:
    memory = resolve_memory_second(
        store,
        field_key=f"product.{key}",
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source_value,
    )
    if memory:
        try:
            translated = _validate_seo_output(
                key=key,
                source_value=source_value,
                translated_value=memory.translated_value,
                target_locale=target_locale,
                dnt=dnt,
            )
            return translated, memory.source
        except RuntimeError as exc:
            logger.warning(
                "reject_seo_memory_hit",
                key=key,
                target_locale=target_locale,
                reason=str(exc),
            )

    async_translator = getattr(translator, "translate_seo_field_async", None)
    if callable(async_translator):
        translated = await async_translator(
            type_name="PRODUCT",
            field=key,
            source_text=source_value,
            target_locale=target_locale,
            dnt=dnt,
        )
    else:
        translated = translator.translate_field(
            "PRODUCT",
            key,
            source_value,
            target_locale,
            dnt,
            exclude_tokens,
        )
    translated = _validate_seo_output(
        key=key,
        source_value=source_value,
        translated_value=translated,
        target_locale=target_locale,
        dnt=dnt,
    )
    return translated, "translator"


async def process_product_seo_bundle(
    *,
    store: NeonTranslationStore,
    translator: Translator,
    product_id: int,
    product_gid: str,
    live_map: dict[str, list[dict[str, Any]]],
    existing_translations: dict[str, dict[str, dict[str, dict[str, Any]]]],
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
) -> dict[str, Any]:
    dnt = load_do_not_translate(SETTINGS.do_not_translate_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]
    source_entries = _source_entries(product_gid, live_map)
    item: dict[str, Any] = {
        "product_id": int(product_id),
        "product_gid": product_gid,
        "source_fields": sorted(source_entries),
        "locales": {},
    }
    if not source_entries:
        item["status"] = "shopify_fallback"
        return item

    any_action = False
    any_failure = False
    for target_locale in target_locales:
        locale_item: dict[str, Any] = {
            "current_fields": [],
            "planned_fields": [],
            "translated_fields": [],
            "registered_fields": [],
            "reasons": {},
            "section_sources": {},
            "planned_values": {},
        }
        payloads: list[dict[str, Any]] = []
        for key in SEO_KEYS:
            source_entry = source_entries.get(key)
            if not source_entry:
                continue
            translation = _translation_item(
                existing_translations,
                target_locale=target_locale,
                product_gid=product_gid,
                key=key,
            )
            reason = _field_reason(
                translation=translation,
            )
            if reason is None:
                locale_item["current_fields"].append(key)
                continue

            any_action = True
            locale_item["planned_fields"].append(key)
            locale_item["reasons"][key] = reason
            if dry_run:
                continue

            translated_value, source_kind = await _translate_seo_field(
                store=store,
                translator=translator,
                key=key,
                source_value=source_entry["value"],
                source_locale=source_locale,
                target_locale=target_locale,
                dnt=dnt,
                exclude_tokens=exclude_tokens,
            )
            store.upsert_translation_memory(
                source_hash=make_source_hash(source_entry["value"]),
                field_key=f"product.{key}",
                source_locale=source_locale,
                target_locale=target_locale,
                source_value=source_entry["value"],
                translated_value=translated_value,
                model=translator.model,
                metadata={"product_gid": product_gid, "content_type": "seo"},
            )
            locale_item["translated_fields"].append(key)
            locale_item["section_sources"][key] = source_kind
            locale_item["planned_values"][key] = {
                "source": source_entry["value"],
                "translation": translated_value,
                "source_length": len(source_entry["value"]),
                "translation_length": len(translated_value),
            }
            payloads.append(
                {
                    "key": key,
                    "locale": target_locale,
                    "value": translated_value,
                    "translatableContentDigest": source_entry["digest"],
                }
            )

        if apply_translations and not dry_run and payloads:
            user_errors = await register_translations(product_gid, payloads)
            if user_errors:
                any_failure = True
                locale_item["status"] = "failed"
                locale_item["user_errors"] = user_errors
            else:
                locale_item["registered_fields"] = [str(item["key"]) for item in payloads]
                locale_item["status"] = "synced"
        elif dry_run and locale_item["planned_fields"]:
            locale_item["status"] = "planned"
        elif payloads:
            locale_item["status"] = "translated"
        else:
            locale_item["status"] = "current"
        item["locales"][target_locale] = locale_item

    if any_failure:
        item["status"] = "failed"
    elif not any_action:
        item["status"] = "current"
    elif dry_run:
        item["status"] = "planned"
    elif apply_translations:
        item["status"] = "synced"
    else:
        item["status"] = "translated"
    return item


async def fetch_product_seo_bundle(
    product_id: int,
    target_locales: list[str],
) -> tuple[
    str,
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
    product_gid = make_product_gid(product_id)
    live_map = await get_translatable_by_ids([product_gid])

    async def _fetch_locale(
        locale: str,
    ) -> tuple[str, dict[str, dict[str, dict[str, Any]]]]:
        return locale, await get_resource_translations_by_ids([product_gid], locale)

    locale_results = await asyncio.gather(*[_fetch_locale(locale) for locale in target_locales])
    return product_gid, live_map, dict(locale_results)


async def audit_product_seo(
    *,
    target_locales: list[str],
) -> dict[str, Any]:
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
            break
        cursor = str(page_info.get("endCursor") or "")
        if not cursor:
            raise RuntimeError("Shopify product SEO pagination returned no endCursor")

    product_sources: dict[str, dict[str, str]] = {}
    for resource in resources:
        product_gid = str(resource.get("resourceId") or "")
        source = {
            str(item.get("key") or ""): str(item.get("value") or "").strip()
            for item in resource.get("translatableContent") or []
            if str(item.get("key") or "") in SEO_KEYS and str(item.get("value") or "").strip()
        }
        if product_gid and source:
            product_sources[product_gid] = source

    resource_ids = sorted(product_sources)
    translations_by_locale: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for locale in target_locales:
        translations_by_locale[locale] = await get_resource_translations_by_ids(
            resource_ids,
            locale,
        )

    dnt = load_do_not_translate(SETTINGS.do_not_translate_path)
    report: dict[str, Any] = {
        "products_total": len(resources),
        "products_with_custom_seo": len(product_sources),
        "source_fields": sum(len(fields) for fields in product_sources.values()),
        "target_locales": target_locales,
        "locales": {},
        "candidate_product_ids": [],
    }
    candidate_ids: set[int] = set()
    for locale in target_locales:
        stats = {
            "fields_expected": 0,
            "fields_current": 0,
            "fields_missing": 0,
            "fields_outdated": 0,
            "fields_quality_warnings": 0,
            "candidate_products": 0,
        }
        locale_candidate_ids: set[int] = set()
        locale_translations = translations_by_locale[locale]
        for product_gid, source_fields in product_sources.items():
            product_id = int(product_gid.rsplit("/", 1)[-1])
            for key, source_value in source_fields.items():
                stats["fields_expected"] += 1
                item = (locale_translations.get(product_gid) or {}).get(key) or {}
                reason = _field_reason(
                    translation=item,
                )
                if reason is None:
                    stats["fields_current"] += 1
                    issue = _translation_issue(
                        source_value=source_value,
                        translated_value=str(item.get("value") or ""),
                        target_locale=locale,
                        dnt=dnt,
                    )
                    if issue:
                        stats["fields_quality_warnings"] += 1
                    continue
                locale_candidate_ids.add(product_id)
                candidate_ids.add(product_id)
                if reason == "missing":
                    stats["fields_missing"] += 1
                elif reason == "outdated":
                    stats["fields_outdated"] += 1
        stats["candidate_products"] = len(locale_candidate_ids)
        report["locales"][locale] = stats
    report["candidate_product_ids"] = sorted(candidate_ids)
    report["candidate_products"] = len(candidate_ids)
    return report


async def sync_catalog_seo(
    *,
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    max_products: int | None = None,
    continue_on_error: bool = True,
    concurrency: int = 1,
) -> dict[str, Any]:
    audit = await audit_product_seo(target_locales=target_locales)
    product_ids = list(audit["candidate_product_ids"])
    if max_products is not None:
        product_ids = product_ids[: max(0, int(max_products))]

    summary: dict[str, Any] = {
        "audit": {key: value for key, value in audit.items() if key != "candidate_product_ids"},
        "selected_products": len(product_ids),
        "processed_products": 0,
        "failed_products": 0,
        "failed_product_ids": [],
        "registered_fields": 0,
        "target_locales": target_locales,
        "apply_translations": apply_translations,
        "dry_run": dry_run,
        "concurrency": max(1, int(concurrency)),
        "items": [],
    }
    if dry_run or not product_ids:
        return summary

    schema_store = NeonTranslationStore()
    try:
        schema_store.ensure_schema()
    finally:
        schema_store.close()

    telemetry: dict[str, Any] = {
        "cache_hits": 0,
        "cache_misses": 0,
        "openai_calls": 0,
        "openai_ms_total": 0,
        "openai_prompt_tokens": 0,
        "openai_completion_tokens": 0,
        "model": SETTINGS.openai_model,
    }
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def _process_one(product_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        async with semaphore:
            store = NeonTranslationStore()
            cache = TranslationCache()
            translator = Translator(
                cache=cache,
                model=SETTINGS.openai_model,
                fallback_model=SETTINGS.openai_fallback_model,
                dry_run=False,
            )
            try:
                product_gid, live_map, existing_translations = await fetch_product_seo_bundle(
                    product_id,
                    target_locales,
                )
                item = await process_product_seo_bundle(
                    store=store,
                    translator=translator,
                    product_id=product_id,
                    product_gid=product_gid,
                    live_map=live_map,
                    existing_translations=existing_translations,
                    target_locales=target_locales,
                    source_locale=source_locale,
                    apply_translations=apply_translations,
                    dry_run=False,
                )
                return item, translator.get_telemetry()
            finally:
                cache.close()
                store.close()

    async def _capture(
        product_id: int,
    ) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None, Exception | None]:
        try:
            item, item_telemetry = await _process_one(product_id)
            return product_id, item, item_telemetry, None
        except Exception as exc:
            return product_id, None, None, exc

    results = await asyncio.gather(*[_capture(product_id) for product_id in product_ids])
    for product_id, item, item_telemetry, exc in results:
        if exc is not None:
            summary["failed_products"] += 1
            summary["failed_product_ids"].append(product_id)
            logger.error(
                "seo_product_failed",
                product_id=product_id,
                error=str(exc),
            )
            if not continue_on_error:
                raise exc
            continue

        assert item is not None
        summary["processed_products"] += 1
        summary["registered_fields"] += sum(
            len(locale_item.get("registered_fields") or [])
            for locale_item in item.get("locales", {}).values()
        )
        summary["items"].append(item)
        for key in (
            "cache_hits",
            "cache_misses",
            "openai_calls",
            "openai_ms_total",
            "openai_prompt_tokens",
            "openai_completion_tokens",
        ):
            telemetry[key] += int((item_telemetry or {}).get(key) or 0)
    summary["telemetry"] = telemetry
    return summary

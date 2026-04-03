from __future__ import annotations

import json
import os
from typing import Any

import structlog

from src.bootstrap.dictionary import resolve_dictionary_first, resolve_memory_second
from src.config.dnt_loader import load_do_not_translate
from src.config.field_policies import DEFAULT_HANDLE_POLICY, should_translate_product_key
from src.config.metafield_policies import make_metafield_leaf_filter, should_translate_metafield
from src.config.settings import SETTINGS
from src.rules.option_value import should_skip_option_name, should_skip_option_value_name
from src.shopify.graphql import (
    get_product_all_metafields,
    get_product_metafields_by_keys,
    get_product_option_resources,
    get_resource_translations,
    get_translatable_by_ids,
    make_product_gid,
    register_translations,
)
from src.state.neon import (
    NeonTranslationStore,
    PDPSourceRecord,
    PDPTranslationRecord,
    make_source_hash,
)
from src.translate.cache import TranslationCache
from src.translate.translator import Translator
from src.translate.validators import make_handle_from_title


AUTO_TYPES = [
    "single_line_text_field",
    "multi_line_text_field",
    "json",
    "rich_text",
]

logger = structlog.get_logger("bootstrap")


def _section_name_product(key: str) -> str:
    return f"product.{key}"


def _section_name_metafield(full_key: str) -> str:
    return f"metafield.{full_key}"


def _section_name_option(entry_key: str) -> str:
    return f"option.{entry_key}"


async def fetch_product_source_bundle(
    product_numeric_id: str | int,
    mf_include: list[tuple[str, str]] | None = None,
    target_locales: list[str] | None = None,
) -> tuple[str, list[dict], dict[str, list[dict]], dict[str, dict[str, str]]]:
    product_gid = make_product_gid(product_numeric_id)
    if mf_include:
        metafields = await get_product_metafields_by_keys(product_gid, mf_include)
    else:
        metafields = await get_product_all_metafields(product_gid, allowed_types=AUTO_TYPES)
    option_resources = await get_product_option_resources(product_gid)
    resource_ids = [
        product_gid,
        *[m["id"] for m in metafields if m.get("id")],
        *[o["resource_id"] for o in option_resources if o.get("resource_id")],
    ]
    live_map = await get_translatable_by_ids(resource_ids)
    translations_by_locale: dict[str, dict[str, str]] = {}
    for locale in (target_locales or []):
        try:
            translations_by_locale[locale] = await get_resource_translations(product_gid, locale)
        except Exception:
            translations_by_locale[locale] = {}
    return product_gid, metafields, live_map, translations_by_locale


def build_pdp_document(
    *,
    shop_domain: str,
    product_gid: str,
    metafields: list[dict],
    live_map: dict[str, list[dict]],
    source_locale: str,
    is_create: bool,
    existing_product: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    meta_by_id = {m.get("id"): m for m in metafields if m.get("id")}
    document: dict[str, Any] = {
        "product_gid": product_gid,
        "shop_domain": shop_domain,
        "source_locale": source_locale,
        "product": {},
        "metafields": {},
        "options": {},
    }
    section_hashes: dict[str, str] = {}
    option_resource_ids = {entry.get("id") for entry in live_map.get(product_gid, []) if entry.get("id")}

    for resource_id, entries in live_map.items():
        if resource_id == product_gid:
            for entry in entries:
                key = entry.get("key") or ""
                if not should_translate_product_key(
                    key,
                    is_create=is_create,
                    existing_product=existing_product,
                    handle_policy=DEFAULT_HANDLE_POLICY,
                ):
                    continue
                section_name = _section_name_product(key)
                value = entry.get("value") or ""
                document["product"][key] = {
                    "resource_id": resource_id,
                    "key": key,
                    "content_kind": "html" if key == "body_html" else "plain",
                    "value": value,
                    "digest": entry.get("digest"),
                    "locale": entry.get("locale") or source_locale,
                }
                section_hashes[section_name] = make_source_hash(value)
            continue

        if resource_id.startswith("gid://shopify/ProductOption") or resource_id.startswith("gid://shopify/ProductOptionValue"):
            live_entry = next((x for x in entries if (x.get("key") or "") == "name"), None)
            if not live_entry:
                continue
            value = live_entry.get("value") or ""
            if not value.strip():
                continue
            kind = "option_name" if resource_id.startswith("gid://shopify/ProductOption/") else "option_value"
            if kind == "option_name":
                skip, _ = should_skip_option_name(value)
            else:
                skip, _ = should_skip_option_value_name(value, [])
            if skip:
                continue
            entry_key = f"{kind}::{resource_id}"
            document["options"][entry_key] = {
                "resource_id": resource_id,
                "key": "name",
                "entry_kind": kind,
                "content_kind": "plain",
                "value": value,
                "digest": live_entry.get("digest"),
                "locale": live_entry.get("locale") or source_locale,
            }
            section_hashes[_section_name_option(entry_key)] = make_source_hash(value)
            continue

        meta = meta_by_id.get(resource_id) or {}
        namespace = meta.get("namespace") or ""
        key = meta.get("key") or ""
        if not should_translate_metafield(namespace, key):
            continue
        full_key = f"{namespace}.{key}"
        live_entry = next((x for x in entries if (x.get("key") or "") == "value"), None)
        if not live_entry:
            continue
        value = live_entry.get("value") or ""
        mf_type = (meta.get("type") or "").lower()
        document["metafields"][full_key] = {
            "resource_id": resource_id,
            "namespace": namespace,
            "key": key,
            "full_key": full_key,
            "metafield_type": mf_type,
            "content_kind": "json" if mf_type == "json" else ("html" if mf_type == "rich_text" else "plain"),
            "value": value,
            "digest": live_entry.get("digest"),
            "locale": live_entry.get("locale") or source_locale,
        }
        section_hashes[_section_name_metafield(full_key)] = make_source_hash(value)

    return document, section_hashes


def _translate_product_type(
    store: NeonTranslationStore,
    translator: Translator,
    source_value: str,
    *,
    source_locale: str,
    target_locale: str,
    existing_translation: str | None,
    dnt,
    exclude_tokens: list[str],
) -> tuple[str, str]:
    source = (source_value or "").strip()
    if not source:
        return ("", "empty")
    resolved = resolve_dictionary_first(
        category="product_type",
        store=store,
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
        existing_translation=existing_translation,
    )
    if resolved:
        return (resolved.translated_value, resolved.source)

    mem = resolve_memory_second(
        store,
        field_key="product.product_type",
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
    )
    if mem:
        return (mem.translated_value, mem.source)

    translated = translator.translate_plain(
        "PRODUCT",
        "product_type",
        source,
        target_locale,
        dnt,
        exclude_tokens,
    )
    if translated:
        store.upsert_dictionary_translation(
            category="product_type",
            source_locale=source_locale,
            target_locale=target_locale,
            source_value=source,
            translated_value=translated,
            metadata={"origin": "translator_fallback"},
        )
    return (translated or source, "translator")


def _translate_dictionary_backed_plain(
    store: NeonTranslationStore,
    translator: Translator,
    source_value: str,
    *,
    category: str,
    memory_key: str,
    source_locale: str,
    target_locale: str,
    existing_translation: str | None,
    dnt,
    exclude_tokens: list[str],
) -> tuple[str, str]:
    source = (source_value or "").strip()
    if not source:
        return ("", "empty")

    resolved = resolve_dictionary_first(
        store,
        category=category,
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
        existing_translation=existing_translation,
    )
    if resolved:
        return (resolved.translated_value, resolved.source)

    mem = resolve_memory_second(
        store,
        field_key=memory_key,
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
    )
    if mem:
        return (mem.translated_value, mem.source)

    translated = translator.translate_plain(
        "METAFIELD",
        "value",
        source,
        target_locale,
        dnt,
        exclude_tokens,
    )
    if translated:
        store.upsert_dictionary_translation(
            category=category,
            source_locale=source_locale,
            target_locale=target_locale,
            source_value=source,
            translated_value=translated,
            metadata={"origin": "translator_fallback"},
        )
    return (translated or source, "translator")


def _translate_json_metafield(
    translator: Translator,
    entry: dict[str, Any],
    *,
    target_locale: str,
    dnt,
    exclude_tokens: list[str],
) -> str:
    return translator.translate_json_value(
        "METAFIELD",
        "value",
        entry["value"],
        target_locale,
        dnt,
        exclude_tokens,
        should_translate_leaf=make_metafield_leaf_filter(entry["namespace"], entry["key"]),
    )


def _translate_option_entry(
    store: NeonTranslationStore,
    translator: Translator,
    entry: dict[str, Any],
    *,
    target_locale: str,
    source_locale: str,
    dnt,
    exclude_tokens: list[str],
) -> tuple[str, str]:
    category = entry["entry_kind"]
    field = "option_name" if category == "option_name" else "option_value_name"
    translated_value, source_kind = _translate_dictionary_backed_plain(
        store,
        translator,
        entry["value"],
        category=category,
        memory_key=f"option.{category}",
        source_locale=source_locale,
        target_locale=target_locale,
        existing_translation=None,
        dnt=dnt,
        exclude_tokens=exclude_tokens,
    )
    return translated_value, source_kind


def translate_pdp_document(
    *,
    store: NeonTranslationStore,
    source_document: dict[str, Any],
    changed_sections: set[str],
    target_locale: str,
    source_locale: str,
    translator: Translator,
    existing_product: bool,
    existing_shopify_translations: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]]:
    dnt_path = SETTINGS.do_not_translate_path if getattr(SETTINGS, "do_not_translate_path", None) else None
    dnt = load_do_not_translate(dnt_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]

    translated_document: dict[str, Any] = {
        "product_gid": source_document["product_gid"],
        "shop_domain": source_document["shop_domain"],
        "source_locale": source_locale,
        "target_locale": target_locale,
        "product": {},
        "metafields": {},
        "options": {},
    }
    translated_hashes: dict[str, str] = {}
    payloads: list[dict] = []
    section_sources: dict[str, str] = {}
    title_translated: str | None = None

    product_entries = source_document.get("product", {})
    product_order = {"title": 10, "body_html": 20, "product_type": 30, "handle": 40}
    for key in sorted(product_entries.keys(), key=lambda k: product_order.get(k, 100)):
        entry = product_entries[key]
        section_name = _section_name_product(key)
        if section_name not in changed_sections:
            continue

        if key == "handle":
            if not DEFAULT_HANDLE_POLICY.allows(
                is_create=not existing_product,
                existing_product=existing_product,
            ):
                continue
            base = title_translated or ""
            translated_value = make_handle_from_title(base) if base else entry["value"]
            section_sources[section_name] = "handle_from_title" if base else "handle_original"
        elif key == "product_type":
            translated_value, source_kind = _translate_product_type(
                store,
                translator,
                entry["value"],
                source_locale=source_locale,
                target_locale=target_locale,
                existing_translation=(existing_shopify_translations or {}).get("product_type"),
                dnt=dnt,
                exclude_tokens=exclude_tokens,
            )
            section_sources[section_name] = source_kind
        elif key == "body_html":
            translated_value = translator.translate_html_document(
                "PRODUCT",
                key,
                entry["value"],
                target_locale,
                dnt,
                exclude_tokens,
            )
            section_sources[section_name] = "translator_html_document"
        else:
            translated_value = translator.translate_plain(
                "PRODUCT",
                key,
                entry["value"],
                target_locale,
                dnt,
                exclude_tokens,
            )
            section_sources[section_name] = "translator"
            if key == "title":
                title_translated = translated_value

        translated_document["product"][key] = translated_value
        translated_hashes[section_name] = make_source_hash(entry["value"])
        payloads.append(
            {
                "resource_id": entry["resource_id"],
                "key": key,
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": entry.get("digest") or "",
            }
        )

    for full_key, entry in source_document.get("metafields", {}).items():
        section_name = _section_name_metafield(full_key)
        if section_name not in changed_sections:
            continue

        if entry["content_kind"] == "json":
            translated_value = _translate_json_metafield(
                translator,
                entry,
                target_locale=target_locale,
                dnt=dnt,
                exclude_tokens=exclude_tokens,
            )
            section_sources[section_name] = "translator_json_leafs"
        elif entry["content_kind"] == "html":
            translated_value = translator.translate_html_document(
                "METAFIELD",
                "value",
                entry["value"],
                target_locale,
                dnt,
                exclude_tokens,
            )
            section_sources[section_name] = "translator_html_document"
        else:
            if full_key == "custom.condizione":
                translated_value, source_kind = _translate_dictionary_backed_plain(
                    store,
                    translator,
                    entry["value"],
                    category="custom.condizione",
                    memory_key=f"metafield.{full_key}",
                    source_locale=source_locale,
                    target_locale=target_locale,
                    existing_translation=None,
                    dnt=dnt,
                    exclude_tokens=exclude_tokens,
                )
                section_sources[section_name] = source_kind
            else:
                mem = resolve_memory_second(
                    store,
                    field_key=f"metafield.{full_key}",
                    source_locale=source_locale,
                    target_locale=target_locale,
                    source_value=entry["value"],
                )
                if mem:
                    translated_value = mem.translated_value
                    section_sources[section_name] = mem.source
                else:
                    translated_value = translator.translate_plain(
                        "METAFIELD",
                        "value",
                        entry["value"],
                        target_locale,
                        dnt,
                        exclude_tokens,
                    )
                    section_sources[section_name] = "translator"

        translated_document["metafields"][full_key] = translated_value
        translated_hashes[section_name] = make_source_hash(entry["value"])
        payloads.append(
            {
                "resource_id": entry["resource_id"],
                "key": "value",
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": entry.get("digest") or "",
            }
        )

    for entry_key, entry in source_document.get("options", {}).items():
        section_name = _section_name_option(entry_key)
        if section_name not in changed_sections:
            continue
        translated_value, source_kind = _translate_option_entry(
            store,
            translator,
            entry,
            target_locale=target_locale,
            source_locale=source_locale,
            dnt=dnt,
            exclude_tokens=exclude_tokens,
        )
        translated_document["options"][entry_key] = translated_value
        translated_hashes[section_name] = make_source_hash(entry["value"])
        section_sources[section_name] = source_kind
        payloads.append(
            {
                "resource_id": entry["resource_id"],
                "key": entry["key"],
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": entry.get("digest") or "",
            }
        )

    return translated_document, translated_hashes, payloads, section_sources


def build_pdp_payloads_from_stored_translation(
    *,
    source_document: dict[str, Any],
    stored_document: dict[str, Any],
    target_locale: str,
    changed_sections: set[str],
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]] | None:
    translated_document: dict[str, Any] = {
        "product_gid": source_document["product_gid"],
        "shop_domain": source_document["shop_domain"],
        "source_locale": source_document["source_locale"],
        "target_locale": target_locale,
        "product": {},
        "metafields": {},
        "options": {},
    }
    translated_hashes: dict[str, str] = {}
    payloads: list[dict] = []
    section_sources: dict[str, str] = {}
    stored_product = dict((stored_document or {}).get("product") or {})
    stored_metafields = dict((stored_document or {}).get("metafields") or {})
    stored_options = dict((stored_document or {}).get("options") or {})

    for section_name in changed_sections:
        if section_name.startswith("product."):
            key = section_name.split(".", 1)[1]
            source_entry = source_document["product"].get(key)
            if not source_entry or key not in stored_product:
                return None
            translated_value = str(stored_product[key])
            translated_document["product"][key] = translated_value
        elif section_name.startswith("metafield."):
            key = section_name.split(".", 1)[1]
            source_entry = source_document["metafields"].get(key)
            if not source_entry or key not in stored_metafields:
                return None
            translated_value = str(stored_metafields[key])
            translated_document["metafields"][key] = translated_value
        elif section_name.startswith("option."):
            key = section_name.split(".", 1)[1]
            source_entry = source_document["options"].get(key)
            if not source_entry or key not in stored_options:
                return None
            translated_value = str(stored_options[key])
            translated_document["options"][key] = translated_value
        else:
            return None

        translated_hashes[section_name] = make_source_hash(source_entry["value"])
        payloads.append(
            {
                "resource_id": source_entry["resource_id"],
                "key": source_entry["key"],
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": source_entry.get("digest") or "",
            }
        )
        section_sources[section_name] = "stored_translation_state"

    return translated_document, translated_hashes, payloads, section_sources


async def bootstrap_products(
    *,
    product_ids: list[int],
    target_locales: list[str],
    mf_include: list[tuple[str, str]] | None = None,
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    existing_products: bool = True,
    is_create: bool = False,
    continue_on_error: bool = True,
) -> dict:
    store = NeonTranslationStore()
    store.ensure_schema()
    cache = TranslationCache(db_path="/tmp/bootstrap-cache.sqlite" if dry_run else None)
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)

    summary = {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "failed_products": 0,
        "failed_product_ids": [],
        "target_locales": target_locales,
    }

    try:
        for product_id in product_ids:
            try:
                product_gid, metafields, live_map, existing_translations = await fetch_product_source_bundle(
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
                    existing_translations=existing_translations,
                    target_locales=target_locales,
                    source_locale=source_locale,
                    apply_translations=apply_translations,
                    dry_run=dry_run,
                    existing_products=existing_products,
                    is_create=is_create,
                    summary=summary,
                )
            except Exception as exc:
                summary["failed_products"] += 1
                summary["failed_product_ids"].append(int(product_id))
                logger.exception(
                    "bootstrap_product_failed",
                    product_id=int(product_id),
                    apply_translations=apply_translations,
                    dry_run=dry_run,
                    continue_on_error=continue_on_error,
                    error=str(exc),
                )
                if not continue_on_error:
                    raise
    finally:
        cache.close()
        store.close()

    return summary


async def process_product_bundle(
    *,
    store: NeonTranslationStore,
    translator: Translator,
    product_id: int,
    product_gid: str,
    metafields: list[dict],
    live_map: dict[str, list[dict]],
    existing_translations: dict[str, dict[str, str]],
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    existing_products: bool,
    is_create: bool,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    local_summary = summary if summary is not None else {
        "products": 0,
        "changed_products": 0,
        "changed_sections": 0,
        "registered": 0,
        "target_locales": target_locales,
        "items": [],
    }
    local_summary.setdefault("items", [])
    already_in_neon = store.has_pdp_source(
        shop_domain=SETTINGS.shopify_domain,
        product_gid=product_gid,
        source_locale=source_locale,
    )
    effective_existing = bool(existing_products)
    effective_is_create = bool(is_create or not existing_products)
    source_document, section_hashes = build_pdp_document(
        shop_domain=SETTINGS.shopify_domain,
        product_gid=product_gid,
        metafields=metafields,
        live_map=live_map,
        source_locale=source_locale,
        is_create=effective_is_create,
        existing_product=effective_existing,
    )
    previous_hashes = store.get_pdp_source_hashes(
        shop_domain=SETTINGS.shopify_domain,
        product_gid=product_gid,
        source_locale=source_locale,
    )
    changed_sections = {
        name for name, source_hash in section_hashes.items()
        if previous_hashes.get(name) != source_hash
    }

    store.upsert_pdp_source(
        PDPSourceRecord(
            shop_domain=SETTINGS.shopify_domain,
            product_gid=product_gid,
            source_locale=source_locale,
            document=source_document,
            section_hashes=section_hashes,
            metadata={"product_id": int(product_id), "already_in_neon": already_in_neon},
        )
    )

    local_summary["products"] += 1
    item_summary: dict[str, Any] = {
        "product_id": int(product_id),
        "product_gid": product_gid,
        "product_title": (source_document.get("product", {}).get("title", {}) or {}).get("value", ""),
        "changed_sections": sorted(list(changed_sections)),
        "locales": {},
    }
    previous_translations = {
        target_locale: store.get_pdp_translation_state(
            shop_domain=SETTINGS.shopify_domain,
            product_gid=product_gid,
            target_locale=target_locale,
        )
        for target_locale in target_locales
    }
    pending_sync_locales = {
        target_locale
        for target_locale, previous_translation in previous_translations.items()
        if apply_translations and not dry_run and (previous_translation is None or previous_translation.status != "synced")
    }
    if not changed_sections and not pending_sync_locales:
        item_summary["status"] = "unchanged"
        local_summary["items"].append(item_summary)
        return local_summary
    if changed_sections:
        local_summary["changed_products"] += 1
        local_summary["changed_sections"] += len(changed_sections)

    for target_locale in target_locales:
        previous_translation = previous_translations.get(target_locale)
        locale_changed_sections = set(changed_sections)
        if previous_translation is None or previous_translation.status != "synced":
            locale_changed_sections.update(section_hashes.keys())
        else:
            for section_name, source_hash in section_hashes.items():
                if previous_translation.section_hashes.get(section_name) != source_hash:
                    locale_changed_sections.add(section_name)

        reused = None
        if (
            previous_translation is not None
            and previous_translation.status != "synced"
            and previous_translation.section_hashes == section_hashes
        ):
            reused = build_pdp_payloads_from_stored_translation(
                source_document=source_document,
                stored_document=previous_translation.document or {},
                target_locale=target_locale,
                changed_sections=locale_changed_sections,
            )

        if reused is not None:
            translated_document, translated_hashes, payloads, section_sources = reused
        else:
            translated_document, translated_hashes, payloads, section_sources = translate_pdp_document(
                store=store,
                source_document=source_document,
                changed_sections=locale_changed_sections,
                target_locale=target_locale,
                source_locale=source_locale,
                translator=translator,
                existing_product=effective_existing,
                existing_shopify_translations=existing_translations.get(target_locale, {}),
            )

        for section_name in locale_changed_sections:
            if section_name.startswith("product."):
                key = section_name.split(".", 1)[1]
                source_value = source_document["product"][key]["value"]
                translated_value = translated_document["product"].get(key, "")
            else:
                suffix = section_name.split(".", 1)[1]
                if section_name.startswith("metafield."):
                    source_value = source_document["metafields"][suffix]["value"]
                    translated_value = translated_document["metafields"].get(suffix, "")
                else:
                    source_value = source_document["options"][suffix]["value"]
                    translated_value = translated_document["options"].get(suffix, "")
            store.upsert_translation_memory(
                source_hash=make_source_hash(source_value),
                field_key=section_name,
                source_locale=source_locale,
                target_locale=target_locale,
                source_value=source_value,
                translated_value=translated_value,
                model=translator.model,
                metadata={"product_gid": product_gid},
            )

        translation_status = "translated"
        translation_metadata: dict[str, Any] = {
            "changed_sections": sorted(list(locale_changed_sections)),
        }
        locale_summary: dict[str, Any] = {
            "translated_sections": sorted(list(locale_changed_sections)),
            "section_sources": section_sources,
        }
        if apply_translations and not dry_run:
            grouped: dict[str, list[dict]] = {}
            for item in payloads:
                grouped.setdefault(item["resource_id"], []).append(
                    {
                        "key": item["key"],
                        "locale": item["locale"],
                        "value": item["value"],
                        "translatableContentDigest": item["translatableContentDigest"],
                    }
                )
            all_user_errors: dict[str, list[dict]] = {}
            for resource_id, resource_payloads in grouped.items():
                user_errors = await register_translations(resource_id, resource_payloads)
                if not user_errors:
                    local_summary["registered"] += len(resource_payloads)
                else:
                    all_user_errors[resource_id] = user_errors
            if all_user_errors:
                translation_status = "failed"
                translation_metadata["user_errors"] = all_user_errors
                locale_summary["user_errors"] = all_user_errors
            else:
                translation_status = "synced"
            if os.environ.get("LOG_VERBOSE_SYNC", "false").lower() in {"1", "true", "yes", "y"}:
                locale_summary["shopify_payloads"] = grouped
        store.upsert_pdp_translation(
            PDPTranslationRecord(
                shop_domain=SETTINGS.shopify_domain,
                product_gid=product_gid,
                target_locale=target_locale,
                document=translated_document,
                section_hashes=translated_hashes,
                status=translation_status,
                model=translator.model,
                metadata=translation_metadata,
            )
        )
        locale_summary["status"] = translation_status
        item_summary["locales"][target_locale] = locale_summary

    statuses = [v.get("status", "translated") for v in item_summary["locales"].values()]
    item_summary["status"] = "failed" if any(s == "failed" for s in statuses) else ("synced" if statuses and all(s == "synced" for s in statuses) else "translated")
    local_summary["items"].append(item_summary)
    return local_summary

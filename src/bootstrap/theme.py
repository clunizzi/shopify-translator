from __future__ import annotations

import os
from typing import Any

from src.config.dnt_loader import load_do_not_translate
from src.config.settings import SETTINGS
from src.config.theme_policies import should_translate_theme_entry
from src.shopify.graphql import list_translatable_resources, register_translations
from src.state.neon import (
    NeonTranslationStore,
    ThemeSourceRecord,
    ThemeTranslationRecord,
    make_source_hash,
)
from src.translate.cache import TranslationCache
from src.translate.translator import Translator


THEME_RESOURCE_TYPES = [
    "ONLINE_STORE_THEME_JSON_TEMPLATE",
    "ONLINE_STORE_THEME_SECTION_GROUP",
]


def _theme_section_name(resource_type: str, key: str) -> str:
    return f"{resource_type}.{key}"


async def fetch_theme_source_bundle(
    *,
    resource_types: list[str] | None = None,
    first: int = 250,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for resource_type in (resource_types or THEME_RESOURCE_TYPES):
        cursor: str | None = None
        while True:
            nodes, page_info = await list_translatable_resources(
                resource_type=resource_type,
                first=first,
                after=cursor,
            )
            for node in nodes:
                out.append(
                    {
                        "resource_type": resource_type,
                        "resource_id": node["resourceId"],
                        "translatableContent": node.get("translatableContent") or [],
                    }
                )
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    return out


def build_theme_documents(
    *,
    shop_domain: str,
    theme_id: str,
    source_locale: str,
    bundle: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, str]]]:
    docs: list[tuple[dict[str, Any], dict[str, str]]] = []
    for item in bundle:
        resource_type = item["resource_type"]
        resource_id = item["resource_id"]
        document: dict[str, Any] = {
            "shop_domain": shop_domain,
            "theme_id": str(theme_id),
            "resource_type": resource_type,
            "resource_id": resource_id,
            "source_locale": source_locale,
            "entries": {},
        }
        section_hashes: dict[str, str] = {}
        for entry in item.get("translatableContent") or []:
            key = str(entry.get("key") or "")
            value = str(entry.get("value") or "")
            if not should_translate_theme_entry(resource_type=resource_type, key=key, value=value):
                continue
            section_name = _theme_section_name(resource_type, key)
            content_kind = "html" if "<" in value and ">" in value else ("liquid" if "{{" in value or "{%" in value else "plain")
            document["entries"][key] = {
                "resource_id": resource_id,
                "key": key,
                "value": value,
                "digest": entry.get("digest") or "",
                "locale": entry.get("locale") or source_locale,
                "content_kind": content_kind,
            }
            section_hashes[section_name] = make_source_hash(value)
        if document["entries"]:
            docs.append((document, section_hashes))
    return docs


def translate_theme_document(
    *,
    source_document: dict[str, Any],
    changed_sections: set[str],
    target_locale: str,
    translator: Translator,
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]]:
    dnt_path = SETTINGS.do_not_translate_path if getattr(SETTINGS, "do_not_translate_path", None) else None
    dnt = load_do_not_translate(dnt_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]

    translated_document: dict[str, Any] = {
        "shop_domain": source_document["shop_domain"],
        "theme_id": source_document["theme_id"],
        "resource_type": source_document["resource_type"],
        "resource_id": source_document["resource_id"],
        "source_locale": source_document["source_locale"],
        "target_locale": target_locale,
        "entries": {},
    }
    translated_hashes: dict[str, str] = {}
    payloads: list[dict] = []
    section_sources: dict[str, str] = {}

    for key, entry in source_document.get("entries", {}).items():
        section_name = _theme_section_name(source_document["resource_type"], key)
        if section_name not in changed_sections:
            continue
        if entry["content_kind"] == "html" or entry["content_kind"] == "liquid":
            translated_value = translator.translate_html_document(
                "ONLINE_STORE_THEME",
                key,
                entry["value"],
                target_locale,
                dnt,
                exclude_tokens,
            )
            section_sources[section_name] = "translator_html_document"
        else:
            translated_value = translator.translate_plain(
                "ONLINE_STORE_THEME",
                key,
                entry["value"],
                target_locale,
                dnt,
                exclude_tokens,
            )
            section_sources[section_name] = "translator"

        translated_document["entries"][key] = translated_value
        translated_hashes[section_name] = make_source_hash(entry["value"])
        payloads.append(
            {
                "resource_id": entry["resource_id"],
                "key": key,
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": entry["digest"],
            }
        )
    return translated_document, translated_hashes, payloads, section_sources


async def bootstrap_theme(
    *,
    theme_id: str | int,
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    resource_types: list[str] | None = None,
) -> dict[str, Any]:
    store = NeonTranslationStore()
    store.ensure_schema()
    cache = TranslationCache(db_path="/tmp/theme-bootstrap-cache.sqlite" if dry_run else None)
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)

    summary: dict[str, Any] = {
        "theme_id": str(theme_id),
        "resource_types": resource_types or THEME_RESOURCE_TYPES,
        "resources": 0,
        "changed_resources": 0,
        "changed_sections": 0,
        "registered": 0,
        "target_locales": target_locales,
        "items": [],
    }

    try:
        bundle = await fetch_theme_source_bundle(resource_types=resource_types)
        docs = build_theme_documents(
            shop_domain=SETTINGS.shopify_domain,
            theme_id=str(theme_id),
            source_locale=source_locale,
            bundle=bundle,
        )
        for source_document, section_hashes in docs:
            resource_type = source_document["resource_type"]
            resource_id = source_document["resource_id"]
            previous_hashes = store.get_theme_source_hashes(
                shop_domain=SETTINGS.shopify_domain,
                theme_id=str(theme_id),
                resource_type=resource_type,
                resource_id=resource_id,
                source_locale=source_locale,
            )
            changed_sections = {
                name for name, source_hash in section_hashes.items() if previous_hashes.get(name) != source_hash
            }
            store.upsert_theme_source(
                ThemeSourceRecord(
                    shop_domain=SETTINGS.shopify_domain,
                    theme_id=str(theme_id),
                    resource_type=resource_type,
                    resource_id=resource_id,
                    source_locale=source_locale,
                    document=source_document,
                    section_hashes=section_hashes,
                    metadata={},
                )
            )
            summary["resources"] += 1
            item_summary: dict[str, Any] = {
                "theme_id": str(theme_id),
                "resource_type": resource_type,
                "resource_id": resource_id,
                "changed_sections": sorted(list(changed_sections)),
                "locales": {},
            }
            pending_sync_locales = set()
            previous_translations = {}
            for target_locale in target_locales:
                state = store.get_theme_translation_state(
                    shop_domain=SETTINGS.shopify_domain,
                    theme_id=str(theme_id),
                    resource_type=resource_type,
                    resource_id=resource_id,
                    target_locale=target_locale,
                )
                previous_translations[target_locale] = state
                if apply_translations and not dry_run and (state is None or state.status != "synced"):
                    pending_sync_locales.add(target_locale)

            if not changed_sections and not pending_sync_locales:
                item_summary["status"] = "unchanged"
                summary["items"].append(item_summary)
                continue

            if changed_sections:
                summary["changed_resources"] += 1
                summary["changed_sections"] += len(changed_sections)

            for target_locale in target_locales:
                previous_translation = previous_translations.get(target_locale)
                locale_changed_sections = set(changed_sections)
                if previous_translation is None or previous_translation.status != "synced":
                    locale_changed_sections.update(section_hashes.keys())
                else:
                    for section_name, source_hash in section_hashes.items():
                        if previous_translation.section_hashes.get(section_name) != source_hash:
                            locale_changed_sections.add(section_name)

                translated_document, translated_hashes, payloads, section_sources = translate_theme_document(
                    source_document=source_document,
                    changed_sections=locale_changed_sections,
                    target_locale=target_locale,
                    translator=translator,
                )

                for section_name in locale_changed_sections:
                    key = section_name.split(".", 1)[1]
                    source_value = source_document["entries"][key]["value"]
                    translated_value = translated_document["entries"].get(key, "")
                    store.upsert_translation_memory(
                        source_hash=make_source_hash(source_value),
                        field_key=section_name,
                        source_locale=source_locale,
                        target_locale=target_locale,
                        source_value=source_value,
                        translated_value=translated_value,
                        model=translator.model,
                        metadata={"theme_id": str(theme_id), "resource_id": resource_id},
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
                    shopify_payloads = [
                        {
                            "key": item["key"],
                            "locale": item["locale"],
                            "value": item["value"],
                            "translatableContentDigest": item["translatableContentDigest"],
                        }
                        for item in payloads
                    ]
                    user_errors = await register_translations(resource_id, shopify_payloads)
                    if not user_errors:
                        summary["registered"] += len(shopify_payloads)
                        translation_status = "synced"
                    else:
                        translation_status = "failed"
                        translation_metadata["user_errors"] = user_errors
                        locale_summary["user_errors"] = user_errors
                    if os.environ.get("LOG_VERBOSE_SYNC", "false").lower() in {"1", "true", "yes", "y"}:
                        locale_summary["shopify_payloads"] = shopify_payloads

                store.upsert_theme_translation(
                    ThemeTranslationRecord(
                        shop_domain=SETTINGS.shopify_domain,
                        theme_id=str(theme_id),
                        resource_type=resource_type,
                        resource_id=resource_id,
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
            summary["items"].append(item_summary)
    finally:
        cache.close()
        store.close()

    return summary

from __future__ import annotations

import os
import re
from typing import Any

import structlog

from src.config.dnt_loader import load_do_not_translate
from src.config.settings import SETTINGS
from src.shopify.graphql import list_translatable_resources, register_translations
from src.state.neon import (
    NeonTranslationStore,
    ThemeSourceRecord,
    ThemeTranslationRecord,
    make_source_hash,
)
from src.translate.cache import TranslationCache
from src.translate.translator import Translator

logger = structlog.get_logger("resources")

GLOBAL_RESOURCE_GROUP = "global"
DEFAULT_RESOURCE_TYPES = [
    "SHOP_POLICY",
    "SHOP",
]

_BLOCKED_VALUE_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^\s*\{\{.*\}\}\s*$", re.DOTALL),
    re.compile(r"^\s*\{%.*%\}\s*$", re.DOTALL),
    re.compile(r"^\s*<svg[\s>].*</svg>\s*$", re.IGNORECASE | re.DOTALL),
    re.compile(r"^\s*https?://", re.IGNORECASE),
    re.compile(r"^\s*[\[{].*[\]}]\s*$", re.DOTALL),
]

_BLOCKED_KEY_FRAGMENTS = {
    "color",
    "icon",
    "image",
    "logo",
    "svg",
    "url",
}

_SHOP_ALLOWED_KEYS = {
    "slogan",
    "short_description",
    "policy_link_text",
    "title",
    "text",
    "button_prefs_open_text",
    "button_accept_text",
    "button_decline_text",
    "preferences_title",
    "preferences_intro_title",
    "preferences_intro_text",
    "preferences_button_accept_text",
    "preferences_button_decline_text",
    "preferences_button_save_text",
    "preferences_bullet_points_title",
    "preferences_bullet_points_first_text",
    "preferences_bullet_points_second_text",
    "preferences_bullet_points_third_text",
    "preferences_purposes_essential_name",
    "preferences_purposes_essential_desc",
    "preferences_purposes_performance_name",
    "preferences_purposes_performance_desc",
    "preferences_purposes_preferences_name",
    "preferences_purposes_preferences_desc",
    "preferences_purposes_marketing_name",
    "preferences_purposes_marketing_desc",
}


def _resource_section_name(resource_type: str, key: str) -> str:
    return f"{resource_type}.{key}"


def _content_kind(value: str) -> str:
    if "<" in value and ">" in value:
        return "html"
    if "{{" in value or "{%" in value:
        return "liquid"
    return "plain"


def _has_letters(value: str) -> bool:
    return any(ch.isalpha() for ch in value)


def should_translate_resource_entry(*, resource_type: str, key: str, value: str) -> bool:
    rt = (resource_type or "").strip().upper()
    key_l = (key or "").strip().lower()
    val = value or ""
    if any(pattern.match(val) for pattern in _BLOCKED_VALUE_PATTERNS):
        return False
    if not _has_letters(val):
        return False
    if rt == "SHOP_POLICY":
        return key_l in {"body", "title", "name"}
    if rt == "SHOP":
        return key_l in _SHOP_ALLOWED_KEYS or key_l.startswith("preferences_")
    return not any(fragment in key_l for fragment in _BLOCKED_KEY_FRAGMENTS)


async def fetch_global_resource_bundle(
    *,
    resource_types: list[str] | None = None,
    resource_ids: list[str] | None = None,
    first: int = 250,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    allowed_ids = set(resource_ids or [])
    for resource_type in resource_types or DEFAULT_RESOURCE_TYPES:
        cursor: str | None = None
        while True:
            nodes, page_info = await list_translatable_resources(
                resource_type=resource_type,
                first=first,
                after=cursor,
            )
            for node in nodes:
                if allowed_ids and node["resourceId"] not in allowed_ids:
                    continue
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


def build_global_resource_documents(
    *,
    shop_domain: str,
    source_locale: str,
    bundle: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, str]]]:
    docs: list[tuple[dict[str, Any], dict[str, str]]] = []
    for item in bundle:
        resource_type = item["resource_type"]
        resource_id = item["resource_id"]
        document: dict[str, Any] = {
            "shop_domain": shop_domain,
            "resource_group": GLOBAL_RESOURCE_GROUP,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "source_locale": source_locale,
            "entries": {},
        }
        section_hashes: dict[str, str] = {}
        for entry in item.get("translatableContent") or []:
            key = str(entry.get("key") or "")
            value = str(entry.get("value") or "")
            if not should_translate_resource_entry(
                resource_type=resource_type, key=key, value=value
            ):
                continue
            section_name = _resource_section_name(resource_type, key)
            document["entries"][key] = {
                "resource_id": resource_id,
                "key": key,
                "value": value,
                "digest": entry.get("digest") or "",
                "locale": entry.get("locale") or source_locale,
                "content_kind": _content_kind(value),
            }
            section_hashes[section_name] = make_source_hash(value)
        if document["entries"]:
            docs.append((document, section_hashes))
    return docs


def _merge_resource_translation_document(
    *,
    source_document: dict[str, Any],
    previous_document: dict[str, Any] | None,
    partial_document: dict[str, Any],
    previous_hashes: dict[str, str] | None,
    partial_hashes: dict[str, str],
    target_locale: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    merged_document: dict[str, Any] = {
        "shop_domain": source_document["shop_domain"],
        "resource_group": GLOBAL_RESOURCE_GROUP,
        "resource_type": source_document["resource_type"],
        "resource_id": source_document["resource_id"],
        "source_locale": source_document["source_locale"],
        "target_locale": target_locale,
        "entries": dict((previous_document or {}).get("entries") or {}),
    }
    merged_document["entries"].update((partial_document or {}).get("entries") or {})

    source_sections = {
        _resource_section_name(source_document["resource_type"], key)
        for key in source_document.get("entries", {})
    }
    source_keys = set(source_document.get("entries", {}).keys())
    merged_hashes = {
        key: value for key, value in dict(previous_hashes or {}).items() if key in source_sections
    }
    merged_hashes.update(partial_hashes)
    merged_document["entries"] = {
        key: value for key, value in merged_document["entries"].items() if key in source_keys
    }
    return merged_document, merged_hashes


def translate_resource_document(
    *,
    source_document: dict[str, Any],
    changed_sections: set[str],
    target_locale: str,
    translator: Translator,
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]]:
    dnt_path = (
        SETTINGS.do_not_translate_path if getattr(SETTINGS, "do_not_translate_path", None) else None
    )
    dnt = load_do_not_translate(dnt_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]

    translated_document: dict[str, Any] = {
        "shop_domain": source_document["shop_domain"],
        "resource_group": GLOBAL_RESOURCE_GROUP,
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
        section_name = _resource_section_name(source_document["resource_type"], key)
        if section_name not in changed_sections:
            continue
        if entry["content_kind"] in {"html", "liquid"}:
            translated_value = translator.translate_html_document(
                source_document["resource_type"],
                key,
                entry["value"],
                target_locale,
                dnt,
                exclude_tokens,
            )
            section_sources[section_name] = "translator_html_document"
        else:
            translated_value = translator.translate_plain(
                source_document["resource_type"],
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


def build_resource_payloads_from_stored_translation(
    *,
    source_document: dict[str, Any],
    stored_document: dict[str, Any],
    target_locale: str,
    changed_sections: set[str],
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]] | None:
    stored_entries = dict((stored_document or {}).get("entries") or {})
    if not stored_entries:
        return None

    translated_document: dict[str, Any] = {
        "shop_domain": source_document["shop_domain"],
        "resource_group": GLOBAL_RESOURCE_GROUP,
        "resource_type": source_document["resource_type"],
        "resource_id": source_document["resource_id"],
        "source_locale": source_document["source_locale"],
        "target_locale": target_locale,
        "entries": {},
    }
    translated_hashes: dict[str, str] = {}
    payloads: list[dict] = []
    section_sources: dict[str, str] = {}

    prefix = source_document["resource_type"] + "."
    for section_name in changed_sections:
        key = (
            section_name[len(prefix) :]
            if section_name.startswith(prefix)
            else section_name.split(".", 1)[1]
        )
        source_entry = source_document["entries"].get(key)
        if not source_entry or key not in stored_entries:
            return None
        translated_value = str(stored_entries[key])
        translated_document["entries"][key] = translated_value
        translated_hashes[section_name] = make_source_hash(source_entry["value"])
        payloads.append(
            {
                "resource_id": source_entry["resource_id"],
                "key": key,
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": source_entry["digest"],
            }
        )
        section_sources[section_name] = "stored_translation_state"

    return translated_document, translated_hashes, payloads, section_sources


def _build_resource_log_event(item_summary: dict[str, Any]) -> dict[str, Any]:
    locales = item_summary.get("locales") or {}
    return {
        "ok": True,
        "resource_group": GLOBAL_RESOURCE_GROUP,
        "resource_type": item_summary.get("resource_type"),
        "resource_id": item_summary.get("resource_id"),
        "status": item_summary.get("status") or "unknown",
        "changed_sections": item_summary.get("changed_sections") or [],
        "target_locales": sorted(list(locales.keys())),
        "translated_sections": {
            locale: data.get("translated_sections") or [] for locale, data in locales.items()
        },
        "section_sources": {
            locale: data.get("section_sources") or {} for locale, data in locales.items()
        },
    }


async def bootstrap_resources(
    *,
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    resource_types: list[str] | None = None,
    resource_ids: list[str] | None = None,
) -> dict[str, Any]:
    store = NeonTranslationStore()
    store.ensure_schema()
    cache = TranslationCache(db_path=":memory:" if dry_run else None)
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)
    requested_resource_types = resource_types or list(DEFAULT_RESOURCE_TYPES)

    summary: dict[str, Any] = {
        "resource_group": GLOBAL_RESOURCE_GROUP,
        "resource_types": requested_resource_types,
        "resources": 0,
        "changed_resources": 0,
        "changed_sections": 0,
        "registered": 0,
        "target_locales": target_locales,
        "items": [],
    }

    try:
        bundle = await fetch_global_resource_bundle(
            resource_types=requested_resource_types,
            resource_ids=resource_ids,
        )
        docs = build_global_resource_documents(
            shop_domain=SETTINGS.shopify_domain,
            source_locale=source_locale,
            bundle=bundle,
        )
        for source_document, section_hashes in docs:
            resource_type = source_document["resource_type"]
            resource_id = source_document["resource_id"]
            previous_hashes = store.get_theme_source_hashes(
                shop_domain=SETTINGS.shopify_domain,
                theme_id=GLOBAL_RESOURCE_GROUP,
                resource_type=resource_type,
                resource_id=resource_id,
                source_locale=source_locale,
            )
            changed_sections = {
                name
                for name, source_hash in section_hashes.items()
                if previous_hashes.get(name) != source_hash
            }
            store.upsert_theme_source(
                ThemeSourceRecord(
                    shop_domain=SETTINGS.shopify_domain,
                    theme_id=GLOBAL_RESOURCE_GROUP,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    source_locale=source_locale,
                    document=source_document,
                    section_hashes=section_hashes,
                    metadata={"resource_group": GLOBAL_RESOURCE_GROUP},
                )
            )
            summary["resources"] += 1
            item_summary: dict[str, Any] = {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "changed_sections": sorted(list(changed_sections)),
                "locales": {},
            }
            previous_translations = {}
            pending_sync_locales = set()
            for target_locale in target_locales:
                state = store.get_theme_translation_state(
                    shop_domain=SETTINGS.shopify_domain,
                    theme_id=GLOBAL_RESOURCE_GROUP,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    target_locale=target_locale,
                )
                previous_translations[target_locale] = state
                if (
                    apply_translations
                    and not dry_run
                    and (state is None or state.status != "synced")
                ):
                    pending_sync_locales.add(target_locale)

            if not changed_sections and not pending_sync_locales:
                item_summary["status"] = "unchanged"
                summary["items"].append(item_summary)
                logger.info("resource_translation", **_build_resource_log_event(item_summary))
                continue

            if changed_sections:
                summary["changed_resources"] += 1
                summary["changed_sections"] += len(changed_sections)

            for target_locale in target_locales:
                previous_translation = previous_translations.get(target_locale)
                locale_changed_sections = set(changed_sections)
                if previous_translation is None:
                    locale_changed_sections.update(section_hashes.keys())
                elif previous_translation.status != "synced":
                    if (
                        not changed_sections
                        and previous_translation.section_hashes == section_hashes
                    ):
                        locale_changed_sections.update(section_hashes.keys())
                    else:
                        for section_name, source_hash in section_hashes.items():
                            if previous_translation.section_hashes.get(section_name) != source_hash:
                                locale_changed_sections.add(section_name)
                else:
                    for section_name, source_hash in section_hashes.items():
                        if previous_translation.section_hashes.get(section_name) != source_hash:
                            locale_changed_sections.add(section_name)

                if not locale_changed_sections:
                    continue

                reused = None
                if previous_translation is not None and previous_translation.status != "synced":
                    reused = build_resource_payloads_from_stored_translation(
                        source_document=source_document,
                        stored_document=previous_translation.document or {},
                        target_locale=target_locale,
                        changed_sections=locale_changed_sections,
                    )

                if reused is not None:
                    partial_document, partial_hashes, payloads, section_sources = reused
                else:
                    partial_document, partial_hashes, payloads, section_sources = (
                        translate_resource_document(
                            source_document=source_document,
                            changed_sections=locale_changed_sections,
                            target_locale=target_locale,
                            translator=translator,
                        )
                    )

                translated_document, translated_hashes = _merge_resource_translation_document(
                    source_document=source_document,
                    previous_document=(
                        previous_translation.document if previous_translation else None
                    ),
                    partial_document=partial_document,
                    previous_hashes=(
                        previous_translation.section_hashes if previous_translation else None
                    ),
                    partial_hashes=partial_hashes,
                    target_locale=target_locale,
                )

                for section_name in locale_changed_sections:
                    prefix = resource_type + "."
                    key = (
                        section_name[len(prefix) :]
                        if section_name.startswith(prefix)
                        else section_name.split(".", 1)[1]
                    )
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
                        metadata={
                            "resource_group": GLOBAL_RESOURCE_GROUP,
                            "resource_id": resource_id,
                        },
                    )

                translation_status = "translated"
                translation_metadata: dict[str, Any] = {
                    "changed_sections": sorted(list(locale_changed_sections))
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
                    if os.environ.get("LOG_VERBOSE_SYNC", "false").lower() in {
                        "1",
                        "true",
                        "yes",
                        "y",
                    }:
                        locale_summary["shopify_payloads"] = shopify_payloads

                store.upsert_theme_translation(
                    ThemeTranslationRecord(
                        shop_domain=SETTINGS.shopify_domain,
                        theme_id=GLOBAL_RESOURCE_GROUP,
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
            item_summary["status"] = (
                "failed"
                if any(s == "failed" for s in statuses)
                else (
                    "synced" if statuses and all(s == "synced" for s in statuses) else "translated"
                )
            )
            summary["items"].append(item_summary)
            logger.info("resource_translation", **_build_resource_log_event(item_summary))
    finally:
        cache.close()
        store.close()

    logger.info(
        "resource_translation_summary",
        resources=summary["resources"],
        changed_resources=summary["changed_resources"],
        changed_sections=summary["changed_sections"],
        registered=summary["registered"],
        resource_types=summary["resource_types"],
        target_locales=summary["target_locales"],
    )
    return summary

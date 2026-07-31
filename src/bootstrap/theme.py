from __future__ import annotations

import os
from typing import Any

import structlog

from src.config.dnt_loader import load_do_not_translate
from src.config.settings import SETTINGS
from src.config.theme_policies import should_translate_theme_entry
from src.shopify.graphql import (
    get_main_theme,
    get_resource_translations_by_ids,
    list_translatable_resources,
    register_translations,
)
from src.state.neon import (
    NeonTranslationStore,
    ThemeSourceRecord,
    ThemeTranslationRecord,
    make_source_hash,
)
from src.translate.cache import TranslationCache
from src.translate.translator import Translator

logger = structlog.get_logger("theme")

THEME_RESOURCE_TYPES = [
    "ONLINE_STORE_THEME_JSON_TEMPLATE",
    "ONLINE_STORE_THEME_SECTION_GROUP",
    "ONLINE_STORE_THEME_LOCALE_CONTENT",
]


class ThemeSafetyError(RuntimeError):
    pass


def _numeric_theme_id(theme_id: str | int) -> str:
    value = str(theme_id or "").strip()
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    return value


def _resource_belongs_to_theme(resource_id: str, theme_id: str | int) -> bool:
    numeric_theme_id = _numeric_theme_id(theme_id)
    value = str(resource_id or "").rstrip("/")
    return (
        value.endswith(f"/{numeric_theme_id}")
        or f"theme_id={numeric_theme_id}" in value
        or f"themeId={numeric_theme_id}" in value
    )


async def assert_approved_main_theme(theme_id: str | int) -> dict[str, str]:
    expected_id = _numeric_theme_id(theme_id)
    main_theme = await get_main_theme()
    actual_id = _numeric_theme_id(main_theme.get("id") or "")
    if not expected_id:
        raise ThemeSafetyError("Theme sync blocked: approved theme ID is missing")
    if not actual_id:
        raise ThemeSafetyError("Theme sync blocked: Shopify MAIN theme ID is missing")
    if expected_id != actual_id:
        raise ThemeSafetyError(
            "Theme sync blocked: approved theme "
            f"{expected_id} does not match MAIN theme {actual_id} ({main_theme.get('name') or 'unknown'})"
        )
    return main_theme


def _theme_section_name(resource_type: str, key: str) -> str:
    return f"{resource_type}.{key}"


def _theme_reuse_key(
    *,
    target_locale: str,
    key: str,
    value: str,
    content_kind: str,
) -> tuple[str, str, str, str]:
    semantic_key = key.split(":", 1)[0].rsplit(".", 1)[-1]
    return (target_locale, semantic_key, content_kind, value.strip())


def _build_theme_log_event(item_summary: dict[str, Any]) -> dict[str, Any]:
    locales = item_summary.get("locales") or {}
    return {
        "ok": True,
        "theme_id": item_summary.get("theme_id"),
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


def _build_theme_verbose_log_event(item_summary: dict[str, Any]) -> dict[str, Any] | None:
    locales = item_summary.get("locales") or {}
    payloads = {
        locale: data.get("shopify_payloads")
        for locale, data in locales.items()
        if data.get("shopify_payloads")
    }
    if not payloads:
        return None
    return {
        "theme_id": item_summary.get("theme_id"),
        "resource_type": item_summary.get("resource_type"),
        "resource_id": item_summary.get("resource_id"),
        "shopify_payloads": payloads,
    }


async def fetch_theme_source_bundle(
    *,
    resource_types: list[str] | None = None,
    first: int = 250,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for resource_type in resource_types or THEME_RESOURCE_TYPES:
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


async def audit_theme_translations(
    *,
    theme_id: str | int,
    target_locales: list[str],
    source_locale: str,
    resource_types: list[str] | None = None,
    include_items: bool = False,
    key_filter: str | None = None,
) -> dict[str, Any]:
    """Read Shopify theme sources/translations without touching Neon or Shopify."""
    main_theme = await assert_approved_main_theme(theme_id)
    bundle = await fetch_theme_source_bundle(resource_types=resource_types)
    docs = build_theme_documents(
        shop_domain=SETTINGS.shopify_domain,
        theme_id=_numeric_theme_id(theme_id),
        source_locale=source_locale,
        bundle=bundle,
    )
    resource_ids = [document["resource_id"] for document, _hashes in docs]
    translations_by_locale = {
        locale: await get_resource_translations_by_ids(resource_ids, locale)
        for locale in target_locales
    }
    summary: dict[str, Any] = {
        "ok": True,
        "read_only": True,
        "theme_id": _numeric_theme_id(theme_id),
        "theme_name": main_theme.get("name") or "",
        "theme_role": main_theme.get("role") or "",
        "theme_updated_at": main_theme.get("updated_at") or "",
        "resource_types": resource_types or THEME_RESOURCE_TYPES,
        "resources": len(docs),
        "source_fields": sum(
            1
            for document, _hashes in docs
            for key in document.get("entries") or {}
            if not key_filter or key_filter in key
        ),
        "key_filter": key_filter,
        "locales": {},
    }
    for locale in target_locales:
        locale_summary: dict[str, Any] = {
            "current": 0,
            "missing": 0,
            "outdated": 0,
            "items": [],
        }
        remote_resources = translations_by_locale.get(locale) or {}
        for document, _hashes in docs:
            resource_id = str(document["resource_id"])
            remote = remote_resources.get(resource_id) or {}
            for key in document.get("entries") or {}:
                if key_filter and key_filter not in key:
                    continue
                translated = remote.get(key)
                if translated is None or not str(translated.get("value") or "").strip():
                    status = "missing"
                elif translated.get("outdated"):
                    status = "outdated"
                else:
                    status = "current"
                locale_summary[status] += 1
                if include_items and (status != "current" or key_filter):
                    locale_summary["items"].append(
                        {
                            "resource_type": document["resource_type"],
                            "resource_id": resource_id,
                            "key": key,
                            "source_value": str(
                                (document.get("entries") or {}).get(key, {}).get("value") or ""
                            ),
                            "translation_value": str((translated or {}).get("value") or ""),
                            "status": status,
                        }
                    )
        if not include_items:
            locale_summary.pop("items", None)
        summary["locales"][locale] = locale_summary
    return summary


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
        if not _resource_belongs_to_theme(str(resource_id), theme_id):
            continue
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
            content_kind = (
                "html"
                if "<" in value and ">" in value
                else ("liquid" if "{{" in value or "{%" in value else "plain")
            )
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
    translation_reuse: dict[tuple[str, str, str, str], str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]]:
    dnt_path = (
        SETTINGS.do_not_translate_path if getattr(SETTINGS, "do_not_translate_path", None) else None
    )
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
        reuse_key = _theme_reuse_key(
            target_locale=target_locale,
            key=key,
            value=entry["value"],
            content_kind=entry["content_kind"],
        )
        translated_value = (translation_reuse or {}).get(reuse_key)
        if translated_value:
            section_sources[section_name] = "current_shopify_translation_reuse"
        elif entry["content_kind"] == "html" or entry["content_kind"] == "liquid":
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
        if translation_reuse is not None:
            translation_reuse[reuse_key] = translated_value

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


def build_theme_payloads_from_stored_translation(
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

    for section_name in changed_sections:
        _, key = section_name.split(".", 1)
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


async def bootstrap_theme(
    *,
    theme_id: str | int,
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    resource_types: list[str] | None = None,
    require_main_theme: bool = True,
    max_translations: int | None = None,
    force_key_fragments: list[str] | None = None,
) -> dict[str, Any]:
    if require_main_theme and apply_translations:
        main_theme = await assert_approved_main_theme(theme_id)
        logger.info(
            "theme_main_guard_passed",
            theme_id=_numeric_theme_id(theme_id),
            theme_name=main_theme.get("name") or "",
            updated_at=main_theme.get("updated_at") or "",
        )

    store = NeonTranslationStore()
    if not dry_run:
        store.ensure_schema()
    cache = TranslationCache(db_path=":memory:" if dry_run else None)
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)

    summary: dict[str, Any] = {
        "theme_id": str(theme_id),
        "resource_types": resource_types or THEME_RESOURCE_TYPES,
        "resources": 0,
        "changed_resources": 0,
        "changed_sections": 0,
        "registered": 0,
        "would_register": 0,
        "dry_run": dry_run,
        "state_persisted": not dry_run,
        "max_translations": max_translations,
        "force_key_fragments": force_key_fragments or [],
        "target_locales": target_locales,
        "items": [],
    }
    remaining_translations = max_translations
    forced_fragments = [
        fragment.strip() for fragment in (force_key_fragments or []) if fragment.strip()
    ]

    def is_forced_key(key: str) -> bool:
        return any(fragment in key for fragment in forced_fragments)

    try:
        bundle = await fetch_theme_source_bundle(resource_types=resource_types)
        docs = build_theme_documents(
            shop_domain=SETTINGS.shopify_domain,
            theme_id=str(theme_id),
            source_locale=source_locale,
            bundle=bundle,
        )
        remote_translations: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        translation_reuse: dict[tuple[str, str, str, str], str] = {}
        if apply_translations and docs:
            resource_ids = [document["resource_id"] for document, _hashes in docs]
            remote_translations = {
                locale: await get_resource_translations_by_ids(resource_ids, locale)
                for locale in target_locales
            }
            for target_locale in target_locales:
                locale_resources = remote_translations.get(target_locale) or {}
                for source_document, _section_hashes in docs:
                    remote = locale_resources.get(source_document["resource_id"]) or {}
                    for key, entry in source_document.get("entries", {}).items():
                        if is_forced_key(key):
                            continue
                        translated = remote.get(key) or {}
                        translated_value = str(translated.get("value") or "").strip()
                        if translated_value and not translated.get("outdated"):
                            translation_reuse[
                                _theme_reuse_key(
                                    target_locale=target_locale,
                                    key=key,
                                    value=entry["value"],
                                    content_kind=entry["content_kind"],
                                )
                            ] = translated_value
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
                name
                for name, source_hash in section_hashes.items()
                if previous_hashes.get(name) != source_hash
            }
            if not dry_run:
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
            pending_remote_sections: dict[str, set[str]] = {}
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
                if apply_translations:
                    remote = (remote_translations.get(target_locale) or {}).get(resource_id) or {}
                    pending_remote_sections[target_locale] = {
                        section_name
                        for section_name in section_hashes
                        if (
                            is_forced_key(section_name.split(".", 1)[1])
                            or (translated := remote.get(section_name.split(".", 1)[1])) is None
                            or not str(translated.get("value") or "").strip()
                            or bool(translated.get("outdated"))
                        )
                    }
                if pending_remote_sections.get(target_locale):
                    pending_sync_locales.add(target_locale)

            if (apply_translations and not pending_sync_locales) or (
                not apply_translations and not changed_sections
            ):
                item_summary["status"] = "unchanged"
                summary["items"].append(item_summary)
                logger.info("theme_translation", **_build_theme_log_event(item_summary))
                continue

            if changed_sections:
                summary["changed_resources"] += 1
                summary["changed_sections"] += len(changed_sections)

            for target_locale in target_locales:
                previous_translation = previous_translations.get(target_locale)
                if apply_translations:
                    locale_changed_sections = set(
                        pending_remote_sections.get(target_locale) or set()
                    )
                else:
                    locale_changed_sections = set(changed_sections)
                    if previous_translation is None or previous_translation.status != "synced":
                        locale_changed_sections.update(section_hashes.keys())
                    else:
                        for section_name, source_hash in section_hashes.items():
                            if previous_translation.section_hashes.get(section_name) != source_hash:
                                locale_changed_sections.add(section_name)

                if remaining_translations is not None:
                    locale_changed_sections = set(
                        sorted(locale_changed_sections)[: max(0, remaining_translations)]
                    )
                if not locale_changed_sections:
                    continue

                reused = None
                if (
                    previous_translation is not None
                    and previous_translation.status != "synced"
                    and previous_translation.section_hashes == section_hashes
                ):
                    reused = build_theme_payloads_from_stored_translation(
                        source_document=source_document,
                        stored_document=previous_translation.document or {},
                        target_locale=target_locale,
                        changed_sections=locale_changed_sections,
                    )

                if reused is not None:
                    translated_document, translated_hashes, payloads, section_sources = reused
                else:
                    translated_document, translated_hashes, payloads, section_sources = (
                        translate_theme_document(
                            source_document=source_document,
                            changed_sections=locale_changed_sections,
                            target_locale=target_locale,
                            translator=translator,
                            translation_reuse=translation_reuse,
                        )
                    )

                if not dry_run:
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
                if apply_translations and dry_run:
                    summary["would_register"] += len(payloads)
                    translation_status = "planned"
                elif apply_translations:
                    shopify_payloads = [
                        {
                            "key": item["key"],
                            "locale": item["locale"],
                            "value": item["value"],
                            "translatableContentDigest": item["translatableContentDigest"],
                        }
                        for item in payloads
                    ]
                    user_errors = (
                        await register_translations(resource_id, shopify_payloads)
                        if shopify_payloads
                        else []
                    )
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

                if not dry_run:
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
                if remaining_translations is not None:
                    remaining_translations -= len(payloads)
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
            logger.info("theme_translation", **_build_theme_log_event(item_summary))
            if os.environ.get("LOG_VERBOSE_SYNC", "false").lower() in {"1", "true", "yes", "y"}:
                verbose = _build_theme_verbose_log_event(item_summary)
                if verbose:
                    logger.info("theme_translation_debug", **verbose)
    finally:
        cache.close()
        store.close()

    logger.info(
        "theme_translation_summary",
        theme_id=str(theme_id),
        resources=summary["resources"],
        changed_resources=summary["changed_resources"],
        changed_sections=summary["changed_sections"],
        registered=summary["registered"],
        target_locales=summary["target_locales"],
    )
    return summary

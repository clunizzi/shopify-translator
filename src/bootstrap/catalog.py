from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import structlog

from src.bootstrap.dictionary import resolve_dictionary_first, resolve_memory_second
from src.config.dnt_loader import load_do_not_translate
from src.config.field_policies import (
    DEFAULT_HANDLE_POLICY,
    HandlePolicy,
    should_translate_product_key,
)
from src.config.metafield_policies import make_metafield_leaf_filter, should_translate_metafield
from src.config.settings import SETTINGS
from src.rules.option_value import should_skip_option_name, should_skip_option_value_name
from src.shopify.graphql import (
    get_product_all_metafields,
    get_product_metafields_by_keys,
    get_product_option_resources,
    get_resource_translations_by_ids,
    get_translatable_by_ids,
    make_product_gid,
    register_translations,
)
from src.state.neon import (
    NeonTranslationStore,
    PDPSourceRecord,
    PDPTranslationRecord,
    PDPTranslationState,
    make_source_hash,
)
from src.translate.cache import TranslationCache
from src.translate.similarity import normalize_text
from src.translate.translator import (
    Translator,
    detect_lang_fast,
    is_technical_value,
    translation_output_issue,
)
from src.translate.validators import make_handle_from_title

AUTO_TYPES = [
    "single_line_text_field",
    "multi_line_text_field",
    "json",
    "rich_text_field",
    "html",
]

logger = structlog.get_logger("bootstrap")
ITALIAN_PRODUCT_TITLE_HINT_RE = re.compile(
    r"(^|[^a-z])(motocoltivatore|motosega|soffiatore|decespugliatore|arieggiatore|tagliasiepi|trattorino|trattore|compatto|rasaerba|biotrituratore|pompa|fresa|elettric[oa]|scoppio|usato|inclus[oaie])($|[^a-z])",
    re.IGNORECASE,
)


class _ReadOnlyTranslationStore:
    """Read-through store that suppresses every state mutation."""

    def __init__(self, store: NeonTranslationStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def upsert_pdp_source(self, record: PDPSourceRecord) -> None:
        return None

    def upsert_pdp_translation(self, record: PDPTranslationRecord) -> None:
        return None

    def upsert_translation_memory(self, **kwargs: Any) -> None:
        return None

    def upsert_dictionary_translation(self, **kwargs: Any) -> None:
        return None


def _section_name_product(key: str) -> str:
    return f"product.{key}"


def _section_name_metafield(full_key: str) -> str:
    return f"metafield.{full_key}"


def _section_name_option(entry_key: str) -> str:
    return f"option.{entry_key}"


def _field_snippet(value: object, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _should_reject_product_title_memory_hit(source_value: str, translated_value: str) -> bool:
    source = (source_value or "").strip()
    translated = (translated_value or "").strip()
    if not source or not translated:
        return False
    if normalize_text(source) != normalize_text(translated):
        return False
    if is_technical_value(source):
        return False
    lang, conf = detect_lang_fast(source)
    return (lang == "it" and conf >= 0.40) or bool(ITALIAN_PRODUCT_TITLE_HINT_RE.search(source))


def _merge_pdp_translation_document(
    *,
    source_document: dict[str, Any],
    previous_document: dict[str, Any] | None,
    partial_document: dict[str, Any],
    previous_hashes: dict[str, str] | None,
    partial_hashes: dict[str, str],
    target_locale: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    merged_document: dict[str, Any] = {
        "product_gid": source_document["product_gid"],
        "shop_domain": source_document["shop_domain"],
        "source_locale": source_document["source_locale"],
        "target_locale": target_locale,
        "product": dict((previous_document or {}).get("product") or {}),
        "metafields": dict((previous_document or {}).get("metafields") or {}),
        "options": dict((previous_document or {}).get("options") or {}),
    }
    for section in ("product", "metafields", "options"):
        merged_document[section].update((partial_document or {}).get(section) or {})

    source_sections = set()
    source_sections.update(_section_name_product(key) for key in source_document.get("product", {}))
    source_sections.update(
        _section_name_metafield(key) for key in source_document.get("metafields", {})
    )
    source_sections.update(_section_name_option(key) for key in source_document.get("options", {}))

    merged_hashes = {
        key: value for key, value in dict(previous_hashes or {}).items() if key in source_sections
    }
    merged_hashes.update(partial_hashes)

    merged_document["product"] = {
        key: value
        for key, value in merged_document["product"].items()
        if _section_name_product(key) in source_sections
    }
    merged_document["metafields"] = {
        key: value
        for key, value in merged_document["metafields"].items()
        if _section_name_metafield(key) in source_sections
    }
    merged_document["options"] = {
        key: value
        for key, value in merged_document["options"].items()
        if _section_name_option(key) in source_sections
    }

    return merged_document, merged_hashes


async def fetch_product_source_bundle(
    product_numeric_id: str | int,
    mf_include: list[tuple[str, str]] | None = None,
    target_locales: list[str] | None = None,
) -> tuple[
    str,
    list[dict],
    dict[str, list[dict]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
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
    translations_by_locale: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}

    async def _fetch_locale(locale: str) -> tuple[str, dict[str, dict[str, dict[str, Any]]]]:
        translations = await get_resource_translations_by_ids(resource_ids, locale)
        return locale, translations

    locale_results = await asyncio.gather(
        *[_fetch_locale(locale) for locale in (target_locales or [])]
    )
    translations_by_locale.update(locale_results)
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
    units: list[str] | None = None,
    handle_policy: HandlePolicy = DEFAULT_HANDLE_POLICY,
) -> tuple[dict[str, Any], dict[str, str]]:
    if units is None:
        dnt_path = (
            SETTINGS.do_not_translate_path
            if getattr(SETTINGS, "do_not_translate_path", None)
            else None
        )
        dnt = load_do_not_translate(dnt_path)
        units = dnt.units
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
    for resource_id, entries in live_map.items():
        if resource_id == product_gid:
            for entry in entries:
                key = entry.get("key") or ""
                if not should_translate_product_key(
                    key,
                    is_create=is_create,
                    existing_product=existing_product,
                    handle_policy=handle_policy,
                ):
                    continue
                section_name = _section_name_product(key)
                value = entry.get("value") or ""
                if not value.strip():
                    continue
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

        if resource_id.startswith("gid://shopify/ProductOption") or resource_id.startswith(
            "gid://shopify/ProductOptionValue"
        ):
            live_entry = next((x for x in entries if (x.get("key") or "") == "name"), None)
            if not live_entry:
                continue
            value = live_entry.get("value") or ""
            if not value.strip():
                continue
            kind = (
                "option_name"
                if resource_id.startswith("gid://shopify/ProductOption/")
                else "option_value"
            )
            if kind == "option_name":
                skip, _ = should_skip_option_name(value)
            else:
                skip, _ = should_skip_option_value_name(value, units)
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
        if not value.strip():
            continue
        mf_type = (meta.get("type") or "").lower()
        if mf_type in {"json", "rich_text_field"}:
            content_kind = "json"
        elif mf_type == "html":
            content_kind = "html"
        else:
            content_kind = "plain"
        document["metafields"][full_key] = {
            "resource_id": resource_id,
            "namespace": namespace,
            "key": key,
            "full_key": full_key,
            "metafield_type": mf_type,
            "content_kind": content_kind,
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
        issue = translation_output_issue(
            source,
            resolved.translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
        if not issue:
            return (resolved.translated_value, resolved.source)
        logger.warning(
            "reject_product_type_dictionary_hit",
            target_locale=target_locale,
            reason=issue,
        )

    mem = resolve_memory_second(
        store,
        field_key="product.product_type",
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
    )
    if mem:
        issue = translation_output_issue(
            source,
            mem.translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
        if not issue:
            return (mem.translated_value, mem.source)
        logger.warning(
            "reject_product_type_memory_hit",
            target_locale=target_locale,
            reason=issue,
        )

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
    translated = _nonblank_translation(translated)
    if translated is None:
        raise RuntimeError("Translator returned a blank product_type")
    return (translated, "translator")


def _translate_product_title(
    store: NeonTranslationStore,
    translator: Translator,
    source_value: str,
    *,
    source_locale: str,
    target_locale: str,
    dnt,
    exclude_tokens: list[str],
) -> tuple[str, str]:
    source = (source_value or "").strip()
    if not source:
        return ("", "empty")

    mem = resolve_memory_second(
        store,
        field_key="product.title",
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
    )
    if mem:
        issue = translation_output_issue(
            source,
            mem.translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
        if issue:
            logger.warning(
                "reject_product_title_memory_hit",
                source_snippet=_field_snippet(source),
                target_locale=target_locale,
                reason=issue,
            )
        elif _should_reject_product_title_memory_hit(source, mem.translated_value):
            logger.warning(
                "reject_product_title_memory_hit_same_as_source",
                source_snippet=_field_snippet(source),
                target_locale=target_locale,
            )
        else:
            return (mem.translated_value, mem.source)

    translated = translator.translate_plain(
        "PRODUCT",
        "title",
        source,
        target_locale,
        dnt,
        exclude_tokens,
    )
    translated = _nonblank_translation(translated)
    if translated is None:
        raise RuntimeError("Translator returned a blank product title")
    return (translated, "translator")


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
        issue = translation_output_issue(
            source,
            resolved.translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
        if not issue:
            return (resolved.translated_value, resolved.source)
        logger.warning(
            "reject_dictionary_hit",
            category=category,
            target_locale=target_locale,
            reason=issue,
        )

    mem = resolve_memory_second(
        store,
        field_key=memory_key,
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
    )
    if mem:
        issue = translation_output_issue(
            source,
            mem.translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
        if not issue:
            return (mem.translated_value, mem.source)
        logger.warning(
            "reject_memory_hit",
            field_key=memory_key,
            target_locale=target_locale,
            reason=issue,
        )

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
    translated = _nonblank_translation(translated)
    if translated is None:
        raise RuntimeError(f"Translator returned a blank value for {memory_key}")
    return (translated, "translator")


def _translate_json_metafield(
    translator: Translator,
    entry: dict[str, Any],
    *,
    target_locale: str,
    dnt,
    exclude_tokens: list[str],
) -> str:
    if entry.get("metafield_type") == "rich_text_field":
        def leaf_filter(path, value):
            return (next(
                        (part for part in reversed(path) if isinstance(part, str)),
                        "",
                    )
                    == "value")
    else:
        leaf_filter = make_metafield_leaf_filter(entry["namespace"], entry["key"])
    return translator.translate_json_value(
        "METAFIELD",
        "value",
        entry["value"],
        target_locale,
        dnt,
        exclude_tokens,
        should_translate_leaf=leaf_filter,
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


def _nonblank_translation(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _shopify_translation_value(
    translations: dict[str, Any] | None,
    *,
    resource_id: str,
    key: str,
) -> str | None:
    """Read both the new resource-aware shape and the legacy product-only shape."""
    data = translations or {}
    resource = data.get(resource_id)
    item: object | None = None
    if isinstance(resource, dict):
        item = resource.get(key)
    elif key in data:
        item = data.get(key)

    if isinstance(item, dict):
        if bool(item.get("outdated")):
            return None
        return _nonblank_translation(item.get("value"))
    return _nonblank_translation(item)


def _require_nonblank_translation(section_name: str, translated_value: object) -> str:
    translated = _nonblank_translation(translated_value)
    if translated is None:
        raise RuntimeError(f"Translator returned a blank value for {section_name}")
    return translated


def _json_leaf_filter(entry: dict[str, Any]):
    if entry.get("metafield_type") == "rich_text_field":
        return (
            lambda path, value: next(
                (part for part in reversed(path) if isinstance(part, str)),
                "",
            )
            == "value"
        )
    return make_metafield_leaf_filter(entry.get("namespace", ""), entry.get("key", ""))


def _is_valid_json_translation(
    source_entry: dict[str, Any],
    translated_value: str,
    *,
    target_locale: str = "",
    dnt=None,
) -> bool:
    try:
        source_obj = json.loads(source_entry.get("value") or "")
        translated_obj = json.loads(translated_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    leaf_filter = _json_leaf_filter(source_entry)

    def _matches(source: Any, translated: Any, path: tuple = ()) -> bool:
        if type(source) is not type(translated):
            return False
        if isinstance(source, dict):
            return set(source) == set(translated) and all(
                _matches(source[key], translated[key], path + (key,)) for key in source
            )
        if isinstance(source, list):
            return len(source) == len(translated) and all(
                _matches(source_item, translated_item, path + (index,))
                for index, (source_item, translated_item) in enumerate(
                    zip(source, translated, strict=False)
                )
            )
        if isinstance(source, str):
            source_text = source.strip()
            if not source_text:
                return translated == source
            if not translated.strip():
                return False
            must_be_preserved = (
                not leaf_filter(path, source)
                or is_technical_value(source_text)
                or source_text.lower().startswith(("http://", "https://"))
            )
            if must_be_preserved:
                return translated == source
            return (
                translation_output_issue(
                    source,
                    translated,
                    target_locale=target_locale,
                    dnt=dnt,
                )
                is None
            )
        return translated == source

    return _matches(source_obj, translated_obj)


def _is_valid_entry_translation(
    source_entry: dict[str, Any],
    translated_value: str,
    *,
    target_locale: str,
    dnt,
) -> bool:
    if source_entry.get("content_kind") == "json":
        return _is_valid_json_translation(
            source_entry,
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
    return (
        translation_output_issue(
            source_entry.get("value") or "",
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        )
        is None
    )


def translate_pdp_document(
    *,
    store: NeonTranslationStore,
    source_document: dict[str, Any],
    changed_sections: set[str],
    target_locale: str,
    source_locale: str,
    translator: Translator,
    existing_product: bool,
    existing_shopify_translations: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]]:
    dnt_path = (
        SETTINGS.do_not_translate_path if getattr(SETTINGS, "do_not_translate_path", None) else None
    )
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
                existing_translation=_shopify_translation_value(
                    existing_shopify_translations,
                    resource_id=entry["resource_id"],
                    key="product_type",
                ),
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
            translated_value, source_kind = _translate_product_title(
                store,
                translator,
                entry["value"],
                source_locale=source_locale,
                target_locale=target_locale,
                dnt=dnt,
                exclude_tokens=exclude_tokens,
            )
            section_sources[section_name] = source_kind
            if key == "title":
                title_translated = translated_value

        translated_value = _require_nonblank_translation(section_name, translated_value)
        if not translator.dry_run and not _is_valid_entry_translation(
            entry,
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        ):
            raise RuntimeError(f"Unsafe translation output for {section_name}")
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
                memory_issue = (
                    translation_output_issue(
                        entry["value"],
                        mem.translated_value,
                        target_locale=target_locale,
                        dnt=dnt,
                    )
                    if mem
                    else None
                )
                if mem and not memory_issue:
                    translated_value = mem.translated_value
                    section_sources[section_name] = mem.source
                else:
                    if mem:
                        logger.warning(
                            "reject_metafield_memory_hit",
                            section=section_name,
                            target_locale=target_locale,
                            reason=memory_issue,
                        )
                    translated_value = translator.translate_plain(
                        "METAFIELD",
                        "value",
                        entry["value"],
                        target_locale,
                        dnt,
                        exclude_tokens,
                    )
                    section_sources[section_name] = "translator"

        translated_value = _require_nonblank_translation(section_name, translated_value)
        if not translator.dry_run and not _is_valid_entry_translation(
            entry,
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        ):
            raise RuntimeError(f"Unsafe translation output for {section_name}")
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
        translated_value = _require_nonblank_translation(section_name, translated_value)
        if not translator.dry_run and not _is_valid_entry_translation(
            entry,
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        ):
            raise RuntimeError(f"Unsafe translation output for {section_name}")
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
    dnt = load_do_not_translate(SETTINGS.do_not_translate_path)
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

        if not translated_value.strip():
            return None
        if not _is_valid_entry_translation(
            source_entry,
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        ):
            logger.warning(
                "reject_invalid_stored_translation",
                section=section_name,
                target_locale=target_locale,
            )
            return None
        translated_hashes[section_name] = make_source_hash(source_entry["value"])
        payload_key = source_entry["key"]
        if section_name.startswith("metafield."):
            payload_key = "value"
        payloads.append(
            {
                "resource_id": source_entry["resource_id"],
                "key": payload_key,
                "locale": target_locale,
                "value": translated_value,
                "translatableContentDigest": source_entry.get("digest") or "",
            }
        )
        section_sources[section_name] = "stored_translation_state"

    return translated_document, translated_hashes, payloads, section_sources


def build_pdp_payloads_from_shopify_translations(
    *,
    source_document: dict[str, Any],
    shopify_translations: dict[str, Any],
    target_locale: str,
    candidate_sections: set[str],
) -> tuple[
    tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]] | None,
    set[str],
]:
    dnt = load_do_not_translate(SETTINGS.do_not_translate_path)
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
    reused_sections: set[str] = set()

    for section_name in sorted(candidate_sections):
        destination: dict[str, Any]
        if section_name.startswith("product."):
            key = section_name.split(".", 1)[1]
            source_entry = source_document.get("product", {}).get(key)
            destination = translated_document["product"]
        elif section_name.startswith("metafield."):
            key = section_name.split(".", 1)[1]
            source_entry = source_document.get("metafields", {}).get(key)
            destination = translated_document["metafields"]
        elif section_name.startswith("option."):
            key = section_name.split(".", 1)[1]
            source_entry = source_document.get("options", {}).get(key)
            destination = translated_document["options"]
        else:
            continue
        if not source_entry:
            continue
        translated_value = _shopify_translation_value(
            shopify_translations,
            resource_id=source_entry["resource_id"],
            key=source_entry["key"] if not section_name.startswith("metafield.") else "value",
        )
        if translated_value is None:
            continue
        if not _is_valid_entry_translation(
            source_entry,
            translated_value,
            target_locale=target_locale,
            dnt=dnt,
        ):
            logger.warning(
                "reject_invalid_shopify_translation",
                section=section_name,
                target_locale=target_locale,
            )
            continue

        destination[key] = translated_value
        translated_hashes[section_name] = make_source_hash(source_entry["value"])
        section_sources[section_name] = "shopify_existing_translation"
        reused_sections.add(section_name)

    if not reused_sections:
        return None, set()
    return (
        translated_document,
        translated_hashes,
        payloads,
        section_sources,
    ), reused_sections


def _merge_pdp_translation_partials(
    *partials: tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]] | None,
) -> tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]]:
    merged_document: dict[str, Any] = {
        "product": {},
        "metafields": {},
        "options": {},
    }
    merged_hashes: dict[str, str] = {}
    merged_payloads: list[dict] = []
    merged_sources: dict[str, str] = {}

    for partial in partials:
        if partial is None:
            continue
        translated_document, translated_hashes, payloads, section_sources = partial
        for top_key in ("product_gid", "shop_domain", "source_locale", "target_locale"):
            if translated_document.get(top_key) is not None:
                merged_document[top_key] = translated_document[top_key]
        for section in ("product", "metafields", "options"):
            merged_document[section].update((translated_document or {}).get(section) or {})
        merged_hashes.update(translated_hashes or {})
        merged_payloads.extend(payloads or [])
        merged_sources.update(section_sources or {})

    return merged_document, merged_hashes, merged_payloads, merged_sources


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
    handle_only: bool = False,
    continue_on_error: bool = True,
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
        "registered": 0,
        "failed_products": 0,
        "failed_product_ids": [],
        "target_locales": target_locales,
        "dry_run": dry_run,
        "state_persisted": not dry_run,
    }

    logger.info(
        "bootstrap_products_started",
        products=len(product_ids),
        target_locales=target_locales,
        apply_translations=apply_translations,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
    )

    try:
        for product_id in product_ids:
            try:
                logger.info("bootstrap_product_started", product_id=int(product_id))
                product_gid, metafields, live_map, existing_translations = (
                    await fetch_product_source_bundle(
                        product_id,
                        mf_include,
                        target_locales=target_locales,
                    )
                )
                logger.info(
                    "bootstrap_product_fetched",
                    product_id=int(product_id),
                    product_gid=product_gid,
                    metafields=len(metafields),
                    translatable_resources=len(live_map),
                    existing_translation_locales=sorted(existing_translations.keys()),
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
                    handle_only=handle_only,
                    summary=summary,
                    persist_state=not dry_run,
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

    logger.info(
        "bootstrap_products_summary",
        products=summary["products"],
        changed_products=summary["changed_products"],
        changed_sections=summary["changed_sections"],
        registered=summary["registered"],
        failed_products=summary["failed_products"],
        failed_product_ids=summary["failed_product_ids"],
        target_locales=summary["target_locales"],
        apply_translations=apply_translations,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
    )

    return summary


async def process_product_bundle(
    *,
    store: NeonTranslationStore,
    translator: Translator,
    product_id: int,
    product_gid: str,
    metafields: list[dict],
    live_map: dict[str, list[dict]],
    existing_translations: dict[str, dict[str, Any]],
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    existing_products: bool,
    is_create: bool,
    handle_only: bool = False,
    summary: dict[str, Any] | None = None,
    persist_state: bool | None = None,
    reconcile_shopify_drift: bool = False,
    content_changes_only: bool = False,
) -> dict[str, Any]:
    if persist_state is None:
        persist_state = not dry_run
    if not persist_state:
        store = _ReadOnlyTranslationStore(store)  # type: ignore[assignment]

    local_summary = (
        summary
        if summary is not None
        else {
            "products": 0,
            "changed_products": 0,
            "changed_sections": 0,
            "registered": 0,
            "target_locales": target_locales,
            "items": [],
        }
    )
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
        handle_policy=HandlePolicy(mode="always") if handle_only else DEFAULT_HANDLE_POLICY,
    )
    if handle_only:
        source_document["product"] = {
            key: value
            for key, value in source_document.get("product", {}).items()
            if key in {"title", "handle"}
        }
        source_document["metafields"] = {}
        source_document["options"] = {}
        section_hashes = {
            key: value
            for key, value in section_hashes.items()
            if key in {"product.title", "product.handle"}
        }
    previous_hashes = store.get_pdp_source_hashes(
        shop_domain=SETTINGS.shopify_domain,
        product_gid=product_gid,
        source_locale=source_locale,
    )
    changed_sections = {
        name
        for name, source_hash in section_hashes.items()
        if previous_hashes.get(name) != source_hash
    }
    removed_sections = set(previous_hashes) - set(section_hashes)
    source_state_changed = bool(changed_sections or removed_sections)

    item_summary: dict[str, Any] | None = None
    if not handle_only:
        local_summary["products"] += 1
        item_summary = {
            "product_id": int(product_id),
            "product_gid": product_gid,
            "product_title": (source_document.get("product", {}).get("title", {}) or {}).get(
                "value", ""
            ),
            "changed_sections": sorted(list(changed_sections)),
            "locales": {},
        }
        if content_changes_only and already_in_neon and not source_state_changed:
            item_summary["status"] = "unchanged"
            item_summary["skip_reason"] = "no_translatable_content_change"
            local_summary["items"].append(item_summary)
            return local_summary

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
    if handle_only:
        return await _process_product_handle_only(
            store=store,
            translator=translator,
            product_id=product_id,
            product_gid=product_gid,
            source_document=source_document,
            section_hashes=section_hashes,
            changed_sections=changed_sections,
            previous_hashes=previous_hashes,
            existing_translations=existing_translations,
            target_locales=target_locales,
            source_locale=source_locale,
            apply_translations=apply_translations,
            dry_run=dry_run,
            summary=local_summary,
        )

    assert item_summary is not None
    previous_translations = {
        target_locale: store.get_pdp_translation_state(
            shop_domain=SETTINGS.shopify_domain,
            product_gid=product_gid,
            target_locale=target_locale,
        )
        for target_locale in target_locales
    }
    missing_translation_sections = {
        target_locale: _missing_pdp_translation_sections(
            previous_translation=previous_translations.get(target_locale),
            source_document=source_document,
            section_hashes=section_hashes,
        )
        for target_locale in target_locales
    }
    shopify_current_sections_by_locale = {
        target_locale: build_pdp_payloads_from_shopify_translations(
            source_document=source_document,
            shopify_translations=existing_translations.get(target_locale, {}),
            target_locale=target_locale,
            candidate_sections=set(section_hashes),
        )[1]
        for target_locale in target_locales
    }
    shopify_drift_sections = {
        target_locale: set(section_hashes) - shopify_current_sections_by_locale[target_locale]
        for target_locale in target_locales
    }
    pending_translation_locales = {
        target_locale
        for target_locale, previous_translation in previous_translations.items()
        if (
            previous_translation is None
            or previous_translation.status not in {"translated", "synced"}
            or bool(missing_translation_sections[target_locale])
            or (
                (apply_translations or reconcile_shopify_drift)
                and bool(shopify_drift_sections[target_locale])
            )
        )
    }
    pending_sync_locales = {
        target_locale
        for target_locale, previous_translation in previous_translations.items()
        if apply_translations
        and (
            previous_translation is None
            or previous_translation.status != "synced"
            or bool(missing_translation_sections[target_locale])
            or bool(shopify_drift_sections[target_locale])
        )
    }
    if not changed_sections and not pending_translation_locales and not pending_sync_locales:
        item_summary["status"] = "unchanged"
        local_summary["items"].append(item_summary)
        return local_summary
    if changed_sections:
        local_summary["changed_products"] += 1
        local_summary["changed_sections"] += len(changed_sections)

    for target_locale in target_locales:
        previous_translation = previous_translations.get(target_locale)
        locale_changed_sections = set(changed_sections)
        locale_missing_sections = missing_translation_sections[target_locale]
        locale_shopify_drift_sections = shopify_drift_sections[target_locale]
        locale_reusable_sections: set[str] = set()
        if previous_translation is None:
            locale_changed_sections.update(section_hashes.keys())
        elif content_changes_only:
            # Webhook-driven sync must react only to source content changes.
            # Missing/outdated translations are repaired by explicit reconciliation,
            # never opportunistically because an order changed inventory.
            locale_changed_sections = set(changed_sections)
        elif not apply_translations and previous_translation.status not in {"translated", "synced"}:
            locale_changed_sections.update(section_hashes.keys())
        else:
            locale_changed_sections.update(locale_missing_sections)
            if apply_translations or reconcile_shopify_drift:
                locale_changed_sections.update(locale_shopify_drift_sections)
            for section_name, source_hash in section_hashes.items():
                if previous_translation.section_hashes.get(section_name) != source_hash:
                    locale_changed_sections.add(section_name)
                elif (
                    apply_translations
                    and previous_translation.status != "synced"
                    and section_name not in locale_missing_sections
                ):
                    locale_reusable_sections.add(section_name)

        if (
            not locale_changed_sections
            and not locale_reusable_sections
            and not (
                apply_translations
                and previous_translation is not None
                and previous_translation.status == "translated"
            )
        ):
            if previous_translation is not None:
                item_summary["locales"][target_locale] = {"status": previous_translation.status}
            continue

        requested_sections = locale_changed_sections | locale_reusable_sections
        partials: list[tuple[dict[str, Any], dict[str, str], list[dict], dict[str, str]] | None] = (
            []
        )

        shopify_current_sections = shopify_current_sections_by_locale[target_locale]
        shopify_requested_sections = requested_sections & shopify_current_sections
        if shopify_requested_sections:
            requested_shopify_partial, _ = build_pdp_payloads_from_shopify_translations(
                source_document=source_document,
                shopify_translations=existing_translations.get(target_locale, {}),
                target_locale=target_locale,
                candidate_sections=shopify_requested_sections,
            )
            partials.append(requested_shopify_partial)

        remaining_sections = requested_sections - shopify_requested_sections
        stored_sections: set[str] = set()
        if previous_translation is not None:
            for section_name in sorted(remaining_sections):
                if previous_translation.section_hashes.get(section_name) != section_hashes.get(
                    section_name
                ):
                    continue
                stored_partial = build_pdp_payloads_from_stored_translation(
                    source_document=source_document,
                    stored_document=previous_translation.document or {},
                    target_locale=target_locale,
                    changed_sections={section_name},
                )
                if stored_partial is not None:
                    partials.append(stored_partial)
                    stored_sections.add(section_name)

        sections_to_translate = remaining_sections - stored_sections
        if sections_to_translate:
            partials.append(
                translate_pdp_document(
                    store=store,
                    source_document=source_document,
                    changed_sections=sections_to_translate,
                    target_locale=target_locale,
                    source_locale=source_locale,
                    translator=translator,
                    existing_product=effective_existing,
                    existing_shopify_translations=existing_translations.get(target_locale, {}),
                )
            )
        translated_document, translated_hashes, payloads, section_sources = (
            _merge_pdp_translation_partials(
                *partials,
            )
        )
        payload_order = {
            "title": 10,
            "body_html": 20,
            "product_type": 30,
            "handle": 40,
            "name": 50,
            "value": 60,
        }
        payloads.sort(
            key=lambda item: (
                str(item.get("resource_id") or ""),
                payload_order.get(str(item.get("key") or ""), 100),
                str(item.get("key") or ""),
            )
        )

        for section_name in sorted(requested_sections):
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
            if not str(translated_value or "").strip():
                raise RuntimeError(f"Blank translation produced for {section_name}")
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
            "changed_sections": sorted(list(requested_sections)),
        }
        locale_summary: dict[str, Any] = {
            "translated_sections": sorted(list(requested_sections)),
            "section_sources": section_sources,
            "would_register": len(payloads) if dry_run and apply_translations else 0,
            "shopify_current_sections": sorted(shopify_current_sections),
        }
        for section_name in sorted(requested_sections):
            if section_name.startswith("product."):
                key = section_name.split(".", 1)[1]
                source_value = source_document["product"].get(key, {}).get("value", "")
                translated_value = translated_document["product"].get(key, "")
            elif section_name.startswith("metafield."):
                key = section_name.split(".", 1)[1]
                source_value = source_document["metafields"].get(key, {}).get("value", "")
                translated_value = translated_document["metafields"].get(key, "")
            elif section_name.startswith("option."):
                key = section_name.split(".", 1)[1]
                source_value = source_document["options"].get(key, {}).get("value", "")
                translated_value = translated_document["options"].get(key, "")
            else:
                continue
            logger.info(
                "translated_section",
                product_id=int(product_id),
                product_gid=product_gid,
                product_title=item_summary.get("product_title") or "",
                target_locale=target_locale,
                section=section_name,
                source=section_sources.get(section_name, "unknown"),
                source_hash=make_source_hash(source_value),
                translated_hash=make_source_hash(translated_value),
                source_snippet=_field_snippet(source_value),
                translated_snippet=_field_snippet(translated_value),
                apply_translations=apply_translations,
            )
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
        elif shopify_current_sections == set(section_hashes):
            translation_status = "synced"
            translation_metadata["reconciled_from_shopify"] = True
            locale_summary["reconciled_from_shopify"] = True

        persisted_document, persisted_hashes = _merge_pdp_translation_document(
            source_document=source_document,
            previous_document=(
                previous_translation.document if previous_translation is not None else None
            ),
            partial_document=translated_document,
            previous_hashes=(
                previous_translation.section_hashes if previous_translation is not None else None
            ),
            partial_hashes=translated_hashes,
            target_locale=target_locale,
        )
        store.upsert_pdp_translation(
            PDPTranslationRecord(
                shop_domain=SETTINGS.shopify_domain,
                product_gid=product_gid,
                target_locale=target_locale,
                document=persisted_document,
                section_hashes=persisted_hashes,
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
        else ("synced" if statuses and all(s == "synced" for s in statuses) else "translated")
    )
    local_summary["items"].append(item_summary)
    return local_summary


def _missing_pdp_translation_sections(
    *,
    previous_translation: PDPTranslationState | None,
    source_document: dict[str, Any],
    section_hashes: dict[str, str],
) -> set[str]:
    if previous_translation is None:
        return set()

    missing: set[str] = set()
    product_document = (
        previous_translation.document.get("product", {}) if previous_translation.document else {}
    )
    metafield_document = (
        previous_translation.document.get("metafields", {}) if previous_translation.document else {}
    )
    option_document = (
        previous_translation.document.get("options", {}) if previous_translation.document else {}
    )

    for section_name in section_hashes:
        if section_name.startswith("product."):
            key = section_name.split(".", 1)[1]
            if (
                key not in product_document
                or _nonblank_translation(product_document.get(key)) is None
            ):
                missing.add(section_name)
        elif section_name.startswith("metafield."):
            key = section_name.split(".", 1)[1]
            if (
                key not in metafield_document
                or _nonblank_translation(metafield_document.get(key)) is None
            ):
                missing.add(section_name)
        elif section_name.startswith("option."):
            key = section_name.split(".", 1)[1]
            if (
                key not in option_document
                or _nonblank_translation(option_document.get(key)) is None
            ):
                missing.add(section_name)

    return missing


async def _process_product_handle_only(
    *,
    store: NeonTranslationStore,
    translator: Translator,
    product_id: int,
    product_gid: str,
    source_document: dict[str, Any],
    section_hashes: dict[str, str],
    changed_sections: set[str],
    previous_hashes: dict[str, str],
    existing_translations: dict[str, dict[str, Any]],
    target_locales: list[str],
    source_locale: str,
    apply_translations: bool,
    dry_run: bool,
    summary: dict[str, Any],
) -> dict[str, Any]:
    item_summary: dict[str, Any] = {
        "product_id": int(product_id),
        "product_gid": product_gid,
        "product_title": (source_document.get("product", {}).get("title", {}) or {}).get(
            "value", ""
        ),
        "changed_sections": sorted(list(changed_sections)),
        "locales": {},
    }
    source_title = (source_document.get("product", {}).get("title", {}) or {}).get("value", "")
    handle_entry = source_document.get("product", {}).get("handle", {}) or {}
    if not handle_entry:
        item_summary["status"] = "unchanged"
        summary["items"].append(item_summary)
        return summary

    any_work = False
    for target_locale in target_locales:
        shopify_product_translations = (
            existing_translations.get(target_locale, {}).get(product_gid, {}) or {}
        )
        shopify_handle = shopify_product_translations.get("handle") or {}
        shopify_handle_value = str(shopify_handle.get("value") or "").strip()
        shopify_handle_outdated = bool(shopify_handle.get("outdated"))
        if shopify_handle_value and not shopify_handle_outdated:
            item_summary["locales"][target_locale] = {
                "status": "current_preserved",
                "translated_sections": [],
                "section_sources": {"product.handle": "shopify_current_preserved"},
            }
            continue

        previous_translation = store.get_pdp_translation_state(
            shop_domain=SETTINGS.shopify_domain,
            product_gid=product_gid,
            target_locale=target_locale,
        )
        previous_handle = (
            ((previous_translation.document or {}).get("product") or {}).get("handle")
            if previous_translation
            else None
        )
        handle_hash_matches = (
            previous_translation is not None
            and previous_translation.section_hashes.get("product.handle")
            == section_hashes.get("product.handle")
        )
        if (
            previous_handle
            and handle_hash_matches
            and (not apply_translations or previous_translation.status == "synced")
        ):
            item_summary["locales"][target_locale] = {
                "status": previous_translation.status if previous_translation else "synced"
            }
            continue

        any_work = True
        previous_title = (
            ((previous_translation.document or {}).get("product") or {}).get("title", "")
            if previous_translation
            else ""
        )
        existing_title = (
            _shopify_translation_value(
                existing_translations.get(target_locale, {}),
                resource_id=product_gid,
                key="title",
            )
            or ""
        )
        if shopify_handle_value and shopify_handle_outdated:
            translated_title = ""
            translated_handle = shopify_handle_value
            title_source = "shopify_outdated_handle_preserved"
        elif previous_title and not _should_reject_product_title_memory_hit(
            source_title, previous_title
        ):
            translated_title = previous_title
            translated_handle = make_handle_from_title(translated_title)
            title_source = "stored_translation_state"
        elif existing_title and not _should_reject_product_title_memory_hit(
            source_title, existing_title
        ):
            translated_title = existing_title
            translated_handle = make_handle_from_title(translated_title)
            title_source = "shopify_existing_translation"
        else:
            translated_title, title_source = _translate_product_title(
                store,
                translator,
                source_title,
                source_locale=source_locale,
                target_locale=target_locale,
                dnt=load_do_not_translate(
                    getattr(SETTINGS, "do_not_translate_path", None)
                    if getattr(SETTINGS, "do_not_translate_path", None)
                    else None
                ),
                exclude_tokens=[],
            )
            translated_handle = make_handle_from_title(translated_title) if translated_title else ""
        translated_document = {
            "product_gid": product_gid,
            "shop_domain": SETTINGS.shopify_domain,
            "source_locale": source_locale,
            "target_locale": target_locale,
            "product": {"handle": translated_handle},
            "metafields": {},
            "options": {},
        }
        translated_hashes = {"product.handle": make_source_hash(handle_entry.get("value") or "")}
        payloads = []
        if translated_handle:
            payloads.append(
                {
                    "key": "handle",
                    "locale": target_locale,
                    "value": translated_handle,
                    "translatableContentDigest": handle_entry.get("digest") or "",
                }
            )

        translation_status = "translated"
        translation_metadata: dict[str, Any] = {"changed_sections": ["product.handle"]}
        locale_summary: dict[str, Any] = {
            "translated_sections": ["product.handle"],
            "section_sources": {"product.handle": f"handle_from_title:{title_source}"},
        }
        if apply_translations and not dry_run:
            user_errors = (
                await register_translations(handle_entry["resource_id"], payloads)
                if payloads
                else [{"field": ["translations", "0", "value"], "message": "Value can't be blank"}]
            )
            if user_errors:
                translation_status = "failed"
                translation_metadata["user_errors"] = {handle_entry["resource_id"]: user_errors}
                locale_summary["user_errors"] = {handle_entry["resource_id"]: user_errors}
            else:
                translation_status = "synced"
                summary["registered"] += len(payloads)

        persisted_document, persisted_hashes = _merge_pdp_translation_document(
            source_document=source_document,
            previous_document=(
                previous_translation.document if previous_translation is not None else None
            ),
            partial_document=translated_document,
            previous_hashes=(
                previous_translation.section_hashes if previous_translation is not None else None
            ),
            partial_hashes=translated_hashes,
            target_locale=target_locale,
        )
        store.upsert_pdp_translation(
            PDPTranslationRecord(
                shop_domain=SETTINGS.shopify_domain,
                product_gid=product_gid,
                target_locale=target_locale,
                document=persisted_document,
                section_hashes=persisted_hashes,
                status=translation_status,
                model=translator.model,
                metadata=translation_metadata,
            )
        )
        store.upsert_translation_memory(
            source_hash=make_source_hash(handle_entry.get("value") or ""),
            field_key="product.handle",
            source_locale=source_locale,
            target_locale=target_locale,
            source_value=handle_entry.get("value") or "",
            translated_value=translated_handle,
            model=translator.model,
            metadata={"product_gid": product_gid},
        )
        logger.info(
            "translated_section",
            product_id=int(product_id),
            product_gid=product_gid,
            product_title=item_summary.get("product_title") or "",
            target_locale=target_locale,
            section="product.handle",
            source=f"handle_from_title:{title_source}",
            source_hash=make_source_hash(handle_entry.get("value") or ""),
            translated_hash=make_source_hash(translated_handle),
            source_snippet=_field_snippet(handle_entry.get("value") or ""),
            translated_snippet=_field_snippet(translated_handle),
            apply_translations=apply_translations,
        )
        locale_summary["status"] = translation_status
        item_summary["locales"][target_locale] = locale_summary

    if not any_work:
        item_summary["status"] = "unchanged"
    else:
        statuses = [v.get("status", "translated") for v in item_summary["locales"].values()]
        item_summary["status"] = (
            "failed"
            if any(s == "failed" for s in statuses)
            else ("synced" if statuses and all(s == "synced" for s in statuses) else "translated")
        )
    summary["items"].append(item_summary)
    return summary

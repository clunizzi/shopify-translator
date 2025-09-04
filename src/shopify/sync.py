from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from src.config.settings import SETTINGS
from src.config.dnt_loader import load_do_not_translate
from src.snapshot.sqlite_snapshot import SnapshotStore
from src.shopify.graphql import (
    get_product_metafields_by_keys,
    get_product_all_metafields,
    get_translatable_by_ids,
    make_product_gid,
    register_translations,
)
from src.translate.cache import TranslationCache
from src.translate.translator import Translator

logger = structlog.get_logger()


async def process_product(
    product_numeric_id: str | int,
    target_locales: list[str],
    mf_include: list[tuple[str, str]],
    mf_json_paths: list[str],  # currently unused; reserved for future selective JSON path filtering
    source_locale: str,
    dry_run: bool = False,
    is_create: bool = False,
    delay_ms_after_create: int = 8000,
    *,
    apply_on_dry_run: bool = False,
    fill_missing_translations: bool | None = None,
) -> dict:
    """
    End-to-end sync for one product: diff translatable content, translate changed, push, snapshot.
    Notes:
      - For METAFIELD JSON, Shopify expects value as JSON STRING (not object) and key="value".
      - Always include translatableContentDigest for concurrency control.
    """
    gid = make_product_gid(product_numeric_id)
    if is_create and delay_ms_after_create > 0:
        await asyncio.sleep(delay_ms_after_create / 1000.0)

    # Resolve metafield IDs: if mf_include empty -> auto-discover textual/JSON metafields
    AUTO_TYPES = [
        "single_line_text_field",
        "multi_line_text_field",
        "json",
        "rich_text",
    ]
    if mf_include:
        mf_nodes = await get_product_metafields_by_keys(gid, mf_include)
    else:
        mf_nodes = await get_product_all_metafields(gid, allowed_types=AUTO_TYPES)
    mf_ids = [n.get("id") for n in mf_nodes if n.get("id")]
    resource_ids = [gid, *mf_ids]

    live_map = await get_translatable_by_ids(resource_ids)

    # Snapshot store and translator setup
    snapshot = SnapshotStore()
    cache = TranslationCache()
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)

    # DNT config (use default YAML if present)
    dnt_path = (
        (SETTINGS.do_not_translate_path) if getattr(SETTINGS, "do_not_translate_path", None) else None
    )
    dnt = load_do_not_translate(dnt_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]

    changed_per_resource: dict[str, list[dict]] = {}
    for rid, contents in live_map.items():
        if not contents:
            continue
        old = snapshot.get_digest_map(rid)
        live_digest_map = {c.get("key"): c.get("digest") for c in contents if c.get("key")}
        if not old and is_create:
            changed = [c for c in contents if c.get("key")]
        else:
            changed = [c for c in contents if old.get(c.get("key")) != c.get("digest")]
        if changed:
            changed_per_resource[rid] = changed

    # Optionally backfill: if requested, include unchanged contents as well (to fill missing locales)
    do_fill_missing = SETTINGS.fill_missing_translations if fill_missing_translations is None else fill_missing_translations
    if do_fill_missing:
        for rid, contents in live_map.items():
            if not contents:
                continue
            # Merge preserving existing list if already changed
            base = changed_per_resource.get(rid, [])
            # include only items with a key to be registered
            extra = [c for c in contents if c.get("key")]
            # Keep order and unique by key
            keys_seen = set([c.get("key") for c in base])
            for c in extra:
                k = c.get("key")
                if k and k not in keys_seen:
                    base.append(c)
                    keys_seen.add(k)
            if base:
                changed_per_resource[rid] = base

    summary = {
        "product_id": int(product_numeric_id),
        "resources": len(resource_ids),
        "changed_resources": len(changed_per_resource),
        "translated": 0,
        "pushed": 0,
        "userErrors": {},
        "changed_keys": {rid: [c.get("key") for c in lst] for rid, lst in changed_per_resource.items()},
    }

    if not changed_per_resource:
        cache.close()
        snapshot.close()
        return summary

    # Build per-locale translations and optionally push
    for locale in target_locales:
        for rid, items in changed_per_resource.items():
            translations_payload: list[dict] = []
            for it in items:
                key = it.get("key") or ""
                digest = it.get("digest") or ""
                value = (it.get("value") or "")

                translated_value = ""
                try:
                    if rid == gid:
                        # Product resource
                        # Map Shopify keys to our translator fields
                        field_map = {
                            "title": "title",
                            "body_html": "body_html",
                            "seo.title": "meta_title",
                            "seo.description": "meta_description",
                            "product_type": "product_type",
                        }
                        field = field_map.get(key)
                        if not field:
                            # Skip unsupported product key
                            continue
                        if field == "body_html":
                            translated_value = translator.translate_html(
                                "PRODUCT", field, value, locale, dnt, exclude_tokens
                            )
                        else:
                            translated_value = translator.translate_plain(
                                "PRODUCT", field, value, locale, dnt, exclude_tokens
                            )
                    else:
                        # Metafield resource: only key == "value" is translatable
                        if key != "value":
                            continue
                        # value must be JSON string; translator handles tolerant parsing
                        translated_value = translator.translate_json_value(
                            "METAFIELD",
                            "value",
                            value,
                            locale,
                            dnt,
                            exclude_tokens,
                        )
                        # Ensure it is a JSON string (Shopify expects a string for metafield translations)
                        try:
                            json.loads(translated_value)
                        except Exception:
                            # if translator returned non-JSON (e.g. empty), keep as original or skip
                            continue
                    summary["translated"] += 1
                except Exception as e:  # pragma: no cover - defensive
                    logger.error("translate_error", key=key, error=str(e))
                    continue

                translations_payload.append(
                    {
                        "key": key,
                        "locale": locale,
                        "value": translated_value,
                        "translatableContentDigest": digest,
                    }
                )

            if not translations_payload:
                continue

            user_errors: list[dict] = []
            if not dry_run:
                user_errors = await register_translations(rid, translations_payload)
                if user_errors:
                    summary["userErrors"][rid] = user_errors
                else:
                    summary["pushed"] += len(translations_payload)

            # Update snapshot only if not dry-run or explicitly applied in dry-run
            if not dry_run or apply_on_dry_run:
                for it in items:
                    k = it.get("key") or ""
                    d = it.get("digest") or ""
                    if k:
                        snapshot.upsert_digest(rid, k, d)

    cache.close()
    snapshot.close()
    return summary

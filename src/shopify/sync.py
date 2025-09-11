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
    # Bind product identifiers into log context
    try:
        getattr(structlog, "contextvars").bind_contextvars(
            product_id=int(product_numeric_id), product_gid=gid
        )
    except Exception:
        pass
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
        # Unbind at function exit (nothing to do)
        try:
            getattr(structlog, "contextvars").unbind_contextvars("product_id", "product_gid")
        except Exception:
            pass
        return summary

    # Build per-locale translations and optionally push
    do_fill_missing = SETTINGS.fill_missing_translations if fill_missing_translations is None else fill_missing_translations
    for locale in target_locales:
        for rid, items in changed_per_resource.items():
            # For product resource, capture current defaults to enable fallbacks (seo -> title/body)
            is_product = (rid == gid)
            # Map available values by key for quick fallback lookup
            existing: dict[str, str] = { (x.get("key") or ""): (x.get("value") or "") for x in items if x.get("key") }

            # Ensure SEO keys are present when doing backfill (some shops omit empty SEO in translatableContent diffs)
            if is_product and do_fill_missing:
                want_keys = {"title", "body_html", "seo.title", "seo.description", "product_type"}
                have_keys = set(existing.keys())
                if rid in live_map:
                    all_nodes = live_map[rid] or []
                    for node in all_nodes:
                        k = node.get("key") or ""
                        if k in want_keys and k not in have_keys:
                            items.append(node)
                            existing[k] = node.get("value") or ""
                            have_keys.add(k)

            # If filling missing, ensure we process title before handle to allow handle generation from translated title
            def _priority(k: str) -> int:
                order = {
                    "title": 10,
                    "body_html": 20,
                    "seo.title": 30,
                    "seo.description": 40,
                    "product_type": 50,
                    "handle": 60,
                }
                return order.get(k, 100)

            items_sorted = sorted(items, key=lambda x: _priority(x.get("key") or ""))

            translations_payload: list[dict] = []
            translated_title_for_handle: str | None = None
            for it in items_sorted:
                key = it.get("key") or ""
                digest = it.get("digest") or ""
                value = (it.get("value") or "")

                translated_value = ""
                try:
                    if is_product:
                        # Product resource
                        # Map Shopify keys to our translator fields
                        field_map = {
                            "title": "title",
                            "body_html": "body_html",
                            "seo.title": "meta_title",
                            "seo.description": "meta_description",
                            "product_type": "product_type",
                            "handle": "handle",
                        }
                        field = field_map.get(key)
                        if not field:
                            # Skip unsupported product key
                            continue
                        # Fallbacks: if seo fields are empty, use product defaults (title/body)
                        if key == "seo.title" and not value:
                            # Prefer explicit product title from existing map
                            value = existing.get("title", value)
                        elif key == "seo.description" and not value:
                            value = existing.get("body_html", value)
                        if field == "body_html":
                            translated_value = translator.translate_html(
                                "PRODUCT", field, value, locale, dnt, exclude_tokens
                            )
                        elif field == "handle":
                            # Use translated title if available to derive handle; otherwise fall back to default content
                            tv = translated_title_for_handle
                            translated_value = translator.translate_field(
                                "PRODUCT",
                                field,
                                default_content=value,
                                target_locale=locale,
                                dnt=dnt,
                                exclude_similarity_tokens=exclude_tokens,
                                title_translated=tv,
                                preserve_handle=False,
                            )
                        else:
                            translated_value = translator.translate_plain(
                                "PRODUCT", field, value, locale, dnt, exclude_tokens
                            )
                            if key == "title":
                                translated_title_for_handle = translated_value or translated_title_for_handle
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
    try:
        getattr(structlog, "contextvars").unbind_contextvars("product_id", "product_gid")
    except Exception:
        pass
    return summary

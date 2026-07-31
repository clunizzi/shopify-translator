from __future__ import annotations

import hashlib
import uuid
from typing import Any

from src.config.settings import SETTINGS
from src.config.theme_policies import should_translate_theme_entry
from src.shopify.graphql import (
    get_main_theme,
    get_theme_file_manifest,
    list_translatable_resources,
)
from src.state.neon import (
    NeonTranslationStore,
    ThemeFileRecord,
    ThemeSourceRecord,
    make_source_hash,
)

TRACKED_THEME_RESOURCE_TYPES = (
    "ONLINE_STORE_THEME_JSON_TEMPLATE",
    "ONLINE_STORE_THEME_SECTION_GROUP",
    "ONLINE_STORE_THEME_APP_EMBED",
    "ONLINE_STORE_THEME_LOCALE_CONTENT",
    "ONLINE_STORE_THEME_SETTINGS_CATEGORY",
    "ONLINE_STORE_THEME_SETTINGS_DATA_SECTIONS",
)

TRANSLATED_THEME_RESOURCE_TYPES = {
    "ONLINE_STORE_THEME_JSON_TEMPLATE",
    "ONLINE_STORE_THEME_SECTION_GROUP",
}


def _numeric_theme_id(value: str | int) -> str:
    return str(value).rstrip("/").rsplit("/", 1)[-1]


def _belongs_to_theme(resource_id: str, theme_id: str) -> bool:
    numeric = _numeric_theme_id(theme_id)
    return (
        resource_id.rstrip("/").endswith(f"/{numeric}")
        or f"theme_id={numeric}" in resource_id
        or f"themeId={numeric}" in resource_id
    )


def _manifest_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["filename"]): dict(item) for item in items}


def _manifest_diff(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    previous_names = set(previous)
    current_names = set(current)
    modified = sorted(
        filename
        for filename in previous_names & current_names
        if (
            str(previous[filename].get("checksum_md5") or "")
            != str(current[filename].get("checksum_md5") or "")
            or int(previous[filename].get("size") or 0) != int(current[filename].get("size") or 0)
        )
    )
    return {
        "added": sorted(current_names - previous_names),
        "modified": modified,
        "deleted": sorted(previous_names - current_names),
    }


async def _fetch_resource_type(resource_type: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page, page_info = await list_translatable_resources(
            resource_type=resource_type,
            first=250,
            after=cursor,
        )
        out.extend(page)
        if not page_info.get("hasNextPage"):
            return out
        cursor = str(page_info.get("endCursor") or "")
        if not cursor:
            raise RuntimeError(f"Shopify {resource_type} pagination returned no endCursor")


async def track_main_theme_read_only(
    *,
    approved_theme_id: str,
    topic: str = "manual/read-only",
    event_id: str | None = None,
    source_locale: str | None = None,
    store: NeonTranslationStore | None = None,
) -> dict[str, Any]:
    """Persist checksums/digests only. Never calls OpenAI or a Shopify mutation."""
    approved_numeric = _numeric_theme_id(approved_theme_id)
    if not approved_numeric:
        raise RuntimeError("APPROVED_THEME_ID/THEME_ID mancante")

    owns_store = store is None
    state = store or NeonTranslationStore()
    try:
        state.ensure_schema()
        main = await get_main_theme()
        actual_numeric = _numeric_theme_id(main["id"])
        manifest_items = await get_theme_file_manifest(main["id"])
        current_manifest = _manifest_map(manifest_items)
        previous_manifest = state.get_theme_file_snapshot(
            shop_domain=SETTINGS.shopify_domain,
            theme_id=actual_numeric,
        )
        file_diff = _manifest_diff(previous_manifest, current_manifest)
        baseline = not previous_manifest

        changed_resources: list[dict[str, Any]] = []
        resource_counts: dict[str, int] = {}
        effective_source_locale = source_locale or SETTINGS.source_locale
        for resource_type in TRACKED_THEME_RESOURCE_TYPES:
            resources = [
                item
                for item in await _fetch_resource_type(resource_type)
                if _belongs_to_theme(str(item.get("resourceId") or ""), actual_numeric)
            ]
            resource_counts[resource_type] = len(resources)
            for resource in resources:
                resource_id = str(resource.get("resourceId") or "")
                content_items = list(resource.get("translatableContent") or [])
                if resource_type in TRANSLATED_THEME_RESOURCE_TYPES:
                    content_items = [
                        item
                        for item in content_items
                        if should_translate_theme_entry(
                            resource_type=resource_type,
                            key=str(item.get("key") or ""),
                            value=str(item.get("value") or ""),
                        )
                    ]
                document = {
                    str(item.get("key") or ""): str(item.get("value") or "")
                    for item in content_items
                    if str(item.get("key") or "")
                }
                if resource_type in TRANSLATED_THEME_RESOURCE_TYPES:
                    hashes = {
                        f"{resource_type}.{str(item.get('key') or '')}": make_source_hash(
                            str(item.get("value") or "")
                        )
                        for item in content_items
                        if str(item.get("key") or "")
                    }
                else:
                    hashes = {
                        str(item.get("key") or ""): (
                            str(item.get("digest") or "")
                            or make_source_hash(str(item.get("value") or ""))
                        )
                        for item in content_items
                        if str(item.get("key") or "")
                    }
                previous_hashes = state.get_theme_source_hashes(
                    shop_domain=SETTINGS.shopify_domain,
                    theme_id=actual_numeric,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    source_locale=effective_source_locale,
                )
                changed_keys = sorted(
                    key
                    for key in set(previous_hashes) | set(hashes)
                    if previous_hashes.get(key) != hashes.get(key)
                )
                if previous_hashes and changed_keys:
                    changed_resources.append(
                        {
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                            "changed_keys": changed_keys,
                        }
                    )
                state.upsert_theme_source(
                    ThemeSourceRecord(
                        shop_domain=SETTINGS.shopify_domain,
                        theme_id=actual_numeric,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        source_locale=effective_source_locale,
                        document=document,
                        section_hashes=hashes,
                        metadata={
                            "tracking_only": True,
                            "theme_name": main["name"],
                            "theme_role": main["role"],
                        },
                    )
                )

        state.replace_theme_file_snapshot(
            shop_domain=SETTINGS.shopify_domain,
            theme_id=actual_numeric,
            records=[
                ThemeFileRecord(
                    shop_domain=SETTINGS.shopify_domain,
                    theme_id=actual_numeric,
                    filename=item["filename"],
                    checksum_md5=item["checksum_md5"],
                    content_type=item["content_type"],
                    size=item["size"],
                    file_updated_at=item["updated_at"],
                )
                for item in manifest_items
            ],
        )

        main_matches = actual_numeric == approved_numeric
        changed_file_count = sum(len(items) for items in file_diff.values())
        if not main_matches:
            status = "blocked_main_mismatch"
        elif baseline:
            status = "baseline"
        elif changed_file_count or changed_resources:
            status = "changed"
        else:
            status = "unchanged"

        details = {
            "read_only": True,
            "baseline": baseline,
            "theme_updated_at": main["updated_at"],
            "files_total": len(manifest_items),
            "file_diff": file_diff,
            "resource_counts": resource_counts,
            "changed_resources": changed_resources,
        }
        stable_event_id = event_id or str(uuid.uuid4())
        if not event_id:
            stable_event_id = hashlib.sha256(
                f"{SETTINGS.shopify_domain}|{actual_numeric}|{topic}|{stable_event_id}".encode()
            ).hexdigest()
        state.append_theme_change_event(
            event_id=stable_event_id,
            shop_domain=SETTINGS.shopify_domain,
            approved_theme_id=approved_numeric,
            actual_theme_id=actual_numeric,
            theme_name=main["name"],
            theme_role=main["role"],
            topic=topic,
            status=status,
            details=details,
        )
        return {
            "ok": True,
            "read_only": True,
            "status": status,
            "approved_theme_id": approved_numeric,
            "actual_theme_id": actual_numeric,
            "theme_name": main["name"],
            "theme_role": main["role"],
            **details,
        }
    finally:
        if owns_store:
            state.close()

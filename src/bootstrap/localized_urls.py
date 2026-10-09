from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from src.shopify.graphql import list_translatable_resources_with_translations

RESOURCE_PATHS = {
    "pages": "PAGE",
    "collections": "COLLECTION",
    "blogs": "BLOG",
}
STABLE_HANDLES = {"collections": {"all", "frontpage"}}
_HTML_INTERNAL_URL_RE = re.compile(
    r"(?P<prefix>\b(?:href|action|formaction)\s*=\s*)(?P<quote>['\"])(?P<url>/(?!/)[^'\"]*)(?P=quote)",
    re.IGNORECASE,
)


def extract_internal_urls_from_html(value: str) -> list[str]:
    return [match.group("url") for match in _HTML_INTERNAL_URL_RE.finditer(str(value or ""))]


async def fetch_localized_handle_maps(
    *,
    target_locales: list[str],
    source_urls: list[str] | None = None,
    first: int = 250,
) -> dict[str, dict[str, dict[str, str]]]:
    maps: dict[str, dict[str, dict[str, str]]] = {
        locale: {path: {} for path in RESOURCE_PATHS} for locale in target_locales
    }
    required_by_path: dict[str, set[str]] | None = None
    if source_urls is not None:
        required_by_path = {path: set() for path in RESOURCE_PATHS}
        for source_url in source_urls:
            parts = [part for part in urlsplit(str(source_url or "")).path.split("/") if part]
            if len(parts) < 2 or parts[0] not in RESOURCE_PATHS:
                continue
            if parts[1] in STABLE_HANDLES.get(parts[0], set()):
                continue
            required_by_path[parts[0]].add(parts[1])

    for path, resource_type in RESOURCE_PATHS.items():
        required_handles = required_by_path.get(path, set()) if required_by_path is not None else None
        if required_handles is not None and not required_handles:
            continue
        found_handles: set[str] = set()
        cursor: str | None = None
        while True:
            nodes, page_info = await list_translatable_resources_with_translations(
                resource_type=resource_type,
                locales=target_locales,
                first=first,
                after=cursor,
            )
            for node in nodes:
                source_handle = next(
                    (
                        str(entry.get("value") or "").strip()
                        for entry in node.get("translatableContent") or []
                        if str(entry.get("key") or "") == "handle"
                    ),
                    "",
                )
                if not source_handle:
                    continue
                if required_handles is not None and source_handle not in required_handles:
                    continue
                found_handles.add(source_handle)
                for locale in target_locales:
                    translated = ((node.get("translations") or {}).get(locale) or {}).get(
                        "handle"
                    ) or {}
                    localized_handle = str(translated.get("value") or "").strip()
                    if localized_handle:
                        maps[locale][path][source_handle] = localized_handle
            if required_handles is not None and found_handles >= required_handles:
                break
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    return maps


def localize_internal_url(
    value: str,
    *,
    target_locale: str,
    route_prefixes: dict[str, str],
    handle_maps: dict[str, dict[str, dict[str, str]]] | None = None,
) -> str:
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        return raw

    prefix = str(route_prefixes.get(target_locale) or "").strip()
    if not prefix:
        prefix = "/" + target_locale.strip().strip("/")
    prefix = "/" + prefix.strip("/")

    # Avoid double-prefixing values already localized for the target market.
    if parts.path == prefix or parts.path.startswith(f"{prefix}/"):
        return raw

    source_path = parts.path
    for locale, configured_prefix in route_prefixes.items():
        other_prefix = "/" + str(configured_prefix or locale).strip().strip("/")
        if other_prefix == prefix:
            continue
        if source_path == other_prefix:
            source_path = "/"
            break
        if source_path.startswith(f"{other_prefix}/"):
            source_path = source_path[len(other_prefix) :]
            break

    path_parts = [part for part in source_path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] in RESOURCE_PATHS:
        localized = (((handle_maps or {}).get(target_locale) or {}).get(path_parts[0]) or {}).get(
            path_parts[1]
        ) or path_parts[1]
        path_parts[1] = localized
    localized_path = "/".join(path_parts)
    if localized_path:
        localized_path = f"{prefix}/{localized_path}"
    else:
        localized_path = f"{prefix}/"
    return urlunsplit(("", "", localized_path, parts.query, parts.fragment))


def localize_internal_urls_in_html(
    value: str,
    *,
    target_locale: str,
    route_prefixes: dict[str, str],
    handle_maps: dict[str, dict[str, dict[str, str]]] | None = None,
) -> str:
    def replace(match: re.Match[str]) -> str:
        localized = localize_internal_url(
            match.group("url"),
            target_locale=target_locale,
            route_prefixes=route_prefixes,
            handle_maps=handle_maps,
        )
        return f'{match.group("prefix")}{match.group("quote")}{localized}{match.group("quote")}'

    return _HTML_INTERNAL_URL_RE.sub(replace, str(value or ""))

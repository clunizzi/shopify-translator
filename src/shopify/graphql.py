from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from src.config.settings import SETTINGS

logger = structlog.get_logger()


class ShopifyGraphQLError(RuntimeError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        self.retry_after = 0.0
        messages = "; ".join(
            str(error.get("message") or "Unknown GraphQL error") for error in errors
        )
        super().__init__(messages or "Shopify GraphQL request failed")

    @property
    def retryable(self) -> bool:
        retryable_codes = {"THROTTLED", "INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE"}
        return any(
            str((error.get("extensions") or {}).get("code") or "").upper() in retryable_codes
            for error in self.errors
        )


def make_product_gid(numeric_id: int | str) -> str:
    return f"gid://shopify/Product/{int(numeric_id)}"


async def _post_graphql(query: str, variables: dict | None = None) -> dict:
    requested_version = SETTINGS.shopify_api_version
    endpoint = f"https://{SETTINGS.shopify_domain}/admin/api/{requested_version}/graphql.json"
    token = SETTINGS.shopify_token
    if not SETTINGS.shopify_domain or not token:
        raise RuntimeError("Shopify endpoint/token non configurati in SETTINGS")
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    timeout = float(getattr(SETTINGS, "request_timeout", 30.0))
    retries = int(getattr(SETTINGS, "retries", 2))
    backoff = 0.75

    payload = {"query": query, "variables": variables or {}}
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
                resp.raise_for_status()
                data = resp.json()
                actual_version = resp.headers.get("x-shopify-api-version")
                if actual_version and actual_version != requested_version:
                    logger.warning(
                        "shopify_api_version_mismatch",
                        requested=requested_version,
                        actual=actual_version,
                    )
                errors = data.get("errors") or []
                if errors:
                    logger.warning("shopify_graphql_errors", errors=errors)
                    error = ShopifyGraphQLError(errors)
                    if error.retryable:
                        cost = (data.get("extensions") or {}).get("cost") or {}
                        throttle = cost.get("throttleStatus") or {}
                        requested = float(cost.get("requestedQueryCost") or 0)
                        available = float(throttle.get("currentlyAvailable") or 0)
                        restore_rate = float(throttle.get("restoreRate") or 0)
                        if restore_rate > 0 and requested > available:
                            error.retry_after = ((requested - available) / restore_rate) + 0.5
                    raise error
                return data
            except Exception as e:  # pragma: no cover - retry path
                last_exc = e
                if isinstance(e, ShopifyGraphQLError) and not e.retryable:
                    break
                if attempt >= retries:
                    break
                await asyncio.sleep(
                    max(
                        backoff * (2**attempt),
                        float(getattr(e, "retry_after", 0.0)),
                    )
                )
        assert last_exc is not None
        raise last_exc


async def get_main_theme() -> dict[str, str]:
    query = (
        "query CurrentMainTheme {"
        "  themes(first: 2, roles: [MAIN]) {"
        "    nodes { id name role updatedAt }"
        "  }"
        "}"
    )
    data = await _post_graphql(query)
    nodes = (((data.get("data") or {}).get("themes") or {}).get("nodes")) or []
    if len(nodes) != 1:
        raise RuntimeError(f"Expected exactly one MAIN theme, found {len(nodes)}")
    node = nodes[0]
    return {
        "id": str(node.get("id") or ""),
        "name": str(node.get("name") or ""),
        "role": str(node.get("role") or ""),
        "updated_at": str(node.get("updatedAt") or ""),
    }


async def list_theme_files(
    *,
    theme_id: str,
    first: int = 2500,
    after: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a theme file manifest without fetching file bodies."""
    gid = (
        theme_id
        if str(theme_id).startswith("gid://")
        else f"gid://shopify/OnlineStoreTheme/{theme_id}"
    )
    q = (
        "query ThemeFileManifest($id: ID!, $first: Int!, $after: String) {"
        "  theme(id: $id) {"
        "    id name role updatedAt"
        "    files(first: $first, after: $after) {"
        "      nodes { filename checksumMd5 contentType size updatedAt }"
        "      pageInfo { hasNextPage endCursor }"
        "      userErrors { code filename }"
        "    }"
        "  }"
        "}"
    )
    data = await _post_graphql(
        q,
        {"id": gid, "first": min(max(1, int(first)), 2500), "after": after},
    )
    theme = ((data.get("data") or {}).get("theme")) or {}
    if not theme:
        raise RuntimeError(f"Shopify theme not found: {gid}")
    files = theme.get("files") or {}
    user_errors = files.get("userErrors") or []
    if user_errors:
        raise RuntimeError(f"Shopify theme file read failed: {user_errors}")
    nodes = [
        {
            "filename": str(node.get("filename") or ""),
            "checksum_md5": str(node.get("checksumMd5") or ""),
            "content_type": str(node.get("contentType") or ""),
            "size": int(node.get("size") or 0),
            "updated_at": str(node.get("updatedAt") or ""),
        }
        for node in files.get("nodes") or []
        if str(node.get("filename") or "")
    ]
    return nodes, (files.get("pageInfo") or {})


async def get_theme_file_manifest(theme_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        nodes, page_info = await list_theme_files(
            theme_id=theme_id,
            first=2500,
            after=cursor,
        )
        out.extend(nodes)
        if not page_info.get("hasNextPage"):
            return out
        cursor = str(page_info.get("endCursor") or "")
        if not cursor:
            raise RuntimeError("Shopify theme file pagination returned no endCursor")


async def get_product_metafields_by_keys(
    product_gid: str, keys: list[tuple[str, str]]
) -> list[dict]:
    """Fetch selected metafields by (namespace,key). Returns items with id, namespace, key, type, value."""
    q = (
        "query MF($pid: ID!, $ns: String!, $key: String!) {"
        "  product(id: $pid) { id metafield(namespace: $ns, key: $key) { id namespace key type value } }"
        "}"
    )
    out: list[dict] = []

    # Execute sequentially or in small parallel batches
    async def _one(ns: str, key: str):
        data = await _post_graphql(q, {"pid": product_gid, "ns": ns, "key": key})
        node = ((data.get("data") or {}).get("product") or {}).get("metafield")
        if node:
            out.append(node)

    # Limit concurrency to be safe
    sem = asyncio.Semaphore(6)

    async def _guard(ns: str, key: str):
        async with sem:
            await _one(ns, key)

    await asyncio.gather(*[_guard(ns, k) for ns, k in keys])
    return out


async def get_translatable_by_ids(ids: list[str]) -> dict[str, list[dict]]:
    if not ids:
        return {}
    q = (
        "query T($ids:[ID!]!) {"
        "  translatableResourcesByIds(first: 250, resourceIds: $ids) {"
        "    nodes { resourceId translatableContent { key value digest locale } }"
        "  }"
        "}"
    )
    out: dict[str, list[dict]] = {}
    for start in range(0, len(ids), 250):
        chunk = ids[start : start + 250]
        data = await _post_graphql(q, {"ids": chunk})
        nodes = (
            ((data.get("data") or {}).get("translatableResourcesByIds") or {}).get("nodes")
        ) or []
        for n in nodes:
            rid = n.get("resourceId")
            t = n.get("translatableContent") or []
            if rid:
                out[rid] = t
    return out


async def list_translatable_resources(
    *,
    resource_type: str,
    first: int = 50,
    after: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    q = (
        "query TranslatableResources($first: Int!, $after: String, $resourceType: TranslatableResourceType!) {"
        "  translatableResources(first: $first, after: $after, resourceType: $resourceType) {"
        "    edges {"
        "      cursor"
        "      node { resourceId translatableContent { key value digest locale } }"
        "    }"
        "    pageInfo { hasNextPage endCursor }"
        "  }"
        "}"
    )
    data = await _post_graphql(
        q,
        {
            "first": int(first),
            "after": after,
            "resourceType": resource_type,
        },
    )
    conn = ((data.get("data") or {}).get("translatableResources")) or {}
    edges = conn.get("edges") or []
    nodes: list[dict[str, Any]] = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        if node.get("resourceId"):
            nodes.append(
                {
                    "cursor": (edge or {}).get("cursor"),
                    "resourceId": node.get("resourceId"),
                    "translatableContent": node.get("translatableContent") or [],
                }
            )
    return nodes, (conn.get("pageInfo") or {})


async def list_translatable_resources_with_translations(
    *,
    resource_type: str,
    locales: list[str],
    first: int = 50,
    after: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """List one resource type together with its current locale state.

    Fetching translations on the paginated connection avoids one GraphQL
    request per resource, which is essential for large collection catalogs.
    Locale aliases are generated only from validated locale identifiers.
    """
    query_locales = sorted(
        {str(locale or "").strip() for locale in locales if str(locale or "").strip()}
    )
    for query_locale in query_locales:
        if any(not (char.isalnum() or char == "-") for char in query_locale):
            raise ValueError(f"Invalid Shopify locale: {query_locale!r}")
    locale_suffixes = {
        query_locale: "".join(char if char.isalnum() else "_" for char in query_locale)
        for query_locale in query_locales
    }
    translation_fields = "".join(
        (
            f" translations_{locale_suffixes[query_locale]}: "
            f'translations(locale: "{query_locale}") '
            "{ key value outdated }"
        )
        for query_locale in query_locales
    )
    q = (
        "query TranslatableResourcesWithTranslations("
        "$first: Int!, $after: String, $resourceType: TranslatableResourceType!) {"
        "  translatableResources(first: $first, after: $after, resourceType: $resourceType) {"
        "    edges {"
        "      cursor"
        "      node {"
        "        resourceId"
        "        translatableContent { key value digest locale }"
        f"        {translation_fields}"
        "      }"
        "    }"
        "    pageInfo { hasNextPage endCursor }"
        "  }"
        "}"
    )
    data = await _post_graphql(
        q,
        {
            "first": int(first),
            "after": after,
            "resourceType": resource_type,
        },
    )
    conn = ((data.get("data") or {}).get("translatableResources")) or {}
    nodes: list[dict[str, Any]] = []
    for edge in conn.get("edges") or []:
        node = (edge or {}).get("node") or {}
        resource_id = str(node.get("resourceId") or "")
        if not resource_id:
            continue
        translations: dict[str, dict[str, dict[str, Any]]] = {}
        for query_locale in query_locales:
            fields: dict[str, dict[str, Any]] = {}
            for item in node.get(f"translations_{locale_suffixes[query_locale]}") or []:
                key = str(item.get("key") or "")
                if key:
                    fields[key] = {
                        "value": str(item.get("value") or ""),
                        "outdated": bool(item.get("outdated")),
                    }
            translations[query_locale] = fields
        nodes.append(
            {
                "cursor": (edge or {}).get("cursor"),
                "resourceId": resource_id,
                "translatableContent": node.get("translatableContent") or [],
                "translations": translations,
            }
        )
    return nodes, (conn.get("pageInfo") or {})


async def register_translations(resource_id: str, translations: list[dict]) -> list[dict]:
    """
    Executes translationsRegister with the provided list of TranslationInput.
    Returns userErrors array (empty on success).
    """
    m = (
        "mutation Register($id: ID!, $translations: [TranslationInput!]!) {"
        "  translationsRegister(resourceId: $id, translations: $translations) {"
        "    userErrors { field message }"
        "    translations { key locale }"
        "  }"
        "}"
    )
    data = await _post_graphql(m, {"id": resource_id, "translations": translations})
    graphql_errors = data.get("errors") or []
    if graphql_errors:
        out: list[dict[str, Any]] = []
        for err in graphql_errors:
            field = err.get("path") or err.get("field") or ["graphql"]
            message = str(err.get("message") or "Unknown GraphQL error")
            out.append({"field": field, "message": message})
        return out
    return (((data.get("data") or {}).get("translationsRegister") or {}).get("userErrors")) or []


async def get_resource_translations(resource_id: str, locale: str) -> dict[str, str]:
    q = (
        "query ResourceTranslations($id: ID!, $locale: String!) {"
        "  translatableResource(resourceId: $id) {"
        "    resourceId"
        "    translations(locale: $locale) { key value }"
        "  }"
        "}"
    )
    data = await _post_graphql(q, {"id": resource_id, "locale": locale})
    items = (((data.get("data") or {}).get("translatableResource") or {}).get("translations")) or []
    return {str(x.get("key") or ""): str(x.get("value") or "") for x in items if x.get("key")}


async def get_resource_translations_by_ids(
    resource_ids: list[str],
    locale: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Fetch current translations for multiple translatable resources.

    Values marked as outdated are returned so the reconciliation planner can
    explicitly decide to retranslate them instead of accidentally reusing them.
    """
    matrix = await get_resource_translation_matrix(resource_ids, [locale])
    return {resource_id: locales.get(locale, {}) for resource_id, locales in matrix.items()}


async def get_resource_translation_matrix(
    resource_ids: list[str],
    locales: list[str],
) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Fetch multiple locales while querying exactly one resource at a time.

    Shopify has returned cross-resource translation data when several
    ``translatableResource`` aliases were requested together. Keeping the
    one-resource boundary is deliberate; locale aliases are safe and avoid
    fetching the same resource twice for the editor.
    """
    if not resource_ids:
        return {}
    query_locales = sorted(
        {str(locale or "").strip() for locale in locales if str(locale or "").strip()}
    )
    if not query_locales:
        return {resource_id: {} for resource_id in resource_ids}
    for query_locale in query_locales:
        if not query_locale or any(not (char.isalnum() or char == "-") for char in query_locale):
            raise ValueError(f"Invalid Shopify locale: {query_locale!r}")
    locale_suffixes = {
        query_locale: "".join(char if char.isalnum() else "_" for char in query_locale)
        for query_locale in query_locales
    }

    async def _fetch_one(resource_id: str) -> dict[str, Any] | None:
        translation_fields = "".join(
            (
                f"    translations_{locale_suffixes[query_locale]}: "
                f'translations(locale: "{query_locale}") '
                "{ key value outdated }"
            )
            for query_locale in query_locales
        )
        query = (
            "query ResourceTranslationMatrix($id: ID!) {"
            "  translatableResource(resourceId: $id) {"
            "    resourceId"
            f"{translation_fields}"
            "  }"
            "}"
        )
        data = await _post_graphql(query, {"id": resource_id})
        return (data.get("data") or {}).get("translatableResource")

    semaphore = asyncio.Semaphore(4)

    async def _guarded(resource_id: str) -> dict[str, Any] | None:
        async with semaphore:
            return await _fetch_one(resource_id)

    nodes = await asyncio.gather(*[_guarded(resource_id) for resource_id in resource_ids])
    out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for node in nodes:
        if not node:
            continue
        resource_id = str(node.get("resourceId") or "")
        if not resource_id:
            continue
        locale_map: dict[str, dict[str, dict[str, Any]]] = {}
        for query_locale in query_locales:
            translations: dict[str, dict[str, Any]] = {}
            for item in node.get(f"translations_{locale_suffixes[query_locale]}") or []:
                key = str(item.get("key") or "")
                if not key:
                    continue
                translations[key] = {
                    "value": str(item.get("value") or ""),
                    "outdated": bool(item.get("outdated")),
                }
            locale_map[query_locale] = translations
        out[resource_id] = locale_map
    return out


async def search_products(query: str, *, first: int = 20) -> list[dict[str, str]]:
    q = (
        "query ProductEditorSearch($first: Int!, $query: String) {"
        "  products(first: $first, query: $query, sortKey: UPDATED_AT, reverse: true) {"
        "    nodes { id title handle status updatedAt }"
        "  }"
        "}"
    )
    data = await _post_graphql(
        q,
        {
            "first": min(max(1, int(first)), 25),
            "query": str(query or "").strip() or None,
        },
    )
    nodes = (((data.get("data") or {}).get("products") or {}).get("nodes")) or []
    return [
        {
            "id": str(node.get("id") or ""),
            "title": str(node.get("title") or ""),
            "handle": str(node.get("handle") or ""),
            "status": str(node.get("status") or ""),
            "updated_at": str(node.get("updatedAt") or ""),
        }
        for node in nodes
        if node.get("id")
    ]


async def get_product_summary(product_gid: str) -> dict[str, str] | None:
    q = (
        "query ProductEditorSummary($id: ID!) {"
        "  product(id: $id) {"
        "    id title handle status updatedAt vendor"
        "    onlineStoreUrl onlineStorePreviewUrl"
        "    images(first: 1) { nodes { url altText } }"
        "    priceRangeV2 {"
        "      minVariantPrice { amount currencyCode }"
        "      maxVariantPrice { amount currencyCode }"
        "    }"
        "  }"
        "}"
    )
    data = await _post_graphql(q, {"id": product_gid})
    node = (data.get("data") or {}).get("product")
    if not node:
        return None
    preview_images = ((node.get("images") or {}).get("nodes")) or []
    preview_image = preview_images[0] if preview_images else {}
    price_range = node.get("priceRangeV2") or {}
    minimum_price = price_range.get("minVariantPrice") or {}
    maximum_price = price_range.get("maxVariantPrice") or {}
    return {
        "id": str(node.get("id") or ""),
        "title": str(node.get("title") or ""),
        "handle": str(node.get("handle") or ""),
        "status": str(node.get("status") or ""),
        "updated_at": str(node.get("updatedAt") or ""),
        "vendor": str(node.get("vendor") or ""),
        "online_store_url": str(node.get("onlineStoreUrl") or ""),
        "online_store_preview_url": str(node.get("onlineStorePreviewUrl") or ""),
        "featured_image_url": str(preview_image.get("url") or ""),
        "featured_image_alt": str(preview_image.get("altText") or ""),
        "minimum_price": str(minimum_price.get("amount") or ""),
        "maximum_price": str(maximum_price.get("amount") or ""),
        "currency_code": str(
            minimum_price.get("currencyCode") or maximum_price.get("currencyCode") or ""
        ),
    }


async def get_product_all_metafields(
    product_gid: str, allowed_types: list[str] | None = None
) -> list[dict]:
    """List all metafields for a product, optionally filtering by Shopify metafield `type`.
    Returns nodes with id, namespace, key, type, value. Paginates by 250.
    """
    q = (
        "query MFAll($pid: ID!, $cursor: String) {"
        "  product(id: $pid) {"
        "    metafields(first: 250, after: $cursor) {"
        "      pageInfo { hasNextPage endCursor }"
        "      edges { node { id namespace key type value } }"
        "    }"
        "  }"
        "}"
    )
    out: list[dict] = []
    cursor: str | None = None
    while True:
        data = await _post_graphql(q, {"pid": product_gid, "cursor": cursor})
        mf = ((data.get("data") or {}).get("product") or {}).get("metafields") or {}
        edges = mf.get("edges") or []
        for e in edges:
            node = (e or {}).get("node")
            if not node:
                continue
            if allowed_types:
                t = (node.get("type") or "").lower()
                if t not in [x.lower() for x in allowed_types]:
                    continue
            out.append(node)
        pi = mf.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
        if not cursor:
            break
    return out


async def get_product_option_resources(product_gid: str) -> list[dict[str, str]]:
    q = (
        "query ProductOptions($pid: ID!) {"
        "  product(id: $pid) {"
        "    options {"
        "      id"
        "      name"
        "      optionValues { id name }"
        "    }"
        "  }"
        "}"
    )
    data = await _post_graphql(q, {"pid": product_gid})
    options = (((data.get("data") or {}).get("product") or {}).get("options")) or []
    out: list[dict[str, str]] = []
    for option in options:
        option_id = str(option.get("id") or "")
        option_name = str(option.get("name") or "")
        if option_id and option_name:
            out.append({"resource_id": option_id, "kind": "option_name"})
        for value in option.get("optionValues") or []:
            value_id = str(value.get("id") or "")
            value_name = str(value.get("name") or "")
            if value_id and value_name:
                out.append({"resource_id": value_id, "kind": "option_value"})
    return out

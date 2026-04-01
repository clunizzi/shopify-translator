from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

from src.config.settings import SETTINGS

logger = structlog.get_logger()


def make_product_gid(numeric_id: int | str) -> str:
    return f"gid://shopify/Product/{int(numeric_id)}"


async def _post_graphql(query: str, variables: dict | None = None) -> dict:
    endpoint = f"https://{SETTINGS.shopify_domain}/admin/api/{getattr(SETTINGS, 'shopify_api_version', '2024-07')}/graphql.json"
    token = SETTINGS.shopify_token
    if not endpoint or not token:
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
                if "errors" in data:
                    logger.warning("shopify_graphql_errors", errors=data.get("errors"))
                return data
            except Exception as e:  # pragma: no cover - retry path
                last_exc = e
                if attempt >= retries:
                    break
                await asyncio.sleep(backoff * (2 ** attempt))
        assert last_exc is not None
        raise last_exc


async def get_product_metafields_by_keys(product_gid: str, keys: list[tuple[str, str]]) -> list[dict]:
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
    data = await _post_graphql(q, {"ids": ids})
    nodes = (((data.get("data") or {}).get("translatableResourcesByIds") or {}).get("nodes")) or []
    out: dict[str, list[dict]] = {}
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
    conn = (((data.get("data") or {}).get("translatableResources")) or {})
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


async def get_product_all_metafields(product_gid: str, allowed_types: list[str] | None = None) -> list[dict]:
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
        mf = (((data.get("data") or {}).get("product") or {}).get("metafields") or {})
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

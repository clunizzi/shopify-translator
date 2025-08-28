from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# Estrattore id numerico da GID Shopify
_GID_NUM_RE = re.compile(r"/(\d+)$")


def extract_numeric_id(gid: str | None) -> int | None:
    """'gid://shopify/Product/12345' -> 12345."""
    if not gid or not isinstance(gid, str):
        return None
    m = _GID_NUM_RE.search(gid)
    return int(m.group(1)) if m else None


def _snippet(obj: Any, limit: int = 400) -> str:
    """Stringa compatta per log; tollerante."""
    try:
        s = json.dumps(obj, ensure_ascii=False) if isinstance(obj, (dict, list)) else str(obj)
        return (s[:limit] + "…") if len(s) > limit else s
    except Exception:
        return "<unserializable>"


class ShopifyGraphQLClient:
    """
    Client GraphQL minimale per Shopify Admin.
    - Costruisce endpoint
    - Gestisce auth header
    - POST con timeout e log sintetici
    """

    def __init__(
        self,
        store_domain: str | None = None,
        token: str | None = None,
        api_version: str = "2024-07",
        *,
        endpoint: str | None = None,
        timeout: float = 30.0,
    ):
        if endpoint:
            self.endpoint = endpoint
        else:
            if not store_domain:
                raise RuntimeError("store_domain mancante per costruire l'endpoint.")
            self.endpoint = f"https://{store_domain}/admin/api/{api_version}/graphql.json"
        if not token:
            raise RuntimeError("token Shopify mancante.")
        self.token = token
        self.api_version = api_version
        self.timeout = timeout
        self.headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.token,
        }

    @classmethod
    def from_settings(cls, settings: Any) -> ShopifyGraphQLClient:
        """Factory standardizzata dal blocco SETTINGS."""
        store = (
            getattr(settings, "shopify_domain", None)
            or getattr(settings, "shopify_store", None)
            or getattr(settings, "shopify_store_domain", None)
        )
        if not store:
            raise RuntimeError(
                "Config mancante: SETTINGS.shopify_domain (es. 'myshop.myshopify.com')."
            )
        api_version = getattr(settings, "shopify_api_version", "2024-07")
        token = getattr(settings, "shopify_token", None) or getattr(
            settings, "shopify_access_token", None
        )
        if not token:
            raise RuntimeError("Config mancante: SETTINGS.shopify_token.")
        timeout = getattr(settings, "http_timeout", 30.0)
        return cls(store_domain=store, token=token, api_version=api_version, timeout=timeout)

    def _post(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.endpoint, headers=self.headers, json=payload)
            logger.debug(
                "shopify_graphql_post",
                status_code=resp.status_code,
                endpoint=self.endpoint,
                payload_snippet=_snippet(payload, 300),
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.warning("shopify_graphql_errors", errors=_snippet(data["errors"], 300))
            return data

    # ---------------------------
    # Query helper ad-hoc usate
    # ---------------------------

    @staticmethod
    def _make_product_gid(numeric_id: int | str) -> str:
        return f"gid://shopify/Product/{int(numeric_id)}"

    def get_product_status_map(
        self, numeric_ids: Iterable[int], batch_size: int | None = None
    ) -> dict[int, str]:
        """
        Ritorna {id_numerico: 'ACTIVE'|'DRAFT'|'ARCHIVED'} usando nodes().
        """
        ids = [int(i) for i in numeric_ids]
        if not ids:
            return {}
        q = (
            "query ProductStatus($ids:[ID!]!) {"
            "  nodes(ids: $ids) {"
            "    __typename"
            "    ... on Product { id status }"
            "  }"
            "}"
        )
        out: dict[int, str] = {}
        bs = int(batch_size or 50)
        for i in range(0, len(ids), bs):
            chunk = ids[i : i + bs]
            gids = [self._make_product_gid(x) for x in chunk]
            data = self._post(q, {"ids": gids})
            nodes = (data.get("data") or {}).get("nodes") or []
            for node in nodes:
                if not node or node.get("__typename") != "Product":
                    continue
                gid = node.get("id") or ""
                num = extract_numeric_id(gid)
                status = (node.get("status") or "").upper()
                if num is not None and status:
                    out[num] = status
            logger.info(
                "shopify_product_status_batch",
                count=len(chunk),
                mapped=len(out),
            )
        return out

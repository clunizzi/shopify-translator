from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config.settings import SETTINGS

logger = structlog.get_logger("shopify")


def _hash_text(s: str) -> str:
    try:
        data = s.encode("utf-8", errors="ignore")
    except Exception:
        data = str(s).encode("utf-8", errors="ignore")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _snippet(obj: object, limit: int) -> str:
    try:
        if isinstance(obj, dict | list):  # UP038
            s = json.dumps(obj, ensure_ascii=False)
        else:
            s = str(obj)
    except Exception:
        s = "<unserializable>"
    return s[: max(0, limit)]


class ShopifyGraphQLClient:
    def __init__(self, store_domain: str, token: str, timeout: float = 20.0) -> None:
        self.endpoint = f"https://{store_domain}/admin/api/2024-07/graphql.json"
        # NON loggare né esporre mai questo header nei log
        self.headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        self.timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(httpx.HTTPError),
    )
    def _post(self, query: str, variables: dict | None = None) -> dict:
        """Esegue una richiesta GraphQL con retry e logging opzionale (sanificato)."""
        payload = {"query": query, "variables": variables or {}}

        if SETTINGS.log_payloads:
            logger.info(
                "api_request",
                api="shopify",
                op="graphql",
                endpoint=self.endpoint,
                query_hash=_hash_text(query),
                variables_hash=_hash_text(json.dumps(payload["variables"], ensure_ascii=False)),
                snippet_query=_snippet(query, SETTINGS.log_payload_max),
                snippet_variables=_snippet(payload["variables"], SETTINGS.log_payload_max),
            )

        t0 = time.perf_counter()
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.endpoint, headers=self.headers, json=payload)
        dt_ms = int((time.perf_counter() - t0) * 1000)

        # Alza per 4xx/5xx
        resp.raise_for_status()
        data = resp.json()

        has_errors = bool(data.get("errors"))
        if SETTINGS.log_payloads:
            # Non logghiamo l'intero 'data'; solo flag errori e snippet errors
            logger.info(
                "api_response",
                api="shopify",
                op="graphql",
                status=resp.status_code,
                duration_ms=dt_ms,
                has_errors=has_errors,
                snippet_errors=_snippet(data.get("errors", ""), SETTINGS.log_payload_max),
            )

        if has_errors:
            # Rendi il messaggio conciso ma informativo
            raise httpx.HTTPError(f"Shopify GraphQL errors: {data['errors']}")
        return data

    @staticmethod
    def to_gid(product_id: int) -> str:
        return f"gid://shopify/Product/{int(product_id)}"

    def get_product_status_map(
        self, numeric_ids: Iterable[int], batch_size: int | None = None
    ) -> dict[int, str]:
        """Restituisce {id_numerico: status} in batch usando nodes()."""
        ids = [int(i) for i in numeric_ids]
        if not ids:
            return {}
        size = batch_size or SETTINGS.batch_size
        out: dict[int, str] = {}
        query = """
        query Nodes($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on Product {
              id
              status
            }
          }
        }
        """.strip()
        for i in range(0, len(ids), size):
            chunk = ids[i : i + size]
            gids = [self.to_gid(x) for x in chunk]
            data = self._post(query, {"ids": gids})
            nodes = data.get("data", {}).get("nodes", [])
            for node in nodes:
                if not node:
                    continue
                gid = node.get("id")
                status = node.get("status")
                if gid and status:
                    try:
                        num = int(gid.rsplit("/", 1)[-1])
                        out[num] = status
                    except Exception:
                        continue
        # Log di riepilogo (senza payload)
        logger.info(
            "shopify_status_summary",
            total=len(ids),
            active=sum(1 for v in out.values() if v == "ACTIVE"),
            returned=len(out),
        )
        return out

from __future__ import annotations

from collections.abc import Iterable

import structlog

from src.config.settings import SETTINGS
from src.shopify.graphql_client import ShopifyGraphQLClient, extract_numeric_id

logger = structlog.get_logger()

QUERY = """
query MetafieldOwners($ids:[ID!]!) {
  nodes(ids: $ids) {
    __typename
    ... on Metafield {
      id
      namespace
      key
      ownerType
      owner {
        __typename
        ... on Product { id }
        ... on Collection { id }
        ... on Customer { id }
      }
    }
  }
}
"""


def make_metafield_gid(numeric_id: int | str) -> str:
    return f"gid://shopify/Metafield/{int(numeric_id)}"


def get_metafields_owner_map(
    numeric_ids: Iterable[int], batch_size: int | None = None
) -> dict[int, dict]:
    """
    Ritorna:
      {
        metafield_numeric_id: {
            "key": str,
            "owner_type": str,             # es. "PRODUCT", "COLLECTION", ...
            "owner_gid": str | None,
            "owner_numeric": int | None,
        },
        ...
      }
    """
    ids = [int(i) for i in numeric_ids]
    if not ids:
        return {}

    client = ShopifyGraphQLClient.from_settings(SETTINGS)

    out: dict[int, dict] = {}
    bs = int(batch_size or getattr(SETTINGS, "shopify_batch", 50))
    for i in range(0, len(ids), bs):
        chunk = ids[i : i + bs]
        gids = [make_metafield_gid(x) for x in chunk]
        data = client._post(QUERY, {"ids": gids})
        nodes = (data.get("data") or {}).get("nodes") or []

        for node in nodes:
            if not node or node.get("__typename") != "Metafield":
                continue
            mf_gid: str = node.get("id") or ""
            mf_numeric = extract_numeric_id(mf_gid)
            key = node.get("key") or ""
            owner_type = node.get("ownerType") or ""
            owner = node.get("owner") or {}
            owner_gid = owner.get("id")
            owner_numeric = extract_numeric_id(owner_gid) if owner_gid else None

            if mf_numeric is not None:
                out[mf_numeric] = {
                    "key": key,
                    "owner_type": owner_type,
                    "owner_gid": owner_gid,
                    "owner_numeric": owner_numeric,
                }

        logger.info(
            "metafield_owner_resolved_batch",
            count=len(chunk),
            mapped=len(out),
        )
    return out

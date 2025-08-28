from __future__ import annotations

from collections.abc import Iterable

from src.config.settings import SETTINGS
from src.shopify.graphql_client import ShopifyGraphQLClient


def _has_shopify_cfg() -> bool:
    token = getattr(SETTINGS, "shopify_token", "") or getattr(SETTINGS, "shopify_access_token", "")
    domain = (
        getattr(SETTINGS, "shopify_domain", "")
        or getattr(SETTINGS, "shopify_store", "")
        or getattr(SETTINGS, "shopify_store_domain", "")
    )
    return bool(token and domain)


def get_active_products_map(ids: Iterable[int], dry_run: bool = False) -> dict[int, bool]:
    """
    Ritorna {id: True/False} dove True = ACTIVE.
    In dry-run (o config assente) considera tutti attivi.
    """
    ids_list = [int(i) for i in ids]
    if dry_run or not _has_shopify_cfg() or not ids_list:
        return {i: True for i in ids_list}

    client = ShopifyGraphQLClient.from_settings(SETTINGS)
    batch = getattr(SETTINGS, "shopify_batch", None) or getattr(SETTINGS, "batch_size", 50)
    status_map = client.get_product_status_map(ids_list, batch_size=batch)
    return {i: (status_map.get(i, "").upper() == "ACTIVE") for i in ids_list}

from __future__ import annotations

from collections.abc import Iterable

from src.config.settings import SETTINGS
from src.shopify.graphql_client import ShopifyGraphQLClient


def get_active_products_map(ids: Iterable[int], dry_run: bool = False) -> dict[int, bool]:
    """Ritorna {id: True/False} dove True = ACTIVE."""
    ids_list = list(ids)
    if dry_run or not SETTINGS.has_shopify:
        return {i: True for i in ids_list}  # in dry-run consideriamo tutti attivi
    client = ShopifyGraphQLClient(SETTINGS.shopify_domain, SETTINGS.shopify_token)
    status_map = client.get_product_status_map(ids_list, batch_size=SETTINGS.batch_size)
    return {i: (status_map.get(i, "").upper() == "ACTIVE") for i in ids_list}

import asyncio
from types import SimpleNamespace

import pytest

from src.shopify import graphql


def test_get_main_theme_returns_single_main(monkeypatch):
    async def _fake_post_graphql(query, variables=None):
        return {
            "data": {
                "themes": {
                    "nodes": [
                        {
                            "id": "gid://shopify/OnlineStoreTheme/123456789012",
                            "name": "Example Store Live",
                            "role": "MAIN",
                            "updatedAt": "2026-07-22T09:21:35Z",
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(graphql, "_post_graphql", _fake_post_graphql)

    result = asyncio.run(graphql.get_main_theme())

    assert result == {
        "id": "gid://shopify/OnlineStoreTheme/123456789012",
        "name": "Example Store Live",
        "role": "MAIN",
        "updated_at": "2026-07-22T09:21:35Z",
    }


def test_post_graphql_raises_on_top_level_errors(monkeypatch):
    class _Response:
        status_code = 200
        headers = {"x-shopify-api-version": "2026-07"}
        request = object()

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "errors": [
                    {
                        "message": "Access denied",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ]
            }

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(graphql.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        graphql,
        "SETTINGS",
        SimpleNamespace(
            shopify_domain="example.myshopify.com",
            shopify_token="token",
            shopify_api_version="2026-07",
            request_timeout=30,
            retries=2,
        ),
    )

    with pytest.raises(graphql.ShopifyGraphQLError, match="Access denied"):
        asyncio.run(graphql._post_graphql("query { shop { name } }"))

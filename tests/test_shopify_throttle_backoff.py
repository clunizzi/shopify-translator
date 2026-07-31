import asyncio
from types import SimpleNamespace

from src.shopify import graphql


class _Response:
    status_code = 200
    headers = {"x-shopify-api-version": "2026-07"}
    request = object()

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 24,
                    "throttleStatus": {
                        "currentlyAvailable": 4,
                        "restoreRate": 100,
                    },
                }
            },
        }


def test_throttle_retry_uses_shopify_budget_backoff(monkeypatch):
    sleeps = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return _Response()

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(graphql.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr(graphql.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        graphql,
        "SETTINGS",
        SimpleNamespace(
            shopify_api_version="2026-07",
            shopify_domain="example.myshopify.com",
            shopify_token="test-token",
            request_timeout=1,
            retries=1,
        ),
    )

    try:
        asyncio.run(graphql._post_graphql("query { shop { id } }"))
    except graphql.ShopifyGraphQLError:
        pass
    else:
        raise AssertionError("expected throttled error")

    assert sleeps
    assert sleeps[0] >= 0.7

import asyncio
from datetime import UTC, datetime

from src.aws_lambda import catalog_poller


def _cfg(*, dry_run=False):
    return catalog_poller.CatalogPollerConfig(
        enabled=True,
        dry_run=dry_run,
        shop_domain="example-store.myshopify.com",
        sqs_url="https://sqs.example/queue",
        checkpoint_table="snapshots",
        shopify_admin_token_secret_arn="",
        initial_lookback_hours=24,
        overlap_seconds=300,
        max_pages=5,
    )


def test_fetch_products_paginates_updated_active_catalog(monkeypatch):
    calls = []

    async def _fake_post_graphql(query, variables):
        calls.append(variables)
        if variables["after"] is None:
            return {
                "data": {
                    "products": {
                        "nodes": [
                            {
                                "id": "gid://shopify/Product/1",
                                "updatedAt": "2026-07-27T10:00:00Z",
                                "status": "ACTIVE",
                            }
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                    }
                }
            }
        return {
            "data": {
                "products": {
                    "nodes": [
                        {
                            "id": "gid://shopify/Product/2",
                            "updatedAt": "2026-07-27T10:01:00Z",
                            "status": "ACTIVE",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    from src.shopify import graphql

    monkeypatch.setattr(graphql, "_post_graphql", _fake_post_graphql)
    products = asyncio.run(
        catalog_poller._fetch_products(
            _cfg(),
            since=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
            cutoff=datetime(2026, 7, 27, 11, 0, tzinfo=UTC),
            full_scan=False,
        )
    )

    assert [product["id"] for product in products] == ["1", "2"]
    assert calls[0]["after"] is None
    assert calls[1]["after"] == "next"
    assert "status:active" in calls[0]["query"]
    assert "updated_at:>" in calls[0]["query"]


def test_dry_run_reads_with_overlap_without_queue_or_checkpoint_writes(monkeypatch):
    checkpoint = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    seen = {}

    monkeypatch.setattr(catalog_poller, "_read_checkpoint", lambda cfg: checkpoint)

    async def _fake_fetch(cfg, *, since, cutoff, full_scan):
        seen["since"] = since
        seen["cutoff"] = cutoff
        return [{"id": "1", "gid": "gid://shopify/Product/1", "updated_at": "", "status": "ACTIVE"}]

    monkeypatch.setattr(catalog_poller, "_fetch_products", _fake_fetch)
    monkeypatch.setattr(
        catalog_poller,
        "_enqueue_products",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )
    monkeypatch.setattr(
        catalog_poller,
        "_write_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    result = asyncio.run(
        catalog_poller._run(
            _cfg(dry_run=True),
            full_scan=False,
            now=datetime(2026, 7, 27, 11, 0, tzinfo=UTC),
        )
    )

    assert seen["since"] == datetime(2026, 7, 27, 9, 55, tzinfo=UTC)
    assert result["products_found"] == 1
    assert result["products_enqueued"] == 0
    assert result["checkpoint_written"] is False


def test_enqueue_batches_ten_messages_and_sets_idempotency_attributes(monkeypatch):
    class _FakeSqs:
        def __init__(self):
            self.calls = []

        def send_message_batch(self, **kwargs):
            self.calls.append(kwargs)
            return {"Successful": [{"Id": entry["Id"]} for entry in kwargs["Entries"]]}

    sqs = _FakeSqs()
    monkeypatch.setattr(catalog_poller, "_sqs", lambda: sqs)
    products = [
        {
            "id": str(index),
            "gid": f"gid://shopify/Product/{index}",
            "updated_at": "2026-07-27T10:00:00Z",
            "status": "ACTIVE",
        }
        for index in range(23)
    ]

    enqueued = catalog_poller._enqueue_products(_cfg(), products=products)

    assert enqueued == 23
    assert [len(call["Entries"]) for call in sqs.calls] == [10, 10, 3]
    first = sqs.calls[0]["Entries"][0]
    assert first["MessageAttributes"]["Topic"]["StringValue"] == "products/update"
    assert first["MessageAttributes"]["EventId"]["StringValue"].startswith(
        "catalog-poller:gid://shopify/Product/0:"
    )

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

from src.aws_lambda.worker import _get_secret
from src.logging_setup import configure_logging

CHECKPOINT_PK = "CONTROL#catalog-poller"
CHECKPOINT_SK = "CHECKPOINT"


@dataclass(frozen=True)
class CatalogPollerConfig:
    enabled: bool
    dry_run: bool
    shop_domain: str
    sqs_url: str
    checkpoint_table: str
    shopify_admin_token_secret_arn: str
    initial_lookback_hours: int
    overlap_seconds: int
    max_pages: int


_DYNAMODB = None
_SQS = None


def _bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _config() -> CatalogPollerConfig:
    return CatalogPollerConfig(
        enabled=_bool(os.environ.get("CATALOG_POLL_ENABLED")),
        dry_run=_bool(os.environ.get("DRY_RUN")),
        shop_domain=os.environ.get("SHOP_DOMAIN", "").strip(),
        sqs_url=os.environ.get("SQS_URL", "").strip(),
        checkpoint_table=os.environ.get("DDB_TABLE", "").strip(),
        shopify_admin_token_secret_arn=os.environ.get(
            "SHOPIFY_ADMIN_TOKEN_SECRET_ARN",
            "",
        ).strip(),
        initial_lookback_hours=max(
            1,
            int(os.environ.get("CATALOG_POLL_INITIAL_LOOKBACK_HOURS", "720")),
        ),
        overlap_seconds=max(
            0,
            int(os.environ.get("CATALOG_POLL_OVERLAP_SECONDS", "300")),
        ),
        max_pages=max(
            1,
            int(os.environ.get("CATALOG_POLL_MAX_PAGES", "50")),
        ),
    )


def _dynamodb():
    global _DYNAMODB
    if _DYNAMODB is None:
        _DYNAMODB = boto3.resource("dynamodb")
    return _DYNAMODB


def _sqs():
    global _SQS
    if _SQS is None:
        _SQS = boto3.client("sqs")
    return _SQS


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _checkpoint_table(cfg: CatalogPollerConfig):
    return _dynamodb().Table(cfg.checkpoint_table)


def _read_checkpoint(cfg: CatalogPollerConfig) -> datetime | None:
    item = (
        _checkpoint_table(cfg)
        .get_item(
            Key={"pk": CHECKPOINT_PK, "sk": CHECKPOINT_SK},
            ConsistentRead=True,
        )
        .get("Item")
        or {}
    )
    return _parse_utc(item.get("last_success_at"))


def _write_checkpoint(
    cfg: CatalogPollerConfig,
    *,
    cutoff: datetime,
    products_enqueued: int,
) -> None:
    _checkpoint_table(cfg).put_item(
        Item={
            "pk": CHECKPOINT_PK,
            "sk": CHECKPOINT_SK,
            "last_success_at": _iso_utc(cutoff),
            "products_enqueued": int(products_enqueued),
            "updated_at": _iso_utc(datetime.now(UTC)),
        }
    )


async def _fetch_products(
    cfg: CatalogPollerConfig,
    *,
    since: datetime,
    cutoff: datetime,
    full_scan: bool,
) -> list[dict[str, str]]:
    from src.shopify.graphql import _post_graphql

    query = (
        "query UpdatedProducts($first: Int!, $after: String, $query: String!) {"
        "  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {"
        "    nodes { id updatedAt status }"
        "    pageInfo { hasNextPage endCursor }"
        "  }"
        "}"
    )
    search = "status:active"
    if not full_scan:
        search += f" updated_at:>'{_iso_utc(since)}' updated_at:<='{_iso_utc(cutoff)}'"

    products: list[dict[str, str]] = []
    cursor: str | None = None
    for _page in range(cfg.max_pages):
        data = await _post_graphql(
            query,
            {
                "first": 250,
                "after": cursor,
                "query": search,
            },
        )
        connection = (data.get("data") or {}).get("products") or {}
        for node in connection.get("nodes") or []:
            product_gid = str(node.get("id") or "")
            if not product_gid:
                continue
            products.append(
                {
                    "id": product_gid.rsplit("/", 1)[-1],
                    "gid": product_gid,
                    "updated_at": str(node.get("updatedAt") or ""),
                    "status": str(node.get("status") or ""),
                }
            )
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return products
        cursor = str(page_info.get("endCursor") or "")
        if not cursor:
            raise RuntimeError("Shopify products pagination returned no endCursor")
    raise RuntimeError(f"Catalog poll exceeded max_pages={cfg.max_pages}")


def _enqueue_products(
    cfg: CatalogPollerConfig,
    *,
    products: list[dict[str, str]],
) -> int:
    enqueued = 0
    for start in range(0, len(products), 10):
        batch = products[start : start + 10]
        entries = []
        for index, product in enumerate(batch):
            event_id = f"catalog-poller:{product['gid']}:{product['updated_at']}"
            entries.append(
                {
                    "Id": str(index),
                    "MessageBody": json.dumps(product, ensure_ascii=False),
                    "MessageAttributes": {
                        "Topic": {
                            "DataType": "String",
                            "StringValue": "products/update",
                        },
                        "Shop": {
                            "DataType": "String",
                            "StringValue": cfg.shop_domain,
                        },
                        "EventId": {
                            "DataType": "String",
                            "StringValue": event_id,
                        },
                    },
                }
            )
        response = _sqs().send_message_batch(
            QueueUrl=cfg.sqs_url,
            Entries=entries,
        )
        failures = response.get("Failed") or []
        if failures:
            raise RuntimeError(f"SQS failed to enqueue {len(failures)} catalog item(s)")
        enqueued += len(entries)
    return enqueued


async def _run(
    cfg: CatalogPollerConfig,
    *,
    full_scan: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    cutoff = (now or datetime.now(UTC)).astimezone(UTC)
    checkpoint = None if full_scan else _read_checkpoint(cfg)
    if checkpoint is None:
        since = cutoff - timedelta(hours=cfg.initial_lookback_hours)
    else:
        since = checkpoint - timedelta(seconds=cfg.overlap_seconds)

    products = await _fetch_products(
        cfg,
        since=since,
        cutoff=cutoff,
        full_scan=full_scan,
    )
    enqueued = 0 if cfg.dry_run else _enqueue_products(cfg, products=products)
    if not cfg.dry_run:
        _write_checkpoint(
            cfg,
            cutoff=cutoff,
            products_enqueued=enqueued,
        )
    return {
        "ok": True,
        "component": "catalog_poller",
        "full_scan": full_scan,
        "dry_run": cfg.dry_run,
        "since": _iso_utc(since),
        "cutoff": _iso_utc(cutoff),
        "products_found": len(products),
        "products_enqueued": enqueued,
        "checkpoint_written": not cfg.dry_run,
    }


def handler(event, context):
    configure_logging()
    cfg = _config()
    if not cfg.enabled:
        result = {
            "ok": True,
            "component": "catalog_poller",
            "skip": "disabled",
        }
        print(json.dumps(result))
        return result
    if not cfg.shop_domain or not cfg.checkpoint_table or not cfg.sqs_url:
        raise RuntimeError("Catalog poller configuration is incomplete")
    if cfg.shopify_admin_token_secret_arn:
        token = _get_secret(cfg.shopify_admin_token_secret_arn)
        os.environ["SHOPIFY_ADMIN_TOKEN"] = token
        os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"] = token
    os.environ["SHOP_DOMAIN"] = cfg.shop_domain
    os.environ["SHOPIFY_STORE_DOMAIN"] = cfg.shop_domain

    full_scan = _bool((event or {}).get("full_scan"))
    result = asyncio.run(_run(cfg, full_scan=full_scan))
    print(json.dumps(result, ensure_ascii=False))
    return result

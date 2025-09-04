from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import boto3

from src.snapshot.sqlite_snapshot import SnapshotStore


import boto3

dynamodb = boto3.resource("dynamodb")
secrets = boto3.client("secretsmanager")
ddb_snap = dynamodb.Table(os.environ["DDB_TABLE"])  # product_snapshots
ddb_dedup = dynamodb.Table(os.environ["DEDUP_TABLE"])  # webhook_dedup

SOURCE_LOCALE = os.environ.get("SOURCE_LOCALE", "en")
TARGET_LOCALES = [s.strip() for s in os.environ.get("TARGET_LOCALES", "").split(",") if s.strip()]
MF_INCLUDE = [tuple(s.strip().split(".", 1)) for s in os.environ.get("MF_INCLUDE", "").split(",") if "." in s]
MF_JSON_PATHS = [s.strip() for s in os.environ.get("MF_JSON_PATHS", "").split(",") if s.strip()]
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "20"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in {"1", "true", "yes", "y"}

# Secrets Manager ARNs (optional but recommended)
OPENAI_API_KEY_SECRET_ARN = os.environ.get("OPENAI_API_KEY_SECRET_ARN")
SHOPIFY_ADMIN_TOKEN_SECRET_ARN = os.environ.get("SHOPIFY_ADMIN_TOKEN_SECRET_ARN")

_CACHED_SECRETS: dict[str, str] = {}


def _get_secret(arn: str | None) -> str:
    if not arn:
        return ""
    if arn in _CACHED_SECRETS:
        return _CACHED_SECRETS[arn]
    resp = secrets.get_secret_value(SecretId=arn)
    val = resp.get("SecretString") or ""
    _CACHED_SECRETS[arn] = val
    return val


def _gid(product_numeric_id: str | int) -> str:
    try:
        return f"gid://shopify/Product/{int(product_numeric_id)}"
    except Exception:
        return f"gid://shopify/Product/{product_numeric_id}"


def _pk(shop: str, gid: str) -> str:
    return f"{shop}#{gid}"


def _now_epoch() -> int:
    return int(time.time())


async def _prefill_sqlite_snapshot_from_ddb(shop: str, product_gid: str, db_path: str, mf_include: list[tuple[str, str]]):
    """Hydrate local SQLite snapshot with any existing DDB digest maps for product + included metafields."""
    store = SnapshotStore(db_path=db_path)
    # Product digests
    try:
        resp = ddb_snap.get_item(Key={"pk": _pk(shop, product_gid), "sk": f"digest#{SOURCE_LOCALE}"})
        item = resp.get("Item") or {}
        digest_map = item.get("digest_map") or {}
        if digest_map:
            store.set_digest_map(product_gid, {str(k): str(v) for k, v in digest_map.items()})
    except Exception as e:
        print(json.dumps({"prefill_error": str(e)}))

    # Metafields: if include is empty, we skip prefill specific IDs (we don't list all here to keep calls low)
    # Prefill will be refreshed on next runs once digests are known.
    if mf_include:
        from src.shopify.graphql import get_product_metafields_by_keys  # lazy import
        mf_nodes = await get_product_metafields_by_keys(product_gid, mf_include)
    else:
        mf_nodes = []
    for n in mf_nodes:
        rid = n.get("id")
        if not rid:
            continue
        try:
            resp = ddb_snap.get_item(Key={"pk": _pk(shop, rid), "sk": f"digest#{SOURCE_LOCALE}"})
            item = resp.get("Item") or {}
            digest_map = item.get("digest_map") or {}
            if digest_map:
                store.set_digest_map(rid, {str(k): str(v) for k, v in digest_map.items()})
        except Exception as e:
            print(json.dumps({"prefill_error": str(e), "rid": rid}))
    store.close()


def _flush_sqlite_snapshot_to_ddb(shop: str, product_gid: str, db_path: str, changed_rids: list[str] | None = None):
    """Persist local SQLite snapshot digest maps to DynamoDB for product and included metafields.

    Note: We only flush metafields listed in MF_INCLUDE to avoid extra API calls. This ensures that
    on cold starts we won't reprocess unchanged metafields due to missing local snapshot.
    """
    store = SnapshotStore(db_path=db_path)
    try:
        # For product
        prod_map = store.get_digest_map(product_gid)
        if prod_map:
            ddb_snap.put_item(
                Item={
                    "pk": _pk(shop, product_gid),
                    "sk": f"digest#{SOURCE_LOCALE}",
                    "digest_map": prod_map,
                    "updated_at": _now_epoch(),
                }
            )

        # Flush any changed resource IDs provided by process_product summary (includes metafields)
        for rid in (changed_rids or []):
            if rid == product_gid:
                continue  # already flushed above
            mf_map = store.get_digest_map(rid)
            if mf_map:
                ddb_snap.put_item(
                    Item={
                        "pk": _pk(shop, rid),
                        "sk": f"digest#{SOURCE_LOCALE}",
                        "digest_map": mf_map,
                        "updated_at": _now_epoch(),
                    }
                )
    finally:
        store.close()


async def _process_one(record):
    attrs = record.get("messageAttributes") or {}
    topic = (attrs.get("Topic") or {}).get("stringValue", "")
    shop = (attrs.get("Shop") or {}).get("stringValue", os.environ.get("SHOP_DOMAIN", ""))
    event_id = (attrs.get("EventId") or {}).get("stringValue", "")

    # Dedup by EventId
    if event_id:
        try:
            ddb_dedup.put_item(
                Item={"event_id": event_id, "ttl": _now_epoch() + 3 * 24 * 3600},
                ConditionExpression="attribute_not_exists(event_id)",
            )
        except ddb_dedup.meta.client.exceptions.ConditionalCheckFailedException:
            print(json.dumps({"skip": "dedup", "event_id": event_id}))
            return

    body = json.loads(record.get("body") or "{}")
    product_id = str(body.get("id") or (body.get("product") or {}).get("id") or "")
    if not product_id:
        print(json.dumps({"skip": "no_product_id"}))
        return

    gid = _gid(product_id)
    pk = _pk(shop, gid)

    # Debounce/coalescing window (atomic): proceed only if absent or expired
    now = _now_epoch()
    try:
        ddb_snap.update_item(
            Key={"pk": pk, "sk": f"source#{SOURCE_LOCALE}"},
            UpdateExpression="SET debounce_until=:t",
            ConditionExpression="attribute_not_exists(debounce_until) OR debounce_until < :now",
            ExpressionAttributeValues={":t": now + DEBOUNCE_SECONDS, ":now": now},
        )
    except ddb_snap.meta.client.exceptions.ConditionalCheckFailedException:
        print(json.dumps({"skip": "debounce", "product_id": product_id}))
        return
    except Exception as e:
        print(json.dumps({"debounce_error": str(e)}))

    # Prefill local snapshot from DynamoDB to avoid full-translate on first pass
    db_path = "/tmp/cache.sqlite"
    await _prefill_sqlite_snapshot_from_ddb(shop, gid, db_path, MF_INCLUDE)

    is_create = (topic.lower() == "products/create")
    try:
        # Resolve secrets and expose as env so SETTINGS picks them up
        if OPENAI_API_KEY_SECRET_ARN:
            # Soft-check OpenAI SDK presence (supports v1 and legacy v0)
            try:
                import openai as _openai  # type: ignore
                v1 = hasattr(_openai, "OpenAI")
                v0 = hasattr(_openai, "ChatCompletion")
                ver = getattr(_openai, "__version__", getattr(_openai, "version", "unknown"))
                print(json.dumps({"openai_import_ok": True, "version": ver, "v1": v1, "v0": v0}))
            except Exception as e:
                print(json.dumps({"openai_import_ok": False, "error": str(e)}))
            os.environ["OPENAI_API_KEY"] = _get_secret(OPENAI_API_KEY_SECRET_ARN)
        if SHOPIFY_ADMIN_TOKEN_SECRET_ARN:
            os.environ["SHOPIFY_ADMIN_TOKEN"] = _get_secret(SHOPIFY_ADMIN_TOKEN_SECRET_ARN)
        # Ensure cache path points to writable storage in Lambda
        os.environ.setdefault("TRANSLATION_CACHE_PATH", "/tmp/cache.sqlite")

        # Import here so SETTINGS captures env just set
        from src.shopify.sync import process_product  # type: ignore
        summary = await process_product(
            product_numeric_id=product_id,
            target_locales=TARGET_LOCALES,
            mf_include=MF_INCLUDE,
            mf_json_paths=MF_JSON_PATHS,
            source_locale=SOURCE_LOCALE,
            dry_run=DRY_RUN,
            is_create=is_create,
        )
        # After successful processing, flush latest digests back to DynamoDB
        changed_rids = list((summary.get("changed_keys") or {}).keys()) if isinstance(summary, dict) else []
        _flush_sqlite_snapshot_to_ddb(shop, gid, db_path, changed_rids)
        print(json.dumps({"ok": True, "product_id": product_id, "summary": summary}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "product_id": product_id, "error": str(e)}))


def handler(event, context):
    loop = asyncio.get_event_loop()
    tasks = [_process_one(r) for r in event.get("Records", [])]
    loop.run_until_complete(asyncio.gather(*tasks))
    return {"statusCode": 200}

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3

from src.logging_setup import configure_logging


@dataclass(frozen=True)
class WorkerConfig:
    ddb_table: str
    dedup_table: str
    source_locale: str
    target_locales: list[str]
    mf_include: list[tuple[str, str]]
    debounce_seconds: int
    dry_run: bool
    shop_domain: str
    openai_api_key_secret_arn: str
    shopify_admin_token_secret_arn: str
    neon_database_url_secret_arn: str
    disable_sync: bool
    log_verbose_sync: bool


_DYNAMODB = None
_SECRETS = None
_CACHED_SECRETS: dict[str, str] = {}


def _config() -> WorkerConfig:
    return WorkerConfig(
        ddb_table=os.environ["DDB_TABLE"],
        dedup_table=os.environ["DEDUP_TABLE"],
        source_locale=os.environ.get("SOURCE_LOCALE", "en"),
        target_locales=[s.strip() for s in os.environ.get("TARGET_LOCALES", "").split(",") if s.strip()],
        mf_include=[tuple(s.strip().split(".", 1)) for s in os.environ.get("MF_INCLUDE", "").split(",") if "." in s],
        debounce_seconds=int(os.environ.get("DEBOUNCE_SECONDS", "20")),
        dry_run=os.environ.get("DRY_RUN", "false").lower() in {"1", "true", "yes", "y"},
        shop_domain=os.environ.get("SHOP_DOMAIN", ""),
        openai_api_key_secret_arn=os.environ.get("OPENAI_API_KEY_SECRET_ARN", ""),
        shopify_admin_token_secret_arn=os.environ.get("SHOPIFY_ADMIN_TOKEN_SECRET_ARN", ""),
        neon_database_url_secret_arn=os.environ.get("NEON_DATABASE_URL_SECRET_ARN", ""),
        disable_sync=os.environ.get("DISABLE_SYNC", "false").lower() in {"1", "true", "yes", "y"},
        log_verbose_sync=os.environ.get("LOG_VERBOSE_SYNC", "false").lower() in {"1", "true", "yes", "y"},
    )


def _dynamodb():
    global _DYNAMODB
    if _DYNAMODB is None:
        _DYNAMODB = boto3.resource("dynamodb")
    return _DYNAMODB


def _secrets():
    global _SECRETS
    if _SECRETS is None:
        _SECRETS = boto3.client("secretsmanager")
    return _SECRETS


def _snap_table(cfg: WorkerConfig):
    return _dynamodb().Table(cfg.ddb_table)


def _dedup_table(cfg: WorkerConfig):
    return _dynamodb().Table(cfg.dedup_table)


def _get_secret(arn: str) -> str:
    if not arn:
        return ""
    if arn in _CACHED_SECRETS:
        return _CACHED_SECRETS[arn]
    resp = _secrets().get_secret_value(SecretId=arn)
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


def _message_id(record: dict[str, Any]) -> str:
    return str(record.get("messageId") or record.get("messageID") or "")


def _resolve_product_id(body: dict[str, Any]) -> str:
    return str(body.get("id") or (body.get("product") or {}).get("id") or "")


def _apply_runtime_secrets(cfg: WorkerConfig) -> None:
    if cfg.openai_api_key_secret_arn:
        os.environ["OPENAI_API_KEY"] = _get_secret(cfg.openai_api_key_secret_arn)
    if cfg.shopify_admin_token_secret_arn:
        os.environ["SHOPIFY_ADMIN_TOKEN"] = _get_secret(cfg.shopify_admin_token_secret_arn)
    if cfg.neon_database_url_secret_arn:
        os.environ["NEON_DATABASE_URL"] = _get_secret(cfg.neon_database_url_secret_arn)
    os.environ["TRANSLATION_CACHE_PATH"] = ":memory:"
    os.environ["LOG_VERBOSE_SYNC"] = "true" if cfg.log_verbose_sync else "false"


def _build_log_event(*, product_id: str, topic: str, summary: dict[str, Any]) -> dict[str, Any]:
    item = ((summary.get("items") or [{}])[0]) if isinstance(summary, dict) else {}
    locales = item.get("locales") or {}
    return {
        "ok": True,
        "event": "translation_sync",
        "product_id": int(product_id),
        "product_title": item.get("product_title") or "",
        "topic": topic,
        "status": item.get("status") or "unknown",
        "changed_sections": item.get("changed_sections") or [],
        "target_locales": sorted(list(locales.keys())),
        "translated_sections": {locale: data.get("translated_sections") or [] for locale, data in locales.items()},
        "section_sources": {locale: data.get("section_sources") or {} for locale, data in locales.items()},
    }


def _build_verbose_log_event(summary: dict[str, Any]) -> dict[str, Any] | None:
    item = ((summary.get("items") or [{}])[0]) if isinstance(summary, dict) else {}
    locales = item.get("locales") or {}
    payloads = {
        locale: data.get("shopify_payloads")
        for locale, data in locales.items()
        if data.get("shopify_payloads")
    }
    if not payloads:
        return None
    return {
        "event": "translation_sync_debug",
        "product_id": item.get("product_id"),
        "product_title": item.get("product_title") or "",
        "shopify_payloads": payloads,
    }


def _mark_event_seen(cfg: WorkerConfig, event_id: str) -> bool:
    if not event_id:
        return True
    dedup = _dedup_table(cfg)
    try:
        dedup.put_item(
            Item={"event_id": event_id, "ttl": _now_epoch() + 3 * 24 * 3600},
            ConditionExpression="attribute_not_exists(event_id)",
        )
        return True
    except dedup.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _acquire_debounce(cfg: WorkerConfig, *, shop: str, product_gid: str) -> bool:
    snap = _snap_table(cfg)
    now = _now_epoch()
    try:
        snap.update_item(
            Key={"pk": _pk(shop, product_gid), "sk": f"source#{cfg.source_locale}"},
            UpdateExpression="SET debounce_until = :until, updated_at = :now",
            ConditionExpression="attribute_not_exists(debounce_until) OR debounce_until < :now",
            ExpressionAttributeValues={
                ":until": now + cfg.debounce_seconds,
                ":now": now,
            },
        )
        return True
    except snap.meta.client.exceptions.ConditionalCheckFailedException:
        return False


async def _run_backend(
    *,
    cfg: WorkerConfig,
    product_id: str,
    is_create: bool,
    shop: str,
) -> dict[str, Any]:
    from src.bootstrap.incremental import sync_products_incremental

    return await sync_products_incremental(
        product_ids=[int(product_id)],
        target_locales=cfg.target_locales,
        mf_include=cfg.mf_include or None,
        source_locale=cfg.source_locale,
        apply_translations=not cfg.dry_run,
        dry_run=cfg.dry_run,
        is_create=is_create,
    )


async def _process_one(record: dict[str, Any], cfg: WorkerConfig) -> tuple[bool, str | None]:
    msg_id = _message_id(record)
    attrs = record.get("messageAttributes") or {}
    topic = (attrs.get("Topic") or {}).get("stringValue", "")
    shop = (attrs.get("Shop") or {}).get("stringValue", cfg.shop_domain)
    event_id = (attrs.get("EventId") or {}).get("stringValue", "")

    try:
        body = json.loads(record.get("body") or "{}")
    except Exception as e:
        print(json.dumps({"ok": False, "message_id": msg_id, "error": f"invalid_json:{e}"}))
        return False, msg_id

    product_id = _resolve_product_id(body)
    if not product_id:
        print(json.dumps({"ok": True, "skip": "no_product_id", "message_id": msg_id}))
        return True, None

    gid = _gid(product_id)
    is_create = topic.lower() == "products/create"

    if not _mark_event_seen(cfg, event_id):
        print(json.dumps({"ok": True, "skip": "dedup", "event_id": event_id, "message_id": msg_id}))
        return True, None

    if not _acquire_debounce(cfg, shop=shop, product_gid=gid):
        print(json.dumps({"ok": True, "skip": "debounce", "product_id": product_id, "message_id": msg_id}))
        return True, None

    try:
        _apply_runtime_secrets(cfg)
        summary = await _run_backend(
            cfg=cfg,
            product_id=product_id,
            is_create=is_create,
            shop=shop,
        )
        event = _build_log_event(product_id=product_id, topic=topic, summary=summary)
        event["message_id"] = msg_id
        print(json.dumps(event, ensure_ascii=False))
        if cfg.log_verbose_sync:
            verbose = _build_verbose_log_event(summary)
            if verbose:
                verbose["message_id"] = msg_id
                print(json.dumps(verbose, ensure_ascii=False))
        return True, None
    except Exception as e:
        print(json.dumps({"ok": False, "product_id": product_id, "message_id": msg_id, "error": str(e)}))
        return False, msg_id


def handler(event, context):
    configure_logging()
    cfg = _config()
    records = event.get("Records", [])
    if cfg.disable_sync:
        print(json.dumps({"ok": True, "skip": "disabled", "component": "worker", "records": len(records)}))
        return {"statusCode": 200, "batchItemFailures": []}

    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(asyncio.gather(*[_process_one(r, cfg) for r in records]))
    failures = [{"itemIdentifier": failure_id} for ok, failure_id in results if not ok and failure_id]
    return {"statusCode": 200, "batchItemFailures": failures}

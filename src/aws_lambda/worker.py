from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
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
    seo_sync_enabled: bool = False
    theme_tracking_enabled: bool = False
    theme_realtime_sync_enabled: bool = False
    approved_theme_id: str = ""
    sqs_url: str = ""


_DYNAMODB = None
_SECRETS = None
_SQS = None
_CACHED_SECRETS: dict[str, str] = {}


def _config() -> WorkerConfig:
    return WorkerConfig(
        ddb_table=os.environ["DDB_TABLE"],
        dedup_table=os.environ["DEDUP_TABLE"],
        source_locale=os.environ.get("SOURCE_LOCALE", "en"),
        target_locales=[
            s.strip() for s in os.environ.get("TARGET_LOCALES", "").split(",") if s.strip()
        ],
        mf_include=[
            tuple(s.strip().split(".", 1))
            for s in os.environ.get("MF_INCLUDE", "").split(",")
            if "." in s
        ],
        debounce_seconds=int(os.environ.get("DEBOUNCE_SECONDS", "60")),
        dry_run=os.environ.get("DRY_RUN", "false").lower() in {"1", "true", "yes", "y"},
        shop_domain=os.environ.get("SHOP_DOMAIN", ""),
        openai_api_key_secret_arn=os.environ.get("OPENAI_API_KEY_SECRET_ARN", ""),
        shopify_admin_token_secret_arn=os.environ.get("SHOPIFY_ADMIN_TOKEN_SECRET_ARN", ""),
        neon_database_url_secret_arn=os.environ.get("NEON_DATABASE_URL_SECRET_ARN", ""),
        disable_sync=os.environ.get("DISABLE_SYNC", "false").lower() in {"1", "true", "yes", "y"},
        log_verbose_sync=os.environ.get("LOG_VERBOSE_SYNC", "false").lower()
        in {"1", "true", "yes", "y"},
        seo_sync_enabled=os.environ.get("SEO_SYNC_ENABLED", "false").lower()
        in {"1", "true", "yes", "y"},
        theme_tracking_enabled=os.environ.get("THEME_TRACKING_ENABLED", "false").lower()
        in {"1", "true", "yes", "y"},
        theme_realtime_sync_enabled=os.environ.get(
            "THEME_REALTIME_SYNC_ENABLED",
            "false",
        ).lower()
        in {"1", "true", "yes", "y"},
        approved_theme_id=(
            os.environ.get("APPROVED_THEME_ID", "") or os.environ.get("THEME_ID", "")
        ).strip(),
        sqs_url=os.environ.get("SQS_URL", ""),
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


def _sqs():
    global _SQS
    if _SQS is None:
        _SQS = boto3.client("sqs")
    return _SQS


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


def _coerce_secret_value(raw: str, *, prefer_keys: list[str] | None = None) -> str:
    if not raw:
        return raw
    txt = raw.strip()
    if not (txt.startswith("{") and txt.endswith("}")):
        return raw
    try:
        data = json.loads(txt)
    except Exception:
        return raw
    if isinstance(data, dict):
        keys = prefer_keys or []
        for key in keys:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return raw


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
        raw = _get_secret(cfg.neon_database_url_secret_arn)
        os.environ["NEON_DATABASE_URL"] = _coerce_secret_value(
            raw, prefer_keys=["connection_uri", "database_url", "DATABASE_URL", "url", "uri"]
        )
    os.environ["TRANSLATION_CACHE_PATH"] = ":memory:"
    os.environ["LOG_VERBOSE_SYNC"] = "true" if cfg.log_verbose_sync else "false"


def _build_log_event(*, product_id: str, topic: str, summary: dict[str, Any]) -> dict[str, Any]:
    item = ((summary.get("items") or [{}])[0]) if isinstance(summary, dict) else {}
    locales = item.get("locales") or {}
    seo_summary = summary.get("seo") or {}
    seo_item = ((seo_summary.get("items") or [{}])[0]) if isinstance(seo_summary, dict) else {}
    return {
        "ok": True,
        "event": "translation_sync",
        "product_id": int(product_id),
        "product_title": item.get("product_title") or "",
        "topic": topic,
        "status": item.get("status") or "unknown",
        "skip_reason": item.get("skip_reason") or "",
        "changed_sections": item.get("changed_sections") or [],
        "target_locales": sorted(list(locales.keys())),
        "translated_sections": {
            locale: data.get("translated_sections") or [] for locale, data in locales.items()
        },
        "section_sources": {
            locale: data.get("section_sources") or {} for locale, data in locales.items()
        },
        "seo_sync_enabled": bool(seo_summary.get("enabled")),
        "seo_status": seo_item.get("status") or "disabled",
        "seo_locales": {
            locale: {
                "status": data.get("status") or "",
                "planned_fields": data.get("planned_fields") or [],
                "registered_fields": data.get("registered_fields") or [],
            }
            for locale, data in (seo_item.get("locales") or {}).items()
        },
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


def _event_already_processed(cfg: WorkerConfig, event_id: str) -> bool:
    if not event_id:
        return False
    dedup = _dedup_table(cfg)
    item = dedup.get_item(Key={"event_id": event_id}).get("Item") or {}
    return item.get("status") == "processed"


def _mark_event_processed(cfg: WorkerConfig, event_id: str) -> None:
    if not event_id:
        return
    _dedup_table(cfg).put_item(
        Item={
            "event_id": event_id,
            "status": "processed",
            "ttl": _now_epoch() + 3 * 24 * 3600,
        },
    )


def _acquire_debounce(cfg: WorkerConfig, *, shop: str, product_gid: str) -> bool:
    snap = _snap_table(cfg)
    now = _now_epoch()
    try:
        snap.update_item(
            Key={"pk": _pk(shop, product_gid), "sk": f"source#{cfg.source_locale}"},
            UpdateExpression=(
                "SET debounce_until = :until, updated_at = :now, #ttl = :ttl "
                "REMOVE followup_until, followup_token, followup_updated_at"
            ),
            ConditionExpression="attribute_not_exists(debounce_until) OR debounce_until <= :now",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":until": now + cfg.debounce_seconds,
                ":now": now,
                ":ttl": now + 30 * 24 * 3600,
            },
        )
        return True
    except snap.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _schedule_debounced_followup(
    cfg: WorkerConfig,
    *,
    shop: str,
    debounce_gid: str,
    topic: str,
    body: dict[str, Any],
) -> str:
    """Coalesce a webhook burst into one delayed current-state refresh.

    Returning the original SQS message as a failure consumed the queue's
    receive budget and eventually moved healthy updates to the DLQ. A compact
    DynamoDB reservation makes one worker responsible for enqueueing the
    follow-up; all other messages in the same window can be acknowledged.
    """
    if not cfg.sqs_url:
        return "failed"

    snap = _snap_table(cfg)
    now = _now_epoch()
    delay_seconds = min(900, max(1, cfg.debounce_seconds + 2))
    token = str(uuid.uuid4())
    try:
        snap.update_item(
            Key={"pk": _pk(shop, debounce_gid), "sk": f"source#{cfg.source_locale}"},
            UpdateExpression=(
                "SET followup_until = :until, followup_token = :token, "
                "followup_updated_at = :now, #ttl = :ttl"
            ),
            ConditionExpression=("attribute_not_exists(followup_until) OR followup_until <= :now"),
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":until": now + delay_seconds,
                ":token": token,
                ":now": now,
                ":ttl": now + 30 * 24 * 3600,
            },
        )
    except snap.meta.client.exceptions.ConditionalCheckFailedException:
        return "already_scheduled"

    try:
        _sqs().send_message(
            QueueUrl=cfg.sqs_url,
            DelaySeconds=delay_seconds,
            MessageBody=json.dumps(body, separators=(",", ":")),
            MessageAttributes={
                "Topic": {"DataType": "String", "StringValue": topic},
                "Shop": {"DataType": "String", "StringValue": shop},
                "EventId": {
                    "DataType": "String",
                    "StringValue": f"coalesced:{token}",
                },
            },
        )
    except Exception as exc:
        # Best-effort rollback. If this fails, the reservation expires before
        # the original SQS message is delivered again, so the update is delayed
        # but not silently lost.
        try:
            snap.update_item(
                Key={"pk": _pk(shop, debounce_gid), "sk": f"source#{cfg.source_locale}"},
                UpdateExpression="REMOVE followup_until, followup_token, followup_updated_at",
                ConditionExpression="followup_token = :token",
                ExpressionAttributeValues={":token": token},
            )
        except Exception:
            pass
        print(
            json.dumps(
                {
                    "ok": False,
                    "warning": "debounce_followup_schedule_failed",
                    "error": str(exc),
                }
            )
        )
        return "failed"
    return "scheduled"


def _coalesce_debounced_record(
    cfg: WorkerConfig,
    *,
    record: dict[str, Any],
    shop: str,
    debounce_gid: str,
    topic: str,
    body: dict[str, Any],
    event_id: str,
) -> tuple[bool, str | None]:
    message_id = _message_id(record)
    result = _schedule_debounced_followup(
        cfg,
        shop=shop,
        debounce_gid=debounce_gid,
        topic=topic,
        body=body,
    )
    if result == "failed":
        print(
            json.dumps(
                {
                    "ok": False,
                    "retry": "debounce_followup_failed",
                    "message_id": message_id,
                }
            )
        )
        return False, message_id

    _mark_event_processed(cfg, event_id)
    print(
        json.dumps(
            {
                "ok": True,
                "skip": "debounce_coalesced",
                "followup": result,
                "message_id": message_id,
            }
        )
    )
    return True, None


async def _run_backend(
    *,
    cfg: WorkerConfig,
    product_id: str,
    is_create: bool,
    shop: str,
) -> dict[str, Any]:
    from src.bootstrap.incremental import sync_products_incremental

    summary = await sync_products_incremental(
        product_ids=[int(product_id)],
        target_locales=cfg.target_locales,
        mf_include=cfg.mf_include or None,
        source_locale=cfg.source_locale,
        apply_translations=not cfg.dry_run,
        dry_run=cfg.dry_run,
        is_create=is_create,
        reconcile_shopify_drift=False,
        content_changes_only=not is_create,
        recover_incomplete_state=False,
        sync_seo=cfg.seo_sync_enabled,
    )
    completed_items = [
        item
        for item in summary.get("items", [])
        if str(item.get("status") or "") in {"translated", "synced"}
    ]
    if (is_create or completed_items) and not cfg.dry_run:
        handle_summary = await sync_products_incremental(
            product_ids=[int(product_id)],
            target_locales=cfg.target_locales,
            mf_include=cfg.mf_include or None,
            source_locale=cfg.source_locale,
            apply_translations=True,
            dry_run=False,
            is_create=False,
            handle_only=True,
        )
        summary["registered"] = int(summary.get("registered", 0)) + int(
            handle_summary.get("registered", 0)
        )
        summary["failed_products"] = int(summary.get("failed_products", 0)) + int(
            handle_summary.get("failed_products", 0)
        )
        summary["failed_product_ids"] = list(summary.get("failed_product_ids", [])) + list(
            handle_summary.get("failed_product_ids", [])
        )
        summary["items"] = list(summary.get("items", [])) + list(handle_summary.get("items", []))
    return summary


async def _run_theme_realtime_sync(
    *,
    cfg: WorkerConfig,
) -> dict[str, Any]:
    from src.bootstrap.theme import THEME_RESOURCE_TYPES, bootstrap_theme

    return await bootstrap_theme(
        theme_id=cfg.approved_theme_id,
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        apply_translations=not cfg.dry_run,
        dry_run=cfg.dry_run,
        resource_types=list(THEME_RESOURCE_TYPES),
    )


async def _process_one(record: dict[str, Any], cfg: WorkerConfig) -> tuple[bool, str | None]:
    msg_id = _message_id(record)
    attrs = record.get("messageAttributes") or {}
    topic = (attrs.get("Topic") or {}).get("stringValue", "")
    shop = (attrs.get("Shop") or {}).get("stringValue", cfg.shop_domain)
    event_id = (attrs.get("EventId") or {}).get("stringValue", "")
    topic_lower = topic.lower()

    try:
        body = json.loads(record.get("body") or "{}")
    except Exception as e:
        print(json.dumps({"ok": False, "message_id": msg_id, "error": f"invalid_json:{e}"}))
        return False, msg_id

    if topic_lower in {"themes/update", "themes/publish"}:
        if not cfg.theme_tracking_enabled and not cfg.theme_realtime_sync_enabled:
            print(json.dumps({"ok": True, "skip": "theme_tracking_disabled", "message_id": msg_id}))
            return True, None
        if _event_already_processed(cfg, event_id):
            print(
                json.dumps(
                    {"ok": True, "skip": "dedup", "event_id": event_id, "message_id": msg_id}
                )
            )
            return True, None
        theme_gid = f"gid://shopify/OnlineStoreTheme/{cfg.approved_theme_id or 'main'}"
        if not _acquire_debounce(cfg, shop=shop, product_gid=theme_gid):
            return _coalesce_debounced_record(
                cfg,
                record=record,
                shop=shop,
                debounce_gid=theme_gid,
                topic="themes/update",
                body={"id": cfg.approved_theme_id or "main"},
                event_id=event_id,
            )
        try:
            _apply_runtime_secrets(cfg)
            from src.bootstrap.theme_tracking import track_main_theme_read_only

            summary = await track_main_theme_read_only(
                approved_theme_id=cfg.approved_theme_id,
                topic=topic_lower,
                event_id=event_id or None,
                source_locale=cfg.source_locale,
            )
            realtime_summary: dict[str, Any] | None = None
            main_matches = str(summary.get("actual_theme_id") or "") == str(
                summary.get("approved_theme_id") or ""
            )
            if cfg.theme_realtime_sync_enabled and main_matches:
                realtime_summary = await _run_theme_realtime_sync(cfg=cfg)
                failed_items = [
                    item
                    for item in (realtime_summary.get("items") or [])
                    if isinstance(item, dict) and item.get("status") == "failed"
                ]
                if failed_items:
                    raise RuntimeError("Theme realtime sync produced failed resources")
            _mark_event_processed(cfg, event_id)
            print(
                json.dumps(
                    {
                        "event": (
                            "theme_realtime_sync"
                            if realtime_summary is not None
                            else "theme_tracking"
                        ),
                        "message_id": msg_id,
                        **summary,
                        "realtime_sync": (
                            {
                                "resources": realtime_summary.get("resources", 0),
                                "changed_resources": realtime_summary.get(
                                    "changed_resources",
                                    0,
                                ),
                                "changed_sections": realtime_summary.get(
                                    "changed_sections",
                                    0,
                                ),
                                "registered": realtime_summary.get(
                                    "registered",
                                    0,
                                ),
                                "target_locales": realtime_summary.get(
                                    "target_locales",
                                    [],
                                ),
                            }
                            if realtime_summary is not None
                            else None
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return True, None
        except Exception as e:
            print(
                json.dumps(
                    {"ok": False, "event": "theme_tracking", "message_id": msg_id, "error": str(e)}
                )
            )
            return False, msg_id

    product_id = _resolve_product_id(body)
    if not product_id:
        print(json.dumps({"ok": True, "skip": "no_product_id", "message_id": msg_id}))
        return True, None

    gid = _gid(product_id)
    is_create = topic_lower == "products/create"

    if _event_already_processed(cfg, event_id):
        print(json.dumps({"ok": True, "skip": "dedup", "event_id": event_id, "message_id": msg_id}))
        return True, None

    if not _acquire_debounce(cfg, shop=shop, product_gid=gid):
        return _coalesce_debounced_record(
            cfg,
            record=record,
            shop=shop,
            debounce_gid=gid,
            topic="products/update",
            body={"id": product_id},
            event_id=event_id,
        )

    try:
        _apply_runtime_secrets(cfg)
        summary = await _run_backend(
            cfg=cfg,
            product_id=product_id,
            is_create=is_create,
            shop=shop,
        )
        _mark_event_processed(cfg, event_id)
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
        print(
            json.dumps(
                {"ok": False, "product_id": product_id, "message_id": msg_id, "error": str(e)}
            )
        )
        return False, msg_id


def handler(event, context):
    configure_logging()
    cfg = _config()
    records = event.get("Records", [])
    if cfg.disable_sync:
        failures = [
            {"itemIdentifier": message_id}
            for record in records
            if (message_id := _message_id(record))
        ]
        print(
            json.dumps(
                {
                    "ok": False,
                    "retry": "sync_paused",
                    "component": "worker",
                    "records": len(records),
                }
            )
        )
        return {"statusCode": 200, "batchItemFailures": failures}

    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(asyncio.gather(*[_process_one(r, cfg) for r in records]))
    failures = [
        {"itemIdentifier": failure_id} for ok, failure_id in results if not ok and failure_id
    ]
    return {"statusCode": 200, "batchItemFailures": failures}

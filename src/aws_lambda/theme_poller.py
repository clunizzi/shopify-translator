from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, replace

import boto3
from botocore.exceptions import ClientError

from src.logging_setup import configure_logging


@dataclass(frozen=True)
class PollerConfig:
    source_locale: str
    target_locales: list[str]
    theme_id: str
    run_theme: bool
    theme_resource_types: list[str]
    global_resource_types: list[str]
    global_resource_poll_enabled: bool
    dry_run: bool
    max_translations: int | None
    log_verbose_sync: bool
    poll_enabled: bool
    checkpoint_table: str
    openai_api_key_secret_arn: str
    shopify_admin_token_secret_arn: str
    neon_database_url_secret_arn: str


_SECRETS = None
_DYNAMODB = None
_CACHED_SECRETS: dict[str, str] = {}
_CONTENT_ACTIONS = {
    "content_product_search",
    "content_product_inspect",
    "content_product_save",
    "content_theme_resources",
    "content_theme_inspect",
    "content_theme_save",
}


def _secrets():
    global _SECRETS
    if _SECRETS is None:
        _SECRETS = boto3.client("secretsmanager")
    return _SECRETS


def _dynamodb():
    global _DYNAMODB
    if _DYNAMODB is None:
        _DYNAMODB = boto3.resource("dynamodb")
    return _DYNAMODB


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


def _config() -> PollerConfig:
    return PollerConfig(
        source_locale=os.environ.get("SOURCE_LOCALE", "en"),
        target_locales=[
            s.strip() for s in os.environ.get("TARGET_LOCALES", "").split(",") if s.strip()
        ],
        theme_id=os.environ.get("THEME_ID", "").strip(),
        run_theme=bool(os.environ.get("THEME_ID", "").strip()),
        theme_resource_types=[
            s.strip() for s in os.environ.get("THEME_RESOURCE_TYPES", "").split(",") if s.strip()
        ],
        global_resource_types=[
            s.strip() for s in os.environ.get("GLOBAL_RESOURCE_TYPES", "").split(",") if s.strip()
        ],
        global_resource_poll_enabled=os.environ.get("GLOBAL_RESOURCE_POLL_ENABLED", "false").lower()
        in {"1", "true", "yes", "y"},
        dry_run=os.environ.get("DRY_RUN", "false").lower() in {"1", "true", "yes", "y"},
        max_translations=None,
        log_verbose_sync=os.environ.get("LOG_VERBOSE_SYNC", "false").lower()
        in {"1", "true", "yes", "y"},
        poll_enabled=os.environ.get("THEME_POLL_ENABLED", "true").lower()
        in {"1", "true", "yes", "y"},
        checkpoint_table=os.environ.get("DDB_TABLE", "").strip(),
        openai_api_key_secret_arn=os.environ.get("OPENAI_API_KEY_SECRET_ARN", ""),
        shopify_admin_token_secret_arn=os.environ.get("SHOPIFY_ADMIN_TOKEN_SECRET_ARN", ""),
        neon_database_url_secret_arn=os.environ.get("NEON_DATABASE_URL_SECRET_ARN", ""),
    )


def _list_from_event(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    return []


def _bool_from_event(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _apply_event_overrides(cfg: PollerConfig, event: object) -> PollerConfig:
    if not isinstance(event, dict):
        return cfg
    theme_resource_types = (
        _list_from_event(event.get("theme_resource_types")) or cfg.theme_resource_types
    )
    global_resource_types = (
        _list_from_event(event.get("global_resource_types")) or cfg.global_resource_types
    )
    theme_id = str(event.get("theme_id") or cfg.theme_id).strip()
    raw_max_translations = event.get("max_translations")
    max_translations = cfg.max_translations
    if raw_max_translations is not None:
        max_translations = max(1, int(raw_max_translations))
    return replace(
        cfg,
        theme_id=theme_id,
        run_theme=_bool_from_event(event.get("run_theme"), default=cfg.run_theme),
        theme_resource_types=theme_resource_types,
        global_resource_types=global_resource_types,
        global_resource_poll_enabled=_bool_from_event(
            event.get("run_global_resources"),
            default=cfg.global_resource_poll_enabled,
        ),
        dry_run=_bool_from_event(event.get("dry_run"), default=cfg.dry_run),
        max_translations=max_translations,
    )


def _apply_runtime_secrets(cfg: PollerConfig) -> None:
    if cfg.openai_api_key_secret_arn:
        os.environ["OPENAI_API_KEY"] = _get_secret(cfg.openai_api_key_secret_arn)
    if cfg.shopify_admin_token_secret_arn:
        os.environ["SHOPIFY_ADMIN_TOKEN"] = _get_secret(cfg.shopify_admin_token_secret_arn)
    if cfg.neon_database_url_secret_arn:
        raw = _get_secret(cfg.neon_database_url_secret_arn)
        os.environ["NEON_DATABASE_URL"] = _coerce_secret_value(
            raw, prefer_keys=["connection_uri", "database_url", "DATABASE_URL", "url", "uri"]
        )
    os.environ["LOG_VERBOSE_SYNC"] = "true" if cfg.log_verbose_sync else "false"


def _build_theme_summary_event(summary: dict[str, object], *, theme_id: str) -> dict[str, object]:
    items = summary.get("items") if isinstance(summary, dict) else []
    statuses = [item.get("status") or "unknown" for item in (items or []) if isinstance(item, dict)]
    if statuses and all(status == "unchanged" for status in statuses):
        overall_status = "unchanged"
    elif any(status == "failed" for status in statuses):
        overall_status = "failed"
    elif statuses and all(status in {"synced", "unchanged"} for status in statuses):
        overall_status = "synced"
    else:
        overall_status = "translated"
    return {
        "ok": True,
        "event": "theme_poll",
        "theme_id": theme_id,
        "status": overall_status,
        "resources": summary.get("resources", 0),
        "changed_resources": summary.get("changed_resources", 0),
        "changed_sections": summary.get("changed_sections", 0),
        "registered": summary.get("registered", 0),
        "target_locales": summary.get("target_locales") or [],
        "resource_types": summary.get("resource_types") or [],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _build_global_resource_summary_event(summary: dict[str, object]) -> dict[str, object]:
    items = summary.get("items") if isinstance(summary, dict) else []
    statuses = [item.get("status") or "unknown" for item in (items or []) if isinstance(item, dict)]
    if statuses and all(status == "unchanged" for status in statuses):
        overall_status = "unchanged"
    elif any(status == "failed" for status in statuses):
        overall_status = "failed"
    elif statuses and all(status in {"synced", "unchanged"} for status in statuses):
        overall_status = "synced"
    else:
        overall_status = "translated"
    return {
        "ok": True,
        "event": "global_resource_poll",
        "resource_group": "global",
        "status": overall_status,
        "resources": summary.get("resources", 0),
        "changed_resources": summary.get("changed_resources", 0),
        "changed_sections": summary.get("changed_sections", 0),
        "registered": summary.get("registered", 0),
        "target_locales": summary.get("target_locales") or [],
        "resource_types": summary.get("resource_types") or [],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _checkpoint_key(group: str, cfg: PollerConfig) -> dict[str, str]:
    suffix = cfg.theme_id if group == "theme" else "global"
    return {"pk": "CONTROL#translation-poller", "sk": f"{group}#{suffix}"}


def _read_fingerprint(cfg: PollerConfig, group: str) -> str:
    if not cfg.checkpoint_table:
        return ""
    item = (
        _dynamodb()
        .Table(cfg.checkpoint_table)
        .get_item(Key=_checkpoint_key(group, cfg), ConsistentRead=True)
        .get("Item")
        or {}
    )
    return str(item.get("fingerprint") or "")


def _write_fingerprint(
    cfg: PollerConfig,
    group: str,
    probe: dict[str, object],
) -> None:
    if not cfg.checkpoint_table:
        return
    _dynamodb().Table(cfg.checkpoint_table).put_item(
        Item={
            **_checkpoint_key(group, cfg),
            "fingerprint": str(probe.get("fingerprint") or ""),
            "resources": int(probe.get("resources") or 0),
            "fields": int(probe.get("fields") or 0),
            "updated_at": int(time.time()),
        }
    )


def _acquire_poll_lock(cfg: PollerConfig, *, ttl_seconds: int = 480) -> str | None:
    if not cfg.checkpoint_table:
        return "no-checkpoint-lock"
    now = int(time.time())
    token = str(uuid.uuid4())
    try:
        _dynamodb().Table(cfg.checkpoint_table).put_item(
            Item={
                "pk": "CONTROL#translation-poller",
                "sk": "LOCK",
                "lock_token": token,
                "expires_at": now + ttl_seconds,
                "updated_at": now,
            },
            ConditionExpression="attribute_not_exists(expires_at) OR expires_at < :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return token


def _release_poll_lock(cfg: PollerConfig, token: str) -> None:
    if not cfg.checkpoint_table or token == "no-checkpoint-lock":
        return
    try:
        _dynamodb().Table(cfg.checkpoint_table).delete_item(
            Key={"pk": "CONTROL#translation-poller", "sk": "LOCK"},
            ConditionExpression="lock_token = :token",
            ExpressionAttributeValues={":token": token},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def _summary_succeeded(summary: dict[str, object]) -> bool:
    return not any(
        isinstance(item, dict) and item.get("status") == "failed"
        for item in (summary.get("items") or [])
    )


async def _run_theme(cfg: PollerConfig) -> dict[str, object]:
    from src.bootstrap.theme import THEME_RESOURCE_TYPES, bootstrap_theme

    return await bootstrap_theme(
        theme_id=cfg.theme_id,
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        apply_translations=not cfg.dry_run,
        dry_run=cfg.dry_run,
        resource_types=cfg.theme_resource_types or list(THEME_RESOURCE_TYPES),
        max_translations=cfg.max_translations,
    )


async def _run_theme_audit(cfg: PollerConfig) -> dict[str, object]:
    from src.bootstrap.theme import (
        THEME_RESOURCE_TYPES,
        audit_theme_translations,
    )

    summary = await audit_theme_translations(
        theme_id=cfg.theme_id,
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        resource_types=cfg.theme_resource_types or list(THEME_RESOURCE_TYPES),
        include_items=True,
    )
    from src.state.neon import NeonTranslationStore, make_source_hash

    store = NeonTranslationStore()
    try:
        for locale, locale_summary in (summary.get("locales") or {}).items():
            if not isinstance(locale_summary, dict):
                continue
            memory = store.get_translation_memory_map(
                source_locale=cfg.source_locale,
                target_locale=str(locale),
            )
            items = locale_summary.pop("items", [])
            reusable = sum(
                1
                for item in items
                if isinstance(item, dict)
                and (
                    make_source_hash(str(item.get("source_value") or "")),
                    f"{item.get('resource_type')}.{item.get('key')}",
                )
                in memory
            )
            pending = int(locale_summary.get("missing") or 0) + int(
                locale_summary.get("outdated") or 0
            )
            locale_summary["reusable_from_memory"] = reusable
            locale_summary["ai_required"] = max(0, pending - reusable)
    finally:
        store.close()
    return summary


async def _run_global_resources(cfg: PollerConfig) -> dict[str, object]:
    from src.bootstrap.resources import DEFAULT_RESOURCE_TYPES, bootstrap_resources

    return await bootstrap_resources(
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        apply_translations=not cfg.dry_run,
        dry_run=cfg.dry_run,
        resource_types=cfg.global_resource_types or list(DEFAULT_RESOURCE_TYPES),
        max_translations=cfg.max_translations,
    )


async def _probe_theme(cfg: PollerConfig) -> dict[str, object]:
    from src.bootstrap.theme import THEME_RESOURCE_TYPES, get_theme_resource_fingerprint

    return await get_theme_resource_fingerprint(
        theme_id=cfg.theme_id,
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        resource_types=cfg.theme_resource_types or list(THEME_RESOURCE_TYPES),
    )


async def _probe_global_resources(cfg: PollerConfig) -> dict[str, object]:
    from src.bootstrap.resources import DEFAULT_RESOURCE_TYPES, get_global_resource_fingerprint

    return await get_global_resource_fingerprint(
        target_locales=cfg.target_locales,
        resource_types=cfg.global_resource_types or list(DEFAULT_RESOURCE_TYPES),
    )


async def _run_scheduled_poll_unlocked(cfg: PollerConfig) -> dict[str, object]:
    """Use Shopify-only fingerprints so unchanged polls never wake Neon or OpenAI."""
    theme_probe = await _probe_theme(cfg) if cfg.run_theme else None
    global_probe = (
        await _probe_global_resources(cfg) if cfg.global_resource_poll_enabled else None
    )
    theme_changed = bool(
        theme_probe
        and (
            not cfg.checkpoint_table
            or _read_fingerprint(cfg, "theme") != theme_probe.get("fingerprint")
        )
    )
    global_changed = bool(
        global_probe
        and (
            not cfg.checkpoint_table
            or _read_fingerprint(cfg, "global") != global_probe.get("fingerprint")
        )
    )

    result: dict[str, object] = {
        "ok": True,
        "event": "scheduled_translation_poll",
        "theme": {
            "status": "changed" if theme_changed else "unchanged",
            "probe": theme_probe,
        }
        if theme_probe
        else None,
        "global": {
            "status": "changed" if global_changed else "unchanged",
            "probe": global_probe,
        }
        if global_probe
        else None,
    }

    # Global handles must settle before theme/page links are reconciled.
    if global_changed:
        global_summary = await _run_global_resources(cfg)
        global_event = _build_global_resource_summary_event(global_summary)
        result["global"] = global_event
        print(json.dumps(global_event, ensure_ascii=False))
        if _summary_succeeded(global_summary):
            fresh_global_probe = await _probe_global_resources(cfg)
            _write_fingerprint(cfg, "global", fresh_global_probe)
    elif global_probe:
        print(
            json.dumps(
                {
                    "ok": True,
                    "event": "global_resource_poll",
                    "status": "unchanged_fingerprint",
                    "resources": global_probe.get("resources", 0),
                    "fields": global_probe.get("fields", 0),
                }
            )
        )

    # A global handle change can alter localized links inside theme fields.
    if theme_changed or (cfg.run_theme and global_changed):
        theme_summary = await _run_theme(cfg)
        theme_event = _build_theme_summary_event(theme_summary, theme_id=cfg.theme_id)
        result["theme"] = theme_event
        print(json.dumps(theme_event, ensure_ascii=False))
        if _summary_succeeded(theme_summary):
            fresh_theme_probe = await _probe_theme(cfg)
            _write_fingerprint(cfg, "theme", fresh_theme_probe)
    elif theme_probe:
        print(
            json.dumps(
                {
                    "ok": True,
                    "event": "theme_poll",
                    "theme_id": cfg.theme_id,
                    "status": "unchanged_fingerprint",
                    "resources": theme_probe.get("resources", 0),
                    "fields": theme_probe.get("fields", 0),
                }
            )
        )
    return result


async def _run_scheduled_poll(cfg: PollerConfig) -> dict[str, object]:
    lock_token = _acquire_poll_lock(cfg)
    if lock_token is None:
        return {
            "ok": True,
            "event": "scheduled_translation_poll",
            "status": "skipped_concurrent_run",
        }
    try:
        return await _run_scheduled_poll_unlocked(cfg)
    finally:
        _release_poll_lock(cfg, lock_token)


def handler(event, context):
    configure_logging()
    cfg = _apply_event_overrides(_config(), event)
    manual = isinstance(event, dict) and _bool_from_event(event.get("manual"), default=False)
    action = str((event or {}).get("action") or "").strip() if isinstance(event, dict) else ""
    job_id = str((event or {}).get("job_id") or "").strip() if isinstance(event, dict) else ""
    actor = str((event or {}).get("actor") or "").strip() if isinstance(event, dict) else ""
    content_action = manual and action in _CONTENT_ACTIONS

    if not cfg.poll_enabled and not manual:
        print(json.dumps({"ok": True, "skip": "theme_poll_disabled"}))
        return {"statusCode": 200}
    if manual and action not in {"theme_audit", "theme_canary", "theme_sync", *_CONTENT_ACTIONS}:
        raise RuntimeError("Unsupported manual theme action")
    if manual and not content_action and not job_id:
        raise RuntimeError("Manual theme action requires job_id")
    if cfg.run_theme and not cfg.theme_id:
        print(json.dumps({"ok": True, "skip": "missing_theme_id"}))
        return {"statusCode": 200}
    if not cfg.run_theme and not cfg.global_resource_poll_enabled:
        print(json.dumps({"ok": True, "skip": "missing_theme_id"}))
        return {"statusCode": 200}

    job_store = None
    claimed_job = False
    try:
        _apply_runtime_secrets(cfg)
        if content_action:
            from src.admin_content import AdminContentError, handle_admin_content_action

            try:
                data = asyncio.run(
                    handle_admin_content_action(
                        action,
                        event if isinstance(event, dict) else {},
                        approved_theme_id=cfg.theme_id,
                    )
                )
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "event": action,
                            "actor": actor or None,
                        }
                    )
                )
                return {"ok": True, "data": data}
            except AdminContentError as error:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "event": action,
                            "error_code": error.code,
                            "actor": actor or None,
                        }
                    )
                )
                return {
                    "ok": False,
                    "error": {
                        "code": error.code,
                        "message": str(error),
                    },
                }

        if manual:
            from src.config.settings import SETTINGS
            from src.state.neon import NeonTranslationStore

            job_store = NeonTranslationStore()
            job_store.ensure_schema()
            claimed_job = job_store.claim_admin_job(
                job_id=job_id,
                shop_domain=SETTINGS.shopify_domain,
            )
            if not claimed_job:
                result = {
                    "ok": True,
                    "skip": "admin_job_already_claimed",
                    "job_id": job_id,
                }
                print(json.dumps(result))
                return result

        result: dict[str, object] = {"ok": True, "event": "theme_poll"}
        if manual and action == "theme_audit":
            summary = asyncio.run(_run_theme_audit(cfg))
            result = {
                **summary,
                "event": action,
                "job_id": job_id,
                "actor": actor,
            }
            print(json.dumps(result, ensure_ascii=False))
        elif manual and cfg.run_theme:
            if manual and action == "theme_canary":
                cfg = replace(cfg, dry_run=False, max_translations=1)
            elif manual and action == "theme_sync":
                # Preserve an optional event cap so operators can reconcile a
                # large live theme in bounded, observable batches. The GUI
                # omits it and therefore retains the full-sync behaviour.
                cfg = replace(cfg, dry_run=False)
            summary = asyncio.run(_run_theme(cfg))
            result = {
                **_build_theme_summary_event(summary, theme_id=cfg.theme_id),
                "event": action or "theme_poll",
                "job_id": job_id or None,
                "actor": actor or None,
                "max_translations": cfg.max_translations,
            }
            print(json.dumps(result, ensure_ascii=False))
        elif not manual:
            result = asyncio.run(_run_scheduled_poll(cfg))
            print(json.dumps(result, ensure_ascii=False))
        if claimed_job and job_store is not None:
            from src.config.settings import SETTINGS

            job_store.complete_admin_job(
                job_id=job_id,
                shop_domain=SETTINGS.shopify_domain,
                result=result,
            )
        return result
    except Exception as e:
        if claimed_job and job_store is not None:
            try:
                from src.config.settings import SETTINGS

                job_store.fail_admin_job(
                    job_id=job_id,
                    shop_domain=SETTINGS.shopify_domain,
                    error=str(e),
                )
            except Exception as job_error:
                print(json.dumps({"ok": False, "job_tracking_error": str(job_error)}))
        print(json.dumps({"ok": False, "error": str(e)}))
        raise
    finally:
        if job_store is not None:
            job_store.close()

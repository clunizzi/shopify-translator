from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, replace

import boto3

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
    openai_api_key_secret_arn: str
    shopify_admin_token_secret_arn: str
    neon_database_url_secret_arn: str


_SECRETS = None
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

    return await audit_theme_translations(
        theme_id=cfg.theme_id,
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        resource_types=cfg.theme_resource_types or list(THEME_RESOURCE_TYPES),
        include_items=False,
    )


async def _run_global_resources(cfg: PollerConfig) -> dict[str, object]:
    from src.bootstrap.resources import DEFAULT_RESOURCE_TYPES, bootstrap_resources

    return await bootstrap_resources(
        target_locales=cfg.target_locales,
        source_locale=cfg.source_locale,
        apply_translations=not cfg.dry_run,
        dry_run=cfg.dry_run,
        resource_types=cfg.global_resource_types or list(DEFAULT_RESOURCE_TYPES),
    )


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

        result: dict[str, object] = {
            "ok": True,
            "event": "theme_poll",
        }
        if manual and action == "theme_audit":
            summary = asyncio.run(_run_theme_audit(cfg))
            result = {
                **summary,
                "event": action,
                "job_id": job_id,
                "actor": actor,
            }
            print(json.dumps(result, ensure_ascii=False))
        elif cfg.run_theme:
            if manual and action == "theme_canary":
                cfg = replace(cfg, dry_run=False, max_translations=1)
            elif manual and action == "theme_sync":
                cfg = replace(cfg, dry_run=False, max_translations=None)
            summary = asyncio.run(_run_theme(cfg))
            result = {
                **_build_theme_summary_event(summary, theme_id=cfg.theme_id),
                "event": action or "theme_poll",
                "job_id": job_id or None,
                "actor": actor or None,
                "max_translations": cfg.max_translations,
            }
            print(json.dumps(result, ensure_ascii=False))
        if cfg.global_resource_poll_enabled and not manual:
            summary = asyncio.run(_run_global_resources(cfg))
            result = _build_global_resource_summary_event(summary)
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

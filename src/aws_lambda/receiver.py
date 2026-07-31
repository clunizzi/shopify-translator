from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

import boto3


@dataclass(frozen=True)
class ReceiverConfig:
    sqs_url: str
    webhook_secret_arn: str
    webhook_secret_plain: str
    disable_sync: bool


_SQS = None
_SECRETS = None
_CACHED_SECRET: str | None = None


def _config() -> ReceiverConfig:
    return ReceiverConfig(
        sqs_url=os.environ["SQS_URL"],
        webhook_secret_arn=os.environ.get("SHOPIFY_WEBHOOK_SECRET_ARN", ""),
        webhook_secret_plain=os.environ.get("SHOPIFY_WEBHOOK_SECRET", ""),
        disable_sync=os.environ.get("DISABLE_SYNC", "false").lower() in {"1", "true", "yes", "y"},
    )


def _sqs():
    global _SQS
    if _SQS is None:
        _SQS = boto3.client("sqs")
    return _SQS


def _secrets():
    global _SECRETS
    if _SECRETS is None:
        _SECRETS = boto3.client("secretsmanager")
    return _SECRETS


def _get_webhook_secret(cfg: ReceiverConfig) -> str:
    global _CACHED_SECRET
    if _CACHED_SECRET is not None:
        return _CACHED_SECRET
    if cfg.webhook_secret_plain:
        _CACHED_SECRET = cfg.webhook_secret_plain
        return _CACHED_SECRET
    if not cfg.webhook_secret_arn:
        raise RuntimeError("Missing SHOPIFY_WEBHOOK_SECRET_ARN or SHOPIFY_WEBHOOK_SECRET")
    resp = _secrets().get_secret_value(SecretId=cfg.webhook_secret_arn)
    sec = resp.get("SecretString") or ""
    _CACHED_SECRET = sec
    return sec


def _valid_hmac(cfg: ReceiverConfig, raw: bytes, header_hmac: str) -> bool:
    secret = _get_webhook_secret(cfg).encode()
    digest = hmac.new(secret, raw, hashlib.sha256).digest()
    calc = base64.b64encode(digest).decode()
    return hmac.compare_digest(calc, header_hmac or "")


def _decode_body(event: dict) -> bytes:
    if event.get("isBase64Encoded"):
        return base64.b64decode(event.get("body") or b"")
    return (event.get("body") or "").encode()


def handler(event, context):
    cfg = _config()

    try:
        headers = {(k or "").lower(): v for k, v in (event.get("headers") or {}).items()}
        topic = headers.get("x-shopify-topic", "")
        shop = headers.get("x-shopify-shop-domain", "")
        event_id = headers.get("x-shopify-event-id", "")
        hmac_header = headers.get("x-shopify-hmac-sha256", "")
        raw_body = _decode_body(event)
    except Exception as e:
        print(json.dumps({"ok": False, "reason": "bad_request", "error": str(e)}))
        return {"statusCode": 400, "body": "Bad Request"}

    try:
        if not _valid_hmac(cfg, raw_body, hmac_header):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "invalid_hmac",
                        "topic": topic,
                        "shop": shop,
                        "event_id": event_id,
                        "body_len": len(raw_body or b""),
                    }
                )
            )
            return {"statusCode": 401, "body": "Invalid HMAC"}

        topic_lower = topic.lower()
        if topic_lower == "products/create":
            delay = 8
        elif topic_lower in {"themes/update", "themes/publish"}:
            delay = 10
        else:
            delay = 0
        resp = _sqs().send_message(
            QueueUrl=cfg.sqs_url,
            MessageBody=raw_body.decode("utf-8"),
            DelaySeconds=delay,
            MessageAttributes={
                "Topic": {"DataType": "String", "StringValue": topic or ""},
                "Shop": {"DataType": "String", "StringValue": shop or ""},
                "EventId": {"DataType": "String", "StringValue": event_id or ""},
            },
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "enqueued": True,
                    "message_id": (resp or {}).get("MessageId"),
                    "topic": topic,
                    "shop": shop,
                    "event_id": event_id,
                    "delay": delay,
                    "sync_paused": cfg.disable_sync,
                }
            )
        )
        return {"statusCode": 200, "body": "OK"}
    except Exception as e:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "enqueue_failed",
                    "topic": topic,
                    "shop": shop,
                    "event_id": event_id,
                    "error": str(e),
                }
            )
        )
        return {"statusCode": 500, "body": "Internal Server Error"}

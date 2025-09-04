from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import boto3

SQS_URL = os.environ["SQS_URL"]
# Prefer Secrets Manager; fallback to plain env if provided (for local testing)
_WEBHOOK_SECRET_ARN = os.environ.get("SHOPIFY_WEBHOOK_SECRET_ARN")
_WEBHOOK_SECRET_PLAIN = os.environ.get("SHOPIFY_WEBHOOK_SECRET")
sqs = boto3.client("sqs")
secrets = boto3.client("secretsmanager")

_CACHED_SECRET: str | None = None


def _get_webhook_secret() -> str:
    global _CACHED_SECRET
    if _CACHED_SECRET is not None:
        return _CACHED_SECRET
    if _WEBHOOK_SECRET_PLAIN:
        _CACHED_SECRET = _WEBHOOK_SECRET_PLAIN
        return _CACHED_SECRET
    if not _WEBHOOK_SECRET_ARN:
        raise RuntimeError("Missing SHOPIFY_WEBHOOK_SECRET_ARN or SHOPIFY_WEBHOOK_SECRET")
    resp = secrets.get_secret_value(SecretId=_WEBHOOK_SECRET_ARN)
    sec = resp.get("SecretString") or ""
    _CACHED_SECRET = sec
    return sec


def _valid_hmac(raw: bytes, header_hmac: str) -> bool:
    secret = _get_webhook_secret().encode()
    digest = hmac.new(secret, raw, hashlib.sha256).digest()
    calc = base64.b64encode(digest).decode()
    return hmac.compare_digest(calc, header_hmac or "")


def handler(event, context):
    try:
        headers = {(k or "").lower(): v for k, v in (event.get("headers") or {}).items()}
        topic = headers.get("x-shopify-topic", "")
        shop = headers.get("x-shopify-shop-domain", "")
        event_id = headers.get("x-shopify-event-id", "")
        hmac_header = headers.get("x-shopify-hmac-sha256", "")

        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(event.get("body") or b"")
        else:
            raw_body = (event.get("body") or "").encode()

        if not _valid_hmac(raw_body, hmac_header):
            # Log invalid HMAC for troubleshooting (no payload leak)
            print(json.dumps({
                "ok": False,
                "reason": "invalid_hmac",
                "topic": topic,
                "shop": shop,
                "event_id": event_id,
                "body_len": len(raw_body or b"")
            }))
            return {"statusCode": 401, "body": "Invalid HMAC"}

        delay = 8 if (topic or "").lower() == "products/create" else 0

        resp = sqs.send_message(
            QueueUrl=SQS_URL,
            MessageBody=raw_body.decode("utf-8"),
            DelaySeconds=delay,
            MessageAttributes={
                "Topic": {"DataType": "String", "StringValue": topic or ""},
                "Shop": {"DataType": "String", "StringValue": shop or ""},
                "EventId": {"DataType": "String", "StringValue": event_id or ""},
            },
        )
        print(json.dumps({
            "ok": True,
            "enqueued": True,
            "message_id": (resp or {}).get("MessageId"),
            "topic": topic,
            "shop": shop,
            "event_id": event_id,
            "delay": delay
        }))
        return {"statusCode": 200, "body": "OK"}
    except Exception as e:  # pragma: no cover - runtime guard
        print(json.dumps({"ok": False, "reason": "exception", "error": str(e)}))
        return {"statusCode": 200, "body": "OK"}

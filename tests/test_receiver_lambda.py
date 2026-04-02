import base64
import hashlib
import hmac

from src.aws_lambda import receiver


def _shopify_hmac(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_receiver_invalid_hmac(monkeypatch):
    monkeypatch.setenv("SQS_URL", "https://example.com/q")
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    monkeypatch.setattr(receiver, "_CACHED_SECRET", None)

    event = {
        "headers": {
            "X-Shopify-Topic": "products/update",
            "X-Shopify-Shop-Domain": "example.myshopify.com",
            "X-Shopify-Event-Id": "evt-1",
            "X-Shopify-Hmac-Sha256": "bad",
        },
        "body": '{"id":101}',
        "isBase64Encoded": False,
    }

    res = receiver.handler(event, None)

    assert res["statusCode"] == 401


def test_receiver_returns_500_on_enqueue_failure(monkeypatch):
    monkeypatch.setenv("SQS_URL", "https://example.com/q")
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", "secret")
    monkeypatch.delenv("DISABLE_SYNC", raising=False)
    monkeypatch.setattr(receiver, "_CACHED_SECRET", None)

    class _FakeSQS:
        def send_message(self, **kwargs):
            raise RuntimeError("sqs down")

    monkeypatch.setattr(receiver, "_SQS", _FakeSQS())

    body = b'{"id":101}'
    event = {
        "headers": {
            "X-Shopify-Topic": "products/update",
            "X-Shopify-Shop-Domain": "example.myshopify.com",
            "X-Shopify-Event-Id": "evt-1",
            "X-Shopify-Hmac-Sha256": _shopify_hmac("secret", body),
        },
        "body": body.decode(),
        "isBase64Encoded": False,
    }

    res = receiver.handler(event, None)

    assert res["statusCode"] == 500

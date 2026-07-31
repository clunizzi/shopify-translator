from dataclasses import replace

from src import admin_content
from src.aws_lambda import theme_poller
from src.config import settings
from src.state import neon


class FakeJobStore:
    instances = []
    claim_result = True

    def __init__(self):
        self.completed = []
        self.failed = []
        self.closed = False
        self.__class__.instances.append(self)

    def ensure_schema(self):
        return None

    def claim_admin_job(self, **_kwargs):
        return self.__class__.claim_result

    def complete_admin_job(self, **kwargs):
        self.completed.append(kwargs)

    def fail_admin_job(self, **kwargs):
        self.failed.append(kwargs)

    def close(self):
        self.closed = True


def _config(*, poll_enabled=False):
    return theme_poller.PollerConfig(
        source_locale="it",
        target_locales=["de", "fr"],
        theme_id="123456789012",
        run_theme=True,
        theme_resource_types=[],
        global_resource_types=[],
        global_resource_poll_enabled=False,
        dry_run=False,
        max_translations=None,
        log_verbose_sync=False,
        poll_enabled=poll_enabled,
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
    )


def _patch_runtime(monkeypatch):
    FakeJobStore.instances = []
    FakeJobStore.claim_result = True
    monkeypatch.setattr(theme_poller, "_config", lambda: _config())
    monkeypatch.setattr(theme_poller, "_apply_runtime_secrets", lambda _cfg: None)
    monkeypatch.setattr(neon, "NeonTranslationStore", FakeJobStore)
    monkeypatch.setattr(
        settings,
        "SETTINGS",
        replace(settings.SETTINGS, shopify_domain="example-store.myshopify.com"),
    )


def test_manual_audit_runs_when_scheduled_poller_is_disabled(monkeypatch):
    _patch_runtime(monkeypatch)

    async def fake_audit(_cfg):
        return {
            "ok": True,
            "read_only": True,
            "locales": {
                "de": {"current": 496, "missing": 0, "outdated": 0},
                "fr": {"current": 496, "missing": 0, "outdated": 0},
            },
        }

    monkeypatch.setattr(theme_poller, "_run_theme_audit", fake_audit)

    result = theme_poller.handler(
        {
            "manual": True,
            "action": "theme_audit",
            "job_id": "11111111-1111-4111-8111-111111111111",
            "actor": "operator@example.com",
            "theme_id": "123456789012",
            "target_locales": ["de", "fr"],
        },
        None,
    )

    assert result["event"] == "theme_audit"
    assert result["read_only"] is True
    store = FakeJobStore.instances[0]
    assert store.completed[0]["result"]["locales"]["de"]["current"] == 496
    assert store.failed == []
    assert store.closed is True


def test_manual_canary_is_hard_capped_to_one(monkeypatch):
    _patch_runtime(monkeypatch)
    seen = {}

    async def fake_sync(cfg):
        seen["cfg"] = cfg
        return {
            "resources": 39,
            "changed_resources": 1,
            "changed_sections": 1,
            "registered": 1,
            "target_locales": cfg.target_locales,
            "resource_types": [],
            "items": [{"status": "synced"}],
        }

    monkeypatch.setattr(theme_poller, "_run_theme", fake_sync)

    result = theme_poller.handler(
        {
            "manual": True,
            "action": "theme_canary",
            "job_id": "22222222-2222-4222-8222-222222222222",
            "actor": "operator@example.com",
            "theme_id": "123456789012",
        },
        None,
    )

    assert seen["cfg"].max_translations == 1
    assert seen["cfg"].dry_run is False
    assert result["registered"] == 1


def test_duplicate_manual_job_is_not_run_again(monkeypatch):
    _patch_runtime(monkeypatch)
    FakeJobStore.claim_result = False

    async def should_not_run(_cfg):
        raise AssertionError("duplicate job must not run")

    monkeypatch.setattr(theme_poller, "_run_theme_audit", should_not_run)

    result = theme_poller.handler(
        {
            "manual": True,
            "action": "theme_audit",
            "job_id": "33333333-3333-4333-8333-333333333333",
        },
        None,
    )

    assert result["skip"] == "admin_job_already_claimed"


def test_content_read_runs_synchronously_without_admin_job(monkeypatch):
    _patch_runtime(monkeypatch)
    seen = {}

    async def fake_content(action, payload, *, approved_theme_id):
        seen.update(
            {
                "action": action,
                "payload": payload,
                "theme_id": approved_theme_id,
            }
        )
        return {"products": [{"id": "gid://shopify/Product/123"}]}

    monkeypatch.setattr(admin_content, "handle_admin_content_action", fake_content)

    result = theme_poller.handler(
        {
            "manual": True,
            "action": "content_product_search",
            "actor": "operator@example.com",
            "query": "motosega",
            "theme_id": "123456789012",
        },
        None,
    )

    assert result["ok"] is True
    assert result["data"]["products"][0]["id"].endswith("/123")
    assert seen["action"] == "content_product_search"
    assert seen["theme_id"] == "123456789012"
    assert FakeJobStore.instances == []

import asyncio
from dataclasses import replace

from src import admin_content
from src.aws_lambda import theme_poller
from src.bootstrap import theme
from src.config import settings
from src.state import neon


class FakeJobStore:
    instances = []
    claim_result = True
    memory_by_locale = {}

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

    def get_translation_memory_map(self, **_kwargs):
        return self.__class__.memory_by_locale.get(_kwargs["target_locale"], {})


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
        checkpoint_table="",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
    )


def _patch_runtime(monkeypatch):
    FakeJobStore.instances = []
    FakeJobStore.claim_result = True
    FakeJobStore.memory_by_locale = {}
    monkeypatch.setattr(theme_poller, "_config", lambda: _config())
    monkeypatch.setattr(theme_poller, "_apply_runtime_secrets", lambda _cfg: None)
    monkeypatch.setattr(neon, "NeonTranslationStore", FakeJobStore)
    monkeypatch.setattr(
        settings,
        "SETTINGS",
        replace(settings.SETTINGS, shopify_domain="example-store.myshopify.com"),
    )


def test_theme_audit_reports_memory_reuse_without_returning_raw_items(monkeypatch):
    _patch_runtime(monkeypatch)
    source_value = "Aggiungi al carrello"
    field_key = "ONLINE_STORE_THEME_LOCALE_CONTENT.customloc.add_to_cart"
    FakeJobStore.memory_by_locale = {
        "de": {(neon.make_source_hash(source_value), field_key): "In den Warenkorb"}
    }

    async def fake_shopify_audit(**_kwargs):
        return {
            "ok": True,
            "read_only": True,
            "locales": {
                "de": {
                    "current": 1,
                    "missing": 2,
                    "outdated": 0,
                    "items": [
                        {
                            "resource_type": "ONLINE_STORE_THEME_LOCALE_CONTENT",
                            "key": "customloc.add_to_cart",
                            "source_value": source_value,
                            "status": "missing",
                        },
                        {
                            "resource_type": "ONLINE_STORE_THEME_LOCALE_CONTENT",
                            "key": "customloc.new_copy",
                            "source_value": "Nuovo testo",
                            "status": "missing",
                        },
                    ],
                }
            },
        }

    monkeypatch.setattr(theme, "audit_theme_translations", fake_shopify_audit)

    result = asyncio.run(theme_poller._run_theme_audit(_config()))

    assert result["locales"]["de"]["reusable_from_memory"] == 1
    assert result["locales"]["de"]["ai_required"] == 1
    assert "items" not in result["locales"]["de"]


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


def test_manual_sync_preserves_explicit_batch_cap(monkeypatch):
    _patch_runtime(monkeypatch)
    seen = {}

    async def fake_sync(cfg):
        seen["cfg"] = cfg
        return {
            "resources": 39,
            "changed_resources": 2,
            "changed_sections": 25,
            "registered": 25,
            "target_locales": cfg.target_locales,
            "resource_types": [],
            "items": [{"status": "synced"}],
        }

    monkeypatch.setattr(theme_poller, "_run_theme", fake_sync)

    result = theme_poller.handler(
        {
            "manual": True,
            "action": "theme_sync",
            "job_id": "22222222-2222-4222-8222-222222222223",
            "actor": "operator@example.com",
            "theme_id": "123456789012",
            "max_translations": 25,
        },
        None,
    )

    assert seen["cfg"].max_translations == 25
    assert seen["cfg"].dry_run is False
    assert result["registered"] == 25


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


def test_scheduled_poll_skips_neon_and_ai_when_fingerprints_are_unchanged(monkeypatch):
    cfg = replace(
        _config(poll_enabled=True),
        checkpoint_table="snapshots",
        global_resource_poll_enabled=True,
    )

    async def fake_theme_probe(_cfg):
        return {"fingerprint": "theme-current", "resources": 49, "fields": 1207}

    async def fake_global_probe(_cfg):
        return {"fingerprint": "global-current", "resources": 209, "fields": 209}

    async def should_not_sync(_cfg):
        raise AssertionError("unchanged fingerprint must not wake the full sync")

    monkeypatch.setattr(theme_poller, "_probe_theme", fake_theme_probe)
    monkeypatch.setattr(theme_poller, "_probe_global_resources", fake_global_probe)
    monkeypatch.setattr(
        theme_poller,
        "_read_fingerprint",
        lambda _cfg, group: f"{group}-current",
    )
    monkeypatch.setattr(theme_poller, "_run_theme", should_not_sync)
    monkeypatch.setattr(theme_poller, "_run_global_resources", should_not_sync)
    monkeypatch.setattr(theme_poller, "_acquire_poll_lock", lambda _cfg: "lock")
    monkeypatch.setattr(theme_poller, "_release_poll_lock", lambda _cfg, _token: None)

    result = asyncio.run(theme_poller._run_scheduled_poll(cfg))

    assert result["event"] == "scheduled_translation_poll"
    assert result["theme"]["status"] == "unchanged"
    assert result["global"]["status"] == "unchanged"


def test_global_change_runs_global_before_theme_and_checkpoints_fresh_state(monkeypatch):
    cfg = replace(
        _config(poll_enabled=True),
        checkpoint_table="snapshots",
        global_resource_poll_enabled=True,
    )
    calls = []
    probe_counts = {"theme": 0, "global": 0}

    async def fake_theme_probe(_cfg):
        probe_counts["theme"] += 1
        return {"fingerprint": "theme-fresh", "resources": 49, "fields": 1207}

    async def fake_global_probe(_cfg):
        probe_counts["global"] += 1
        return {"fingerprint": "global-fresh", "resources": 209, "fields": 209}

    async def fake_global_sync(_cfg):
        calls.append("global")
        return {"items": [{"status": "synced"}], "registered": 1}

    async def fake_theme_sync(_cfg):
        calls.append("theme")
        return {"items": [{"status": "unchanged"}], "registered": 0}

    written = []
    monkeypatch.setattr(theme_poller, "_probe_theme", fake_theme_probe)
    monkeypatch.setattr(theme_poller, "_probe_global_resources", fake_global_probe)
    monkeypatch.setattr(
        theme_poller,
        "_read_fingerprint",
        lambda _cfg, group: "theme-fresh" if group == "theme" else "global-old",
    )
    monkeypatch.setattr(theme_poller, "_run_global_resources", fake_global_sync)
    monkeypatch.setattr(theme_poller, "_run_theme", fake_theme_sync)
    monkeypatch.setattr(
        theme_poller,
        "_write_fingerprint",
        lambda _cfg, group, probe: written.append((group, probe["fingerprint"])),
    )
    monkeypatch.setattr(theme_poller, "_acquire_poll_lock", lambda _cfg: "lock")
    monkeypatch.setattr(theme_poller, "_release_poll_lock", lambda _cfg, _token: None)

    asyncio.run(theme_poller._run_scheduled_poll(cfg))

    assert calls == ["global", "theme"]
    assert written == [("global", "global-fresh"), ("theme", "theme-fresh")]
    assert probe_counts == {"theme": 2, "global": 2}


def test_concurrent_scheduled_poll_is_skipped_before_shopify_probe(monkeypatch):
    cfg = replace(_config(poll_enabled=True), checkpoint_table="snapshots")

    async def should_not_probe(_cfg):
        raise AssertionError("concurrent run must stop before Shopify queries")

    monkeypatch.setattr(theme_poller, "_acquire_poll_lock", lambda _cfg: None)
    monkeypatch.setattr(theme_poller, "_probe_theme", should_not_probe)

    result = asyncio.run(theme_poller._run_scheduled_poll(cfg))

    assert result["status"] == "skipped_concurrent_run"

import asyncio

from src.aws_lambda import worker


def test_handler_disabled(monkeypatch):
    monkeypatch.setenv("DDB_TABLE", "snap")
    monkeypatch.setenv("DEDUP_TABLE", "dedup")
    monkeypatch.setenv("DISABLE_SYNC", "true")

    res = worker.handler({"Records": [{"messageId": "m1"}]}, None)

    assert res["statusCode"] == 200
    assert res["batchItemFailures"] == [{"itemIdentifier": "m1"}]


def test_process_one_reports_failure_id(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de", "fr"],
        mf_include=[],
        debounce_seconds=20,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
    )

    monkeypatch.setattr(worker, "_event_already_processed", lambda cfg, event_id: False)
    monkeypatch.setattr(worker, "_acquire_debounce", lambda cfg, shop, product_gid: True)
    monkeypatch.setattr(worker, "_apply_runtime_secrets", lambda cfg: None)
    monkeypatch.setattr(worker, "_mark_event_processed", lambda cfg, event_id: None)

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "_run_backend", _boom)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "m-123",
                "body": '{"id": 101}',
                "messageAttributes": {
                    "Topic": {"stringValue": "products/update"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "evt-1"},
                },
            },
            cfg,
        )
    )

    assert not ok
    assert failure_id == "m-123"


def test_process_one_dedup_skip_is_success(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de"],
        mf_include=[],
        debounce_seconds=20,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
    )

    monkeypatch.setattr(worker, "_event_already_processed", lambda cfg, event_id: True)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "m-124",
                "body": '{"id": 202}',
                "messageAttributes": {
                    "Topic": {"stringValue": "products/update"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "evt-2"},
                },
            },
            cfg,
        )
    )

    assert ok
    assert failure_id is None


def test_process_one_debounce_requests_retry(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de"],
        mf_include=[],
        debounce_seconds=20,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
    )

    monkeypatch.setattr(worker, "_event_already_processed", lambda cfg, event_id: False)
    monkeypatch.setattr(worker, "_acquire_debounce", lambda cfg, shop, product_gid: False)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "m-125",
                "body": '{"id": 303}',
                "messageAttributes": {
                    "Topic": {"stringValue": "products/update"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "evt-3"},
                },
            },
            cfg,
        )
    )

    assert not ok
    assert failure_id == "m-125"


def test_debounce_defers_only_the_current_message(monkeypatch):
    calls = []

    class _FakeSQS:
        def change_message_visibility(self, **kwargs):
            calls.append(kwargs)

    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de"],
        mf_include=[],
        debounce_seconds=20,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
        sqs_url="https://example.com/products",
    )
    monkeypatch.setattr(worker, "_SQS", _FakeSQS())

    worker._defer_debounced_record(
        cfg,
        {
            "messageId": "m-126",
            "receiptHandle": "receipt-126",
        },
    )

    assert calls == [
        {
            "QueueUrl": "https://example.com/products",
            "ReceiptHandle": "receipt-126",
            "VisibilityTimeout": 20,
        }
    ]


def test_product_update_backend_only_processes_content_changes(monkeypatch):
    calls = []
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de", "fr"],
        mf_include=[],
        debounce_seconds=60,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
        seo_sync_enabled=True,
    )

    async def _fake_sync_products_incremental(**kwargs):
        calls.append(kwargs)
        return {"items": [{"status": "unchanged"}]}

    from src.bootstrap import incremental

    monkeypatch.setattr(
        incremental,
        "sync_products_incremental",
        _fake_sync_products_incremental,
    )

    asyncio.run(
        worker._run_backend(
            cfg=cfg,
            product_id="123",
            is_create=False,
            shop="example.myshopify.com",
        )
    )

    assert len(calls) == 1
    assert calls[0]["content_changes_only"] is True
    assert calls[0]["apply_translations"] is True
    assert calls[0]["sync_seo"] is True


def test_theme_update_runs_read_only_tracker_not_product_backend(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de", "fr"],
        mf_include=[],
        debounce_seconds=60,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
        theme_tracking_enabled=True,
        approved_theme_id="123456789012",
    )
    calls = []
    monkeypatch.setattr(worker, "_event_already_processed", lambda _cfg, _event_id: False)
    monkeypatch.setattr(worker, "_acquire_debounce", lambda _cfg, shop, product_gid: True)
    monkeypatch.setattr(worker, "_apply_runtime_secrets", lambda _cfg: None)
    monkeypatch.setattr(worker, "_mark_event_processed", lambda _cfg, _event_id: None)

    async def _unexpected_product_backend(**_kwargs):
        raise AssertionError("theme webhook must not enter product translation")

    monkeypatch.setattr(worker, "_run_backend", _unexpected_product_backend)

    from src.bootstrap import theme_tracking

    async def _track(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "read_only": True,
            "status": "unchanged",
            "approved_theme_id": "123456789012",
            "actual_theme_id": "123456789012",
        }

    monkeypatch.setattr(theme_tracking, "track_main_theme_read_only", _track)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "theme-1",
                "body": "{}",
                "messageAttributes": {
                    "Topic": {"stringValue": "themes/update"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "theme-event-1"},
                },
            },
            cfg,
        )
    )

    assert ok is True
    assert failure_id is None
    assert calls == [
        {
            "approved_theme_id": "123456789012",
            "topic": "themes/update",
            "event_id": "theme-event-1",
            "source_locale": "it",
        }
    ]


def test_theme_update_realtime_audits_remote_even_after_loop_event(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de", "fr"],
        mf_include=[],
        debounce_seconds=60,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
        theme_tracking_enabled=True,
        theme_realtime_sync_enabled=True,
        approved_theme_id="123456789012",
    )
    sync_calls = []
    marked = []
    monkeypatch.setattr(
        worker,
        "_event_already_processed",
        lambda _cfg, _event_id: False,
    )
    monkeypatch.setattr(
        worker,
        "_acquire_debounce",
        lambda _cfg, shop, product_gid: True,
    )
    monkeypatch.setattr(worker, "_apply_runtime_secrets", lambda _cfg: None)
    monkeypatch.setattr(
        worker,
        "_mark_event_processed",
        lambda _cfg, event_id: marked.append(event_id),
    )

    from src.bootstrap import theme_tracking

    async def _track(**_kwargs):
        return {
            "ok": True,
            "read_only": True,
            "status": "unchanged",
            "approved_theme_id": "123456789012",
            "actual_theme_id": "123456789012",
        }

    async def _sync(*, cfg):
        sync_calls.append(cfg.approved_theme_id)
        return {
            "resources": 39,
            "changed_resources": 0,
            "changed_sections": 0,
            "registered": 0,
            "target_locales": ["de", "fr"],
            "items": [{"status": "unchanged"}],
        }

    monkeypatch.setattr(theme_tracking, "track_main_theme_read_only", _track)
    monkeypatch.setattr(worker, "_run_theme_realtime_sync", _sync)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "theme-loop",
                "body": "{}",
                "messageAttributes": {
                    "Topic": {"stringValue": "themes/update"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "theme-loop-event"},
                },
            },
            cfg,
        )
    )

    assert ok is True
    assert failure_id is None
    assert sync_calls == ["123456789012"]
    assert marked == ["theme-loop-event"]


def test_theme_realtime_blocks_when_main_theme_guard_mismatches(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de", "fr"],
        mf_include=[],
        debounce_seconds=60,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
        theme_tracking_enabled=True,
        theme_realtime_sync_enabled=True,
        approved_theme_id="123456789012",
    )
    monkeypatch.setattr(
        worker,
        "_event_already_processed",
        lambda _cfg, _event_id: False,
    )
    monkeypatch.setattr(
        worker,
        "_acquire_debounce",
        lambda _cfg, shop, product_gid: True,
    )
    monkeypatch.setattr(worker, "_apply_runtime_secrets", lambda _cfg: None)
    monkeypatch.setattr(worker, "_mark_event_processed", lambda _cfg, _event_id: None)

    from src.bootstrap import theme_tracking

    async def _track(**_kwargs):
        return {
            "ok": True,
            "read_only": True,
            "status": "blocked_main_mismatch",
            "approved_theme_id": "123456789012",
            "actual_theme_id": "999",
        }

    async def _unexpected_sync(**_kwargs):
        raise AssertionError("mismatched MAIN theme must never be translated")

    monkeypatch.setattr(theme_tracking, "track_main_theme_read_only", _track)
    monkeypatch.setattr(worker, "_run_theme_realtime_sync", _unexpected_sync)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "theme-mismatch",
                "body": "{}",
                "messageAttributes": {
                    "Topic": {"stringValue": "themes/publish"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "theme-mismatch-event"},
                },
            },
            cfg,
        )
    )

    assert ok is True
    assert failure_id is None


def test_theme_realtime_failed_resource_retries_sqs_message(monkeypatch):
    cfg = worker.WorkerConfig(
        ddb_table="snap",
        dedup_table="dedup",
        source_locale="it",
        target_locales=["de", "fr"],
        mf_include=[],
        debounce_seconds=60,
        dry_run=False,
        shop_domain="example.myshopify.com",
        openai_api_key_secret_arn="",
        shopify_admin_token_secret_arn="",
        neon_database_url_secret_arn="",
        disable_sync=False,
        log_verbose_sync=False,
        theme_tracking_enabled=True,
        theme_realtime_sync_enabled=True,
        approved_theme_id="123456789012",
    )
    marked = []
    monkeypatch.setattr(
        worker,
        "_event_already_processed",
        lambda _cfg, _event_id: False,
    )
    monkeypatch.setattr(
        worker,
        "_acquire_debounce",
        lambda _cfg, shop, product_gid: True,
    )
    monkeypatch.setattr(worker, "_apply_runtime_secrets", lambda _cfg: None)
    monkeypatch.setattr(
        worker,
        "_mark_event_processed",
        lambda _cfg, event_id: marked.append(event_id),
    )

    from src.bootstrap import theme_tracking

    async def _track(**_kwargs):
        return {
            "ok": True,
            "status": "changed",
            "approved_theme_id": "123456789012",
            "actual_theme_id": "123456789012",
        }

    async def _sync(*, cfg):
        return {
            "items": [{"status": "failed"}],
            "registered": 0,
        }

    monkeypatch.setattr(theme_tracking, "track_main_theme_read_only", _track)
    monkeypatch.setattr(worker, "_run_theme_realtime_sync", _sync)

    ok, failure_id = asyncio.run(
        worker._process_one(
            {
                "messageId": "theme-failed",
                "body": "{}",
                "messageAttributes": {
                    "Topic": {"stringValue": "themes/update"},
                    "Shop": {"stringValue": "example.myshopify.com"},
                    "EventId": {"stringValue": "theme-failed-event"},
                },
            },
            cfg,
        )
    )

    assert ok is False
    assert failure_id == "theme-failed"
    assert marked == []

import asyncio

from src.aws_lambda import worker


def test_handler_disabled(monkeypatch):
    monkeypatch.setenv("DDB_TABLE", "snap")
    monkeypatch.setenv("DEDUP_TABLE", "dedup")
    monkeypatch.setenv("DISABLE_SYNC", "true")

    res = worker.handler({"Records": [{"messageId": "m1"}]}, None)

    assert res["statusCode"] == 200
    assert res["batchItemFailures"] == []


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

    monkeypatch.setattr(worker, "_mark_event_seen", lambda cfg, event_id: True)
    monkeypatch.setattr(worker, "_acquire_debounce", lambda cfg, shop, product_gid: True)
    monkeypatch.setattr(worker, "_apply_runtime_secrets", lambda cfg: None)

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

    monkeypatch.setattr(worker, "_mark_event_seen", lambda cfg, event_id: False)

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

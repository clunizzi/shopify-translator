import asyncio

from src.bootstrap import handles

P1 = "gid://shopify/Product/1"
P2 = "gid://shopify/Product/2"
P3 = "gid://shopify/Product/3"


def _resource(product_gid: str, title: str, handle: str) -> dict:
    return {
        "resourceId": product_gid,
        "translatableContent": [
            {"key": "title", "value": title, "digest": f"title-{product_gid}"},
            {"key": "handle", "value": handle, "digest": f"handle-{product_gid}"},
        ],
    }


def test_audit_preserves_current_handle_and_repairs_only_missing_or_outdated(monkeypatch):
    async def _resources():
        return [
            _resource(P1, "Uno", "uno"),
            _resource(P2, "Due", "due"),
            _resource(P3, "Tre", "tre"),
        ]

    async def _translations(resource_ids, locale):
        assert resource_ids == [P1, P2, P3]
        assert locale == "de"
        return {
            P1: {
                "title": {"value": "Eins", "outdated": False},
                "handle": {"value": "historische-url", "outdated": False},
            },
            P2: {
                "title": {"value": "Zwei Kompakt", "outdated": False},
            },
            P3: {
                "title": {"value": "Drei", "outdated": False},
                "handle": {"value": "alte-url", "outdated": True},
            },
        }

    monkeypatch.setattr(handles, "_fetch_product_resources", _resources)
    monkeypatch.setattr(handles, "get_resource_translations_by_ids", _translations)

    report = asyncio.run(handles.audit_product_handles(target_locales=["de"]))

    assert report["locales"]["de"]["handles_current"] == 1
    assert report["locales"]["de"]["handles_missing"] == 1
    assert report["locales"]["de"]["handles_outdated"] == 1
    assert report["blocked_items"] == []
    assert [(item["product_id"], item["action"], item["value"]) for item in report["plan"]] == [
        (2, "create_missing", "zwei-kompakt"),
        (3, "refresh_digest", "alte-url"),
    ]


def test_missing_handle_with_collision_or_unusable_title_is_blocked(monkeypatch):
    async def _resources():
        return [
            _resource(P1, "Uno", "uno"),
            _resource(P2, "Due", "due"),
            _resource(P3, "Tre", "tre"),
        ]

    async def _translations(_resource_ids, _locale):
        return {
            P1: {
                "title": {"value": "Gleich", "outdated": False},
                "handle": {"value": "gleich", "outdated": False},
            },
            P2: {"title": {"value": "Gleich", "outdated": False}},
            P3: {"title": {"value": "Alt", "outdated": True}},
        }

    monkeypatch.setattr(handles, "_fetch_product_resources", _resources)
    monkeypatch.setattr(handles, "get_resource_translations_by_ids", _translations)

    report = asyncio.run(handles.audit_product_handles(target_locales=["de"]))

    assert report["plan"] == []
    assert report["locales"]["de"]["blocked_collisions"] == 1
    assert report["locales"]["de"]["blocked_outdated_title"] == 1
    assert report["blocked"] == 2
    assert report["blocked_items"] == [
        {
            "product_id": 2,
            "product_gid": P2,
            "locale": "de",
            "reason": "localized_handle_collision",
            "source_title": "Due",
            "source_handle": "due",
            "localized_title": "Gleich",
            "desired_handle": "gleich",
            "collision_owner_ids": [1],
        },
        {
            "product_id": 3,
            "product_gid": P3,
            "locale": "de",
            "reason": "outdated_localized_title",
            "source_title": "Tre",
            "source_handle": "tre",
            "localized_title": "Alt",
            "desired_handle": "",
            "collision_owner_ids": [],
        },
    ]


def test_allocate_unique_handle_uses_first_available_numeric_suffix():
    reserved = {"gleich", "gleich-2", "other"}

    assert handles._allocate_unique_handle("gleich", reserved=reserved) == "gleich-3"
    assert "gleich-3" in reserved


def test_allocate_unique_handle_keeps_free_desired_value():
    reserved = {"other"}

    assert handles._allocate_unique_handle("frei", reserved=reserved) == "frei"
    assert "frei" in reserved


def test_repair_plan_only_never_registers(monkeypatch):
    async def _audit(*, target_locales):
        return {
            "target_locales": target_locales,
            "locales": {},
            "plan": [
                {
                    "product_id": 2,
                    "product_gid": P2,
                    "locale": "fr",
                    "action": "create_missing",
                    "value": "deux",
                    "source_digest": "digest",
                }
            ],
        }

    async def _unexpected(*_args, **_kwargs):
        raise AssertionError("plan-only must not write Shopify")

    monkeypatch.setattr(handles, "audit_product_handles", _audit)
    monkeypatch.setattr(handles, "register_translations", _unexpected)

    summary = asyncio.run(
        handles.repair_product_handles(
            target_locales=["fr"],
            apply_translations=False,
            dry_run=False,
        )
    )
    assert summary["selected_items"] == 1
    assert summary["registered_items"] == 0


def test_repair_outdated_reuses_exact_existing_value(monkeypatch):
    async def _audit(*, target_locales):
        return {
            "target_locales": target_locales,
            "locales": {},
            "plan": [
                {
                    "product_id": 3,
                    "product_gid": P3,
                    "locale": "de",
                    "action": "refresh_digest",
                    "value": "url-storica-immutata",
                    "source_digest": "new-digest",
                }
            ],
        }

    calls = []

    async def _register(resource_id, payloads):
        calls.append((resource_id, payloads))
        return []

    monkeypatch.setattr(handles, "audit_product_handles", _audit)
    monkeypatch.setattr(handles, "register_translations", _register)

    summary = asyncio.run(
        handles.repair_product_handles(
            target_locales=["de"],
            apply_translations=True,
            dry_run=False,
        )
    )
    assert summary["registered_items"] == 1
    assert calls[0][1][0]["value"] == "url-storica-immutata"


def test_repair_collects_failures_without_rewriting_other_plan_items(monkeypatch):
    async def _audit(*, target_locales):
        return {
            "target_locales": target_locales,
            "locales": {},
            "plan": [
                {
                    "product_id": product_id,
                    "product_gid": f"gid://shopify/Product/{product_id}",
                    "locale": "de",
                    "action": "create_missing",
                    "value": f"handle-{product_id}",
                    "source_digest": f"digest-{product_id}",
                }
                for product_id in (1, 2)
            ],
        }

    async def _register(resource_id, _payloads):
        if resource_id.endswith("/2"):
            return [{"field": ["translations"], "message": "collision"}]
        return []

    monkeypatch.setattr(handles, "audit_product_handles", _audit)
    monkeypatch.setattr(handles, "register_translations", _register)

    summary = asyncio.run(
        handles.repair_product_handles(
            target_locales=["de"],
            apply_translations=True,
            dry_run=False,
            concurrency=4,
        )
    )
    assert summary["registered_items"] == 1
    assert summary["failed_items"] == 1
    assert summary["failures"][0]["product_id"] == 2

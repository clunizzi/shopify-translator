from src.shopify.sync import process_product


def test_sync_dry_run(monkeypatch, tmp_path):
    # Arrange: fake Shopify responses
    pid = 101
    product_gid = f"gid://shopify/Product/{pid}"
    metafield_gid = "gid://shopify/Metafield/555"

    async def fake_get_product_metafields_by_keys(product_gid_param, keys):
        assert product_gid_param == product_gid
        return [
            {"id": metafield_gid, "namespace": "custom", "key": "specs", "type": "json", "value": "{}"}
        ]

    async def fake_get_translatable_by_ids(ids):
        assert product_gid in ids and metafield_gid in ids
        # Mark product title and metafield value as changed
        return {
            product_gid: [
                {"key": "title", "value": "Zaino tecnico 25L", "digest": "P1", "locale": "it"},
            ],
            metafield_gid: [
                {"key": "value", "value": '{"blocks": [{"title": "Leggero"}]}', "digest": "M1", "locale": "it"}
            ],
        }

    calls = {"register": 0}

    async def fake_register(resource_id, translations):
        calls["register"] += 1
        return []

    monkeypatch.setenv("OPENAI_API_KEY", "x")  # allow translator instantiation if needed
    monkeypatch.chdir(tmp_path)

    import src.shopify.graphql as gq

    monkeypatch.setattr(gq, "get_product_metafields_by_keys", fake_get_product_metafields_by_keys)
    monkeypatch.setattr(gq, "get_translatable_by_ids", fake_get_translatable_by_ids)
    monkeypatch.setattr(gq, "register_translations", fake_register)

    # Act
    import asyncio as _asyncio

    res = _asyncio.run(process_product(
        product_numeric_id=pid,
        target_locales=["fr-FR"],
        mf_include=[("custom", "specs")],
        mf_json_paths=["blocks[].title"],
        source_locale="it",
        dry_run=True,
        is_create=False,
        apply_on_dry_run=False,
    ))

    # Assert: nothing pushed in dry-run, snapshot not updated
    assert res["product_id"] == pid
    assert res["translated"] >= 1
    assert res["pushed"] == 0
    assert calls["register"] == 0

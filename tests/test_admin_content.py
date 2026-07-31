import asyncio

import pytest

from src import admin_content


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8628338393308", "id:8628338393308"),
        (
            "gid://shopify/Product/8628338393308",
            "id:8628338393308",
        ),
        (
            "https://admin.shopify.com/store/example-store/products/8628338393308",
            "id:8628338393308",
        ),
        ("motosega stihl", "motosega stihl"),
    ],
)
def test_normalize_product_search_query(value, expected):
    assert admin_content.normalize_product_search_query(value) == expected


def test_inspect_product_includes_seo_metafields_options_and_live_status(monkeypatch):
    product_gid = "gid://shopify/Product/123"
    metafield_gid = "gid://shopify/Metafield/456"
    option_gid = "gid://shopify/ProductOption/789"

    async def fake_summary(_product_gid):
        return {
            "id": product_gid,
            "title": "Motosega",
            "handle": "motosega",
            "status": "ACTIVE",
            "updated_at": "2026-07-30T10:00:00Z",
            "online_store_url": "https://shop.example.com/products/motosega",
        }

    async def fake_metafields(_product_gid, allowed_types=None):
        assert "json" in allowed_types
        return [
            {
                "id": metafield_gid,
                "namespace": "custom",
                "key": "benefits",
                "type": "multi_line_text_field",
                "value": "Potente",
            }
        ]

    async def fake_options(_product_gid):
        return [{"resource_id": option_gid, "kind": "option_name"}]

    async def fake_sources(resource_ids):
        assert resource_ids == [product_gid, metafield_gid, option_gid]
        return {
            product_gid: [
                {
                    "key": "title",
                    "value": "Motosega",
                    "digest": "title-digest",
                    "locale": "it",
                },
                {
                    "key": "seo.title",
                    "value": "Motosega professionale",
                    "digest": "seo-digest",
                    "locale": "it",
                },
                {
                    "key": "handle",
                    "value": "motosega",
                    "digest": "handle-digest",
                    "locale": "it",
                },
            ],
            metafield_gid: [
                {
                    "key": "value",
                    "value": "Potente",
                    "digest": "meta-digest",
                    "locale": "it",
                }
            ],
            option_gid: [
                {
                    "key": "name",
                    "value": "Cilindrata",
                    "digest": "option-digest",
                    "locale": "it",
                }
            ],
        }

    async def fake_matrix(resource_ids, locales):
        assert locales == ["de", "fr"]
        return {resource_id: {"de": {}, "fr": {}} for resource_id in resource_ids} | {
            product_gid: {
                "de": {
                    "title": {
                        "value": "Kettensäge",
                        "outdated": False,
                    }
                },
                "fr": {
                    "title": {
                        "value": "Tronçonneuse",
                        "outdated": True,
                    }
                },
            }
        }

    monkeypatch.setattr(admin_content, "get_product_summary", fake_summary)
    monkeypatch.setattr(admin_content, "get_product_all_metafields", fake_metafields)
    monkeypatch.setattr(admin_content, "get_product_option_resources", fake_options)
    monkeypatch.setattr(admin_content, "get_translatable_by_ids", fake_sources)
    monkeypatch.setattr(admin_content, "get_resource_translation_matrix", fake_matrix)

    result = asyncio.run(admin_content.inspect_product(product_gid))
    keys = {(field["group"], field["key"]) for field in result["fields"]}

    assert ("product", "seo.title") in keys
    assert ("product", "handle") in keys
    assert ("metafield", "value") in keys
    assert ("option", "name") in keys
    title = next(field for field in result["fields"] if field["key"] == "title")
    assert title["translations"]["de"]["status"] == "current"
    assert title["translations"]["fr"]["status"] == "outdated"
    assert result["preview_urls"]["de"] == ("https://shop.example.com/de-de/products/motosega")
    assert result["preview_urls"]["fr"] == ("https://shop.example.com/fr-fr/products/motosega")


def test_inspect_product_hides_shopify_default_option_placeholders(monkeypatch):
    product_gid = "gid://shopify/Product/123"
    option_gid = "gid://shopify/ProductOption/789"
    option_value_gid = "gid://shopify/ProductOptionValue/790"

    async def fake_summary(_product_gid):
        return {
            "id": product_gid,
            "title": "Motosega",
            "handle": "motosega",
            "status": "ACTIVE",
            "online_store_url": "https://shop.example.com/products/motosega",
        }

    async def fake_metafields(_product_gid, allowed_types=None):
        return []

    async def fake_options(_product_gid):
        return [
            {"resource_id": option_gid, "kind": "option_name"},
            {"resource_id": option_value_gid, "kind": "option_value"},
        ]

    async def fake_sources(resource_ids):
        assert resource_ids == [product_gid, option_gid, option_value_gid]
        return {
            product_gid: [
                {
                    "key": "title",
                    "value": "Motosega",
                    "digest": "title-digest",
                    "locale": "it",
                }
            ],
            option_gid: [
                {
                    "key": "name",
                    "value": "Title",
                    "digest": "option-digest",
                    "locale": "it",
                }
            ],
            option_value_gid: [
                {
                    "key": "name",
                    "value": "Default Title",
                    "digest": "value-digest",
                    "locale": "it",
                }
            ],
        }

    async def fake_matrix(resource_ids, locales):
        return {resource_id: {locale: {} for locale in locales} for resource_id in resource_ids}

    monkeypatch.setattr(admin_content, "get_product_summary", fake_summary)
    monkeypatch.setattr(admin_content, "get_product_all_metafields", fake_metafields)
    monkeypatch.setattr(admin_content, "get_product_option_resources", fake_options)
    monkeypatch.setattr(admin_content, "get_translatable_by_ids", fake_sources)
    monkeypatch.setattr(admin_content, "get_resource_translation_matrix", fake_matrix)

    result = asyncio.run(admin_content.inspect_product(product_gid))

    assert [(field["group"], field["key"]) for field in result["fields"]] == [("product", "title")]


def test_json_editor_exposes_only_policy_approved_string_paths():
    field = admin_content._field_payload(
        resource_id="gid://shopify/Metafield/456",
        resource_label="custom.manuals",
        group="metafield",
        key="value",
        source={
            "value": (
                '{"items":[{"label":"Manuale italiano",'
                '"source_url":"https://cdn.example/manual.pdf",'
                '"format":"PDF","size":"2 MB"}]}'
            ),
            "digest": "digest",
            "locale": "it",
        },
        matrix={},
        metafield_type="json",
        metafield_namespace="custom",
        metafield_key="manuals",
    )

    assert field["editable_json_paths"] == [["items", 0, "label"]]


def test_save_registers_exactly_one_translation_after_digest_check(monkeypatch):
    inspection = {
        "product": {"id": "gid://shopify/Product/123"},
        "fields": [
            {
                "resource_id": "gid://shopify/Product/123",
                "key": "title",
                "content_kind": "plain",
                "source": {
                    "value": "Motosega",
                    "digest": "digest-1",
                },
            }
        ],
    }
    seen = {}

    async def fake_inspect(_product_id):
        return inspection

    async def fake_register(resource_id, translations):
        seen["resource_id"] = resource_id
        seen["translations"] = translations
        return []

    async def fake_matrix(_resource_ids, _locales):
        return {
            "gid://shopify/Product/123": {
                "de": {
                    "title": {
                        "value": "Kettensäge",
                        "outdated": False,
                    }
                }
            }
        }

    monkeypatch.setattr(admin_content, "inspect_product", fake_inspect)
    monkeypatch.setattr(admin_content, "register_translations", fake_register)
    monkeypatch.setattr(admin_content, "get_resource_translation_matrix", fake_matrix)

    result = asyncio.run(
        admin_content.save_product_translation(
            {
                "product_id": "gid://shopify/Product/123",
                "resource_id": "gid://shopify/Product/123",
                "key": "title",
                "locale": "de",
                "value": "Kettensäge",
                "digest": "digest-1",
            }
        )
    )

    assert seen["resource_id"] == "gid://shopify/Product/123"
    assert seen["translations"] == [
        {
            "locale": "de",
            "key": "title",
            "value": "Kettensäge",
            "translatableContentDigest": "digest-1",
        }
    ]
    assert result["saved"]["status"] == "current"


def test_save_fails_closed_when_source_digest_changed(monkeypatch):
    async def fake_inspect(_product_id):
        return {
            "product": {"id": "gid://shopify/Product/123"},
            "fields": [
                {
                    "resource_id": "gid://shopify/Product/123",
                    "key": "title",
                    "content_kind": "plain",
                    "source": {"value": "Motosega", "digest": "new-digest"},
                }
            ],
        }

    async def should_not_register(*_args, **_kwargs):
        raise AssertionError("stale edits must not reach Shopify")

    monkeypatch.setattr(admin_content, "inspect_product", fake_inspect)
    monkeypatch.setattr(admin_content, "register_translations", should_not_register)

    with pytest.raises(admin_content.AdminContentError) as exc:
        asyncio.run(
            admin_content.save_product_translation(
                {
                    "product_id": "gid://shopify/Product/123",
                    "resource_id": "gid://shopify/Product/123",
                    "key": "title",
                    "locale": "de",
                    "value": "Kettensäge",
                    "digest": "old-digest",
                }
            )
        )

    assert exc.value.code == "SOURCE_CHANGED"


@pytest.mark.parametrize(
    ("source", "value", "kind"),
    [
        ("Ciao {{ product.title }}", "Hallo {{ product.handle }}", "liquid"),
        ("<p>Ciao</p>", "<div>Hallo</div>", "html"),
        ('{"text":"Ciao","enabled":true}', '{"text":"Hallo","enabled":false}', "json"),
    ],
)
def test_manual_editor_rejects_structural_damage(source, value, kind):
    issue = admin_content._manual_value_issue(
        source,
        value,
        kind=kind,
        key="text",
    )
    assert issue

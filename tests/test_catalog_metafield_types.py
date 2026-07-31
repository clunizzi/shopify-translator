from src.bootstrap.catalog import AUTO_TYPES, build_pdp_document


def _document_for(metafield_type: str):
    resource_id = "gid://shopify/Metafield/1"
    document, _ = build_pdp_document(
        shop_domain="example.myshopify.com",
        product_gid="gid://shopify/Product/1",
        metafields=[
            {
                "id": resource_id,
                "namespace": "custom",
                "key": "description",
                "type": metafield_type,
            }
        ],
        live_map={
            resource_id: [
                {
                    "key": "value",
                    "value": '{"type":"root","children":[]}',
                    "digest": "digest",
                    "locale": "it",
                }
            ]
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
        units=[],
    )
    return document


def test_auto_types_use_shopify_rich_text_field_name():
    assert "rich_text_field" in AUTO_TYPES
    assert "html" in AUTO_TYPES
    assert "rich_text" not in AUTO_TYPES


def test_rich_text_field_is_translated_as_json_ast():
    document = _document_for("rich_text_field")

    assert document["metafields"]["custom.description"]["content_kind"] == "json"


def test_html_metafield_is_translated_as_html():
    document = _document_for("html")

    assert document["metafields"]["custom.description"]["content_kind"] == "html"

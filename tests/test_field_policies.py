from src.bootstrap.catalog import build_pdp_document
from src.config.field_policies import should_translate_product_key
from src.config.metafield_policies import should_translate_metafield_leaf


def test_existing_product_handle_is_skipped():
    assert not should_translate_product_key(
        "handle",
        is_create=False,
        existing_product=True,
    )


def test_new_product_handle_is_allowed_on_create():
    assert should_translate_product_key(
        "handle",
        is_create=True,
        existing_product=False,
    )


def test_seo_keys_are_excluded():
    assert not should_translate_product_key(
        "seo.title",
        is_create=True,
        existing_product=False,
    )
    assert not should_translate_product_key(
        "seo.description",
        is_create=True,
        existing_product=False,
    )


def test_accessori_translates_only_human_title_leaf():
    assert should_translate_metafield_leaf(
        "custom",
        "accessori",
        ("items", 0, "title"),
        "Upgrade Kit HEXA 36 RH 60 - 4 in 1",
    )
    assert not should_translate_metafield_leaf(
        "custom",
        "accessori",
        ("items", 0, "product_handle"),
        "upgrade-kit-2-hexa-36-rh60",
    )
    assert not should_translate_metafield_leaf(
        "custom",
        "accessori",
        ("items", 0, "image"),
        "https://example.com/image.jpg",
    )


def test_manuals_only_translates_label():
    assert should_translate_metafield_leaf(
        "custom",
        "manuals",
        ("items", 0, "label"),
        "Istruzioni per l'uso",
    )
    assert not should_translate_metafield_leaf(
        "custom",
        "manuals",
        ("items", 0, "source_url"),
        "https://example.com/manual.pdf",
    )


def test_existing_product_bootstrap_excludes_handle_even_without_prior_state():
    document, section_hashes = build_pdp_document(
        shop_domain="example.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Motozappa Honda", "digest": "d1", "locale": "it"},
                {"key": "handle", "value": "motozappa-honda", "digest": "d2", "locale": "it"},
                {"key": "body_html", "value": "<p>Test</p>", "digest": "d3", "locale": "it"},
            ]
        },
        source_locale="it",
        is_create=False,
        existing_product=True,
    )

    assert "handle" not in document["product"]
    assert "product.handle" not in section_hashes


def test_new_product_bootstrap_includes_handle():
    document, section_hashes = build_pdp_document(
        shop_domain="example.myshopify.com",
        product_gid="gid://shopify/Product/123",
        metafields=[],
        live_map={
            "gid://shopify/Product/123": [
                {"key": "title", "value": "Motozappa Honda", "digest": "d1", "locale": "it"},
                {"key": "handle", "value": "motozappa-honda", "digest": "d2", "locale": "it"},
            ]
        },
        source_locale="it",
        is_create=True,
        existing_product=False,
    )

    assert "handle" in document["product"]
    assert "product.handle" in section_hashes

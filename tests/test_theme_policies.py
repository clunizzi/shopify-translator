from src.config.theme_policies import should_translate_theme_entry


def test_theme_policy_allows_storefront_copy_fields():
    assert should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
        key="section.collection.hero.heading:abc",
        value="Spedizione gratuita su ordini superiori a 100€",
    )


def test_theme_policy_blocks_icons_svg_and_pure_liquid():
    assert not should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_SECTION_GROUP",
        key="section.header.icon_name:abc",
        value="chat",
    )
    assert not should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_SECTION_GROUP",
        key="section.header.logo_svg:abc",
        value="<svg><path /></svg>",
    )
    assert not should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
        key="section.collection.heading:abc",
        value="{{ collection.title }}",
    )


def test_theme_policy_skips_locale_content_even_if_textual():
    assert not should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_LOCALE_CONTENT",
        key="customloc.foo",
        value="Testo personalizzato",
    )


def test_theme_policy_allows_text_fields_inside_image_with_text_sections():
    assert should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
        key="section.index.json.image_with_text_bg_kgBTcM.heading_7YbxBB.heading:1yfftwktofaeh",
        value="Cos'è un Easy-Commerce?",
    )
    assert should_translate_theme_entry(
        resource_type="ONLINE_STORE_THEME_JSON_TEMPLATE",
        key="section.index.json.image_with_text_bg_kgBTcM.text_rNjyhk.text:1bwq785q13jyp",
        value="<p>AgriEden è un easy-commerce semplificato.</p>",
    )

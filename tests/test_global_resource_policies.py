from src.bootstrap.resources import should_translate_resource_entry


def test_shop_seo_fields_are_excluded():
    assert not should_translate_resource_entry(
        resource_type="SHOP",
        key="meta_title",
        value="Example Store: prodotti agricoli professionali",
    )
    assert not should_translate_resource_entry(
        resource_type="SHOP",
        key="meta_description",
        value="Scopri il catalogo Example Store.",
    )


def test_shop_non_seo_content_remains_translatable():
    assert should_translate_resource_entry(
        resource_type="SHOP",
        key="slogan",
        value="Coltiviamo il futuro",
    )
    assert should_translate_resource_entry(
        resource_type="SHOP",
        key="preferences_title",
        value="Preferenze cookie",
    )

from src.bootstrap import theme


class _PartiallyFailingTranslator:
    model = "test-model"

    def translate_plain(self, _resource_type, key, value, target_locale, _dnt, _exclude):
        if key == "section.bad.body:2":
            raise theme.TranslationError("quality gate rejected residual Italian")
        return f"{value} [{target_locale}]"

    def translate_html_document(
        self, resource_type, key, value, target_locale, dnt, exclude
    ):
        return self.translate_plain(resource_type, key, value, target_locale, dnt, exclude)


def test_theme_document_skips_one_failed_field_and_keeps_other_payloads():
    resource_type = "ONLINE_STORE_THEME_JSON_TEMPLATE"
    source_document = {
        "shop_domain": "example.myshopify.com",
        "theme_id": "111",
        "resource_type": resource_type,
        "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
        "source_locale": "it",
        "entries": {
            "section.good.heading:1": {
                "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
                "value": "Buono",
                "digest": "d1",
                "content_kind": "plain",
            },
            "section.bad.body:2": {
                "resource_id": "gid://shopify/OnlineStoreThemeJsonTemplate/1?theme_id=111",
                "value": "Testo difficile",
                "digest": "d2",
                "content_kind": "html",
            },
        },
    }
    changed_sections = {
        f"{resource_type}.section.good.heading:1",
        f"{resource_type}.section.bad.body:2",
    }

    document, hashes, payloads, sources = theme.translate_theme_document(
        source_document=source_document,
        changed_sections=changed_sections,
        target_locale="de",
        translator=_PartiallyFailingTranslator(),
    )

    assert document["entries"] == {"section.good.heading:1": "Buono [de]"}
    assert list(hashes) == [f"{resource_type}.section.good.heading:1"]
    assert [payload["key"] for payload in payloads] == ["section.good.heading:1"]
    assert sources[f"{resource_type}.section.bad.body:2"].startswith("translation_error:")

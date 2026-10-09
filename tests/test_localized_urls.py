import asyncio

from src.bootstrap import localized_urls
from src.bootstrap.localized_urls import (
    extract_internal_urls_from_html,
    localize_internal_url,
    localize_internal_urls_in_html,
)


def test_localized_page_url_uses_market_prefix_and_translated_handle():
    result = localize_internal_url(
        "/pages/negozio#orari",
        target_locale="de",
        route_prefixes={"de": "/de-de"},
        handle_maps={"de": {"pages": {"negozio": "shop"}}},
    )

    assert result == "/de-de/pages/shop#orari"


def test_localized_system_collection_keeps_handle_and_query():
    result = localize_internal_url(
        "/collections/all?sort_by=best-selling",
        target_locale="fr",
        route_prefixes={"fr": "/fr-fr"},
        handle_maps={},
    )

    assert result == "/fr-fr/collections/all?sort_by=best-selling"


def test_localized_url_does_not_duplicate_existing_target_prefix():
    result = localize_internal_url(
        "/fr-fr/pages/boutique#horaires",
        target_locale="fr",
        route_prefixes={"de": "/de-de", "fr": "/fr-fr"},
        handle_maps={},
    )

    assert result == "/fr-fr/pages/boutique#horaires"


def test_html_internal_links_are_localized_without_changing_external_links():
    source = (
        '<p><a href="/pages/negozio#orari">Negozio</a> '
        '<a href="https://example.com/pages/negozio">Esterno</a></p>'
    )

    assert extract_internal_urls_from_html(source) == ["/pages/negozio#orari"]
    result = localize_internal_urls_in_html(
        source,
        target_locale="de",
        route_prefixes={"de": "/de-de"},
        handle_maps={"de": {"pages": {"negozio": "laden"}}},
    )

    assert 'href="/de-de/pages/laden#orari"' in result
    assert 'href="https://example.com/pages/negozio"' in result


def test_handle_lookup_only_fetches_resources_referenced_by_theme(monkeypatch):
    calls = []

    async def _fake_list(*, resource_type, locales, first, after):
        calls.append(resource_type)
        assert resource_type == "PAGE"
        return (
            [
                {
                    "resourceId": "gid://shopify/Page/1",
                    "translatableContent": [{"key": "handle", "value": "negozio"}],
                    "translations": {
                        "de": {"handle": {"value": "laden", "outdated": False}},
                        "fr": {"handle": {"value": "boutique", "outdated": False}},
                    },
                }
            ],
            {"hasNextPage": True, "endCursor": "unused"},
        )

    monkeypatch.setattr(
        localized_urls,
        "list_translatable_resources_with_translations",
        _fake_list,
    )
    result = asyncio.run(
        localized_urls.fetch_localized_handle_maps(
            target_locales=["de", "fr"],
            source_urls=["/pages/negozio", "/collections/all"],
        )
    )

    assert calls == ["PAGE"]
    assert result["de"]["pages"]["negozio"] == "laden"
    assert result["fr"]["pages"]["negozio"] == "boutique"

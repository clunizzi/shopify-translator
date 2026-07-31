import asyncio

from src.shopify import graphql


def test_register_translations_maps_top_level_graphql_errors(monkeypatch):
    async def _fake_post_graphql(query, variables):
        return {
            "errors": [
                {
                    "message": "Variable $translations of type [TranslationInput!]! was provided invalid value",
                    "path": ["translationsRegister"],
                }
            ]
        }

    monkeypatch.setattr(graphql, "_post_graphql", _fake_post_graphql)

    result = asyncio.run(
        graphql.register_translations(
            "gid://shopify/OnlineStoreThemeJsonTemplate/1",
            [
                {
                    "key": "section.home.heading:abc",
                    "locale": "de",
                    "value": "Willkommen",
                    "translatableContentDigest": "d1",
                }
            ],
        )
    )

    assert result == [
        {
            "field": ["translationsRegister"],
            "message": "Variable $translations of type [TranslationInput!]! was provided invalid value",
        }
    ]

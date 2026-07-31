import asyncio

from src.shopify import graphql


def test_get_resource_translations_by_ids_preserves_outdated_state(monkeypatch):
    seen = {}

    async def _fake_post_graphql(query, variables):
        seen["query"] = query
        seen["variables"] = variables
        return {
            "data": {
                "translatableResource": {
                    "resourceId": "gid://shopify/Product/123",
                    "translations_de": [
                        {"key": "title", "value": "Titel", "outdated": False},
                        {"key": "body_html", "value": "<p>Alt</p>", "outdated": True},
                    ],
                }
            }
        }

    monkeypatch.setattr(graphql, "_post_graphql", _fake_post_graphql)

    result = asyncio.run(
        graphql.get_resource_translations_by_ids(
            ["gid://shopify/Product/123"],
            "de",
        )
    )

    assert seen["variables"] == {
        "id": "gid://shopify/Product/123",
    }
    assert "translatableResource(resourceId: $id)" in seen["query"]
    assert 'translations_de: translations(locale: "de")' in seen["query"]
    assert result["gid://shopify/Product/123"]["title"] == {
        "value": "Titel",
        "outdated": False,
    }
    assert result["gid://shopify/Product/123"]["body_html"]["outdated"] is True


def test_translation_resource_reads_are_chunked_at_shopify_limit(monkeypatch):
    calls = []

    async def _fake_post_graphql(query, variables):
        calls.append(variables)
        resource_ids = variables.get("resourceIds") or variables.get("ids") or []
        if not resource_ids:
            resource_id = variables["id"]
            return {
                "data": {
                    "translatableResource": {
                        "resourceId": resource_id,
                        "translations_de": [
                            {
                                "key": "title",
                                "value": "Titel",
                                "outdated": False,
                            }
                        ],
                    }
                }
            }
        return {
            "data": {
                "translatableResourcesByIds": {
                    "nodes": [
                        {
                            "resourceId": resource_id,
                            "translatableContent": [{"key": "title", "value": "Titolo"}],
                            "translations": [{"key": "title", "value": "Titel", "outdated": False}],
                        }
                        for resource_id in resource_ids
                    ]
                }
            }
        }

    monkeypatch.setattr(graphql, "_post_graphql", _fake_post_graphql)
    resource_ids = [f"gid://shopify/Metafield/{index}" for index in range(251)]

    sources = asyncio.run(graphql.get_translatable_by_ids(resource_ids))
    translations = asyncio.run(graphql.get_resource_translations_by_ids(resource_ids, "de"))

    assert [len(call.get("ids") or []) for call in calls[:2]] == [250, 1]
    assert [len(call) for call in calls[2:]] == [1] * 251
    assert len(sources) == 251
    assert len(translations) == 251


def test_translation_matrix_fetches_two_locales_in_one_resource_request(monkeypatch):
    seen = []

    async def _fake_post_graphql(query, variables):
        seen.append((query, variables))
        return {
            "data": {
                "translatableResource": {
                    "resourceId": variables["id"],
                    "translations_de": [{"key": "title", "value": "Titel", "outdated": False}],
                    "translations_fr": [{"key": "title", "value": "Titre", "outdated": True}],
                }
            }
        }

    monkeypatch.setattr(graphql, "_post_graphql", _fake_post_graphql)
    matrix = asyncio.run(
        graphql.get_resource_translation_matrix(
            ["gid://shopify/Product/123"],
            ["de", "fr"],
        )
    )

    assert len(seen) == 1
    assert 'translations_de: translations(locale: "de")' in seen[0][0]
    assert 'translations_fr: translations(locale: "fr")' in seen[0][0]
    assert matrix["gid://shopify/Product/123"]["de"]["title"]["value"] == "Titel"
    assert matrix["gid://shopify/Product/123"]["fr"]["title"]["outdated"] is True

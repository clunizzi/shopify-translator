import asyncio

from src.shopify import graphql


def test_product_summary_returns_preview_metadata(monkeypatch):
    async def fake_post(query, variables):
        assert "onlineStorePreviewUrl" in query
        assert "images(first: 1)" in query
        assert variables == {"id": "gid://shopify/Product/123"}
        return {
            "data": {
                "product": {
                    "id": "gid://shopify/Product/123",
                    "title": "Motosega",
                    "handle": "motosega",
                    "status": "ACTIVE",
                    "updatedAt": "2026-07-30T10:00:00Z",
                    "vendor": "Stihl",
                    "onlineStoreUrl": "https://shop.example.com/products/motosega",
                    "onlineStorePreviewUrl": "https://example.myshopify.com/products_preview",
                    "images": {
                        "nodes": [
                            {
                                "url": "https://cdn.shopify.com/image.jpg",
                                "altText": "Motosega",
                            }
                        ]
                    },
                    "priceRangeV2": {
                        "minVariantPrice": {
                            "amount": "325.00",
                            "currencyCode": "EUR",
                        },
                        "maxVariantPrice": {
                            "amount": "479.00",
                            "currencyCode": "EUR",
                        },
                    },
                }
            }
        }

    monkeypatch.setattr(graphql, "_post_graphql", fake_post)
    result = asyncio.run(graphql.get_product_summary("gid://shopify/Product/123"))

    assert result["vendor"] == "Stihl"
    assert result["featured_image_url"] == "https://cdn.shopify.com/image.jpg"
    assert result["minimum_price"] == "325.00"
    assert result["maximum_price"] == "479.00"
    assert result["currency_code"] == "EUR"

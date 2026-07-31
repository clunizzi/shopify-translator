import asyncio

from src.bootstrap import theme_tracking

THEME_ID = "123456789012"


class FakeStore:
    def __init__(self, previous_files=None, previous_hashes=None):
        self.previous_files = previous_files or {}
        self.previous_hashes = previous_hashes or {}
        self.sources = []
        self.files = []
        self.events = []

    def ensure_schema(self):
        pass

    def get_theme_file_snapshot(self, **_kwargs):
        return self.previous_files

    def get_theme_source_hashes(self, **kwargs):
        return self.previous_hashes.get((kwargs["resource_type"], kwargs["resource_id"]), {})

    def upsert_theme_source(self, record):
        self.sources.append(record)

    def replace_theme_file_snapshot(self, **kwargs):
        self.files = kwargs["records"]

    def append_theme_change_event(self, **kwargs):
        self.events.append(kwargs)


def _patch_shopify(monkeypatch, *, main_id=THEME_ID, files=None, resources=None):
    async def _main():
        return {
            "id": f"gid://shopify/OnlineStoreTheme/{main_id}",
            "name": "Example Store Live",
            "role": "MAIN",
            "updated_at": "2026-07-29T10:00:00Z",
        }

    async def _files(_theme_id):
        return files or []

    async def _resources(_resource_type):
        return resources or []

    monkeypatch.setattr(theme_tracking, "get_main_theme", _main)
    monkeypatch.setattr(theme_tracking, "get_theme_file_manifest", _files)
    monkeypatch.setattr(theme_tracking, "_fetch_resource_type", _resources)


def test_theme_tracking_is_snapshot_only_and_detects_file_diff(monkeypatch):
    previous = {
        "templates/product.json": {
            "filename": "templates/product.json",
            "checksum_md5": "old",
            "size": 10,
        },
        "assets/removed.css": {
            "filename": "assets/removed.css",
            "checksum_md5": "gone",
            "size": 2,
        },
    }
    files = [
        {
            "filename": "templates/product.json",
            "checksum_md5": "new",
            "content_type": "application/json",
            "size": 11,
            "updated_at": "now",
        },
        {
            "filename": "locales/it.json",
            "checksum_md5": "it",
            "content_type": "application/json",
            "size": 20,
            "updated_at": "now",
        },
    ]
    resources = [
        {
            "resourceId": f"gid://shopify/OnlineStoreThemeLocaleContent/{THEME_ID}",
            "translatableContent": [
                {
                    "key": "products.product.add_to_cart",
                    "value": "Aggiungi",
                    "digest": "d1",
                }
            ],
        }
    ]
    _patch_shopify(monkeypatch, files=files, resources=resources)
    store = FakeStore(previous_files=previous)

    out = asyncio.run(
        theme_tracking.track_main_theme_read_only(
            approved_theme_id=THEME_ID,
            event_id="event-1",
            store=store,
        )
    )

    assert out["status"] == "changed"
    assert out["file_diff"] == {
        "added": ["locales/it.json"],
        "modified": ["templates/product.json"],
        "deleted": ["assets/removed.css"],
    }
    assert store.sources
    translated_source = next(
        record
        for record in store.sources
        if record.resource_type == "ONLINE_STORE_THEME_LOCALE_CONTENT"
    )
    assert translated_source.section_hashes == {"products.product.add_to_cart": "d1"}
    assert store.files
    assert store.events[0]["status"] == "changed"
    assert store.events[0]["details"]["read_only"] is True


def test_theme_tracking_blocks_mismatched_main_without_shopify_writes(monkeypatch):
    _patch_shopify(monkeypatch, main_id="999")
    store = FakeStore()

    out = asyncio.run(
        theme_tracking.track_main_theme_read_only(
            approved_theme_id=THEME_ID,
            event_id="event-2",
            store=store,
        )
    )

    assert out["status"] == "blocked_main_mismatch"
    assert out["approved_theme_id"] == THEME_ID
    assert out["actual_theme_id"] == "999"
    assert out["read_only"] is True


def test_theme_resource_filter_accepts_locale_and_query_style_ids():
    assert theme_tracking._belongs_to_theme(
        f"gid://shopify/OnlineStoreThemeLocaleContent/{THEME_ID}",
        THEME_ID,
    )
    assert theme_tracking._belongs_to_theme(
        f"gid://shopify/OnlineStoreThemeJsonTemplate/123?theme_id={THEME_ID}",
        THEME_ID,
    )
    assert not theme_tracking._belongs_to_theme(
        "gid://shopify/OnlineStoreThemeLocaleContent/999",
        THEME_ID,
    )

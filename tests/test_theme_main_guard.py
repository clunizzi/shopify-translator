import asyncio

import pytest

from src.bootstrap import theme


def test_main_theme_guard_accepts_approved_main(monkeypatch):
    async def _fake_get_main_theme():
        return {
            "id": "gid://shopify/OnlineStoreTheme/123456789012",
            "name": "Example Store Live",
            "role": "MAIN",
            "updated_at": "2026-07-22T09:21:35Z",
        }

    monkeypatch.setattr(theme, "get_main_theme", _fake_get_main_theme)

    result = asyncio.run(theme.assert_approved_main_theme("123456789012"))

    assert result["name"] == "Example Store Live"


def test_main_theme_guard_blocks_stale_theme_id(monkeypatch):
    async def _fake_get_main_theme():
        return {
            "id": "gid://shopify/OnlineStoreTheme/123456789012",
            "name": "Example Store Live",
            "role": "MAIN",
            "updated_at": "2026-07-22T09:21:35Z",
        }

    monkeypatch.setattr(theme, "get_main_theme", _fake_get_main_theme)

    with pytest.raises(theme.ThemeSafetyError, match="191341822334.*123456789012"):
        asyncio.run(theme.assert_approved_main_theme("191341822334"))


def test_bootstrap_blocks_before_opening_local_state(monkeypatch):
    async def _block(_theme_id):
        raise theme.ThemeSafetyError("theme changed")

    def _unexpected_store():
        raise AssertionError("Neon must not be opened before the MAIN guard passes")

    monkeypatch.setattr(theme, "assert_approved_main_theme", _block)
    monkeypatch.setattr(theme, "NeonTranslationStore", _unexpected_store)

    with pytest.raises(theme.ThemeSafetyError, match="theme changed"):
        asyncio.run(
            theme.bootstrap_theme(
                theme_id="191341822334",
                target_locales=["de"],
                source_locale="it",
                apply_translations=True,
                dry_run=False,
            )
        )

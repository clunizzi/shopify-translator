from __future__ import annotations

from src.config.settings import SETTINGS
from src.config.theme_policy_loader import ThemeTranslationPolicy, load_theme_translation_policy


_POLICY_CACHE: ThemeTranslationPolicy | None = None


def get_theme_translation_policy() -> ThemeTranslationPolicy:
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        _POLICY_CACHE = load_theme_translation_policy(SETTINGS.theme_translation_policy_path)
    return _POLICY_CACHE


def should_translate_theme_entry(*, resource_type: str, key: str, value: str) -> bool:
    policy = get_theme_translation_policy()
    rt = (resource_type or "").strip().upper()
    if rt not in policy.allowed_resource_types:
        return False

    key_l = (key or "").strip().lower()
    value_s = value or ""
    if not key_l or not value_s.strip():
        return False

    if any(fragment in key_l for fragment in policy.blocked_key_fragments):
        return False

    if policy.allowed_key_fragments and not any(fragment in key_l for fragment in policy.allowed_key_fragments):
        return False

    for pattern in policy.blocked_value_patterns:
        if pattern.search(value_s):
            return False

    return True

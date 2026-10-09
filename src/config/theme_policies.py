from __future__ import annotations

import re

from src.config.settings import SETTINGS
from src.config.theme_policy_loader import ThemeTranslationPolicy, load_theme_translation_policy

_POLICY_CACHE: ThemeTranslationPolicy | None = None


def get_theme_translation_policy() -> ThemeTranslationPolicy:
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        _POLICY_CACHE = load_theme_translation_policy(SETTINGS.theme_translation_policy_path)
    return _POLICY_CACHE


def is_localizable_theme_url(*, key: str, value: str) -> bool:
    key_path = (key or "").strip().lower().split(":", 1)[0]
    leaf_key = key_path.rsplit(".", 1)[-1]
    value_s = (value or "").strip()
    return (
        value_s.startswith("/")
        and not value_s.startswith("//")
        and ("url" in leaf_key or "link" in leaf_key)
    )


def should_translate_theme_entry(*, resource_type: str, key: str, value: str) -> bool:
    policy = get_theme_translation_policy()
    rt = (resource_type or "").strip().upper()
    if rt not in policy.allowed_resource_types:
        return False

    key_l = (key or "").strip().lower()
    value_s = value or ""
    if not key_l or not value_s.strip():
        return False

    key_path = key_l.split(":", 1)[0]
    leaf_key = key_path.rsplit(".", 1)[-1]

    # Shopify exposes theme-owned locale strings together with thousands of
    # platform-managed checkout/account strings. Only explicitly configured
    # store-owned namespaces are translated automatically.
    is_custom_locale = rt == "ONLINE_STORE_THEME_LOCALE_CONTENT" and any(
        key_path.startswith(prefix) for prefix in policy.allowed_locale_key_prefixes
    )
    if rt == "ONLINE_STORE_THEME_LOCALE_CONTENT" and not is_custom_locale:
        return False

    if is_localizable_theme_url(key=key, value=value_s):
        return True

    allowed = (
        is_custom_locale
        or any(fragment in leaf_key for fragment in policy.allowed_key_fragments)
        or any(fragment in key_l for fragment in policy.allowed_key_fragments)
        or (
            policy.translate_unclassified_text
            and rt
            in {
                "ONLINE_STORE_THEME_JSON_TEMPLATE",
                "ONLINE_STORE_THEME_SECTION_GROUP",
            }
        )
    )
    if policy.allowed_key_fragments and not allowed:
        return False

    if leaf_key in policy.blocked_leaf_names:
        return False

    if any(fragment in leaf_key for fragment in policy.blocked_key_fragments):
        return False

    if any(fragment in key_path for fragment in policy.blocked_key_path_fragments):
        return False

    for pattern in policy.blocked_value_patterns:
        if pattern.search(value_s):
            return False

    # Unclassified JSON settings may also contain numeric thresholds, IDs and
    # timestamps. Requiring a real alphabetic character keeps those out while
    # still accepting normal copy (including HTML and accented characters).
    return bool(re.search(r"[^\W\d_]", value_s, re.UNICODE))

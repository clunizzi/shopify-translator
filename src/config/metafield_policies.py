from __future__ import annotations

from collections.abc import Callable

from src.config.metafield_policy_loader import MetafieldPolicy, load_metafield_policy
from src.config.settings import SETTINGS


BLOCKED_NAMESPACE_PREFIXES = (
    "mm-google",
    "google",
)


def _default_policy() -> MetafieldPolicy:
    return load_metafield_policy(
        getattr(
            SETTINGS,
            "metafield_translation_policy_path",
            "src/config/metafield_translation.yaml",
        )
    )


_POLICY_CACHE: MetafieldPolicy | None = None


def get_metafield_policy() -> MetafieldPolicy:
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        _POLICY_CACHE = _default_policy()
    return _POLICY_CACHE


def _leaf_name(path: tuple) -> str:
    for part in reversed(path):
        if isinstance(part, str):
            return part
    return ""


def should_translate_metafield_leaf(
    namespace: str,
    key: str,
    path: tuple,
    value: str,
) -> bool:
    ns = (namespace or "").strip().lower()
    if any(ns.startswith(prefix) for prefix in BLOCKED_NAMESPACE_PREFIXES):
        return False
    policy = get_metafield_policy()
    leaf_name = _leaf_name(path)
    if leaf_name in policy.blocked_leaf_names:
        return False

    full_key = f"{(namespace or '').strip()}.{(key or '').strip()}"
    allowed = policy.allowed_leafs_by_key.get(full_key)
    if allowed is not None:
        return leaf_name in allowed

    text = (value or "").strip().lower()
    if text.startswith("http://") or text.startswith("https://"):
        return False
    return bool(leaf_name)


def make_metafield_leaf_filter(namespace: str, key: str) -> Callable[[tuple, str], bool]:
    def _allow(path: tuple, value: str) -> bool:
        return should_translate_metafield_leaf(namespace, key, path, value)

    return _allow


def should_translate_metafield(namespace: str, key: str) -> bool:
    ns = (namespace or "").strip().lower()
    if any(ns.startswith(prefix) for prefix in BLOCKED_NAMESPACE_PREFIXES):
        return False
    return bool((namespace or "").strip() and (key or "").strip())

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ThemeTranslationPolicy:
    allowed_resource_types: frozenset[str]
    allowed_locale_key_prefixes: tuple[str, ...]
    allowed_key_fragments: tuple[str, ...]
    translate_unclassified_text: bool
    blocked_leaf_names: frozenset[str]
    blocked_key_fragments: tuple[str, ...]
    blocked_key_path_fragments: tuple[str, ...]
    blocked_value_patterns: tuple[re.Pattern[str], ...]


def load_theme_translation_policy(path: str | Path) -> ThemeTranslationPolicy:
    p = Path(path)
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parents[2]
        candidate = (repo_root / p).resolve()
        if candidate.exists():
            p = candidate
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ThemeTranslationPolicy(
        allowed_resource_types=frozenset(
            str(x).strip() for x in (data.get("allowed_resource_types") or []) if str(x).strip()
        ),
        allowed_locale_key_prefixes=tuple(
            str(x).strip().lower()
            for x in (data.get("allowed_locale_key_prefixes") or [])
            if str(x).strip()
        ),
        allowed_key_fragments=tuple(
            str(x).strip().lower()
            for x in (data.get("allowed_key_fragments") or [])
            if str(x).strip()
        ),
        translate_unclassified_text=bool(data.get("translate_unclassified_text", False)),
        blocked_leaf_names=frozenset(
            str(x).strip().lower() for x in (data.get("blocked_leaf_names") or []) if str(x).strip()
        ),
        blocked_key_fragments=tuple(
            str(x).strip().lower()
            for x in (data.get("blocked_key_fragments") or [])
            if str(x).strip()
        ),
        blocked_key_path_fragments=tuple(
            str(x).strip().lower()
            for x in (data.get("blocked_key_path_fragments") or [])
            if str(x).strip()
        ),
        blocked_value_patterns=tuple(
            re.compile(str(x), re.IGNORECASE | re.DOTALL)
            for x in (data.get("blocked_value_patterns") or [])
            if str(x).strip()
        ),
    )

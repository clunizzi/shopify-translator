from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MetafieldPolicy:
    blocked_leaf_names: frozenset[str]
    allowed_leafs_by_key: dict[str, frozenset[str]]


def load_metafield_policy(path: str | Path) -> MetafieldPolicy:
    p = Path(path)
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parents[2]
        candidate = (repo_root / p).resolve()
        if candidate.exists():
            p = candidate
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    blocked = frozenset(str(x).strip() for x in (data.get("blocked_leaf_names") or []) if str(x).strip())
    policies = data.get("policies") or {}
    allowed: dict[str, frozenset[str]] = {}
    for full_key, cfg in policies.items():
        leafs = cfg.get("allowed_leaf_names") or []
        allowed[str(full_key).strip()] = frozenset(str(x).strip() for x in leafs if str(x).strip())
    return MetafieldPolicy(
        blocked_leaf_names=blocked,
        allowed_leafs_by_key=allowed,
    )

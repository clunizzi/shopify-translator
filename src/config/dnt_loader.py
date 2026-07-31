from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # only for typing; avoid import-time side effects
    from src.translate.translator import DoNotTranslateConfig


def load_do_not_translate(dnt_config_path: str | Path | None) -> DoNotTranslateConfig:
    from src.translate.translator import DoNotTranslateConfig  # local import to avoid cycles

    if not dnt_config_path:
        return DoNotTranslateConfig(brands=[], units=[], tokens=[], glossary={})
    with Path(dnt_config_path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DoNotTranslateConfig(
        brands=list(data.get("brands", []) or []),
        units=list(data.get("units", []) or []),
        tokens=list(data.get("tokens", []) or []),
        glossary={
            str(locale): {
                str(source): (
                    [str(value)]
                    if isinstance(value, str)
                    else [str(item) for item in (value or [])]
                )
                for source, value in (entries or {}).items()
            }
            for locale, entries in (data.get("glossary", {}) or {}).items()
        },
    )

from __future__ import annotations

import langid


def detect_language(text: str) -> tuple[str, float]:
    """Ritorna (lang, conf)."""
    lang, conf = langid.classify(text or "")
    return lang, float(conf)


def detect_per_segment(segments: list[str]) -> list[tuple[str, float]]:
    return [detect_language(s) for s in segments]

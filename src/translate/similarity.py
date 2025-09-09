from __future__ import annotations

import re
from collections.abc import Sequence

# Try to use rapidfuzz if available; otherwise fall back to difflib
try:  # pragma: no cover - import guard
    from rapidfuzz import fuzz as _rf_fuzz

    def _ratio(a: str, b: str) -> float:
        return _rf_fuzz.ratio(a, b) / 100.0

except Exception:  # pragma: no cover - fallback path
    import difflib

    def _ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio()


def normalize_text(s: str, exclude_tokens: Sequence[str] | None = None) -> str:
    s2 = s.strip().lower()
    s2 = re.sub(r"\s+", " ", s2)
    if exclude_tokens:
        # Rimuove token protetti dal confronto
        for tok in exclude_tokens:
            if not tok:
                continue
            pattern = re.escape(tok.strip().lower())
            s2 = re.sub(rf"\b{pattern}\b", "", s2)
        s2 = re.sub(r"\s+", " ", s2).strip()
    return s2


def similarity(a: str, b: str, exclude_tokens: Sequence[str] | None = None) -> float:
    na = normalize_text(a, exclude_tokens)
    nb = normalize_text(b, exclude_tokens)
    if not na and not nb:
        return 1.0
    return _ratio(na, nb)

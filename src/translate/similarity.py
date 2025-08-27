from __future__ import annotations

import re
from collections.abc import Sequence

from rapidfuzz import fuzz


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
    return fuzz.ratio(na, nb) / 100.0

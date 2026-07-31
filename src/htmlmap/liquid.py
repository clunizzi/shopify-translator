from __future__ import annotations

import re

# Ordine importante: RAW prima per inghiottire Liquid interno
RAW_BLOCK = r"\{\%-?\s*raw\s*\-?\%\}.*?\{\%-?\s*endraw\s*\-?\%\}"
OUTPUT = r"\{\{\-?.*?\-?\}\}"
TAG = r"\{\%-?.*?\-?\%\}"


def detect_has_liquid(s: str) -> bool:
    """Rileva rapidamente la presenza di Liquid."""
    if not s or "{" not in s:
        return False
    return (
        re.search(RAW_BLOCK, s, flags=re.DOTALL) is not None
        or re.search(OUTPUT, s, flags=re.DOTALL) is not None
        or re.search(TAG, s, flags=re.DOTALL) is not None
    )


def protect_liquid(html: str) -> tuple[str, dict[int, str]]:
    """
    Sostituisce Liquid con placeholder [[L0]], [[L1]]...
    Ritorna (html_senza_liquid, {idx: contenuto_liquid}).
    """
    mapping: dict[int, str] = {}
    counter = 0

    def make_repl():
        def _repl(m: re.Match) -> str:
            nonlocal counter, mapping  # <-- spostato qui: serve nel frame che fa l'assegnazione
            ph = f"[[L{counter}]]"
            mapping[counter] = m.group(0)
            counter += 1
            return ph

        return _repl

    out = re.sub(RAW_BLOCK, make_repl(), html, flags=re.DOTALL)
    out = re.sub(OUTPUT, make_repl(), out, flags=re.DOTALL)
    out = re.sub(TAG, make_repl(), out, flags=re.DOTALL)
    return out, mapping


def unprotect_liquid(html: str, mapping: dict[int, str]) -> str:
    """Reinieziona [[L#]] → Liquid originale."""
    if not mapping:
        return html
    for i in sorted(mapping.keys()):
        html = html.replace(f"[[L{i}]]", mapping[i])
    return html

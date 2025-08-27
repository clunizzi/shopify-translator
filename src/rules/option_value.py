from __future__ import annotations

import re
from collections.abc import Sequence

# Sinonimi/varianti comuni per "Title" / "Default Title"
OPTION_TITLE_KEYWORDS = {"title", "titolo", "titel", "titre"}
OPTION_VALUE_DEFAULT_TITLE_KEYWORDS = {
    "default title",
    "titolo predefinito",
    "titre par défaut",
    "standardtitel",
}

SIZE_TOKENS = {"xs", "s", "m", "l", "xl", "xxl", "xxxl"}

# Codici tipo IP54, M10, DN25, A2-70...
CODE_RE = re.compile(r"\b([A-Z]{1,3}\d{1,3}(?:[A-Z0-9\-]{0,4}))\b")

# Numeri decimali e frazioni
NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
FRAC_RE = re.compile(r"\b\d+/\d+\b")

MULT_SIGN_RE = re.compile(r"[x×*]")
SEP_RE = re.compile(r"[-/|,]")

TRAILING_COLON_RE = re.compile(r"[:;]+$")


def normalize_option_label(text: str) -> tuple[str, bool]:
    """
    Per OPTION 'name': rimuove ":" o ";" finali e trimma.
    Ritorna (label_normalizzata, aveva_due_punti_finali).
    """
    s = (text or "").strip()
    had = bool(TRAILING_COLON_RE.search(s))
    s = TRAILING_COLON_RE.sub("", s).strip()
    return s, had


def is_only_numbers_units_codes(text: str, units: Sequence[str]) -> tuple[bool, str]:
    """
    True se la stringa è composta solo da numeri/frazioni, unità note, taglie, codici e separatori.
    Ritorna (is_only, reason).
    """
    s = (text or "").strip().lower()
    if not s:
        return True, "empty"

    # Singola lettera (es. taglia abbigliamento)
    if len(s) == 1 and s.isalpha():
        return True, "single_letter"

    tokens = re.findall(r"[a-zA-Z0-9\-°\"']+", s)

    # Taglie pure
    if all(tok in SIZE_TOKENS for tok in tokens):
        return True, "size_only"

    # Unità dinamiche dal config
    units_set = {u.lower() for u in units}
    UNIT_RE = (
        re.compile(
            r"\b("
            + "|".join(re.escape(u) for u in sorted(units_set, key=len, reverse=True))
            + r")\b"
        )
        if units_set
        else None
    )

    # Rimuovi progressivamente componenti consentite e verifica se resta testo alpha
    tmp = s
    tmp = NUM_RE.sub(" ", tmp)
    tmp = FRAC_RE.sub(" ", tmp)
    tmp = CODE_RE.sub(" ", tmp)
    if UNIT_RE:
        tmp = UNIT_RE.sub(" ", tmp)
    tmp = MULT_SIGN_RE.sub(" ", tmp)
    tmp = SEP_RE.sub(" ", tmp)

    if not re.search(r"[a-zA-Z]", tmp):
        if UNIT_RE and UNIT_RE.search(s):
            return True, "units_only"
        if CODE_RE.search(s):
            return True, "code_only"
        return True, "numbers_only"

    return False, ""


def should_skip_option_name(default_content: str) -> tuple[bool, str]:
    """
    Skip per PRODUCT_OPTION (Field=name):
    - label uguale a "Title" (o varianti) -> skip.
    """
    s = (default_content or "").strip().lower()
    if s in OPTION_TITLE_KEYWORDS:
        return True, "option_title_keyword"
    return False, ""


def should_skip_option_value_name(default_content: str, units: Sequence[str]) -> tuple[bool, str]:
    """
    Skip per PRODUCT_OPTION_VALUE (Field=name):
    - "Default Title" (o varianti)
    - solo numeri/unità/taglie/codici/separatori
    """
    s = (default_content or "").strip().lower()
    if s in OPTION_VALUE_DEFAULT_TITLE_KEYWORDS:
        return True, "value_default_title"
    only, reason = is_only_numbers_units_codes(default_content, units)
    if only:
        return True, reason
    return False, ""

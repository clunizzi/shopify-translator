from __future__ import annotations

import re
from dataclasses import dataclass

from slugify import slugify

HANDLE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def make_handle_from_title(title: str, max_len: int = 255) -> str:
    slug = slugify(title, lowercase=True, allow_unicode=False)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_len]


def validate_handle(handle: str, max_len: int = 255) -> bool:
    if not handle or len(handle) > max_len:
        return False
    return bool(HANDLE_RE.fullmatch(handle))


@dataclass
class MetaRules:
    max_title_len: int = 60
    max_desc_len: int = 160


def enforce_meta_title_format(name_model: str, brand: str, benefit: str) -> str:
    # "Nome e modello | Marca | Benefit"
    parts = [p.strip() for p in [name_model, brand, benefit] if p and p.strip()]
    return " | ".join(parts)


def validate_meta_length(s: str, max_len: int) -> tuple[bool, str]:
    """Ritorna (is_within_limit, text_or_truncated). Garantisce len(result) <= max_len."""
    if len(s) <= max_len:
        return True, s
    # Troncamento preferendo confine di parola, altrimenti hard-cut
    cut = s[: max_len + 1]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    else:
        cut = s[:max_len]
    return False, cut

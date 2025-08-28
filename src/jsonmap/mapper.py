from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

HTML_RE = re.compile(r"<[A-Za-z][^>]*>|&[a-zA-Z]+;|</")


def try_parse_json(s: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(s)
    except Exception:
        return False, None


def is_probably_html(text: str) -> bool:
    if not text:
        return False
    return bool(HTML_RE.search(text))


def iter_string_leaves(obj: Any, path: tuple = ()) -> Iterable[tuple[tuple, str, bool]]:
    """
    Itera ricorsivamente e produce tuple: (path_tuple, string_value, may_be_html)
    path_tuple è una sequenza di chiavi/indici per ricostruire.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_string_leaves(v, path + (k,))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            yield from iter_string_leaves(v, path + (idx,))
    elif isinstance(obj, str):
        yield (path, obj, is_probably_html(obj))
    else:
        # non-string leaf: niente
        return


def rebuild_with_replacements(obj: Any, rep: dict[tuple, str]) -> Any:
    """
    Ritorna una nuova struttura con i valori stringa rimpiazzati
    secondo la mappa rep {path_tuple: new_string}.
    """
    if isinstance(obj, dict):
        return {k: rebuild_with_replacements(v, rep) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rebuild_with_replacements(v, rep) for v in obj]
    # foglia
    return rep.get((), obj) if () in rep else obj  # path vuoto per input stringa pura (caso raro)

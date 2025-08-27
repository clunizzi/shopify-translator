from __future__ import annotations

import re

_placeholder_re = re.compile(r"\[\[T(\d+)\]\]")


def reinject_text(html_with_placeholders: str, translations: list[str]) -> str:
    def _repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return translations[idx] if 0 <= idx < len(translations) else ""

    return _placeholder_re.sub(_repl, html_with_placeholders)

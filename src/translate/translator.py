from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from html import escape as escape_html
from typing import Any

import structlog
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import SETTINGS
from src.htmlmap.extract import extract_text_segments
from src.htmlmap.liquid import detect_has_liquid
from src.htmlmap.reinject import reinject_text
from src.translate.cache import TranslationCache
from src.translate.similarity import normalize_text
from src.translate.validators import (
    MetaRules,
    make_handle_from_title,
    validate_handle,
)

logger = structlog.get_logger("translate")

TECH_VALUE_RE = re.compile(
    r"""^
    \s*
    (?:
        (?:https?://\S+)                              # URL
        |
        (?:
            [\d\s.,/×x*+\-–—▶<>≤≥%:]+                # numeri + separatori
            (?:                                       # unità opzionali
                (?:mm|cm|m|km|mm²|cm²|m²|mm3|cm3|m3|mm³|cm³|m³|
                 ml|mL|l|L|kg|g|mg|
                 kW|W|V|A|Ah|Hz|
                 HP|hp|CV|cv|
                 dB(?:\(A\))?|m/s(?:²)?|m³/h|cf/min|min|sec|s|°C|°F|bar|Pa|N·m
                )
            )?
            (?:\s*[\"'″″′’”]|)                        # pollici/apici opzionali
            [\s\d¹²³⁴⁵⁶⁷⁸⁹⁰\.\-–—▶<>≤≥%:\/\(\)\[\]]* # apici/simboli vari
        )
        |
        (?:[XS]L|S|M|L|XL|XXL|XXXL)                   # taglie comuni
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

PREFIX_TECH_SPLIT_RE = re.compile(
    r"^\s*([0-9\s.,/×x*+-]+(?:mm|cm|m|mm³|cm³|m³|kW|W|V|A|Ah|Hz|dB\(A\)|dB|m/s(?:²)?|m³/h|cf/min|min|%|°C|°F)?\s*(?:-|–|—|:)\s*)(.+)$",
    re.IGNORECASE,
)

OPENAI_JSON_ERROR_SENTINEL = "\u241bOPENAI_JSON_ERROR\u241b"


class TranslationError(RuntimeError):
    """A translation could not be produced and must not be published."""


# Handle preservation -NN

HANDLE_SUFFIX_RE = re.compile(r"^(?P<stem>.+?)(?P<suffix>-\d{1,5})$")


def _split_numeric_suffix(handle: str) -> tuple[str, str]:
    """
    Se l'handle termina con '-<numero>' (1..5 cifre), ritorna (stem, suffix), altrimenti (handle, "").
    Esempi: 'molla-14' -> ('molla', '-14'); 'bg-66-ced' -> ('bg-66-ced', '')
    """
    s = (handle or "").strip()
    m = HANDLE_SUFFIX_RE.match(s)
    if m:
        return m.group("stem"), m.group("suffix")
    return s, ""


# --- Helpers logging/snippets -------------------------------------------------


def _snippet_ell(s: object, limit: int = 500) -> str:
    """
    Ritorna una versione stringa di `s` tagliata a `limit` caratteri,
    con ellissi finale se troncata. Tollerante a tipi non stringa.
    """
    try:
        text = str(s or "")
    except Exception:
        text = ""
    return text if len(text) <= limit else (text[:limit] + "…")


# --- JSON parsing helpers -----------------------------------------------------
def _strip_code_fences_and_extract_json(text: str) -> str | None:
    """
    Pulisce blocchi ```json ...``` e prova a estrarre il JSON principale.

    Ritorna una stringa JSON (obj o array) pronta per json.loads, oppure None.
    """
    if not text:
        return None
    s = text.strip()

    # Rimuovi triple backticks (con o senza 'json')
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)

    # Se la risposta ha prefissi tipo 'Output:' o testo extra, prova a prendere
    # dal primo '{' all'ultima '}', altrimenti dal primo '[' all'ultima ']'.
    # 1) prova oggetto
    left = s.find("{")
    right = s.rfind("}")
    if 0 <= left < right:
        candidate = s[left : right + 1].strip()
        # sanity check veloce
        if candidate.count("{") >= 1 and candidate.count("}") >= 1:
            return candidate

    # 2) prova array
    left = s.find("[")
    right = s.rfind("]")
    if 0 <= left < right:
        candidate = s[left : right + 1].strip()
        if candidate.startswith("[") and candidate.endswith("]"):
            return candidate

    # 3) fallback: se sembra già JSON minimale
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        return s

    return None


def is_technical_value(s: str) -> bool:
    """True se stringa è solo numeri/unità/simboli (o URL/size)."""
    return bool(TECH_VALUE_RE.match((s or "").strip()))


def split_prefix_tech_text(s: str) -> tuple[str, str] | None:
    """
    Se stringa ha prefisso tecnico ('20m - test'), separa (prefisso, testo).
    """
    m = PREFIX_TECH_SPLIT_RE.match(s or "")
    if m:
        return m.group(1), m.group(2).strip()
    return None


def try_extract_json(s: str) -> Any | None:
    """Prova a fare json.loads con fallback: estrai da prima { o [ } fino a ultima } o ]."""
    if not s or not s.strip():
        return None
    txt = s.strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    # fallback: ritaglia
    start_brace = txt.find("{")
    start_bracket = txt.find("[")
    start = min([i for i in [start_brace, start_bracket] if i >= 0], default=-1)
    if start < 0:
        return None
    cut = txt[start:]
    # taglia alla chiusura compatibile più a destra
    end_brace = cut.rfind("}")
    end_bracket = cut.rfind("]")
    end = max(end_brace, end_bracket)
    if end >= 0:
        cut = cut[: end + 1]
    try:
        return json.loads(cut)
    except Exception:
        return None


def safe_parse_openai_list(output: str) -> list[str]:
    """
    Accetta sia:
      - oggetto {"translations":[...]}
      - lista nuda [...]
      - output con fence ```json
    Ritorna lista di stringhe o lista vuota.
    """
    s = (output or "").strip()
    if s.lower().startswith("output:"):
        s = s[len("output:") :].strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.startswith("json"):
            s = s[4:].strip()
    # prova oggetto
    try:
        obj = json.loads(s)
        if (
            isinstance(obj, dict)
            and "translations" in obj
            and isinstance(obj["translations"], list)
        ):
            return [str(x) if x is not None else "" for x in obj["translations"]]
        if isinstance(obj, list):
            return [str(x) if x is not None else "" for x in obj]
    except Exception:
        pass
    # estrai prima lista plausibile
    lb = s.find("[")
    rb = s.rfind("]")
    if lb >= 0 and rb > lb:
        try:
            arr = json.loads(s[lb : rb + 1])
            if isinstance(arr, list):
                return [str(x) if x is not None else "" for x in arr]
        except Exception:
            return []
    return []


def detect_lang_fast(text: str) -> tuple[str, float]:
    """
    Heuristica leggera per de/fr/es/en/it basata su token comuni e caratteri accentati.
    Ritorna (lang_code, confidence 0..1). Serve solo per logging/telemetria.
    """
    s = (text or "").lower()
    if not s.strip():
        return ("unknown", 0.0)

    def _count_tokens(words: set[str]) -> int:
        return sum(len(re.findall(rf"\\b{re.escape(w)}\\b", s)) for w in words)

    # Token set minimali
    TOK_DE = {
        "und",
        "mit",
        "für",
        "der",
        "die",
        "das",
        "nicht",
        "auch",
        "oder",
        "ein",
        "eine",
        "zum",
        "zur",
    }
    TOK_FR = {
        "le",
        "la",
        "les",
        "des",
        "un",
        "une",
        "pour",
        "avec",
        "est",
        "sur",
        "et",
        "ou",
        "pas",
        "aux",
        "du",
    }
    TOK_ES = {
        "el",
        "la",
        "los",
        "las",
        "para",
        "con",
        "es",
        "no",
        "y",
        "o",
        "del",
        "al",
        "una",
        "un",
    }
    TOK_EN = {"the", "and", "with", "for", "is", "are", "or", "not", "to", "of", "in", "on"}
    TOK_IT = {
        "il",
        "lo",
        "la",
        "i",
        "gli",
        "le",
        "con",
        "per",
        "è",
        "non",
        "uno",
        "una",
        "degli",
        "delle",
    }

    # Accenti/caratteri speciali
    ACC_DE = "äöüß"
    ACC_FR = "àâæçéèêëîïôœùûüÿ"
    ACC_ES = "áéíóúñ"
    ACC_IT = "àèéìòù"

    scores = {
        "de": _count_tokens(TOK_DE) + sum(1 for c in s if c in ACC_DE),
        "fr": _count_tokens(TOK_FR) + sum(1 for c in s if c in ACC_FR),
        "es": _count_tokens(TOK_ES) + sum(1 for c in s if c in ACC_ES),
        "en": _count_tokens(TOK_EN),
        "it": _count_tokens(TOK_IT) + sum(1 for c in s if c in ACC_IT),
    }
    best_lang = max(scores, key=scores.get)
    total = sum(scores.values()) or 1
    conf = scores[best_lang] / total
    # Se segnale debolissimo, considera unknown
    if scores[best_lang] < 2 and conf < 0.4:
        return ("unknown", conf)
    return (best_lang, conf)


"""
Compat layer for OpenAI SDKs:
- Prefer SDK v1 (`from openai import OpenAI`) if available
- Fallback to older v0-style SDK (`import openai` and use `openai.ChatCompletion.create`)
This avoids `'NoneType' object is not callable'` when v1 class is missing.
"""
try:  # SDK v1
    import openai as _openai  # for typing/usage extraction
    from openai import AsyncOpenAI as _AsyncOpenAI  # type: ignore
    from openai import OpenAI as _OpenAI

    _OPENAI_STYLE = "v1"
except Exception:  # pragma: no cover
    try:  # older v0-style SDK
        import openai as _openai  # type: ignore

        _AsyncOpenAI = None  # type: ignore
        _OpenAI = None  # type: ignore
        _OPENAI_STYLE = "v0"
    except Exception:  # no SDK available
        _openai = None  # type: ignore
        _AsyncOpenAI = None  # type: ignore
        _OpenAI = None  # type: ignore
        _OPENAI_STYLE = "none"


@dataclass
class DoNotTranslateConfig:
    brands: Sequence[str]
    units: Sequence[str]
    tokens: Sequence[str]
    glossary: Mapping[str, Mapping[str, Sequence[str]]] = dataclass_field(default_factory=dict)


def _locale_glossary(
    dnt: DoNotTranslateConfig,
    target_locale: str,
) -> Mapping[str, Sequence[str]]:
    locale = (target_locale or "").strip().replace("_", "-")
    language = locale.split("-", 1)[0].lower()
    glossary = dnt.glossary or {}
    return glossary.get(locale, glossary.get(locale.lower(), glossary.get(language, {})))


def _exact_glossary_translation(
    source: str,
    dnt: DoNotTranslateConfig,
    target_locale: str,
) -> str:
    """Return the canonical target for an exact, whole-segment glossary match."""
    normalized_source = " ".join(str(source or "").split()).casefold()
    if not normalized_source:
        return ""
    for source_term, target_terms in _locale_glossary(dnt, target_locale).items():
        normalized_term = " ".join(str(source_term or "").split()).casefold()
        if normalized_source != normalized_term:
            continue
        for target_term in target_terms:
            canonical = str(target_term or "").strip()
            if canonical:
                return canonical
    return ""


def _applicable_glossary_entries(
    source: str,
    entries: Mapping[str, Sequence[str]],
) -> list[tuple[str, Sequence[str]]]:
    """Prefer specific phrases while retaining separate, non-overlapping terms."""
    matches: list[tuple[int, int, str, Sequence[str]]] = []
    for source_term, required_terms in entries.items():
        pattern = r"(?<!\w)" + re.escape(source_term.strip()) + r"(?!\w)"
        for match in re.finditer(pattern, source or "", flags=re.IGNORECASE):
            matches.append((match.start(), match.end(), source_term, required_terms))

    selected: list[tuple[int, int, str, Sequence[str]]] = []
    for candidate in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0])):
        start, end, _, _ = candidate
        if any(
            start < chosen_end and end > chosen_start for chosen_start, chosen_end, _, _ in selected
        ):
            continue
        selected.append(candidate)
    return [(source_term, required_terms) for _, _, source_term, required_terms in selected]


def translation_output_issue(
    source_text: str,
    translated_text: str,
    *,
    target_locale: str = "",
    dnt: DoNotTranslateConfig | None = None,
) -> str | None:
    """Return why an output must not be trusted, or ``None`` when it is safe."""
    source = str(source_text or "").strip()
    translated = str(translated_text or "").strip()
    if not translated:
        return "blank"

    source.lower()
    translated_lower = translated.lower()
    if "```" in translated and "```" not in source:
        return "unexpected_code_fence"
    if (
        OPENAI_JSON_ERROR_SENTINEL.lower() in translated_lower
        or "openai_json_error" in translated_lower
    ):
        return "error_sentinel"

    try:
        translated_json = json.loads(translated)
    except (TypeError, ValueError, json.JSONDecodeError):
        translated_json = None
    try:
        source_json = json.loads(source)
    except (TypeError, ValueError, json.JSONDecodeError):
        source_json = None
    if isinstance(translated_json, dict | list) and not isinstance(source_json, dict | list):
        return "unexpected_embedded_json"

    if len(translated) > max(len(source) * 4, len(source) + 300):
        return "extreme_length_inflation"

    if dnt is not None:
        translated_folded = translated.casefold()
        for source_term, required_terms in _applicable_glossary_entries(
            source,
            _locale_glossary(dnt, target_locale),
        ):
            accepted = [
                str(term).strip().casefold() for term in required_terms if str(term).strip()
            ]
            if accepted and not any(term in translated_folded for term in accepted):
                return f"glossary_mismatch:{source_term}"
    return None


def json_translation_output_issue(
    source_text: str,
    translated_text: str,
    *,
    target_locale: str,
    dnt: DoNotTranslateConfig,
    should_translate_leaf: Callable[[tuple, str], bool] | None = None,
) -> str | None:
    """Validate JSON leaves without accepting model chatter as content."""
    try:
        source_obj = json.loads(source_text)
        translated_obj = json.loads(translated_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "invalid_json"

    def _walk(source: Any, translated: Any, path: tuple = ()) -> str | None:
        if type(source) is not type(translated):
            return "json_type_changed"
        if isinstance(source, dict):
            if set(source) != set(translated):
                return "json_keys_changed"
            for key in source:
                issue = _walk(source[key], translated[key], path + (key,))
                if issue:
                    return issue
            return None
        if isinstance(source, list):
            if len(source) != len(translated):
                return "json_length_changed"
            for index, (source_item, translated_item) in enumerate(
                zip(source, translated, strict=False)
            ):
                issue = _walk(source_item, translated_item, path + (index,))
                if issue:
                    return issue
            return None
        if isinstance(source, str):
            if not source.strip():
                return None if translated == source else "blank_json_leaf_changed"
            if should_translate_leaf and not should_translate_leaf(path, source):
                return None if translated == source else "blocked_json_leaf_changed"
            if is_technical_value(source.strip()) or source.strip().lower().startswith(
                ("http://", "https://")
            ):
                return None if translated == source else "technical_json_leaf_changed"
            return translation_output_issue(
                source,
                translated,
                target_locale=target_locale,
                dnt=dnt,
            )
        return None

    return _walk(source_obj, translated_obj)


def _glossary_prompt_line(dnt: DoNotTranslateConfig, target_locale: str) -> str:
    entries = _locale_glossary(dnt, target_locale)
    if not entries:
        return ""
    rendered = "; ".join(
        f"{source} → {' / '.join(str(term) for term in targets)}"
        for source, targets in entries.items()
        if targets
    )
    return (
        " Usa questa terminologia di dominio obbligatoria quando il termine sorgente è presente: "
        + rendered
        + ". In caso di corrispondenze sovrapposte, la frase più specifica ha la precedenza."
    )


def _build_system_prompt(
    type_name: str,
    field: str,
    target_locale: str,
    dnt: DoNotTranslateConfig,
    *,
    strict: bool = False,
) -> str:
    """
    Prompt di sistema per PRODUCT / COLLECTION / altri tipi.
    - meta_title: traduzione fedele e concisa (<= 60 char)
    - meta_description: traduzione naturale e informativa (<= 160 char)
    """
    dont = ", ".join(sorted(set(dnt.brands + dnt.units + dnt.tokens)))
    spec = getattr(SETTINGS, "translator_specialization", "")
    brand = getattr(SETTINGS, "translator_brand", "")
    audience = getattr(SETTINGS, "translator_audience", "")
    src_name = getattr(SETTINGS, "source_language_name", "italiano")
    domain_line = (
        f"Sei un traduttore tecnico specializzato in {spec}, conosci i termini specifici del dominio. "
        if spec
        else "Sei un traduttore tecnico accurato. "
    )
    brand_line = (
        f"Lavori sui contenuti di {brand} per un pubblico di {audience}. "
        if brand or audience
        else ""
    )
    base = (
        domain_line
        + brand_line
        + f"Traduci da {src_name} a {target_locale} il contenuto del campo '{field}' "
        + f"per il tipo '{type_name}'. Non tradurre marchi, unità di misura, sigle e i seguenti termini esatti: {dont}. "
        + "Mantieni numeri, codici, compatibilità, modelli, liste e punteggiatura. "
        + "Traduci in modo fedele: non aggiungere frasi, esempi, keyword, brand o informazioni che non sono presenti nel testo originale. "
        + "Non inventare marchi, accessori o dotazioni. Non riscrivere liberamente per fare SEO. "
        + "Niente markdown o spiegazioni; restituisci solo il testo tradotto."
        + _glossary_prompt_line(dnt, target_locale)
    )
    if field == "meta_title":
        base += (
            " Mantieni struttura, significato e keyword presenti nel testo sorgente; "
            "non imporre nuovi separatori e resta conciso (massimo 60 caratteri). "
            "Scrivi un titolo grammaticalmente naturale nella lingua target: non concatenare "
            "keyword e non eliminare preposizioni o congiunzioni necessarie. Se serve accorciare, "
            "rimuovi un dettaglio secondario completo invece di comprimere la grammatica."
        )
    if field == "meta_description":
        base += (
            " Scrivi una descrizione naturale e informativa, senza emoji, senza aggiungere "
            "keyword o dettagli; non allungare artificialmente il testo e resta entro 160 caratteri."
        )
    if field == "title":
        base += (
            " Usa le maiuscole naturali della lingua target: non applicare il Title Case "
            "a ogni parola; conserva maiuscoli solo marchi, sigle e modelli."
        )
    if strict:
        base += " Evita parafrasi inutili: traduci fedelmente, nessuna omissione, nessun contenuto aggiuntivo."
    return base


_META_TRAILING_SEPARATOR_RE = re.compile(r"(?:\||/|[-–—:;,])\s*$")
_META_DANGLING_WORDS = {
    "de": {
        "aber",
        "auf",
        "aus",
        "bei",
        "der",
        "die",
        "das",
        "ein",
        "eine",
        "für",
        "im",
        "in",
        "mit",
        "oder",
        "und",
        "von",
        "zu",
        "zum",
        "zur",
    },
    "fr": {
        "à",
        "avec",
        "de",
        "des",
        "du",
        "en",
        "et",
        "la",
        "le",
        "les",
        "ou",
        "pour",
        "un",
        "une",
    },
}


def meta_shape_issue(field: str, text: str, target_locale: str) -> str | None:
    value = (text or "").strip()
    limit = MetaRules().max_title_len if field == "meta_title" else MetaRules().max_desc_len
    if not value:
        return "blank"
    if len(value) > limit:
        return "too_long"
    if _META_TRAILING_SEPARATOR_RE.search(value):
        return "trailing_separator"
    last_word_match = re.search(r"([\wÀ-ÿ]+)\s*[.!?]?\s*$", value, flags=re.UNICODE)
    last_word = last_word_match.group(1).casefold() if last_word_match else ""
    language = (target_locale or "").split("-", 1)[0].lower()
    if last_word in _META_DANGLING_WORDS.get(language, set()):
        return "dangling_word"
    return None


def _hash_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


PLACEHOLDER_PREFIX_RE = r"^\[[a-z]{2}(?:-[A-Z]{2})?\]\s"
ITALIAN_PRODUCT_TITLE_HINT_RE = re.compile(
    r"(^|[^a-z])(motocoltivatore|motosega|soffiatore|decespugliatore|arieggiatore|tagliasiepi|trattorino|rasaerba|biotrituratore|pompa|fresa|elettric[oa]|scoppio|usato|inclus[oaie])($|[^a-z])",
    re.IGNORECASE,
)


def _html_tag_inventory(html: str) -> dict[str, int]:
    try:
        soup = BeautifulSoup(html or "", "html5lib")
        inv: dict[str, int] = {}
        for tag in soup.find_all(True):
            name = (tag.name or "").lower()
            inv[name] = inv.get(name, 0) + 1
        return inv
    except Exception:
        return {}


def _html_tag_sequence(html: str) -> list[str]:
    try:
        soup = BeautifulSoup(html or "", "html5lib")
        return [(tag.name or "").lower() for tag in soup.find_all(True)]
    except Exception:
        return []


def _html_protected_attr_sequence(html: str) -> list[tuple[str, str, str]]:
    try:
        soup = BeautifulSoup(html or "", "html5lib")
        out: list[tuple[str, str, str]] = []
        for tag in soup.find_all(True):
            name = (tag.name or "").lower()
            for attr_name in ("href", "src"):
                if tag.has_attr(attr_name):
                    out.append((name, attr_name, str(tag.get(attr_name) or "")))
        return out
    except Exception:
        return []


def _is_html_translation_structure_safe(source_html: str, translated_html: str) -> bool:
    src = _html_tag_inventory(source_html)
    dst = _html_tag_inventory(translated_html)
    if not src:
        return bool(translated_html)
    if set(dst.keys()) - set(src.keys()):
        return False
    for tag_name, count in src.items():
        if dst.get(tag_name, 0) != count:
            return False
    if _html_tag_sequence(source_html) != _html_tag_sequence(translated_html):
        return False
    if _html_protected_attr_sequence(source_html) != _html_protected_attr_sequence(translated_html):
        return False
    return True


def _visible_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html or "", "html5lib")
        for tag in soup.find_all(["style", "script", "noscript"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


_PROTECTED_HTML_BLOCK_RE = re.compile(
    r"(<(?:style|script|noscript)\b[^>]*>.*?</(?:style|script|noscript)>)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_protected_html_blocks(html: str) -> tuple[str, list[tuple[str, str]]]:
    text = html or ""
    blocks: list[tuple[str, str]] = []

    def _repl(match: re.Match[str]) -> str:
        token = f"__HTML_BLOCK_{len(blocks)}__"
        blocks.append((token, match.group(1)))
        return token

    stripped = _PROTECTED_HTML_BLOCK_RE.sub(_repl, text)
    return stripped, blocks


def _restore_protected_html_blocks(html: str, blocks: Sequence[tuple[str, str]]) -> str:
    out = html or ""
    for token, original in blocks:
        out = out.replace(token, original)
    return out


def _long_source_fragments_still_present(source_html: str, translated_html: str) -> list[str]:
    translated_text = re.sub(r"\s+", " ", _visible_text(translated_html)).strip()
    if not translated_text:
        return []
    try:
        soup = BeautifulSoup(source_html or "", "html5lib")
        for tag in soup.find_all(["style", "script", "noscript"]):
            tag.decompose()
        fragments = []
        for node in soup.find_all(string=True):
            text = re.sub(r"\s+", " ", str(node)).strip()
            if (
                len(text) < 25
                or " " not in text
                or is_technical_value(text)
                or _looks_like_catalog_model_reference(text)
                or _looks_like_postal_address(text)
            ):
                continue
            fragments.extend(
                part.strip()
                for part in re.split(r"(?<=[.!?])\s+|\n+", text)
                if len(part.strip()) >= 25 and " " in part.strip()
            )
    except Exception:
        source_text = re.sub(r"\s+", " ", _visible_text(source_html)).strip()
        fragments = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+|\n+", source_text)
            if len(part.strip()) >= 25 and " " in part.strip()
        ]
    return [fragment for fragment in fragments if fragment in translated_text]


def _looks_like_catalog_model_reference(value: str) -> bool:
    """Recognize brand/model compatibility rows that must remain unchanged.

    These rows are public technical identifiers, not Italian prose. Requiring
    a spaced dash, a digit in the model side and no Italian descriptive words
    keeps the exemption deliberately narrower than a generic "contains code"
    rule.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) < 25 or len(text) > 180 or re.search(r"[!?]", text):
        return False
    match = re.match(r"^(.+?)\s+[—–-]\s+(.+)$", text)
    if not match:
        return False
    brand, model = match.groups()
    if not re.search(r"\d", model):
        return False
    descriptive_words = {
        "adatto",
        "compatibile",
        "compatibili",
        "con",
        "macchina",
        "modello",
        "modelli",
        "per",
        "ricambio",
        "versione",
    }
    brand_words = {word.lower() for word in re.findall(r"[A-Za-zÀ-ÿ]+", brand)}
    if brand_words & descriptive_words:
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ]", brand) and re.search(r"[A-Za-z0-9]", model))


def _looks_like_postal_address(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not re.search(r"\d", text):
        return False
    if re.search(r"\bSS\s*\d", text, flags=re.IGNORECASE):
        return True
    return bool(
        re.search(
            r"\b(?:via|viale|vicolo|piazza|corso|strada|contrada|località|loc\.?|"
            r"rue|avenue|boulevard|straße|strasse|platz|weg)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _should_reject_plain_cache_hit(
    *,
    type_name: str,
    field: str,
    source_text: str,
    translated_text: str,
    target_locale: str,
) -> bool:
    if type_name != "PRODUCT" or field != "title":
        return False
    source = (source_text or "").strip()
    translated = (translated_text or "").strip()
    if not source or not translated:
        return False
    if normalize_text(source) != normalize_text(translated):
        return False
    if is_technical_value(source):
        return False
    lang, conf = detect_lang_fast(source)
    if (lang != "it" or conf < 0.40) and not ITALIAN_PRODUCT_TITLE_HINT_RE.search(source):
        return False
    logger.warning(
        "reject_plain_cache_hit_same_as_source",
        type_name=type_name,
        field=field,
        target_locale=target_locale,
        source_snippet=_snippet_ell(source, 180),
    )
    return True


class Translator:
    def __init__(
        self,
        cache: TranslationCache,
        model: str | None = None,
        dry_run: bool = False,
        *,
        ignore_cache: bool = False,
        fallback_model: str | None = None,
    ):
        self.cache = cache
        self.model = model or SETTINGS.openai_model
        self.fallback_model = (
            SETTINGS.openai_fallback_model if fallback_model is None else fallback_model
        ).strip()
        if self.fallback_model == self.model:
            self.fallback_model = ""
        self.dry_run = dry_run
        self.ignore_cache = ignore_cache
        self._client = None
        self._async_client = None
        # stats cache (per processo)
        self.cache_hits = 0
        self.cache_misses = 0
        # telemetry (per processo)
        self.openai_calls = 0
        self.openai_ms_total = 0
        self.openai_prompt_tokens = 0
        self.openai_completion_tokens = 0
        self.fallback_calls = 0

    @staticmethod
    def _chat_completion_options(
        *,
        model: str,
        response_format: dict[str, str],
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "model": model,
            "response_format": response_format,
        }
        if model.startswith("gpt-6.1"):
            # GPT-6.1 does not support `none` and reasoning models reject
            # sampling parameters such as temperature. Low is the documented
            # fit for routine transformation/rewrite work.
            options["reasoning_effort"] = "low"
        elif model.startswith("gpt-5.6"):
            # GPT-5.6 defaults to medium reasoning. The translator needs the
            # latency/cost profile of the previous non-reasoning request.
            options["reasoning_effort"] = "none"
        else:
            options["temperature"] = 0
        return options

    def _client_openai(self):
        if self.dry_run:
            return None
        if not SETTINGS.has_openai:
            raise RuntimeError("OPENAI_API_KEY mancante")
        if self._client is None:
            if _OPENAI_STYLE == "v1" and _OpenAI is not None:
                # SDK v1 client
                self._client = _OpenAI()
            elif _OPENAI_STYLE == "v0" and _openai is not None:
                # Legacy SDK v0 uses module-level api_key and ChatCompletion
                _openai.api_key = SETTINGS.openai_api_key
                self._client = _openai
            else:
                raise RuntimeError("OpenAI SDK non disponibile: installa 'openai' nel runtime")
        return self._client

    def _async_client_openai(self):
        if self.dry_run:
            return None
        if not SETTINGS.has_openai:
            raise RuntimeError("OPENAI_API_KEY mancante")
        if self._async_client is None:
            if _OPENAI_STYLE == "v1" and _AsyncOpenAI is not None:
                self._async_client = _AsyncOpenAI()
            else:
                raise RuntimeError("OpenAI Async SDK non disponibile")
        return self._async_client

    async def _call_openai_async(
        self,
        system: str,
        text: str,
        *,
        model: str | None = None,
    ) -> tuple[str, dict]:
        client = self._async_client_openai()
        assert client is not None
        request_model = model or self.model
        t0 = time.perf_counter()
        resp = await client.chat.completions.create(
            **self._chat_completion_options(
                model=request_model,
                response_format={"type": "text"},
            ),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        usage_obj = getattr(resp, "usage", None)
        usage = usage_obj.model_dump() if usage_obj else {}
        duration_ms = int((time.perf_counter() - t0) * 1000)
        self.openai_calls += 1
        self.openai_ms_total += duration_ms
        self.openai_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.openai_completion_tokens += int(usage.get("completion_tokens") or 0)
        return out, {
            "usage": usage,
            "duration_ms": duration_ms,
            "resp_hash": _hash_text(out),
            "model": request_model,
        }

    async def _translate_and_validate_async(
        self,
        *,
        field: str,
        text: str,
        target_locale: str,
        system: str,
        dnt: DoNotTranslateConfig,
    ) -> str:
        primary_attempts = max(1, self._retry_max_for_field(field))
        models = [self.model] * primary_attempts
        if self.fallback_model:
            models.append(self.fallback_model)
        for attempt, request_model in enumerate(models, start=1):
            try:
                translated, _meta = await self._call_openai_async(
                    system,
                    text,
                    model=request_model,
                )
                if request_model == self.fallback_model:
                    self.fallback_calls += 1
            except Exception as exc:
                if attempt < len(models):
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise TranslationError(
                    f"Translation failed for field {field!r} after {len(models)} attempt(s)"
                ) from exc
            translated = (translated or "").strip()
            if not translated:
                if attempt < len(models):
                    continue
                raise TranslationError(f"Translation returned empty output for field {field!r}")
            issue = translation_output_issue(
                text,
                translated,
                target_locale=target_locale,
                dnt=dnt,
            )
            if issue:
                if attempt < len(models):
                    continue
                raise TranslationError(
                    f"Translation returned unsafe output for field {field!r}: {issue}"
                )
            return translated
        raise TranslationError(f"Translation failed for field {field!r}")

    async def translate_seo_field_async(
        self,
        *,
        type_name: str,
        field: str,
        source_text: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
    ) -> str:
        """SEO plain translation for concurrent backfills, without local stale-cache reads."""
        text = (source_text or "").strip()
        if not text:
            return ""
        canonical = _exact_glossary_translation(text, dnt, target_locale)
        if canonical:
            return canonical
        self.cache_misses += 1
        system = _build_system_prompt(type_name, field, target_locale, dnt, strict=False)
        draft = await self._translate_and_validate_async(
            field=field,
            text=text,
            target_locale=target_locale,
            system=system,
            dnt=dnt,
        )
        return await self._fit_meta_translation_async(
            type_name=type_name,
            field=field,
            source_text=text,
            draft=draft,
            target_locale=target_locale,
            dnt=dnt,
        )

    async def _fit_meta_translation_async(
        self,
        *,
        type_name: str,
        field: str,
        source_text: str,
        draft: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
    ) -> str:
        result = (draft or "").strip()
        issue = meta_shape_issue(field, result, target_locale) or translation_output_issue(
            source_text,
            result,
            target_locale=target_locale,
            dnt=dnt,
        )
        if issue is None:
            return result
        limit = MetaRules().max_title_len if field == "meta_title" else MetaRules().max_desc_len
        base_system = (
            _build_system_prompt(type_name, field, target_locale, dnt, strict=True)
            + " Riscrivi la bozza come testo naturale e completo. "
            + "Non tagliare e non creare catene di keyword. Elimina dettagli secondari completi."
        )
        budgets = [50, 42, 34] if field == "meta_title" else [130, 105, 80]
        for budget in budgets:
            system = (
                base_system
                + f" LIMITE ASSOLUTO PER QUESTO TENTATIVO: {budget} caratteri, spazi inclusi. "
                + "Questo limite prevale su qualunque altro numero nel prompt."
            )
            payload = json.dumps(
                {
                    "source": source_text,
                    "draft": result,
                    "max_characters": budget,
                },
                ensure_ascii=False,
            )
            result, _meta = await self._call_openai_async(system, payload)
            result = (result or "").strip()
            issue = meta_shape_issue(field, result, target_locale) or translation_output_issue(
                source_text,
                result,
                target_locale=target_locale,
                dnt=dnt,
            )
            if issue is None:
                return result
        raise TranslationError(
            f"Unable to produce a complete {field} within {limit} characters: {issue}"
        )

    def _cache_key(self, type_name: str, field: str, locale: str, text_norm: str) -> str:
        return self.cache.make_key(
            type_name, field, locale, text_norm, SETTINGS.rules_version, self.model
        )

    # --- Cell-level cache helpers -------------------------------------------
    def _cell_key_plain(
        self,
        type_name: str,
        field: str,
        locale: str,
        text: str,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        sig = normalize_text(text or "", exclude_tokens=exclude_similarity_tokens)
        payload = f"cell|plain|{type_name}|{field}|{locale}|{sig}|{SETTINGS.rules_version}|{self.model}|{SETTINGS.cache_algo_version}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _cell_key_html(
        self,
        type_name: str,
        field: str,
        locale: str,
        html: str,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        """
        Robust HTML cell-cache key: hash the Liquid-protected HTML instead of normalized segments.
        This avoids collisions across different HTMLs that normalize to similar text.
        """
        try:
            from src.htmlmap.liquid import detect_has_liquid, protect_liquid

            html_in = html or ""
            html_prot = protect_liquid(html_in)[0] if detect_has_liquid(html_in) else html_in
        except Exception:
            html_prot = html or ""
        sha = hashlib.sha256(html_prot.encode("utf-8", errors="ignore")).hexdigest()
        payload = f"cell|html|{type_name}|{field}|{locale}|{SETTINGS.rules_version}|{self.model}|{SETTINGS.cache_algo_version}|sha256={sha}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _cell_key_json(
        self,
        type_name: str,
        field: str,
        locale: str,
        raw: str,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        """
        Robust JSON cell-cache key: hash a canonical JSON string when possible.
        Falls back to hashing the raw string if parsing fails.
        """
        try:
            obj = try_extract_json(raw)
            if obj is None:
                canon = raw or ""
            else:
                canon = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            canon = raw or ""
        sha = hashlib.sha256(canon.encode("utf-8", errors="ignore")).hexdigest()
        payload = f"cell|json|{type_name}|{field}|{locale}|{SETTINGS.rules_version}|{self.model}|{SETTINGS.cache_algo_version}|sha256={sha}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    @retry(
        reraise=True,
        stop=stop_after_attempt(SETTINGS.retry_max),
        wait=wait_exponential(min=1, max=8),
    )
    def _call_openai(
        self,
        system: str,
        text: str,
        *,
        model: str | None = None,
    ) -> tuple[str, dict]:
        client = self._client_openai()
        assert client is not None
        request_model = model or self.model
        if SETTINGS.log_payloads:
            logger.info(
                "api_request",
                api="openai",
                model=request_model,
                req_hash=_hash_text(text),
                system_hash=_hash_text(system),
                snippet_req=text[: SETTINGS.log_payload_max],
            )
        t0 = time.perf_counter()
        # SDK v1 vs older v0-style SDK
        if hasattr(client, "chat") and hasattr(client.chat, "completions"):
            resp = client.chat.completions.create(
                **self._chat_completion_options(
                    model=request_model,
                    response_format={"type": "text"},
                ),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            )
            out = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None) and resp.usage.model_dump() or {}
        else:
            # Legacy v0 path
            resp = client.ChatCompletion.create(  # type: ignore[attr-defined]
                model=request_model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            )
            choice0 = (resp.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            out = (msg.get("content") or "").strip()
            usage = resp.get("usage") or {}
        dt = int((time.perf_counter() - t0) * 1000)
        # Telemetry counters
        try:
            self.openai_calls += 1
            self.openai_ms_total += dt
            pt = 0
            ct = 0
            if isinstance(usage, dict):
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
            self.openai_prompt_tokens += pt
            self.openai_completion_tokens += ct
        except Exception:
            pass
        meta = {
            "usage": usage,
            "duration_ms": dt,
            "resp_hash": _hash_text(out),
            "model": request_model,
        }
        if SETTINGS.log_payloads:
            logger.info(
                "api_response",
                api="openai",
                status=200,
                duration_ms=dt,
                usage=meta["usage"],
                resp_chars=len(out),
                resp_hash=meta["resp_hash"],
                snippet_resp=out[: SETTINGS.log_payload_max],
            )
        return out, meta

    @retry(
        reraise=True,
        stop=stop_after_attempt(SETTINGS.retry_max),
        wait=wait_exponential(min=1, max=8),
    )
    def _call_openai_json(
        self,
        system: str,
        payload: dict,
        *,
        model: str | None = None,
    ) -> tuple[dict, dict]:
        """
        Chiede un JSON (response_format=json_object). Ritorna (obj, meta).
        """
        client = self._client_openai()
        assert client is not None
        request_model = model or self.model
        user_content = json.dumps(payload, ensure_ascii=False)
        if SETTINGS.log_payloads:
            logger.info(
                "api_request",
                api="openai",
                model=request_model,
                req_hash=_hash_text(user_content),
                system_hash=_hash_text(system),
                snippet_req=user_content[: SETTINGS.log_payload_max],
            )
        t0 = time.perf_counter()
        if hasattr(client, "chat") and hasattr(client.chat, "completions"):
            resp = client.chat.completions.create(
                **self._chat_completion_options(
                    model=request_model,
                    response_format={"type": "json_object"},
                ),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None) and resp.usage.model_dump() or {}
        else:
            # Legacy v0: no response_format; ask JSON via prompt and parse
            resp = client.ChatCompletion.create(  # type: ignore[attr-defined]
                model=request_model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            )
            choice0 = (resp.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            text = (msg.get("content") or "").strip()
            usage = resp.get("usage") or {}
        dt = int((time.perf_counter() - t0) * 1000)
        # Telemetry counters
        try:
            self.openai_calls += 1
            self.openai_ms_total += dt
            pt = 0
            ct = 0
            if isinstance(usage, dict):
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
            self.openai_prompt_tokens += pt
            self.openai_completion_tokens += ct
        except Exception:
            pass
        try:
            obj = json.loads(text)
        except Exception as e:
            logger.warning("openai_json_parse_error", error=str(e))
            raise
        meta = {
            "usage": usage,
            "duration_ms": dt,
            "resp_hash": _hash_text(text),
            "model": request_model,
        }
        if SETTINGS.log_payloads:
            logger.info(
                "api_response",
                api="openai",
                status=200,
                duration_ms=dt,
                usage=meta["usage"],
                resp_chars=len(text),
                resp_hash=meta["resp_hash"],
                snippet_resp=text[: SETTINGS.log_payload_max],
            )
        return obj, meta

    def _retry_max_for_field(self, field: str) -> int:
        if field == "option_name":
            return max(1, SETTINGS.retry_max_option)
        if field == "option_value_name":
            return max(1, SETTINGS.retry_max_value)
        return max(1, SETTINGS.retry_max)

    # --- dentro class Translator ---

    def _unpack_openai_resp(self, resp: object) -> tuple[str, dict]:
        """
        Ritorna (text, meta) da una risposta OpenAI che può essere:
        - stringa semplice
        - (stringa, meta_dict)
        Qualsiasi altra forma viene ridotta a (str(resp), {}).
        """
        try:
            if isinstance(resp, tuple) and len(resp) == 2:
                text = resp[0] or ""
                meta = resp[1] or {}
                return str(text), (meta if isinstance(meta, dict) else {})
            return (str(resp or ""), {})
        except Exception:
            return (str(resp), {})

    def get_telemetry(self) -> dict:
        """Raccoglie counters utili per telemetria/log finale."""
        return {
            "cache_hits": int(getattr(self, "cache_hits", 0)),
            "cache_misses": int(getattr(self, "cache_misses", 0)),
            "openai_calls": int(getattr(self, "openai_calls", 0)),
            "openai_ms_total": int(getattr(self, "openai_ms_total", 0)),
            "openai_prompt_tokens": int(getattr(self, "openai_prompt_tokens", 0)),
            "openai_completion_tokens": int(getattr(self, "openai_completion_tokens", 0)),
            "fallback_calls": int(getattr(self, "fallback_calls", 0)),
            "model": self.model,
            "fallback_model": self.fallback_model,
        }

    def _translate_and_validate(
        self,
        field: str,
        text: str,
        target_locale: str,
        exclude_similarity_tokens: Sequence[str],
        system: str,
        *,
        strict: bool = False,
        dnt: DoNotTranslateConfig | None = None,
    ) -> str:
        # dry-run fast path
        if self.dry_run:
            out = f"[{target_locale}] {text}"
            try:
                lang, _ = detect_lang_fast(out)  # best effort
            except Exception:
                lang = "unknown"
            logger.info(
                "translate",
                field=field,
                attempt=1,
                lang_out=lang,
                decision="accept_dry_run",
                model=self.model,
                duration_ms=0,
                usage={},
            )
            return out

        primary_attempts = max(1, self._retry_max_for_field(field))
        models = [self.model] * primary_attempts
        if self.fallback_model:
            models.append(self.fallback_model)

        for attempt, request_model in enumerate(models, start=1):
            t0 = time.monotonic()
            try:
                resp = self._call_openai(system, text, model=request_model)
                if request_model == self.fallback_model:
                    self.fallback_calls += 1
                out_text, meta = self._unpack_openai_resp(resp)
            except Exception as e:
                decision = "retry_error" if attempt < len(models) else "reject_error"
                logger.warning(
                    "translate",
                    field=field,
                    attempt=attempt,
                    lang_out="unknown",
                    decision=decision,
                    reason=str(e),
                    model=request_model,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    usage={},
                )
                if attempt < len(models):
                    continue
                raise TranslationError(
                    f"Translation failed for field {field!r} after {len(models)} attempt(s)"
                ) from e

            translated = (out_text or "").strip()
            duration_ms = int((time.monotonic() - t0) * 1000)
            usage = meta.get("usage", {}) if isinstance(meta, dict) else {}
            if not translated:
                logger.warning(
                    "translate",
                    field=field,
                    attempt=attempt,
                    decision="retry_empty" if attempt < len(models) else "reject_empty",
                    model=request_model,
                    duration_ms=duration_ms,
                    usage=usage,
                )
                if attempt < len(models):
                    continue
                raise TranslationError(f"Translation returned empty output for field {field!r}")
            output_issue = translation_output_issue(
                text,
                translated,
                target_locale=target_locale,
                dnt=dnt,
            )
            if output_issue:
                logger.warning(
                    "translate",
                    field=field,
                    attempt=attempt,
                    decision="retry_unsafe" if attempt < len(models) else "reject_unsafe",
                    reason=output_issue,
                    model=request_model,
                    duration_ms=duration_ms,
                    usage=usage,
                )
                if attempt < len(models):
                    continue
                raise TranslationError(
                    f"Translation returned unsafe output for field {field!r}: {output_issue}"
                )

            # Log minimale (niente similarità/lingua)
            logger.info(
                "translate",
                field=field,
                attempt=attempt,
                decision="accept",
                model=request_model,
                duration_ms=duration_ms,
                usage=usage,
            )
            return translated

        raise TranslationError(f"Translation failed for field {field!r}")

    def _should_skip_similarity(
        self, default_text: str, exclude_similarity_tokens: Sequence[str]
    ) -> bool:
        return normalize_text(default_text, exclude_tokens=exclude_similarity_tokens) == ""

    # -------------------------------
    # Cache alias per campi equivalenti (lookup) + translate_plain unico
    # -------------------------------
    def _alias_fields_for_lookup(self, field: str) -> list[str]:
        f = field.strip()
        if f == "value":
            return ["value", "option_name", "title"]
        if f == "option_value_name":
            return ["option_value_name", "value"]
        if f == "option_name":
            return ["option_name", "title", "value"]
        if f == "title":
            return ["title", "option_name", "value"]
        return [f]  # niente alias per body_html/meta/handle/product_type

    def _cache_get_with_alias(self, type_name: str, field: str, locale: str, text_norm: str) -> str:
        for f in self._alias_fields_for_lookup(field):
            key = self.cache.make_key(
                type_name, f, locale, text_norm, SETTINGS.rules_version, self.model
            )
            entry = self.cache.get(key)
            translated = (entry.get("translated") or "").strip() if entry else ""
            if translated:
                if f != field:
                    logger.info("cache_hit_alias", from_field=f, to_field=field)
                self.cache_hits += 1
                return translated
        return ""

    def translate_plain(
        self,
        type_name: str,
        field: str,
        default_content: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        """Traduzione testo plain con cell-cache first e lookup cache alias; scrive solo sulla chiave primaria."""
        text = (default_content or "").strip()
        if not text:
            return ""

        canonical = _exact_glossary_translation(text, dnt, target_locale)
        if canonical:
            logger.info(
                "glossary_exact_plain",
                field=field,
                target_locale=target_locale,
            )
            return canonical

        # 0) Cell-level cache (prima di tutto)
        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_plain(
                    type_name, field, target_locale, text, exclude_similarity_tokens
                )
                found = self.cache.get_cell(cell_key)
                if found and isinstance(found.get("value"), str):
                    output_issue = translation_output_issue(
                        text,
                        found["value"],
                        target_locale=target_locale,
                        dnt=dnt,
                    )
                    if not output_issue and not _should_reject_plain_cache_hit(
                        type_name=type_name,
                        field=field,
                        source_text=text,
                        translated_text=found["value"],
                        target_locale=target_locale,
                    ):
                        self.cache_hits += 1
                        logger.info("cell_cache_hit_plain_any", field=field)
                        return found["value"]
                    if output_issue:
                        logger.warning(
                            "cell_cache_rejected_plain",
                            field=field,
                            reason=output_issue,
                        )
        except Exception:
            pass

        text_norm = normalize_text(text)

        # 1) cache read-through con alias
        cached = self._cache_get_with_alias(type_name, field, target_locale, text_norm)
        if cached:
            output_issue = translation_output_issue(
                text,
                cached,
                target_locale=target_locale,
                dnt=dnt,
            )
            if output_issue or _should_reject_plain_cache_hit(
                type_name=type_name,
                field=field,
                source_text=text,
                translated_text=cached,
                target_locale=target_locale,
            ):
                cached = ""
        if cached:
            # consolida anche in cell-cache per coerenza futura
            try:
                if not self.ignore_cache:
                    cell_key = self._cell_key_plain(
                        type_name, field, target_locale, text, exclude_similarity_tokens
                    )
                    self.cache.set_cell(cell_key, cached, self.model, meta={})
            except Exception:
                pass
            return cached

        # 2) OpenAI
        # conto come cache miss (nessun hit in cell-cache/alias)
        try:
            self.cache_misses += 1
        except Exception:
            pass
        system = _build_system_prompt(type_name, field, target_locale, dnt, strict=False)
        translated = self._translate_and_validate(
            field,
            text,
            target_locale,
            exclude_similarity_tokens,
            system,
            dnt=dnt,
        )

        # 3) scrittura cache solo su chiave primaria + cell-level
        key = self._cache_key(type_name, field, target_locale, text_norm)
        self.cache.set(key, {"translated": translated or ""}, model=self.model)
        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_plain(
                    type_name, field, target_locale, text, exclude_similarity_tokens
                )
                self.cache.set_cell(cell_key, translated or "", self.model, meta={})
        except Exception:
            pass
        return translated

    def translate_html_document(
        self,
        type_name: str,
        field: str,
        html: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        html_in = html or ""
        if not html_in.strip():
            return ""
        html_for_translation, protected_blocks = _strip_protected_html_blocks(html_in)
        if not html_for_translation.strip():
            return html_in

        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_html(
                    type_name, field, target_locale, html_in, exclude_similarity_tokens
                )
                found = self.cache.get_cell(cell_key)
                if found and isinstance(found.get("value"), str):
                    cached_value = found["value"]
                    untranslated_fragments = _long_source_fragments_still_present(
                        html_in, cached_value
                    )
                    output_issue = translation_output_issue(
                        _visible_text(html_in),
                        _visible_text(cached_value),
                        target_locale=target_locale,
                        dnt=dnt,
                    )
                    if not untranslated_fragments and not output_issue:
                        self.cache_hits += 1
                        logger.info(
                            "cell_cache_hit_html_document", type_name=type_name, field=field
                        )
                        return cached_value
                    logger.warning(
                        "cell_cache_rejected_html_document",
                        type_name=type_name,
                        field=field,
                        reason="untranslated_source_fragment",
                        fragment_count=len(untranslated_fragments),
                        output_issue=output_issue,
                    )
        except Exception:
            pass

        try:
            self.cache_misses += 1
        except Exception:
            pass

        dont = ", ".join(sorted(set(dnt.brands + dnt.units + dnt.tokens)))
        system = (
            _build_system_prompt(type_name, field, target_locale, dnt, strict=True)
            + " Riceverai un documento HTML completo. "
            + "Traduci solo il testo visibile e mantieni intatta la struttura HTML esistente. "
            + "Usa esclusivamente i tag già presenti nell'input: non aggiungere nuovi tag, non rimuovere tag esistenti e non cambiare il numero dei tag. "
            + "Non aggiungere heading nuovi come h1, h2, h3, né paragrafi, liste, tabelle o line break non presenti. "
            + "Se trovi una lista o una sezione di compatibilità tra modelli/macchine/ricambi, mantienila rigorosamente invariata nella struttura, nell'ordine e nei riferimenti tecnici; non trasformarla in testo discorsivo. "
            + f"Non tradurre marchi, unità, codici, SKU, modelli e questi termini esatti: {dont}. "
            + "Restituisci solo HTML valido."
        )

        if self.dry_run:
            return html_in

        translated, _meta = self._unpack_openai_resp(
            self._call_openai(system, html_for_translation)
        )
        out_html = (translated or "").strip()
        if not out_html:
            raise TranslationError(f"HTML translation returned empty output for field {field!r}")
        if not _is_html_translation_structure_safe(html_for_translation, out_html):
            logger.warning(
                "html_document_validation_fallback",
                field=field,
                reason="tag_structure_changed",
            )
            out_html = self._translate_html_text_nodes(
                type_name=type_name,
                field=field,
                html=html_for_translation,
                target_locale=target_locale,
                dnt=dnt,
                exclude_similarity_tokens=exclude_similarity_tokens,
            )
            if not _is_html_translation_structure_safe(
                html_for_translation,
                out_html,
            ):
                raise TranslationError(
                    f"HTML text-node fallback changed tag structure for field {field!r}"
                )
        output_issue = translation_output_issue(
            _visible_text(html_for_translation),
            _visible_text(out_html),
            target_locale=target_locale,
            dnt=dnt,
        )
        if output_issue:
            logger.warning("html_document_validation_failed", field=field, reason=output_issue)
            raise TranslationError(
                f"HTML translation returned unsafe output for field {field!r}: {output_issue}"
            )
        untranslated_fragments = _long_source_fragments_still_present(
            html_for_translation, out_html
        )
        if untranslated_fragments:
            logger.warning(
                "html_document_validation_retry",
                field=field,
                reason="untranslated_source_fragment",
                fragment_count=len(untranslated_fragments),
            )
            retry_system = (
                system
                + " Alcune frasi sono rimaste nella lingua sorgente. "
                + "Ritraduci l'intero documento assicurandoti che tutto il testo visibile discorsivo sia nella lingua target."
            )
            translated_retry, _meta = self._unpack_openai_resp(
                self._call_openai(retry_system, html_for_translation)
            )
            retry_html = (translated_retry or "").strip()
            if not retry_html:
                raise TranslationError(
                    f"HTML translation retry returned empty output for field {field!r}"
                )
            if not _is_html_translation_structure_safe(html_for_translation, retry_html):
                logger.warning(
                    "html_document_validation_failed",
                    field=field,
                    reason="retry_tag_structure_changed",
                )
                raise TranslationError(
                    f"HTML translation retry changed tag structure for field {field!r}"
                )
            retry_issue = translation_output_issue(
                _visible_text(html_for_translation),
                _visible_text(retry_html),
                target_locale=target_locale,
                dnt=dnt,
            )
            if retry_issue:
                raise TranslationError(
                    f"HTML translation retry returned unsafe output for field {field!r}: {retry_issue}"
                )
            retry_untranslated = _long_source_fragments_still_present(
                html_for_translation, retry_html
            )
            if retry_untranslated:
                logger.warning(
                    "html_document_validation_failed",
                    field=field,
                    reason="untranslated_source_fragment",
                    fragment_count=len(retry_untranslated),
                )
                raise TranslationError(
                    f"HTML translation left {len(retry_untranslated)} untranslated source fragment(s)"
                )
            out_html = retry_html
        out_html = _restore_protected_html_blocks(out_html, protected_blocks)

        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_html(
                    type_name, field, target_locale, html_in, exclude_similarity_tokens
                )
                self.cache.set_cell(cell_key, out_html, self.model, meta={"mode": "full_document"})
        except Exception:
            pass
        return out_html

    def _translate_html_text_nodes(
        self,
        *,
        type_name: str,
        field: str,
        html: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        mapped_html, segments = extract_text_segments(html)
        translated_segments = list(segments)

        for index, segment in enumerate(segments):
            match = re.match(r"^(\s*)(.*?)(\s*)$", segment, flags=re.DOTALL)
            if not match:
                continue
            prefix, core, suffix = match.groups()
            if (
                not core
                or not any(character.isalpha() for character in core)
                or re.fullmatch(r"__HTML_BLOCK_\d+__", core)
            ):
                continue
            if is_technical_value(core):
                value = core
            else:
                value = self.translate_plain(
                    type_name,
                    "value",
                    core,
                    target_locale,
                    dnt,
                    exclude_similarity_tokens,
                ).strip()
            if not value:
                raise TranslationError(
                    f"HTML text-node fallback returned a blank segment for field {field!r}"
                )
            if ("<" in value or ">" in value) and not ("<" in core or ">" in core):
                raise TranslationError(
                    f"HTML text-node fallback returned markup for field {field!r}"
                )
            translated_segments[index] = prefix + escape_html(value, quote=False) + suffix

        return reinject_text(mapped_html, translated_segments)

    def translate_field(
        self,
        type_name: str,
        field: str,
        default_content: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
        title_translated: str | None = None,
        preserve_handle: bool = False,
    ) -> str:

        field = field.strip()
        text = default_content or ""

        if field == "body_html":
            return self.translate_html_document(
                type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
            )

        if detect_has_liquid(text):
            return self.translate_html_document(
                type_name, field, text, target_locale, dnt, exclude_similarity_tokens
            )

        if field == "handle":
            # recupera suffisso numerico dall'handle originale (Default content)
            _, orig_suffix = _split_numeric_suffix(default_content)

            if preserve_handle:
                # tenta traduzione del handle così com’è
                result = self.translate_plain(
                    type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
                )
                ok = validate_handle(result)
                if not ok:
                    base = title_translated or default_content
                    result = make_handle_from_title(base)
                # se c'è un suffisso numerico originale e non è già presente, ri-applicalo
                if orig_suffix and not (result or "").endswith(orig_suffix):
                    result = f"{result}{orig_suffix}"
                return result

            # Non preserviamo: generiamo dallo slug del TITLE tradotto (o default come fallback)
            if title_translated is not None:
                if title_translated == "":
                    return ""  # reject: niente handle senza titolo valido
                base = title_translated
            else:
                base = default_content

            result = make_handle_from_title(base)
            if orig_suffix:
                result = f"{result}{orig_suffix}"
            return result

        if field in {"meta_title", "meta_description", "title", "product_type"}:
            # Cell-level cache per campi plain
            try:
                if not self.ignore_cache:
                    cell_key = self._cell_key_plain(
                        type_name,
                        field,
                        target_locale,
                        default_content or "",
                        exclude_similarity_tokens,
                    )
                    found = self.cache.get_cell(cell_key)
                    if found and isinstance(found.get("value"), str):
                        self.cache_hits += 1
                        logger.info("cell_cache_hit_plain", field=field)
                        if field in {"meta_title", "meta_description"}:
                            return self._fit_meta_translation(
                                type_name=type_name,
                                field=field,
                                source_text=default_content,
                                draft=found["value"],
                                target_locale=target_locale,
                                dnt=dnt,
                            )
                        return found["value"]
            except Exception:
                pass
            result = self.translate_plain(
                type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
            )
            if result == "":
                return ""  # reject già deciso
            if field in {"meta_title", "meta_description"}:
                result = self._fit_meta_translation(
                    type_name=type_name,
                    field=field,
                    source_text=default_content,
                    draft=result,
                    target_locale=target_locale,
                    dnt=dnt,
                )
                text_norm = normalize_text(default_content or "")
                key = self._cache_key(type_name, field, target_locale, text_norm)
                self.cache.set(key, {"translated": result}, model=self.model)
            # Salva cell-level cache
            try:
                if not self.ignore_cache:
                    cell_key = self._cell_key_plain(
                        type_name,
                        field,
                        target_locale,
                        default_content or "",
                        exclude_similarity_tokens,
                    )
                    self.cache.set_cell(cell_key, result, self.model, meta={})
            except Exception:
                pass
            return result

        return self.translate_plain(
            type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
        )

    def _fit_meta_translation(
        self,
        *,
        type_name: str,
        field: str,
        source_text: str,
        draft: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
    ) -> str:
        """Riscrive una meta troppo lunga o mozzata; non effettua mai hard-cut."""
        result = (draft or "").strip()
        issue = meta_shape_issue(field, result, target_locale) or translation_output_issue(
            source_text,
            result,
            target_locale=target_locale,
            dnt=dnt,
        )
        if issue is None:
            return result
        if self.dry_run:
            return result

        limit = MetaRules().max_title_len if field == "meta_title" else MetaRules().max_desc_len
        base_system = (
            _build_system_prompt(type_name, field, target_locale, dnt, strict=True)
            + " La bozza ricevuta non rispetta il limite o termina a metà frase. "
            + "Non tagliare parole o frasi, non terminare con separatori, articoli, congiunzioni o preposizioni. "
            + "Mantieni una grammatica naturale: non concatenare keyword e non eliminare connettivi necessari. "
            + "Se serve, comprimi eliminando dettagli secondari completi, senza aggiungere informazioni. "
            + "Restituisci esclusivamente la versione finale."
        )
        budgets = [50, 42, 34] if field == "meta_title" else [130, 105, 80]
        for budget in budgets:
            system = (
                base_system
                + f" LIMITE ASSOLUTO PER QUESTO TENTATIVO: {budget} caratteri, spazi inclusi. "
                + "Questo limite prevale su qualunque altro numero nel prompt."
            )
            payload = json.dumps(
                {
                    "source": source_text,
                    "draft": result,
                    "max_characters": budget,
                },
                ensure_ascii=False,
            )
            rewritten, _meta = self._unpack_openai_resp(self._call_openai(system, payload))
            result = (rewritten or "").strip()
            issue = meta_shape_issue(field, result, target_locale) or translation_output_issue(
                source_text,
                result,
                target_locale=target_locale,
                dnt=dnt,
            )
            if issue is None:
                return result
        raise TranslationError(
            f"Unable to produce a complete {field} within {limit} characters: {issue}"
        )

    # -------------------------------
    # JSON translation for METAFIELD
    # -------------------------------
    def translate_json_value(
        self,
        type_name: str,
        field_logical: str,  # "value" | "meta_title" | "meta_description"
        default_content: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
        *,
        batch_size: int = 8,
        should_translate_leaf: Callable[[tuple, str], bool] | None = None,
    ) -> str:
        """
        Traduce solo i VALORI (leaf string) del JSON, saltando numeri/unità/URL.
        Fail-closed: in errore solleva TranslationError per impedire payload vuoti o sorgente.
        """
        logger.info("json_detect", type_name=type_name, field=field_logical)

        # Cell-level cache (intero JSON string tradotto): early-return se presente
        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_json(
                    type_name,
                    field_logical,
                    target_locale,
                    default_content or "",
                    exclude_similarity_tokens,
                )
                found = self.cache.get_cell(cell_key)
                if found and isinstance(found.get("value"), str):
                    output_issue = json_translation_output_issue(
                        default_content,
                        found["value"],
                        target_locale=target_locale,
                        dnt=dnt,
                        should_translate_leaf=should_translate_leaf,
                    )
                    if not output_issue:
                        self.cache_hits += 1
                        logger.info("cell_cache_hit_json", field=field_logical)
                        return found["value"]
                    logger.warning(
                        "cell_cache_rejected_json",
                        field=field_logical,
                        reason=output_issue,
                    )
        except Exception:
            pass

        # 0) Parse JSON; se fallisce → tratta come plain
        obj = try_extract_json(default_content)
        if obj is None:
            # Plain string: applica stesse regole di skip tecnico/prefisso e traduci come testo
            s = (default_content or "").strip()
            if not s:
                return ""
            if is_technical_value(s):
                logger.info("json_skip_tech", reason="technical_plain")
                return s
            pref = split_prefix_tech_text(s)
            if pref:
                prefix, text = pref
            else:
                prefix, text = ("", s)

            translated = self.translate_plain(
                type_name,
                field_logical if field_logical in {"meta_title", "meta_description"} else "value",
                text,
                target_locale,
                dnt=dnt,
                exclude_similarity_tokens=exclude_similarity_tokens,
            )
            return (prefix + translated) if translated else ""

        # 1) Flatten: raccogli tutte le leaf string
        paths: list[tuple] = []
        leaves: list[str] = []

        def _walk(x: Any, path: tuple):
            if isinstance(x, dict):
                for k, v in x.items():
                    _walk(v, path + (k,))
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    _walk(v, path + (i,))
            elif isinstance(x, str):
                leaves.append(x)
                paths.append(path)
            else:
                # tipi non string non si traducono
                return

        _walk(obj, ())

        if not leaves:
            # niente da tradurre
            return json.dumps(obj, ensure_ascii=False)

        # 2) Cache & skip tecnico per leaf
        cached_out: dict[int, str] = {}
        todo_texts: list[str] = []
        todo_idxs: list[list[int]] = []
        todo_position_by_text: dict[str, int] = {}
        skip_count = 0
        policy_skip_count = 0

        dont = list(sorted(set(dnt.brands + dnt.units + dnt.tokens)))

        for i, seg in enumerate(leaves):
            s = (seg or "").strip()
            path = paths[i]
            if should_translate_leaf and not should_translate_leaf(path, s):
                cached_out[i] = s
                policy_skip_count += 1
                continue
            if (
                not s
                or is_technical_value(s)
                or s.lower().startswith("http://")
                or s.lower().startswith("https://")
            ):
                cached_out[i] = s
                skip_count += 1
                continue

            # gestisci prefisso tecnico (es. "20m - ")
            prefix = ""
            sp = split_prefix_tech_text(s)
            if sp:
                prefix, s = sp

            canonical = _exact_glossary_translation(s, dnt, target_locale)
            if canonical:
                cached_out[i] = prefix + canonical
                logger.info(
                    "glossary_exact_json",
                    field=field_logical,
                    target_locale=target_locale,
                )
                continue

            text_norm = normalize_text(s)
            key = self._cache_key(type_name, field_logical, target_locale, text_norm)
            entry = self.cache.get(key)
            translated = (entry.get("translated") or "").strip() if entry else ""
            if translated:
                output_issue = translation_output_issue(
                    s,
                    translated,
                    target_locale=target_locale,
                    dnt=dnt,
                )
                if output_issue:
                    logger.warning(
                        "cache_rejected_json",
                        field=field_logical,
                        reason=output_issue,
                    )
                    translated = ""
            if translated:
                cached_out[i] = prefix + translated
                self.cache_hits += 1
                logger.info("cache_hit_json", field=field_logical)
            else:
                todo_position = todo_position_by_text.get(s)
                if todo_position is None:
                    todo_position = len(todo_texts)
                    todo_position_by_text[s] = todo_position
                    todo_texts.append(s)
                    todo_idxs.append([])
                todo_idxs[todo_position].append(i)
                # memorizza prefix per reiniezione post-traduzione
                cached_out[i] = prefix  # temporaneamente solo prefisso

        if policy_skip_count or skip_count:
            logger.info(
                "json_segments_filtered",
                field=field_logical,
                policy_skipped=policy_skip_count,
                technical_or_url_skipped=skip_count,
            )

        if todo_texts:
            self.cache_misses += len(todo_texts)
            logger.info("cache_miss_json", field=field_logical, segments=len(todo_texts))

        # 3) Batch OpenAI sui miss
        fresh_map: dict[int, str] = {}
        try:
            if todo_texts and not self.dry_run:
                system = (
                    _build_system_prompt(type_name, field_logical, target_locale, dnt, strict=True)
                    + " Traduci SOLO i valori testuali (non le chiavi). "
                    'Rispondi con un JSON valido: o un oggetto {"translations":[...]} '
                    "oppure direttamente una lista [...]. Ordine invariato."
                )
                for b in range(0, len(todo_texts), batch_size):
                    batch = todo_texts[b : b + batch_size]
                    user_payload = json.dumps(
                        {"values": batch, "do_not_translate": dont},
                        ensure_ascii=False,
                    )
                    resp = self._call_openai(system, user_payload)
                    resp_text = resp[0] if isinstance(resp, tuple) else resp
                    arr = safe_parse_openai_list(resp_text)
                    if not arr:
                        logger.error(
                            "openai_json_error",
                            error="empty_or_parse_fail",
                            field=field_logical,
                            sample=resp_text[:300],
                        )
                        raise TranslationError(
                            f"JSON translation returned invalid output for field {field_logical!r}"
                        )
                    # riallinea lunghezze
                    if len(arr) < len(batch):
                        arr += [""] * (len(batch) - len(arr))
                    elif len(arr) > len(batch):
                        arr = arr[: len(batch)]
                    if any(not str(item or "").strip() for item in arr):
                        raise TranslationError(
                            f"JSON translation returned empty segment(s) for field {field_logical!r}"
                        )
                    for source_item, translated_item in zip(batch, arr, strict=False):
                        output_issue = translation_output_issue(
                            source_item,
                            str(translated_item or ""),
                            target_locale=target_locale,
                            dnt=dnt,
                        )
                        if output_issue:
                            raise TranslationError(
                                "JSON translation returned unsafe segment "
                                f"for field {field_logical!r}: {output_issue}"
                            )

                    # copia nelle posizioni originali
                    for j, t in enumerate(arr):
                        indexes = todo_idxs[b + j]
                        for idx in indexes:
                            full = (cached_out[idx] or "") + (t or "")
                            fresh_map[idx] = full
                        # aggiorna cache singolo leaf (senza prefisso tecnico)
                        text_norm = normalize_text(batch[j])
                        key = self._cache_key(type_name, field_logical, target_locale, text_norm)
                        self.cache.set(key, {"translated": t or ""}, model=self.model)
                logger.info("openai_call_json", field=field_logical, segments=len(todo_texts))
            elif todo_texts and self.dry_run:
                # DRY-RUN: eco con marker
                for j, s in enumerate(todo_texts):
                    for idx in todo_idxs[j]:
                        fresh_map[idx] = (cached_out[idx] or "") + f"[{target_locale}] {s}"
        except TranslationError:
            raise
        except Exception as e:
            logger.error("openai_json_error", error=str(e), field=field_logical)
            raise TranslationError(f"JSON translation failed for field {field_logical!r}") from e

        # 4) Ricostruzione oggetto
        def _set_in(obj_ref: Any, path: tuple, value: Any):
            cur = obj_ref
            for k in path[:-1]:
                cur = cur[k]
            cur[path[-1]] = value

        out_obj = json.loads(json.dumps(obj))  # shallow copy via json
        for i, path in enumerate(paths):
            if i in fresh_map:
                _set_in(out_obj, path, fresh_map[i])
            elif i in cached_out:
                _set_in(out_obj, path, cached_out[i])
            else:
                _set_in(out_obj, path, leaves[i])

        # Nessun log di similarità/lingua

        out_str = json.dumps(out_obj, ensure_ascii=False)
        # Salva cell-level cache
        try:
            if not self.ignore_cache:
                meta = {"leaves": len(leaves)} if "leaves" in locals() else {}
                cell_key = self._cell_key_json(
                    type_name,
                    field_logical,
                    target_locale,
                    default_content or "",
                    exclude_similarity_tokens,
                )
                self.cache.set_cell(cell_key, out_str, self.model, meta=meta)
        except Exception:
            pass
        return out_str

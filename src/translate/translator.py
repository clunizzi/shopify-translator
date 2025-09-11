from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential
from bs4 import BeautifulSoup

from src.config.settings import SETTINGS
from src.htmlmap.extract import extract_text_segments
from src.htmlmap.liquid import detect_has_liquid, protect_liquid, unprotect_liquid
from src.htmlmap.reinject import reinject_text
from src.translate.cache import TranslationCache
from src.translate.similarity import normalize_text, similarity
from src.translate.validators import (
    MetaRules,
    enforce_meta_title_format,
    make_handle_from_title,
    validate_handle,
    validate_meta_length,
)

logger = structlog.get_logger("translate")

TECH_VALUE_RE = re.compile(
    r"""^
    \s*
    (?:
        (?:https?://\S+)                              # URL
        |
        (?:
            [\d\s.,/×x*+-]+                           # numeri + separatori
            (?:                                       # unità opzionali
                (?:mm|cm|m|km|mm²|cm²|m²|mm3|cm3|m3|mm³|cm³|m³|
                 ml|mL|l|L|kg|g|mg|
                 kW|W|V|A|Ah|Hz|
                 HP|hp|CV|cv|
                 dB(?:\(A\))?|m/s(?:²)?|m³/h|cf/min|min|sec|s|°C|°F|bar|Pa|N·m
                )
            )?
            (?:\s*[\"'″″′’”]|)                        # pollici/apici opzionali
            [\s\d¹²³⁴⁵⁶⁷⁸⁹⁰\.\-\/\(\)\[\]]*          # apici/simboli vari
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

OPENAI_JSON_ERROR_SENTINEL = "\u241BOPENAI_JSON_ERROR\u241B"

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


# --- Minor post-processing helpers ------------------------------------------
_MD_EM_RE = re.compile(r"(\*{1,2}[^*]+?\*{1,2}|_{1,2}[^_]+?_{1,2})")

def _fix_inline_markdown_spacing(s: str) -> str:
    """
    Inserisce spazi attorno a blocchi stile markdown (*bold*, **bold**, _em_, __strong__)
    se adiacenti a caratteri non-spazio, per evitare incollaggi tipo 'parola*bold*parola'.
    Non altera il contenuto interno ai marker.
    """
    if not s:
        return s
    # spazio prima
    s = re.sub(r"([^\s])(\*{1,2}[^*]+?\*{1,2})", r"\1 \2", s)
    s = re.sub(r"([^\s](_{1,2}[^_]+?_{1,2}))", r" \1", s)
    # spazio dopo
    s = re.sub(r"(\*{1,2}[^*]+?\*{1,2})([^\s])", r"\1 \2", s)
    s = re.sub(r"(_{1,2}[^_]+?_{1,2})([^\s])", r"\1 \2", s)
    return s


# --- JSON parsing helpers -----------------------------------------------------

def _safe_json_loads(text: str) -> dict:
    """
    Tenta di parse-are la risposta OpenAI in un dict con chiave 'translations'.
    Accetta:
      - oggetto {"translations":[...]}
      - lista nuda [...]
      - output con ```json ... ``` o testo extra
    Ritorna sempre un dict {"translations": list[str]} anche se vuota.
    """
    s = (text or "").strip()

    # 1) Prova parse diretto
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and isinstance(obj.get("translations"), list):
            return {"translations": [str(x or "") for x in obj["translations"]]}
        if isinstance(obj, list):
            return {"translations": [str(x or "") for x in obj]}
    except Exception:
        pass

    # 2) Prova a ripulire code fences / estrarre il core JSON
    candidate = _strip_code_fences_and_extract_json(s)
    if candidate:
        try:
            obj2 = json.loads(candidate)
            if isinstance(obj2, dict) and isinstance(obj2.get("translations"), list):
                return {"translations": [str(x or "") for x in obj2["translations"]]}
            if isinstance(obj2, list):
                return {"translations": [str(x or "") for x in obj2]}
        except Exception:
            pass

    # 3) Fallback: usa il parser permissivo a lista
    arr = safe_parse_openai_list(s)
    if arr:
        return {"translations": [str(x or "") for x in arr]}

    # 4) Estremo fallback: nessuna traduzione estratta
    return {"translations": []}


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
    l = s.find("{")
    r = s.rfind("}")
    if 0 <= l < r:
        candidate = s[l : r + 1].strip()
        # sanity check veloce
        if candidate.count("{") >= 1 and candidate.count("}") >= 1:
            return candidate

    # 2) prova array
    l = s.find("[")
    r = s.rfind("]")
    if 0 <= l < r:
        candidate = s[l : r + 1].strip()
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
- Fallback to legacy v0 (`import openai` and use `openai.ChatCompletion.create`)
This avoids `'NoneType' object is not callable'` when v1 class is missing.
"""
try:  # SDK v1
    from openai import OpenAI as _OpenAI  # type: ignore
    import openai as _openai  # for typing/usage extraction
    _OPENAI_STYLE = "v1"
except Exception:  # pragma: no cover
    try:  # Legacy v0
        import openai as _openai  # type: ignore
        _OpenAI = None  # type: ignore
        _OPENAI_STYLE = "v0"
    except Exception:  # no SDK available
        _openai = None  # type: ignore
        _OpenAI = None  # type: ignore
        _OPENAI_STYLE = "none"


@dataclass
class DoNotTranslateConfig:
    brands: Sequence[str]
    units: Sequence[str]
    tokens: Sequence[str]


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
    - PRODUCT.meta_title: vincolo 'Nome e modello | Marca | Benefit' (<= 60 char)
    - COLLECTION.meta_title: solo concisione (<= 60 char)
    - meta_description: 150–160 char, senza emoji/markdown
    """
    dont = ", ".join(sorted(set(dnt.brands + dnt.units + dnt.tokens)))
    spec = getattr(SETTINGS, "translator_specialization", "")
    src_name = getattr(SETTINGS, "source_language_name", "italiano")
    domain_line = (
        f"Sei un traduttore tecnico specializzato in {spec}, conosci i termini specifici del dominio. "
        if spec else "Sei un traduttore tecnico accurato. "
    )
    base = (
        domain_line
        + f"Traduci da {src_name} a {target_locale} il contenuto del campo '{field}' "
        + f"per il tipo '{type_name}'. Non tradurre marchi, unità di misura, sigle e i seguenti termini esatti: {dont}. "
        + "Mantieni numeri, codici e punteggiatura. Niente markdown o spiegazioni; restituisci solo il testo tradotto."
    )
    if field == "meta_title":
        if type_name == "PRODUCT":
            base += " Rispetta il formato: 'Nome e modello | Marca | Benefit' e mantieni conciso (≤ 60 caratteri)."
        else:
            base += " Mantieni il titolo conciso e descrittivo (≤ 60 caratteri)."
    if field == "meta_description":
        base += (
            " Scrivi una descrizione naturale tra 150 e 160 caratteri, informativa, senza emoji."
        )
    if strict:
        base += " Evita parafrasi inutili: traduci fedelmente, nessuna omissione."
    return base


def _hash_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


PLACEHOLDER_PREFIX_RE = r"^\[[a-z]{2}(?:-[A-Z]{2})?\]\s"


class Translator:
    def __init__(
        self,
        cache: TranslationCache,
        model: str | None = None,
        dry_run: bool = False,
        *,
        ignore_cache: bool = False,
    ):
        self.cache = cache
        self.model = model or SETTINGS.openai_model
        self.dry_run = dry_run
        self.ignore_cache = ignore_cache
        self._client = None
        # stats cache (per processo)
        self.cache_hits = 0
        self.cache_misses = 0

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

    def _cache_key(self, type_name: str, field: str, locale: str, text_norm: str) -> str:
        return self.cache.make_key(
            type_name, field, locale, text_norm, SETTINGS.rules_version, self.model
        )

    # --- Cell-level cache helpers -------------------------------------------
    def _cell_key_plain(self, type_name: str, field: str, locale: str, text: str, exclude_similarity_tokens: Sequence[str]) -> str:
        sig = normalize_text(text or "", exclude_tokens=exclude_similarity_tokens)
        payload = f"cell|plain|{type_name}|{field}|{locale}|{sig}|{SETTINGS.rules_version}|{self.model}|{SETTINGS.cache_algo_version}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _cell_key_html(self, type_name: str, field: str, locale: str, html: str, exclude_similarity_tokens: Sequence[str]) -> str:
        # Proteggi Liquid solo per la firma, poi estrai segmenti testuali e normalizza
        try:
            from src.htmlmap.liquid import detect_has_liquid, protect_liquid
            from src.htmlmap.extract import extract_text_segments
            html_in = html or ""
            html_prot = protect_liquid(html_in)[0] if detect_has_liquid(html_in) else html_in
            _, segs = extract_text_segments(html_prot)
        except Exception:
            segs = [html or ""]
        norm = [normalize_text(s or "", exclude_tokens=exclude_similarity_tokens) for s in segs]
        sig = "||".join(norm)
        payload = f"cell|html|{type_name}|{field}|{locale}|{sig}|{SETTINGS.rules_version}|{self.model}|{SETTINGS.cache_algo_version}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _cell_key_json(self, type_name: str, field: str, locale: str, raw: str, exclude_similarity_tokens: Sequence[str]) -> str:
        # Parse tollerante e raccogli solo leaf string normalizzate in ordine
        try:
            obj = try_extract_json(raw)
        except Exception:
            obj = None
        leaves: list[str] = []
        def _walk(o):
            from typing import Any
            if isinstance(o, dict):
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
            elif isinstance(o, str):
                s = o or ""
                # Applica le stesse regole “skip tecnico” impiegate a runtime
                if is_technical_value(s):
                    return
                p = split_prefix_tech_text(s)
                if p:
                    _, s2 = p
                    s = s2
                leaves.append(s)
        if obj is None:
            leaves = [raw or ""]
        else:
            _walk(obj)
        norm = [normalize_text(s or "", exclude_tokens=exclude_similarity_tokens) for s in leaves]
        sig = "||".join(norm)
        payload = f"cell|json|{type_name}|{field}|{locale}|{sig}|{SETTINGS.rules_version}|{self.model}|{SETTINGS.cache_algo_version}"
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    @retry(
        reraise=True,
        stop=stop_after_attempt(SETTINGS.retry_max),
        wait=wait_exponential(min=1, max=8),
    )
    def _call_openai(self, system: str, text: str) -> tuple[str, dict]:
        client = self._client_openai()
        assert client is not None
        if SETTINGS.log_payloads:
            logger.info(
                "api_request",
                api="openai",
                model=self.model,
                req_hash=_hash_text(text),
                system_hash=_hash_text(system),
                snippet_req=text[: SETTINGS.log_payload_max],
            )
        t0 = time.perf_counter()
        # SDK v1 vs legacy v0
        if hasattr(client, "chat") and hasattr(client.chat, "completions"):
            resp = client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "text"},
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
                model=self.model,
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
        meta = {
            "usage": usage,
            "duration_ms": dt,
            "resp_hash": _hash_text(out),
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
    def _call_openai_json(self, system: str, payload: dict) -> tuple[dict, dict]:
        """
        Chiede un JSON (response_format=json_object). Ritorna (obj, meta).
        """
        client = self._client_openai()
        assert client is not None
        user_content = json.dumps(payload, ensure_ascii=False)
        if SETTINGS.log_payloads:
            logger.info(
                "api_request",
                api="openai",
                model=self.model,
                req_hash=_hash_text(user_content),
                system_hash=_hash_text(system),
                snippet_req=user_content[: SETTINGS.log_payload_max],
            )
        t0 = time.perf_counter()
        if hasattr(client, "chat") and hasattr(client.chat, "completions"):
            resp = client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
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
                model=self.model,
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
        try:
            obj = json.loads(text)
        except Exception as e:
            logger.warning("openai_json_parse_error", error=str(e))
            raise
        meta = {
            "usage": usage,
            "duration_ms": dt,
            "resp_hash": _hash_text(text),
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

    def _threshold_for_field(self, field: str) -> float:
        if field == "body_html":
            return SETTINGS.sim_t_html
        if field in {"meta_title", "meta_description"}:
            return SETTINGS.sim_t_meta
        if field == "product_type":
            return SETTINGS.sim_t_product_type
        if field == "title":
            return SETTINGS.sim_t_title
        if field == "handle":
            return SETTINGS.sim_t_handle
        if field == "option_name":
            return SETTINGS.sim_t_option
        if field == "option_value_name":
            return SETTINGS.sim_t_value
        return SETTINGS.sim_t_title

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
                text = (resp[0] or "")
                meta = resp[1] or {}
                return str(text), (meta if isinstance(meta, dict) else {})
            return (str(resp or ""), {})
        except Exception:
            return (str(resp), {})

    def _translate_and_validate(
        self,
        field: str,
        text: str,
        target_locale: str,
        exclude_similarity_tokens: Sequence[str],
        system: str,
        *,
        strict: bool = False,
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
                sim=0.0,
                threshold=self._threshold_for_field(field),
                lang_out=lang,
                decision="accept_dry_run",
                reason="",
                model=self.model,
                duration_ms=0,
                usage={},
            )
            return out

        threshold = self._threshold_for_field(field)
        max_attempts = max(1, self._retry_max_for_field(field))

        last_out: str = text
        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._call_openai(system, text)
                out_text, meta = self._unpack_openai_resp(resp)
            except Exception as e:
                decision = "retry_error" if attempt < max_attempts else ("reject_error" if strict else "accept_error")
                logger.warning(
                    "translate",
                    field=field,
                    attempt=attempt,
                    sim=None,
                    threshold=threshold,
                    lang_out="unknown",
                    decision=decision,
                    reason=str(e),
                    model=self.model,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    usage={},
                )
                if attempt < max_attempts:
                    continue
                return "" if strict else last_out

            last_out = (out_text or "").strip()
            duration_ms = int((time.monotonic() - t0) * 1000)
            usage = meta.get("usage", {}) if isinstance(meta, dict) else {}

            # lingua + similarità
            try:
                lang, conf = detect_lang_fast(last_out)
            except Exception:
                lang, conf = ("unknown", 0.0)

            try:
                sim = similarity(text, last_out, exclude_tokens=exclude_similarity_tokens)
            except TypeError:
                from difflib import SequenceMatcher
                sim = SequenceMatcher(None, (text or "").lower(), (last_out or "").lower()).ratio()

            suspicious = (lang.split("-")[0] == "it") or (sim >= threshold and (target_locale[:2] != lang[:2]))

            if suspicious and attempt < max_attempts:
                logger.info(
                    "translate",
                    field=field,
                    attempt=attempt,
                    sim=sim,
                    threshold=threshold,
                    lang_out=lang,
                    decision="retry",
                    reason="lang_it_or_high_similarity",
                    model=self.model,
                    duration_ms=duration_ms,
                    usage=usage,
                )
                continue

            decision = "accept_suspect" if suspicious else "accept"
            reason = "lang_it_or_high_similarity" if suspicious else ""
            logger.info(
                "translate",
                field=field,
                attempt=attempt,
                sim=sim,
                threshold=threshold,
                lang_out=lang,
                decision=("reject" if (suspicious and strict) else decision),
                reason=reason,
                model=self.model,
                duration_ms=duration_ms,
                usage=usage,
            )
            if suspicious and strict:
                return ""
            return last_out

        return "" if strict else last_out


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
        """Traduzione testo plain con lookup cache alias; scrive solo sulla chiave primaria."""
        text = (default_content or "").strip()
        if not text:
            return ""

        text_norm = normalize_text(text)

        # 1) cache read-through con alias
        cached = self._cache_get_with_alias(type_name, field, target_locale, text_norm)
        if cached:
            return cached

        # 2) OpenAI
        system = _build_system_prompt(type_name, field, target_locale, dnt, strict=False)
        translated = self._translate_and_validate(
            field, text, target_locale, exclude_similarity_tokens, system
        )

        # 3) scrittura cache solo su chiave primaria
        key = self._cache_key(type_name, field, target_locale, text_norm)
        self.cache.set(key, {"translated": translated or ""}, model=self.model)
        return translated

    def translate_html(
        self,
        type_name: str,
        field: str,
        html: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
        *,
        strict: bool = False,
    ) -> str:
        """
        Traduce HTML:
        - Protegge Liquid ({{ }}, {% %}, {% raw %}...{% endraw %})
        - Estrae segmenti testuali -> [[T#]]
        - Cache per segmento
        - Una call OpenAI per i miss (JSON {"translations":[...]})
        - Parsing JSON tollerante; non ferma la pipeline
        - Re-inietta testi e poi Liquid
        - Similarità/lang: solo log
        """
        html_in = html or ""

        # Cell-level cache (intero HTML pronto): early-return se presente
        try:
            if not self.ignore_cache:
                # per la firma usa exclude_similarity_tokens come nel resto della pipeline
                dnt = dnt  # no-op per chiarezza tipo
                cell_key = self._cell_key_html(type_name, field, target_locale, html_in, exclude_similarity_tokens)
                found = self.cache.get_cell(cell_key)
                if found and isinstance(found.get("value"), str):
                    self.cache_hits += 1
                    logger.info("cell_cache_hit_html", type_name=type_name, field=field)
                    return found["value"]
        except Exception:
            pass

        # 0) Protezione Liquid
        liquid_map: dict[int, str] = {}
        if detect_has_liquid(html_in):
            html_prot, liquid_map = protect_liquid(html_in)
            logger.info("liquid_protected", count=len(liquid_map))
        else:
            html_prot = html_in

        # 1) Estrazione segmenti testuali
        html_map, segments = extract_text_segments(html_prot)

        # 2) Cache per segmenti
        cached_out: dict[int, str] = {}
        todo: list[str] = []
        idxs: list[int] = []
        for i, seg in enumerate(segments):
            text_norm = normalize_text(seg)
            key = self._cache_key(type_name, field, target_locale, text_norm)
            entry = self.cache.get(key)
            translated = (entry.get("translated") or "").strip() if entry else ""
            if translated:
                cached_out[i] = translated
            else:
                todo.append(seg)
                idxs.append(i)

        hits = len(cached_out)
        misses = len(todo)
        if hits:
            self.cache_hits += hits
            logger.info("cache_hit_html", type_name=type_name, field=field, segments=hits)
        if misses:
            self.cache_misses += misses
            logger.info("cache_miss_html", type_name=type_name, field=field, segments=misses)

        # 3) Traduzione miss: per blocchi (default) o unica batch per segmenti (legacy)
        fresh_map: dict[int, str] = {}
        mode = getattr(SETTINGS, "html_translate_mode", "block").strip().lower()
        if misses > 0 and not self.dry_run:
            dont = list(sorted(set(dnt.brands + dnt.units + dnt.tokens)))
            if mode == "block":
                # Costruisci gruppi di indici per blocco (p, li, h1..h6, blockquote, figcaption, td, th, dt, dd)
                try:
                    soup2 = BeautifulSoup(html_map or "", "html5lib")
                    root2 = soup2.body if soup2.body else soup2
                    BLOCK_TAGS = {"p","li","h1","h2","h3","h4","h5","h6","blockquote","figcaption","td","th","dt","dd"}
                    # raccogli gruppi in ordine
                    groups: list[list[int]] = []
                    seen = set()
                    import re as _re
                    ph_re = _re.compile(r"\[\[T(\d+)\]\]")
                    for el in root2.find_all(BLOCK_TAGS):
                        inner = el.decode_contents()
                        idxs_in = [int(m.group(1)) for m in ph_re.finditer(inner)]
                        if idxs_in:
                            groups.append(idxs_in)
                            for _i in idxs_in:
                                seen.add(_i)
                    # aggiungi eventuali indici non coperti da blocchi
                    all_idxs = list(range(len(segments)))
                    for i in all_idxs:
                        if i not in seen:
                            groups.append([i])
                except Exception:
                    groups = [idxs]  # fallback: tutti i miss insieme

                # Per ogni gruppo, chiedi traduzione in ordine (lista), tenendo conto dei cache hit
                for g in groups:
                    # Costruisci la lista completa dei testi del gruppo (non solo miss) per dare contesto
                    values = [segments[i] for i in g]
                    system = (
                        _build_system_prompt(type_name, field, target_locale, dnt, strict=strict)
                        + " Considera l'intero elenco come un paragrafo unico; traduci ogni elemento usando il contesto degli altri."
                        + " Restituisci SOLO un oggetto JSON con chiave 'translations' (lista di stringhe) nello stesso ordine dell'elenco fornito."
                    )
                    user_payload = json.dumps(
                        {"target_locale": target_locale, "field": field, "do_not_translate": dont, "segments": values},
                        ensure_ascii=False,
                    )
                    try:
                        resp = self._call_openai(system=system, text=user_payload)
                        resp_text = resp[0] if isinstance(resp, tuple) else resp
                        obj = _safe_json_loads(resp_text)
                        arr = obj.get("translations")
                        if not isinstance(arr, list):
                            raise ValueError("missing 'translations' list")
                        # riallinea lunghezze
                        if len(arr) < len(values):
                            arr += [""] * (len(values) - len(arr))
                        elif len(arr) > len(values):
                            arr = arr[: len(values)]
                    except Exception as e:
                        logger.error(
                            "openai_json_error",
                            error=str(e),
                            sample=_snippet_ell(resp_text if 'resp_text' in locals() else "", 500),
                            field=field,
                        )
                        arr = values  # no-op fallback
                    # Applica a tutte le posizioni del gruppo e aggiorna cache segmenti
                    for j, out_txt in enumerate(arr):
                        idx = g[j]
                        fresh_map[idx] = str(out_txt) if out_txt is not None else ""
                        key = self._cache_key(type_name, field, target_locale, normalize_text(segments[idx]))
                        self.cache.set(key, {"translated": fresh_map[idx]}, model=self.model)
                logger.info("openai_call_html", type_name=type_name, field=field, segments=misses, mode="block")
            else:
                # Legacy: unica batch per tutti i miss
                payload = {
                    "target_locale": target_locale,
                    "field": field,
                    "do_not_translate": dont,
                    "segments": todo,
                }
                system = (
                    _build_system_prompt(type_name, field, target_locale, dnt, strict=strict)
                    + " Restituisci SOLO un oggetto JSON con chiave 'translations' (lista di stringhe) nello stesso ordine di 'segments'."
                )
                logger.info("openai_call_html", type_name=type_name, field=field, segments=len(todo), mode="segment")
                try:
                    resp = self._call_openai(system=system, text=json.dumps(payload, ensure_ascii=False))
                    resp_text = resp[0] if isinstance(resp, tuple) else resp  # compat log meta
                    obj = _safe_json_loads(resp_text)
                    translations = obj.get("translations")
                    if not isinstance(translations, list):
                        raise ValueError("missing 'translations' list")
                    translated_list = [str(x) if x is not None else "" for x in translations]
                except Exception as e:
                    logger.error(
                        "openai_json_error",
                        error=str(e),
                        sample=_snippet_ell(resp_text if 'resp_text' in locals() else "", 500),
                        field=field,
                    )
                    translated_list = list(todo)  # fallback no-op
                for pos, txt in zip(idxs, translated_list, strict=False):
                    fresh_map[pos] = txt
                    key = self._cache_key(
                        type_name,
                        field,
                        target_locale,
                        normalize_text(todo[idxs.index(pos)])
                    )
                    self.cache.set(key, {"translated": txt}, model=self.model)
        # 4) Ricostruzione segmenti completi (aggiusta spazi attorno a *...*/_..._)
        translated_segments: list[str] = []
        for i in range(len(segments)):
            if i in cached_out:
                val = cached_out[i]
                translated_segments.append(_fix_inline_markdown_spacing(val))
            elif i in fresh_map:
                val = fresh_map[i]
                translated_segments.append(_fix_inline_markdown_spacing(val))
            else:
                translated_segments.append(segments[i])

        # 5) Re-iniezione testi e Liquid
        out_html = reinject_text(html_map, translated_segments)
        if liquid_map:
            out_html = unprotect_liquid(out_html, liquid_map)

        # 6) Similarità/Lang: log informativi
        plain_src = " ".join(s.strip() for s in segments if s and s.strip())
        plain_dst = " ".join(s.strip() for s in translated_segments if s and s.strip())
        try:
            lang, conf = detect_lang_fast(plain_dst)  # già definito nel modulo
        except Exception:
            lang, conf = ("unknown", 0.0)
        sim = similarity(plain_src, plain_dst, exclude_tokens=exclude_similarity_tokens)
        logger.info("html_similarity", extra={"sim": sim, "lang": lang, "conf": conf})
        # Salva cell-level cache (HTML completo) per futuri hit
        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_html(type_name, field, target_locale, html_in, exclude_similarity_tokens)
                meta = {"segments": len(segments)}
                self.cache.set_cell(cell_key, out_html, self.model, meta=meta)
        except Exception:
            pass
        return out_html


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
        text  = default_content or ""

        if field == "body_html":
            return self.translate_html(
                type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
            )
        
        if detect_has_liquid(text):
            return self.translate_html(
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
                    cell_key = self._cell_key_plain(type_name, field, target_locale, default_content or "", exclude_similarity_tokens)
                    found = self.cache.get_cell(cell_key)
                    if found and isinstance(found.get("value"), str):
                        self.cache_hits += 1
                        logger.info("cell_cache_hit_plain", field=field)
                        return found["value"]
            except Exception:
                pass
            result = self.translate_plain(
                type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
            )
            if result == "":
                return ""  # reject già deciso
            if field == "meta_title":
                parts = [p.strip() for p in result.split("|")]
                if len(parts) < 3:
                    nm = parts[0] if parts else result
                    result = enforce_meta_title_format(nm, "", "")
                _, adjusted = validate_meta_length(result, MetaRules().max_title_len)
                result = adjusted
            if field == "meta_description":
                _, adjusted = validate_meta_length(result, MetaRules().max_desc_len)
                result = adjusted
            # Salva cell-level cache
            try:
                if not self.ignore_cache:
                    cell_key = self._cell_key_plain(type_name, field, target_locale, default_content or "", exclude_similarity_tokens)
                    self.cache.set_cell(cell_key, result, self.model, meta={})
            except Exception:
                pass
            return result

        return self.translate_plain(
            type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
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
    ) -> str:
        """
        Traduce solo i VALORI (leaf string) del JSON, saltando numeri/unità/URL.
        Tollerante: non solleva, in errore ritorna "" per permettere al chiamante di impostare Status.
        """
        logger.info("json_detect", type_name=type_name, field=field_logical)

        # Cell-level cache (intero JSON string tradotto): early-return se presente
        try:
            if not self.ignore_cache:
                cell_key = self._cell_key_json(type_name, field_logical, target_locale, default_content or "", exclude_similarity_tokens)
                found = self.cache.get_cell(cell_key)
                if found and isinstance(found.get("value"), str):
                    self.cache_hits += 1
                    logger.info("cell_cache_hit_json", field=field_logical)
                    return found["value"]
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
        todo_idxs: list[int] = []
        skip_count = 0

        dont = list(sorted(set(dnt.brands + dnt.units + dnt.tokens)))

        for i, seg in enumerate(leaves):
            s = (seg or "").strip()
            if (
                not s
                or is_technical_value(s)
                or s.lower().startswith("http://")
                or s.lower().startswith("https://")
            ):
                cached_out[i] = s
                skip_count += 1
                logger.info("json_segment_skipped", reason="tech_or_url")
                continue

            # gestisci prefisso tecnico (es. "20m - ")
            prefix = ""
            sp = split_prefix_tech_text(s)
            if sp:
                prefix, s = sp

            text_norm = normalize_text(s)
            key = self._cache_key(type_name, field_logical, target_locale, text_norm)
            entry = self.cache.get(key)
            translated = (entry.get("translated") or "").strip() if entry else ""
            if translated:
                cached_out[i] = prefix + translated
                self.cache_hits += 1
                logger.info("cache_hit_json", field=field_logical)
            else:
                todo_texts.append(s)
                todo_idxs.append(i)
                # memorizza prefix per reiniezione post-traduzione
                cached_out[i] = prefix  # temporaneamente solo prefisso

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
                        # lasciamo vuoto: caller imposterà Status
                        return ""
                    # riallinea lunghezze
                    if len(arr) < len(batch):
                        arr += [""] * (len(batch) - len(arr))
                    elif len(arr) > len(batch):
                        arr = arr[: len(batch)]

                    # copia nelle posizioni originali
                    for j, t in enumerate(arr):
                        idx = todo_idxs[b + j]
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
                    idx = todo_idxs[j]
                    fresh_map[idx] = (cached_out[idx] or "") + f"[{target_locale}] {s}"
        except Exception as e:
            logger.error("openai_json_error", error=str(e), field=field_logical)
            return ""

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

        # 5) Similarità / lingua: solo logging, mai bloccare
        plain_src = " ".join(x.strip() for x in leaves if x and isinstance(x, str))
        # dst flatten
        # prendi valori string dall'oggetto finale
        flat_dst: list[str] = []

        def _walk2(x: Any):
            if isinstance(x, dict):
                for v in x.values():
                    _walk2(v)
            elif isinstance(x, list):
                for v in x:
                    _walk2(v)
            elif isinstance(x, str):
                flat_dst.append(x)

        _walk2(out_obj)
        plain_dst = " ".join(x.strip() for x in flat_dst if x)
        try:
            lang, conf = detect_lang_fast(plain_dst)  # già importato altrove nel file
        except Exception:
            lang, conf = ("unknown", 0.0)
        sim = similarity(plain_src, plain_dst, exclude_tokens=exclude_similarity_tokens)
        logger.info(
            "json_similarity",
            extra={
                "field": field_logical,
                "lang": lang,
                "conf": conf,
                "sim": sim,
                "threshold": self._threshold_for_field(field_logical),
            },
        )

        out_str = json.dumps(out_obj, ensure_ascii=False)
        # Salva cell-level cache
        try:
            if not self.ignore_cache:
                meta = {"leaves": len(leaves)} if 'leaves' in locals() else {}
                cell_key = self._cell_key_json(type_name, field_logical, target_locale, default_content or "", exclude_similarity_tokens)
                self.cache.set_cell(cell_key, out_str, self.model, meta=meta)
        except Exception:
            pass
        return out_str

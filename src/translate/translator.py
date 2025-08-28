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

from src.config.settings import SETTINGS
from src.htmlmap.extract import extract_text_segments
from src.htmlmap.reinject import reinject_text
from src.translate.cache import TranslationCache
from src.translate.langdetect import detect_language
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


try:
    from openai import OpenAI  # SDK v1
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


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
    base = (
        "Sei un traduttore tecnico specializzato in attrazzatura per il giardinaggio e l'agricoltura, conosci tutti i termini specifici di questo campo."
        f"Traduci dall'italiano a {target_locale} il contenuto del campo '{field}' "
        f"per il tipo '{type_name}'. Non tradurre marchi, unità di misura, sigle e i seguenti termini esatti: {dont}. "
        "Mantieni numeri, codici e punteggiatura. Niente markdown o spiegazioni; restituisci solo il testo tradotto."
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
    def __init__(self, cache: TranslationCache, model: str | None = None, dry_run: bool = False):
        self.cache = cache
        self.model = model or SETTINGS.openai_model
        self.dry_run = dry_run
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
            self._client = OpenAI()
        return self._client

    def _cache_key(self, type_name: str, field: str, locale: str, text_norm: str) -> str:
        return self.cache.make_key(
            type_name, field, locale, text_norm, SETTINGS.rules_version, self.model
        )

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
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "text"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        dt = int((time.perf_counter() - t0) * 1000)
        out = (resp.choices[0].message.content or "").strip()
        meta = {
            "usage": getattr(resp, "usage", None) and resp.usage.model_dump() or {},
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
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        dt = int((time.perf_counter() - t0) * 1000)
        text = (resp.choices[0].message.content or "").strip()
        try:
            obj = json.loads(text)
        except Exception as e:
            logger.warning("openai_json_parse_error", error=str(e))
            raise
        meta = {
            "usage": getattr(resp, "usage", None) and resp.usage.model_dump() or {},
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

    def _should_skip_similarity(
        self, default_text: str, exclude_similarity_tokens: Sequence[str]
    ) -> bool:
        return normalize_text(default_text, exclude_tokens=exclude_similarity_tokens) == ""

    def translate_plain(
        self,
        type_name: str,
        field: str,
        default_text: str,
        target_locale: str,
        dnt: DoNotTranslateConfig,
        exclude_similarity_tokens: Sequence[str],
    ) -> str:
        text_norm = normalize_text(default_text)
        key = self._cache_key(type_name, field, target_locale, text_norm)

        cached = self.cache.get(key)
        if cached:
            translated = (cached.get("translated") or "").strip()
            self.cache_hits += 1
            logger.info(
                "cache_hit_plain", type_name=type_name, field=field, chars=len(default_text)
            )
            return translated

        self.cache_misses += 1
        logger.info("cache_miss_plain", type_name=type_name, field=field, chars=len(default_text))

        threshold = self._threshold_for_field(field)
        max_local_retries = max(1, SETTINGS.retry_max)
        translated = default_text
        start = time.perf_counter()
        for attempt in range(1, max_local_retries + 1):
            if self.dry_run:
                translated = f"[{target_locale}] {default_text}".strip()
                meta = {"usage": {}, "duration_ms": 0}
            else:
                strict = attempt > 1
                system = _build_system_prompt(type_name, field, target_locale, dnt, strict=strict)
                translated, meta = self._call_openai(system, default_text)

            lang, _ = detect_language(translated)
            sim_ok = not self._should_skip_similarity(default_text, exclude_similarity_tokens)
            sim = (
                similarity(default_text, translated, exclude_tokens=exclude_similarity_tokens)
                if sim_ok
                else 0.0
            )

            decision = "accept"
            reason = ""
            if not self.dry_run and sim_ok and sim >= threshold and lang != target_locale[:2]:
                decision = "retry" if attempt < max_local_retries else "reject"
                reason = "similarity_high"

            logger.info(
                "translate",
                field=field,
                attempt=attempt,
                sim=sim,
                threshold=threshold,
                lang_out=lang,
                decision=decision,
                reason=reason,
                model=self.model,
                duration_ms=int((time.perf_counter() - start) * 1000),
                usage=meta.get("usage", {}),
            )

            if decision == "accept":
                break
            if decision == "reject":
                translated = ""
                break

        if not self.dry_run:
            self.cache.set(key, {"translated": translated}, self.model)
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
        Traduce HTML in batch:
        - Estrae segmenti testuali -> [[T0]], [[T1]]... (ordine stabile)
        - Usa cache per segmento (hit/miss loggati)
        - Chiama OpenAI una sola volta per i miss, chiedendo JSON {"translations":[...]}
        - Gestisce risposta come str o tuple (text, meta)
        - Parsing JSON tollerante (no crash) + fallback per segmenti vuoti
        - Guard finale: se output “solo tag”, restituisce l’HTML originale
        """
        # 1) Estrazione segmenti
        html_map, segments = extract_text_segments(html or "")

        # 2) Cache per segmenti
        cached_out: dict[int, str] = {}
        todo: list[str] = []
        idxs: list[int] = []  # indici dei segmenti mancanti

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

        # 3) Call OpenAI una sola volta per i miss (se non dry_run e ci sono miss)
        fresh_map: dict[int, str] = {}
        if misses > 0:
            payload = {
                "target_locale": target_locale,
                "field": field,
                "do_not_translate": list(sorted(set(dnt.brands + dnt.units + dnt.tokens))),
                "segments": todo,
            }
            system = (
                _build_system_prompt(type_name, field, target_locale, dnt, strict=strict)
                + " Restituisci SOLO un oggetto JSON con chiave 'translations': una lista di stringhe, "
                "una per ciascun segmento in 'segments', nello stesso ordine."
            )

            if not self.dry_run:
                logger.info("openai_call_html", type_name=type_name, field=field, segments=misses)

            req_text = json.dumps(payload, ensure_ascii=False)
            if self.dry_run:
                resp_text = json.dumps({"translations": todo}, ensure_ascii=False)
            else:
                resp = self._call_openai(system, req_text)
                resp_text = resp[0] if isinstance(resp, tuple) else resp

            # 4) Parsing tollerante
            translated_list: list[str] = []
            try:
                obj = json.loads(resp_text)
                translations = obj.get("translations")
                if not isinstance(translations, list):
                    raise ValueError("missing translations list")
                translated_list = [str(x or "").strip() for x in translations]
            except Exception as e:
                logger.error(
                    "openai_json_error",
                    error=str(e)[:200],
                    sample=(resp_text or "")[:200],
                    field=field,
                )
                translated_list = []

            # Pad o tronca per allineare alle aspettative
            if len(translated_list) != len(todo):
                if len(translated_list) > len(todo):
                    translated_list = translated_list[: len(todo)]
                else:
                    translated_list.extend([""] * (len(todo) - len(translated_list)))

            # Fallback per-segmento: se vuoto, usa il sorgente
            translated_list = [
                dst if (dst or "").strip() else src
                for src, dst in zip(todo, translated_list, strict=False)
            ]

            # Mappa {idx -> traduzione} per i miss
            fresh_map = {i: translated_list[k] for k, i in enumerate(idxs)}

            # Aggiorna cache solo per i segmenti appena tradotti
            for i, seg in zip(idxs, todo, strict=False):
                text_norm = normalize_text(seg)
                key = self._cache_key(type_name, field, target_locale, text_norm)
                self.cache.set(key, {"translated": fresh_map.get(i, "")}, self.model)

        # 5) Ricostruzione lista finale nell'ordine originale
        translated_segments: list[str] = []
        for i in range(len(segments)):
            if i in cached_out:
                translated_segments.append(cached_out[i])
            else:
                translated_segments.append(fresh_map.get(i, ""))

        # 6) Similarità/lingua (tollerante, solo log)
        plain_src = " ".join(seg.strip() for seg in segments if seg and seg.strip())
        plain_dst = " ".join(seg.strip() for seg in translated_segments if seg and seg.strip())
        try:
            lang, conf = detect_lang_fast(plain_dst)
        except Exception:
            lang, conf = ("unknown", 0.0)

        sim = similarity(plain_src, plain_dst, exclude_tokens=exclude_similarity_tokens)
        logger.info(
            "html_similarity",
            field=field,
            lang=lang,
            conf=conf,
            sim=sim,
            threshold=SETTINGS.sim_t_html,
        )

        # Guard finale: se output “solo tag” (no testo) ma input aveva testo, restituisci originale
        if plain_src and not plain_dst:
            logger.warning("html_translation_empty_fallback", field=field)
            return html or ""

        # 7) Re-iniezione nelle strutture HTML e ritorno
        out_html = reinject_text(html_map, translated_segments)
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
        if field == "body_html":
            return self.translate_html(
                type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
            )

        if field == "handle":
            if preserve_handle:
                result = self.translate_plain(
                    type_name, field, default_content, target_locale, dnt, exclude_similarity_tokens
                )
                ok = validate_handle(result)
                if not ok:
                    base = title_translated or default_content
                    result = make_handle_from_title(base)
                return result
            # Non preserviamo: generiamo dallo slug del TITLE tradotto.
            # Se il TITLE è stato rifiutato (""), blocchiamo anche l'handle.
            if title_translated is not None:
                if title_translated == "":
                    return ""  # reject: niente handle senza titolo valido
                base = title_translated
            else:
                # fallback raro: se il titolo non è ancora stato visto, usa il default
                base = default_content
            return make_handle_from_title(base)

        if field in {"meta_title", "meta_description", "title", "product_type"}:
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

        return json.dumps(out_obj, ensure_ascii=False)

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

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
        dnt: "DoNotTranslateConfig",
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
                resp_text, meta = json.dumps({"translations": todo}, ensure_ascii=False), {}
            else:
                resp = self._call_openai(system, req_text)
                if isinstance(resp, tuple):
                    resp_text, meta = resp
                else:
                    resp_text, meta = resp, {}

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
            translated_list = [dst if (dst or "").strip() else src for src, dst in zip(todo, translated_list)]

            # Mappa {idx -> traduzione} per i miss
            fresh_map = {i: translated_list[k] for k, i in enumerate(idxs)}

            # Aggiorna cache solo per i segmenti appena tradotti
            for i, seg in zip(idxs, todo):
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
        logger.info("html_similarity", field=field, lang=lang, conf=conf, sim=sim, threshold=SETTINGS.sim_t_html)

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

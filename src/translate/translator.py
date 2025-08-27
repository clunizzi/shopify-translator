from __future__ import annotations

import hashlib
import json
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
    field: str, target_locale: str, dnt: DoNotTranslateConfig, strict: bool = False
) -> str:
    dont = ", ".join(sorted(set(dnt.brands + dnt.units + dnt.tokens)))
    base = (
        "Sei un traduttore tecnico specializzato in attrazzatura per il giardinaggio e l'agricoltura, conosci tutti i termini specifici di questo campo." 
        "Traduci dall'italiano a "
        f"{target_locale} il contenuto del campo '{field}'. "
        "Non tradurre marchi, unità di misura, sigle e i seguenti termini esatti: "
        f"{dont}. Mantieni numeri, codici e punteggiatura. "
        "Niente markdown o spiegazioni; restituisci solo il testo tradotto."
    )
    if field == "meta_title":
        base += " Rispetta il formato: 'Nome e modello | Marca | Benefit' e mantieni conciso."
    if field == "meta_description":
        base += (
            " Scrivi una descrizione naturale tra 150 e 160 caratteri, informativa, senza emoji."
        )
    if strict:
        base += (
            " Traduci TUTTO integralmente nella lingua target; non lasciare parole in italiano, "
            "eccetto i termini indicati da non tradurre."
        )
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
        return SETTINGS.sim_t_title

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
            cached_text = cached.get("translated", "")
            import re as _re

            if not _re.match(PLACEHOLDER_PREFIX_RE, cached_text or ""):
                return cached_text

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
                system = _build_system_prompt(field, target_locale, dnt, strict=strict)
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
    ) -> str:
        """
        Batch: traduce tutti i segmenti testuali in UNA sola chiamata JSON.
        - Usa cache per-segment; chiama OpenAI solo per i mancanti (al più 1 call per tentativo).
        - Se JSON malformato o length mismatch dopo i retry -> ritorna "" (reject) e continua.
        """
        html_map, segments = extract_text_segments(html or "")

        # Prepara lista segmenti non vuoti + cache lookup
        idxs: list[int] = []
        todo: list[str] = []
        cached_out: dict[int, str] = {}
        for i, seg in enumerate(segments):
            s = seg.strip()
            if not s:
                cached_out[i] = seg  # preserva spacing
                continue
            # lookup cache plain
            text_norm = normalize_text(s)
            key = self._cache_key(type_name, field, target_locale, text_norm)
            entry = self.cache.get(key)
            cached_text = (entry or {}).get("translated", "") if entry else ""
            if cached_text and not cached_text.startswith("["):
                cached_out[i] = cached_text
            else:
                idxs.append(i)
                todo.append(s)

        # Se serve, una sola chiamata JSON (con retry locale + tenacity interna)
        translated_list: list[str] = []
        if self.dry_run:
            translated_list = [f"[{target_locale}] {t}" for t in todo]
        elif todo:
            attempts = max(1, SETTINGS.retry_max)
            last_reason = ""
            for attempt in range(1, attempts + 1):
                try:
                    strict = False
                    system = (
                        _build_system_prompt(field, target_locale, dnt, strict=strict)
                        + " Restituisci SOLO un oggetto JSON con chiave 'translations': "
                        "una lista di stringhe tradotte della STESSA lunghezza e nello STESSO ordine dei 'segments' forniti."
                    )
                    payload = {
                        "target_locale": target_locale,
                        "field": field,
                        "do_not_translate": list(sorted(set(dnt.brands + dnt.units + dnt.tokens))),
                        "segments": todo,
                    }
                    obj, meta = self._call_openai_json(system, payload)
                    translations = obj.get("translations")
                    if not isinstance(translations, list):
                        last_reason = "json_not_list"
                        logger.warning("html_batch_mismatch", attempt=attempt, reason=last_reason)
                        continue
                    if len(translations) != len(todo):
                        last_reason = "length_mismatch"
                        logger.warning(
                            "html_batch_mismatch",
                            attempt=attempt,
                            reason=last_reason,
                            expected=len(todo),
                            got=len(translations),
                        )
                        continue
                    translated_list = [str(x or "").strip() for x in translations]
                    break  # OK
                except Exception as e:
                    last_reason = "json_parse_error"
                    logger.warning("html_batch_exception", attempt=attempt, error=str(e))
                    continue

            if not translated_list or len(translated_list) != len(todo):
                # fallito dopo i retry -> rifiuta HTML ma non bloccare il job
                logger.error(
                    "html_batch_error",
                    reason=last_reason or "unknown",
                    expected=len(todo),
                    got=len(translated_list or []),
                )
                return ""

        # Ricostruisci la lista completa dei segmenti tradotti e aggiorna cache
        out_segments: list[str] = []
        pos = 0
        for i, seg in enumerate(segments):
            if i in cached_out:
                out_segments.append(cached_out[i])
                continue
            tr = translated_list[pos] if pos < len(translated_list) else ""
            pos += 1
            out_segments.append(tr)
            if not self.dry_run:
                # salva in cache per-segment
                text_norm = normalize_text(seg.strip())
                key = self._cache_key(type_name, field, target_locale, text_norm)
                self.cache.set(key, {"translated": tr}, self.model)

        # Similarità a livello pagina (plain text)
        plain_src = " ".join([s.strip() for s in segments if s.strip()])
        plain_dst = " ".join([s.strip() for s in out_segments if s.strip()])
        sim = similarity(plain_src, plain_dst, exclude_tokens=exclude_similarity_tokens)
        threshold = self._threshold_for_field("body_html")
        lang, _ = detect_language(plain_dst or "")
        decision = (
            "accept" if (self.dry_run or sim < threshold or lang == target_locale[:2]) else "reject"
        )
        logger.info(
            "translate_html_batch",
            total_segments=len(segments),
            translated=len(todo),
            sim=sim,
            threshold=threshold,
            lang_out=lang,
            decision=decision,
            model=self.model,
        )
        if decision == "reject":
            logger.warning("html_reject", sim=sim, threshold=threshold)
            return ""

        out_html = reinject_text(html_map, out_segments)
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

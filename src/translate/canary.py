from __future__ import annotations

import hashlib
import time
from typing import Any

from src.config.dnt_loader import load_do_not_translate
from src.config.settings import SETTINGS
from src.translate.cache import TranslationCache
from src.translate.translator import Translator

TITLE_SAMPLE = "Trattore tagliaerba compatto con raccolta posteriore"
HTML_SAMPLE = (
    "<p>Ideale per la manutenzione di prati e aree verdi.</p>"
    "<ul><li>Larghezza di taglio: 92 cm</li><li>Motore bicilindrico</li></ul>"
)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def run_model_canary(
    *,
    models: list[str],
    target_locales: list[str],
) -> dict[str, Any]:
    """Exercise text, HTML and JSON response contracts without Shopify/Neon."""
    dnt = load_do_not_translate(SETTINGS.do_not_translate_path)
    results: list[dict[str, Any]] = []
    for model in models:
        for locale in target_locales:
            cache = TranslationCache(":memory:")
            translator = Translator(
                cache=cache,
                model=model,
                fallback_model="",
                ignore_cache=True,
            )
            started = time.perf_counter()
            item: dict[str, Any] = {
                "model": model,
                "locale": locale,
                "status": "passed",
                "contracts": {},
            }
            try:
                title = translator.translate_plain(
                    "PRODUCT",
                    "title",
                    TITLE_SAMPLE,
                    locale,
                    dnt,
                    [],
                )
                item["contracts"]["plain_text"] = {
                    "ok": bool(title.strip()),
                    "chars": len(title),
                    "hash": _fingerprint(title),
                    "snippet": title[:120],
                }

                html = translator.translate_html_document(
                    "PRODUCT",
                    "body_html",
                    HTML_SAMPLE,
                    locale,
                    dnt,
                    [],
                )
                item["contracts"]["html"] = {
                    "ok": bool(html.strip()),
                    "chars": len(html),
                    "hash": _fingerprint(html),
                }

                json_obj, _meta = translator._call_openai_json(
                    (
                        f"Return valid JSON only. Translate the value to {locale}; "
                        "keep the key exactly unchanged."
                    ),
                    {"label": "Aggiungi al carrello"},
                )
                item["contracts"]["json_object"] = {
                    "ok": isinstance(json_obj, dict) and bool(json_obj),
                    "keys": sorted(str(key) for key in json_obj),
                }
                if not all(contract["ok"] for contract in item["contracts"].values()):
                    item["status"] = "failed"
            except Exception as exc:
                item["status"] = "failed"
                item["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                item["duration_ms"] = int((time.perf_counter() - started) * 1000)
                item["telemetry"] = translator.get_telemetry()
                cache.close()
            results.append(item)
    return {
        "read_only": True,
        "shopify_calls": 0,
        "neon_writes": 0,
        "models": models,
        "target_locales": target_locales,
        "passed": sum(item["status"] == "passed" for item in results),
        "failed": sum(item["status"] != "passed" for item in results),
        "results": results,
    }

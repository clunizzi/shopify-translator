from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _get_bool(name: str, default: bool) -> bool:
    """Parse boolean env vars more robustly (accepts 1/0, true/false, yes/no, y/n)."""
    v = os.getenv(name)
    if v is None or v == "":
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y"}:
        return True
    if s in {"0", "false", "no", "n"}:
        return False
    try:
        return bool(int(s))
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    # Shopify
    shopify_domain: str = os.getenv("SHOPIFY_STORE_DOMAIN", "") or os.getenv("SHOP_DOMAIN", "")
    shopify_token: str = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "") or os.getenv(
        "SHOPIFY_ADMIN_TOKEN", ""
    )
    shopify_api_version: str = os.getenv("SHOPIFY_API_VERSION", "2026-07")

    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    openai_fallback_model: str = os.getenv("OPENAI_FALLBACK_MODEL", "")

    # General
    target_locale: str = os.getenv("TARGET_LOCALE", "fr-FR")
    batch_size: int = _get_int("BATCH_SIZE", 200)

    # Similarity thresholds per field
    sim_t_title: float = _get_float("SIM_T_TITLE", 0.985)
    sim_t_html: float = _get_float("SIM_T_HTML", 0.98)
    sim_t_meta: float = _get_float("SIM_T_META", 0.985)
    sim_t_product_type: float = _get_float("SIM_T_PRODUCT_TYPE", 0.985)
    sim_t_handle: float = _get_float("SIM_T_HANDLE", 0.985)
    # Nuovi
    sim_t_option: float = _get_float("SIM_T_OPTION", 0.99)
    sim_t_value: float = _get_float("SIM_T_VALUE", 0.99)

    retry_max: int = _get_int("RETRY_MAX", 2)
    # Nuovi
    retry_max_option: int = _get_int("RETRY_MAX_OPTION", 1)
    retry_max_value: int = _get_int("RETRY_MAX_VALUE", 1)

    rules_version: int = _get_int("RULES_VERSION", 1)
    # Cache algorithm version (bump to invalidate cell-cache signatures)
    cache_algo_version: int = _get_int("CACHE_ALGO_VERSION", 2)

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "")
    log_retention_days: int = _get_int("LOG_RETENTION_DAYS", 14)
    log_payloads: bool = _get_bool("LOG_PAYLOADS", False)
    log_payload_max: int = _get_int("LOG_PAYLOAD_MAX", 500)

    # Sync/Shopify extras
    source_locale: str = os.getenv("SOURCE_LOCALE", "en")
    target_locales_raw: str = os.getenv("TARGET_LOCALES", "")
    mf_include_raw: str = os.getenv("MF_INCLUDE", "")
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "30"))
    retries: int = _get_int("RETRIES", 2)
    dry_run_default: bool = _get_bool("DRY_RUN", False)
    delay_ms_after_create: int = _get_int("DELAY_MS_AFTER_CREATE", 8000)
    # Optional path to DNT yaml
    do_not_translate_path: str | None = os.getenv(
        "DO_NOT_TRANSLATE_YAML",
        str(Path(__file__).with_name("do_not_translate.yaml")),
    )

    # Prompt specialization (domain) and source language label
    # Customize to adapt the translator tone/domain without changing code.
    translator_specialization: str = os.getenv(
        "TRANSLATOR_SPECIALIZATION",
        "e-commerce product catalog",
    )
    translator_brand: str = os.getenv("TRANSLATOR_BRAND", "")
    translator_audience: str = os.getenv(
        "TRANSLATOR_AUDIENCE",
        "online shoppers who expect accurate and natural product information",
    )
    metafield_translation_policy_path: str = os.getenv(
        "METAFIELD_TRANSLATION_POLICY_PATH",
        "src/config/metafield_translation.yaml",
    )
    theme_translation_policy_path: str = os.getenv(
        "THEME_TRANSLATION_POLICY_PATH",
        "src/config/theme_translation.yaml",
    )
    bootstrap_ids_file: str = os.getenv("BOOTSTRAP_IDS_FILE", "state/bootstrap_product_ids.txt")
    bootstrap_apply_translations: bool = _get_bool("BOOTSTRAP_APPLY_TRANSLATIONS", False)
    bootstrap_existing_products: bool = _get_bool("BOOTSTRAP_EXISTING_PRODUCTS", True)
    # Human-friendly name of source language for prompts (e.g., "italiano", "inglese")
    source_language_name: str = os.getenv("SOURCE_LANGUAGE_NAME", "source language")
    neon_database_url: str = os.getenv("NEON_DATABASE_URL", "")

    @property
    def has_shopify(self) -> bool:
        return bool(self.shopify_domain and self.shopify_token)

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    # ---- Helpers for CSV/env style lists ----
    def get_target_locales(self) -> list[str]:
        raw = (self.target_locales_raw or "").strip()
        return [x.strip() for x in raw.split(",") if x.strip()] if raw else []

    def get_mf_include(self) -> list[tuple[str, str]]:
        raw = (self.mf_include_raw or "").strip()
        if not raw:
            return []
        items: list[tuple[str, str]] = []
        for part in raw.split(","):
            p = part.strip()
            if not p or "." not in p:
                continue
            ns, key = p.split(".", 1)
            items.append((ns.strip(), key.strip()))
        return items


SETTINGS = Settings()

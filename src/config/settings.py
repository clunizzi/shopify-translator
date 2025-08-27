from __future__ import annotations

import os
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Settings:
    # Shopify
    shopify_domain: str = os.getenv("SHOPIFY_STORE_DOMAIN", "")
    shopify_token: str = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "")

    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o")

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

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "")
    log_retention_days: int = _get_int("LOG_RETENTION_DAYS", 14)
    log_payloads: bool = bool(int(os.getenv("LOG_PAYLOADS", "0")))
    log_payload_max: int = _get_int("LOG_PAYLOAD_MAX", 500)

    @property
    def has_shopify(self) -> bool:
        return bool(self.shopify_domain and self.shopify_token)

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)


SETTINGS = Settings()

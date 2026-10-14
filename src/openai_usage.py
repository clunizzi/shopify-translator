from __future__ import annotations

from typing import Any

# Standard, short-context token rates in USD per 1M tokens. Keep the pricing
# date with every persisted estimate so historical dashboard values remain
# explainable when model prices change.
OPENAI_PRICING_DATE = "2026-10-14"
OPENAI_PRICING_USD_PER_MILLION = {
    "gpt-6-luna": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "output": 0.50},
    "gpt-6-sol": {"input": 2.00, "cached": 0.20, "cache_write": 2.50, "output": 10.00},
    "gpt-6.1-sol": {"input": 2.00, "cached": 0.10, "cache_write": 2.50, "output": 10.00},
    "gpt-5.6-terra": {
        "input": 2.00,
        "cached": 0.20,
        "cache_write": 2.50,
        "output": 12.00,
    },
    "gpt-5.6-sol": {"input": 4.00, "cached": 0.40, "cache_write": 5.00, "output": 20.00},
}


def estimate_openai_cost(telemetry: dict[str, Any]) -> dict[str, Any]:
    total_usd = 0.0
    unknown_models: list[str] = []
    for model, usage in (telemetry.get("openai_usage_by_model") or {}).items():
        rates = OPENAI_PRICING_USD_PER_MILLION.get(model)
        if rates is None:
            unknown_models.append(model)
            continue
        prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
        cached_tokens = max(0, int(usage.get("cached_prompt_tokens") or 0))
        cache_write_tokens = max(0, int(usage.get("cache_write_tokens") or 0))
        uncached_tokens = max(0, prompt_tokens - cached_tokens - cache_write_tokens)
        completion_tokens = max(0, int(usage.get("completion_tokens") or 0))
        total_usd += (
            uncached_tokens * rates["input"]
            + cached_tokens * rates["cached"]
            + cache_write_tokens * rates["cache_write"]
            + completion_tokens * rates["output"]
        ) / 1_000_000
    return {
        "amount": round(total_usd, 8),
        "currency": "USD",
        "pricing_date": OPENAI_PRICING_DATE,
        "complete": not unknown_models,
        "unknown_models": sorted(unknown_models),
    }


def persist_translation_usage_event(
    *,
    shop_domain: str,
    event_key: str,
    event_type: str,
    entity_id: str,
    topic: str,
    source: str,
    status: str,
    telemetry: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist compact usage telemetry. No source or translated text is stored."""

    from src.state.neon import NeonTranslationStore

    normalized_telemetry = telemetry if isinstance(telemetry, dict) else {}
    estimate = estimate_openai_cost(normalized_telemetry)
    store = NeonTranslationStore()
    try:
        inserted = store.append_translation_usage_event(
            shop_domain=shop_domain,
            event_key=event_key,
            event_type=event_type,
            entity_id=entity_id,
            topic=topic,
            source=source,
            status=status,
            telemetry=normalized_telemetry,
            estimated_cost=estimate,
            metadata=metadata,
        )
        return {"inserted": inserted, "estimated_cost": estimate}
    finally:
        store.close()

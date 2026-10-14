import asyncio
from types import SimpleNamespace

from src.translate.cache import TranslationCache
from src.translate.translator import DoNotTranslateConfig, Translator


class _AsyncCompletions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
            usage=None,
        )


def test_gpt56_request_uses_none_reasoning_without_temperature():
    options = Translator._chat_completion_options(
        model="gpt-5.6-terra",
        response_format={"type": "text"},
    )
    assert options["reasoning_effort"] == "none"
    assert "temperature" not in options


def test_gpt61_request_uses_low_reasoning_without_temperature():
    options = Translator._chat_completion_options(
        model="gpt-6.1-sol",
        response_format={"type": "text"},
    )
    assert options["reasoning_effort"] == "low"
    assert "temperature" not in options


def test_gpt6_luna_request_uses_no_reasoning_without_temperature():
    options = Translator._chat_completion_options(
        model="gpt-6-luna",
        response_format={"type": "text"},
    )
    assert options["reasoning_effort"] == "none"
    assert "temperature" not in options


def test_telemetry_tracks_cached_and_reasoning_tokens_by_model():
    cache = TranslationCache(":memory:")
    translator = Translator(cache=cache, model="gpt-6-luna")

    translator._record_openai_usage(
        model="gpt-6-luna",
        duration_ms=25,
        usage={
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
    )

    telemetry = translator.get_telemetry()
    assert telemetry["openai_calls"] == 1
    assert telemetry["openai_cached_prompt_tokens"] == 80
    assert telemetry["openai_reasoning_tokens"] == 4
    assert telemetry["openai_usage_by_model"]["gpt-6-luna"] == {
        "calls": 1,
        "duration_ms": 25,
        "prompt_tokens": 120,
        "cached_prompt_tokens": 80,
        "cache_write_tokens": 0,
        "completion_tokens": 30,
        "reasoning_tokens": 4,
    }
    cache.close()


def test_legacy_request_keeps_zero_temperature():
    options = Translator._chat_completion_options(
        model="gpt-5.2",
        response_format={"type": "json_object"},
    )
    assert options["temperature"] == 0
    assert "reasoning_effort" not in options


def test_async_validation_failure_falls_back_to_sol(monkeypatch):
    cache = TranslationCache(":memory:")
    translator = Translator(
        cache=cache,
        model="gpt-5.6-terra",
        fallback_model="gpt-5.6-sol",
    )
    completions = _AsyncCompletions(["", "Übersetzter Text"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(translator, "_async_client_openai", lambda: client)
    monkeypatch.setattr(translator, "_retry_max_for_field", lambda _field: 1)

    result = asyncio.run(
        translator._translate_and_validate_async(
            field="title",
            text="Testo italiano",
            target_locale="de",
            system="Translate",
            dnt=DoNotTranslateConfig(brands=[], units=[], tokens=[], glossary={}),
        )
    )

    assert result == "Übersetzter Text"
    assert completions.calls[0]["model"] == "gpt-5.6-terra"
    assert completions.calls[1]["model"] == "gpt-5.6-sol"
    assert translator.fallback_calls == 1
    cache.close()

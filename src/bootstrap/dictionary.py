from __future__ import annotations

from dataclasses import dataclass

from src.state.neon import NeonTranslationStore, make_source_hash


@dataclass(frozen=True)
class DictionaryResolution:
    translated_value: str
    source: str


DICTIONARY_CATEGORIES = {
    "product_type": "product_type",
    "custom.condizione": "custom.condizione",
    "option_name": "option_name",
    "option_value": "option_value",
}


def resolve_dictionary_first(
    store: NeonTranslationStore,
    *,
    category: str,
    source_locale: str,
    target_locale: str,
    source_value: str,
    existing_translation: str | None = None,
) -> DictionaryResolution | None:
    source = (source_value or "").strip()
    if not source:
        return None
    if existing_translation:
        store.upsert_dictionary_translation(
            category=category,
            source_locale=source_locale,
            target_locale=target_locale,
            source_value=source,
            translated_value=existing_translation,
            metadata={"origin": "shopify_existing_translation"},
        )
        return DictionaryResolution(translated_value=existing_translation, source="dictionary:shopify_existing")

    known = store.get_dictionary_translation(
        category=category,
        source_locale=source_locale,
        target_locale=target_locale,
        source_value=source,
    )
    if known:
        return DictionaryResolution(translated_value=known, source="dictionary:neon")
    return None


def resolve_memory_second(
    store: NeonTranslationStore,
    *,
    field_key: str,
    source_locale: str,
    target_locale: str,
    source_value: str,
) -> DictionaryResolution | None:
    source = (source_value or "").strip()
    if not source:
        return None
    cached = store.get_translation_memory(
        source_hash=make_source_hash(source),
        field_key=field_key,
        source_locale=source_locale,
        target_locale=target_locale,
    )
    if cached is None:
        return None
    return DictionaryResolution(translated_value=cached, source="memory")

from src.bootstrap.dictionary import resolve_dictionary_first, resolve_memory_second


class _FakeStore:
    def __init__(self):
        self.dictionary = {}
        self.memory = {}

    def upsert_dictionary_translation(self, **kwargs):
        key = (
            kwargs["category"],
            kwargs["source_locale"],
            kwargs["target_locale"],
            kwargs["source_value"],
        )
        self.dictionary[key] = kwargs["translated_value"]

    def get_dictionary_translation(self, *, category, source_locale, target_locale, source_value):
        return self.dictionary.get((category, source_locale, target_locale, source_value))

    def get_translation_memory(self, *, source_hash, field_key, source_locale, target_locale):
        return self.memory.get((source_hash, field_key, source_locale, target_locale))


def test_dictionary_resolution_prefers_existing_translation():
    store = _FakeStore()
    res = resolve_dictionary_first(
        store,
        category="product_type",
        source_locale="it",
        target_locale="fr",
        source_value="Motozappa",
        existing_translation="Motobineuse",
    )
    assert res is not None
    assert res.translated_value == "Motobineuse"


def test_memory_resolution_uses_cached_translation():
    from src.state.neon import make_source_hash

    store = _FakeStore()
    source_value = "Trasmissione"
    source_hash = make_source_hash(source_value)
    store.memory[(source_hash, "metafield.custom.specifiche_tecniche", "it", "de")] = "Getriebe"
    res = resolve_memory_second(
        store,
        field_key="metafield.custom.specifiche_tecniche",
        source_locale="it",
        target_locale="de",
        source_value=source_value,
    )
    assert res is not None
    assert res.translated_value == "Getriebe"

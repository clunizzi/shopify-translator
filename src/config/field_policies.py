from __future__ import annotations

from dataclasses import dataclass

PRODUCT_KEYS_EXCLUDED = {
    "seo.title",
    "seo.description",
    "meta_title",
    "meta_description",
}

PRODUCT_KEYS_SUPPORTED = {
    "title",
    "body_html",
    "product_type",
    "handle",
}


@dataclass(frozen=True)
class HandlePolicy:
    mode: str = "create_only"

    def allows(self, *, is_create: bool, existing_product: bool) -> bool:
        if self.mode == "never":
            return False
        if self.mode == "always":
            return True
        if self.mode == "create_only":
            return is_create and not existing_product
        return False


DEFAULT_HANDLE_POLICY = HandlePolicy(mode="create_only")


def should_translate_product_key(
    key: str,
    *,
    is_create: bool,
    existing_product: bool,
    handle_policy: HandlePolicy = DEFAULT_HANDLE_POLICY,
) -> bool:
    field_key = (key or "").strip()
    if not field_key:
        return False
    if field_key in PRODUCT_KEYS_EXCLUDED:
        return False
    if field_key not in PRODUCT_KEYS_SUPPORTED:
        return False
    if field_key == "handle":
        return handle_policy.allows(is_create=is_create, existing_product=existing_product)
    return True

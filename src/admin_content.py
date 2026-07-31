from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from src.bootstrap.catalog import AUTO_TYPES
from src.bootstrap.theme import (
    THEME_RESOURCE_TYPES,
    assert_approved_main_theme,
    build_theme_documents,
    fetch_theme_source_bundle,
)
from src.config.metafield_policies import (
    should_translate_metafield,
    should_translate_metafield_leaf,
)
from src.config.settings import SETTINGS
from src.rules.option_value import (
    should_skip_option_name,
    should_skip_option_value_name,
)
from src.shopify.graphql import (
    get_product_all_metafields,
    get_product_option_resources,
    get_product_summary,
    get_resource_translation_matrix,
    get_translatable_by_ids,
    register_translations,
    search_products,
)
from src.translate.translator import translation_output_issue
from src.translate.validators import validate_handle

TARGET_LOCALES = ("de", "fr")
STOREFRONT_LOCALE_PATHS = {
    "de": "de-de",
    "fr": "fr-fr",
}
PRODUCT_GID_RE = re.compile(r"^gid://shopify/Product/([1-9][0-9]*)$")
PRODUCT_ADMIN_URL_RE = re.compile(r"/products/([1-9][0-9]*)(?:/|$)")
LIQUID_RE = re.compile(
    r"\{\%-?\s*raw\s*\-?\%\}.*?\{\%-?\s*endraw\s*\-?\%\}"
    r"|\{\{\-?.*?\-?\}\}"
    r"|\{\%-?.*?\-?\%\}",
    re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9:-]*)\b[^>]*>")


class AdminContentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def normalize_product_search_query(value: object) -> str:
    """Turn IDs, product GIDs and Admin URLs into Shopify's exact ID filter."""
    query = str(value or "").strip()
    if not query:
        return ""
    if query.isdigit():
        return f"id:{query}"
    gid_match = PRODUCT_GID_RE.fullmatch(query)
    if gid_match:
        return f"id:{gid_match.group(1)}"
    url_match = PRODUCT_ADMIN_URL_RE.search(query)
    if url_match:
        return f"id:{url_match.group(1)}"
    return query


def _translation_status(item: dict[str, Any] | None) -> str:
    if not item or not str(item.get("value") or "").strip():
        return "missing"
    return "outdated" if bool(item.get("outdated")) else "current"


def _content_kind(key: str, value: str, metafield_type: str = "") -> str:
    normalized_type = metafield_type.lower()
    if normalized_type in {"json", "rich_text_field"}:
        return "json"
    if key == "handle":
        return "handle"
    if normalized_type == "html" or ("<" in value and ">" in value):
        return "html"
    if "{{" in value or "{%" in value:
        return "liquid"
    return "plain"


def _field_payload(
    *,
    resource_id: str,
    resource_label: str,
    group: str,
    key: str,
    source: dict[str, Any],
    matrix: dict[str, dict[str, dict[str, dict[str, Any]]]],
    metafield_type: str = "",
    metafield_namespace: str = "",
    metafield_key: str = "",
) -> dict[str, Any]:
    translations: dict[str, dict[str, Any]] = {}
    for locale in TARGET_LOCALES:
        item = ((matrix.get(resource_id) or {}).get(locale) or {}).get(key)
        translations[locale] = {
            "value": str((item or {}).get("value") or ""),
            "outdated": bool((item or {}).get("outdated")),
            "status": _translation_status(item),
        }
    source_value = str(source.get("value") or "")
    editable_json_paths: list[list[str | int]] = []
    if metafield_type.lower() in {"json", "rich_text_field"}:
        try:
            source_json = json.loads(source_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            source_json = None

        def _walk(value: Any, path: tuple[str | int, ...] = ()) -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    _walk(child_value, (*path, str(child_key)))
            elif isinstance(value, list):
                for index, child_value in enumerate(value):
                    _walk(child_value, (*path, index))
            elif isinstance(value, str) and should_translate_metafield_leaf(
                metafield_namespace,
                metafield_key,
                path,
                value,
            ):
                editable_json_paths.append(list(path))

        if source_json is not None:
            _walk(source_json)
    return {
        "resource_id": resource_id,
        "resource_label": resource_label,
        "group": group,
        "key": key,
        "content_kind": _content_kind(key, source_value, metafield_type),
        "metafield_type": metafield_type,
        "editable_json_paths": editable_json_paths,
        "source": {
            "locale": str(source.get("locale") or "it"),
            "value": source_value,
            "digest": str(source.get("digest") or ""),
        },
        "translations": translations,
    }


def _require_product_gid(value: object) -> str:
    product_gid = str(value or "").strip()
    if not PRODUCT_GID_RE.fullmatch(product_gid):
        raise AdminContentError("INVALID_PRODUCT_ID", "Prodotto non valido.")
    return product_gid


async def inspect_product(product_id: object) -> dict[str, Any]:
    product_gid = _require_product_gid(product_id)
    product = await get_product_summary(product_gid)
    if not product:
        raise AdminContentError("PRODUCT_NOT_FOUND", "Prodotto non trovato su Shopify.")

    metafields = await get_product_all_metafields(product_gid, allowed_types=AUTO_TYPES)
    metafields = [
        item
        for item in metafields
        if should_translate_metafield(
            str(item.get("namespace") or ""),
            str(item.get("key") or ""),
        )
    ]
    options = await get_product_option_resources(product_gid)
    resource_ids = [
        product_gid,
        *[str(item.get("id")) for item in metafields if item.get("id")],
        *[str(item.get("resource_id")) for item in options if item.get("resource_id")],
    ]
    source_map = await get_translatable_by_ids(resource_ids)
    matrix = await get_resource_translation_matrix(resource_ids, list(TARGET_LOCALES))
    metafields_by_id = {str(item.get("id")): item for item in metafields if item.get("id")}
    options_by_id = {
        str(item.get("resource_id")): item for item in options if item.get("resource_id")
    }

    fields: list[dict[str, Any]] = []
    for resource_id in resource_ids:
        for source in source_map.get(resource_id) or []:
            key = str(source.get("key") or "")
            value = str(source.get("value") or "")
            if not key or not value.strip():
                continue
            if resource_id == product_gid:
                fields.append(
                    _field_payload(
                        resource_id=resource_id,
                        resource_label="Prodotto",
                        group="product",
                        key=key,
                        source=source,
                        matrix=matrix,
                    )
                )
                continue
            metafield = metafields_by_id.get(resource_id)
            if metafield:
                namespace = str(metafield.get("namespace") or "")
                metafield_key = str(metafield.get("key") or "")
                fields.append(
                    _field_payload(
                        resource_id=resource_id,
                        resource_label=f"{namespace}.{metafield_key}",
                        group="metafield",
                        key=key,
                        source=source,
                        matrix=matrix,
                        metafield_type=str(metafield.get("type") or ""),
                        metafield_namespace=namespace,
                        metafield_key=metafield_key,
                    )
                )
                continue
            option = options_by_id.get(resource_id)
            if option:
                if option.get("kind") == "option_name":
                    skip_option, _ = should_skip_option_name(value)
                else:
                    skip_option, _ = should_skip_option_value_name(value, ())
                if skip_option:
                    continue
                label = "Nome opzione" if option.get("kind") == "option_name" else "Valore opzione"
                fields.append(
                    _field_payload(
                        resource_id=resource_id,
                        resource_label=label,
                        group="option",
                        key=key,
                        source=source,
                        matrix=matrix,
                    )
                )

    preview_urls: dict[str, str] = {}
    online_store_url = str(product.get("online_store_url") or "")
    parsed_storefront = urlparse(online_store_url)
    if parsed_storefront.scheme and parsed_storefront.netloc:
        preview_urls["it"] = online_store_url
        handle_field = next(
            (
                field
                for field in fields
                if field.get("group") == "product" and field.get("key") == "handle"
            ),
            None,
        )
        for locale, locale_path in STOREFRONT_LOCALE_PATHS.items():
            translated_handle = str(
                ((((handle_field or {}).get("translations") or {}).get(locale) or {}).get("value"))
                or product.get("handle")
                or ""
            ).strip()
            if translated_handle:
                preview_urls[locale] = (
                    f"{parsed_storefront.scheme}://{parsed_storefront.netloc}/"
                    f"{locale_path}/products/{quote(translated_handle, safe='-')}"
                )

    return {
        "product": product,
        "locales": list(TARGET_LOCALES),
        "fields": fields,
        "preview_urls": preview_urls,
    }


def _theme_resource_label(resource_id: str, resource_type: str) -> str:
    decoded = unquote(resource_id)
    parsed = urlparse(decoded)
    query = parse_qs(parsed.query)
    key = (query.get("key") or [""])[0]
    if key:
        return key
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if tail:
        return tail
    return resource_type.replace("ONLINE_STORE_THEME_", "").replace("_", " ").title()


async def _theme_documents(theme_id: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    main_theme = await assert_approved_main_theme(theme_id)
    bundle = await fetch_theme_source_bundle(resource_types=list(THEME_RESOURCE_TYPES))
    built = build_theme_documents(
        shop_domain=SETTINGS.shopify_domain,
        theme_id=str(theme_id),
        source_locale="it",
        bundle=bundle,
    )
    return main_theme, [document for document, _hashes in built]


async def list_theme_resources(theme_id: object) -> dict[str, Any]:
    approved_id = str(theme_id or "").strip()
    main_theme, documents = await _theme_documents(approved_id)
    resources = []
    for document in documents:
        entries = document.get("entries") or {}
        resources.append(
            {
                "resource_id": str(document.get("resource_id") or ""),
                "resource_type": str(document.get("resource_type") or ""),
                "label": _theme_resource_label(
                    str(document.get("resource_id") or ""),
                    str(document.get("resource_type") or ""),
                ),
                "field_count": len(entries),
            }
        )
    resources.sort(key=lambda item: (item["label"].casefold(), item["resource_type"]))
    return {
        "theme": {
            "id": approved_id,
            "name": main_theme.get("name") or "",
            "role": main_theme.get("role") or "",
            "updated_at": main_theme.get("updated_at") or "",
        },
        "resources": resources,
    }


async def inspect_theme_resource(theme_id: object, resource_id: object) -> dict[str, Any]:
    approved_id = str(theme_id or "").strip()
    requested_id = str(resource_id or "").strip()
    main_theme, documents = await _theme_documents(approved_id)
    document = next(
        (item for item in documents if str(item.get("resource_id") or "") == requested_id),
        None,
    )
    if not document:
        raise AdminContentError(
            "THEME_RESOURCE_NOT_FOUND",
            "Questa risorsa non appartiene al tema MAIN autorizzato.",
        )
    matrix = await get_resource_translation_matrix([requested_id], list(TARGET_LOCALES))
    resource_type = str(document.get("resource_type") or "")
    label = _theme_resource_label(requested_id, resource_type)
    fields = [
        _field_payload(
            resource_id=requested_id,
            resource_label=label,
            group="theme",
            key=key,
            source=source,
            matrix=matrix,
        )
        for key, source in (document.get("entries") or {}).items()
    ]
    return {
        "theme": {
            "id": approved_id,
            "name": main_theme.get("name") or "",
            "role": main_theme.get("role") or "",
        },
        "resource": {
            "resource_id": requested_id,
            "resource_type": resource_type,
            "label": label,
        },
        "locales": list(TARGET_LOCALES),
        "fields": fields,
    }


def _same_json_shape(source: Any, translated: Any) -> bool:
    if type(source) is not type(translated):
        return False
    if isinstance(source, dict):
        return set(source) == set(translated) and all(
            _same_json_shape(source[key], translated[key]) for key in source
        )
    if isinstance(source, list):
        return len(source) == len(translated) and all(
            _same_json_shape(left, right) for left, right in zip(source, translated, strict=False)
        )
    if isinstance(source, str):
        return isinstance(translated, str)
    return source == translated


def _manual_value_issue(source: str, value: str, *, kind: str, key: str) -> str | None:
    if not value.strip():
        return "La traduzione non può essere vuota."
    if len(value) > 100_000:
        return "La traduzione supera il limite di sicurezza."
    if kind == "handle" or key == "handle":
        if not validate_handle(value):
            return "L’handle deve contenere solo lettere minuscole, numeri e trattini."
        return None
    if kind == "json":
        try:
            source_json = json.loads(source)
            translated_json = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return "Il JSON non è valido."
        if not _same_json_shape(source_json, translated_json):
            return "La struttura JSON, le chiavi e i valori tecnici devono restare invariati."
    source_liquid = LIQUID_RE.findall(source)
    translated_liquid = LIQUID_RE.findall(value)
    if source_liquid != translated_liquid:
        return "I token Liquid devono restare identici e nello stesso ordine."
    if kind == "html":
        source_tags = [(close, tag.lower()) for close, tag in HTML_TAG_RE.findall(source)]
        translated_tags = [(close, tag.lower()) for close, tag in HTML_TAG_RE.findall(value)]
        if source_tags != translated_tags:
            return "La struttura dei tag HTML deve restare invariata."
    generic_issue = translation_output_issue(source, value)
    if generic_issue:
        return f"Contenuto non sicuro ({generic_issue})."
    return None


async def _save_from_inspection(
    *,
    inspection: dict[str, Any],
    resource_id: object,
    key: object,
    locale: object,
    value: object,
    digest: object,
) -> dict[str, Any]:
    requested_resource_id = str(resource_id or "").strip()
    requested_key = str(key or "").strip()
    requested_locale = str(locale or "").strip().lower()
    requested_value = str(value or "")
    requested_digest = str(digest or "").strip()
    if requested_locale not in TARGET_LOCALES:
        raise AdminContentError("INVALID_LOCALE", "Lingua non autorizzata.")

    field = next(
        (
            item
            for item in inspection.get("fields") or []
            if item.get("resource_id") == requested_resource_id and item.get("key") == requested_key
        ),
        None,
    )
    if not field:
        raise AdminContentError(
            "FIELD_NOT_FOUND",
            "Il campo non appartiene più alla risorsa autorizzata.",
        )
    current_digest = str((field.get("source") or {}).get("digest") or "")
    if not requested_digest or requested_digest != current_digest:
        raise AdminContentError(
            "SOURCE_CHANGED",
            "Il contenuto italiano è cambiato. Ricarica prima di salvare.",
        )
    issue = _manual_value_issue(
        str((field.get("source") or {}).get("value") or ""),
        requested_value,
        kind=str(field.get("content_kind") or "plain"),
        key=requested_key,
    )
    if issue:
        raise AdminContentError("UNSAFE_TRANSLATION", issue)

    errors = await register_translations(
        requested_resource_id,
        [
            {
                "locale": requested_locale,
                "key": requested_key,
                "value": requested_value,
                "translatableContentDigest": current_digest,
            }
        ],
    )
    if errors:
        message = "; ".join(str(item.get("message") or "Errore Shopify") for item in errors)
        raise AdminContentError("SHOPIFY_REJECTED", message)
    verified = await get_resource_translation_matrix(
        [requested_resource_id],
        [requested_locale],
    )
    item = ((verified.get(requested_resource_id) or {}).get(requested_locale) or {}).get(
        requested_key
    ) or {}
    return {
        "resource_id": requested_resource_id,
        "key": requested_key,
        "locale": requested_locale,
        "value": str(item.get("value") or requested_value),
        "status": _translation_status(item),
    }


async def save_product_translation(payload: dict[str, Any]) -> dict[str, Any]:
    inspection = await inspect_product(payload.get("product_id"))
    result = await _save_from_inspection(
        inspection=inspection,
        **{key: payload.get(key) for key in ("resource_id", "key", "locale", "value", "digest")},
    )
    return {"product": inspection["product"], "saved": result}


async def save_theme_translation(payload: dict[str, Any]) -> dict[str, Any]:
    theme_id = str(payload.get("theme_id") or "").strip()
    if str(payload.get("confirmation") or "").strip() != f"SAVE {theme_id}":
        raise AdminContentError(
            "CONFIRMATION_MISMATCH",
            f"Scrivi SAVE {theme_id} per confermare.",
        )
    inspection = await inspect_theme_resource(theme_id, payload.get("resource_id"))
    result = await _save_from_inspection(
        inspection=inspection,
        **{key: payload.get(key) for key in ("resource_id", "key", "locale", "value", "digest")},
    )
    return {"theme": inspection["theme"], "resource": inspection["resource"], "saved": result}


async def handle_admin_content_action(
    action: str,
    payload: dict[str, Any],
    *,
    approved_theme_id: str,
) -> dict[str, Any]:
    if action == "content_product_search":
        return {
            "products": await search_products(
                normalize_product_search_query(payload.get("query")),
                first=20,
            )
        }
    if action == "content_product_inspect":
        return await inspect_product(payload.get("product_id"))
    if action == "content_product_save":
        return await save_product_translation(payload)
    if action == "content_theme_resources":
        return await list_theme_resources(approved_theme_id)
    if action == "content_theme_inspect":
        return await inspect_theme_resource(approved_theme_id, payload.get("resource_id"))
    if action == "content_theme_save":
        if str(payload.get("theme_id") or "") != approved_theme_id:
            raise AdminContentError("THEME_MISMATCH", "Tema non autorizzato.")
        return await save_theme_translation(payload)
    raise AdminContentError("INVALID_ACTION", "Operazione contenuti non riconosciuta.")

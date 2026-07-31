from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import typer

from src.bootstrap.catalog import bootstrap_products
from src.bootstrap.handles import (
    audit_product_handles,
    complete_blocked_product_handles,
    repair_product_handles,
)
from src.bootstrap.incremental import sync_products_incremental
from src.bootstrap.reconcile import (
    prune_blank_product_type_states,
    reconcile_product_translation_states,
)
from src.bootstrap.resources import DEFAULT_RESOURCE_TYPES, bootstrap_resources
from src.bootstrap.seo import audit_product_seo, sync_catalog_seo
from src.bootstrap.theme import (
    THEME_RESOURCE_TYPES,
    audit_theme_translations,
    bootstrap_theme,
)
from src.bootstrap.theme_tracking import track_main_theme_read_only
from src.config.settings import SETTINGS
from src.logging_setup import configure_logging
from src.shopify.graphql import list_translatable_resources
from src.state.neon import NeonTranslationStore
from src.translate.canary import run_model_canary

configure_logging()

app = typer.Typer(add_completion=False, help="Shopify product translation CLI")
cache_app = typer.Typer(help="Local translation-cache utilities")


# --- Cache utilities ---------------------------------------------------------
def _resolve_cache_path(override: Path | None = None) -> str:
    if override is not None:
        return str(override)
    env = os.getenv("TRANSLATION_CACHE_PATH")
    if env and env.strip():
        return env.strip()
    return ":memory:"


@cache_app.command("info")
def cache_info(
    db_path: Path | None = typer.Option(None, "--db-path", help="Path cache locale"),  # noqa: B008
):
    """Show effective cache DB path and table stats."""
    path = _resolve_cache_path(db_path)
    if path == ":memory:":
        print(
            json.dumps(
                {"path": path, "exists": False, "ephemeral": True, "tables": {}}, ensure_ascii=False
            )
        )
        return
    path_obj = Path(path)
    exists = path_obj.exists()
    info = {
        "path": path,
        "exists": exists,
        "size_bytes": (path_obj.stat().st_size if exists else 0),
        "tables": {},
    }
    try:
        import sqlite3

        if exists:
            conn = sqlite3.connect(path_obj)
            cur = conn.cursor()
            # list tables
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    cnt = int(cur.fetchone()[0])
                except Exception:
                    cnt = None  # view or unreadable
                info["tables"][t] = cnt
            conn.close()
    except Exception as e:  # pragma: no cover - defensive
        info["error"] = str(e)

    import json as _json

    print(_json.dumps(info, ensure_ascii=False))


@cache_app.command("purge")
def cache_purge(
    db_path: Path | None = typer.Option(None, "--db-path", help="Path cache locale"),  # noqa: B008
    all: bool = typer.Option(
        False,
        "--all/--translations-only",
        help="Pulisce sia translations sia tabelle ausiliarie (default: solo translations)",
    ),  # noqa: B008
    vacuum: bool = typer.Option(
        False, "--vacuum/--no-vacuum", help="Run VACUUM after purge"
    ),  # noqa: B008
):
    """Pulisce la cache locale del traduttore."""
    path = _resolve_cache_path(db_path)
    if path == ":memory:":
        typer.echo("Default cache is in-memory; nothing persistent to purge.")
        return
    path_obj = Path(path)
    # If DB doesn't exist, nothing to do
    if not path_obj.exists():
        typer.echo(f"No DB at {path_obj}; nothing to purge.")
        return
    import sqlite3

    conn = sqlite3.connect(path_obj)
    try:
        with conn:
            # remove translations table rows or whole table if schema changed
            try:
                conn.execute("DELETE FROM translations")
            except Exception:
                conn.execute("DROP TABLE IF EXISTS translations")
            # also clear cell_cache
            try:
                conn.execute("DELETE FROM cell_cache")
            except Exception:
                conn.execute("DROP TABLE IF EXISTS cell_cache")
            if all:
                conn.execute("DROP TABLE IF EXISTS snapshot_translatable")
        if vacuum:
            try:
                conn.execute("VACUUM")
            except Exception:
                pass
    finally:
        conn.close()
    typer.echo(f"Purged cache at {path_obj} (all={all}, vacuum={vacuum}).")


app.add_typer(cache_app, name="cache")


def _parse_product_ids(product_id: list[int], ids_file: Path | None = None) -> list[int]:
    def _clean_csv_token(value: object) -> str:
        return str(value).strip().strip('"').strip("'")

    out = [int(x) for x in product_id]
    if ids_file:
        text = ids_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        if lines:
            header = [_clean_csv_token(part).lower() for part in lines[0].split(",")]
            if "id" in header or "product_id" in header:
                import csv

                reader = csv.DictReader(lines)
                for row in reader:
                    raw = (
                        (row.get("ID") or row.get("id"))
                        or (row.get("product_id") or row.get("PRODUCT_ID"))
                        or ""
                    )
                    s = _clean_csv_token(raw)
                    if not s:
                        continue
                    out.append(int(s))
            else:
                for line in lines:
                    s = _clean_csv_token(line)
                    if not s or s.startswith("#"):
                        continue
                    out.append(int(s))
    return sorted(set(out))


@app.command("theme-translatables")
def theme_translatables_cmd(
    resource_type: list[str] = typer.Option(
        [
            "ONLINE_STORE_THEME_SETTINGS_DATA_SECTIONS",
            "ONLINE_STORE_THEME_JSON_TEMPLATE",
            "ONLINE_STORE_THEME_SECTION_GROUP",
            "ONLINE_STORE_THEME_LOCALE_CONTENT",
        ],
        "--resource-type",
        help="TranslatableResourceType del tema (ripetibile)",
    ),  # noqa: B008
    first: int = typer.Option(
        50, "--first", min=1, max=250, help="Numero massimo per tipo"
    ),  # noqa: B008
    key_filter: str | None = typer.Option(
        None,
        "--key-filter",
        help="Filtra solo i translatableContent.key che contengono questa stringa",
    ),  # noqa: B008
    value_filter: str | None = typer.Option(
        None,
        "--value-filter",
        help="Filtra solo i translatableContent.value che contengono questa stringa",
    ),  # noqa: B008
):
    """Ispeziona i translatable del tema via Shopify GraphQL."""

    async def _run() -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for rt in resource_type:
            nodes, page_info = await list_translatable_resources(resource_type=rt, first=first)
            rows: list[dict] = []
            for node in nodes:
                content = node.get("translatableContent") or []
                if key_filter:
                    content = [
                        x for x in content if key_filter.lower() in str(x.get("key") or "").lower()
                    ]
                if value_filter:
                    content = [
                        x
                        for x in content
                        if value_filter.lower() in str(x.get("value") or "").lower()
                    ]
                if not content:
                    continue
                rows.append(
                    {
                        "resourceId": node.get("resourceId"),
                        "translatableContent": content,
                    }
                )
            out[rt] = {
                "nodes": rows,
                "pageInfo": page_info,
            }
        return out

    import json as _json

    print(_json.dumps(asyncio.run(_run()), ensure_ascii=False))


@app.command("theme-bootstrap")
def theme_bootstrap_cmd(
    theme_id: str = typer.Option(
        ..., "--theme-id", help="ID numerico del tema Shopify"
    ),  # noqa: B008
    target_locales: str | None = typer.Option(
        None, "--target-locales", help="Locali target separati da virgola"
    ),  # noqa: B008
    resource_type: list[str] = typer.Option(
        list(THEME_RESOURCE_TYPES),
        "--resource-type",
        help="TranslatableResourceType del tema da includere (ripetibile)",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato/memory su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Dry run"),  # noqa: B008
    max_translations: int | None = typer.Option(
        None,
        "--max-translations",
        min=1,
        help="Canary: limita il numero totale di campi tradotti/registrati",
    ),  # noqa: B008
    force_key_fragment: list[str] = typer.Option(
        [],
        "--force-key-fragment",
        help="Ritraduce solo chiavi che contengono il frammento (ripetibile)",
    ),  # noqa: B008
):
    """Bootstrap del theme editor content su Neon, con apply opzionale su Shopify."""
    tl = (
        [x.strip() for x in target_locales.split(",") if x.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(
        bootstrap_theme(
            theme_id=theme_id,
            target_locales=tl,
            source_locale=SETTINGS.source_locale,
            apply_translations=apply_translations,
            dry_run=dry_run,
            resource_types=resource_type or list(THEME_RESOURCE_TYPES),
            max_translations=max_translations,
            force_key_fragments=force_key_fragment or None,
        )
    )
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


@app.command("theme-audit")
def theme_audit_cmd(
    theme_id: str = typer.Option(
        ..., "--theme-id", help="ID numerico del tema Shopify"
    ),  # noqa: B008
    target_locales: str | None = typer.Option(
        None,
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    resource_type: list[str] = typer.Option(
        list(THEME_RESOURCE_TYPES),
        "--resource-type",
        help="TranslatableResourceType del tema da includere (ripetibile)",
    ),  # noqa: B008
    include_items: bool = typer.Option(
        False,
        "--include-items/--summary-only",
        help="Include il dettaglio dei campi mancanti o obsoleti",
    ),  # noqa: B008
    key_filter: str | None = typer.Option(
        None,
        "--key-filter",
        help="Limita l’audit alle chiavi che contengono questo testo",
    ),  # noqa: B008
):
    """Audit Shopify read-only: copertura delle traduzioni del solo tema MAIN approvato."""
    locales = (
        [item.strip() for item in target_locales.split(",") if item.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(
        audit_theme_translations(
            theme_id=theme_id,
            target_locales=locales,
            source_locale=SETTINGS.source_locale,
            resource_types=resource_type or list(THEME_RESOURCE_TYPES),
            include_items=include_items,
            key_filter=key_filter,
        )
    )
    print(json.dumps(out, ensure_ascii=False))


@app.command("resources-bootstrap")
def resources_bootstrap_cmd(
    target_locales: str | None = typer.Option(
        None, "--target-locales", help="Locali target separati da virgola"
    ),  # noqa: B008
    resource_type: list[str] = typer.Option(
        list(DEFAULT_RESOURCE_TYPES),
        "--resource-type",
        help="TranslatableResourceType globale da includere (ripetibile)",
    ),  # noqa: B008
    resource_id: list[str] = typer.Option(
        [],
        "--resource-id",
        help="ResourceId Shopify da includere (ripetibile)",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato/memory su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Dry run"),  # noqa: B008
):
    """Bootstrap di risorse globali Shopify: policy, cookie banner, branding."""
    tl = (
        [x.strip() for x in target_locales.split(",") if x.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(
        bootstrap_resources(
            target_locales=tl,
            source_locale=SETTINGS.source_locale,
            apply_translations=apply_translations,
            dry_run=dry_run,
            resource_types=resource_type or list(DEFAULT_RESOURCE_TYPES),
            resource_ids=resource_id or None,
        )
    )
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


@app.command("bootstrap-products")
def bootstrap_products_cmd(
    product_id: list[int] = typer.Option(
        [], "--product-id", help="ID numerico prodotto (ripetibile)"
    ),  # noqa: B008
    ids_file: Path | None = typer.Option(
        None,
        "--ids-file",
        help="File con un product ID per riga",
    ),  # noqa: B008
    target_locales: str | None = typer.Option(
        None, "--target-locales", help="Locali target separati da virgola"
    ),  # noqa: B008
    mf_include: str | None = typer.Option(
        None,
        "--mf-include",
        help="Lista namespace.key separati da virgola; vuoto = auto-discovery",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        SETTINGS.bootstrap_apply_translations,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato/memory su Neon",
    ),  # noqa: B008
    existing_products: bool = typer.Option(
        SETTINGS.bootstrap_existing_products,
        "--existing-products/--new-products",
        help="Bootstrap prudente per catalogo esistente oppure onboarding di prodotti nuovi",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Audit read-only: non chiama OpenAI e non scrive su Shopify o Neon",
    ),  # noqa: B008
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--fail-fast",
        help="Continua col batch anche se un prodotto fallisce",
    ),  # noqa: B008
    handle_only: bool = typer.Option(
        False,
        "--handle-only",
        help="Genera/applica solo product.handle usando i titoli già tradotti",
    ),  # noqa: B008
):
    """Bootstrap field-by-field del catalogo da product IDs, con stato persistito su Neon."""
    resolved_ids_file = ids_file or Path(SETTINGS.bootstrap_ids_file)
    ids = _parse_product_ids(product_id, resolved_ids_file if resolved_ids_file.exists() else None)
    if not ids:
        raise typer.BadParameter("Serve almeno un --product-id o --ids-file")

    tl = (
        [x.strip() for x in target_locales.split(",") if x.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    mf_inc = (
        [
            (a.strip(), b.strip())
            for a, b in (s.split(".", 1) for s in mf_include.split(",") if "." in s)
        ]
        if mf_include
        else []
    )

    out = asyncio.run(
        bootstrap_products(
            product_ids=ids,
            target_locales=tl,
            mf_include=mf_inc or None,
            source_locale=SETTINGS.source_locale,
            apply_translations=apply_translations,
            dry_run=dry_run,
            existing_products=existing_products,
            is_create=not existing_products,
            handle_only=handle_only,
            continue_on_error=continue_on_error,
        )
    )
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


@app.command("bootstrap")
def bootstrap_alias_cmd(
    product_id: list[int] = typer.Option(
        [], "--product-id", help="ID numerico prodotto (ripetibile)"
    ),  # noqa: B008
    ids_file: Path | None = typer.Option(
        None, "--ids-file", help="File ID oppure CSV con header ID"
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        SETTINGS.bootstrap_apply_translations,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Dry run"),  # noqa: B008
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--fail-fast",
        help="Continua col batch anche se un prodotto fallisce",
    ),  # noqa: B008
):
    """Alias corto del bootstrap PDP-based."""
    bootstrap_products_cmd(
        product_id=product_id,
        ids_file=ids_file,
        target_locales=None,
        mf_include=None,
        apply_translations=apply_translations,
        existing_products=SETTINGS.bootstrap_existing_products,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
        handle_only=False,
    )


@app.command("sync-products-neon")
def sync_products_neon_cmd(
    product_id: list[int] = typer.Option(
        [], "--product-id", help="ID numerico prodotto (ripetibile)"
    ),  # noqa: B008
    ids_file: Path | None = typer.Option(
        None,
        "--ids-file",
        help="File con un product ID per riga",
    ),  # noqa: B008
    target_locales: str | None = typer.Option(
        None, "--target-locales", help="Locali target separati da virgola"
    ),  # noqa: B008
    mf_include: str | None = typer.Option(
        None,
        "--mf-include",
        help="Lista namespace.key separati da virgola; vuoto = auto-discovery",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato/memory su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Audit read-only: non chiama OpenAI e non scrive su Shopify o Neon",
    ),  # noqa: B008
    handle_only: bool = typer.Option(
        False,
        "--handle-only",
        help="Genera/applica solo product.handle usando i titoli già tradotti",
    ),  # noqa: B008
    reconcile_shopify_drift: bool = typer.Option(
        False,
        "--reconcile-shopify-drift",
        help="In store-only rigenera anche sezioni Shopify mancanti, obsolete o non valide",
    ),  # noqa: B008
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--fail-fast",
        help="Continua col batch e riporta i prodotti falliti",
    ),  # noqa: B008
):
    """Sync incrementale field-by-field basata su Neon/PostgreSQL."""
    ids = _parse_product_ids(product_id, ids_file)
    if not ids:
        raise typer.BadParameter("Serve almeno un --product-id o --ids-file")

    tl = (
        [x.strip() for x in target_locales.split(",") if x.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    mf_inc = (
        [
            (a.strip(), b.strip())
            for a, b in (s.split(".", 1) for s in mf_include.split(",") if "." in s)
        ]
        if mf_include
        else []
    )

    out = asyncio.run(
        sync_products_incremental(
            product_ids=ids,
            target_locales=tl,
            mf_include=mf_inc or None,
            source_locale=SETTINGS.source_locale,
            apply_translations=apply_translations,
            dry_run=dry_run,
            is_create=False,
            handle_only=handle_only,
            reconcile_shopify_drift=reconcile_shopify_drift,
            continue_on_error=continue_on_error,
        )
    )
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


@app.command("reconcile-product-states")
def reconcile_product_states_cmd(
    target_locales: str = typer.Option(
        "de,fr",
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    candidate_statuses: str = typer.Option(
        "failed",
        "--candidate-statuses",
        help="Stati Neon da verificare, separati da virgola; usa missing per gli assenti",
    ),  # noqa: B008
    max_products: int = typer.Option(
        25,
        "--max-products",
        min=1,
        help="Numero massimo di prodotti da controllare",
    ),  # noqa: B008
    batch_size: int = typer.Option(
        25,
        "--batch-size",
        min=1,
        max=100,
        help="Prodotti caricati da Neon per ciclo",
    ),  # noqa: B008
    concurrency: int = typer.Option(
        3,
        "--concurrency",
        min=1,
        max=6,
        help="Prodotti verificati contemporaneamente su Shopify",
    ),  # noqa: B008
    apply_state: bool = typer.Option(
        False,
        "--apply-state/--dry-run",
        help="Aggiorna solo gli stati Neon; Shopify resta sempre in sola lettura",
    ),  # noqa: B008
):
    """Riconcilia gli stati Neon leggendo le traduzioni live da Shopify."""
    locales = [locale.strip().lower() for locale in target_locales.split(",") if locale.strip()]
    if not locales:
        raise typer.BadParameter("Serve almeno un locale target")
    statuses = [
        status.strip().lower() for status in candidate_statuses.split(",") if status.strip()
    ]
    if not statuses:
        raise typer.BadParameter("Serve almeno uno stato candidato")
    out = asyncio.run(
        reconcile_product_translation_states(
            target_locales=locales,
            candidate_statuses=statuses,
            max_products=max_products,
            batch_size=batch_size,
            concurrency=concurrency,
            dry_run=not apply_state,
        )
    )
    print(json.dumps(out, ensure_ascii=False))


@app.command("prune-blank-product-types")
def prune_blank_product_types_cmd(
    target_locales: str = typer.Option(
        "de,fr",
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    max_products: int = typer.Option(
        3000,
        "--max-products",
        min=1,
        help="Numero massimo di candidati partial da controllare",
    ),  # noqa: B008
    batch_size: int = typer.Option(
        250,
        "--batch-size",
        min=1,
        max=250,
        help="Product resource verificati per query batch",
    ),  # noqa: B008
    apply_state: bool = typer.Option(
        False,
        "--apply-state/--dry-run",
        help="Aggiorna soltanto Neon; Shopify resta in sola lettura",
    ),  # noqa: B008
):
    """Rimuove dagli stati Neon i product_type con sorgente Shopify vuota."""
    locales = [locale.strip().lower() for locale in target_locales.split(",") if locale.strip()]
    if not locales:
        raise typer.BadParameter("Serve almeno un locale target")
    out = asyncio.run(
        prune_blank_product_type_states(
            target_locales=locales,
            max_products=max_products,
            batch_size=batch_size,
            dry_run=not apply_state,
        )
    )
    print(json.dumps(out, ensure_ascii=False))


@app.command("sync")
def sync_alias_cmd(
    product_id: list[int] = typer.Option(
        [], "--product-id", help="ID numerico prodotto (ripetibile)"
    ),  # noqa: B008
    ids_file: Path | None = typer.Option(
        None, "--ids-file", help="File ID oppure CSV con header ID"
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Dry run"),  # noqa: B008
):
    """Alias corto della sync incrementale Neon-based."""
    sync_products_neon_cmd(
        product_id=product_id,
        ids_file=ids_file,
        target_locales=None,
        mf_include=None,
        apply_translations=apply_translations,
        dry_run=dry_run,
        handle_only=False,
        continue_on_error=True,
    )


@app.command("seo-audit")
def seo_audit_cmd(
    target_locales: str | None = typer.Option(
        None,
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
):
    """Audit read-only dei meta title/description personalizzati su Shopify."""
    locales = (
        [item.strip() for item in target_locales.split(",") if item.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(audit_product_seo(target_locales=locales))
    out.pop("candidate_product_ids", None)
    print(json.dumps(out, ensure_ascii=False))


@app.command("theme-track")
def theme_track_cmd(
    approved_theme_id: str = typer.Option(
        os.getenv("APPROVED_THEME_ID", "") or os.getenv("THEME_ID", ""),
        "--approved-theme-id",
        help="ID del tema MAIN approvato; il comando resta sempre read-only su Shopify",
    ),  # noqa: B008
):
    """Salva manifest/checksum del MAIN senza modificare file o traduzioni."""
    out = asyncio.run(
        track_main_theme_read_only(
            approved_theme_id=approved_theme_id,
            topic="manual/read-only",
        )
    )
    print(json.dumps(out, ensure_ascii=False))


@app.command("handles-audit")
def handles_audit_cmd(
    target_locales: str | None = typer.Option(
        None,
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    include_items: bool = typer.Option(
        False,
        "--include-items/--summary-only",
        help="Include operazioni candidate e casi bloccati con relativa causa",
    ),  # noqa: B008
):
    """Audit read-only degli handle localizzati; non modifica Shopify."""
    locales = (
        [item.strip() for item in target_locales.split(",") if item.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(audit_product_handles(target_locales=locales))
    if not include_items:
        out.pop("plan", None)
        out.pop("blocked_items", None)
    print(json.dumps(out, ensure_ascii=False))


@app.command("handles-repair")
def handles_repair_cmd(
    target_locales: str | None = typer.Option(
        None,
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--plan-only",
        help="Registra su Shopify solo handle mancanti o digest obsoleti",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Calcola il piano senza scrivere su Shopify",
    ),  # noqa: B008
    max_items: int | None = typer.Option(
        None,
        "--max-items",
        min=1,
        help="Limita il rollout alle prime N operazioni",
    ),  # noqa: B008
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--fail-fast",
        help="Continua il batch se un handle fallisce",
    ),  # noqa: B008
    concurrency: int = typer.Option(
        1,
        "--concurrency",
        min=1,
        max=4,
        help="Registrazioni in parallelo (massimo 4)",
    ),  # noqa: B008
    include_items: bool = typer.Option(
        False,
        "--include-items/--summary-only",
        help="Include il dettaglio delle operazioni pianificate",
    ),  # noqa: B008
):
    """Ripara gli handle senza riscrivere URL localizzati correnti."""
    locales = (
        [item.strip() for item in target_locales.split(",") if item.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(
        repair_product_handles(
            target_locales=locales,
            apply_translations=apply_translations,
            dry_run=dry_run,
            max_items=max_items,
            continue_on_error=continue_on_error,
            concurrency=concurrency,
        )
    )
    if not include_items:
        out.pop("items", None)
    print(json.dumps(out, ensure_ascii=False))


@app.command("handles-complete")
def handles_complete_cmd(
    target_locales: str | None = typer.Option(
        None,
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--plan-only",
        help="Registra i titoli mancanti e gli handle univoci su Shopify",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Audit puro: non chiama OpenAI e non scrive su Shopify o Neon",
    ),  # noqa: B008
    max_items: int | None = typer.Option(
        None,
        "--max-items",
        min=1,
        help="Limita il rollout alle prime N correzioni",
    ),  # noqa: B008
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--fail-fast",
        help="Continua il batch se una correzione fallisce",
    ),  # noqa: B008
    include_items: bool = typer.Option(
        False,
        "--include-items/--summary-only",
        help="Include titolo e handle prodotti per ogni correzione",
    ),  # noqa: B008
):
    """Completa titoli mancanti e collisioni degli handle localizzati."""
    locales = (
        [item.strip() for item in target_locales.split(",") if item.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(
        complete_blocked_product_handles(
            target_locales=locales,
            apply_translations=apply_translations,
            dry_run=dry_run,
            max_items=max_items,
            continue_on_error=continue_on_error,
        )
    )
    if not include_items:
        out.pop("items", None)
    print(json.dumps(out, ensure_ascii=False))


@app.command("seo-sync")
def seo_sync_cmd(
    target_locales: str | None = typer.Option(
        None,
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
    apply_translations: bool = typer.Option(
        False,
        "--apply-translations/--store-only",
        help="Registra su Shopify oppure genera soltanto memory su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Solo piano: niente OpenAI e nessuna scrittura Shopify/Neon",
    ),  # noqa: B008
    max_products: int | None = typer.Option(
        None,
        "--max-products",
        min=1,
        help="Limita il rollout ai primi N prodotti candidati",
    ),  # noqa: B008
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--fail-fast",
        help="Continua il batch se un prodotto fallisce",
    ),  # noqa: B008
    concurrency: int = typer.Option(
        1,
        "--concurrency",
        min=1,
        max=4,
        help="Prodotti elaborati in parallelo (massimo 4)",
    ),  # noqa: B008
    include_items: bool = typer.Option(
        False,
        "--include-items/--summary-only",
        help="Include il dettaglio per prodotto; di default stampa solo il riepilogo",
    ),  # noqa: B008
):
    """Sincronizza solo SEO custom mancante o obsoleta."""
    locales = (
        [item.strip() for item in target_locales.split(",") if item.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    out = asyncio.run(
        sync_catalog_seo(
            target_locales=locales,
            source_locale=SETTINGS.source_locale,
            apply_translations=apply_translations,
            dry_run=dry_run,
            max_products=max_products,
            continue_on_error=continue_on_error,
            concurrency=concurrency,
        )
    )
    if not include_items:
        out["item_count"] = len(out.get("items") or [])
        out.pop("items", None)
    print(json.dumps(out, ensure_ascii=False))


@app.command("model-canary")
def model_canary_cmd(
    models: str = typer.Option(
        "gpt-5.6-terra,gpt-5.6-sol",
        "--models",
        help="Modelli separati da virgola",
    ),  # noqa: B008
    target_locales: str = typer.Option(
        "de,fr",
        "--target-locales",
        help="Locali target separati da virgola",
    ),  # noqa: B008
):
    """Canary OpenAI su testo/HTML/JSON; zero chiamate Shopify e zero scritture Neon."""
    out = run_model_canary(
        models=[item.strip() for item in models.split(",") if item.strip()],
        target_locales=[item.strip() for item in target_locales.split(",") if item.strip()],
    )
    print(json.dumps(out, ensure_ascii=False))


@app.command("neon-reset")
def neon_reset_cmd(
    yes: bool = typer.Option(
        False, "--yes", help="Conferma il reset dello schema Neon"
    ),  # noqa: B008
):
    """Resetta lo schema del nuovo backend Neon/PostgreSQL."""
    if not yes:
        raise typer.BadParameter("Passa --yes per confermare il reset dello schema Neon")
    store = NeonTranslationStore()
    try:
        store.reset_schema()
    finally:
        store.close()
    typer.echo("Schema Neon resettato.")


@app.command("sync-webhook")
def sync_webhook(
    disable: bool = typer.Option(
        True, "--disable/--enable", help="Disabilita/abilita la sync webhook"
    ),  # noqa: B008
    target: str = typer.Option("both", "--target", help="receiver|worker|both"),  # noqa: B008
    project: str | None = typer.Option(
        None,
        "--project",
        help="Prefisso 'project' usato da Terraform per dedurre i nomi (es. <project>-shopify-...)",
    ),  # noqa: B008
    tfvars_path: Path = typer.Option(
        Path("infra/terraform/terraform.tfvars"),
        "--tfvars-path",
        help="Percorso a terraform.tfvars per dedurre automaticamente project",
    ),  # noqa: B008
    tfstate_path: Path = typer.Option(
        Path("infra/terraform/terraform.tfstate"),
        "--tfstate-path",
        help="Percorso a terraform.tfstate per dedurre nomi o project",
    ),  # noqa: B008
    receiver_name: str | None = typer.Option(
        None, "--receiver-name", help="Override nome Lambda receiver"
    ),  # noqa: B008
    worker_name: str | None = typer.Option(
        None, "--worker-name", help="Override nome Lambda worker"
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Mostra cosa farebbe senza applicare"
    ),  # noqa: B008
):
    """
    Abilita/Disabilita la sync lato Lambda impostando DISABLE_SYNC su receiver/worker.
    Se non passi i nomi, prova a dedurli da --project secondo lo schema Terraform:
      <project>-shopify-webhook-receiver / <project>-shopify-webhook-worker
    In alternativa usa env RECEIVER_LAMBDA_NAME/WORKER_LAMBDA_NAME.
    """
    tgt = (target or "").strip().lower()
    if tgt not in {"receiver", "worker", "both"}:
        raise typer.BadParameter("--target deve essere receiver|worker|both")

    def _read_project_from_tfvars(p: Path) -> str | None:
        try:
            if not p.exists():
                return None
            txt = p.read_text(encoding="utf-8")
            import re as _re

            m = _re.search(r"^\s*project\s*=\s*\"([^\"]+)\"", txt, flags=_re.MULTILINE)
            return m.group(1).strip() if m else None
        except Exception:
            return None

    def _read_names_from_tfstate(p: Path) -> tuple[str | None, str | None, str | None]:
        """Ritorna (receiver_name, worker_name, project) deducendo da tfstate se possibile."""
        try:
            if not p.exists():
                return (None, None, None)
            import json as _json

            data = _json.loads(p.read_text(encoding="utf-8"))
            resources = data.get("resources") or []
            rn = None
            wn = None
            proj = None
            for r in resources:
                if r.get("type") == "aws_lambda_function":
                    for inst in r.get("instances") or []:
                        attrs = inst.get("attributes") or {}
                        fn = attrs.get("function_name") or ""
                        if fn.endswith("-shopify-webhook-receiver"):
                            rn = fn
                            if "-shopify-webhook-receiver" in fn:
                                proj = fn[: fn.rfind("-shopify-webhook-receiver")]
                        if fn.endswith("-shopify-webhook-worker"):
                            wn = fn
                            if "-shopify-webhook-worker" in fn and proj is None:
                                proj = fn[: fn.rfind("-shopify-webhook-worker")]
                if r.get("type") == "aws_dynamodb_table":
                    for inst in r.get("instances") or []:
                        attrs = inst.get("attributes") or {}
                        name = attrs.get("name") or ""
                        if name.endswith("-shopify-product-snapshots") and proj is None:
                            proj = name[: name.rfind("-shopify-product-snapshots")]
            return (rn, wn, proj)
        except Exception:
            return (None, None, None)

    def _deduce_names() -> tuple[str | None, str | None]:
        rn = receiver_name or os.getenv("RECEIVER_LAMBDA_NAME")
        wn = worker_name or os.getenv("WORKER_LAMBDA_NAME")
        if (rn and wn) or (tgt == "receiver" and rn) or (tgt == "worker" and wn):
            return rn, wn
        proj = project or _read_project_from_tfvars(tfvars_path)
        if not proj:
            rn2, wn2, proj2 = _read_names_from_tfstate(tfstate_path)
            # Se tfstate ha già i nomi, usiamoli direttamente
            rname = rn or rn2
            wname = wn or wn2
            if (tgt in {"receiver", "both"} and not rname) or (
                tgt in {"worker", "both"} and not wname
            ):
                # come fallback, prova a costruire dai proj se presente
                proj = proj2
            else:
                return rname, wname
        if proj:
            base = f"{proj}-shopify"
            rn2 = f"{base}-webhook-receiver"
            wn2 = f"{base}-webhook-worker"
            return rn or (rn2 if tgt in {"receiver", "both"} else None), wn or (
                wn2 if tgt in {"worker", "both"} else None
            )
        # Fallback estremo: prova a individuare da AWS le funzioni con suffisso noto
        try:
            import boto3  # type: ignore

            lam = boto3.client("lambda")
            funcs = []
            paginator = lam.get_paginator("list_functions")
            for page in paginator.paginate():
                funcs.extend(page.get("Functions") or [])
            cand_r = [
                f["FunctionName"]
                for f in funcs
                if f.get("FunctionName", "").endswith("-shopify-webhook-receiver")
            ]
            cand_w = [
                f["FunctionName"]
                for f in funcs
                if f.get("FunctionName", "").endswith("-shopify-webhook-worker")
            ]
            rname = rn or (cand_r[0] if cand_r else None)
            wname = wn or (cand_w[0] if cand_w else None)
            return rname if tgt in {"receiver", "both"} else None, (
                wname if tgt in {"worker", "both"} else None
            )
        except Exception:
            return rn, wn
        return rn, wn

    rn, wn = _deduce_names()
    missing: list[str] = []
    if tgt in {"receiver", "both"} and not rn:
        missing.append("receiver_name o --project")
    if tgt in {"worker", "both"} and not wn:
        missing.append("worker_name o --project")
    if missing:
        raise typer.BadParameter("Servono " + ", ".join(missing))

    desired = "true" if disable else "false"
    pairs: list[tuple[str, str]] = []
    if tgt in {"receiver", "both"} and rn:
        pairs.append(("receiver", rn))
    if tgt in {"worker", "both"} and wn:
        pairs.append(("worker", wn))

    if dry_run:
        for kind, fn in pairs:
            typer.echo(f"[dry-run] {kind}: set DISABLE_SYNC={desired} on {fn}")
        return

    try:
        import boto3  # type: ignore

        lam = boto3.client("lambda")
        for kind, fn in pairs:
            cfg = lam.get_function_configuration(FunctionName=fn)
            env = (cfg.get("Environment") or {}).get("Variables") or {}
            env["DISABLE_SYNC"] = desired
            lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
            typer.echo(f"[{kind}] DISABLE_SYNC={desired} impostato su {fn}")
    except Exception as e:  # pragma: no cover
        typer.echo(f"Errore aggiornando le Lambda: {e}")
        typer.echo("Comandi AWS CLI equivalenti:")
        for _kind, fn in pairs:
            typer.echo(
                "aws lambda update-function-configuration --function-name "
                + fn
                + " --environment 'Variables={DISABLE_SYNC="
                + desired
                + "}'"
            )

    # (sync-toggle command rimosso su richiesta)


@app.command("theme-poll-cloud")
def theme_poll_cloud(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Prefisso 'project' usato da Terraform per dedurre il nome Lambda",
    ),  # noqa: B008
    tfvars_path: Path = typer.Option(
        Path("infra/terraform/terraform.tfvars"),
        "--tfvars-path",
        help="Percorso a terraform.tfvars per dedurre automaticamente project",
    ),  # noqa: B008
    tfstate_path: Path = typer.Option(
        Path("infra/terraform/terraform.tfstate"),
        "--tfstate-path",
        help="Percorso a terraform.tfstate per dedurre i nomi",
    ),  # noqa: B008
    function_name: str | None = typer.Option(
        None, "--function-name", help="Override nome Lambda theme-poller"
    ),  # noqa: B008
    global_resource_type: list[str] = typer.Option(
        [],
        "--global-resource-type",
        help="Override resource type globale da processare in cloud (ripetibile)",
    ),  # noqa: B008
    theme: bool = typer.Option(
        True, "--theme/--no-theme", help="Esegui anche il polling del tema"
    ),  # noqa: B008
    global_resources: bool = typer.Option(
        False,
        "--global-resources/--no-global-resources",
        help="Esegui polling risorse globali; implicito se passi --global-resource-type",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Invoca il poller in dry run"
    ),  # noqa: B008
    sync: bool = typer.Option(
        False, "--sync/--async", help="Attendi risposta del poller"
    ),  # noqa: B008
):
    """
    Invoca la Lambda di polling tema direttamente in cloud.
    Usa --project oppure --function-name. Default: invocazione async.
    """

    def _read_project_from_tfvars(p: Path) -> str | None:
        try:
            if not p.exists():
                return None
            txt = p.read_text(encoding="utf-8")
            import re as _re

            m = _re.search(r"^\s*project\s*=\s*\"([^\"]+)\"", txt, flags=_re.MULTILINE)
            return m.group(1).strip() if m else None
        except Exception:
            return None

    def _read_name_from_tfstate(p: Path) -> str | None:
        try:
            if not p.exists():
                return None
            import json as _json

            data = _json.loads(p.read_text(encoding="utf-8"))
            resources = data.get("resources") or []
            for r in resources:
                if r.get("type") == "aws_lambda_function":
                    for inst in r.get("instances") or []:
                        attrs = inst.get("attributes") or {}
                        fn = attrs.get("function_name") or ""
                        if fn.endswith("-shopify-theme-poller"):
                            return fn
            return None
        except Exception:
            return None

    fn = function_name or os.getenv("THEME_POLLER_LAMBDA_NAME")
    if not fn:
        proj = project or _read_project_from_tfvars(tfvars_path)
        if not proj:
            fn = _read_name_from_tfstate(tfstate_path)
        if not fn and proj:
            fn = f"{proj}-shopify-theme-poller"
    if not fn:
        raise typer.BadParameter("Serve --function-name o --project (o tfvars/tfstate valido).")

    try:
        import boto3  # type: ignore

        lam = boto3.client("lambda")
        invocation = "RequestResponse" if sync else "Event"
        payload = {
            "run_theme": theme,
            "run_global_resources": bool(global_resources or global_resource_type),
            "dry_run": dry_run,
        }
        if global_resource_type:
            payload["global_resource_types"] = global_resource_type
        resp = lam.invoke(
            FunctionName=fn,
            InvocationType=invocation,
            Payload=json.dumps(payload).encode("utf-8"),
        )
        status = resp.get("StatusCode")
        typer.echo(f"Invocata {fn} ({invocation}), status={status}")
        if sync and resp.get("Payload"):
            body = resp["Payload"].read().decode("utf-8", errors="ignore")
            if body:
                typer.echo(body)
    except Exception as e:  # pragma: no cover
        typer.echo(f"Errore invocando la Lambda: {e}")


if __name__ == "__main__":
    app()

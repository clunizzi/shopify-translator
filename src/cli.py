from __future__ import annotations

from pathlib import Path
import os

import typer

from src.bootstrap.catalog import bootstrap_products
from src.bootstrap.incremental import sync_products_incremental
from src.config.settings import SETTINGS
from src.shopify.graphql import list_translatable_resources
from src.state.neon import NeonTranslationStore
import asyncio

app = typer.Typer(add_completion=False, help="Shopify product translation CLI")
cache_app = typer.Typer(help="Local translation-cache utilities")


# --- Cache utilities ---------------------------------------------------------
def _resolve_cache_path(override: Path | None = None) -> Path:
    if override is not None:
        return override
    env = os.getenv("TRANSLATION_CACHE_PATH")
    if env and env.strip():
        return Path(env.strip())
    return Path("state/cache.sqlite")


@cache_app.command("info")
def cache_info(
    db_path: Path | None = typer.Option(None, "--db-path", help="Path cache locale"),  # noqa: B008
):
    """Show effective cache DB path and table stats."""
    path = _resolve_cache_path(db_path)
    exists = path.exists()
    info = {
        "path": str(path),
        "exists": exists,
        "size_bytes": (path.stat().st_size if exists else 0),
        "tables": {},
    }
    try:
        import sqlite3

        if exists:
            conn = sqlite3.connect(path)
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
    vacuum: bool = typer.Option(False, "--vacuum/--no-vacuum", help="Run VACUUM after purge"),  # noqa: B008
):
    """Pulisce la cache locale del traduttore."""
    path = _resolve_cache_path(db_path)
    # If DB doesn't exist, nothing to do
    if not path.exists():
        typer.echo(f"No DB at {path}; nothing to purge.")
        return
    import sqlite3

    conn = sqlite3.connect(path)
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
    typer.echo(f"Purged cache at {path} (all={all}, vacuum={vacuum}).")


app.add_typer(cache_app, name="cache")


def _parse_product_ids(product_id: list[int], ids_file: Path | None = None) -> list[int]:
    out = [int(x) for x in product_id]
    if ids_file:
        text = ids_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        if lines:
            header = [part.strip().lower() for part in lines[0].split(",")]
            if "id" in header or "product_id" in header:
                import csv

                reader = csv.DictReader(lines)
                for row in reader:
                    raw = (
                        (row.get("ID") or row.get("id"))
                        or (row.get("product_id") or row.get("PRODUCT_ID"))
                        or ""
                    )
                    s = str(raw).strip()
                    if not s:
                        continue
                    out.append(int(s))
            else:
                for line in lines:
                    s = line.strip()
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
    first: int = typer.Option(50, "--first", min=1, max=250, help="Numero massimo per tipo"),  # noqa: B008
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
                    content = [x for x in content if key_filter.lower() in str(x.get("key") or "").lower()]
                if value_filter:
                    content = [x for x in content if value_filter.lower() in str(x.get("value") or "").lower()]
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


@app.command("bootstrap-products")
def bootstrap_products_cmd(
    product_id: list[int] = typer.Option([], "--product-id", help="ID numerico prodotto (ripetibile)"),  # noqa: B008
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
        help="Non chiama OpenAI/Shopify; utile per verificare fetch e stato",
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
        [(a.strip(), b.strip()) for a, b in (s.split(".", 1) for s in mf_include.split(",") if "." in s)]
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
        )
    )
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


@app.command("bootstrap")
def bootstrap_alias_cmd(
    product_id: list[int] = typer.Option([], "--product-id", help="ID numerico prodotto (ripetibile)"),  # noqa: B008
    ids_file: Path | None = typer.Option(None, "--ids-file", help="File ID oppure CSV con header ID"),  # noqa: B008
    apply_translations: bool = typer.Option(
        SETTINGS.bootstrap_apply_translations,
        "--apply-translations/--store-only",
        help="Registra le traduzioni su Shopify oppure salva solo stato su Neon",
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Dry run"),  # noqa: B008
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
    )


@app.command("sync-products-neon")
def sync_products_neon_cmd(
    product_id: list[int] = typer.Option([], "--product-id", help="ID numerico prodotto (ripetibile)"),  # noqa: B008
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
        help="Non chiama OpenAI/Shopify; utile per verificare il diff incrementale",
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
        [(a.strip(), b.strip()) for a, b in (s.split(".", 1) for s in mf_include.split(",") if "." in s)]
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
        )
    )
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


@app.command("sync")
def sync_alias_cmd(
    product_id: list[int] = typer.Option([], "--product-id", help="ID numerico prodotto (ripetibile)"),  # noqa: B008
    ids_file: Path | None = typer.Option(None, "--ids-file", help="File ID oppure CSV con header ID"),  # noqa: B008
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
    )


@app.command("neon-reset")
def neon_reset_cmd(
    yes: bool = typer.Option(False, "--yes", help="Conferma il reset dello schema Neon"),  # noqa: B008
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
    disable: bool = typer.Option(True, "--disable/--enable", help="Disabilita/abilita la sync webhook"),  # noqa: B008
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
    receiver_name: str | None = typer.Option(None, "--receiver-name", help="Override nome Lambda receiver"),  # noqa: B008
    worker_name: str | None = typer.Option(None, "--worker-name", help="Override nome Lambda worker"),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Mostra cosa farebbe senza applicare"),  # noqa: B008
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
            resources = (data.get("resources") or [])
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
            if (tgt in {"receiver", "both"} and not rname) or (tgt in {"worker", "both"} and not wname):
                # come fallback, prova a costruire dai proj se presente
                proj = proj2
            else:
                return rname, wname
        if proj:
            base = f"{proj}-shopify"
            rn2 = f"{base}-webhook-receiver"
            wn2 = f"{base}-webhook-worker"
            return rn or (rn2 if tgt in {"receiver", "both"} else None), wn or (wn2 if tgt in {"worker", "both"} else None)
        # Fallback estremo: prova a individuare da AWS le funzioni con suffisso noto
        try:
            import boto3  # type: ignore
            lam = boto3.client("lambda")
            funcs = []
            paginator = lam.get_paginator("list_functions")
            for page in paginator.paginate():
                funcs.extend(page.get("Functions") or [])
            cand_r = [f["FunctionName"] for f in funcs if f.get("FunctionName", "").endswith("-shopify-webhook-receiver")]
            cand_w = [f["FunctionName"] for f in funcs if f.get("FunctionName", "").endswith("-shopify-webhook-worker")]
            rname = rn or (cand_r[0] if cand_r else None)
            wname = wn or (cand_w[0] if cand_w else None)
            return rname if tgt in {"receiver", "both"} else None, wname if tgt in {"worker", "both"} else None
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
        for kind, fn in pairs:
            typer.echo(
                "aws lambda update-function-configuration --function-name "
                + fn
                + " --environment 'Variables={DISABLE_SYNC="
                + desired
                + "}'"
            )


    # (sync-toggle command rimosso su richiesta)

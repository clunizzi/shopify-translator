from __future__ import annotations

from pathlib import Path
import os

import typer

from src.config.settings import SETTINGS
from src.pipeline.process_csv import process_file
from src.shopify.sync import process_product as sync_process_product
from src.shopify.graphql import make_product_gid
import asyncio

app = typer.Typer(add_completion=False, help="Shopify CSV translator")
cache_app = typer.Typer(help="Cache SQLite utilities")


# Opzioni predefinite centralizzate (evita B008 nelle signature)
OPT_STATS: bool = True
OPT_TRUNCATE: bool = False
OPT_NO_STDOUT: bool = False
OPT_AUTO_CLASSIFY: bool = True


@app.command("process")
def process(
    input: Path = typer.Option(..., "--input", "-i", help="Path CSV input"),  # noqa: B008
    output: Path = typer.Option(..., "--output", "-o", help="Path CSV output"),  # noqa: B008
    target_locales: str = typer.Option(
        SETTINGS.target_locale,
        "--target-locales",
        "--target-locale",
        help="Locale target singolo o lista separata da virgola (es. fr-FR oppure de-DE,fr-FR)",
    ),  # noqa: B008
    dnt: Path | None = typer.Option(None, "--dnt", help="Path YAML do_not_translate"),  # noqa: B008
    preserve_handle: bool = typer.Option(
        False, "--preserve-handle", help="Tenta trad. handle invece di generarlo"
    ),  # noqa: B008
    resume: bool = typer.Option(
        True, "--resume/--no-resume", help="Resume con checkpoint"
    ),  # noqa: B008
    force: bool = typer.Option(
        False, "--force", help="Ignora cache/checkpoint e ritraduce"
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Nessuna chiamata a Shopify/OpenAI"
    ),  # noqa: B008
    stats: bool = typer.Option(
        OPT_STATS, "--stats/--no-stats", help="Logga summary finale"
    ),  # noqa: B008
    types: str = typer.Option(
        "auto",
        "--types",
        help="Filtra Type (es. PRODUCT,COLLECTION). 'auto' elabora tutti i Type presenti",
    ),  # noqa: B008
    first_n: int | None = typer.Option(
        None, "--first-n", help="Primi N Identification unici"
    ),  # noqa: B008
    ids: str | None = typer.Option(
        None, "--ids", help="Lista ID numerici separati da virgola"
    ),  # noqa: B008
    ids_file: Path | None = typer.Option(
        None, "--ids-file", help="File con un ID per riga"
    ),  # noqa: B008
    id_range: str | None = typer.Option(
        None, "--id-range", help="Intervallo 'start:end'"
    ),  # noqa: B008
    log_file: Path | None = typer.Option(
        None, "--log-file", help="Log JSONL su file"
    ),  # noqa: B008
    no_stdout: bool = typer.Option(
        OPT_NO_STDOUT, "--no-stdout", help="Silenzia stdout (solo file)"
    ),  # noqa: B008
    overwrite_output: bool = typer.Option(
        True,
        "--overwrite-output/--append-output",
        help="Sovrascrive l'output (default) invece di appenderlo",
    ),  # noqa: B008
    auto_classify: bool = typer.Option(
        OPT_AUTO_CLASSIFY,
        "--auto-classify/--no-auto-classify",
        help="Riconosce automaticamente JSON/HTML/URL/valori tecnici/plain se Field non è informativo",
    ),  # noqa: B008
):
    """
    Esegue la pipeline di traduzione.
    """
    # Parse locale/i: accetta singolo o lista separata da virgola
    t_locales_list = [x.strip() for x in (target_locales or "").split(",") if x.strip()] or [SETTINGS.target_locale]
    primary_locale = t_locales_list[0]

    summary = {}
    try:
        summary = process_file(
            input_csv=input,
            output_csv=output,
            target_locale=primary_locale,
            target_locales=t_locales_list,
            dnt_config_path=dnt,
            preserve_handle=preserve_handle,
            resume=resume,
            force=force,
            dry_run=dry_run,
            stats=stats,
            types=types,
            first_n=first_n,
            ids=ids,
            ids_file=ids_file,
            id_range=id_range,
            log_file=log_file,
            no_stdout=no_stdout,
            overwrite_output=overwrite_output,
            auto_classify=auto_classify,
        )
    finally:
        pass
    # Non stampo "Done ..." per non rompere piping | jq
    if stats:
        import json

        print(json.dumps(summary, ensure_ascii=False))


@app.command("sync-shopify")
def sync_shopify(
    product_id: list[int] = typer.Option(
        ..., "--product-id", help="ID numerico prodotto (ripetibile)",
    ),  # noqa: B008
    target_locales: str | None = typer.Option(
        None, "--target-locales", help="Locali di destinazione separati da virgola"
    ),  # noqa: B008
    mf_include: str | None = typer.Option(
        None, "--mf-include", help="Lista namespace.key separati da virgola"
    ),  # noqa: B008
    mf_json_paths: str | None = typer.Option(
        None, "--mf-json-paths", help="JSON path rules (coma-separati)"
    ),  # noqa: B008
    create: bool = typer.Option(False, "--create", help="Tratta come products/create"),  # noqa: B008
    update: bool = typer.Option(True, "--update/--no-update", help="Tratta come update"),  # noqa: B008
    dry_run: bool = typer.Option(None, "--dry-run", help="Dry-run (override SETTINGS.DRY_RUN)"),  # noqa: B008
    apply_on_dry_run: bool = typer.Option(
        False, "--apply-on-dry-run", help="Aggiorna snapshot anche in dry-run"
    ),  # noqa: B008
):
    """Sincronizza traduzioni per prodotti esistenti su Shopify (products/create|update)."""
    if create and not update:
        is_create = True
    elif update and not create:
        is_create = False
    else:
        # default: update
        is_create = False

    tl = (
        [x.strip() for x in target_locales.split(",") if x.strip()]
        if target_locales
        else (SETTINGS.get_target_locales() or [SETTINGS.target_locale])
    )
    mf_inc = (
        [(a.strip(), b.strip()) for a, b in (s.split(".", 1) for s in mf_include.split(",") if "." in s)]
        if mf_include
        else SETTINGS.get_mf_include()
    )
    mf_paths = (
        [x.strip() for x in mf_json_paths.split(",") if x.strip()] if mf_json_paths else SETTINGS.get_mf_json_paths()
    )
    dr = SETTINGS.dry_run_default if dry_run is None else bool(dry_run)

    async def _run():
        results = []
        for pid in product_id:
            res = await sync_process_product(
                product_numeric_id=pid,
                target_locales=tl,
                mf_include=mf_inc,
                mf_json_paths=mf_paths,
                source_locale=SETTINGS.source_locale,
                dry_run=dr,
                is_create=is_create,
                delay_ms_after_create=SETTINGS.delay_ms_after_create,
                apply_on_dry_run=apply_on_dry_run,
            )
            results.append(res)
        return results

    out = asyncio.run(_run())
    import json as _json

    print(_json.dumps(out, ensure_ascii=False))


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
    db_path: Path | None = typer.Option(None, "--db-path", help="Path SQLite cache"),  # noqa: B008
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
    db_path: Path | None = typer.Option(None, "--db-path", help="Path SQLite cache"),  # noqa: B008
    all: bool = typer.Option(
        False,
        "--all/--translations-only",
        help="Drop both translations and snapshot tables (default: only translations)",
    ),  # noqa: B008
    vacuum: bool = typer.Option(False, "--vacuum/--no-vacuum", help="Run VACUUM after purge"),  # noqa: B008
):
    """Purge cache data. By default clears only translations table."""
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

from __future__ import annotations

from pathlib import Path

import typer

from src.config.settings import SETTINGS
from src.pipeline.process_csv import process_file
from src.shopify.sync import process_product as sync_process_product
from src.shopify.graphql import make_product_gid
import asyncio

app = typer.Typer(add_completion=False, help="Shopify CSV translator")


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
    # Opzioni opzionali per mettere in pausa la sync AWS durante l'elaborazione CSV
    no_sync: bool = typer.Option(
        False,
        "--no-sync",
        help="Metti in pausa la sync webhook su AWS (DISABLE_SYNC=true su Receiver/Worker)",
    ),  # noqa: B008
    sync_target: str = typer.Option(
        "both",
        "--sync-target",
        help="Target Lambda da disabilitare se --no-sync: receiver|worker|both",
    ),  # noqa: B008
    receiver_name: str | None = typer.Option(
        None,
        "--receiver-name",
        help="Nome funzione Lambda receiver (richiesto se --no-sync e --sync-target include receiver)",
    ),  # noqa: B008
    worker_name: str | None = typer.Option(
        None,
        "--worker-name",
        help="Nome funzione Lambda worker (richiesto se --no-sync e --sync-target include worker)",
    ),  # noqa: B008
    re_enable_sync: bool = typer.Option(
        False,
        "--re-enable-sync",
        help="Riabilita la sync (DISABLE_SYNC=false) a fine elaborazione se era stata disabilitata",
    ),  # noqa: B008
):
    """
    Esegue la pipeline di traduzione.
    """
    # Parse locale/i: accetta singolo o lista separata da virgola
    t_locales_list = [x.strip() for x in (target_locales or "").split(",") if x.strip()] or [SETTINGS.target_locale]
    primary_locale = t_locales_list[0]

    # Opzionale: disabilita la sync AWS prima dell'elaborazione
    if no_sync:
        targets: list[tuple[str, str]] = []
        st = (sync_target or "").strip().lower()
        if st in ("receiver", "both"):
            if not receiver_name:
                raise typer.BadParameter("--receiver-name richiesto con --no-sync per target receiver/both")
            targets.append(("receiver", receiver_name))
        if st in ("worker", "both"):
            if not worker_name:
                raise typer.BadParameter("--worker-name richiesto con --no-sync per target worker/both")
            targets.append(("worker", worker_name))

        try:
            import boto3  # type: ignore

            lam = boto3.client("lambda")
            for kind, fn in targets:
                cfg = lam.get_function_configuration(FunctionName=fn)
                env = (cfg.get("Environment") or {}).get("Variables") or {}
                env["DISABLE_SYNC"] = "true"
                lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
                typer.echo(f"[{kind}] DISABLE_SYNC=true impostato su {fn}")
        except Exception as e:  # pragma: no cover - fallback
            typer.echo(f"Impossibile aggiornare Lambda via boto3: {e}")
            typer.echo("Esegui i seguenti comandi AWS CLI equivalenti:")
            for kind, fn in targets:
                typer.echo(
                    "aws lambda update-function-configuration --function-name "
                    + fn
                    + " --environment 'Variables={DISABLE_SYNC=true}'"
                )

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
        # Riabilita la sync se richiesto
        if no_sync and re_enable_sync:
            targets2: list[tuple[str, str]] = []
            st2 = (sync_target or "").strip().lower()
            if st2 in ("receiver", "both"):
                if not receiver_name:
                    raise typer.BadParameter("--receiver-name richiesto per --re-enable-sync su receiver/both")
                targets2.append(("receiver", receiver_name))
            if st2 in ("worker", "both"):
                if not worker_name:
                    raise typer.BadParameter("--worker-name richiesto per --re-enable-sync su worker/both")
                targets2.append(("worker", worker_name))
            try:
                import boto3  # type: ignore
                lam = boto3.client("lambda")
                for kind, fn in targets2:
                    cfg = lam.get_function_configuration(FunctionName=fn)
                    env = (cfg.get("Environment") or {}).get("Variables") or {}
                    env["DISABLE_SYNC"] = "false"
                    lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
                    typer.echo(f"[{kind}] DISABLE_SYNC=false impostato su {fn}")
            except Exception as e:  # pragma: no cover
                typer.echo(f"Impossibile riabilitare la sync via boto3: {e}")
                typer.echo("Comandi AWS CLI equivalenti:")
                for kind, fn in targets2:
                    typer.echo(
                        "aws lambda update-function-configuration --function-name "
                        + fn
                        + " --environment 'Variables={DISABLE_SYNC=false}'"
                    )
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


    # (sync-toggle command rimosso su richiesta)

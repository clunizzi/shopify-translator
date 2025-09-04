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
    target_locale: str = typer.Option(
        SETTINGS.target_locale, "--target-locale", help="Locale target es. fr-FR"
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
    truncate_output: bool = typer.Option(
        OPT_TRUNCATE, "--truncate-output", help="Tronca l'output invece che appenderlo"
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
    summary = process_file(
        input_csv=input,
        output_csv=output,
        target_locale=target_locale,
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
        truncate_output=truncate_output,
        auto_classify=auto_classify,
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

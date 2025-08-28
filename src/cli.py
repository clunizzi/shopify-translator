from __future__ import annotations

from pathlib import Path

import typer

from src.config.settings import SETTINGS
from src.pipeline.process_csv import process_file

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

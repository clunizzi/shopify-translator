from __future__ import annotations

from pathlib import Path

import typer

from src.config.settings import SETTINGS
from src.pipeline.process_csv import process_file

app = typer.Typer(help="Traduttore CSV Shopify (PRODUCT)")

# Opzioni principali
OPT_INPUT = typer.Option(..., "--input", "-i", help="Path CSV input")
OPT_OUTPUT = typer.Option(..., "--output", "-o", help="Path CSV output")
OPT_TARGET = typer.Option(
    SETTINGS.target_locale, "--target-locale", help="Locale destinazione es. fr-FR"
)
OPT_DNT = typer.Option(None, "--dnt", help="Path YAML do_not_translate")

# Subsetting
OPT_FIRSTN = typer.Option(None, "--first-n", help="Primi N Identification unici")
OPT_IDS = typer.Option(None, "--ids", help="Lista ID separati da virgola")
OPT_IDS_FILE = typer.Option(None, "--ids-file", help="File con ID (uno per riga)")
OPT_ID_RANGE = typer.Option(None, "--id-range", help="Range numerico inclusivo START:END")

# Logging
OPT_LOG_FILE = typer.Option(
    SETTINGS.log_file or None, "--log-file", help="Scrivi log JSONL su file"
)
OPT_NO_STDOUT = typer.Option(False, "--no-stdout", help="Solo file (niente stdout)")

# Altri flag
OPT_PRESERVE = typer.Option(
    False, "--preserve-handle", help="Tenta trad. handle invece di generarlo"
)
OPT_RESUME = typer.Option(True, "--resume/--no-resume", help="Resume con checkpoint")
OPT_FORCE = typer.Option(False, "--force", help="Ignora cache/checkpoint e ritraduce")
OPT_DRY = typer.Option(False, "--dry-run", help="Nessuna chiamata a Shopify/OpenAI")
OPT_STATS = typer.Option(True, "--stats/--no-stats", help="Logga statistiche finali")
OPT_TRUNCATE = typer.Option(
    False, "--truncate-output", help="Sovrascrive l'output (riscrive header)"
)


@app.command("process")
def process(
    input: Path = OPT_INPUT,
    output: Path = OPT_OUTPUT,
    target_locale: str = OPT_TARGET,
    dnt: Path | None = OPT_DNT,
    first_n: int | None = OPT_FIRSTN,
    ids: str | None = OPT_IDS,
    ids_file: Path | None = OPT_IDS_FILE,
    id_range: str | None = OPT_ID_RANGE,
    log_file: Path | None = OPT_LOG_FILE,
    no_stdout: bool = OPT_NO_STDOUT,
    preserve_handle: bool = OPT_PRESERVE,
    resume: bool = OPT_RESUME,
    force: bool = OPT_FORCE,
    dry_run: bool = OPT_DRY,
    stats: bool = OPT_STATS,
    truncate_output: bool = OPT_TRUNCATE,
):
    summary = process_file(
        input_csv=input,
        output_csv=output,
        target_locale=target_locale,
        dnt_config_path=dnt,
        # subset
        first_n=first_n,
        ids=ids,
        ids_file=ids_file,
        id_range=id_range,
        # logging
        log_file=log_file,
        no_stdout=no_stdout,
        # altri
        preserve_handle=preserve_handle,
        resume=resume,
        force=force,
        dry_run=dry_run,
        stats=stats,
        # output
        truncate_output=truncate_output,
    )
    # manda il “Done …” su stderr per non sporcare JSON su stdout
    typer.echo(f"Done. {summary}", err=True)

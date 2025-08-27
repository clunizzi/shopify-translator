from __future__ import annotations

import hashlib
import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import polars as pl
import structlog

from src.config.settings import SETTINGS
from src.io.csv_reader import identification_order, iter_groups_in_input_order, read_csv
from src.io.csv_writer import append_rows, init_output
from src.shopify.product_status import get_active_products_map
from src.translate.cache import TranslationCache
from src.translate.translator import DoNotTranslateConfig, Translator

logger = structlog.get_logger()

TEXT_COLS = [
    "Type",
    "Field",
    "Locale",
    "Market",
    "Status",
    "Default content",
    "Translated content",
]
SCHEMA = {
    "Type": pl.Utf8,
    "Identification": pl.Int64,
    "Field": pl.Utf8,
    "Locale": pl.Utf8,
    "Market": pl.Utf8,
    "Status": pl.Utf8,
    "Default content": pl.Utf8,
    "Translated content": pl.Utf8,
}


def _configure_logging(log_file: Path | None, no_stdout: bool) -> None:
    handlers: list[logging.Handler] = []
    level = getattr(logging, SETTINGS.log_level.upper(), logging.INFO)

    if not no_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(sh)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            filename=str(log_file),
            when="midnight",
            backupCount=SETTINGS.log_retention_days,
            encoding="utf-8",
            utc=False,
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(fh)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for h in handlers:
        root.addHandler(h)

    # SOLO i nostri logger devono arrivare qui; silenzia librerie rumorose
    for name in ("httpx", "httpcore", "urllib3", "hpack", "h11"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False  # evita che finiscano nei nostri handler

    structlog.configure(
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.dict_tracebacks,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
    )


def load_do_not_translate(config_path: str | Path | None) -> DoNotTranslateConfig:
    brands: list[str] = []
    units: list[str] = []
    tokens: list[str] = []
    if config_path and Path(config_path).exists():
        import yaml  # lazy import

        data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        brands = list(map(str, data.get("brands", []) or []))
        units = list(map(str, data.get("units", []) or []))
        tokens = list(map(str, data.get("tokens", []) or []))
    return DoNotTranslateConfig(brands=brands, units=units, tokens=tokens)


def _load_checkpoint(cp_path: Path) -> dict:
    if cp_path.exists():
        try:
            return json.loads(cp_path.read_text())
        except Exception:
            return {}
    return {}


def _save_checkpoint(cp_path: Path, content: dict) -> None:
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.write_text(json.dumps(content, ensure_ascii=False, indent=2))


def _hash_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _parse_ids(ids: str | None) -> set[int]:
    out: set[int] = set()
    if not ids:
        return out
    for part in ids.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _parse_ids_file(path: str | Path | None) -> set[int]:
    out: set[int] = set()
    if not path:
        return out
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.add(int(line))
        except ValueError:
            continue
    return out


def _parse_id_range(rng: str | None) -> set[int]:
    out: set[int] = set()
    if not rng:
        return out
    try:
        start_s, end_s = rng.split(":", 1)
        start_i = int(start_s.strip())
        end_i = int(end_s.strip())
        if start_i <= end_i:
            out.update(range(start_i, end_i + 1))
    except Exception:
        pass
    return out


def _compute_allowed_ids(
    df: pl.DataFrame,
    first_n: int | None,
    ids: str | None,
    ids_file: str | Path | None,
    id_range: str | None,
) -> set[int] | None:
    order = identification_order(df)
    allowed_seq: list[int] | None = None

    base_union: set[int] = set()
    base_union |= _parse_ids(ids)
    base_union |= _parse_ids_file(ids_file)
    base_union |= _parse_id_range(id_range)

    if base_union:
        allowed_seq = [i for i in order if i in base_union]

    if first_n is not None:
        if first_n <= 0:
            return set()
        if allowed_seq is None:
            allowed_seq = order[:first_n]
        else:
            allowed_seq = allowed_seq[:first_n]

    if allowed_seq is None:
        return None
    return set(allowed_seq)


def _checkpoint_path_for(input_hash: str, locale: str) -> Path:
    Path("state").mkdir(parents=True, exist_ok=True)
    return Path("state") / f"{input_hash}_{locale}.json"


def _load_checkpoint_any(
    new_path: Path, fallback_old: Path, expect_hash: str, expect_locale: str
) -> dict:
    """
    Carica prima dal path per-file/locale; se assente prova il vecchio 'state/checkpoint.json'
    e migra se combacia.
    """
    if new_path.exists():
        try:
            return json.loads(new_path.read_text())
        except Exception:
            return {}

    if fallback_old.exists():
        try:
            data = json.loads(fallback_old.read_text())
        except Exception:
            return {}
        if data.get("input_hash") == expect_hash and data.get("locale") == expect_locale:
            # migra scrivendo sul nuovo path
            new_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            return data
    return {}


def process_file(
    input_csv: str | Path,
    output_csv: str | Path,
    target_locale: str,
    dnt_config_path: str | Path | None,
    preserve_handle: bool,
    resume: bool,
    force: bool,
    dry_run: bool,
    stats: bool,
    *,
    first_n: int | None = None,
    ids: str | None = None,
    ids_file: str | Path | None = None,
    id_range: str | None = None,
    log_file: str | Path | None = None,
    no_stdout: bool = False,
    truncate_output: bool = False,
) -> dict:
    _configure_logging(
        Path(log_file) if log_file else (Path(SETTINGS.log_file) if SETTINGS.log_file else None),
        no_stdout,
    )

    in_path = Path(input_csv)
    input_hash = _hash_file(in_path)

    df = read_csv(in_path).filter(pl.col("Type") == "PRODUCT")

    allowed_ids = _compute_allowed_ids(df, first_n, ids, ids_file, id_range)
    if allowed_ids is not None:
        df = df.filter(pl.col("Identification").cast(pl.Int64).is_in(list(allowed_ids)))

    out_path = Path(output_csv)
    init_output(out_path, truncate=truncate_output)

    # checkpoint per file+locale
    cp_new = _checkpoint_path_for(input_hash, target_locale)
    cp_old = Path("state/checkpoint.json")
    cp = _load_checkpoint_any(cp_new, cp_old, input_hash, target_locale)
    completed: set[int] = set()
    if resume and cp and cp.get("input_hash") == input_hash and cp.get("locale") == target_locale:
        completed = set(cp.get("completed_identifications", []))

    cache = TranslationCache()
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)
    dnt = load_do_not_translate(dnt_config_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]

    all_ids = [int(x) for x in df["Identification"].to_list()]
    unique_ids = sorted(set(all_ids))
    active_map = get_active_products_map(unique_ids, dry_run=dry_run)

    summary = {
        "translated_rows": 0,  # inteso come "righe scritte"
        "skipped_inactive": 0,
        "cache_hit": 0,
        "processed_products": 0,
    }

    for pid, sub in iter_groups_in_input_order(df):
        if pid in completed and not force:
            logger.info("skip_completed", product_id=pid)
            continue

        if not active_map.get(pid, False):
            summary["skipped_inactive"] += len(sub)
            logger.info("skip_inactive", product_id=pid)
            continue

        title_translated: str | None = None
        rows_out: list[pl.DataFrame] = []

        for row in sub.iter_rows(named=True):
            # normalizza None -> "" prima di lavorare
            for c in TEXT_COLS:
                if row.get(c) is None:
                    row[c] = ""

            field = row["Field"].strip()
            default = row["Default content"] or ""
            translated_existing = row["Translated content"] or ""

            if translated_existing and not force:
                rows_out.append(pl.DataFrame([row], schema=SCHEMA))
                summary["translated_rows"] += 1  # conta anche righe già compilate
                continue

            if field == "title":
                title_translated = translator.translate_field(
                    "PRODUCT",
                    field,
                    default,
                    target_locale,
                    dnt=dnt,
                    exclude_similarity_tokens=exclude_tokens,
                )
                translated = title_translated
            else:
                translated = translator.translate_field(
                    "PRODUCT",
                    field,
                    default,
                    target_locale,
                    dnt=dnt,
                    exclude_similarity_tokens=exclude_tokens,
                    title_translated=title_translated,
                    preserve_handle=preserve_handle,
                )

            if translated == "":
                err_code = (
                    "ERROR_SIMILARITY_HTML"
                    if field == "body_html"
                    else f"ERROR_SIMILARITY_{field.upper()}"
                )
                row["Status"] = err_code

            row["Translated content"] = translated
            rows_out.append(pl.DataFrame([row], schema=SCHEMA))
            summary["translated_rows"] += 1

        if rows_out:
            batch = pl.concat(rows_out, rechunk=True).with_columns(
                pl.col("Identification").cast(pl.Int64),
                *(pl.col(c).cast(pl.Utf8) for c in TEXT_COLS),
            )
            append_rows(out_path, batch)

        completed.add(pid)
        cp = {
            "input_hash": input_hash,
            "locale": target_locale,
            "completed_identifications": sorted(list(completed)),
            "stats": summary,
        }
        cp_new.write_text(json.dumps(cp, ensure_ascii=False, indent=2))
        summary["processed_products"] += 1

    if stats:
        logger.info("summary", **summary)
    cache.close()
    return summary

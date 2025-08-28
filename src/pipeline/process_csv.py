from __future__ import annotations

import hashlib
import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
import structlog

from src.config.settings import SETTINGS
from src.io.csv_reader import iter_groups_in_input_order, read_csv
from src.io.csv_writer import append_rows, init_output
from src.rules.option_value import (
    normalize_option_label,
    should_skip_option_name,
    should_skip_option_value_name,
)
from src.shopify.metafield_owner import get_metafields_owner_map
from src.shopify.product_status import get_active_products_map
from src.translate.cache import TranslationCache
from src.translate.translator import Translator

if TYPE_CHECKING:
    from src.translate.translator import DoNotTranslateConfig

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

ALLOWED_TYPES = {"PRODUCT", "PRODUCT_OPTION", "PRODUCT_OPTION_VALUE", "COLLECTION", "METAFIELD"}


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

    for name in ("httpx", "httpcore", "urllib3", "hpack", "h11"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False

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


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _checkpoint_path_for(input_hash: str, locale: str) -> Path:
    Path("state").mkdir(parents=True, exist_ok=True)
    return Path("state") / f"{input_hash}_{locale}.json"


def _load_checkpoint_any(
    new_path: Path, fallback_old: Path, expect_hash: str, expect_locale: str
) -> dict:
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
            new_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            return data
    return {}


def _parse_types_arg(types: str, df_types: set[str]) -> set[str]:
    if not types or types.strip().lower() == "auto":
        return set(t for t in df_types if t in ALLOWED_TYPES) or {"PRODUCT"}
    parts = [t.strip().upper() for t in types.split(",") if t.strip()]
    sel = set(p for p in parts if p in ALLOWED_TYPES)
    return sel or {"PRODUCT"}


def _compute_allowed_ids(
    df: pl.DataFrame,
    first_n: int | None,
    ids: str | None,
    ids_file: str | Path | None,
    id_range: str | None,
) -> set[int] | None:
    if ids:
        return set(int(x) for x in ids.split(",") if x.strip())
    if ids_file:
        p = Path(ids_file)
        return set(int(x.strip()) for x in p.read_text().splitlines() if x.strip().isdigit())
    if id_range and ":" in id_range:
        a, b = id_range.split(":", 1)
        try:
            return set(range(int(a), int(b) + 1))
        except Exception:
            return set()
    if first_n:
        seen: set[int] = set()
        out: list[int] = []
        for pid in df["Identification"].to_list():
            pid = int(pid)
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
                if len(out) >= first_n:
                    break
        return set(out)
    return None


def load_do_not_translate(dnt_config_path: str | Path | None) -> DoNotTranslateConfig:
    import yaml

    from src.translate.translator import DoNotTranslateConfig  # lazy to avoid cycle

    if not dnt_config_path:
        return DoNotTranslateConfig(brands=[], units=[], tokens=[])
    with Path(dnt_config_path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DoNotTranslateConfig(
        brands=list(data.get("brands", []) or []),
        units=list(data.get("units", []) or []),
        tokens=list(data.get("tokens", []) or []),
    )


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
    types: str = "auto",
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

    # Lettura + filtro tipi
    df_all = read_csv(in_path)
    present_types = set(df_all["Type"].unique().to_list())
    wanted_types = _parse_types_arg(types, present_types)
    df = df_all.filter(pl.col("Type").is_in(list(wanted_types)))

    # FILTRI ID prima delle query (riduce chiamate)
    allowed_ids = _compute_allowed_ids(df, first_n, ids, ids_file, id_range)
    if allowed_ids is not None:
        df = df.filter(pl.col("Identification").cast(pl.Int64).is_in(list(allowed_ids)))

    # --- Metafield owner/key map + active (solo se richiesto) ---
    metafield_owner_map: dict[int, dict] = {}
    metafield_active_map: dict[int, bool] = {}
    if "METAFIELD" in wanted_types:
        try:
            mf_ids = [
                int(x) for x in df.filter(pl.col("Type") == "METAFIELD")["Identification"].to_list()
            ]
            mf_ids = sorted(set(mf_ids))
        except Exception:
            mf_ids = []

        if mf_ids:
            metafield_owner_map = get_metafields_owner_map(mf_ids)
            # Attivi solo per owner PRODUCT
            owner_product_ids = sorted(
                set(
                    d["owner_numeric"]
                    for d in metafield_owner_map.values()
                    if d.get("owner_type") == "PRODUCT" and d.get("owner_numeric") is not None
                )
            )
            product_active = (
                get_active_products_map(owner_product_ids, dry_run=dry_run)
                if owner_product_ids
                else {}
            )
            for mf_id, info in metafield_owner_map.items():
                if info.get("owner_type") == "PRODUCT":
                    onum = info.get("owner_numeric")
                    metafield_active_map[mf_id] = bool(product_active.get(int(onum or 0), False))
                else:
                    # non-PRODUCT: procedo senza filtro
                    metafield_active_map[mf_id] = True

    # Shopify status SOLO per PRODUCT
    product_ids_for_status: list[int] = []
    if "PRODUCT" in wanted_types:
        try:
            product_ids_for_status = [
                int(x) for x in df.filter(pl.col("Type") == "PRODUCT")["Identification"].to_list()
            ]
            product_ids_for_status = sorted(set(product_ids_for_status))
        except Exception:
            product_ids_for_status = []
    if product_ids_for_status:
        active_map = get_active_products_map(product_ids_for_status, dry_run=dry_run)
    else:
        active_map = {}
        logger.info("shopify_status_skip", reason="no_PRODUCT_in_types")

    # Output + checkpoint
    out_path = Path(output_csv)
    init_output(out_path, truncate=truncate_output)

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

    summary = {
        "translated_rows": 0,
        "copied_rows_existing": 0,
        "skipped_inactive": 0,
        "skipped_by_rule_option": 0,
        "skipped_by_rule_option_value": 0,
        "translated_option_rows": 0,
        "translated_option_value_rows": 0,
        "unchanged_by_rule_value": 0,
        "skipped_empty_body_html": 0,
        "translated_metafield_rows": 0,
        "cache_hit": 0,
        "processed_products": 0,
    }

    for pid, sub in iter_groups_in_input_order(df):
        if pid in completed and not force:
            logger.info("skip_completed", product_id=pid)
            continue

        sub_types = set(sub["Type"].unique().to_list())

        # METAFIELD: skip se owner=PRODUCT inattivo
        if "METAFIELD" in sub_types:
            active_ok = metafield_active_map.get(int(pid), True)
            if not active_ok:
                summary["skipped_inactive"] += len(sub)
                logger.info("skip_metafield_inactive_owner", metafield_id=int(pid))
                continue

        # PRODUCT: skip se inattivo
        if "PRODUCT" in sub_types and not active_map.get(pid, False):
            summary["skipped_inactive"] += len(sub)
            logger.info("skip_inactive", product_id=pid)
            continue

        title_translated: str | None = None
        rows_out: list[pl.DataFrame] = []

        for row in sub.iter_rows(named=True):
            # normalizzazione null -> ""
            for c in TEXT_COLS:
                if row.get(c) is None:
                    row[c] = ""

            type_name = row["Type"].strip().upper()
            field_csv = (row["Field"] or "").strip()
            default = row["Default content"] or ""
            translated_existing = row["Translated content"] or ""

            # Skip silenzioso: body_html senza contenuto (stringa vuota)
            if field_csv == "body_html" and not default.strip():
                summary["skipped_empty_body_html"] += 1
                logger.info("skip_empty_body_html", product_id=pid, type_name=type_name)
                continue

            # Se già presente e non forzi, copia la riga esistente
            if translated_existing and not force:
                rows_out.append(pl.DataFrame([row], schema=SCHEMA))
                summary["copied_rows_existing"] += 1
                continue

            translated = ""
            did_translate = False

            if type_name == "PRODUCT":
                if field_csv == "title":
                    title_translated = translator.translate_field(
                        "PRODUCT",
                        "title",
                        default,
                        target_locale,
                        dnt=dnt,
                        exclude_similarity_tokens=exclude_tokens,
                    )
                    translated = title_translated
                    did_translate = True
                else:
                    translated = translator.translate_field(
                        "PRODUCT",
                        field_csv,
                        default,
                        target_locale,
                        dnt=dnt,
                        exclude_similarity_tokens=exclude_tokens,
                        title_translated=title_translated,
                        preserve_handle=preserve_handle,
                    )
                    did_translate = True

            elif type_name == "PRODUCT_OPTION":
                skip, reason = should_skip_option_name(default)
                if skip:
                    summary["skipped_by_rule_option"] += 1
                    logger.info(
                        "skip_option_rule", reason=reason, product_id=pid, value=default[:120]
                    )
                    continue
                label_norm, had_colon = normalize_option_label(default)
                translated = translator.translate_plain(
                    "PRODUCT_OPTION",
                    "option_name",
                    label_norm,
                    target_locale,
                    dnt=dnt,
                    exclude_similarity_tokens=exclude_tokens,
                )
                if translated == "":
                    row["Status"] = "ERROR_SIMILARITY_OPTION"
                if had_colon and translated:
                    translated = translated + ":"
                summary["translated_option_rows"] += 1
                did_translate = True

            elif type_name == "PRODUCT_OPTION_VALUE":
                skip, reason = should_skip_option_value_name(default, dnt.units)
                if skip:
                    if reason == "value_default_title":
                        summary["skipped_by_rule_option_value"] += 1
                        logger.info(
                            "skip_value_rule", reason=reason, product_id=pid, value=default[:120]
                        )
                        continue
                    row["Translated content"] = default
                    row["Status"] = f"UNCHANGED_BY_RULE_VALUE:{reason}"
                    rows_out.append(pl.DataFrame([row], schema=SCHEMA))
                    summary["translated_rows"] += 1  # viene considerata "output scritto"
                    summary["unchanged_by_rule_value"] += 1
                    logger.info(
                        "value_rule_unchanged", reason=reason, product_id=pid, value=default[:120]
                    )
                    continue

                translated = translator.translate_plain(
                    "PRODUCT_OPTION_VALUE",
                    "option_value_name",
                    default,
                    target_locale,
                    dnt=dnt,
                    exclude_similarity_tokens=exclude_tokens,
                )
                if translated == "":
                    row["Status"] = "ERROR_SIMILARITY_VALUE"
                summary["translated_option_value_rows"] += 1
                did_translate = True

            elif type_name == "COLLECTION":
                if field_csv == "title":
                    title_translated = translator.translate_field(
                        "COLLECTION",
                        "title",
                        default,
                        target_locale,
                        dnt=dnt,
                        exclude_similarity_tokens=exclude_tokens,
                    )
                    translated = title_translated
                    did_translate = True
                else:
                    translated = translator.translate_field(
                        "COLLECTION",
                        field_csv,
                        default,
                        target_locale,
                        dnt=dnt,
                        exclude_similarity_tokens=exclude_tokens,
                        title_translated=title_translated,
                        preserve_handle=preserve_handle,
                    )
                    did_translate = True

            elif type_name == "METAFIELD":
                info = metafield_owner_map.get(int(pid), {}) if metafield_owner_map else {}
                key = (info.get("key") or "").strip()
                # mappa campo logico per policy/prompt
                if key == "title_tag":
                    field_logical = "meta_title"
                elif key == "description_tag":
                    field_logical = "meta_description"
                else:
                    field_logical = "value"

                translated = translator.translate_json_value(
                    "METAFIELD",
                    field_logical,
                    default,
                    target_locale,
                    dnt=dnt,
                    exclude_similarity_tokens=exclude_tokens,
                )

                src_strip = default.strip()
                if translated == "":
                    if src_strip in ("{}", "[]"):
                        translated = src_strip  # JSON vuoto: preserva, no errore
                    else:
                        row["Status"] = (
                            "ERROR_SIMILARITY_JSON"
                            if src_strip.startswith("{") or src_strip.startswith("[")
                            else "ERROR_SIMILARITY_VALUE"
                        )
                summary["translated_metafield_rows"] += 1
                did_translate = True

            # error code per reject su PRODUCT/COLLECTION (plain/html)
            if type_name in {"PRODUCT", "COLLECTION"} and translated == "":
                err_code = (
                    "ERROR_SIMILARITY_HTML"
                    if field_csv == "body_html"
                    else f"ERROR_SIMILARITY_{field_csv.upper()}"
                )
                row["Status"] = err_code

            row["Translated content"] = translated
            rows_out.append(pl.DataFrame([row], schema=SCHEMA))
            if did_translate:
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

    # aggiorna contatore cache_hit (se esposto dal Translator)
    summary["cache_hit"] = getattr(translator, "cache_hits", 0)

    if stats:
        logger.info("summary", **summary)
    cache.close()
    return summary

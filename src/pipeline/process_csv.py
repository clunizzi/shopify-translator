from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sys
from datetime import UTC, datetime
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
from src.translate.translator import OPENAI_JSON_ERROR_SENTINEL, Translator

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

# Type riconosciuti lato Shopify
SHOPIFY_STATUS_TYPES = {"PRODUCT"}
SHOPIFY_METAFIELD_TYPES = {"METAFIELD"}

# Regex per auto-classify
RE_TAG = re.compile(r"<[A-Za-z][^>]*>")
RE_URL = re.compile(r"^https?://", re.IGNORECASE)
RE_TECH = re.compile(
    r"""^
    [\s\-\+\(\)\[\]]*
    [\d.,/\s×xX\-]+
    \s*
    (mm|cm|m|km|mm²|cm²|m²|ha|ml|mL|L|dl|cl|kg|g|mg|
     kW|W|V|A|Ah|Hz|bar|psi|Pa|dB|dB\(A\)|m/s|m/s²|m³/h|l/h|rpm|cv|HP|Nm|°C|°F|
     %)?
    [\s\-\+\(\)\[\]0-9¹²³⁴⁵⁶⁷⁸⁹"']*
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)


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


def _normalize_type(t: str | None) -> str:
    return (t or "").strip().upper()


def _parse_types_arg(
    types: str | None,
    df_types: set[str] | None,
    allowed: set[str] | None = None,
) -> set[str]:
    """
    Determina i Type da processare.
    - Non dipende da un global ALLOWED_TYPES.
    - Normalizza sempre (strip + uppercase).
    - Non restituisce mai None: fallback sensati.
    """
    DEFAULT_ALLOWED = {
        "PRODUCT",
        "PRODUCT_OPTION",
        "PRODUCT_OPTION_VALUE",
        "COLLECTION",
        "METAFIELD",
    }
    allowed = (allowed or DEFAULT_ALLOWED)

    safe_present = {str(t).strip().upper() for t in (df_types or set()) if t is not None}
    allowed_present = safe_present & allowed

    if not types or str(types).strip().lower() == "auto":
        return allowed_present or {"PRODUCT"}

    parts = [p.strip().upper() for p in str(types).split(",") if p and p.strip()]
    sel = {p for p in parts if p in allowed}
    return sel or allowed_present or {"PRODUCT"}



def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

def _new_run_id() -> str:
    return f"{_now_iso()}-{os.getpid()}"

def _append_state_index(entry: dict) -> None:
    idx = Path("state") / "index.jsonl"
    idx.parent.mkdir(parents=True, exist_ok=True)
    with idx.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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

    from src.translate.translator import DoNotTranslateConfig  # lazy per evitare cicli

    if not dnt_config_path:
        return DoNotTranslateConfig(brands=[], units=[], tokens=[])
    with Path(dnt_config_path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DoNotTranslateConfig(
        brands=list(data.get("brands", []) or []),
        units=list(data.get("units", []) or []),
        tokens=list(data.get("tokens", []) or []),
    )


def _looks_html(s: str) -> bool:
    return bool(RE_TAG.search(s))


def _looks_json(s: str) -> bool:
    s = (s or "").lstrip()
    return s.startswith("{") or s.startswith("[")


def _is_url(s: str) -> bool:
    return bool(RE_URL.match(s or ""))


def _is_technical_value(s: str) -> bool:
    return bool(RE_TECH.match((s or "").strip()))


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
    auto_classify: bool = True,  # <— nuovo: per gestire CSV “generici”
) -> dict:
    _configure_logging(
        Path(log_file) if log_file else (Path(SETTINGS.log_file) if SETTINGS.log_file else None),
        no_stdout,
    )

    in_path = Path(input_csv)
    out_path = Path(output_csv)
    input_hash = _hash_file(in_path)

    df_all = read_csv(in_path)

    # Normalizza la colonna Type (se esiste) per sicurezza
    has_type_col = "Type" in df_all.columns
    if has_type_col:
        df_all = df_all.with_columns(
            pl.col("Type").cast(pl.Utf8).fill_null("").str.strip_chars().str.to_uppercase()
        )

    # Decidi se filtrare oppure no
    types_arg = (types or "").strip().lower()
    filter_enabled = has_type_col and types_arg not in ("", "auto")

    if filter_enabled:
        present_types = set(df_all["Type"].unique().to_list())
        wanted_types = _parse_types_arg(types, present_types)  # garantisce set non vuoto
        df = df_all.filter(pl.col("Type").is_in(list(wanted_types)))
        logger.info("types_filter", wanted=list(wanted_types))
    else:
        # NIENTE filtro: processa tutto il CSV (LINK, SHOPIFY_POLICY, ecc. verranno gestiti come GENERIC)
        wanted_types = None
        df = df_all
        if has_type_col:
            logger.info("types_autodetect", action="no_filter_all_types")
        else:
            logger.info("no_type_column", action="process_generic")

    # Filtro ID (first_n/ids/ids_file/id_range)
    allowed_ids = _compute_allowed_ids(df, first_n, ids, ids_file, id_range)
    if allowed_ids is not None:
        df = df.filter(pl.col("Identification").cast(pl.Int64).is_in(list(allowed_ids)))

    init_output(out_path, truncate=truncate_output)

    # Checkpoint
    cp_new = _checkpoint_path_for(input_hash, target_locale)
    cp_old = Path("state/checkpoint.json")
    cp_loaded = _load_checkpoint_any(cp_new, cp_old, input_hash, target_locale)

    run_id = _new_run_id()
    host = {"hostname": socket.gethostname(), "pid": os.getpid()}
    options = {
        "types": sorted(list(wanted_types)) if has_type_col and wanted_types else None,
        "first_n": first_n,
        "ids": ids,
        "ids_file": str(ids_file) if ids_file else None,
        "id_range": id_range,
        "preserve_handle": preserve_handle,
        "resume": resume,
        "force": force,
        "dry_run": dry_run,
        "truncate_output": truncate_output,
        "log_file": str(log_file) if log_file else None,
        "no_stdout": no_stdout,
        "auto_classify": auto_classify,
    }
    env = {
        "model": SETTINGS.openai_model,
        "rules_version": SETTINGS.rules_version,
        "batch_size": SETTINGS.batch_size,
        "shopify_domain": getattr(SETTINGS, "shopify_domain", ""),
        "shopify_api_version": getattr(SETTINGS, "shopify_api_version", "2024-07"),
    }

    total_groups = int(df["Identification"].n_unique())
    completed: set[int] = set()
    if resume and cp_loaded and cp_loaded.get("input_hash") == input_hash and cp_loaded.get("locale") == target_locale:
        completed = set(cp_loaded.get("completed_identifications", []))

    summary = {
        "translated_rows": 0,
        "skipped_inactive": 0,
        "skipped_by_rule_option": 0,
        "skipped_by_rule_option_value": 0,
        "translated_option_rows": 0,
        "translated_option_value_rows": 0,
        "unchanged_by_rule_value": 0,
        "skipped_empty_body_html": 0,
        "translated_metafield_rows": 0,
        "json_errors": 0,
        "classified_empty": 0,
        "classified_json": 0,
        "classified_html": 0,
        "classified_url": 0,
        "classified_tech": 0,
        "classified_plain": 0,
        "cache_hit": 0,
        "processed_products": 0,
    }

    cp = {
        "run_id": run_id,
        "run_status": "running",
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "input_hash": input_hash,
        "locale": target_locale,
        "input_path": str(in_path.resolve()),
        "output_path": str(out_path.resolve()),
        "input_basename": in_path.name,
        "output_basename": out_path.name,
        "options": options,
        "env": env,
        "host": host,
        "progress": {
            "completed_identifications": sorted(list(completed)),
            "processed_products": 0,
            "total_groups": total_groups,
        },
        "stats": summary,
    }
    cp_new.write_text(json.dumps(cp, ensure_ascii=False, indent=2))
    _append_state_index(
        {
            "run_id": run_id,
            "input_hash": input_hash,
            "locale": target_locale,
            "input_basename": in_path.name,
            "output_basename": out_path.name,
            "started_at": cp["started_at"],
            "run_status": "running",
        }
    )

    cache = TranslationCache()
    translator = Translator(cache=cache, model=SETTINGS.openai_model, dry_run=dry_run)
    dnt = load_do_not_translate(dnt_config_path)
    exclude_tokens = [*dnt.brands, *dnt.units, *dnt.tokens]

    try:
        # --- Shopify status SOLO se esiste "Type" e tra i wanted c'è PRODUCT ---
        product_ids_for_status: list[int] = []
        if has_type_col and wanted_types and "PRODUCT" in wanted_types:
            try:
                product_ids_for_status = [
                    int(x)
                    for x in df.filter(pl.col("Type") == "PRODUCT")["Identification"].to_list()
                ]
                product_ids_for_status = sorted(set(product_ids_for_status))
            except Exception:
                product_ids_for_status = []
        if product_ids_for_status:
            active_map = get_active_products_map(product_ids_for_status, dry_run=dry_run)
        else:
            active_map = {}
            logger.info(
                "shopify_status_skip",
                reason="no_PRODUCT_in_types" if has_type_col else "no_type_column",
            )

        # --- Metafield owner/key/active SOLO se esiste "Type" e c'è METAFIELD ---
        metafield_owner_map: dict[int, dict] = {}
        metafield_active_map: dict[int, bool] = {}
        if has_type_col and wanted_types and "METAFIELD" in wanted_types:
            try:
                mf_ids = [
                    int(x)
                    for x in df.filter(pl.col("Type") == "METAFIELD")["Identification"].to_list()
                ]
                mf_ids = sorted(set(mf_ids))
            except Exception:
                mf_ids = []
            if mf_ids:
                metafield_owner_map = get_metafields_owner_map(mf_ids)
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
                        metafield_active_map[mf_id] = True

        # --- LOOP PRINCIPALE ---
        for pid, sub in iter_groups_in_input_order(df):
            if pid in completed and not force:
                logger.info("skip_completed", product_id=pid)
                continue

            sub_types = set(sub["Type"].unique().to_list()) if has_type_col else set()
            is_metafield_group = has_type_col and ("METAFIELD" in sub_types)
            is_product_group = has_type_col and ("PRODUCT" in sub_types)

            if is_metafield_group:
                active_ok = metafield_active_map.get(int(pid), True)
                if not active_ok:
                    summary["skipped_inactive"] += len(sub)
                    logger.info("skip_metafield_inactive_owner", metafield_id=int(pid))
                    continue

            if is_product_group and not active_map.get(pid, False):
                summary["skipped_inactive"] += len(sub)
                logger.info("skip_inactive", product_id=pid)
                continue

            rows_out: list[pl.DataFrame] = []
            title_translated: str | None = None

            for row in sub.iter_rows(named=True):
                for c in TEXT_COLS:
                    if row.get(c) is None:
                        row[c] = ""

                # Fallback di type_name se la colonna manca
                type_name = (row.get("Type") or "GENERIC").strip().upper()
                field_csv = (row.get("Field") or "").strip()
                default = row.get("Default content") or ""
                translated_existing = row.get("Translated content") or ""

                if field_csv == "body_html" and not default.strip():
                    summary["skipped_empty_body_html"] += 1
                    logger.info("skip_empty_body_html", product_id=pid, type_name=type_name)
                    continue

                if translated_existing and not force:
                    rows_out.append(pl.DataFrame([row], schema=SCHEMA))
                    summary["translated_rows"] += 1
                    continue

                translated = ""

                # --- rami esistenti invariati (PRODUCT / OPTIONS / COLLECTION / METAFIELD) ---
                # Nota: se has_type_col è False, si cade nell’else GENERIC più sotto.

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

                elif type_name == "PRODUCT_OPTION_VALUE":
                    skip, reason = should_skip_option_value_name(default, dnt.units)
                    if skip:
                        if reason == "value_default_title":
                            summary["skipped_by_rule_option_value"] += 1
                            logger.info(
                                "skip_value_rule",
                                reason=reason,
                                product_id=pid,
                                value=default[:120],
                            )
                            continue
                        row["Translated content"] = default
                        row["Status"] = f"UNCHANGED_BY_RULE_VALUE:{reason}"
                        rows_out.append(pl.DataFrame([row], schema=SCHEMA))
                        summary["translated_rows"] += 1
                        summary["unchanged_by_rule_value"] += 1
                        logger.info(
                            "value_rule_unchanged",
                            reason=reason,
                            product_id=pid,
                            value=default[:120],
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

                elif type_name == "METAFIELD":
                    info = metafield_owner_map.get(int(pid), {}) if metafield_owner_map else {}
                    key = (info.get("key") or "").strip()
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

                else:
                    # GENERIC (o Type non gestito): traduzione “plain”
                    translated = translator.translate_field(
                        type_name,
                        field_csv or "value",
                        default,
                        target_locale,
                        dnt=dnt,
                        exclude_similarity_tokens=exclude_tokens,
                        title_translated=None,
                        preserve_handle=False,
                    )

                # Sentinel JSON error (se integrato nel Translator)
                if translated == OPENAI_JSON_ERROR_SENTINEL:
                    row["Status"] = "ERROR_OPENAI_JSON"
                    row["Translated content"] = ""
                    summary["json_errors"] += 1
                    rows_out.append(pl.DataFrame([row], schema=SCHEMA))
                    continue

                if type_name in {"PRODUCT", "COLLECTION"} and translated == "":
                    err_code = (
                        "ERROR_SIMILARITY_HTML"
                        if field_csv == "body_html"
                        else f"ERROR_SIMILARITY_{field_csv.upper()}"
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
            summary["processed_products"] += 1
            cp["updated_at"] = _now_iso()
            cp["progress"]["completed_identifications"] = sorted(list(completed))
            cp["progress"]["processed_products"] = summary["processed_products"]
            cp["stats"] = summary
            cp_new.write_text(json.dumps(cp, ensure_ascii=False, indent=2))

    except Exception:
        cp["run_status"] = "aborted"
        cp["updated_at"] = _now_iso()
        cp["stats"] = summary
        cp_new.write_text(json.dumps(cp, ensure_ascii=False, indent=2))
        _append_state_index(
            {
                "run_id": cp["run_id"],
                "input_hash": cp["input_hash"],
                "locale": cp["locale"],
                "input_basename": cp["input_basename"],
                "output_basename": cp["output_basename"],
                "ended_at": cp["updated_at"],
                "run_status": "aborted",
            }
        )
        raise
    else:
        cp["run_status"] = "completed"
        cp["updated_at"] = _now_iso()
        cp["stats"] = summary
        cp_new.write_text(json.dumps(cp, ensure_ascii=False, indent=2))
        _append_state_index(
            {
                "run_id": cp["run_id"],
                "input_hash": cp["input_hash"],
                "locale": cp["locale"],
                "input_basename": cp["input_basename"],
                "output_basename": cp["output_basename"],
                "ended_at": cp["updated_at"],
                "run_status": "completed",
            }
        )
    finally:
        summary["cache_hit"] = getattr(translator, "cache_hits", 0)
        cache.close()
        if stats:
            logger.info("summary", **summary)

    return summary


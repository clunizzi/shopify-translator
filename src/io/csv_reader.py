from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import polars as pl

REQUIRED_COLUMNS = [
    "Type",
    "Identification",
    "Field",
    "Locale",
    "Market",
    "Status",
    "Default content",
    "Translated content",
]


def read_csv(path: str | Path) -> pl.DataFrame:
    """Legge tutto il CSV in Polars, valida lo schema e ritorna un DataFrame."""
    df = pl.read_csv(path, infer_schema_length=1000)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV mancano colonne: {missing}")
    return df


def identification_order(df: pl.DataFrame) -> list[int]:
    """Ritorna la lista di Identification in ordine di prima occorrenza."""
    # Normalizza Identification a int
    ids = df["Identification"].cast(pl.Int64)
    first_idx = (
        pl.DataFrame({"Identification": ids, "__idx": pl.arange(0, len(df), eager=True)})
        .group_by("Identification")
        .agg(pl.col("__idx").min().alias("__first"))
        .sort("__first")
    )
    return first_idx["Identification"].to_list()


def iter_groups_in_input_order(df: pl.DataFrame) -> Iterable[tuple[int, pl.DataFrame]]:
    """Itera (identification, sotto-DF) nell'ordine di prima occorrenza."""
    order = identification_order(df)
    for pid in order:
        sub = df.filter(pl.col("Identification") == pid)
        yield int(pid), sub

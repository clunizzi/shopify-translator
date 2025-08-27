from __future__ import annotations

from pathlib import Path

import polars as pl

HEADER = [
    "Type",
    "Identification",
    "Field",
    "Locale",
    "Market",
    "Status",
    "Default content",
    "Translated content",
]


def init_output(path: str | Path, truncate: bool = False) -> None:
    """
    Crea il file CSV di output:
    - se truncate=True: riscrive l'header (reset totale).
    - altrimenti: crea solo se non esiste o è vuoto (append-only).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if not truncate:
        if p.exists() and p.stat().st_size > 0:
            return  # già presente → append-only

    with p.open("w", encoding="utf-8", newline="") as f:
        f.write(",".join(HEADER) + "\n")


def append_rows(path: str | Path, df: pl.DataFrame) -> None:
    """
    Appende righe al CSV già inizializzato, senza header.
    Garantisce l'ordine colonne richiesto.
    """
    ordered = df.select(HEADER)
    p = Path(path)
    with p.open("a", encoding="utf-8", newline="") as f:
        ordered.write_csv(f, include_header=False)

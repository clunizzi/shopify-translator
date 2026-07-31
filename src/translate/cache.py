from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from src.state.neon import sanitize_json_value, sanitize_text


class TranslationCache:
    def __init__(self, db_path: str | Path | None = None) -> None:
        """
        SQLite-backed cache with safe default path.
        - Default path comes from env TRANSLATION_CACHE_PATH or ':memory:'.
        - The default is in-memory so local stale cache does not persist across runs.
        - If a filesystem path is explicitly requested but unavailable, falls back to ':memory:'.
        """
        raw_path = (
            db_path if db_path is not None else os.getenv("TRANSLATION_CACHE_PATH", ":memory:")
        )
        if str(raw_path).strip() == ":memory:":
            self.path = raw_path
            self.conn = sqlite3.connect(":memory:")
            self._init()
            return

        desired = Path(raw_path)
        self.path = desired
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        except Exception:
            self.path = ":memory:"
            self.conn = sqlite3.connect(":memory:")
        self._init()

    def _init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                model TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                hits INTEGER DEFAULT 0
            )"""
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cell_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                model TEXT,
                meta  TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                hits INTEGER DEFAULT 0
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def make_key(
        type_name: str,
        field: str,
        locale: str,
        normalized_text: str,
        rules_version: int,
        model: str,
    ) -> str:
        payload = f"{type_name}|{field}|{locale}|{normalized_text}|{rules_version}|{model}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        cur = self.conn.execute("SELECT value, hits FROM translations WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return None
        value_json, hits = row
        self.conn.execute("UPDATE translations SET hits = ? WHERE key = ?", (hits + 1, key))
        self.conn.commit()
        return json.loads(value_json)

    def set(self, key: str, value: dict, model: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO translations (key, value, model) VALUES (?, ?, ?)",
            (
                sanitize_text(key),
                json.dumps(sanitize_json_value(value), ensure_ascii=False),
                sanitize_text(model),
            ),
        )
        self.conn.commit()

    # --- Cell-level cache ----------------------------------------------------
    def get_cell(self, key: str) -> dict | None:
        cur = self.conn.execute("SELECT value, meta, hits FROM cell_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return None
        value_text, meta_text, hits = row
        self.conn.execute("UPDATE cell_cache SET hits = ? WHERE key = ?", (int(hits) + 1, key))
        self.conn.commit()
        try:
            meta = json.loads(meta_text) if meta_text else {}
        except Exception:
            meta = {}
        return {"value": value_text, "meta": meta}

    def set_cell(self, key: str, value: str, model: str, meta: dict | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cell_cache (key, value, model, meta) VALUES (?, ?, ?, ?)",
            (
                sanitize_text(key),
                sanitize_text(value),
                sanitize_text(model),
                json.dumps(sanitize_json_value(meta or {}), ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

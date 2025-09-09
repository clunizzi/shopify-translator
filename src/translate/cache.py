from __future__ import annotations

import hashlib
import json
import sqlite3
import errno
import os
from pathlib import Path


class TranslationCache:
    def __init__(self, db_path: str | Path | None = None) -> None:
        """
        SQLite-backed cache with safe default path.
        - Default path comes from env TRANSLATION_CACHE_PATH or 'state/cache.sqlite'.
        - If the filesystem is read-only (e.g., AWS Lambda), falls back to '/tmp/cache.sqlite'.
        """
        desired = Path(
            db_path if db_path is not None else os.getenv("TRANSLATION_CACHE_PATH", "state/cache.sqlite")
        )
        self.path = desired
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        except Exception as e:
            # Read-only FS or invalid path: fallback to /tmp
            fallback = Path("/tmp/cache.sqlite")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            self.path = fallback
            self.conn = sqlite3.connect(self.path)
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
            (key, json.dumps(value, ensure_ascii=False), model),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

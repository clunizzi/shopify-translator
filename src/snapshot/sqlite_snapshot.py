from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Dict


DB_PATH_DEFAULT = Path("state/cache.sqlite")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SnapshotStore:
    """
    Minimal snapshot store living in the same SQLite file as the translation cache.
    Table: snapshot_translatable(resource_id, key, digest, updated_at)
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        """
        Snapshot store shares the same SQLite file as the translation cache by default.
        - If `db_path` is None, use env TRANSLATION_CACHE_PATH or 'state/cache.sqlite'.
        - If the chosen path is not writable (e.g., AWS Lambda root FS), fallback to '/tmp/cache.sqlite'.
        """
        chosen = Path(db_path) if db_path is not None else Path(
            os.getenv("TRANSLATION_CACHE_PATH", str(DB_PATH_DEFAULT))
        )
        self.path = chosen
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        except Exception:
            # read-only FS or invalid parent; fallback to /tmp to ensure write access
            self.path = Path("/tmp/cache.sqlite")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        self._init()

    def _init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_translatable (
              resource_id TEXT NOT NULL,
              key         TEXT NOT NULL,
              digest      TEXT NOT NULL,
              updated_at  TEXT NOT NULL,
              PRIMARY KEY (resource_id, key)
            )
            """
        )
        self.conn.commit()

    def get_digest_map(self, resource_id: str) -> Dict[str, str]:
        cur = self.conn.execute(
            "SELECT key, digest FROM snapshot_translatable WHERE resource_id = ?",
            (resource_id,),
        )
        return {k: d for k, d in cur.fetchall()}

    def set_digest_map(self, resource_id: str, digest_map: dict[str, str]) -> None:
        # Upsert in a transaction
        now = _utc_iso()
        with self.conn:
            for k, d in digest_map.items():
                self.conn.execute(
                    """
                    INSERT INTO snapshot_translatable(resource_id, key, digest, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(resource_id, key)
                    DO UPDATE SET digest=excluded.digest, updated_at=excluded.updated_at
                    """,
                    (resource_id, k, d, now),
                )

    def upsert_digest(self, resource_id: str, key: str, digest: str) -> None:
        now = _utc_iso()
        self.conn.execute(
            """
            INSERT INTO snapshot_translatable(resource_id, key, digest, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(resource_id, key)
            DO UPDATE SET digest=excluded.digest, updated_at=excluded.updated_at
            """,
            (resource_id, key, digest, now),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

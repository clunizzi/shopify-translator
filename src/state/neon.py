from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_source_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8", errors="ignore")).hexdigest()


def default_sslrootcert() -> str | None:
    explicit = os.getenv("PGSSLROOTCERT", "").strip()
    if explicit:
        return explicit
    candidates = [
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/usr/lib/ssl/cert.pem",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


@dataclass(frozen=True)
class PDPSourceRecord:
    shop_domain: str
    product_gid: str
    source_locale: str
    document: dict[str, Any]
    section_hashes: dict[str, str]
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PDPTranslationRecord:
    shop_domain: str
    product_gid: str
    target_locale: str
    document: dict[str, Any]
    section_hashes: dict[str, str]
    status: str
    model: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PDPTranslationState:
    section_hashes: dict[str, str]
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ThemeSourceRecord:
    shop_domain: str
    theme_id: str
    resource_type: str
    resource_id: str
    source_locale: str
    document: dict[str, Any]
    section_hashes: dict[str, str]
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ThemeTranslationRecord:
    shop_domain: str
    theme_id: str
    resource_type: str
    resource_id: str
    target_locale: str
    document: dict[str, Any]
    section_hashes: dict[str, str]
    status: str
    model: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ThemeTranslationState:
    document: dict[str, Any] | None
    section_hashes: dict[str, str]
    status: str
    metadata: dict[str, Any]


class NeonTranslationStore:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.getenv("NEON_DATABASE_URL", "")
        if not self.dsn:
            raise RuntimeError("NEON_DATABASE_URL mancante")
        self._conn = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import psycopg
        except Exception as e:  # pragma: no cover
            raise RuntimeError("psycopg non disponibile; aggiungi la dipendenza per Neon/PostgreSQL") from e
        extra: dict[str, str] = {}
        if "sslrootcert=" not in self.dsn:
            sslrootcert = default_sslrootcert()
            if sslrootcert:
                extra["sslrootcert"] = sslrootcert
        if "sslmode=" not in self.dsn and ("sslrootcert" in extra or os.getenv("PGSSLROOTCERT")):
            extra["sslmode"] = "verify-full"
        self._conn = psycopg.connect(self.dsn, **extra)
        self._conn.autocommit = True
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def ensure_schema(self) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pdp_source_state (
                  shop_domain TEXT NOT NULL,
                  product_gid TEXT NOT NULL,
                  source_locale TEXT NOT NULL,
                  document JSONB NOT NULL,
                  section_hashes JSONB NOT NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (shop_domain, product_gid, source_locale)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pdp_translation_state (
                  shop_domain TEXT NOT NULL,
                  product_gid TEXT NOT NULL,
                  target_locale TEXT NOT NULL,
                  document JSONB NOT NULL,
                  section_hashes JSONB NOT NULL,
                  status TEXT NOT NULL,
                  model TEXT NOT NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (shop_domain, product_gid, target_locale)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_memory (
                  source_hash TEXT NOT NULL,
                  field_key TEXT NOT NULL,
                  source_locale TEXT NOT NULL,
                  target_locale TEXT NOT NULL,
                  source_value TEXT NOT NULL,
                  translated_value TEXT NOT NULL,
                  model TEXT NOT NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (source_hash, field_key, source_locale, target_locale)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_dictionary (
                  category TEXT NOT NULL,
                  source_locale TEXT NOT NULL,
                  target_locale TEXT NOT NULL,
                  source_value TEXT NOT NULL,
                  translated_value TEXT NOT NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (category, source_locale, target_locale, source_value)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS theme_source_state (
                  shop_domain TEXT NOT NULL,
                  theme_id TEXT NOT NULL,
                  resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  source_locale TEXT NOT NULL,
                  document JSONB NOT NULL,
                  section_hashes JSONB NOT NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (shop_domain, theme_id, resource_type, resource_id, source_locale)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS theme_translation_state (
                  shop_domain TEXT NOT NULL,
                  theme_id TEXT NOT NULL,
                  resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  target_locale TEXT NOT NULL,
                  document JSONB NOT NULL,
                  section_hashes JSONB NOT NULL,
                  status TEXT NOT NULL,
                  model TEXT NOT NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (shop_domain, theme_id, resource_type, resource_id, target_locale)
                )
                """
            )

    def reset_schema(self) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS theme_translation_state")
            cur.execute("DROP TABLE IF EXISTS theme_source_state")
            cur.execute("DROP TABLE IF EXISTS pdp_translation_state")
            cur.execute("DROP TABLE IF EXISTS pdp_source_state")
            cur.execute("DROP TABLE IF EXISTS translation_memory")
            cur.execute("DROP TABLE IF EXISTS translation_dictionary")
            # Drop previous field-oriented prototypes too, if present.
            cur.execute("DROP TABLE IF EXISTS translation_state")
            cur.execute("DROP TABLE IF EXISTS source_field_state")
        self.ensure_schema()

    def has_pdp_source(self, *, shop_domain: str, product_gid: str, source_locale: str) -> bool:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM pdp_source_state
                WHERE shop_domain = %s
                  AND product_gid = %s
                  AND source_locale = %s
                LIMIT 1
                """,
                (shop_domain, product_gid, source_locale),
            )
            return cur.fetchone() is not None

    def get_pdp_source_hashes(
        self,
        *,
        shop_domain: str,
        product_gid: str,
        source_locale: str,
    ) -> dict[str, str]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT section_hashes
                FROM pdp_source_state
                WHERE shop_domain = %s
                  AND product_gid = %s
                  AND source_locale = %s
                """,
                (shop_domain, product_gid, source_locale),
            )
            row = cur.fetchone()
            if not row:
                return {}
            data = row[0] or {}
            return {str(k): str(v) for k, v in dict(data).items()}

    def upsert_pdp_source(self, record: PDPSourceRecord) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pdp_source_state (
                  shop_domain, product_gid, source_locale,
                  document, section_hashes, metadata, updated_at
                )
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (shop_domain, product_gid, source_locale)
                DO UPDATE SET
                  document = EXCLUDED.document,
                  section_hashes = EXCLUDED.section_hashes,
                  metadata = EXCLUDED.metadata,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    record.shop_domain,
                    record.product_gid,
                    record.source_locale,
                    json.dumps(record.document, ensure_ascii=False),
                    json.dumps(record.section_hashes, ensure_ascii=False),
                    json.dumps(record.metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def upsert_pdp_translation(self, record: PDPTranslationRecord) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pdp_translation_state (
                  shop_domain, product_gid, target_locale,
                  document, section_hashes, status, model, metadata, updated_at
                )
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s)
                ON CONFLICT (shop_domain, product_gid, target_locale)
                DO UPDATE SET
                  document = EXCLUDED.document,
                  section_hashes = EXCLUDED.section_hashes,
                  status = EXCLUDED.status,
                  model = EXCLUDED.model,
                  metadata = EXCLUDED.metadata,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    record.shop_domain,
                    record.product_gid,
                    record.target_locale,
                    json.dumps(record.document, ensure_ascii=False),
                    json.dumps(record.section_hashes, ensure_ascii=False),
                    record.status,
                    record.model,
                    json.dumps(record.metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def get_pdp_translation_state(
        self,
        *,
        shop_domain: str,
        product_gid: str,
        target_locale: str,
    ) -> PDPTranslationState | None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT section_hashes, status, metadata
                FROM pdp_translation_state
                WHERE shop_domain = %s
                  AND product_gid = %s
                  AND target_locale = %s
                """,
                (shop_domain, product_gid, target_locale),
            )
            row = cur.fetchone()
            if not row:
                return None
            section_hashes = {str(k): str(v) for k, v in dict(row[0] or {}).items()}
            status = str(row[1] or "")
            metadata = dict(row[2] or {})
            return PDPTranslationState(
                section_hashes=section_hashes,
                status=status,
                metadata=metadata,
            )

    def get_translation_memory(
        self,
        *,
        source_hash: str,
        field_key: str,
        source_locale: str,
        target_locale: str,
    ) -> str | None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT translated_value
                FROM translation_memory
                WHERE source_hash = %s
                  AND field_key = %s
                  AND source_locale = %s
                  AND target_locale = %s
                """,
                (source_hash, field_key, source_locale, target_locale),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def upsert_translation_memory(
        self,
        *,
        source_hash: str,
        field_key: str,
        source_locale: str,
        target_locale: str,
        source_value: str,
        translated_value: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO translation_memory (
                  source_hash, field_key, source_locale, target_locale,
                  source_value, translated_value, model, metadata, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (source_hash, field_key, source_locale, target_locale)
                DO UPDATE SET
                  source_value = EXCLUDED.source_value,
                  translated_value = EXCLUDED.translated_value,
                  model = EXCLUDED.model,
                  metadata = EXCLUDED.metadata,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    source_hash,
                    field_key,
                    source_locale,
                    target_locale,
                    source_value,
                    translated_value,
                    model,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def get_dictionary_translation(
        self,
        *,
        category: str,
        source_locale: str,
        target_locale: str,
        source_value: str,
    ) -> str | None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT translated_value
                FROM translation_dictionary
                WHERE category = %s
                  AND source_locale = %s
                  AND target_locale = %s
                  AND source_value = %s
                """,
                (category, source_locale, target_locale, source_value),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def upsert_dictionary_translation(
        self,
        *,
        category: str,
        source_locale: str,
        target_locale: str,
        source_value: str,
        translated_value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO translation_dictionary (
                  category, source_locale, target_locale,
                  source_value, translated_value, metadata, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (category, source_locale, target_locale, source_value)
                DO UPDATE SET
                  translated_value = EXCLUDED.translated_value,
                  metadata = EXCLUDED.metadata,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    category,
                    source_locale,
                    target_locale,
                    source_value,
                    translated_value,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def has_theme_source(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        resource_type: str,
        resource_id: str,
        source_locale: str,
    ) -> bool:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM theme_source_state
                WHERE shop_domain = %s
                  AND theme_id = %s
                  AND resource_type = %s
                  AND resource_id = %s
                  AND source_locale = %s
                LIMIT 1
                """,
                (shop_domain, theme_id, resource_type, resource_id, source_locale),
            )
            return cur.fetchone() is not None

    def get_theme_source_hashes(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        resource_type: str,
        resource_id: str,
        source_locale: str,
    ) -> dict[str, str]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT section_hashes
                FROM theme_source_state
                WHERE shop_domain = %s
                  AND theme_id = %s
                  AND resource_type = %s
                  AND resource_id = %s
                  AND source_locale = %s
                """,
                (shop_domain, theme_id, resource_type, resource_id, source_locale),
            )
            row = cur.fetchone()
            if not row:
                return {}
            data = row[0] or {}
            return {str(k): str(v) for k, v in dict(data).items()}

    def upsert_theme_source(self, record: ThemeSourceRecord) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO theme_source_state (
                  shop_domain, theme_id, resource_type, resource_id, source_locale,
                  document, section_hashes, metadata, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                ON CONFLICT (shop_domain, theme_id, resource_type, resource_id, source_locale)
                DO UPDATE SET
                  document = EXCLUDED.document,
                  section_hashes = EXCLUDED.section_hashes,
                  metadata = EXCLUDED.metadata,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    record.shop_domain,
                    record.theme_id,
                    record.resource_type,
                    record.resource_id,
                    record.source_locale,
                    json.dumps(record.document, ensure_ascii=False),
                    json.dumps(record.section_hashes, ensure_ascii=False),
                    json.dumps(record.metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def get_theme_translation_state(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        resource_type: str,
        resource_id: str,
        target_locale: str,
    ) -> ThemeTranslationState | None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT document, section_hashes, status, metadata
                FROM theme_translation_state
                WHERE shop_domain = %s
                  AND theme_id = %s
                  AND resource_type = %s
                  AND resource_id = %s
                  AND target_locale = %s
                """,
                (shop_domain, theme_id, resource_type, resource_id, target_locale),
            )
            row = cur.fetchone()
            if not row:
                return None
            document = dict(row[0] or {})
            section_hashes = {str(k): str(v) for k, v in dict(row[1] or {}).items()}
            status = str(row[2] or "")
            metadata = dict(row[3] or {})
            return ThemeTranslationState(
                document=document,
                section_hashes=section_hashes,
                status=status,
                metadata=metadata,
            )

    def upsert_theme_translation(self, record: ThemeTranslationRecord) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO theme_translation_state (
                  shop_domain, theme_id, resource_type, resource_id, target_locale,
                  document, section_hashes, status, model, metadata, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s)
                ON CONFLICT (shop_domain, theme_id, resource_type, resource_id, target_locale)
                DO UPDATE SET
                  document = EXCLUDED.document,
                  section_hashes = EXCLUDED.section_hashes,
                  status = EXCLUDED.status,
                  model = EXCLUDED.model,
                  metadata = EXCLUDED.metadata,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    record.shop_domain,
                    record.theme_id,
                    record.resource_type,
                    record.resource_id,
                    record.target_locale,
                    json.dumps(record.document, ensure_ascii=False),
                    json.dumps(record.section_hashes, ensure_ascii=False),
                    record.status,
                    record.model,
                    json.dumps(record.metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

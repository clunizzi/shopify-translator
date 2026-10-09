from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sanitize_text(value: str) -> str:
    return (value or "").replace("\x00", "")


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): sanitize_json_value(v) for k, v in value.items()}
    return value


def make_source_hash(value: str) -> str:
    return hashlib.sha256(sanitize_text(value).encode("utf-8", errors="ignore")).hexdigest()


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
    document: dict[str, Any] | None
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


@dataclass(frozen=True)
class ThemeFileRecord:
    shop_domain: str
    theme_id: str
    filename: str
    checksum_md5: str
    content_type: str
    size: int
    file_updated_at: str


class NeonTranslationStore:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.getenv("NEON_DATABASE_URL", "")
        if not self.dsn:
            raise RuntimeError("NEON_DATABASE_URL mancante")
        self._conn = None

    def _connect(self):
        if self._conn is not None and not getattr(self._conn, "closed", False):
            return self._conn
        self._conn = None
        try:
            import psycopg
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "psycopg non disponibile; aggiungi la dipendenza per Neon/PostgreSQL"
            ) from e
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

    def _reset_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @staticmethod
    def _is_retryable_db_error(exc: Exception) -> bool:
        message = str(exc).lower()
        retryable_fragments = (
            "connection is bad",
            "connection already closed",
            "consuming input failed",
            "server closed the connection",
            "ssl error",
            "unexpected eof",
            "terminating connection",
        )
        return any(fragment in message for fragment in retryable_fragments)

    def _run_db(self, operation: Callable[[Any], T]) -> T:
        for attempt in range(2):
            conn = self._connect()
            try:
                return operation(conn)
            except Exception as exc:
                if attempt == 0 and self._is_retryable_db_error(exc):
                    self._reset_connection()
                    continue
                raise
        raise RuntimeError("Neon operation retry exhausted")

    def close(self) -> None:
        if self._conn is not None:
            self._reset_connection()

    def ensure_schema(self) -> None:
        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute("""
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
                    """)
                cur.execute("""
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
                    """)
                cur.execute("""
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
                    """)
                cur.execute("""
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
                    """)
                cur.execute("""
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
                    """)
                cur.execute("""
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
                    """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS theme_file_state (
                      shop_domain TEXT NOT NULL,
                      theme_id TEXT NOT NULL,
                      filename TEXT NOT NULL,
                      checksum_md5 TEXT NOT NULL,
                      content_type TEXT NOT NULL,
                      size BIGINT NOT NULL,
                      file_updated_at TEXT NOT NULL,
                      seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      PRIMARY KEY (shop_domain, theme_id, filename)
                    )
                    """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS theme_change_events (
                      event_id TEXT PRIMARY KEY,
                      shop_domain TEXT NOT NULL,
                      approved_theme_id TEXT NOT NULL,
                      actual_theme_id TEXT NOT NULL,
                      theme_name TEXT NOT NULL,
                      theme_role TEXT NOT NULL,
                      topic TEXT NOT NULL,
                      status TEXT NOT NULL,
                      details JSONB NOT NULL DEFAULT '{}'::jsonb,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS admin_jobs (
                      id UUID PRIMARY KEY,
                      shop_domain TEXT NOT NULL,
                      action TEXT NOT NULL
                        CHECK (action IN ('theme_audit', 'theme_canary', 'theme_sync')),
                      status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                      actor_email TEXT NOT NULL,
                      request JSONB NOT NULL DEFAULT '{}'::jsonb,
                      result JSONB NOT NULL DEFAULT '{}'::jsonb,
                      error TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      started_at TIMESTAMPTZ,
                      finished_at TIMESTAMPTZ,
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS admin_jobs_shop_created_idx
                    ON admin_jobs (shop_domain, created_at DESC)
                    """)
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS admin_jobs_one_active_theme_idx
                    ON admin_jobs (shop_domain)
                    WHERE status IN ('queued', 'running')
                    """)

        self._run_db(run)

    def reset_schema(self) -> None:
        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS theme_translation_state")
                cur.execute("DROP TABLE IF EXISTS theme_change_events")
                cur.execute("DROP TABLE IF EXISTS theme_file_state")
                cur.execute("DROP TABLE IF EXISTS theme_source_state")
                cur.execute("DROP TABLE IF EXISTS admin_jobs")
                cur.execute("DROP TABLE IF EXISTS pdp_translation_state")
                cur.execute("DROP TABLE IF EXISTS pdp_source_state")
                cur.execute("DROP TABLE IF EXISTS translation_memory")
                cur.execute("DROP TABLE IF EXISTS translation_dictionary")
                # Drop previous field-oriented prototypes too, if present.
                cur.execute("DROP TABLE IF EXISTS translation_state")
                cur.execute("DROP TABLE IF EXISTS source_field_state")

        self._run_db(run)
        self.ensure_schema()

    def claim_admin_job(self, *, job_id: str, shop_domain: str) -> bool:
        def run(conn: Any) -> bool:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_jobs
                    SET
                      status = 'running',
                      attempts = attempts + 1,
                      started_at = COALESCE(started_at, NOW()),
                      finished_at = NULL,
                      error = NULL,
                      updated_at = NOW()
                    WHERE id = %s::uuid
                      AND shop_domain = %s
                      AND (
                        status = 'queued'
                        OR (status = 'failed' AND attempts < 3)
                      )
                    RETURNING id
                    """,
                    (job_id, shop_domain),
                )
                return cur.fetchone() is not None

        return self._run_db(run)

    def complete_admin_job(
        self,
        *,
        job_id: str,
        shop_domain: str,
        result: dict[str, Any],
    ) -> None:
        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_jobs
                    SET
                      status = 'succeeded',
                      result = %s::jsonb,
                      error = NULL,
                      finished_at = NOW(),
                      updated_at = NOW()
                    WHERE id = %s::uuid
                      AND shop_domain = %s
                      AND status = 'running'
                    """,
                    (
                        json.dumps(sanitize_json_value(result), ensure_ascii=False),
                        job_id,
                        shop_domain,
                    ),
                )

        self._run_db(run)

    def fail_admin_job(
        self,
        *,
        job_id: str,
        shop_domain: str,
        error: str,
    ) -> None:
        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE admin_jobs
                    SET
                      status = 'failed',
                      error = %s,
                      finished_at = NOW(),
                      updated_at = NOW()
                    WHERE id = %s::uuid
                      AND shop_domain = %s
                      AND status = 'running'
                    """,
                    (sanitize_text(error)[:1000], job_id, shop_domain),
                )

        self._run_db(run)

    def has_pdp_source(self, *, shop_domain: str, product_gid: str, source_locale: str) -> bool:
        def run(conn: Any) -> bool:
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

        return self._run_db(run)

    def get_pdp_source_hashes(
        self,
        *,
        shop_domain: str,
        product_gid: str,
        source_locale: str,
    ) -> dict[str, str]:
        def run(conn: Any) -> dict[str, str]:
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

        return self._run_db(run)

    def upsert_pdp_source(self, record: PDPSourceRecord) -> None:
        def run(conn: Any) -> None:
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
                        json.dumps(sanitize_json_value(record.document), ensure_ascii=False),
                        json.dumps(sanitize_json_value(record.section_hashes), ensure_ascii=False),
                        json.dumps(sanitize_json_value(record.metadata or {}), ensure_ascii=False),
                        _utc_now(),
                    ),
                )

        self._run_db(run)

    def upsert_pdp_translation(self, record: PDPTranslationRecord) -> None:
        def run(conn: Any) -> None:
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
                        json.dumps(sanitize_json_value(record.document), ensure_ascii=False),
                        json.dumps(sanitize_json_value(record.section_hashes), ensure_ascii=False),
                        record.status,
                        record.model,
                        json.dumps(sanitize_json_value(record.metadata or {}), ensure_ascii=False),
                        _utc_now(),
                    ),
                )

        self._run_db(run)

    def get_pdp_translation_state(
        self,
        *,
        shop_domain: str,
        product_gid: str,
        target_locale: str,
    ) -> PDPTranslationState | None:
        def run(conn: Any) -> PDPTranslationState | None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT document, section_hashes, status, metadata
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
                document = dict(row[0] or {})
                section_hashes = {str(k): str(v) for k, v in dict(row[1] or {}).items()}
                status = str(row[2] or "")
                metadata = dict(row[3] or {})
                return PDPTranslationState(
                    document=document,
                    section_hashes=section_hashes,
                    status=status,
                    metadata=metadata,
                )

        return self._run_db(run)

    def list_pdp_reconciliation_candidates(
        self,
        *,
        shop_domain: str,
        source_locale: str,
        target_locales: list[str],
        statuses: list[str],
        limit: int,
        after_product_gid: str = "",
    ) -> list[dict[str, Any]]:
        if not target_locales or not statuses or limit <= 0:
            return []

        def run(conn: Any) -> list[dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH requested_locales AS (
                      SELECT UNNEST(%s::text[]) AS target_locale
                    )
                    SELECT
                      source.product_gid,
                      source.document,
                      source.section_hashes,
                      jsonb_object_agg(
                        requested.target_locale,
                        jsonb_build_object(
                          'status', COALESCE(translation.status, 'missing'),
                          'model', COALESCE(translation.model, 'shopify-live-reconcile'),
                          'metadata', COALESCE(translation.metadata, '{}'::jsonb),
                          'document', COALESCE(translation.document, '{}'::jsonb),
                          'section_hashes', COALESCE(translation.section_hashes, '{}'::jsonb)
                        )
                      ) AS translations
                    FROM pdp_source_state AS source
                    CROSS JOIN requested_locales AS requested
                    LEFT JOIN pdp_translation_state AS translation
                      ON translation.shop_domain = source.shop_domain
                     AND translation.product_gid = source.product_gid
                     AND translation.target_locale = requested.target_locale
                    WHERE source.shop_domain = %s
                      AND source.source_locale = %s
                      AND (
                        translation.status = ANY(%s)
                        OR (
                          translation.product_gid IS NULL
                          AND 'missing' = ANY(%s)
                        )
                      )
                      AND source.product_gid > %s
                    GROUP BY
                      source.product_gid,
                      source.document,
                      source.section_hashes
                    ORDER BY source.product_gid
                    LIMIT %s
                    """,
                    (
                        target_locales,
                        shop_domain,
                        source_locale,
                        statuses,
                        statuses,
                        after_product_gid,
                        int(limit),
                    ),
                )
                return [
                    {
                        "product_gid": str(row[0]),
                        "source_document": dict(row[1] or {}),
                        "source_hashes": {
                            str(key): str(value) for key, value in dict(row[2] or {}).items()
                        },
                        "translations": dict(row[3] or {}),
                    }
                    for row in cur.fetchall()
                ]

        return self._run_db(run)

    def count_pdp_translation_statuses(
        self,
        *,
        shop_domain: str,
        target_locales: list[str],
    ) -> dict[str, dict[str, int]]:
        if not target_locales:
            return {}

        def run(conn: Any) -> dict[str, dict[str, int]]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT target_locale, status, COUNT(*)
                    FROM pdp_translation_state
                    WHERE shop_domain = %s
                      AND target_locale = ANY(%s)
                    GROUP BY target_locale, status
                    ORDER BY target_locale, status
                    """,
                    (shop_domain, target_locales),
                )
                result: dict[str, dict[str, int]] = {}
                for locale, status, count in cur.fetchall():
                    result.setdefault(str(locale), {})[str(status)] = int(count)
                return result

        return self._run_db(run)

    def get_translation_memory(
        self,
        *,
        source_hash: str,
        field_key: str,
        source_locale: str,
        target_locale: str,
    ) -> str | None:
        def run(conn: Any) -> str | None:
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

        return self._run_db(run)

    def get_translation_memory_map(
        self,
        *,
        source_locale: str,
        target_locale: str,
    ) -> dict[tuple[str, str], str]:
        """Load exact source/field translations in one query.

        Theme IDs deliberately aren't part of the key: an unchanged field can be
        reused safely when a cloned or newly published theme has the same source
        value and semantic field key.
        """

        def run(conn: Any) -> dict[tuple[str, str], str]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_hash, field_key, translated_value
                    FROM translation_memory
                    WHERE source_locale = %s
                      AND target_locale = %s
                    """,
                    (source_locale, target_locale),
                )
                return {
                    (str(source_hash), str(field_key)): str(translated_value)
                    for source_hash, field_key, translated_value in cur.fetchall()
                    if translated_value is not None and str(translated_value).strip()
                }

        return self._run_db(run)

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
        def run(conn: Any) -> None:
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
                        sanitize_text(source_value),
                        sanitize_text(translated_value),
                        sanitize_text(model),
                        json.dumps(sanitize_json_value(metadata or {}), ensure_ascii=False),
                        _utc_now(),
                    ),
                )

        self._run_db(run)

    def get_dictionary_translation(
        self,
        *,
        category: str,
        source_locale: str,
        target_locale: str,
        source_value: str,
    ) -> str | None:
        def run(conn: Any) -> str | None:
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

        return self._run_db(run)

    def get_dictionary_translations(
        self,
        *,
        category: str,
        source_locale: str,
        target_locale: str,
        source_values: list[str],
    ) -> dict[str, str]:
        """Load a bounded set of dictionary entries in one Neon round trip."""

        values = list(dict.fromkeys(value for value in source_values if value.strip()))
        if not values:
            return {}

        def run(conn: Any) -> dict[str, str]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_value, translated_value
                    FROM translation_dictionary
                    WHERE category = %s
                      AND source_locale = %s
                      AND target_locale = %s
                      AND source_value = ANY(%s)
                    """,
                    (category, source_locale, target_locale, values),
                )
                return {
                    str(source_value): str(translated_value)
                    for source_value, translated_value in cur.fetchall()
                    if translated_value is not None and str(translated_value).strip()
                }

        return self._run_db(run)

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
        def run(conn: Any) -> None:
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
                        sanitize_text(category),
                        source_locale,
                        target_locale,
                        sanitize_text(source_value),
                        sanitize_text(translated_value),
                        json.dumps(sanitize_json_value(metadata or {}), ensure_ascii=False),
                        _utc_now(),
                    ),
                )

        self._run_db(run)

    def upsert_dictionary_translations(
        self,
        *,
        category: str,
        source_locale: str,
        target_locale: str,
        translations: dict[str, str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist multiple shared leaf translations in one transaction."""

        rows = [
            (source_value, translated_value)
            for source_value, translated_value in translations.items()
            if source_value.strip() and translated_value.strip()
        ]
        if not rows:
            return

        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.executemany(
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
                    [
                        (
                            sanitize_text(category),
                            source_locale,
                            target_locale,
                            sanitize_text(source_value),
                            sanitize_text(translated_value),
                            json.dumps(sanitize_json_value(metadata or {}), ensure_ascii=False),
                            _utc_now(),
                        )
                        for source_value, translated_value in rows
                    ],
                )

        self._run_db(run)

    def has_theme_source(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        resource_type: str,
        resource_id: str,
        source_locale: str,
    ) -> bool:
        def run(conn: Any) -> bool:
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

        return self._run_db(run)

    def get_theme_source_hashes(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        resource_type: str,
        resource_id: str,
        source_locale: str,
    ) -> dict[str, str]:
        def run(conn: Any) -> dict[str, str]:
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

        return self._run_db(run)

    def upsert_theme_source(self, record: ThemeSourceRecord) -> None:
        def run(conn: Any) -> None:
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
                        json.dumps(sanitize_json_value(record.document), ensure_ascii=False),
                        json.dumps(sanitize_json_value(record.section_hashes), ensure_ascii=False),
                        json.dumps(sanitize_json_value(record.metadata or {}), ensure_ascii=False),
                        _utc_now(),
                    ),
                )

        self._run_db(run)

    def get_theme_translation_state(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        resource_type: str,
        resource_id: str,
        target_locale: str,
    ) -> ThemeTranslationState | None:
        def run(conn: Any) -> ThemeTranslationState | None:
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

        return self._run_db(run)

    def upsert_theme_translation(self, record: ThemeTranslationRecord) -> None:
        def run(conn: Any) -> None:
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
                        json.dumps(sanitize_json_value(record.document), ensure_ascii=False),
                        json.dumps(sanitize_json_value(record.section_hashes), ensure_ascii=False),
                        record.status,
                        record.model,
                        json.dumps(sanitize_json_value(record.metadata or {}), ensure_ascii=False),
                        _utc_now(),
                    ),
                )

        self._run_db(run)

    def get_theme_file_snapshot(
        self,
        *,
        shop_domain: str,
        theme_id: str,
    ) -> dict[str, dict[str, Any]]:
        def run(conn: Any) -> dict[str, dict[str, Any]]:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT filename, checksum_md5, content_type, size, file_updated_at
                    FROM theme_file_state
                    WHERE shop_domain = %s AND theme_id = %s
                    """,
                    (shop_domain, theme_id),
                )
                return {
                    str(row[0]): {
                        "filename": str(row[0]),
                        "checksum_md5": str(row[1] or ""),
                        "content_type": str(row[2] or ""),
                        "size": int(row[3] or 0),
                        "updated_at": str(row[4] or ""),
                    }
                    for row in cur.fetchall()
                }

        return self._run_db(run)

    def replace_theme_file_snapshot(
        self,
        *,
        shop_domain: str,
        theme_id: str,
        records: list[ThemeFileRecord],
    ) -> None:
        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                filenames = [record.filename for record in records]
                for record in records:
                    cur.execute(
                        """
                        INSERT INTO theme_file_state (
                          shop_domain, theme_id, filename, checksum_md5,
                          content_type, size, file_updated_at, seen_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (shop_domain, theme_id, filename)
                        DO UPDATE SET
                          checksum_md5 = EXCLUDED.checksum_md5,
                          content_type = EXCLUDED.content_type,
                          size = EXCLUDED.size,
                          file_updated_at = EXCLUDED.file_updated_at,
                          seen_at = NOW()
                        """,
                        (
                            shop_domain,
                            theme_id,
                            record.filename,
                            record.checksum_md5,
                            record.content_type,
                            int(record.size),
                            record.file_updated_at,
                        ),
                    )
                if filenames:
                    cur.execute(
                        """
                        DELETE FROM theme_file_state
                        WHERE shop_domain = %s
                          AND theme_id = %s
                          AND NOT (filename = ANY(%s))
                        """,
                        (shop_domain, theme_id, filenames),
                    )
                else:
                    cur.execute(
                        "DELETE FROM theme_file_state WHERE shop_domain = %s AND theme_id = %s",
                        (shop_domain, theme_id),
                    )

        self._run_db(run)

    def append_theme_change_event(
        self,
        *,
        event_id: str,
        shop_domain: str,
        approved_theme_id: str,
        actual_theme_id: str,
        theme_name: str,
        theme_role: str,
        topic: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        def run(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO theme_change_events (
                      event_id, shop_domain, approved_theme_id, actual_theme_id,
                      theme_name, theme_role, topic, status, details, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        sanitize_text(event_id),
                        sanitize_text(shop_domain),
                        sanitize_text(approved_theme_id),
                        sanitize_text(actual_theme_id),
                        sanitize_text(theme_name),
                        sanitize_text(theme_role),
                        sanitize_text(topic),
                        sanitize_text(status),
                        json.dumps(sanitize_json_value(details), ensure_ascii=False),
                    ),
                )

        self._run_db(run)

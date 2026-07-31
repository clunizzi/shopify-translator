#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

TABLES = (
    "pdp_source_state",
    "pdp_translation_state",
    "translation_memory",
    "translation_dictionary",
    "theme_source_state",
    "theme_translation_state",
)

INVENTORY_SQL = """
SELECT *
FROM (
  SELECT 'pdp_source_state' AS table_name, COUNT(*)::bigint AS row_count,
         COALESCE(MAX(updated_at)::text, '') AS latest_update
  FROM pdp_source_state
  UNION ALL
  SELECT 'pdp_translation_state', COUNT(*)::bigint, COALESCE(MAX(updated_at)::text, '')
  FROM pdp_translation_state
  UNION ALL
  SELECT 'theme_source_state', COUNT(*)::bigint, COALESCE(MAX(updated_at)::text, '')
  FROM theme_source_state
  UNION ALL
  SELECT 'theme_translation_state', COUNT(*)::bigint, COALESCE(MAX(updated_at)::text, '')
  FROM theme_translation_state
  UNION ALL
  SELECT 'translation_dictionary', COUNT(*)::bigint, COALESCE(MAX(updated_at)::text, '')
  FROM translation_dictionary
  UNION ALL
  SELECT 'translation_memory', COUNT(*)::bigint, COALESCE(MAX(updated_at)::text, '')
  FROM translation_memory
) AS inventory
ORDER BY table_name
"""


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"Required command not found: {name}")
    return path


def libpq_env(dsn: str) -> dict[str, str]:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        fail("Database URL must use the postgres or postgresql scheme.")
    if not parsed.hostname or not parsed.username or not parsed.path.lstrip("/"):
        fail("Database URL is missing host, user or database name.")

    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("PG"):
            env.pop(key, None)

    env.update(
        {
            "PGHOST": parsed.hostname,
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": unquote(parsed.username),
            "PGPASSWORD": unquote(parsed.password or ""),
            "PGDATABASE": unquote(parsed.path.lstrip("/")),
            "PGCONNECT_TIMEOUT": "20",
        }
    )

    query = parse_qs(parsed.query, keep_blank_values=True)
    query_to_env = {
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "channel_binding": "PGCHANNELBINDING",
        "application_name": "PGAPPNAME",
        "options": "PGOPTIONS",
    }
    for query_key, env_key in query_to_env.items():
        values = query.get(query_key)
        if values and values[-1]:
            env[env_key] = values[-1]

    if "PGSSLROOTCERT" not in env:
        for candidate in (
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/usr/lib/ssl/cert.pem",
        ):
            if Path(candidate).is_file():
                env["PGSSLROOTCERT"] = candidate
                break
    return env


def run_text(command: list[str], *, env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            command,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown database error").strip()
        raise RuntimeError(f"{Path(command[0]).name} failed: {detail}") from None
    return completed.stdout.strip()


def postgres_major(psql: str, *, env: dict[str, str]) -> int:
    version_num = int(run_text([psql, "-X", "-v", "ON_ERROR_STOP=1", "-Atc", "SHOW server_version_num"], env=env))
    return version_num // 10000


def matching_postgres_binary(name: str, major: int) -> str:
    configured = os.getenv(f"PG_{name.upper()}_BIN", "")
    candidates = [
        configured,
        f"/usr/lib/postgresql/{major}/bin/{name}",
        shutil.which(name) or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    fail(f"No compatible {name} binary found for PostgreSQL {major}.", 3)
    raise AssertionError("unreachable")


def binary_major(binary: str) -> int:
    output = subprocess.run(
        [binary, "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for token in output.split():
        if token[:1].isdigit():
            return int(token.split(".", 1)[0])
    fail(f"Unable to determine PostgreSQL utility version: {binary}", 3)
    raise AssertionError("unreachable")


def main() -> None:
    source_dsn = os.getenv("SOURCE_DATABASE_URL", "")
    target_dsn = os.getenv("TARGET_DATABASE_URL", "")
    if not source_dsn:
        fail("Missing required environment variable: SOURCE_DATABASE_URL")
    if not target_dsn:
        fail("Missing required environment variable: TARGET_DATABASE_URL")
    if source_dsn == target_dsn:
        fail("Source and target database URLs must be different.")
    if "-pooler." in source_dsn or "-pooler." in target_dsn:
        fail("Use direct, unpooled Neon connection strings for both source and target.")

    psql = require_command("psql")
    source_env = libpq_env(source_dsn)
    target_env = libpq_env(target_dsn)
    source_major = postgres_major(psql, env=source_env)
    target_major = postgres_major(psql, env=target_env)
    if source_major != target_major:
        fail(
            f"PostgreSQL major versions differ: source={source_major}, target={target_major}.",
            3,
        )

    pg_dump = matching_postgres_binary("pg_dump", source_major)
    pg_restore = matching_postgres_binary("pg_restore", source_major)
    if binary_major(pg_dump) < source_major or binary_major(pg_restore) < source_major:
        fail(f"PostgreSQL {source_major} requires matching dump and restore utilities.", 3)

    values = ",".join(f"('{table}')" for table in TABLES)
    target_table_count = run_text(
        [
            psql,
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            (
                "SELECT COUNT(*) FROM (VALUES "
                f"{values}"
                ") AS expected(name) "
                "WHERE to_regclass('public.' || expected.name) IS NOT NULL"
            ),
        ],
        env=target_env,
    )
    if target_table_count != "0":
        fail(
            f"Target already contains {target_table_count} translator tables; refusing to overwrite it.",
            4,
        )

    with tempfile.TemporaryDirectory(prefix="shopify-neon-migration.", dir="/tmp") as temp_dir:
        dump_path = Path(temp_dir) / "shopify-translator.dump"
        dump_command = [
            pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            *(f"--table=public.{table}" for table in TABLES),
            f"--file={dump_path}",
        ]

        print(f"Creating a PostgreSQL {source_major} custom-format dump...")
        subprocess.run(dump_command, env=source_env, check=True)

        print("Restoring into the empty target project...")
        subprocess.run(
            [
                pg_restore,
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--dbname",
                target_env["PGDATABASE"],
                str(dump_path),
            ],
            env=target_env,
            check=True,
        )

        inventory_command = [
            psql,
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-A",
            "-F",
            "\t",
            "-t",
            "-c",
            INVENTORY_SQL,
        ]
        source_inventory = run_text(inventory_command, env=source_env)
        target_inventory = run_text(inventory_command, env=target_env)
        if source_inventory != target_inventory:
            fail("Migration verification failed: source and target inventories differ.", 5)

    target_size = run_text(
        [
            psql,
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            "SELECT pg_size_pretty(pg_database_size(current_database()))",
        ],
        env=target_env,
    )
    print("Migration verified successfully.")
    print(f"PostgreSQL major: {target_major}")
    print(f"Target database size: {target_size}")
    print("Translator table inventory:")
    print(target_inventory)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Iterable

try:
    import psycopg
    from psycopg.types.json import Json
except ImportError:  # pragma: no cover - guided install
    print(
        "Missing psycopg. Install with: uv sync --extra db",
        file=sys.stderr,
    )
    raise


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_PATH = REPO_ROOT / "data" / "main.sqlite"
DEFAULT_MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: list[str]
    transforms: dict[str, Callable[[Any], Any]]


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def to_json(value: Any) -> Json | None:
    if value is None:
        return None
    if isinstance(value, Json):
        return value
    if isinstance(value, (dict, list)):
        return Json(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return Json(value)
        return Json(parsed)
    return Json(value)


def load_sqlite_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(row) for row in rows]


def resolve_sqlite_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    for key in ("DEMI_SQLITE_PATH", "CLAUDIUS_SQLITE_PATH"):
        env_path = os.getenv(key)
        if env_path:
            return Path(env_path)
    candidates = [
        DEFAULT_SQLITE_PATH,
        REPO_ROOT / "data" / "main.sqlite",
        REPO_ROOT / "data" / "claudius.sqlite",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return DEFAULT_SQLITE_PATH


def resolve_postgres_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    for key in (
        "DEMI_MAIN_DB_URL",
        "CLAUDIUS_MAIN_DB_URL",
        "MAIN_DB_URL",
        "DATABASE_URL",
    ):
        value = os.getenv(key)
        if value:
            return value
    raise SystemExit("Postgres URL not set. Use --postgres-url or DEMI_MAIN_DB_URL.")


def split_sql(script: str) -> list[str]:
    statements = [chunk.strip() for chunk in script.split(";")]
    return [stmt for stmt in statements if stmt]


def apply_migrations(conn: psycopg.Connection, migrations_dir: Path) -> list[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS demi_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL
        );
        """
    )
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM demi_migrations").fetchall()
    }
    applied_now: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        sql = path.read_text()
        statements = split_sql(sql)
        if not statements:
            continue
        with conn.transaction():
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO demi_migrations (version, applied_at) VALUES (%s, %s)",
                (version, datetime.utcnow()),
            )
        applied_now.append(version)
    return applied_now


def truncate_tables(conn: psycopg.Connection, tables: Iterable[str]) -> None:
    table_list = ", ".join(tables)
    with conn.transaction():
        conn.execute(f"TRUNCATE TABLE {table_list} RESTART IDENTITY;")


def reset_sequences(conn: psycopg.Connection, tables: Iterable[str]) -> None:
    with conn.transaction():
        for table in tables:
            conn.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence(%s, %s),
                    COALESCE(MAX(id), 1),
                    MAX(id) IS NOT NULL
                )
                FROM {table};
                """,
                (table, "id"),
            )


def insert_rows(
    conn: psycopg.Connection,
    spec: TableSpec,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    columns = spec.columns
    transforms = spec.transforms
    values: list[tuple[Any, ...]] = []
    for row in rows:
        record: list[Any] = []
        for column in columns:
            value = row.get(column)
            transform = transforms.get(column)
            if transform:
                value = transform(value)
            record.append(value)
        values.append(tuple(record))
    placeholders = ", ".join(["%s"] * len(columns))
    cols = ", ".join(columns)
    sql = f"INSERT INTO {spec.name} ({cols}) VALUES ({placeholders})"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(sql, values)
    return len(values)


def build_specs() -> list[TableSpec]:
    return [
        TableSpec(
            name="tenants",
            columns=[
                "id",
                "provider",
                "external_id",
                "key",
                "workspace_path",
                "session_id",
                "vercel_project_id",
                "last_deploy_url",
                "created_at",
                "updated_at",
            ],
            transforms={
                "created_at": parse_dt,
                "updated_at": parse_dt,
            },
        ),
        TableSpec(
            name="messages",
            columns=[
                "id",
                "tenant_id",
                "provider",
                "provider_message_id",
                "received_at",
                "text",
                "raw_json",
                "project_name",
                "status",
            ],
            transforms={
                "received_at": parse_dt,
                "raw_json": to_json,
            },
        ),
        TableSpec(
            name="runs",
            columns=[
                "id",
                "tenant_id",
                "message_id",
                "status",
                "started_at",
                "finished_at",
                "error",
                "total_cost_usd",
                "usage_json",
                "project_name",
                "lease_expires_at",
                "last_heartbeat_at",
                "last_activity_at",
            ],
            transforms={
                "started_at": parse_dt,
                "finished_at": parse_dt,
                "lease_expires_at": parse_dt,
                "last_heartbeat_at": parse_dt,
                "last_activity_at": parse_dt,
                "usage_json": to_json,
            },
        ),
        TableSpec(
            name="billing_orders",
            columns=[
                "id",
                "tenant_id",
                "order_type",
                "status",
                "price_usd",
                "currency",
                "stripe_session_id",
                "stripe_payment_url",
                "stripe_subscription_id",
                "metadata_json",
                "error",
                "created_at",
                "updated_at",
            ],
            transforms={
                "metadata_json": to_json,
                "created_at": parse_dt,
                "updated_at": parse_dt,
            },
        ),
        TableSpec(
            name="supabase_projects",
            columns=[
                "id",
                "tenant_id",
                "project_ref",
                "project_id",
                "project_name",
                "region",
                "status",
                "api_url",
                "publishable_key",
                "secret_key",
                "anon_key",
                "service_role_key",
                "raw_json",
                "created_at",
                "updated_at",
            ],
            transforms={
                "raw_json": to_json,
                "created_at": parse_dt,
                "updated_at": parse_dt,
            },
        ),
        TableSpec(
            name="event_jobs",
            columns=[
                "id",
                "tenant_id",
                "job_type",
                "payload_json",
                "status",
                "attempts",
                "run_after",
                "last_error",
                "created_at",
                "updated_at",
            ],
            transforms={
                "payload_json": to_json,
                "run_after": parse_dt,
                "created_at": parse_dt,
                "updated_at": parse_dt,
            },
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate the main SQLite DB to Postgres/Supabase",
    )
    parser.add_argument("--sqlite-path", help="Path to main.sqlite")
    parser.add_argument("--postgres-url", help="Postgres connection string")
    parser.add_argument(
        "--migrations-dir",
        default=str(DEFAULT_MIGRATIONS_DIR),
        help="Directory containing Postgres SQL migrations",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Skip applying migrations",
    )
    parser.add_argument(
        "--skip-data",
        action="store_true",
        help="Skip migrating data",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="Truncate Postgres tables before importing",
    )
    args = parser.parse_args()

    sqlite_path = resolve_sqlite_path(args.sqlite_path)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite DB not found: {sqlite_path}")
    postgres_url = resolve_postgres_url(args.postgres_url)
    migrations_dir = Path(args.migrations_dir)
    if not migrations_dir.exists() and not args.skip_migrations:
        raise SystemExit(f"Migrations directory not found: {migrations_dir}")

    specs = build_specs()
    tables = [spec.name for spec in specs]

    with sqlite3.connect(sqlite_path) as sqlite_conn:
        with psycopg.connect(postgres_url) as pg_conn:
            if not args.skip_migrations:
                applied = apply_migrations(pg_conn, migrations_dir)
                if applied:
                    print(f"Applied migrations: {', '.join(applied)}")

            if args.skip_data:
                return

            existing = {
                table: pg_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }
            if any(count > 0 for count in existing.values()) and not args.wipe:
                raise SystemExit(
                    "Postgres tables already contain data. Use --wipe to truncate first."
                )
            if args.wipe:
                truncate_tables(pg_conn, tables)

            for spec in specs:
                rows = load_sqlite_rows(sqlite_conn, spec.name)
                inserted = insert_rows(pg_conn, spec, rows)
                print(f"{spec.name}: {inserted} rows")

            reset_sequences(pg_conn, tables)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from claudius.models import NormalizedMessage, Tenant


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init(self) -> None:
        conn = self.connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                external_id TEXT NOT NULL,
                key TEXT NOT NULL UNIQUE,
                workspace_path TEXT,
                session_id TEXT,
                vercel_project_id TEXT,
                last_deploy_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                text TEXT,
                raw_json TEXT,
                status TEXT,
                UNIQUE(tenant_id, provider_message_id)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                total_cost_usd REAL,
                usage_json TEXT
            );

            CREATE TABLE IF NOT EXISTS billing_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                price_usd REAL,
                currency TEXT,
                stripe_session_id TEXT,
                stripe_payment_url TEXT,
                stripe_subscription_id TEXT,
                metadata_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS supabase_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL UNIQUE,
                project_ref TEXT,
                project_id TEXT,
                project_name TEXT,
                region TEXT,
                status TEXT,
                api_url TEXT,
                publishable_key TEXT,
                secret_key TEXT,
                anon_key TEXT,
                service_role_key TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                job_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                run_after TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._migrate_messages_unique_index(conn)
        self._migrate_runs_usage_columns(conn)
        conn.commit()

    def _migrate_messages_unique_index(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if not row:
            return

        if self._has_unique_index(conn, "messages", ["tenant_id", "provider_message_id"]):
            return

        conn.execute(
            """
            CREATE TABLE messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_message_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                text TEXT,
                raw_json TEXT,
                status TEXT,
                UNIQUE(tenant_id, provider_message_id)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO messages_new (id, tenant_id, provider, provider_message_id, received_at, text, raw_json, status)
            SELECT id, tenant_id, provider, provider_message_id, received_at, text, raw_json, status
            FROM messages;
            """
        )
        conn.execute("DROP TABLE messages;")
        conn.execute("ALTER TABLE messages_new RENAME TO messages;")

    def _migrate_runs_usage_columns(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if not row:
            return

        columns = {info["name"] for info in conn.execute("PRAGMA table_info(runs);").fetchall()}
        if "total_cost_usd" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN total_cost_usd REAL;")
        if "usage_json" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN usage_json TEXT;")

    @staticmethod
    def _has_unique_index(
        conn: sqlite3.Connection, table: str, columns: list[str]
    ) -> bool:
        for index in conn.execute(f"PRAGMA index_list({table});").fetchall():
            if not index["unique"]:
                continue
            index_name = index["name"]
            cols = [
                row["name"]
                for row in conn.execute(f"PRAGMA index_info({index_name});").fetchall()
            ]
            if cols == columns:
                return True
        return False

    def get_or_create_tenant(self, provider: str, external_id: str) -> Tenant:
        conn = self.connect()
        key = f"{provider}:{external_id}"
        row = conn.execute("SELECT * FROM tenants WHERE key = ?", (key,)).fetchone()
        if row:
            return self._row_to_tenant(row)

        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO tenants (provider, external_id, key, workspace_path, session_id,
                                 vercel_project_id, last_deploy_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                external_id,
                key,
                None,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tenants WHERE key = ?", (key,)).fetchone()
        return self._row_to_tenant(row)

    def get_tenant_by_id(self, tenant_id: int) -> Tenant | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if not row:
            return None
        return self._row_to_tenant(row)

    def get_tenant_by_key(self, key: str) -> Tenant | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM tenants WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return self._row_to_tenant(row)

    def get_tenant_by_external(self, provider: str, external_id: str) -> Tenant | None:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM tenants WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        ).fetchone()
        if not row:
            return None
        return self._row_to_tenant(row)

    def update_tenant_workspace(self, tenant_id: int, workspace_path: str) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE tenants SET workspace_path = ?, updated_at = ? WHERE id = ?",
            (workspace_path, now, tenant_id),
        )
        conn.commit()

    def update_tenant_session(self, tenant_id: int, session_id: str) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE tenants SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, now, tenant_id),
        )
        conn.commit()

    def update_tenant_deploy_url(self, tenant_id: int, url: str) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE tenants SET last_deploy_url = ?, updated_at = ? WHERE id = ?",
            (url, now, tenant_id),
        )
        conn.commit()

    def create_event_job(
        self,
        tenant_id: int,
        job_type: str,
        payload: dict[str, Any],
        run_after: str | None = None,
    ) -> int:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        if run_after is None:
            run_after = now
        cur = conn.execute(
            """
            INSERT INTO event_jobs (tenant_id, job_type, payload_json, status, attempts, run_after, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                job_type,
                json.dumps(payload),
                "pending",
                0,
                run_after,
                None,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def fetch_pending_event_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        rows = conn.execute(
            """
            SELECT * FROM event_jobs
            WHERE status = 'pending' AND run_after <= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_event_job_running(self, job_id: int) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE event_jobs SET status = ?, updated_at = ? WHERE id = ?",
            ("running", now, job_id),
        )
        conn.commit()

    def mark_event_job_done(self, job_id: int) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE event_jobs SET status = ?, updated_at = ? WHERE id = ?",
            ("completed", now, job_id),
        )
        conn.commit()

    def mark_event_job_failed(
        self,
        job_id: int,
        error: str,
        retry_after_seconds: int | None = None,
        max_attempts: int = 5,
    ) -> None:
        conn = self.connect()
        row = conn.execute(
            "SELECT attempts FROM event_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        attempts = int(row["attempts"]) + 1 if row else 1
        now = datetime.now(tz=timezone.utc)
        status = "failed" if attempts >= max_attempts else "pending"
        run_after = now
        if retry_after_seconds and status == "pending":
            run_after = now + timedelta(seconds=retry_after_seconds)
        conn.execute(
            """
            UPDATE event_jobs
            SET status = ?, attempts = ?, run_after = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                attempts,
                run_after.isoformat(),
                error,
                now.isoformat(),
                job_id,
            ),
        )
        conn.commit()

    def record_message(self, tenant_id: int, msg: NormalizedMessage) -> tuple[int, bool]:
        conn = self.connect()
        raw_json = json.dumps(msg.raw)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO messages (tenant_id, provider, provider_message_id, received_at, text, raw_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                msg.provider,
                msg.provider_message_id,
                msg.received_at.isoformat(),
                msg.text,
                raw_json,
                "received",
            ),
        )
        conn.commit()
        if cur.rowcount == 1:
            return int(cur.lastrowid), True
        row = conn.execute(
            """
            SELECT id FROM messages
            WHERE tenant_id = ? AND provider_message_id = ?
            """,
            (tenant_id, msg.provider_message_id),
        ).fetchone()
        return (int(row["id"]) if row else 0), False

    def update_message_status(self, message_id: int, status: str) -> None:
        conn = self.connect()
        conn.execute(
            "UPDATE messages SET status = ? WHERE id = ?",
            (status, message_id),
        )
        conn.commit()

    def get_next_pending_message(self, tenant_id: int) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(
            """
            SELECT * FROM messages
            WHERE tenant_id = ? AND status = 'pending'
            ORDER BY received_at ASC
            LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()

    def get_pending_messages(self, tenant_id: int) -> list[sqlite3.Row]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE tenant_id = ? AND status = 'pending'
            ORDER BY received_at ASC
            """,
            (tenant_id,),
        ).fetchall()
        return list(rows or [])

    def update_message_statuses(self, message_ids: list[int], status: str) -> None:
        if not message_ids:
            return
        conn = self.connect()
        placeholders = ",".join("?" for _ in message_ids)
        conn.execute(
            f"UPDATE messages SET status = ? WHERE id IN ({placeholders})",
            (status, *message_ids),
        )
        conn.commit()

    def has_inflight_run(self, tenant_id: int) -> bool:
        conn = self.connect()
        row = conn.execute(
            "SELECT 1 FROM runs WHERE tenant_id = ? AND status = 'running' LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return row is not None

    def get_inflight_run(self, tenant_id: int) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM runs WHERE tenant_id = ? AND status = 'running' LIMIT 1",
            (tenant_id,),
        ).fetchone()

    def create_run(self, tenant_id: int, message_id: int | None = None) -> int:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO runs (tenant_id, message_id, status, started_at) VALUES (?, ?, 'running', ?)",
            (tenant_id, message_id or 0, now),
        )
        conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str = "completed", error: str | None = None) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (status, now, error, run_id),
        )
        conn.commit()

    def update_run_usage(
        self,
        run_id: int,
        total_cost_usd: float | None = None,
        usage: dict | None = None,
    ) -> None:
        if total_cost_usd is None and not usage:
            return
        conn = self.connect()
        usage_json = json.dumps(usage) if usage is not None else None
        conn.execute(
            "UPDATE runs SET total_cost_usd = ?, usage_json = ? WHERE id = ?",
            (total_cost_usd, usage_json, run_id),
        )
        conn.commit()

    def create_billing_order(
        self,
        tenant_id: int,
        order_type: str,
        status: str,
        price_usd: float | None = None,
        currency: str | None = None,
        metadata: dict | None = None,
        error: str | None = None,
    ) -> int:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = conn.execute(
            """
            INSERT INTO billing_orders (
                tenant_id, order_type, status, price_usd, currency, metadata_json, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                order_type,
                status,
                price_usd,
                currency,
                json.dumps(metadata) if metadata else None,
                error,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def update_billing_order_payment(
        self,
        order_id: int,
        stripe_session_id: str | None,
        stripe_payment_url: str | None,
        status: str = "pending_payment",
    ) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE billing_orders
            SET status = ?, stripe_session_id = ?, stripe_payment_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, stripe_session_id, stripe_payment_url, now, order_id),
        )
        conn.commit()

    def mark_billing_order_paid(
        self,
        order_id: int,
        stripe_session_id: str | None = None,
        stripe_subscription_id: str | None = None,
    ) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE billing_orders
            SET status = ?, stripe_session_id = ?, stripe_subscription_id = ?, updated_at = ?
            WHERE id = ?
            """,
            ("paid", stripe_session_id, stripe_subscription_id, now, order_id),
        )
        conn.commit()

    def mark_billing_order_failed(self, order_id: int, error: str) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE billing_orders
            SET status = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            ("failed", error, now, order_id),
        )
        conn.commit()

    def update_billing_order_status(
        self,
        order_id: int,
        status: str,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        metadata_json = None
        if metadata:
            row = conn.execute(
                "SELECT metadata_json FROM billing_orders WHERE id = ?", (order_id,)
            ).fetchone()
            current = {}
            if row and row["metadata_json"]:
                try:
                    current = json.loads(row["metadata_json"])
                except (TypeError, ValueError):
                    current = {}
            current.update(metadata)
            metadata_json = json.dumps(current)

        if metadata_json is None:
            conn.execute(
                """
                UPDATE billing_orders
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, order_id),
            )
        else:
            conn.execute(
                """
                UPDATE billing_orders
                SET status = ?, error = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, metadata_json, now, order_id),
            )
        conn.commit()

    def get_billing_order(self, order_id: int) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM billing_orders WHERE id = ?", (order_id,)
        ).fetchone()

    def get_billing_order_by_subscription(self, subscription_id: str) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM billing_orders WHERE stripe_subscription_id = ?",
            (subscription_id,),
        ).fetchone()

    def get_billing_order_by_session(self, session_id: str) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM billing_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()

    def upsert_supabase_project(
        self,
        tenant_id: int,
        project_ref: str | None,
        project_id: str | None,
        project_name: str | None,
        region: str | None,
        status: str | None,
        api_url: str | None,
        publishable_key: str | None,
        secret_key: str | None,
        anon_key: str | None,
        service_role_key: str | None,
        raw: dict | None = None,
    ) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO supabase_projects (
                tenant_id, project_ref, project_id, project_name, region, status, api_url,
                publishable_key, secret_key, anon_key, service_role_key, raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                project_ref = excluded.project_ref,
                project_id = excluded.project_id,
                project_name = excluded.project_name,
                region = excluded.region,
                status = excluded.status,
                api_url = excluded.api_url,
                publishable_key = excluded.publishable_key,
                secret_key = excluded.secret_key,
                anon_key = excluded.anon_key,
                service_role_key = excluded.service_role_key,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (
                tenant_id,
                project_ref,
                project_id,
                project_name,
                region,
                status,
                api_url,
                publishable_key,
                secret_key,
                anon_key,
                service_role_key,
                json.dumps(raw) if raw else None,
                now,
                now,
            ),
        )
        conn.commit()

    def get_supabase_project(self, tenant_id: int) -> sqlite3.Row | None:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM supabase_projects WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()

    def _row_to_tenant(self, row: sqlite3.Row) -> Tenant:
        return Tenant(
            id=row["id"],
            provider=row["provider"],
            external_id=row["external_id"],
            key=row["key"],
            workspace_path=row["workspace_path"],
            session_id=row["session_id"],
            vercel_project_id=row["vercel_project_id"],
            last_deploy_url=row["last_deploy_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

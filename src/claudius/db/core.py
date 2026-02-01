from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from claudius.models import NormalizedMessage, Tenant
from claudius.db.sqlite_utils import configure_sqlite_connection
from claudius.config import Settings


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            settings = Settings()
            conn = sqlite3.connect(
                self.db_path,
                timeout=settings.sqlite_timeout_seconds,
            )
            conn.row_factory = sqlite3.Row
            configure_sqlite_connection(conn)
            self._local.conn = conn
        return conn

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
                project_name TEXT,
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
                usage_json TEXT,
                project_name TEXT,
                lease_expires_at TEXT,
                last_heartbeat_at TEXT,
                last_activity_at TEXT
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

            CREATE TABLE IF NOT EXISTS run_inputs (
                id TEXT PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                run_id INTEGER,
                project_name TEXT,
                source TEXT NOT NULL,
                provider_message_id TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_at TEXT,
                handled_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                run_id INTEGER,
                project_name TEXT,
                correlation_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS active_runs (
                tenant_id INTEGER NOT NULL,
                project_name TEXT NOT NULL,
                run_id INTEGER NOT NULL,
                lease_expires_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, project_name)
            );
            """
        )
        self._migrate_messages_unique_index(conn)
        self._migrate_runs_usage_columns(conn)
        self._migrate_messages_project_column(conn)
        self._migrate_runs_lease_columns(conn)
        self._migrate_run_input_indexes(conn)
        conn.commit()

    def _migrate_messages_unique_index(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if not row:
            return

        if self._has_unique_index(conn, "messages", ["tenant_id", "provider_message_id"]):
            return

        columns = {info["name"] for info in conn.execute("PRAGMA table_info(messages);").fetchall()}
        has_project = "project_name" in columns

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
                project_name TEXT,
                status TEXT,
                UNIQUE(tenant_id, provider_message_id)
            );
            """
        )
        if has_project:
            conn.execute(
                """
                INSERT INTO messages_new (
                    id, tenant_id, provider, provider_message_id, received_at, text, raw_json,
                    project_name, status
                )
                SELECT id, tenant_id, provider, provider_message_id, received_at, text, raw_json,
                       project_name, status
                FROM messages;
                """
            )
        else:
            conn.execute(
                """
                INSERT INTO messages_new (
                    id, tenant_id, provider, provider_message_id, received_at, text, raw_json,
                    project_name, status
                )
                SELECT id, tenant_id, provider, provider_message_id, received_at, text, raw_json,
                       NULL, status
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

    def _migrate_runs_lease_columns(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if not row:
            return
        columns = {info["name"] for info in conn.execute("PRAGMA table_info(runs);").fetchall()}
        if "project_name" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN project_name TEXT;")
        if "lease_expires_at" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN lease_expires_at TEXT;")
        if "last_heartbeat_at" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN last_heartbeat_at TEXT;")
        if "last_activity_at" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN last_activity_at TEXT;")

    def _migrate_messages_project_column(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if not row:
            return
        columns = {info["name"] for info in conn.execute("PRAGMA table_info(messages);").fetchall()}
        if "project_name" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN project_name TEXT;")

    def _migrate_run_input_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS run_inputs_dedupe_idx
                ON run_inputs (tenant_id, provider_message_id)
                WHERE provider_message_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS run_inputs_queue_idx
                ON run_inputs (tenant_id, project_name, status, created_at)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS outbox_dedupe_idx
                ON outbox (tenant_id, correlation_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS active_runs_run_idx
                ON active_runs (run_id)
            """
        )

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

    def list_tenants(self) -> list[Tenant]:
        conn = self.connect()
        rows = conn.execute("SELECT * FROM tenants").fetchall()
        return [self._row_to_tenant(row) for row in rows or []]

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
            INSERT INTO event_jobs (
                tenant_id, job_type, payload_json, status, attempts, run_after,
                last_error, created_at, updated_at
            )
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
            INSERT OR IGNORE INTO messages (
                tenant_id, provider, provider_message_id, received_at, text, raw_json,
                status, project_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                msg.provider,
                msg.provider_message_id,
                msg.received_at.isoformat(),
                msg.text,
                raw_json,
                "received",
                msg.project_name,
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

    def update_message_project(self, message_id: int, project_name: str | None) -> None:
        conn = self.connect()
        conn.execute(
            "UPDATE messages SET project_name = ? WHERE id = ?",
            (project_name, message_id),
        )
        conn.commit()

    def get_next_pending_message(
        self, tenant_id: int, project_name: str | None = None
    ) -> sqlite3.Row | None:
        conn = self.connect()
        if project_name:
            return conn.execute(
                """
                SELECT * FROM messages
                WHERE tenant_id = ? AND status = 'pending' AND project_name = ?
                ORDER BY received_at ASC
                LIMIT 1
                """,
                (tenant_id, project_name),
            ).fetchone()
        return conn.execute(
            """
            SELECT * FROM messages
            WHERE tenant_id = ? AND status = 'pending'
            ORDER BY received_at ASC
            LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()

    def get_pending_messages(
        self, tenant_id: int, project_name: str | None = None
    ) -> list[sqlite3.Row]:
        conn = self.connect()
        if project_name:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE tenant_id = ? AND status = 'pending' AND project_name = ?
                ORDER BY received_at ASC
                """,
                (tenant_id, project_name),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE tenant_id = ? AND status = 'pending'
                ORDER BY received_at ASC
                """,
                (tenant_id,),
            ).fetchall()
        return list(rows or [])

    def fetch_pending_message_groups(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT tenant_id, project_name, MIN(received_at) AS oldest_received_at
            FROM messages
            WHERE status = 'pending'
            GROUP BY tenant_id, project_name
            ORDER BY oldest_received_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def fetch_processing_message_groups(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT tenant_id, project_name, MIN(received_at) AS oldest_received_at
            FROM messages
            WHERE status = 'processing'
            GROUP BY tenant_id, project_name
            ORDER BY oldest_received_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def requeue_processing_messages(self, tenant_id: int, project_name: str | None) -> int:
        conn = self.connect()
        if project_name:
            cur = conn.execute(
                """
                UPDATE messages
                SET status = 'pending'
                WHERE tenant_id = ? AND project_name = ? AND status = 'processing'
                """,
                (tenant_id, project_name),
            )
        else:
            cur = conn.execute(
                """
                UPDATE messages
                SET status = 'pending'
                WHERE tenant_id = ? AND status = 'processing'
                """,
                (tenant_id,),
            )
        conn.commit()
        return cur.rowcount

    def clear_pending_and_processing_messages(
        self, tenant_id: int, project_name: str | None
    ) -> int:
        conn = self.connect()
        if project_name:
            cur = conn.execute(
                """
                UPDATE messages
                SET status = 'processed'
                WHERE tenant_id = ? AND project_name = ? AND status IN ('pending', 'processing')
                """,
                (tenant_id, project_name),
            )
        else:
            cur = conn.execute(
                """
                UPDATE messages
                SET status = 'processed'
                WHERE tenant_id = ? AND status IN ('pending', 'processing')
                """,
                (tenant_id,),
            )
        conn.commit()
        return cur.rowcount

    def finish_running_runs(
        self, tenant_id: int, project_name: str | None, error: str
    ) -> int:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        if project_name:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?, error = ?
                WHERE tenant_id = ? AND project_name = ? AND status = 'running'
                """,
                (now, error, tenant_id, project_name),
            )
        else:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?, error = ?
                WHERE tenant_id = ? AND status = 'running'
                """,
                (now, error, tenant_id),
            )
        conn.commit()
        return cur.rowcount

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

    def fetch_messages_by_statuses(
        self,
        tenant_id: int,
        statuses: list[str],
        project_name: str | None = None,
    ) -> list[sqlite3.Row]:
        if not statuses:
            return []
        conn = self.connect()
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = [tenant_id, *statuses]
        project_clause = ""
        if project_name:
            project_clause = " AND project_name = ?"
            params.append(project_name)
        rows = conn.execute(
            f"""
            SELECT * FROM messages
            WHERE tenant_id = ? AND status IN ({placeholders}){project_clause}
            ORDER BY received_at ASC
            """,
            tuple(params),
        ).fetchall()
        return list(rows or [])

    def create_run_input(
        self,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        source: str,
        provider_message_id: str | None,
        payload: dict[str, Any],
        status: str = "queued",
    ) -> str:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        run_input_id = str(uuid.uuid4())
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO run_inputs (
                id, tenant_id, run_id, project_name, source, provider_message_id,
                payload_json, status, claimed_at, handled_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_input_id,
                tenant_id,
                run_id,
                project_name,
                source,
                provider_message_id,
                json.dumps(payload),
                status,
                None,
                None,
                now,
            ),
        )
        conn.commit()
        if cur.rowcount == 1:
            return run_input_id
        row = conn.execute(
            """
            SELECT id FROM run_inputs
            WHERE tenant_id = ? AND provider_message_id = ?
            """,
            (tenant_id, provider_message_id),
        ).fetchone()
        return str(row["id"]) if row else run_input_id

    def claim_run_inputs_for_project(
        self, tenant_id: int, project_name: str | None, limit: int = 10
    ) -> list[dict[str, Any]]:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        where_project = ""
        params: list[Any] = [tenant_id]
        if project_name:
            where_project = " AND project_name = ?"
            params.append(project_name)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT * FROM run_inputs
            WHERE tenant_id = ? AND status = 'queued'{where_project}
            ORDER BY created_at ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE run_inputs
            SET status = 'claimed', claimed_at = ?
            WHERE id IN ({placeholders})
            """,
            (now, *ids),
        )
        conn.commit()
        return [dict(row) for row in rows]

    def update_run_inputs_statuses(self, ids: list[str], status: str) -> None:
        if not ids:
            return
        conn = self.connect()
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(tz=timezone.utc).isoformat()
        fields = "status = ?"
        params: list[Any] = [status]
        if status == "handled":
            fields = "status = ?, handled_at = ?"
            params.append(now)
        elif status == "queued":
            fields = "status = ?, claimed_at = NULL"
        params.extend(ids)
        conn.execute(
            f"UPDATE run_inputs SET {fields} WHERE id IN ({placeholders})",
            tuple(params),
        )
        conn.commit()

    def cancel_run_inputs(self, tenant_id: int, project_name: str | None) -> int:
        conn = self.connect()
        if project_name:
            cur = conn.execute(
                """
                UPDATE run_inputs
                SET status = 'cancelled'
                WHERE tenant_id = ? AND project_name = ? AND status IN ('queued', 'claimed')
                """,
                (tenant_id, project_name),
            )
        else:
            cur = conn.execute(
                """
                UPDATE run_inputs
                SET status = 'cancelled'
                WHERE tenant_id = ? AND status IN ('queued', 'claimed')
                """,
                (tenant_id,),
            )
        conn.commit()
        return cur.rowcount

    def fetch_run_inputs(
        self,
        tenant_id: int,
        project_name: str | None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        conn = self.connect()
        params: list[Any] = [tenant_id]
        where_project = ""
        where_status = ""
        if project_name:
            where_project = " AND project_name = ?"
            params.append(project_name)
        if status:
            where_status = " AND status = ?"
            params.append(status)
        limit_clause = ""
        if limit:
            limit_clause = " LIMIT ?"
            params.append(limit)
        rows = conn.execute(
            f"""
            SELECT * FROM run_inputs
            WHERE tenant_id = ?{where_project}{where_status}
            ORDER BY created_at ASC{limit_clause}
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows or []]

    def fetch_queued_run_input_groups(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT tenant_id, project_name, MIN(created_at) AS oldest_created_at
            FROM run_inputs
            WHERE status = 'queued'
            GROUP BY tenant_id, project_name
            ORDER BY oldest_created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows or []]

    def enqueue_outbox(
        self,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> str:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        outbox_id = str(uuid.uuid4())
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO outbox (
                id, tenant_id, run_id, project_name, correlation_id,
                payload_json, status, created_at, sent_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                tenant_id,
                run_id,
                project_name,
                correlation_id,
                json.dumps(payload),
                "queued",
                now,
                None,
            ),
        )
        conn.commit()
        if cur.rowcount == 1:
            return outbox_id
        row = conn.execute(
            """
            SELECT id FROM outbox
            WHERE tenant_id = ? AND correlation_id = ?
            """,
            (tenant_id, correlation_id),
        ).fetchone()
        return str(row["id"]) if row else outbox_id

    def claim_outbox(self, limit: int = 25) -> list[dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM outbox
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE outbox SET status = 'sending' WHERE id IN ({placeholders})",
            tuple(ids),
        )
        conn.commit()
        return [dict(row) for row in rows]

    def update_outbox_statuses(self, ids: list[str], status: str) -> None:
        if not ids:
            return
        conn = self.connect()
        placeholders = ",".join("?" for _ in ids)
        now = datetime.now(tz=timezone.utc).isoformat()
        fields = "status = ?"
        params: list[Any] = [status]
        if status == "sent":
            fields = "status = ?, sent_at = ?"
            params.append(now)
        params.extend(ids)
        conn.execute(
            f"UPDATE outbox SET {fields} WHERE id IN ({placeholders})",
            tuple(params),
        )
        conn.commit()

    def set_active_run(
        self,
        tenant_id: int,
        project_name: str,
        run_id: int,
        lease_expires_at: str | None,
    ) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO active_runs (
                tenant_id, project_name, run_id, lease_expires_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, project_name)
            DO UPDATE SET run_id = excluded.run_id,
                          lease_expires_at = excluded.lease_expires_at,
                          updated_at = excluded.updated_at
            """,
            (tenant_id, project_name, run_id, lease_expires_at, now),
        )
        conn.commit()

    def get_active_run(
        self, tenant_id: int, project_name: str | None
    ) -> sqlite3.Row | None:
        conn = self.connect()
        if project_name:
            row = conn.execute(
                """
                SELECT * FROM active_runs
                WHERE tenant_id = ? AND project_name = ?
                LIMIT 1
                """,
                (tenant_id, project_name),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM active_runs
                WHERE tenant_id = ?
                LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
        return row

    def clear_active_run(self, tenant_id: int, project_name: str | None) -> None:
        conn = self.connect()
        if project_name:
            conn.execute(
                "DELETE FROM active_runs WHERE tenant_id = ? AND project_name = ?",
                (tenant_id, project_name),
            )
        else:
            conn.execute("DELETE FROM active_runs WHERE tenant_id = ?", (tenant_id,))
        conn.commit()

    def has_inflight_run(self, tenant_id: int, project_name: str | None = None) -> bool:
        conn = self.connect()
        if project_name:
            row = conn.execute(
                """
                SELECT 1 FROM runs
                WHERE tenant_id = ? AND project_name = ? AND status = 'running'
                LIMIT 1
                """,
                (tenant_id, project_name),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE tenant_id = ? AND status = 'running' LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return row is not None

    def get_inflight_run(
        self, tenant_id: int, project_name: str | None = None
    ) -> sqlite3.Row | None:
        conn = self.connect()
        if project_name:
            return conn.execute(
                """
                SELECT * FROM runs
                WHERE tenant_id = ? AND project_name = ? AND status = 'running'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (tenant_id, project_name),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM runs WHERE tenant_id = ? AND status = 'running' LIMIT 1",
            (tenant_id,),
        ).fetchone()

    def create_run(
        self,
        tenant_id: int,
        message_id: int | None = None,
        project_name: str | None = None,
        lease_seconds: int | None = None,
    ) -> int:
        conn = self.connect()
        now_dt = datetime.now(tz=timezone.utc)
        now = now_dt.isoformat()
        lease_expires = None
        if lease_seconds is not None:
            lease_expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        cur = conn.execute(
            """
            INSERT INTO runs (
                tenant_id, message_id, status, started_at, project_name,
                lease_expires_at, last_heartbeat_at, last_activity_at
            )
            VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                message_id or 0,
                now,
                project_name,
                lease_expires,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def update_run_lease(
        self,
        run_id: int,
        lease_expires_at: str | None,
        last_activity_at: str | None = None,
        last_heartbeat_at: str | None = None,
    ) -> None:
        conn = self.connect()
        fields = ["lease_expires_at = ?"]
        params: list[Any] = [lease_expires_at]
        if last_activity_at is not None:
            fields.append("last_activity_at = ?")
            params.append(last_activity_at)
        if last_heartbeat_at is not None:
            fields.append("last_heartbeat_at = ?")
            params.append(last_heartbeat_at)
        params.append(run_id)
        conn.execute(
            f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.commit()

    def expire_stale_runs(
        self, tenant_id: int, project_name: str | None, now: datetime
    ) -> int:
        conn = self.connect()
        now_str = now.isoformat()
        if project_name:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?, error = ?
                WHERE tenant_id = ? AND project_name = ? AND status = 'running'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (now_str, "lease_expired", tenant_id, project_name, now_str),
            )
        else:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?, error = ?
                WHERE tenant_id = ? AND status = 'running'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (now_str, "lease_expired", tenant_id, now_str),
            )
        conn.commit()
        return cur.rowcount

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
                tenant_id, order_type, status, price_usd, currency, metadata_json,
                error, created_at, updated_at
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
                raw = row["metadata_json"]
                if isinstance(raw, dict):
                    current = raw
                else:
                    try:
                        current = json.loads(raw)
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
                publishable_key, secret_key, anon_key, service_role_key, raw_json,
                created_at, updated_at
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

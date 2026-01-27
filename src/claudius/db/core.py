from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from claudius.models import DomainOrder, NormalizedMessage, Tenant


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

            CREATE TABLE IF NOT EXISTS domain_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                status TEXT NOT NULL,
                price_usd REAL,
                currency TEXT,
                quote_json TEXT,
                stripe_session_id TEXT,
                stripe_payment_url TEXT,
                vercel_response_json TEXT,
                error TEXT,
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

    def create_domain_order(
        self,
        tenant_id: int,
        domain: str,
        status: str,
        price_usd: float | None = None,
        currency: str | None = None,
        quote_json: dict | None = None,
        error: str | None = None,
    ) -> int:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = conn.execute(
            """
            INSERT INTO domain_orders (
                tenant_id, domain, status, price_usd, currency, quote_json, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                domain,
                status,
                price_usd,
                currency,
                json.dumps(quote_json) if quote_json else None,
                error,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def update_domain_order_payment(
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
            UPDATE domain_orders
            SET status = ?, stripe_session_id = ?, stripe_payment_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, stripe_session_id, stripe_payment_url, now, order_id),
        )
        conn.commit()

    def mark_domain_order_paid(self, order_id: int, stripe_session_id: str | None = None) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE domain_orders
            SET status = ?, stripe_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            ("paid", stripe_session_id, now, order_id),
        )
        conn.commit()

    def mark_domain_order_purchased(
        self, order_id: int, vercel_response: dict | None = None
    ) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE domain_orders
            SET status = ?, vercel_response_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "purchased",
                json.dumps(vercel_response) if vercel_response else None,
                now,
                order_id,
            ),
        )
        conn.commit()

    def mark_domain_order_failed(self, order_id: int, error: str) -> None:
        conn = self.connect()
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE domain_orders
            SET status = ?, error = ?, updated_at = ?
            WHERE id = ?
            """,
            ("failed", error, now, order_id),
        )
        conn.commit()

    def get_domain_order(self, order_id: int) -> DomainOrder | None:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM domain_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not row:
            return None
        return DomainOrder(
            id=row["id"],
            tenant_id=row["tenant_id"],
            domain=row["domain"],
            status=row["status"],
            price_usd=row["price_usd"],
            currency=row["currency"],
            quote_json=row["quote_json"],
            stripe_session_id=row["stripe_session_id"],
            stripe_payment_url=row["stripe_payment_url"],
            vercel_response_json=row["vercel_response_json"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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

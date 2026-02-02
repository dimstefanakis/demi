from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from demi.config import Settings
from demi.db.sqlite_utils import configure_sqlite_connection

class TenantDatabase:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._local = threading.local()

    def _should_recover(self, exc: sqlite3.Error) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "disk i/o error",
                "database disk image is malformed",
                "file is not a database",
                "not a database",
                "malformed",
            )
        )

    def _reset_db(self, reason: str) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

        if self.db_path.exists():
            stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.db_path.with_suffix(f"{self.db_path.suffix}.corrupt-{stamp}")
            try:
                self.db_path.rename(backup)
            except OSError:
                pass

        for suffix in ("-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

        try:
            self.init()
        except sqlite3.Error:
            pass

    def _execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        fetchone: bool = False,
        fetchall: bool = False,
        commit: bool = False,
    ):
        conn = self.connect()
        try:
            cur = conn.execute(sql, params)
            if commit:
                conn.commit()
            if fetchone:
                return cur.fetchone()
            if fetchall:
                return cur.fetchall()
            return cur
        except sqlite3.OperationalError as exc:
            if not self._should_recover(exc):
                raise
            self._reset_db(str(exc))
            conn = self.connect()
            cur = conn.execute(sql, params)
            if commit:
                conn.commit()
            if fetchone:
                return cur.fetchone()
            if fetchall:
                return cur.fetchall()
            return cur

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
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            );
            """
        )
        conn.commit()

    def record_event(self, event_type: str, payload: dict[str, Any]) -> int:
        received_at = datetime.now(tz=timezone.utc).isoformat()
        payload_json = json.dumps(payload)
        cur = self._execute(
            """
            INSERT INTO events (event_type, payload_json, received_at)
            VALUES (?, ?, ?)
            """,
            (event_type, payload_json, received_at),
            commit=True,
        )
        return int(cur.lastrowid)

    def set_kv(self, namespace: str, key: str, value: dict[str, Any] | None) -> None:
        updated_at = datetime.now(tz=timezone.utc).isoformat()
        value_json = json.dumps(value) if value is not None else None
        self._execute(
            """
            INSERT INTO kv_store (namespace, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (namespace, key, value_json, updated_at),
            commit=True,
        )

    def get_kv(self, namespace: str, key: str) -> dict[str, Any] | None:
        row = self._execute(
            "SELECT value_json FROM kv_store WHERE namespace = ? AND key = ?",
            (namespace, key),
            fetchone=True,
        )
        if not row or not row["value_json"]:
            return None
        try:
            payload = json.loads(row["value_json"])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None


def ensure_tenant_db(path: Path) -> TenantDatabase:
    db = TenantDatabase(path)
    db.init()
    return db

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TenantDatabase:
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
        conn = self.connect()
        received_at = datetime.now(tz=timezone.utc).isoformat()
        payload_json = json.dumps(payload)
        cur = conn.execute(
            """
            INSERT INTO events (event_type, payload_json, received_at)
            VALUES (?, ?, ?)
            """,
            (event_type, payload_json, received_at),
        )
        conn.commit()
        return int(cur.lastrowid)

    def set_kv(self, namespace: str, key: str, value: dict[str, Any] | None) -> None:
        conn = self.connect()
        updated_at = datetime.now(tz=timezone.utc).isoformat()
        value_json = json.dumps(value) if value is not None else None
        conn.execute(
            """
            INSERT INTO kv_store (namespace, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (namespace, key, value_json, updated_at),
        )
        conn.commit()

    def get_kv(self, namespace: str, key: str) -> dict[str, Any] | None:
        conn = self.connect()
        row = conn.execute(
            "SELECT value_json FROM kv_store WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
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


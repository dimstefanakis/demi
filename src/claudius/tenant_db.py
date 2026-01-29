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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL
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


def ensure_tenant_db(path: Path) -> TenantDatabase:
    db = TenantDatabase(path)
    db.init()
    return db

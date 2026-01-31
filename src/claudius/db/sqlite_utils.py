from __future__ import annotations

import sqlite3

from claudius.config import Settings


def configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    settings = Settings()
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA journal_mode={settings.sqlite_journal_mode}")
        conn.execute(f"PRAGMA synchronous={settings.sqlite_synchronous}")
        conn.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
    except sqlite3.DatabaseError:
        return

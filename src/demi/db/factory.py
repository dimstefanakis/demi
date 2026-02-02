from __future__ import annotations

from demi.config import Settings
from demi.db.core import Database
from demi.db.supabase_db import SupabaseDatabase


def build_database(settings: Settings):
    backend = (settings.main_db_backend or "sqlite").strip().lower()
    if backend == "supabase":
        if not settings.main_db_supabase_url or not settings.main_db_supabase_service_key:
            raise RuntimeError(
                "main_db_supabase_url and main_db_supabase_service_key are required for supabase"
            )
        return SupabaseDatabase(
            url=settings.main_db_supabase_url,
            service_key=settings.main_db_supabase_service_key,
        )
    return Database(settings.resolved_db_path())

from __future__ import annotations

from demi.config import Settings
from demi.db.supabase_db import SupabaseDatabase


def build_database(settings: Settings):
    if not settings.main_db_supabase_url or not settings.main_db_supabase_service_key:
        raise RuntimeError(
            "main_db_supabase_url and main_db_supabase_service_key are required for supabase"
        )
    return SupabaseDatabase(
        url=settings.main_db_supabase_url,
        service_key=settings.main_db_supabase_service_key,
    )

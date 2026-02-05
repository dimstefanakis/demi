from __future__ import annotations

"""Supabase-only database entrypoint.

Historically this module provided a SQLite-backed Database class. The system now
runs exclusively on Supabase, so we re-export the SupabaseDatabase here to
preserve imports without keeping any SQLite code.
"""

from demi.db.supabase_db import SupabaseDatabase

Database = SupabaseDatabase

__all__ = ["Database", "SupabaseDatabase"]

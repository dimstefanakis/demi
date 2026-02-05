from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from demi.db.supabase_db import SupabaseDatabase


def get_block(db: SupabaseDatabase, tenant_id: int, scope: str) -> dict[str, Any] | None:
    payload = db.get_tenant_kv(tenant_id, "system", f"{scope}_block")
    return payload if isinstance(payload, dict) else None


def clear_block(db: SupabaseDatabase, tenant_id: int, scope: str) -> None:
    db.set_tenant_kv(tenant_id, "system", f"{scope}_block", None)
    db.set_tenant_kv(tenant_id, "system", f"{scope}_failures", None)


def record_hard_failure(
    db: SupabaseDatabase,
    tenant_id: int,
    scope: str,
    *,
    reason: str,
    message: str | None = None,
    max_failures: int = 2,
) -> dict[str, Any]:
    current = db.get_tenant_kv(tenant_id, "system", f"{scope}_failures") or {}
    try:
        count = int(current.get("count") or 0) + 1
    except (TypeError, ValueError):
        count = 1
    now = datetime.now(tz=timezone.utc).isoformat()
    failure_payload = {
        "count": count,
        "reason": reason,
        "message": message,
        "at": now,
    }
    db.set_tenant_kv(tenant_id, "system", f"{scope}_failures", failure_payload)

    if count >= max_failures:
        block_payload = {
            "count": count,
            "reason": reason,
            "message": message,
            "at": now,
        }
        db.set_tenant_kv(tenant_id, "system", f"{scope}_block", block_payload)
        return {"count": count, "blocked": True, "block": block_payload}

    return {"count": count, "blocked": False}

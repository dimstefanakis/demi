from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claudius.tenant_db import ensure_tenant_db


def get_block(tasks_dir: Path, scope: str) -> dict[str, Any] | None:
    db = _tenant_db(tasks_dir)
    payload = db.get_kv("system", f"{scope}_block")
    return payload if isinstance(payload, dict) else None


def clear_block(tasks_dir: Path, scope: str) -> None:
    db = _tenant_db(tasks_dir)
    db.set_kv("system", f"{scope}_block", None)
    db.set_kv("system", f"{scope}_failures", None)


def record_hard_failure(
    tasks_dir: Path,
    scope: str,
    *,
    reason: str,
    message: str | None = None,
    max_failures: int = 2,
) -> dict[str, Any]:
    db = _tenant_db(tasks_dir)
    current = db.get_kv("system", f"{scope}_failures") or {}
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
    db.set_kv("system", f"{scope}_failures", failure_payload)

    if count >= max_failures:
        block_payload = {
            "count": count,
            "reason": reason,
            "message": message,
            "at": now,
        }
        db.set_kv("system", f"{scope}_block", block_payload)
        return {"count": count, "blocked": True, "block": block_payload}

    return {"count": count, "blocked": False}


def _tenant_db(tasks_dir: Path):
    return ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")

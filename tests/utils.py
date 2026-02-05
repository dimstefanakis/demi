from __future__ import annotations

import uuid
from datetime import datetime, timezone

from demi.config import Settings
from demi.db.factory import build_database
from demi.models import NormalizedMessage


def build_test_db():
    settings = Settings()
    db = build_database(settings)
    db.init()
    _clear_outbox(db)
    _clear_queue_tables(db)
    _clear_pending_message_statuses(db)
    return db


def unique_external_id(prefix: str = "test") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def create_test_tenant(db, provider: str = "telegram", external_id: str | None = None):
    external_id = external_id or unique_external_id("tenant")
    return db.get_or_create_tenant(provider, external_id)


def create_message(
    db,
    tenant_id: int,
    provider: str = "telegram",
    provider_message_id: str | None = None,
    tenant_external_id: str | None = None,
    text: str | None = "hello",
    raw: dict | None = None,
    project_name: str | None = None,
):
    provider_message_id = provider_message_id or unique_external_id("msg")
    tenant_external_id = tenant_external_id or unique_external_id("user")
    message = NormalizedMessage(
        provider=provider,
        provider_message_id=provider_message_id,
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text=text,
        images=[],
        raw=raw or {},
        project_name=project_name,
    )
    message_id, _ = db.record_message(tenant_id, message)
    return message_id, message


def _clear_outbox(db) -> None:
    try:
        rows = db.list_outbox(status="queued", limit=1000)
    except Exception:
        return
    ids = [str(row["id"]) for row in rows if row.get("id")]
    if not ids:
        return
    try:
        db.update_outbox_statuses(ids, "sent")
    except Exception:
        return


def _clear_queue_tables(db) -> None:
    for table in (
        "run_inputs",
        "pending_messages",
        "active_runs",
    ):
        _try_delete_by_tenant(db, table)


def _try_delete_by_tenant(db, table: str) -> None:
    try:
        db._execute(db._table(table).delete().neq("tenant_id", 0))
    except Exception:
        return


def _clear_pending_message_statuses(db) -> None:
    try:
        db._execute(
            db._table("messages")
            .update({"status": "processed"})
            .in_("status", ["pending", "processing"])
        )
    except Exception:
        return

import asyncio
from datetime import datetime, timezone

import pytest

from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


class FakeAgent:
    async def prepare_context(
        self,
        workspace,
        task_path,
        message,
        messenger=None,
        inflight_stream=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        run_id=None,
        runtime_env=None,
        execution_context=None,
    ):
        return type("AgentResult", (), {"session_id": None, "summary": "ok"})()


class FakeMessenger:
    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        return None


@pytest.mark.asyncio
async def test_migrate_legacy_queue_moves_pending_messages(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="legacy-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Legacy pending",
        images=[],
        raw={},
        project_name="main",
    )
    message_id, _ = db.record_message(tenant.id, msg)
    db.update_message_status(message_id, "pending")
    # Supabase reads can be briefly stale across sequential writes in CI; ensure
    # pending status is visible before running migration assertions.
    for _ in range(10):
        row = db.get_message(message_id)
        if row and row.get("status") == "pending":
            break
        await asyncio.sleep(0.05)

    migrated = await orchestrator.migrate_legacy_queue()

    assert migrated == 1
    rows = db.fetch_run_inputs(tenant.id, None, status="queued")
    assert rows
    row = db.get_message(message_id)
    assert row["status"] == "processed"

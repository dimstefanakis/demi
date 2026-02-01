from datetime import datetime, timezone

import pytest

from claudius.db.core import Database
from claudius.models import NormalizedMessage
from claudius.orchestrator import Orchestrator
from claudius.workspace.core import WorkspaceManager


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
        runtime_env=None,
    ):
        return type("AgentResult", (), {"session_id": None, "summary": "ok"})()


class FakeMessenger:
    async def send_text(self, tenant_external_id, text):
        return None


@pytest.mark.asyncio
async def test_migrate_legacy_queue_moves_pending_messages(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "777")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="legacy-1",
        tenant_external_id="777",
        received_at=datetime.now(tz=timezone.utc),
        text="Legacy pending",
        images=[],
        raw={},
        project_name="main",
    )
    message_id, _ = db.record_message(tenant.id, msg)
    db.update_message_status(message_id, "pending")

    migrated = await orchestrator.migrate_legacy_queue()

    assert migrated == 1
    rows = db.fetch_run_inputs(tenant.id, "main", status="queued")
    assert rows
    row = db.connect().execute(
        "SELECT status FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row["status"] == "processed"

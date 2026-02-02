from datetime import datetime, timezone, timedelta

import pytest

from demi.db.core import Database
from demi.jobs.pending_worker import PendingWorker, PendingWorkerConfig
from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager


class FakeAgent:
    def __init__(self):
        self.calls = []

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
        self.calls.append((workspace.root, task_path))
        return type("AgentResult", (), {"session_id": None, "summary": "ok"})()

    async def send_interaction_message(
        self,
        workspace,
        text,
        messenger,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
    ) -> None:
        return None


class FakeMessenger:
    async def send_text(self, tenant_external_id, text):
        return None


@pytest.mark.asyncio
async def test_pending_worker_drains_queue(tmp_path):
    db = Database(tmp_path / "main.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "222")
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    raw = {
        "message": {
            "message_id": 123,
            "date": int(datetime.now(tz=timezone.utc).timestamp()),
            "chat": {"id": 222},
            "text": "How would you connect stripe?",
        }
    }
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="123",
        tenant_external_id="222",
        received_at=datetime.now(tz=timezone.utc),
        text="How would you connect stripe?",
        images=[],
        raw=raw,
        project_name=workspace.project_name,
    )
    message_id, _ = db.record_message(tenant.id, msg)
    orchestrator._enqueue_run_input(
        tenant_id=tenant.id,
        run_id=None,
        project_name=workspace.project_name,
        message_id=message_id,
        msg=msg,
        status="queued",
    )

    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            processing_grace_seconds=0,
        ),
    )

    groups = db.fetch_queued_run_input_groups(limit=5)
    assert groups
    await worker._handle_group(groups[0])

    row = db.connect().execute(
        "SELECT status FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row["status"] == "processed"
    assert agent.calls


@pytest.mark.asyncio
async def test_pending_worker_clears_stale_inflight_run(tmp_path):
    db = Database(tmp_path / "main.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "333")
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-1",
        tenant_external_id="333",
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    message_id, _ = db.record_message(tenant.id, msg)

    run_id = db.create_run(
        tenant.id,
        message_id=message_id,
        project_name=workspace.project_name,
        lease_seconds=3600,
    )
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=901)
    expired = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    conn = db.connect()
    conn.execute(
        "UPDATE runs SET started_at = ?, lease_expires_at = ? WHERE id = ?",
        (past.isoformat(), expired.isoformat(), run_id),
    )
    conn.commit()

    orchestrator._enqueue_run_input(
        tenant_id=tenant.id,
        run_id=None,
        project_name=workspace.project_name,
        message_id=message_id,
        msg=msg,
        status="queued",
    )

    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            processing_grace_seconds=0,
            run_stale_seconds=900,
        ),
    )

    groups = db.fetch_queued_run_input_groups(limit=5)
    assert groups
    await worker._handle_group(groups[0])

    run = db.connect().execute(
        "SELECT status, error FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert run["status"] == "failed"
    assert run["error"] == "lease_expired"
    row = db.connect().execute(
        "SELECT status FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row["status"] == "processed"

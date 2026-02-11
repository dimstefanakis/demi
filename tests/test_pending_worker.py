from datetime import datetime, timezone, timedelta

import pytest

from demi.jobs.pending_worker import PendingWorker, PendingWorkerConfig
from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        del (
            message,
            messenger,
            tenant_id,
            db,
            payments,
            session_id,
            provider,
            tenant_external_id,
            message_id,
            billing_checked,
            asset_paths,
            execution_bridge,
        )
        return {
            "ok": True,
            "project_name": workspace.project_name,
            "should_run": True,
            "queue_run": False,
            "dedupe": False,
            "ask_questions": [],
        }

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
        run_id=None,
        message_id=None,
    ) -> None:
        return None


class FakeMessenger:
    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        return None


@pytest.mark.asyncio
async def test_pending_worker_sleeps_when_inflight(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="inflight-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Queued request",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    message_id, _ = db.record_message(tenant.id, msg)
    db.create_run(
        tenant.id,
        message_id=message_id,
        project_name=workspace.project_name,
        lease_seconds=3600,
    )
    orchestrator._enqueue_run_input(
        tenant_id=tenant.id,
        run_id=None,
        project_name=workspace.project_name,
        message_id=message_id,
        msg=msg,
        status="queued",
    )

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("demi.jobs.pending_worker.asyncio.sleep", fake_sleep)

    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=0.05,
            batch_size=5,
            processing_grace_seconds=0,
        ),
    )

    groups = db.fetch_queued_run_input_groups(limit=5)
    assert groups
    await worker._handle_group(groups[0])

    assert slept


@pytest.mark.asyncio
async def test_pending_worker_idle_backoff_is_capped(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )
    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=2.5,
            batch_size=5,
            active_check_interval_seconds=10.0,
        ),
    )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            worker.stop()

    async def fake_check_active_runs() -> None:
        return None

    monkeypatch.setattr("demi.jobs.pending_worker.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(worker, "_check_active_runs", fake_check_active_runs)

    await worker.run_forever()

    assert sleeps == [5.0, 10.0, 10.0]


@pytest.mark.asyncio
async def test_pending_worker_drains_queue(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
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
        tenant_external_id=tenant.external_id,
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

    row = db.get_message(message_id)
    assert row is not None
    assert row["status"] == "processed"
    assert agent.calls


@pytest.mark.asyncio
async def test_pending_worker_recovers_stale_received_messages(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    stale_received_at = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-received-1",
        tenant_external_id=tenant.external_id,
        received_at=stale_received_at,
        text="Recover me",
        images=[],
        raw={},
        project_name="main",
    )
    message_id, inserted = db.record_message(tenant.id, msg)
    assert inserted is True
    assert db.get_message(message_id)["status"] == "received"

    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            processing_grace_seconds=30,
        ),
    )

    await worker._recover_stale_received_messages()

    row = db.get_message(message_id)
    assert row is not None
    assert row["status"] == "processed"
    assert agent.calls


@pytest.mark.asyncio
async def test_pending_worker_stale_received_failure_is_terminal(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    stale_received_at = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-received-fail-1",
        tenant_external_id=tenant.external_id,
        received_at=stale_received_at,
        text="Recover me maybe",
        images=[],
        raw={},
        project_name="main",
    )
    message_id, inserted = db.record_message(tenant.id, msg)
    assert inserted is True
    assert db.get_message(message_id)["status"] == "received"

    async def _boom(
        _msg,
        *,
        allow_existing_received=False,
        allow_interaction_stream=True,
    ):
        del _msg, allow_existing_received, allow_interaction_stream
        raise RuntimeError("forced_failure")

    monkeypatch.setattr(orchestrator, "handle_message", _boom)

    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            processing_grace_seconds=30,
        ),
    )

    await worker._recover_stale_received_messages()

    row = db.get_message(message_id)
    assert row is not None
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_pending_worker_stale_received_left_received_is_terminal(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    stale_received_at = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-received-stuck-1",
        tenant_external_id=tenant.external_id,
        received_at=stale_received_at,
        text="still received",
        images=[],
        raw={},
        project_name="main",
    )
    message_id, inserted = db.record_message(tenant.id, msg)
    assert inserted is True
    assert db.get_message(message_id)["status"] == "received"

    class _Accepted:
        status = "accepted"

    async def _stuck(
        _msg,
        *,
        allow_existing_received=False,
        allow_interaction_stream=True,
    ):
        del _msg, allow_existing_received, allow_interaction_stream
        # Simulates a route attempt that returned without transitioning status.
        return _Accepted()

    monkeypatch.setattr(orchestrator, "handle_message", _stuck)

    worker = PendingWorker(
        db=db,
        orchestrator=orchestrator,
        config=PendingWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            processing_grace_seconds=30,
        ),
    )

    await worker._recover_stale_received_messages()

    row = db.get_message(message_id)
    assert row is not None
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_pending_worker_clears_stale_inflight_run(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-1",
        tenant_external_id=tenant.external_id,
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
    db.update_run_timestamps(
        run_id,
        started_at=past.isoformat(),
        lease_expires_at=expired.isoformat(),
    )

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

    run = db.get_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] == "lease_expired"
    row = db.get_message(message_id)
    assert row["status"] == "processed"

import asyncio
from types import SimpleNamespace

import pytest

from demi.jobs.outbox_worker import OutboxWorker, OutboxWorkerConfig
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


class FakeInteractionAgent:
    def __init__(self):
        self.instructions = []

    async def send_interaction_instruction(
        self,
        workspace,
        instruction,
        messenger,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        run_id=None,
        message_id=None,
    ):
        self.instructions.append(
            {
                "instruction": instruction,
                "tenant_id": tenant_id,
                "tenant_external_id": tenant_external_id,
                "workspace": workspace,
            }
        )
        return None


class UsageInteractionAgent:
    async def send_interaction_instruction(
        self,
        workspace,
        instruction,
        messenger,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        run_id=None,
        message_id=None,
    ):
        return SimpleNamespace(total_cost_usd=0.5, usage={"input_tokens": 5})


class SessionInteractionAgent:
    def __init__(self, returned_session_id: str):
        self.received_session_ids: list[str | None] = []
        self.returned_session_id = returned_session_id

    async def send_interaction_instruction(
        self,
        workspace,
        instruction,
        messenger,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        run_id=None,
        message_id=None,
    ):
        self.received_session_ids.append(session_id)
        return SimpleNamespace(session_id=self.returned_session_id)


class TimeoutInteractionAgent:
    async def send_interaction_instruction(
        self,
        workspace,
        instruction,
        messenger,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        run_id=None,
        message_id=None,
    ):
        await asyncio.sleep(0.2)
        return None


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        self.sent.append((tenant_external_id, text))


@pytest.mark.asyncio
async def test_outbox_worker_routes_interaction_updates(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="main")

    outbox_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name="main",
        correlation_id="update-1",
        payload={
            "type": "interaction_update",
            "text": "Finished step 1",
            "final": True,
            "tenant_external_id": tenant.external_id,
            "provider": "telegram",
            "project_name": "main",
        },
    )

    agent = FakeInteractionAgent()
    worker = OutboxWorker(
        db=db,
        messenger=FakeMessenger(),
        config=OutboxWorkerConfig(poll_interval=0.01, batch_size=5),
        interaction_agent=agent,
        workspace_manager=workspace_manager,
    )

    await worker.process_once()

    assert agent.instructions
    assert "Final: True" in agent.instructions[0]["instruction"]
    assert "Correlation ID: update-1" in agent.instructions[0]["instruction"]
    rows = db.list_outbox(tenant.id, limit=5)
    row = next((item for item in rows if str(item.get("id")) == str(outbox_id)), None)
    assert row is not None
    assert row["status"] == "sent"


@pytest.mark.asyncio
async def test_outbox_worker_routes_pm_suggestions(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="main")

    outbox_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name="main",
        correlation_id="pm-suggestion-1",
        payload={
            "type": "pm_suggestion",
            "action": "pm_suggestion",
            "text": "i checked your site and noticed missing testimonials. want me to add them?",
            "priority": "medium",
            "pm_action_type": "suggest_feature",
            "autonomy_tier": "suggest",
            "tenant_external_id": tenant.external_id,
            "provider": "telegram",
        },
    )

    agent = FakeInteractionAgent()
    worker = OutboxWorker(
        db=db,
        messenger=FakeMessenger(),
        config=OutboxWorkerConfig(poll_interval=0.01, batch_size=5),
        interaction_agent=agent,
        workspace_manager=workspace_manager,
    )

    await worker.process_once()

    assert agent.instructions
    instruction = agent.instructions[0]["instruction"]
    assert "Proactive suggestion for the user" in instruction
    assert "pm_action_type" not in instruction
    assert "Autonomy tier" not in instruction
    assert "Correlation ID: pm-suggestion-1" in instruction
    rows = db.list_outbox(tenant.id, limit=5)
    row = next((item for item in rows if str(item.get("id")) == str(outbox_id)), None)
    assert row is not None
    assert row["status"] == "sent"


@pytest.mark.asyncio
async def test_outbox_worker_records_interaction_usage(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="main")

    run_id = db.create_run(tenant.id, message_id=None, project_name="main")
    db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=run_id,
        project_name="main",
        correlation_id="update-usage",
        payload={
            "type": "interaction_update",
            "text": "Working on it",
            "final": False,
            "tenant_external_id": tenant.external_id,
            "provider": "telegram",
            "project_name": "main",
        },
    )

    worker = OutboxWorker(
        db=db,
        messenger=FakeMessenger(),
        config=OutboxWorkerConfig(poll_interval=0.01, batch_size=5),
        interaction_agent=UsageInteractionAgent(),
        workspace_manager=workspace_manager,
    )

    await worker.process_once()

    row = db.get_run(run_id)
    assert row is not None
    assert row["total_cost_usd"] == pytest.approx(0.5)
    usage = row["usage_json"]
    assert usage["interaction"][0]["input_tokens"] == 5


@pytest.mark.asyncio
async def test_outbox_worker_uses_and_updates_interaction_session_state(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="main")
    db.set_tenant_kv(
        tenant.id,
        "interaction",
        "claude_instruction_session",
        {"session_id": "interaction-session-prev"},
    )

    db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name="main",
        correlation_id="update-session",
        payload={
            "type": "interaction_update",
            "text": "Session update ping",
            "final": False,
            "tenant_external_id": tenant.external_id,
            "provider": "telegram",
            "project_name": "main",
        },
    )

    agent = SessionInteractionAgent(returned_session_id="interaction-session-next")
    worker = OutboxWorker(
        db=db,
        messenger=FakeMessenger(),
        config=OutboxWorkerConfig(poll_interval=0.01, batch_size=5),
        interaction_agent=agent,
        workspace_manager=workspace_manager,
    )

    await worker.process_once()

    assert agent.received_session_ids == ["interaction-session-prev"]
    payload = db.get_tenant_kv(tenant.id, "interaction", "claude_instruction_session") or {}
    assert payload.get("session_id") == "interaction-session-next"


@pytest.mark.asyncio
async def test_outbox_worker_requeues_timed_out_interaction_update(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="main")

    outbox_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name="main",
        correlation_id="update-timeout",
        payload={
            "type": "interaction_update",
            "text": "Still working...",
            "final": False,
            "tenant_external_id": tenant.external_id,
            "provider": "telegram",
            "project_name": "main",
        },
    )

    worker = OutboxWorker(
        db=db,
        messenger=FakeMessenger(),
        config=OutboxWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            interaction_send_timeout_seconds=0.05,
            retry_base_seconds=0.01,
            retry_max_seconds=0.05,
        ),
        interaction_agent=TimeoutInteractionAgent(),
        workspace_manager=workspace_manager,
    )

    await worker.process_once()

    rows = db.list_outbox(tenant.id, limit=10)
    row = next((item for item in rows if str(item.get("id")) == str(outbox_id)), None)
    assert row is not None
    # Timed-out sends should be retried, not permanently dropped.
    assert row["status"] in {"queued", "failed"}

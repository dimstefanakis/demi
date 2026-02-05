from datetime import datetime, timezone

import pytest

from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, unique_external_id


class FakeAgent:
    def __init__(self, decision):
        self.decision = decision
        self.route_calls = []
        self.prepare_calls = []

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
    ):
        self.route_calls.append(
            {
                "workspace": workspace,
                "message": message,
                "tenant_id": tenant_id,
            }
        )
        return self.decision

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
        self.prepare_calls.append((workspace.root, task_path, message))
        return type(
            "AgentResult",
            (),
            {"session_id": session_id, "summary": "ok", "total_cost_usd": None, "usage": None},
        )()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        self.sent.append((tenant_external_id, text))


@pytest.mark.asyncio
async def test_interaction_decision_can_skip_run(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": ["What should the headline say?"],
        "purpose": "clarify",
        "plan": None,
    }
    agent = FakeAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Can you make it premium?",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.route_calls
    assert not agent.prepare_calls

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    runs = db.list_recent_runs(tenant.id, project_name=None, limit=5)
    assert len(runs) == 0
    row = db.get_message_by_provider_id(tenant.id, "i-1")
    assert row is not None
    assert row["status"] == "processed"


@pytest.mark.asyncio
async def test_interaction_decision_creates_run(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": True,
        "queue_run": False,
        "dedupe": False,
        "ask_questions": [],
        "purpose": "build",
        "plan": "ship a first draft",
    }
    agent = FakeAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build me a landing page",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.prepare_calls
    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    runs = db.list_recent_runs(tenant.id, project_name=None, limit=1)
    assert runs
    run_id = int(runs[0]["id"])
    run = db.get_run(run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_summary"] == "ok"
    decision_payload = run["interaction_decision_json"]
    assert decision_payload["purpose"] == "build"

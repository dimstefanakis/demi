from datetime import datetime, timezone

import pytest

from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


class FakeAgentNoDeploy:
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
        return {
            "ok": True,
            "project_name": workspace.project_name,
            "should_run": True,
            "queue_run": False,
            "dedupe": False,
            "ask_questions": [],
            "purpose": "answer",
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
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_stale_deploy_url_not_sent(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgentNoDeploy(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="51",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="What was this site built with?",
        images=[],
        raw={},
    )

    workspace = workspace_manager.ensure_workspace(tenant.key)
    (workspace.tasks_dir / "deploy_url.txt").write_text("https://stale.example.com")

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert orchestrator.messenger.sent == []

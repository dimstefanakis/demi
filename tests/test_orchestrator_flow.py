from datetime import datetime, timezone

import pytest

from claudius.db.core import Database
from claudius.models import NormalizedMessage
from claudius.orchestrator import Orchestrator
from claudius.workspace.core import WorkspaceManager


class FakeAgent:
    def __init__(self, deploy_url="https://example.com/site"):
        self.calls = []
        self.deploy_url = deploy_url

    async def prepare_context(self, workspace, task_path, message, messenger=None, session_id=None):
        self.calls.append((workspace.root, task_path, session_id))
        (workspace.tasks_dir / "response.json").write_text(
            "{\"kind\": \"deploy\", \"deploy_url\": \"%s\"}" % self.deploy_url
        )
        (workspace.tasks_dir / "result_summary.md").write_text("ok")
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text):
        self.sent.append((tenant_external_id, text))


@pytest.mark.asyncio
async def test_orchestrator_new_site_flow(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="51",
        tenant_external_id="987654",
        received_at=datetime.now(tz=timezone.utc),
        text="Build me a barber site",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    tenant = db.get_or_create_tenant("telegram", "987654")
    assert tenant.last_deploy_url == "https://example.com/site"
    assert (workspace_manager.root_dir / tenant.key / "tasks" / "response.json").exists()
    assert "https://example.com/site" in orchestrator.messenger.sent[0][1]

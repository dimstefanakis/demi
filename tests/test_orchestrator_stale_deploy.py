from datetime import datetime, timezone

import pytest

from claudius.db.core import Database
from claudius.models import NormalizedMessage
from claudius.orchestrator import Orchestrator
from claudius.workspace.core import WorkspaceManager


class FakeAgentNoDeploy:
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
        if messenger is not None:
            await messenger.send_text(message.tenant_external_id, "It was built with Next.js.")
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_stale_deploy_url_not_sent(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgentNoDeploy(),
        messenger=FakeMessenger(),
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="51",
        tenant_external_id="987654",
        received_at=datetime.now(tz=timezone.utc),
        text="What was this site built with?",
        images=[],
        raw={},
    )

    workspace = workspace_manager.ensure_workspace("telegram:987654")
    (workspace.tasks_dir / "deploy_url.txt").write_text("https://stale.example.com")

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert orchestrator.messenger.sent == ["It was built with Next.js."]

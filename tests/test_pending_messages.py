from datetime import datetime, timezone

import pytest

from claudius.db.core import Database
from claudius.models import NormalizedMessage
from claudius.orchestrator import Orchestrator
from claudius.workspace.core import WorkspaceManager


class FakeAgent:
    def __init__(self):
        self.messages = []

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
    ):
        self.messages.append(message)
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    async def send_text(self, tenant_external_id, text):
        return None


def _raw_update(message_id: str, text: str):
    return {
        "message": {
            "message_id": int(message_id),
            "date": int(datetime.now(tz=timezone.utc).timestamp()),
            "chat": {"id": 123},
            "text": text,
        }
    }


@pytest.mark.asyncio
async def test_pending_messages_coalesced(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    tenant = db.get_or_create_tenant("telegram", "123")

    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=WorkspaceManager(root_dir=tmp_path / "data"),
        agent=agent,
        messenger=FakeMessenger(),
    )

    msg_a = NormalizedMessage(
        provider="telegram",
        provider_message_id="1",
        tenant_external_id="123",
        received_at=datetime.now(tz=timezone.utc),
        text="Use this header image",
        images=[],
        raw=_raw_update("1", "Use this header image"),
    )
    msg_b = NormalizedMessage(
        provider="telegram",
        provider_message_id="2",
        tenant_external_id="123",
        received_at=datetime.now(tz=timezone.utc),
        text="And update the gallery text",
        images=[],
        raw=_raw_update("2", "And update the gallery text"),
    )

    id_a, _ = db.record_message(tenant.id, msg_a)
    id_b, _ = db.record_message(tenant.id, msg_b)
    db.update_message_status(id_a, "pending")
    db.update_message_status(id_b, "pending")

    await orchestrator._drain_pending_messages(tenant)

    assert len(agent.messages) == 1
    combined = agent.messages[0].text or ""
    assert "Use this header image" in combined
    assert "And update the gallery text" in combined

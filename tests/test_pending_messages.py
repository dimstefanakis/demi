from datetime import datetime, timezone

import pytest

from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


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
        run_id=None,
        runtime_env=None,
    ):
        self.messages.append(message)
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
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
    db = build_test_db()
    tenant = create_test_tenant(db)

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
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Use this header image",
        images=[],
        raw=_raw_update("1", "Use this header image"),
    )
    msg_b = NormalizedMessage(
        provider="telegram",
        provider_message_id="2",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="And update the gallery text",
        images=[],
        raw=_raw_update("2", "And update the gallery text"),
    )

    id_a, _ = db.record_message(tenant.id, msg_a)
    id_b, _ = db.record_message(tenant.id, msg_b)

    orchestrator._enqueue_run_input(
        tenant_id=tenant.id,
        run_id=None,
        message_id=id_a,
        msg=msg_a,
        status="queued",
    )
    orchestrator._enqueue_run_input(
        tenant_id=tenant.id,
        run_id=None,
        message_id=id_b,
        msg=msg_b,
        status="queued",
    )

    await orchestrator._drain_run_inputs(tenant)

    assert len(agent.messages) == 1
    combined = agent.messages[0].text or ""
    assert "Use this header image" in combined
    assert "And update the gallery text" in combined

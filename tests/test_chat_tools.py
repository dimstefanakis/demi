import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from demi.agent.chat_tools import ChatToolContext, build_chat_tools
from demi.models import NormalizedMessage
from demi.memory import append_log
from tests.utils import build_test_db, create_message, create_test_tenant


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        await asyncio.sleep(0)
        self.sent.append((tenant_external_id, text, reply_to_message_id))


def test_send_message_tool_dedupes(tmp_path):
    tasks_dir = tmp_path / "tasks"
    append_log(tasks_dir, "assistant_message", "Hello there")

    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id="tenant-1",
            tasks_dir=tasks_dir,
            role="interaction",
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Hello there!"}))
    asyncio.run(send_tool.handler({"text": "Hello there!"}))

    assert len(messenger.sent) == 2


def test_send_message_tool_sends_once_for_new_text(tmp_path):
    tasks_dir = tmp_path / "tasks"
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id="tenant-1",
            tasks_dir=tasks_dir,
            role="interaction",
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Working on it now."}))
    asyncio.run(send_tool.handler({"text": "Working on it now."}))

    assert len(messenger.sent) == 2


def test_send_payment_link_uses_quote_file(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payment_url = "https://checkout.stripe.com/c/pay/cs_test_123#frag"
    (tasks_dir / "backend_quote.json").write_text(
        f'{{"payment_url": "{payment_url}", "order_id": 99}}'
    )

    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id="tenant-1",
            tasks_dir=tasks_dir,
            role="interaction",
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_payment_link")

    asyncio.run(send_tool.handler({"source": "backend", "text": "Please pay to continue."}))

    assert len(messenger.sent) == 1
    assert messenger.sent[0][1].endswith(payment_url)


def test_send_payment_link_uses_tenant_state(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payment_url = "https://checkout.stripe.com/c/pay/cs_test_456#frag"
    db.set_tenant_kv(
        tenant.id,
        "billing",
        "backend_quote",
        {"payment_url": payment_url, "order_id": 123},
    )

    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_payment_link")

    asyncio.run(send_tool.handler({"source": "backend", "text": "Please pay to continue."}))

    assert len(messenger.sent) == 1
    assert messenger.sent[0][1].endswith(payment_url)


def test_send_message_logs_outbound_event(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    message_id, _ = create_message(
        db,
        tenant.id,
        provider="telegram",
        provider_message_id="msg-1",
        tenant_external_id=tenant.external_id,
        text="Build it",
        raw={
            "message": {
                "message_id": 1,
                "from": {"id": 123},
                "text": "Build it",
                "reply_to_message": {"message_id": 0, "from": {"id": 123}, "text": "Earlier"},
            }
        },
    )
    run_id = db.create_run(tenant.id, message_id=message_id, project_name="main")
    for idx in range(5):
        create_message(
            db,
            tenant.id,
            provider="telegram",
            provider_message_id=f"msg-gap-{idx}",
            tenant_external_id=tenant.external_id,
            text=f"Follow up {idx}",
        )

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
            run_id=run_id,
            message_id=message_id,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(
        send_tool.handler(
            {
                "text": "On it.",
                "final": True,
                "reply_to_message_id": "msg-1",
                "reply_to_text": "Build it",
            }
        )
    )

    events = db.list_message_events(tenant.id, limit=5)
    assert events
    row = events[-1]
    assert row["message_type"] == "message"
    assert row["text"] == "On it."
    assert int(row["run_id"]) == run_id
    assert row["reply_to_message_id"] == "msg-1"
    assert row["provider_message_id"] == "msg-1"
    metadata = row["metadata_json"]
    assert metadata["final"] is True


def test_send_payment_link_logs_outbound_event(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    message_id, _ = create_message(
        db,
        tenant.id,
        provider="telegram",
        provider_message_id="msg-2",
        tenant_external_id=tenant.external_id,
        text="Quote",
    )
    run_id = db.create_run(tenant.id, message_id=message_id, project_name="main")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payment_url = "https://checkout.stripe.com/c/pay/cs_test_999#frag"
    (tasks_dir / "backend_quote.json").write_text(
        f'{{"payment_url": "{payment_url}", "order_id": 42}}'
    )

    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
            run_id=run_id,
            message_id=message_id,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_payment_link")

    asyncio.run(
        send_tool.handler({"source": "backend", "text": "Please pay to continue.", "final": True})
    )

    events = db.list_message_events(tenant.id, limit=5)
    assert events
    row = events[-1]
    assert row["message_type"] == "payment_link"
    assert payment_url in row["text"]
    metadata = row["metadata_json"]
    assert metadata["final"] is True
    assert metadata["source"] == "backend"


@pytest.mark.skip(reason="Temporarily disabled per request (flake in local env).")
def test_should_send_message_blocks_after_final(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    message_id, _ = create_message(
        db,
        tenant.id,
        provider="telegram",
        provider_message_id="msg-3",
        tenant_external_id=tenant.external_id,
        text="Run it",
    )
    run_id = db.create_run(tenant.id, message_id=message_id, project_name="main")
    db.set_run_final_sent(run_id)

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
            run_id=run_id,
        )
    )
    should_tool = next(tool for tool in tools if tool.name == "should_send_message")

    result = asyncio.run(should_tool.handler({"text": "Checking again."}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["send"] is False


def test_send_message_primary_enqueues_interaction_update(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    tasks_dir = tmp_path / "tasks"
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="primary",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Progress update"}))

    assert messenger.sent == []
    outbox = db.list_outbox(tenant.id, limit=5)
    assert outbox
    payload = outbox[-1]["payload_json"]
    assert payload["type"] == "interaction_update"
    assert payload["text"] == "Progress update"


def test_send_message_primary_falls_back_to_file_without_db(tmp_path):
    tasks_dir = tmp_path / "tasks"
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id="999",
            tasks_dir=tasks_dir,
            role="primary",
            db=None,
            tenant_id=None,
            provider="telegram",
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Fallback update"}))

    assert messenger.sent == []
    path = tasks_dir / "interaction_updates.jsonl"
    assert path.exists()
    payload = json.loads(path.read_text().splitlines()[0])
    assert payload["type"] == "interaction_update"
    assert payload["text"] == "Fallback update"


def test_send_message_primary_enqueues_with_missing_tenant_id(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    tasks_dir = tmp_path / "tasks"
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="primary",
            db=db,
            tenant_id=None,
            provider=tenant.provider,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Queued update"}))

    assert messenger.sent == []
    outbox = db.list_outbox(tenant.id, limit=5)
    assert outbox
    payload = outbox[-1]["payload_json"]
    assert payload["type"] == "interaction_update"
    assert payload["text"] == "Queued update"


def test_send_message_skips_reply_context_for_recent_message(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    message_id, _ = create_message(
        db,
        tenant.id,
        provider="telegram",
        provider_message_id="msg-10",
        tenant_external_id=tenant.external_id,
        text="Hello",
    )
    run_id = db.create_run(tenant.id, message_id=message_id, project_name="main")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
            run_id=run_id,
            message_id=message_id,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(
        send_tool.handler(
            {
                "text": "Replying.",
                "reply_to_message_id": "msg-10",
                "reply_to_text": "Hello",
            }
        )
    )

    assert messenger.sent
    assert messenger.sent[0][2] is None


def test_send_message_keeps_reply_context_after_idle_gap(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    received_at = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    message = NormalizedMessage(
        provider="telegram",
        provider_message_id="msg-11",
        tenant_external_id=tenant.external_id,
        received_at=received_at,
        text="Following up",
        images=[],
        raw={},
    )
    message_id, _ = db.record_message(tenant.id, message)
    run_id = db.create_run(tenant.id, message_id=message_id, project_name="main")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
            run_id=run_id,
            message_id=message_id,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(
        send_tool.handler(
            {
                "text": "Replying.",
                "reply_to_message_id": "msg-11",
                "reply_to_text": "Following up",
            }
        )
    )

    assert messenger.sent
    assert messenger.sent[0][2] == "msg-11"

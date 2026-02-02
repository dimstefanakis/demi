import asyncio
import json
from pathlib import Path

from demi.agent.chat_tools import ChatToolContext, build_chat_tools
from demi.tenant_db import ensure_tenant_db
from demi.memory import append_log


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text):
        await asyncio.sleep(0)
        self.sent.append((tenant_external_id, text))


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


def test_send_payment_link_uses_tenant_db(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payment_url = "https://checkout.stripe.com/c/pay/cs_test_456#frag"
    db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
    db.set_kv("billing", "backend_quote", {"payment_url": payment_url, "order_id": 123})

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


def test_should_send_message_blocks_after_final(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "run_request.json").write_text(
        json.dumps({"message": {"provider_message_id": "msg-1"}})
    )
    db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
    db.set_kv("system", "final_sent", {"run_id": "msg-1"})

    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger, tenant_external_id="tenant-1", tasks_dir=tasks_dir
        )
    )
    should_tool = next(tool for tool in tools if tool.name == "should_send_message")

    result = asyncio.run(should_tool.handler({"text": "Checking again."}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["send"] is False


def test_send_message_blocks_on_reply_to_mismatch(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "run_request.json").write_text(
        json.dumps({"message": {"provider_message_id": "msg-9", "text": "Ping?"}})
    )
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger, tenant_external_id="tenant-1", tasks_dir=tasks_dir
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(
        send_tool.handler(
            {
                "text": "Working on it.",
                "reply_to_message_id": "msg-9",
                "reply_to_text": "Different",
            }
        )
    )

    assert len(messenger.sent) == 0


def test_should_send_message_blocks_on_reply_to_mismatch(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "run_request.json").write_text(
        json.dumps({"message": {"provider_message_id": "msg-7", "text": "Status?"}})
    )
    messenger = FakeMessenger()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger, tenant_external_id="tenant-1", tasks_dir=tasks_dir
        )
    )
    should_tool = next(tool for tool in tools if tool.name == "should_send_message")

    result = asyncio.run(
        should_tool.handler(
            {
                "text": "On it now.",
                "reply_to_message_id": "msg-7",
                "reply_to_text": "Other",
            }
        )
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["send"] is False

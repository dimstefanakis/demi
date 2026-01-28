import asyncio
from pathlib import Path

from claudius.agent.chat_tools import ChatToolContext, build_chat_tools
from claudius.memory import append_log


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
            messenger=messenger, tenant_external_id="tenant-1", tasks_dir=tasks_dir
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
            messenger=messenger, tenant_external_id="tenant-1", tasks_dir=tasks_dir
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Working on it now."}))
    asyncio.run(send_tool.handler({"text": "Working on it now."}))

    assert len(messenger.sent) == 2

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


class FakeExecutionBridge:
    def __init__(self, agents=None):
        self._agents = list(agents or [])
        self.stream_calls = []
        self.stop_calls = []

    def list_execution_agents(self, tenant_id, tenant_key):
        del tenant_id, tenant_key
        return list(self._agents)

    async def stream_to_execution_agent(self, tenant_key, run_id, text):
        self.stream_calls.append(
            {
                "tenant_key": tenant_key,
                "run_id": run_id,
                "text": text,
            }
        )
        return {"ok": True, "status": "streamed", "run_id": run_id}

    async def stop_execution_agent(self, run_id, reason, notify):
        self.stop_calls.append(
            {
                "run_id": run_id,
                "reason": reason,
                "notify": notify,
            }
        )
        return type("StopResult", (), {"status": "accepted", "detail": "run_cancelled"})()


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

    # Tool-level dedupe should skip redundant sends even if the model fails to call should_send_message.
    assert len(messenger.sent) == 0


def test_send_message_dedupes_url_variants(tmp_path):
    tasks_dir = tmp_path / "tasks"
    append_log(
        tasks_dir,
        "assistant_message",
        "Use this checkout link: https://example.com/pay/session_abc now.",
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
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(
        send_tool.handler(
            {"text": "Use this checkout link https://example.com/pay/session_xyz now"}
        )
    )

    assert messenger.sent == []


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

    assert len(messenger.sent) == 1


def test_send_message_interaction_triggers_stream_close_callback(tmp_path):
    tasks_dir = tmp_path / "tasks"
    messenger = FakeMessenger()
    calls: list[str] = []
    tools = build_chat_tools(
        ChatToolContext(
            messenger=messenger,
            tenant_external_id="tenant-1",
            tasks_dir=tasks_dir,
            role="interaction",
            on_interaction_message_sent=lambda: calls.append("closed"),
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Working on it now."}))

    assert len(messenger.sent) == 1
    assert calls == ["closed"]


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
                "correlation_id": "update-1",
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
    assert metadata["correlation_id"] == "update-1"


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
        send_tool.handler(
            {
                "source": "backend",
                "text": "Please pay to continue.",
                "final": True,
                "correlation_id": "update-pay-1",
            }
        )
    )

    events = db.list_message_events(tenant.id, limit=5)
    assert events
    row = events[-1]
    assert row["message_type"] == "payment_link"
    assert payment_url in row["text"]
    metadata = row["metadata_json"]
    assert metadata["final"] is True
    assert metadata["source"] == "backend"
    assert metadata["correlation_id"] == "update-pay-1"


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
    project_name = "alpha"
    tasks_dir = tmp_path / "projects" / project_name / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    run_id = db.create_run(tenant.id, project_name=project_name)
    correlation_id = "execution-final:77"
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
            run_id=run_id,
            execution_context=project_name,
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_message")

    asyncio.run(send_tool.handler({"text": "Progress update", "correlation_id": correlation_id}))

    assert messenger.sent == []
    outbox = db.list_outbox(tenant.id, limit=5)
    assert outbox
    row = outbox[-1]
    assert int(row["run_id"]) == run_id
    assert row["project_name"] == project_name
    assert row["correlation_id"] == correlation_id
    payload = row["payload_json"]
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


def test_stream_to_execution_agent_selects_run_by_id(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    bridge = FakeExecutionBridge(
        agents=[
            {"run_id": 41, "project_name": "main", "status": "running"},
            {"run_id": 42, "project_name": "kappa", "status": "running"},
        ]
    )
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id="tenant-1",
            tenant_key="telegram:tenant-1",
            provider="telegram",
            tasks_dir=tasks_dir,
            role="interaction",
            execution_bridge=bridge,
        )
    )
    stream_tool = next(tool for tool in tools if tool.name == "stream_to_execution_agent")

    result = asyncio.run(stream_tool.handler({"text": "keep going", "run_id": 42}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert bridge.stream_calls
    assert bridge.stream_calls[-1]["run_id"] == 42


def test_stream_to_execution_agent_requires_run_id_when_ambiguous(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    bridge = FakeExecutionBridge(
        agents=[
            {"run_id": 51, "project_name": "main", "status": "running"},
            {"run_id": 52, "project_name": "main", "status": "running"},
        ]
    )
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id="tenant-2",
            tenant_key="telegram:tenant-2",
            provider="telegram",
            tasks_dir=tasks_dir,
            role="interaction",
            execution_bridge=bridge,
        )
    )
    stream_tool = next(tool for tool in tools if tool.name == "stream_to_execution_agent")

    result = asyncio.run(stream_tool.handler({"text": "update active run"}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is False
    assert payload["status"] == "ambiguous_run"
    assert bridge.stream_calls == []


def test_stream_to_execution_agent_skips_file_fallback_for_db_stream(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    class DbStreamBridge(FakeExecutionBridge):
        async def stream_to_execution_agent(self, tenant_key, run_id, text):
            self.stream_calls.append(
                {
                    "tenant_key": tenant_key,
                    "run_id": run_id,
                    "text": text,
                }
            )
            return {
                "ok": True,
                "status": "db_stream",
                "run_id": run_id,
                "stream_input_id": "db-1",
            }

    bridge = DbStreamBridge(agents=[{"run_id": 90, "project_name": "main", "status": "running"}])
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id="tenant-db-stream",
            tenant_key="telegram:tenant-db-stream",
            provider="telegram",
            tasks_dir=tasks_dir,
            role="interaction",
            execution_bridge=bridge,
        )
    )
    stream_tool = next(tool for tool in tools if tool.name == "stream_to_execution_agent")

    result = asyncio.run(stream_tool.handler({"text": "continue work"}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload["status"] == "db_stream"
    assert bridge.stream_calls
    assert not (tasks_dir / "inflight_updates.jsonl").exists()


def test_stream_to_execution_agent_writes_file_fallback_for_file_stream(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    class FileStreamBridge(FakeExecutionBridge):
        async def stream_to_execution_agent(self, tenant_key, run_id, text):
            self.stream_calls.append(
                {
                    "tenant_key": tenant_key,
                    "run_id": run_id,
                    "text": text,
                }
            )
            return {
                "ok": True,
                "status": "file_stream",
                "run_id": run_id,
            }

    bridge = FileStreamBridge(agents=[{"run_id": 91, "project_name": "main", "status": "running"}])
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id="tenant-file-stream",
            tenant_key="telegram:tenant-file-stream",
            provider="telegram",
            tasks_dir=tasks_dir,
            role="interaction",
            execution_bridge=bridge,
        )
    )
    stream_tool = next(tool for tool in tools if tool.name == "stream_to_execution_agent")

    result = asyncio.run(stream_tool.handler({"text": "continue work"}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload["status"] == "file_stream"
    updates_path = tasks_dir / "inflight_updates.jsonl"
    assert updates_path.exists()
    lines = updates_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["text"] == "continue work"


def test_upsert_execution_context_forks_instead_of_renaming(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    existing = db.ensure_execution_agent(tenant_id=tenant.id, context="Calculator")
    assert existing is not None

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id=tenant.external_id,
            tenant_key=tenant.key,
            provider=tenant.provider,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
        )
    )
    upsert_tool = next(tool for tool in tools if tool.name == "upsert_execution_context")

    result = asyncio.run(
        upsert_tool.handler(
            {
                "execution_agent_id": int(existing["id"]),
                "context": "Tic Tac Toe",
                "status": "active",
            }
        )
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload["status"] == "context_forked"
    assert int(payload["forked_from_execution_agent_id"]) == int(existing["id"])
    forked_id = int(payload["execution_agent"]["execution_agent_id"])
    assert forked_id != int(existing["id"])

    rows = db.list_execution_agents(tenant.id, include_inactive=True, limit=10)
    contexts = {str(row.get("context") or "") for row in rows}
    assert "Calculator" in contexts
    assert "Tic Tac Toe" in contexts

    original_row = db.get_execution_agent(int(existing["id"]))
    assert original_row is not None
    assert original_row["context"] == "Calculator"


def test_upsert_execution_context_renames_only_when_explicit(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    existing = db.ensure_execution_agent(tenant_id=tenant.id, context="Calculator")
    assert existing is not None

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id=tenant.external_id,
            tenant_key=tenant.key,
            provider=tenant.provider,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
        )
    )
    upsert_tool = next(tool for tool in tools if tool.name == "upsert_execution_context")

    result = asyncio.run(
        upsert_tool.handler(
            {
                "execution_agent_id": int(existing["id"]),
                "context": "Tic Tac Toe",
                "status": "active",
                "rename_existing": True,
            }
        )
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload.get("status") is None
    assert int(payload["execution_agent"]["execution_agent_id"]) == int(existing["id"])
    assert payload["execution_agent"]["context"] == "Tic Tac Toe"

def test_stop_execution_agent_rejects_cross_tenant_run(tmp_path):
    db = build_test_db()
    tenant_a = create_test_tenant(db)
    tenant_b = create_test_tenant(db)
    run_id = db.create_run(tenant_b.id, message_id=0, project_name="main")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    bridge = FakeExecutionBridge()
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id=tenant_a.external_id,
            tenant_key=tenant_a.key,
            provider=tenant_a.provider,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant_a.id,
            execution_bridge=bridge,
        )
    )
    stop_tool = next(tool for tool in tools if tool.name == "stop_execution_agent")

    result = asyncio.run(stop_tool.handler({"run_id": run_id}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is False
    assert payload["status"] == "not_found"
    assert bridge.stop_calls == []


def test_send_payment_link_bypasses_when_testing_mode_enabled(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    db.set_tenant_kv(
        tenant.id,
        "system",
        "testing_mode",
        {"enabled": True},
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
        )
    )
    send_tool = next(tool for tool in tools if tool.name == "send_payment_link")

    result = asyncio.run(send_tool.handler({"text": "please pay"}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload["status"] == "testing_mode_bypass"
    assert messenger.sent == []


def test_request_assistant_subscription_bypasses_when_testing_mode_enabled(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    db.set_tenant_kv(
        tenant.id,
        "system",
        "testing_mode",
        {"enabled": True},
    )
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id=tenant.external_id,
            tasks_dir=tmp_path / "tasks",
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
        )
    )
    request_tool = next(tool for tool in tools if tool.name == "request_assistant_subscription")

    result = asyncio.run(request_tool.handler({}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload["status"] == "testing_mode_authorized"
    assert db.get_latest_billing_order(tenant.id, "assistant_subscription") is None


def test_register_scheduler_trigger_and_list(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
        )
    )
    register_tool = next(tool for tool in tools if tool.name == "register_scheduler_trigger")
    list_tool = next(tool for tool in tools if tool.name == "list_scheduler_triggers")

    register_result = asyncio.run(
        register_tool.handler(
            {
                "trigger_id": "hourly-healthcheck",
                "trigger_type": "cron",
                "cron": "0 * * * *",
                "payload": {"summary": "hourly check"},
            }
        )
    )
    register_payload = json.loads(register_result["content"][0]["text"])
    list_result = asyncio.run(list_tool.handler({}))
    list_payload = json.loads(list_result["content"][0]["text"])

    assert register_payload["ok"] is True
    assert register_payload["trigger_type"] == "cron"
    assert list_payload["ok"] is True
    assert list_payload["count"] >= 1
    assert any(item.get("id") == "hourly-healthcheck" for item in list_payload["triggers"])


def test_unregister_scheduler_trigger(tmp_path):
    db = build_test_db()
    tenant = create_test_tenant(db)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    tools = build_chat_tools(
        ChatToolContext(
            messenger=FakeMessenger(),
            tenant_external_id=tenant.external_id,
            tasks_dir=tasks_dir,
            role="interaction",
            db=db,
            tenant_id=tenant.id,
            provider=tenant.provider,
        )
    )
    register_tool = next(tool for tool in tools if tool.name == "register_scheduler_trigger")
    unregister_tool = next(tool for tool in tools if tool.name == "unregister_scheduler_trigger")
    list_tool = next(tool for tool in tools if tool.name == "list_scheduler_triggers")

    asyncio.run(
        register_tool.handler(
            {
                "trigger_id": "remove-me",
                "trigger_type": "cron",
                "cron": "*/5 * * * *",
            }
        )
    )
    unregister_result = asyncio.run(unregister_tool.handler({"trigger_id": "remove-me"}))
    unregister_payload = json.loads(unregister_result["content"][0]["text"])
    list_result = asyncio.run(list_tool.handler({}))
    list_payload = json.loads(list_result["content"][0]["text"])

    assert unregister_payload["ok"] is True
    assert unregister_payload["removed"] is True
    assert list_payload["ok"] is True
    assert not any(item.get("id") == "remove-me" for item in list_payload["triggers"])

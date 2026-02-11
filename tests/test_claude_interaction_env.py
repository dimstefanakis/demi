import pytest

import demi.agent.claude as claude_module
from demi.agent.claude import ClaudeAgent
from demi.config import Settings
from demi.models import NormalizedMessage
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


class DummyMessenger:
    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        del tenant_external_id, text, reply_to_message_id
        return None


def test_execution_thinking_max_tokens_sanitized():
    assert Settings(execution_max_thinking_tokens=2048).execution_thinking_max_tokens() == 2048
    assert Settings(execution_max_thinking_tokens=512).execution_thinking_max_tokens() == 1024
    assert Settings(execution_max_thinking_tokens=0).execution_thinking_max_tokens() is None


def test_base_agent_env_includes_env_file_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=test-anthropic-key",
                "VERCEL_TOKEN=test-vercel-token",
                "CUSTOM_AGENT_VAR=custom-value",
                "AGENT_EMAIL=demi@example.com",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    settings = Settings(root_dir=tmp_path)
    memory_path = tmp_path / "memory.md"
    memory_path.write_text("# memory\n", encoding="utf-8")
    workspace = type("WorkspaceStub", (), {"memory_path": memory_path})()

    env = ClaudeAgent._base_agent_env(settings=settings, workspace=workspace)

    assert env.get("ANTHROPIC_API_KEY") == "test-anthropic-key"
    assert env.get("VERCEL_TOKEN") == "test-vercel-token"
    assert env.get("CUSTOM_AGENT_VAR") == "custom-value"
    assert env.get("AGENT_EMAIL") == "demi@example.com"
    assert env.get("GIT_AUTHOR_EMAIL") == "demi@example.com"
    assert env.get("GIT_COMMITTER_EMAIL") == "demi@example.com"


@pytest.mark.asyncio
async def test_interaction_env_includes_tool_search_memory_and_tenant_home(tmp_path, monkeypatch):
    capture: dict = {}

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            self.stop_reason = "end_turn"
            self.subtype = "success"
            self.result = "ok"
            self.total_cost_usd = 0.01
            self.usage = {"output_tokens": 1}
            self.session_id = "interaction-session-123"

    class FakeClient:
        def __init__(self, options):
            capture["options"] = options

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "interaction-session-123"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    agent = ClaudeAgent()

    await agent.send_interaction_instruction(
        workspace=workspace,
        instruction="Send a short status update.",
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
        provider=tenant.provider,
        tenant_external_id=tenant.external_id,
        message_id=42,
    )

    options = capture.get("options")
    assert options is not None
    assert options.env.get("ENABLE_TOOL_SEARCH") == "1"
    assert options.env.get("CLAUDE_CODE_ENABLE_MEMORY_TOOL") == "true"
    assert options.env.get("CLAUDE_CODE_MEMORY_FILE_PATH") == str(workspace.memory_path)
    assert str(options.env.get("HOME") or "").endswith(f"/tenant-{tenant.id}")


@pytest.mark.asyncio
async def test_prepare_context_sets_execution_max_thinking_tokens(
    tmp_path, monkeypatch
):
    capture: dict = {}

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            self.stop_reason = "end_turn"
            self.subtype = "success"
            self.result = "ok"
            self.total_cost_usd = 0.01
            self.usage = {"output_tokens": 1}
            self.session_id = "execution-session-123"

    class FakeClient:
        def __init__(self, options):
            capture["options"] = options

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "execution-session-123"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    def _fake_tooling_bootstrap(*, tenant_root, settings):
        del tenant_root, settings
        return type("ToolingResult", (), {"bin_path": None})()

    monkeypatch.setenv("EXECUTION_MAX_THINKING_TOKENS", "3072")
    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(claude_module, "bootstrap_tenant_tooling", _fake_tooling_bootstrap)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.tasks_dir / "task.md"
    task_path.write_text("# task\n", encoding="utf-8")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="exec-thinking-1",
        tenant_external_id=tenant.external_id,
        received_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        text="implement feature x",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
    )

    options = capture.get("options")
    assert options is not None
    assert options.max_thinking_tokens == 3072


@pytest.mark.asyncio
async def test_route_interaction_retries_until_valid_output(tmp_path, monkeypatch):
    captured_options = []
    query_calls = {"count": 0}
    result_calls = {"count": 0}

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            result_calls["count"] += 1
            self.stop_reason = "end_turn"
            self.subtype = "success"
            if result_calls["count"] == 1:
                self.result = "not valid json"
                self.structured_output = None
            else:
                self.result = '{"should_run": true, "billing_check": false, "billing_checked": true}'
                self.structured_output = {
                    "should_run": True,
                    "billing_check": False,
                    "billing_checked": True,
                }
            self.total_cost_usd = 0.01
            self.usage = {"output_tokens": 1}
            self.session_id = "interaction-session-456"

    class FakeClient:
        def __init__(self, options):
            captured_options.append(options)

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            query_calls["count"] += 1
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "interaction-session-456"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="route-invalid-1",
        tenant_external_id=tenant.external_id,
        received_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        text="build me a simple page",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    result = await agent.route_interaction(
        workspace=workspace,
        message=message,
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
        provider=tenant.provider,
        tenant_external_id=tenant.external_id,
        billing_checked=False,
    )

    assert captured_options
    assert isinstance(captured_options[0].output_format, dict)
    assert query_calls["count"] == 2
    assert result.decision["should_run"] is True
    assert result.decision["billing_check"] is False
    assert result.decision["billing_checked"] is True


@pytest.mark.asyncio
async def test_route_interaction_raises_after_retry_exhaustion(
    tmp_path, monkeypatch
):
    query_calls = {"count": 0}
    result_calls = {"count": 0}

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            result_calls["count"] += 1
            self.stop_reason = "end_turn"
            self.subtype = "success"
            self.result = f"still invalid {result_calls['count']}"
            self.structured_output = None
            self.total_cost_usd = 0.01
            self.usage = {"output_tokens": 1}
            self.session_id = "interaction-session-789"

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            query_calls["count"] += 1
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "interaction-session-789"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="route-invalid-2",
        tenant_external_id=tenant.external_id,
        received_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        text="ship the update",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    monkeypatch.setenv("INTERACTION_AGENT_ROUTING_MAX_RETRIES", "2")
    with pytest.raises(RuntimeError, match="interaction_agent_invalid_output"):
        await agent.route_interaction(
            workspace=workspace,
            message=message,
            messenger=DummyMessenger(),
            tenant_id=tenant.id,
            db=db,
            provider=tenant.provider,
            tenant_external_id=tenant.external_id,
            billing_checked=True,
        )
    # Initial attempt + 2 retries
    assert query_calls["count"] == 3


@pytest.mark.asyncio
async def test_route_interaction_aborts_on_repeated_invalid_output(tmp_path, monkeypatch):
    query_calls = {"count": 0}

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            self.stop_reason = "end_turn"
            self.subtype = "success"
            self.result = "API Error: Unable to connect to API (ECONNRESET)"
            self.structured_output = None
            self.total_cost_usd = 0.0
            self.usage = {"output_tokens": 0}
            self.session_id = "interaction-session-repeat-invalid"

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            query_calls["count"] += 1
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "interaction-session-repeat-invalid"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="route-invalid-repeated",
        tenant_external_id=tenant.external_id,
        received_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        text="status?",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    monkeypatch.setenv("INTERACTION_AGENT_ROUTING_MAX_RETRIES", "8")
    with pytest.raises(RuntimeError, match="interaction_agent_invalid_output"):
        await agent.route_interaction(
            workspace=workspace,
            message=message,
            messenger=DummyMessenger(),
            tenant_id=tenant.id,
            db=db,
            provider=tenant.provider,
            tenant_external_id=tenant.external_id,
            billing_checked=True,
        )
    # First invalid output + one retry, then abort due to repeated invalid output.
    assert query_calls["count"] == 2


@pytest.mark.asyncio
async def test_route_interaction_aborts_on_retry_cost_budget(tmp_path, monkeypatch):
    query_calls = {"count": 0}
    result_calls = {"count": 0}

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            result_calls["count"] += 1
            self.stop_reason = "end_turn"
            self.subtype = "success"
            self.result = f"invalid output {result_calls['count']}"
            self.structured_output = None
            self.total_cost_usd = 0.08
            self.usage = {"output_tokens": 0}
            self.session_id = "interaction-session-retry-cost"

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            query_calls["count"] += 1
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "interaction-session-retry-cost"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="route-invalid-cost-budget",
        tenant_external_id=tenant.external_id,
        received_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        text="status?",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    monkeypatch.setenv("INTERACTION_AGENT_ROUTING_MAX_RETRIES", "8")
    monkeypatch.setenv("INTERACTION_AGENT_ROUTING_MAX_COST_USD", "0.1")
    with pytest.raises(RuntimeError, match="interaction_agent_invalid_output"):
        await agent.route_interaction(
            workspace=workspace,
            message=message,
            messenger=DummyMessenger(),
            tenant_id=tenant.id,
            db=db,
            provider=tenant.provider,
            tenant_external_id=tenant.external_id,
            billing_checked=True,
        )
    # Attempt1 (0.08) continues; attempt2 pushes cumulative cost above 0.1 and aborts.
    assert query_calls["count"] == 2

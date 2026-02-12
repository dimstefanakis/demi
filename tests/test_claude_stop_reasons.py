import asyncio
from datetime import datetime, timezone

import pytest

import demi.agent.claude as claude_module
from demi.agent.claude import ClaudeAgent
from demi.agent.inflight import InflightTextStream
from demi.models import NormalizedMessage
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant


class DummyMessenger:
    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        del tenant_external_id, text, reply_to_message_id
        return None


def _patch_sdk(
    monkeypatch,
    *,
    stop_reason: str | None,
    subtype: str = "success",
    result: str = "ok",
    capture: dict | None = None,
):
    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self):
            self.stop_reason = stop_reason
            self.subtype = subtype
            self.result = result
            self.total_cost_usd = 0.01
            self.usage = {"output_tokens": 1}
            self.session_id = "session-123"

    class FakeClient:
        def __init__(self, options):
            self.options = options
            if capture is not None:
                capture["options"] = options

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            async for _ in prompt_stream:
                break
            return None

        async def receive_messages(self):
            yield FakeSystemMessage("init", {"session_id": "session-123"})
            yield FakeResultMessage()

        async def receive_response(self):
            async for msg in self.receive_messages():
                yield msg

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)


def _tenant_stop_reason_events(db, tenant_id: int) -> list[dict]:
    return list(
        db._execute(
            db._table("tenant_events")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("event_type", "agent_stop_reason")
            .order("received_at", desc=False)
        )
        or []
    )


def _tenant_usage_events(db, tenant_id: int) -> list[dict]:
    return list(
        db._execute(
            db._table("tenant_events")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("event_type", "agent_usage")
            .order("received_at", desc=False)
        )
        or []
    )


@pytest.mark.asyncio
async def test_prepare_context_records_stop_reason_event_for_end_turn(tmp_path, monkeypatch):
    _patch_sdk(monkeypatch, stop_reason="end_turn")
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="stop-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    result = await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
        run_id=111,
    )

    assert result.stop_reason == "end_turn"
    assert result.result_subtype == "success"
    events = _tenant_stop_reason_events(db, tenant.id)
    assert events
    payload = events[-1]["payload_json"]
    assert payload["context"] == "prepare_context"
    assert payload["stop_reason"] == "end_turn"
    assert payload["status"] == "completed"
    assert payload["run_id"] == 111


@pytest.mark.asyncio
async def test_prepare_context_records_stop_sequence_as_completed(tmp_path, monkeypatch):
    _patch_sdk(monkeypatch, stop_reason="stop_sequence")
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="stop-seq-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    result = await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
        run_id=333,
    )

    assert result.stop_reason == "stop_sequence"
    assert result.result_subtype == "success"
    events = _tenant_stop_reason_events(db, tenant.id)
    assert events
    payload = events[-1]["payload_json"]
    assert payload["context"] == "prepare_context"
    assert payload["stop_reason"] == "stop_sequence"
    assert payload["status"] == "completed"
    assert payload["run_id"] == 333


@pytest.mark.asyncio
async def test_prepare_context_fails_on_max_tokens_and_records_event(tmp_path, monkeypatch):
    _patch_sdk(monkeypatch, stop_reason="max_tokens")
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="stop-2",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    with pytest.raises(RuntimeError, match="agent_stop_reason_max_tokens"):
        await agent.prepare_context(
            workspace=workspace,
            task_path=task_path,
            message=message,
            messenger=DummyMessenger(),
            tenant_id=tenant.id,
            db=db,
            run_id=222,
        )

    events = _tenant_stop_reason_events(db, tenant.id)
    assert events
    payload = events[-1]["payload_json"]
    assert payload["context"] == "prepare_context"
    assert payload["stop_reason"] == "max_tokens"
    assert payload["status"] == "incomplete"
    assert payload["run_id"] == 222


@pytest.mark.asyncio
async def test_prepare_context_fails_on_error_subtype_even_with_end_turn(tmp_path, monkeypatch):
    _patch_sdk(monkeypatch, stop_reason="end_turn", subtype="error_max_turns")
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="stop-err-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    with pytest.raises(RuntimeError, match="agent_result_subtype_error_max_turns"):
        await agent.prepare_context(
            workspace=workspace,
            task_path=task_path,
            message=message,
            messenger=DummyMessenger(),
            tenant_id=tenant.id,
            db=db,
            run_id=444,
        )

    events = _tenant_stop_reason_events(db, tenant.id)
    assert events
    payload = events[-1]["payload_json"]
    assert payload["context"] == "prepare_context"
    assert payload["stop_reason"] == "end_turn"
    assert payload["result_subtype"] == "error_max_turns"
    assert payload["status"] == "error"
    assert payload["run_id"] == 444


@pytest.mark.asyncio
async def test_prepare_context_sets_tool_search_memory_env_and_records_usage(
    tmp_path, monkeypatch
):
    capture: dict = {}
    _patch_sdk(monkeypatch, stop_reason="end_turn", capture=capture)
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="stop-env-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
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
        run_id=555,
        runtime_env={"GITHUB_TOKEN": "ghs_runtime_token"},
    )

    options = capture.get("options")
    assert options is not None
    assert options.env.get("ENABLE_TOOL_SEARCH") == "1"
    assert options.env.get("CLAUDE_CODE_ENABLE_MEMORY_TOOL") == "true"
    assert options.env.get("CLAUDE_CODE_MEMORY_FILE_PATH") == str(workspace.memory_path)
    assert options.env.get("GITHUB_TOKEN") == "ghs_runtime_token"

    events = _tenant_usage_events(db, tenant.id)
    assert events
    payload = events[-1]["payload_json"]
    assert payload["context"] == "prepare_context"
    assert payload["total_cost_usd"] == pytest.approx(0.01)
    assert payload["run_id"] == 555
    usage = payload["usage"] or {}
    assert usage["output_tokens"] == 1


@pytest.mark.asyncio
async def test_prepare_context_resume_fallback_reenables_inflight_stream(tmp_path, monkeypatch):
    capture: dict[str, list] = {
        "accepting_at_query_start": [],
        "options": [],
        "resume_options": [],
    }

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
            self.session_id = "session-fresh"

    stream = InflightTextStream(queue=asyncio.Queue())

    class FakeClient:
        def __init__(self, options):
            self.options = options
            capture["options"].append(options)
            capture["resume_options"].append(options.resume)

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            capture["accepting_at_query_start"].append(stream.accepting)
            async for _ in prompt_stream:
                pass
            return None

        async def receive_response(self):
            if self.options.resume:
                raise RuntimeError("invalid session")
            yield FakeSystemMessage("init", {"session_id": "session-fresh"})
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="resume-fallback-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    result = await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=DummyMessenger(),
        inflight_stream=stream,
        tenant_id=tenant.id,
        db=db,
        run_id=777,
        session_id="session-stale",
    )

    assert result.session_id == "session-fresh"
    assert capture["resume_options"][:2] == ["session-stale", None]
    assert capture["accepting_at_query_start"][:2] == [True, True]
    expected_home = str(workspace.tenant_root / ".execution_home")
    assert all(str(options.env.get("HOME")) == expected_home for options in capture["options"])


@pytest.mark.asyncio
async def test_prepare_context_resume_fallback_on_process_error(tmp_path, monkeypatch):
    capture: dict[str, list] = {
        "options": [],
        "resume_options": [],
    }

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
            self.session_id = "session-fresh"

    class FakeClient:
        def __init__(self, options):
            self.options = options
            capture["options"].append(options)
            capture["resume_options"].append(options.resume)

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            async for _ in prompt_stream:
                pass
            return None

        async def receive_response(self):
            if self.options.resume:
                from claude_agent_sdk._errors import ProcessError

                raise ProcessError(
                    "Command failed with exit code 1",
                    exit_code=1,
                    stderr="Check stderr output for details",
                )
            yield FakeSystemMessage("init", {"session_id": "session-fresh"})
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="resume-fallback-process-error-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    result = await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
        run_id=778,
        session_id="session-stale",
    )

    assert result.session_id == "session-fresh"
    assert capture["resume_options"][:2] == ["session-stale", None]
    expected_home = str(workspace.tenant_root / ".execution_home")
    assert all(str(options.env.get("HOME")) == expected_home for options in capture["options"])


@pytest.mark.asyncio
async def test_prepare_context_retries_once_on_error_during_execution(tmp_path, monkeypatch):
    capture: dict[str, list] = {
        "resume_options": [],
    }

    class FakeSystemMessage:
        def __init__(self, subtype: str, data: dict | None = None):
            self.subtype = subtype
            self.data = data or {}

    class FakeResultMessage:
        def __init__(self, *, subtype: str, session_id: str):
            self.stop_reason = "end_turn"
            self.subtype = subtype
            self.result = "ok"
            self.total_cost_usd = 0.01
            self.usage = {"output_tokens": 1}
            self.session_id = session_id

    class FakeClient:
        calls = 0

        def __init__(self, options):
            self.options = options
            capture["resume_options"].append(options.resume)

        async def connect(self):
            return None

        async def query(self, prompt_stream, session_id=None):
            del session_id
            async for _ in prompt_stream:
                pass
            return None

        async def receive_response(self):
            FakeClient.calls += 1
            if FakeClient.calls == 1:
                yield FakeSystemMessage("init", {"session_id": "session-error"})
                yield FakeResultMessage(
                    subtype="error_during_execution",
                    session_id="session-error",
                )
                return
            yield FakeSystemMessage("init", {"session_id": "session-recovered"})
            yield FakeResultMessage(
                subtype="success",
                session_id="session-recovered",
            )

        async def disconnect(self):
            return None

    monkeypatch.setattr(claude_module, "SystemMessage", FakeSystemMessage)
    monkeypatch.setattr(claude_module, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClient)

    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    task_path = workspace.write_task("# Task\n\nDo work\n")
    message = NormalizedMessage(
        provider=tenant.provider,
        provider_message_id="retry-error-during-execution-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build a site",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    agent = ClaudeAgent()

    result = await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=DummyMessenger(),
        tenant_id=tenant.id,
        db=db,
        run_id=888,
        session_id="session-stale",
    )

    assert result.session_id == "session-recovered"
    assert result.result_subtype == "success"
    assert capture["resume_options"][:2] == ["session-stale", None]

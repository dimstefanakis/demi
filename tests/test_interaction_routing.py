import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from demi.models import Attachment, NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, unique_external_id


class FakeAgent:
    def __init__(self, decision):
        self.decision = decision
        self.route_calls = []
        self.prepare_calls = []

    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        self.route_calls.append(
            {
                "workspace": workspace,
                "message": message,
                "tenant_id": tenant_id,
                "session_id": session_id,
            }
        )
        return self.decision

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
        execution_context=None,
    ):
        self.prepare_calls.append((workspace.root, task_path, message))
        return type(
            "AgentResult",
            (),
            {"session_id": session_id, "summary": "ok", "total_cost_usd": None, "usage": None},
        )()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        self.sent.append((tenant_external_id, text))


class BlockingRouteAgent(FakeAgent):
    def __init__(self, decision):
        super().__init__(decision)
        import asyncio

        self._release = asyncio.Event()

    def release(self) -> None:
        self._release.set()

    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        self.route_calls.append(
            {
                "workspace": workspace,
                "message": message,
                "tenant_id": tenant_id,
                "session_id": session_id,
            }
        )
        await self._release.wait()
        return self.decision


class BlockingFailRouteAgent(BlockingRouteAgent):
    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        self.route_calls.append(
            {
                "workspace": workspace,
                "message": message,
                "tenant_id": tenant_id,
                "session_id": session_id,
            }
        )
        await self._release.wait()
        raise RuntimeError("routing_failed_after_stream")


class BillingRecheckBlockingAgent(FakeAgent):
    def __init__(self):
        super().__init__({})
        self._release = asyncio.Event()

    def release(self) -> None:
        self._release.set()

    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        self.route_calls.append(
            {
                "workspace": workspace,
                "message": message,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "billing_checked": billing_checked,
            }
        )
        if not billing_checked:
            await self._release.wait()
            return {
                "ok": True,
                "should_run": False,
                "queue_run": False,
                "dedupe": True,
                "ask_questions": [],
                "purpose": "chat",
                "plan": None,
                "billing_check": True,
                "billing_checked": False,
            }
        return {
            "ok": True,
            "should_run": False,
            "queue_run": False,
            "dedupe": True,
            "ask_questions": [],
            "purpose": "chat",
            "plan": None,
            "billing_check": False,
            "billing_checked": True,
        }


class RouteErrorAgent(FakeAgent):
    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        raise RuntimeError("routing_failed")


class SessionResultRouteAgent(FakeAgent):
    def __init__(self, decision, returned_session_id: str):
        super().__init__(decision)
        self.returned_session_id = returned_session_id

    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        message_id=None,
        billing_checked=False,
        asset_paths=None,
        execution_bridge=None,
    ):
        self.route_calls.append(
            {
                "workspace": workspace,
                "message": message,
                "tenant_id": tenant_id,
                "session_id": session_id,
            }
        )
        return type(
            "InteractionRouteResult",
            (),
            {
                "decision": self.decision,
                "session_id": self.returned_session_id,
                "total_cost_usd": None,
                "usage": None,
            },
        )()


class InstructionSessionAgent(FakeAgent):
    def __init__(self, decision):
        super().__init__(decision)
        self.instruction_calls: list[str | None] = []

    async def send_interaction_instruction(
        self,
        *,
        workspace,
        instruction,
        messenger=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=None,
        provider=None,
        tenant_external_id=None,
        run_id=None,
        message_id=None,
        asset_paths=None,
        execution_bridge=None,
    ):
        del (
            workspace,
            instruction,
            messenger,
            tenant_id,
            db,
            payments,
            provider,
            tenant_external_id,
            run_id,
            message_id,
            asset_paths,
            execution_bridge,
        )
        self.instruction_calls.append(session_id)
        return type(
            "AgentResult",
            (),
            {
                "session_id": "interaction-session-instruction-next",
                "summary": "ok",
                "total_cost_usd": None,
                "usage": None,
            },
        )()


class MergeFreezeOrchestrator(Orchestrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.merge_accepting_flags: list[bool] = []

    def _merge_interaction_session_messages(
        self,
        *,
        batched_messages,
        session,
    ):
        if session is not None:
            self.merge_accepting_flags.append(bool(session.stream.accepting))
        return super()._merge_interaction_session_messages(
            batched_messages=batched_messages,
            session=session,
        )


@pytest.mark.asyncio
async def test_interaction_decision_can_skip_run(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": ["What should the headline say?"],
        "purpose": "clarify",
        "plan": None,
    }
    agent = FakeAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Can you make it premium?",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.route_calls
    assert not agent.prepare_calls

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    runs = db.list_recent_runs(tenant.id, project_name=None, limit=5)
    assert len(runs) == 0
    row = db.get_message_by_provider_id(tenant.id, "i-1")
    assert row is not None
    assert row["status"] == "processed"


@pytest.mark.asyncio
async def test_interaction_decision_creates_run(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": True,
        "queue_run": False,
        "dedupe": False,
        "ask_questions": [],
        "purpose": "build",
        "plan": "ship a first draft",
    }
    agent = FakeAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build me a landing page",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.prepare_calls
    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    runs = db.list_recent_runs(tenant.id, project_name=None, limit=1)
    assert runs
    run_id = int(runs[0]["id"])
    run = db.get_run(run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_summary"] == "ok"
    decision_payload = run["interaction_decision_json"]
    assert decision_payload["purpose"] == "build"


@pytest.mark.asyncio
async def test_interaction_routing_uses_interaction_session_state(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
    }
    agent = FakeAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    tenant = db.get_or_create_tenant("telegram", tenant_external_id)
    db.update_tenant_session(tenant.id, "execution-session-should-not-be-used")
    db.set_tenant_kv(
        tenant.id,
        "interaction",
        "claude_session",
        {"session_id": "interaction-session-1"},
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-route-session-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="quick question",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.route_calls
    assert agent.route_calls[-1]["session_id"] == "interaction-session-1"


@pytest.mark.asyncio
async def test_interaction_routing_persists_returned_session_id(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
    }
    agent = SessionResultRouteAgent(decision, returned_session_id="interaction-session-2")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-route-session-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="another question",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    payload = db.get_tenant_kv(tenant.id, "interaction", "claude_session") or {}
    assert payload.get("session_id") == "interaction-session-2"


@pytest.mark.asyncio
async def test_instruction_and_route_share_unified_session(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
    }
    agent = InstructionSessionAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    tenant = db.get_or_create_tenant("telegram", tenant_external_id)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    db.set_tenant_kv(
        tenant.id,
        "interaction",
        "claude_session",
        {"session_id": "interaction-session-prev"},
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="i-instruction-session-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="hello",
        images=[],
        raw={},
    )

    sent = await orchestrator._send_interaction_instruction(
        workspace=workspace,
        tenant=tenant,
        msg=msg,
        instruction="test instruction",
        run_id=None,
        message_id=None,
        asset_paths=None,
    )

    assert sent is True
    # Instruction call receives the same unified session used by routing
    assert agent.instruction_calls == ["interaction-session-prev"]
    # Updated session is stored under the unified key, available for next route call
    payload = db.get_tenant_kv(tenant.id, "interaction", "claude_session") or {}
    assert payload.get("session_id") == "interaction-session-instruction-next"


@pytest.mark.asyncio
async def test_interaction_streams_messages_arriving_mid_route(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
        "purpose": "chat",
        "plan": None,
    }
    agent = BlockingRouteAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )
    tenant_external_id = unique_external_id("tenant")
    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="stream-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Hey buddy",
        images=[],
        raw={},
    )
    second = NormalizedMessage(
        provider="telegram",
        provider_message_id="stream-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="More context while routing",
        images=[],
        raw={},
    )

    first_task = asyncio.create_task(orchestrator.handle_message(first))
    await asyncio.sleep(0.05)
    second_task = asyncio.create_task(orchestrator.handle_message(second))
    await asyncio.sleep(0.05)
    agent.release()
    first_result = await first_task
    second_result = await second_task

    assert first_result.status == "accepted"
    assert second_result.status == "accepted"
    assert len(agent.route_calls) == 1

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    first_row = db.get_message_by_provider_id(tenant.id, "stream-1")
    second_row = db.get_message_by_provider_id(tenant.id, "stream-2")
    assert first_row is not None
    assert second_row is not None
    assert first_row["status"] == "processed"
    assert second_row["status"] == "processed"


@pytest.mark.asyncio
async def test_interaction_marks_seed_message_processing_while_routing(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
        "purpose": "chat",
        "plan": None,
    }
    agent = BlockingRouteAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )
    tenant_external_id = unique_external_id("tenant")
    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="processing-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Please process this",
        images=[],
        raw={},
    )

    first_task = asyncio.create_task(orchestrator.handle_message(first))
    await asyncio.sleep(0.05)

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    first_row = db.get_message_by_provider_id(tenant.id, "processing-1")
    assert first_row is not None
    assert first_row["status"] == "processing"

    agent.release()
    result = await first_task
    assert result.status == "accepted"

    first_row = db.get_message_by_provider_id(tenant.id, "processing-1")
    assert first_row is not None
    assert first_row["status"] == "processed"


@pytest.mark.asyncio
async def test_interaction_does_not_batch_other_received_messages(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
        "purpose": "chat",
        "plan": None,
    }
    agent = FakeAgent(decision)
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    tenant = db.get_or_create_tenant("telegram", tenant_external_id)
    older = NormalizedMessage(
        provider="telegram",
        provider_message_id="old-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Older pending input",
        images=[],
        raw={},
    )
    db.record_message(tenant.id, older)

    newer = NormalizedMessage(
        provider="telegram",
        provider_message_id="old-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Newest message only",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(newer)

    assert result.status == "accepted"
    assert len(agent.route_calls) == 1
    routed = agent.route_calls[0]["message"]
    assert routed.text == "Newest message only"

    older_row = db.get_message_by_provider_id(tenant.id, "old-1")
    newer_row = db.get_message_by_provider_id(tenant.id, "old-2")
    assert older_row is not None
    assert newer_row is not None
    assert older_row["status"] == "received"
    assert newer_row["status"] == "processed"


@pytest.mark.asyncio
async def test_interaction_session_closes_when_routing_raises(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=RouteErrorAgent({}),
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="err-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Trigger router error",
        images=[],
        raw={},
    )

    with pytest.raises(RuntimeError, match="routing_failed"):
        await orchestrator.handle_message(msg)

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    assert orchestrator._interaction_session_for(tenant.id) is None
    row = db.get_message_by_provider_id(tenant.id, "err-1")
    assert row is not None
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_streamed_message_status_rolls_back_when_routing_fails(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = BlockingFailRouteAgent({})
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )
    tenant_external_id = unique_external_id("tenant")
    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="fail-stream-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Initial request",
        images=[],
        raw={},
    )
    second = NormalizedMessage(
        provider="telegram",
        provider_message_id="fail-stream-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Follow-up while routing",
        images=[],
        raw={},
    )

    first_task = asyncio.create_task(orchestrator.handle_message(first))
    await asyncio.sleep(0.05)
    second_result = await orchestrator.handle_message(second)
    assert second_result.status == "accepted"
    assert second_result.detail == "interaction_streamed"
    await asyncio.sleep(0.05)
    agent.release()
    with pytest.raises(RuntimeError, match="routing_failed_after_stream"):
        await first_task

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    first_row = db.get_message_by_provider_id(tenant.id, "fail-stream-1")
    second_row = db.get_message_by_provider_id(tenant.id, "fail-stream-2")
    assert first_row is not None
    assert second_row is not None
    assert first_row["status"] == "failed"
    assert second_row["status"] == "received"


@pytest.mark.asyncio
async def test_billing_reroute_uses_merged_streamed_message_snapshot(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = BillingRecheckBlockingAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )
    tenant_external_id = unique_external_id("tenant")
    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="bill-stream-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Was thinking about buildin",
        images=[],
        raw={},
    )
    second = NormalizedMessage(
        provider="telegram",
        provider_message_id="bill-stream-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="A tic tac toe game",
        images=[],
        raw={},
    )

    first_task = asyncio.create_task(orchestrator.handle_message(first))
    await asyncio.sleep(0.05)
    second_result = await orchestrator.handle_message(second)
    assert second_result.status == "accepted"
    assert second_result.detail == "interaction_streamed"

    await asyncio.sleep(0.05)
    agent.release()
    first_result = await first_task
    assert first_result.status == "accepted"

    assert len(agent.route_calls) >= 2
    second_call = agent.route_calls[1]
    assert second_call["billing_checked"] is True
    routed = second_call["message"]
    assert routed.provider_message_id == "bill-stream-2"
    assert routed.text == "Was thinking about buildin\nA tic tac toe game"


@pytest.mark.asyncio
async def test_interaction_recomputes_assets_after_merge_and_project_switch(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "beta",
        "should_run": True,
        "queue_run": False,
        "dedupe": False,
        "ask_questions": [],
        "purpose": "build",
        "plan": "ship",
    }
    agent = BlockingRouteAgent(decision)

    class DownloadMessenger(FakeMessenger):
        def __init__(self):
            super().__init__()
            self.download_calls: list[tuple[str, list[str]]] = []

        async def download_images(self, images, assets_dir):
            assets_dir.mkdir(parents=True, exist_ok=True)
            paths: list[str] = []
            file_ids: list[str] = []
            for image in images:
                file_id = str(image.provider_file_id)
                file_ids.append(file_id)
                path = assets_dir / f"{file_id}.txt"
                path.write_text(file_id)
                paths.append(str(path))
            self.download_calls.append((str(assets_dir), file_ids))
            return paths

    messenger = DownloadMessenger()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
    )
    tenant_external_id = unique_external_id("tenant")

    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="asset-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="First request",
        images=[Attachment(provider_file_id="img-first")],
        raw={},
    )
    second = NormalizedMessage(
        provider="telegram",
        provider_message_id="asset-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text=None,
        images=[Attachment(provider_file_id="img-second")],
        raw={},
    )

    first_task = asyncio.create_task(orchestrator.handle_message(first))
    await asyncio.sleep(0.05)
    second_result = await orchestrator.handle_message(second)
    await asyncio.sleep(0.05)
    agent.release()
    first_result = await first_task

    assert first_result.status == "accepted"
    assert second_result.status == "accepted"
    assert second_result.detail == "interaction_streamed"
    assert agent.prepare_calls

    task_path = Path(agent.prepare_calls[-1][1])
    task_content = task_path.read_text()
    assert "img-first" in task_content
    assert "img-second" in task_content
    assert "assets/img-first.txt" in task_content
    assert "assets/img-second.txt" in task_content

    assert any(
        assets_dir.endswith("/assets") and {"img-first", "img-second"} <= set(file_ids)
        for assets_dir, file_ids in messenger.download_calls
    )


@pytest.mark.asyncio
async def test_interaction_transcribes_voice_message_before_routing(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "reply_sent": True,
        "ask_questions": [],
        "purpose": "chat",
    }
    agent = FakeAgent(decision)

    class VoiceMessenger(FakeMessenger):
        def __init__(self):
            super().__init__()
            self.download_calls: list[str] = []

        async def download_images(self, images, assets_dir):
            assets_dir.mkdir(parents=True, exist_ok=True)
            paths: list[str] = []
            for image in images:
                file_id = str(image.provider_file_id)
                self.download_calls.append(file_id)
                path = assets_dir / f"{file_id}.ogg"
                path.write_bytes(b"voice-bytes")
                paths.append(str(path))
            return paths

    class FakeSpeechToText:
        def __init__(self):
            self.paths: list[Path] = []

        async def transcribe_file(self, file_path: Path):
            self.paths.append(file_path)
            return "build me a bakery website"

    messenger = VoiceMessenger()
    speech_to_text = FakeSpeechToText()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
        speech_to_text=speech_to_text,
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="voice-turn-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="(voice message)",
        images=[Attachment(provider_file_id="voice-file-1")],
        raw={
            "message": {
                "voice": {
                    "file_id": "voice-file-1",
                    "mime_type": "audio/ogg",
                }
            }
        },
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert result.detail == "no_run"
    assert messenger.download_calls
    assert messenger.download_calls[0] == "voice-file-1"
    assert messenger.download_calls.count("voice-file-1") == 1
    assert len(speech_to_text.paths) == 1
    assert agent.route_calls
    assert agent.route_calls[0]["message"].text == "build me a bakery website"
    assert agent.route_calls[0]["message"].images == []

    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    message_row = db.get_message_by_provider_id(int(tenant.id), "voice-turn-1")
    assert message_row is not None
    assert message_row.get("text") == "build me a bakery website"


@pytest.mark.asyncio
async def test_interaction_freezes_stream_before_merge_snapshot(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    decision = {
        "ok": True,
        "project_name": "main",
        "should_run": False,
        "queue_run": False,
        "dedupe": True,
        "ask_questions": [],
        "purpose": "chat",
        "plan": None,
    }
    agent = BlockingRouteAgent(decision)
    orchestrator = MergeFreezeOrchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )
    tenant_external_id = unique_external_id("tenant")
    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="freeze-1",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
    )
    second = NormalizedMessage(
        provider="telegram",
        provider_message_id="freeze-2",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="More context",
        images=[],
        raw={},
    )

    first_task = asyncio.create_task(orchestrator.handle_message(first))
    await asyncio.sleep(0.05)
    second_task = asyncio.create_task(orchestrator.handle_message(second))
    await asyncio.sleep(0.05)
    agent.release()
    first_result = await first_task
    second_result = await second_task

    assert first_result.status == "accepted"
    assert second_result.status == "accepted"
    assert orchestrator.merge_accepting_flags
    assert all(flag is False for flag in orchestrator.merge_accepting_flags)

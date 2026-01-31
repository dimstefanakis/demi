from datetime import datetime, timedelta, timezone
import json

import pytest

from claudius.db.core import Database
from claudius.models import NormalizedMessage
from claudius.orchestrator import Orchestrator
from claudius.workspace.core import WorkspaceManager


class FakeAgent:
    def __init__(self, deploy_url="https://example.com/site"):
        self.calls = []
        self.deploy_url = deploy_url

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
        self.calls.append((workspace.root, task_path, session_id))
        if db is not None and tenant_id is not None:
            db.update_tenant_deploy_url(tenant_id, self.deploy_url)
        if messenger is not None:
            await messenger.send_text(
                message.tenant_external_id, f"Your site is live: {self.deploy_url}"
            )
        (workspace.tasks_dir / "result_summary.md").write_text("ok")
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text):
        self.sent.append((tenant_external_id, text))


@pytest.mark.asyncio
async def test_orchestrator_new_site_flow(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="51",
        tenant_external_id="987654",
        received_at=datetime.now(tz=timezone.utc),
        text="Build me a barber site",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    tenant = db.get_or_create_tenant("telegram", "987654")
    assert tenant.last_deploy_url == "https://example.com/site"
    assert "https://example.com/site" in orchestrator.messenger.sent[0][1]


@pytest.mark.asyncio
async def test_orchestrator_reconciles_run_result(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "111")
    workspace = workspace_manager.ensure_workspace(tenant.key)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="1",
        tenant_external_id="111",
        received_at=datetime.now(tz=timezone.utc),
        text="Europe",
        images=[],
        raw={},
    )
    message_id, _ = db.record_message(tenant.id, msg)
    db.update_message_status(message_id, "processing")
    run_id = db.create_run(
        tenant.id, message_id=message_id, project_name=workspace.project_name
    )

    result_payload = {
        "session_id": "session-1",
        "summary": "ok",
        "total_cost_usd": 1.23,
        "usage": {"input_tokens": 10},
    }
    (workspace.tasks_dir / "run_result.json").write_text(json.dumps(result_payload))

    new_msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="2",
        tenant_external_id="111",
        received_at=datetime.now(tz=timezone.utc),
        text="Try again",
        images=[],
        raw={},
    )
    result = await orchestrator.handle_message(new_msg)

    assert result.status == "accepted"
    row = db.connect().execute("select status from runs where id = ?", (run_id,)).fetchone()
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_orchestrator_blocks_after_two_hard_failures(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    messenger = FakeMessenger()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=messenger,
    )

    tenant = db.get_or_create_tenant("telegram", "555")
    workspace = workspace_manager.ensure_workspace(tenant.key)

    tool_runs = workspace.tasks_dir / "tool_runs.jsonl"
    tool_runs.write_text(
        "\n".join(
            [
                (
                    '{"timestamp":"2026-01-29T00:00:00+00:00","tool":"provision",'
                    '"result":{"status":"missing_org"}}'
                ),
                (
                    '{"timestamp":"2026-01-29T00:00:01+00:00","tool":"provision",'
                    '"result":{"status":"missing_org"}}'
                ),
            ]
        )
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="x1",
        tenant_external_id="555",
        received_at=datetime.now(tz=timezone.utc),
        text="Status?",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    jobs = db.connect().execute("select * from event_jobs").fetchall()
    assert jobs


@pytest.mark.asyncio
async def test_orchestrator_clears_block_on_retry(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    messenger = FakeMessenger()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=messenger,
    )

    tenant = db.get_or_create_tenant("telegram", "777")
    workspace = workspace_manager.ensure_workspace(tenant.key)

    tool_runs = workspace.tasks_dir / "tool_runs.jsonl"
    tool_runs.write_text(
        "\n".join(
            [
                (
                    '{"timestamp":"2026-01-29T00:00:00+00:00","tool":"provision",'
                    '"result":{"status":"missing_org"}}'
                ),
                (
                    '{"timestamp":"2026-01-29T00:00:01+00:00","tool":"provision",'
                    '"result":{"status":"missing_org"}}'
                ),
            ]
        )
    )

    first = NormalizedMessage(
        provider="telegram",
        provider_message_id="x1",
        tenant_external_id="777",
        received_at=datetime.now(tz=timezone.utc),
        text="Status?",
        images=[],
        raw={},
    )

    first_result = await orchestrator.handle_message(first)
    assert first_result.status == "accepted"

    second = NormalizedMessage(
        provider="telegram",
        provider_message_id="x2",
        tenant_external_id="777",
        received_at=datetime.now(tz=timezone.utc),
        text="Try again",
        images=[],
        raw={},
    )

    second_result = await orchestrator.handle_message(second)

    assert second_result.status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("directive", ["project: beta", "/project beta"])
async def test_orchestrator_allows_parallel_projects(tmp_path, directive):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "333")
    alpha_ws = workspace_manager.ensure_workspace(tenant.key, project_name="alpha")
    beta_ws = workspace_manager.ensure_workspace(tenant.key, project_name="beta")

    db.create_run(tenant.id, message_id=0, project_name=alpha_ws.project_name)

    msg_text = f"{directive}\nUpdate the hero headline"
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="parallel-1",
        tenant_external_id="333",
        received_at=datetime.now(tz=timezone.utc),
        text=msg_text,
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.calls
    assert agent.calls[-1][0] == beta_ws.root


@pytest.mark.asyncio
async def test_orchestrator_updates_request_status_for_pending(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "444")
    workspace = workspace_manager.ensure_workspace(tenant.key)
    db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="pending-1",
        tenant_external_id="444",
        received_at=datetime.now(tz=timezone.utc),
        text="Add a testimonials section",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "busy"
    status_text = (workspace.tasks_dir / "request_status.md").read_text()
    assert "Pending Messages" in status_text
    assert "Add a testimonials section" in status_text


@pytest.mark.asyncio
async def test_orchestrator_expires_stale_run(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "555")
    workspace = workspace_manager.ensure_workspace(tenant.key)
    run_id = db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    db.update_run_lease(
        run_id, lease_expires_at=past.isoformat(), last_heartbeat_at=past.isoformat()
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-1",
        tenant_external_id="555",
        received_at=datetime.now(tz=timezone.utc),
        text="Continue",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    row = db.connect().execute("SELECT status, error FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "lease_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reset_text",
    ["/reset", "/reset now", "/reset@mybot", "/reset@mybot now"],
)
async def test_orchestrator_reset_clears_state(tmp_path, reset_text):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "999")
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    pending_msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="p1",
        tenant_external_id="999",
        received_at=datetime.now(tz=timezone.utc),
        text="pending",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    pending_id, _ = db.record_message(tenant.id, pending_msg)
    db.update_message_status(pending_id, "pending")

    processing_msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="p2",
        tenant_external_id="999",
        received_at=datetime.now(tz=timezone.utc),
        text="processing",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )
    processing_id, _ = db.record_message(tenant.id, processing_msg)
    db.update_message_status(processing_id, "processing")

    db.create_run(tenant.id, message_id=processing_id, project_name=workspace.project_name)

    reset_msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="reset-1",
        tenant_external_id="999",
        received_at=datetime.now(tz=timezone.utc),
        text=reset_text,
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(reset_msg)

    assert result.status == "accepted"
    rows = db.connect().execute(
        "SELECT status FROM messages WHERE id IN (?, ?)",
        (pending_id, processing_id),
    ).fetchall()
    assert all(row["status"] == "processed" for row in rows)
    run = db.connect().execute(
        "SELECT status, error FROM runs WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
        (tenant.id,),
    ).fetchone()
    assert run["status"] == "failed"
    assert run["error"] == "user_reset"


@pytest.mark.asyncio
async def test_orchestrator_infers_project_from_description(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", "222")
    main_ws = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    cafe_ws = workspace_manager.ensure_workspace(tenant.key, project_name="cafe")

    (main_ws.root / "DESCRIPTION.md").write_text("Main brand site for the holding company.")
    (cafe_ws.root / "DESCRIPTION.md").write_text(
        "Cafe project. Coffee menu, pastries, and brunch."
    )

    active_path = main_ws.tenant_root / "projects" / "active.txt"
    active_path.write_text("main\n")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="99",
        tenant_external_id="222",
        received_at=datetime.now(tz=timezone.utc),
        text="Please update the coffee menu prices",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.calls
    used_root = agent.calls[-1][0]
    assert used_root == cafe_ws.root

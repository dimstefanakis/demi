from datetime import datetime, timedelta, timezone
import json

import pytest

import claudius.orchestrator as orchestrator_module
from claudius.db.core import Database
from claudius.domains.github_app import GitHubAppConfig
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
        self.calls.append((workspace.root, task_path, session_id, runtime_env))
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
async def test_orchestrator_passes_github_runtime_env(tmp_path, monkeypatch):
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
    runtime_env_payload = {
        "GITHUB_TOKEN": "ghs_short_lived",
        "GITHUB_REPO_FULL_NAME": "acme/test-repo",
        "GITHUB_REPO_NAME": "test-repo",
    }

    async def _fake_runtime_env(_settings, _workspace, _tenant):
        return runtime_env_payload

    monkeypatch.setattr(orchestrator, "_github_runtime_env", _fake_runtime_env)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="52",
        tenant_external_id="987655",
        received_at=datetime.now(tz=timezone.utc),
        text="Create a marketing site",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.calls
    runtime_env = agent.calls[-1][3]
    assert runtime_env == runtime_env_payload
    assert "GITHUB_APP_PRIVATE_KEY" not in runtime_env


@pytest.mark.asyncio
async def test_github_runtime_env_uses_short_lived_token_only(tmp_path, monkeypatch):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )
    tenant = db.get_or_create_tenant("telegram", "1000")
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    config = GitHubAppConfig(
        org="acme",
        app_id="123",
        installation_id="456",
        private_key="very-secret-private-key",
    )

    class FakeManager:
        def __init__(self, _config):
            self.config = _config

        async def ensure_repo(self, project_root, repo_name=None):
            return type(
                "Repo",
                (),
                {
                    "full_name": "acme/claudius-1000-main",
                    "name": "claudius-1000-main",
                    "clone_url": "https://github.com/acme/claudius-1000-main.git",
                    "ssh_url": "git@github.com:acme/claudius-1000-main.git",
                    "default_branch": "main",
                },
            )()

        async def create_repo_token(self, repo):
            return "ghs_short_lived_token"

    monkeypatch.setattr(
        orchestrator_module.GitHubAppConfig,
        "from_settings",
        staticmethod(lambda _settings: config),
    )
    monkeypatch.setattr(orchestrator_module, "GitHubRepoManager", FakeManager)

    runtime_env = await orchestrator._github_runtime_env(
        orchestrator_module.Settings(),
        workspace,
        tenant,
    )

    assert runtime_env is not None
    assert runtime_env["GITHUB_TOKEN"] == "ghs_short_lived_token"
    assert runtime_env["GITHUB_REPO_FULL_NAME"] == "acme/claudius-1000-main"
    assert "GITHUB_APP_PRIVATE_KEY" not in runtime_env
    assert "GITHUB_APP_ID" not in runtime_env
    assert "GITHUB_APP_INSTALLATION_ID" not in runtime_env


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
    run_id = db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)
    db.set_active_run(tenant.id, workspace.project_name, run_id, None)

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
    assert "Queued Run Inputs" in status_text
    assert "Add a testimonials section" in status_text


@pytest.mark.asyncio
async def test_orchestrator_queues_run_inputs_when_active(tmp_path):
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

    tenant = db.get_or_create_tenant("telegram", "555")
    workspace = workspace_manager.ensure_workspace(tenant.key)
    run_id = db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)
    db.set_active_run(tenant.id, workspace.project_name, run_id, None)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="queued-1",
        tenant_external_id="555",
        received_at=datetime.now(tz=timezone.utc),
        text="Queue this update",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "busy"
    assert not agent.calls
    queued = db.fetch_run_inputs(tenant.id, workspace.project_name, status="queued")
    assert queued


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

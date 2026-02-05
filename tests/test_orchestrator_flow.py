from datetime import datetime, timedelta, timezone
import json

import pytest

import demi.orchestrator as orchestrator_module
from demi.domains.github_app import GitHubAppConfig
from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, create_test_tenant, unique_external_id


class FakeAgent:
    def __init__(self, deploy_url="https://example.com/site", decision_project: str | None = None):
        self.calls = []
        self.deploy_url = deploy_url
        self.route_calls = []
        self.decision_project = decision_project

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
    ):
        self.route_calls.append((workspace.root, message))
        return {
            "ok": True,
            "project_name": self.decision_project or workspace.project_name,
            "should_run": True,
            "queue_run": False,
            "dedupe": False,
            "ask_questions": [],
            "purpose": "execute",
        }

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
        self.calls.append((workspace.root, task_path, session_id, runtime_env))
        if db is not None and tenant_id is not None:
            db.update_tenant_deploy_url(tenant_id, self.deploy_url)
        (workspace.tasks_dir / "result_summary.md").write_text("ok")
        return type("AgentResult", (), {"session_id": session_id, "summary": "ok"})()


class FakeMessenger:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        self.sent.append((tenant_external_id, text))


@pytest.mark.asyncio
async def test_orchestrator_new_site_flow(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="51",
        tenant_external_id=tenant_external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Build me a barber site",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    tenant = db.get_tenant_by_external("telegram", tenant_external_id)
    assert tenant is not None
    assert tenant.last_deploy_url == "https://example.com/site"


@pytest.mark.asyncio
async def test_orchestrator_passes_github_runtime_env(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    agent = FakeAgent(decision_project="beta")
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

    tenant_external_id = unique_external_id("tenant")
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="52",
        tenant_external_id=tenant_external_id,
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
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )
    tenant = create_test_tenant(db)
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
                    "full_name": "acme/site-1000-main",
                    "name": "site-1000-main",
                    "clone_url": "https://github.com/acme/site-1000-main.git",
                    "ssh_url": "git@github.com:acme/site-1000-main.git",
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
    assert runtime_env["GITHUB_REPO_FULL_NAME"] == "acme/site-1000-main"
    assert "GITHUB_APP_PRIVATE_KEY" not in runtime_env
    assert "GITHUB_APP_ID" not in runtime_env
    assert "GITHUB_APP_INSTALLATION_ID" not in runtime_env


@pytest.mark.asyncio
async def test_github_runtime_env_prefers_repo_name_file(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    (workspace.tasks_dir / "repo_name.txt").write_text("pc-parts-athens\n")

    config = GitHubAppConfig(
        org="acme",
        app_id="123",
        installation_id="456",
        private_key="very-secret-private-key",
    )

    class FakeManager:
        def __init__(self, _config):
            self.config = _config
            self.repo_names = []

        async def ensure_repo(self, project_root, repo_name=None):
            self.repo_names.append(repo_name)
            return type(
                "Repo",
                (),
                {
                    "full_name": "acme/pc-parts-athens",
                    "name": "pc-parts-athens",
                    "clone_url": "https://github.com/acme/pc-parts-athens.git",
                    "ssh_url": "git@github.com:acme/pc-parts-athens.git",
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
    manager = FakeManager(config)
    monkeypatch.setattr(orchestrator_module, "GitHubRepoManager", lambda _config: manager)

    runtime_env = await orchestrator._github_runtime_env(
        orchestrator_module.Settings(),
        workspace,
        tenant,
    )

    assert runtime_env is not None
    assert manager.repo_names == ["pc-parts-athens"]
    assert runtime_env["GITHUB_REPO_FULL_NAME"] == "acme/pc-parts-athens"


@pytest.mark.asyncio
async def test_github_runtime_env_retries_on_name_conflict(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )
    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    (workspace.tasks_dir / "repo_name.txt").write_text("pc-parts-athens\n")

    config = GitHubAppConfig(
        org="acme",
        app_id="123",
        installation_id="456",
        private_key="very-secret-private-key",
    )

    class FakeManager:
        def __init__(self, _config):
            self.config = _config
            self.repo_names = []
            self.calls = 0

        async def ensure_repo(self, project_root, repo_name=None):
            self.repo_names.append(repo_name)
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("github_repo_name_conflict")
            return type(
                "Repo",
                (),
                {
                    "full_name": f"acme/{repo_name}",
                    "name": repo_name,
                    "clone_url": f"https://github.com/acme/{repo_name}.git",
                    "ssh_url": f"git@github.com:acme/{repo_name}.git",
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
    manager = FakeManager(config)
    monkeypatch.setattr(orchestrator_module, "GitHubRepoManager", lambda _config: manager)

    runtime_env = await orchestrator._github_runtime_env(
        orchestrator_module.Settings(),
        workspace,
        tenant,
    )

    assert runtime_env is not None
    assert len(manager.repo_names) == 2
    assert manager.repo_names[0] == "pc-parts-athens"
    assert manager.repo_names[1].startswith("pc-parts-athens-")


@pytest.mark.asyncio
async def test_orchestrator_supersedes_and_cancels_active_run(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    class SupersedeAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.cancel_calls = []

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
        ):
            return {
                "ok": True,
                "project_name": workspace.project_name,
                "should_run": True,
                "queue_run": False,
                "dedupe": False,
                "ask_questions": [],
                "supersede_active_run": True,
                "purpose": "supersede",
            }

        async def cancel_run(self, workspace):
            self.cancel_calls.append(workspace.root)

    agent = SupersedeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")
    seed_msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="seed",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="seed",
        images=[],
        raw={},
    )
    seed_id, _ = db.record_message(tenant.id, seed_msg)
    run_id = db.create_run(
        tenant.id, message_id=seed_id, project_name=workspace.project_name
    )
    db.set_active_run(tenant.id, workspace.project_name, run_id, datetime.now(tz=timezone.utc).isoformat())

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="52",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="new request",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    assert agent.cancel_calls


@pytest.mark.asyncio
async def test_migrate_legacy_queue_ignores_duplicate_inputs(tmp_path, monkeypatch):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="main")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="dup-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="pending message",
        images=[],
        raw={},
    )
    message_id, _ = db.record_message(tenant.id, msg)
    db.update_message_status(message_id, "pending")

    def _raise_duplicate(*_args, **_kwargs):
        raise RuntimeError(
            'duplicate key value violates unique constraint "run_inputs_dedupe_idx"'
        )

    monkeypatch.setattr(orchestrator, "_enqueue_run_input", _raise_duplicate)

    migrated = await orchestrator.migrate_legacy_queue()

    assert migrated == 0
    row = db.get_message(message_id)
    assert row["status"] == "processed"


@pytest.mark.asyncio
async def test_orchestrator_reconciles_run_result(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="1",
        tenant_external_id=tenant.external_id,
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
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Try again",
        images=[],
        raw={},
    )
    result = await orchestrator.handle_message(new_msg)

    assert result.status == "accepted"
    run_row = db.get_run(run_id)
    assert run_row["status"] == "completed"


@pytest.mark.asyncio
async def test_orchestrator_reconcile_does_not_double_count_usage(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="1",
        tenant_external_id=tenant.external_id,
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

    db.add_run_usage(
        run_id,
        total_cost_usd=1.23,
        usage={"input_tokens": 10},
        usage_key="primary",
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
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Try again",
        images=[],
        raw={},
    )
    result = await orchestrator.handle_message(new_msg)

    assert result.status == "accepted"
    run_row = db.get_run(run_id)
    assert float(run_row["total_cost_usd"]) == 1.23
    usage = run_row["usage_json"]
    assert len(usage.get("primary", [])) == 1


@pytest.mark.asyncio
async def test_orchestrator_refreshes_interaction_context_after_project_switch(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(decision_project="project-b"),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace_manager.ensure_workspace(tenant.key, project_name="project-b")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="msg-3",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Switch project",
        images=[],
        raw={},
    )
    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    switched_workspace = workspace_manager.ensure_workspace(tenant.key, project_name="project-b")
    context_payload = json.loads(
        (switched_workspace.tasks_dir / "interaction_context.json").read_text()
    )
    assert context_payload["project_name"] == "project-b"


@pytest.mark.asyncio
@pytest.mark.skip(reason="Temporarily disabled per request (flake in local env).")
async def test_orchestrator_blocks_after_two_hard_failures(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    messenger = FakeMessenger()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=messenger,
    )

    tenant = create_test_tenant(db)
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
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Status?",
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    jobs = db.fetch_pending_event_jobs(limit=5)
    assert jobs


@pytest.mark.asyncio
async def test_orchestrator_clears_block_on_retry(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    messenger = FakeMessenger()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=messenger,
    )

    tenant = create_test_tenant(db)
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
        tenant_external_id=tenant.external_id,
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
        tenant_external_id=tenant.external_id,
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
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    agent = FakeAgent()
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    alpha_ws = workspace_manager.ensure_workspace(tenant.key, project_name="alpha")
    beta_ws = workspace_manager.ensure_workspace(tenant.key, project_name="beta")

    db.create_run(tenant.id, message_id=0, project_name=alpha_ws.project_name)

    msg_text = f"{directive}\nUpdate the hero headline"
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="parallel-1",
        tenant_external_id=tenant.external_id,
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
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key)
    run_id = db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)
    db.set_active_run(tenant.id, workspace.project_name, run_id, None)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="pending-1",
        tenant_external_id=tenant.external_id,
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
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    agent = FakeAgent()

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key)
    run_id = db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)
    db.set_active_run(tenant.id, workspace.project_name, run_id, None)

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="queued-1",
        tenant_external_id=tenant.external_id,
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
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key)
    run_id = db.create_run(tenant.id, message_id=0, project_name=workspace.project_name)
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    db.update_run_lease(
        run_id, lease_expires_at=past.isoformat(), last_heartbeat_at=past.isoformat()
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="stale-1",
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Continue",
        images=[],
        raw={},
        project_name=workspace.project_name,
    )

    result = await orchestrator.handle_message(msg)

    assert result.status == "accepted"
    run_row = db.get_run(run_id)
    assert run_row["status"] == "failed"
    assert run_row["error"] == "lease_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reset_text",
    ["/reset", "/reset now", "/reset@mybot", "/reset@mybot now"],
)
async def test_orchestrator_reset_clears_state(tmp_path, reset_text):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=FakeAgent(),
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
    workspace = workspace_manager.ensure_workspace(tenant.key, project_name="main")

    pending_msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="p1",
        tenant_external_id=tenant.external_id,
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
        tenant_external_id=tenant.external_id,
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
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text=reset_text,
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(reset_msg)

    assert result.status == "accepted"
    pending_row = db.get_message(pending_id)
    processing_row = db.get_message(processing_id)
    assert pending_row["status"] == "processed"
    assert processing_row["status"] == "processed"
    runs = db.list_recent_runs(tenant.id, project_name=workspace.project_name, limit=1)
    assert runs
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"] == "user_reset"


@pytest.mark.asyncio
async def test_orchestrator_infers_project_from_description(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")

    agent = FakeAgent(decision_project="cafe")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=FakeMessenger(),
    )

    tenant = create_test_tenant(db)
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
        tenant_external_id=tenant.external_id,
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

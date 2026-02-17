from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from demi.config import Settings
from demi.runtime.docker_agent import DockerAgent


def test_docker_agent_entrypoint_command_includes_run_id():
    agent = DockerAgent(
        pool=object(),
        settings=Settings(),
        mount_path="/workspace",
    )

    command = agent._entrypoint_command(321)
    assert command == ["python", "-m", "demi.runtime.agent_entrypoint", "--run-id", "321"]


def test_docker_agent_prepare_context_accepts_execution_context(tmp_path):
    class _FakePool:
        def __init__(self) -> None:
            self.run_calls: list[tuple[object, list[str], dict[str, str]]] = []

        def pop_container_for_workspace(self, _tenant_root):
            return None

        async def exec_in_container(self, *_args, **_kwargs):
            raise AssertionError("exec_in_container should not be called when no slot exists")

        async def retire_container(self, *_args, **_kwargs):
            raise AssertionError("retire_container should not be called when no slot exists")

        async def run_in_fresh_container(self, tenant_root, command, env=None):
            self.run_calls.append((tenant_root, list(command), dict(env or {})))

    pool = _FakePool()
    agent = DockerAgent(pool=pool, settings=Settings(), mount_path="/workspace")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "run_result.json").write_text(
        json.dumps({"run_id": 7, "session_id": "session-1"}),
        encoding="utf-8",
    )

    workspace = SimpleNamespace(root=tmp_path, tenant_root=tmp_path, tasks_dir=tasks_dir)
    message = SimpleNamespace(provider="telegram", tenant_external_id="tenant-3")

    result = asyncio.run(
        agent.prepare_context(
            workspace=workspace,
            task_path=tasks_dir / "task.md",
            message=message,
            run_id=7,
            execution_context="Guidra",
        )
    )

    assert result.session_id == "session-1"
    assert len(pool.run_calls) == 1
    tenant_root, command, _env = pool.run_calls[0]
    assert tenant_root == tmp_path
    assert command == ["python", "-m", "demi.runtime.agent_entrypoint", "--run-id", "7"]


def test_docker_agent_prepare_context_reads_retry_policy(tmp_path):
    class _FakePool:
        def pop_container_for_workspace(self, _tenant_root):
            return None

        async def exec_in_container(self, *_args, **_kwargs):
            raise AssertionError("exec_in_container should not be called when no slot exists")

        async def retire_container(self, *_args, **_kwargs):
            raise AssertionError("retire_container should not be called when no slot exists")

        async def run_in_fresh_container(self, _tenant_root, _command, env=None):
            del env

    agent = DockerAgent(pool=_FakePool(), settings=Settings(), mount_path="/workspace")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "run_result.json").write_text(
        json.dumps(
            {
                "run_id": 11,
                "session_id": "session-2",
                "retry_policy": {"retryable": True, "dedupe_key": "event:abc"},
            }
        ),
        encoding="utf-8",
    )

    workspace = SimpleNamespace(root=tmp_path, tenant_root=tmp_path, tasks_dir=tasks_dir)
    message = SimpleNamespace(provider="telegram", tenant_external_id="tenant-4")

    result = asyncio.run(
        agent.prepare_context(
            workspace=workspace,
            task_path=tasks_dir / "task.md",
            message=message,
            run_id=11,
        )
    )

    assert result.session_id == "session-2"
    assert result.retry_policy == {"retryable": True, "dedupe_key": "event:abc"}


def test_docker_agent_prepare_context_rejects_stale_run_result(tmp_path):
    class _FakePool:
        def pop_container_for_workspace(self, _tenant_root):
            return None

        async def exec_in_container(self, *_args, **_kwargs):
            raise AssertionError("exec_in_container should not be called when no slot exists")

        async def retire_container(self, *_args, **_kwargs):
            raise AssertionError("retire_container should not be called when no slot exists")

        async def run_in_fresh_container(self, _tenant_root, _command, env=None):
            del env

    agent = DockerAgent(pool=_FakePool(), settings=Settings(), mount_path="/workspace")

    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "run_result.json").write_text(
        json.dumps({"run_id": 999, "session_id": "stale-session"}),
        encoding="utf-8",
    )

    workspace = SimpleNamespace(root=tmp_path, tenant_root=tmp_path, tasks_dir=tasks_dir)
    message = SimpleNamespace(provider="telegram", tenant_external_id="tenant-5")

    try:
        asyncio.run(
            agent.prepare_context(
                workspace=workspace,
                task_path=tasks_dir / "task.md",
                message=message,
                run_id=12,
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "agent_result_stale_run_result"
    else:
        raise AssertionError("expected stale run_result rejection")


def test_docker_agent_custom_allowlist_keeps_claude_auth_mode():
    agent = DockerAgent(
        pool=object(),
        settings=Settings(docker_env_allowlist="TELEGRAM_BOT_TOKEN"),
        mount_path="/workspace",
    )

    allowlist = agent._env_allowlist()
    assert "CLAUDE_AUTH_MODE" in allowlist
    assert "AGENTMAIL_API_KEY" in allowlist
    assert "AGENTMAIL_INBOX_ADDRESS" in allowlist


def test_docker_agent_custom_allowlist_build_env_keeps_agentmail_vars():
    agent = DockerAgent(
        pool=object(),
        settings=Settings(
            docker_env_allowlist="TELEGRAM_BOT_TOKEN",
            agentmail_api_key="test-agentmail-key",
            agentmail_inbox_address="demi@example.com",
        ),
        mount_path="/workspace",
    )

    env = agent._build_env()
    assert env.get("AGENTMAIL_API_KEY") == "test-agentmail-key"
    assert env.get("AGENTMAIL_INBOX_ADDRESS") == "demi@example.com"

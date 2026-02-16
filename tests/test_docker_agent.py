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
        json.dumps({"session_id": "session-1"}),
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

from __future__ import annotations

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

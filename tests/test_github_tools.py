import asyncio
import json

import claudius.agent.github_tools as github_tools_module
from claudius.agent.github_tools import GitHubToolContext, build_github_tools


def _prepare_repo_tool(tmp_path, runtime_env=None):
    tasks_dir = tmp_path / "tasks"
    tools = build_github_tools(
        GitHubToolContext(tasks_dir=tasks_dir, runtime_env=runtime_env),
    )
    return next(tool for tool in tools if tool.name == "prepare_repo"), tasks_dir


def test_prepare_repo_uses_runtime_repo_credentials(tmp_path):
    tool, _ = _prepare_repo_tool(
        tmp_path,
        runtime_env={
            "GITHUB_TOKEN": "ghs_runtime_token",
            "GITHUB_REPO_FULL_NAME": "acme/shop-site",
            "GITHUB_REPO_NAME": "shop-site",
            "GITHUB_REPO_URL": "https://github.com/acme/shop-site",
            "GITHUB_REPO_HTTP_URL": "https://github.com/acme/shop-site.git",
            "GITHUB_REPO_DEFAULT_BRANCH": "main",
        },
    )
    result = asyncio.run(tool.handler({"repo_name": "ignored"}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is True
    assert payload["status"] == "ready"
    assert payload["warning"] == "using_runtime_repo_credentials"
    assert payload["repo"]["full_name"] == "acme/shop-site"
    assert payload["token"] == "ghs_runtime_token"


def test_prepare_repo_redacts_runtime_token_in_tool_logs(tmp_path):
    tool, tasks_dir = _prepare_repo_tool(
        tmp_path,
        runtime_env={
            "GITHUB_TOKEN": "ghs_runtime_token",
            "GITHUB_REPO_FULL_NAME": "acme/shop-site",
            "GITHUB_REPO_NAME": "shop-site",
        },
    )
    asyncio.run(tool.handler({}))

    line = (tasks_dir / "tool_runs.jsonl").read_text().splitlines()[-1]
    record = json.loads(line)

    assert record["tool"] == "prepare_repo"
    assert record["result"]["token"] == "redacted"
    assert "ghs_runtime_token" not in line


def test_prepare_repo_ignores_process_env_without_runtime_context(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_cross_tenant_token")
    monkeypatch.setenv("GITHUB_REPO_FULL_NAME", "other-org/other-repo")
    monkeypatch.setenv("GITHUB_REPO_NAME", "other-repo")
    monkeypatch.setattr(
        github_tools_module.GitHubAppConfig,
        "from_settings",
        staticmethod(lambda _settings: None),
    )

    tool, _ = _prepare_repo_tool(tmp_path, runtime_env=None)
    result = asyncio.run(tool.handler({}))
    payload = json.loads(result["content"][0]["text"])

    assert payload["ok"] is False
    assert payload["status"] == "missing_github_config"

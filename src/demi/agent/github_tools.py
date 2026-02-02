from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import json
import time

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from demi.agent.tool_logging import log_tool_run
from demi.config import Settings
from demi.domains.github_app import GitHubAppConfig, GitHubRepo, GitHubRepoManager


GITHUB_SERVER_NAME = "demi-github"


@dataclass(frozen=True)
class GitHubToolContext:
    tasks_dir: Path
    runtime_env: Mapping[str, str] | None = None


def build_github_tools(context: GitHubToolContext) -> list[SdkMcpTool[Any]]:
    def _log(
        tool_name: str,
        args: dict[str, Any],
        result: Any | None = None,
        error: str | None = None,
        start: float | None = None,
    ) -> None:
        duration_ms = None
        if start is not None:
            duration_ms = (time.monotonic() - start) * 1000.0
        log_tool_run(
            context.tasks_dir,
            tool_name,
            args=args,
            result=result,
            error=error,
            duration_ms=duration_ms,
        )

    @tool(
        "prepare_repo",
        "Ensure a GitHub repo exists and return repo details + a short-lived token.",
        {
            "type": "object",
            "properties": {
                "repo_name": {"type": "string"},
            },
            "required": [],
        },
    )
    async def prepare_repo(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        repo_name = str(args.get("repo_name") or "").strip()
        env_repo, env_token = _repo_from_runtime_env(context.runtime_env)
        if env_repo and env_token:
            payload = {
                "ok": True,
                "status": "ready",
                "repo": env_repo.to_dict(),
                "token": env_token,
                "warning": "using_runtime_repo_credentials",
            }
            log_payload = dict(payload)
            log_payload["token"] = "redacted"
            _log("prepare_repo", args, result=log_payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        settings = Settings()
        config = GitHubAppConfig.from_settings(settings)
        if not config or not config.enabled:
            payload = {"ok": False, "status": "missing_github_config"}
            _log("prepare_repo", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        manager = GitHubRepoManager(config)
        project_root = context.tasks_dir.parent

        repo = manager.load_repo(project_root)
        warning = None
        if repo:
            try:
                remote = await manager.client.get_repo(repo.full_name)
            except Exception as exc:  # noqa: BLE001
                warning = f"repo_refresh_failed:{type(exc).__name__}"
            else:
                if remote:
                    manager.write_repo(project_root, remote)
                    repo = remote
                else:
                    warning = "repo_missing"
                    repo = None
        if repo is None:
            if not repo_name:
                payload = {"ok": False, "status": "repo_name_required"}
                _log("prepare_repo", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            try:
                repo = await manager.ensure_repo(project_root, repo_name=repo_name)
            except RuntimeError as exc:
                status = "name_conflict" if _is_name_conflict(exc) else "github_error"
                payload = {"ok": False, "status": status, "error": str(exc)}
                _log("prepare_repo", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": status != "name_conflict",
                }

        try:
            token = await manager.client.create_installation_token(repositories=[repo.name])
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "status": "token_failed", "error": str(exc)}
            _log("prepare_repo", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        payload = {
            "ok": True,
            "status": "ready",
            "repo": repo.to_dict(),
            "token": token,
        }
        if warning:
            payload["warning"] = warning
        log_payload = dict(payload)
        log_payload["token"] = "redacted"
        _log("prepare_repo", args, result=log_payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    return [prepare_repo]


def build_github_server(context: GitHubToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=GITHUB_SERVER_NAME,
        version="1.0.0",
        tools=build_github_tools(context),
    )


def _is_name_conflict(exc: Exception) -> bool:
    message = str(exc).lower()
    if "github_repo_name_conflict" in message:
        return True
    if "github_api_error:422" in message:
        return True
    if "already exists" in message:
        return True
    if "name already exists" in message:
        return True
    return False


def _repo_from_runtime_env(
    runtime_env: Mapping[str, str] | None,
) -> tuple[GitHubRepo | None, str | None]:
    if not runtime_env:
        return None, None
    token = _runtime_value(runtime_env, "GITHUB_TOKEN")
    full_name = _runtime_value(runtime_env, "GITHUB_REPO_FULL_NAME")
    repo_name = _runtime_value(runtime_env, "GITHUB_REPO_NAME")
    if not token or not full_name or not repo_name:
        return None, None
    repo = GitHubRepo(
        id=None,
        name=repo_name,
        full_name=full_name,
        html_url=_runtime_url(runtime_env, "GITHUB_REPO_HTML_URL", "GITHUB_REPO_URL"),
        clone_url=_runtime_url(runtime_env, "GITHUB_REPO_HTTP_URL", "GITHUB_REPO_URL"),
        ssh_url=_runtime_value(runtime_env, "GITHUB_REPO_SSH_URL"),
        default_branch=_runtime_value(runtime_env, "GITHUB_REPO_DEFAULT_BRANCH"),
        private=None,
    )
    return repo, token


def _runtime_url(runtime_env: Mapping[str, str], primary: str, fallback: str) -> str | None:
    value = _runtime_value(runtime_env, primary)
    if value:
        return value
    return _runtime_value(runtime_env, fallback)


def _runtime_value(runtime_env: Mapping[str, str], key: str) -> str | None:
    value = str(runtime_env.get(key) or "").strip()
    return value or None

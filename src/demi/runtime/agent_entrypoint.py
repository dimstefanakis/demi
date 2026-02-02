from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from demi.agent.claude import ClaudeAgent
from demi.config import Settings
from demi.messaging.file import FileMessenger
from demi.messaging.telegram import TelegramClient, TelegramConfig
from demi.models import Attachment, NormalizedMessage
from demi.workspace.core import WorkspaceManager


_GITHUB_RUNTIME_ENV_KEYS = (
    "GITHUB_TOKEN",
    "GITHUB_REPO_FULL_NAME",
    "GITHUB_REPO_NAME",
    "GITHUB_REPO_OWNER",
    "GITHUB_REPO_URL",
    "GITHUB_REPO_HTTP_URL",
    "GITHUB_REPO_HTML_URL",
    "GITHUB_REPO_SSH_URL",
    "GITHUB_REPO_DEFAULT_BRANCH",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single agent task inside a container.")
    parser.add_argument("--request", required=True, help="Path to run_request.json inside container")
    return parser.parse_args()


def _load_message(payload: dict) -> NormalizedMessage:
    message = payload.get("message") or {}
    images = [
        Attachment(
            provider_file_id=str(img.get("provider_file_id")),
            width=img.get("width"),
            height=img.get("height"),
        )
        for img in message.get("images") or []
    ]
    received_raw = message.get("received_at")
    if received_raw:
        received_at = datetime.fromisoformat(received_raw)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
    else:
        received_at = datetime.now(tz=timezone.utc)
    return NormalizedMessage(
        provider=str(message.get("provider") or "telegram"),
        provider_message_id=str(message.get("provider_message_id") or "unknown"),
        tenant_external_id=str(message.get("tenant_external_id") or "unknown"),
        received_at=received_at,
        text=message.get("text"),
        images=images,
        raw=message.get("raw") or {},
    )


def _load_runtime_env_from_process() -> dict[str, str] | None:
    runtime_env: dict[str, str] = {}
    for key in _GITHUB_RUNTIME_ENV_KEYS:
        value = str(os.getenv(key) or "").strip()
        if value:
            runtime_env[key] = value
    return runtime_env or None


async def _run(request_path: Path) -> int:
    payload = json.loads(request_path.read_text())
    workspace_root = Path(payload.get("workspace_root") or "/workspace")
    task_path = Path(payload.get("task_path") or (workspace_root / "tasks" / "latest.md"))
    session_id = payload.get("session_id")

    settings = Settings()
    workspace_manager = WorkspaceManager(root_dir=workspace_root)
    workspace = workspace_manager.ensure_workspace_at_path(workspace_root)

    message = _load_message(payload)

    if settings.telegram_bot_token:
        messenger = TelegramClient(TelegramConfig(bot_token=settings.telegram_bot_token))
    else:
        messenger = FileMessenger(tasks_dir=workspace.tasks_dir)

    agent = ClaudeAgent()
    runtime_env = _load_runtime_env_from_process()
    result = await agent.prepare_context(
        workspace=workspace,
        task_path=task_path,
        message=message,
        messenger=messenger,
        inflight_stream=None,
        tenant_id=None,
        db=None,
        payments=None,
        session_id=session_id,
        runtime_env=runtime_env,
    )

    result_path = workspace.tasks_dir / "run_result.json"
    result_payload = asdict(result)
    result_path.write_text(json.dumps(result_payload, indent=2))
    return 0


def main() -> None:
    args = _parse_args()
    request_path = Path(args.request)
    try:
        exit_code = asyncio.run(_run(request_path))
    except Exception as exc:  # noqa: BLE001
        error_path = request_path.parent / "run_result.json"
        error_payload = {"error": str(exc)}
        error_path.write_text(json.dumps(error_payload, indent=2))
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

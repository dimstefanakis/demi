from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

from claudius.db.core import Database
from claudius.models import NormalizedMessage, OrchestratorResult
from claudius.workspace.core import WorkspaceManager
from claudius.memory import build_summarization_prompt, read_logs, rewrite_logs
from claudius.memory.logs import append_log, write_chat_history


@dataclass
class Orchestrator:
    db: Database
    workspace_manager: WorkspaceManager
    agent: Any
    messenger: Any

    async def handle_message(self, msg: NormalizedMessage) -> OrchestratorResult:
        tenant = self.db.get_or_create_tenant(msg.provider, msg.tenant_external_id)

        workspace = self.workspace_manager.ensure_workspace(tenant.key)
        if tenant.workspace_path != str(workspace.root):
            self.db.update_tenant_workspace(tenant.id, str(workspace.root))

        inserted = self.db.record_message(tenant.id, msg)
        if not inserted:
            return OrchestratorResult(status="duplicate", detail="message already processed")

        if self.db.has_inflight_run(tenant.id):
            return OrchestratorResult(status="busy", detail="tenant already running")

        asset_paths = []
        if msg.images and hasattr(self.messenger, "download_images"):
            asset_paths = await self.messenger.download_images(msg.images, workspace.assets_dir)

        task_content = self._build_task_content(msg, asset_paths)
        task_path = workspace.write_task(task_content)

        self._clear_run_artifacts(workspace.tasks_dir)
        self._append_chat_log(workspace.tasks_dir, "user", msg.text or "", msg.received_at)
        write_chat_history(workspace.tasks_dir)
        self._maybe_prepare_compaction(workspace.tasks_dir)

        run_id = self.db.create_run(tenant.id)
        try:
            agent_result = await self.agent.prepare_context(
                workspace=workspace,
                task_path=task_path,
                message=msg,
                messenger=self.messenger,
                session_id=tenant.session_id,
            )
            if agent_result.session_id:
                self.db.update_tenant_session(tenant.id, agent_result.session_id)
            self.db.update_run_usage(
                run_id,
                total_cost_usd=getattr(agent_result, "total_cost_usd", None),
                usage=getattr(agent_result, "usage", None),
            )
            response = self._read_response_contract(workspace.tasks_dir / "response.json")
            if response:
                kind = response.get("kind")
                if kind == "deploy" and response.get("deploy_url"):
                    deploy_url = response["deploy_url"]
                    self.db.update_tenant_deploy_url(tenant.id, deploy_url)
                    await self.messenger.send_text(
                        msg.tenant_external_id, f"Your site is live: {deploy_url}"
                    )
                    self._append_chat_log(workspace.tasks_dir, "assistant", f"DEPLOY_URL {deploy_url}")
                elif response.get("text"):
                    await self.messenger.send_text(msg.tenant_external_id, response["text"])
                    self._append_chat_log(workspace.tasks_dir, "assistant", response["text"])
            self.db.finish_run(run_id, status="completed")
            return OrchestratorResult(status="accepted")
        except Exception as exc:  # noqa: BLE001
            self.db.finish_run(run_id, status="failed", error=str(exc))
            raise

    @staticmethod
    def _build_task_content(msg: NormalizedMessage, asset_paths: list[str] | None = None) -> str:
        lines = ["# Task", "", f"Message: {msg.text or ''}".strip()]
        if msg.images:
            lines.append("\n## Images")
            for image in msg.images:
                lines.append(f"- {image.provider_file_id} ({image.width}x{image.height})")
        if asset_paths:
            lines.append("\n## Saved Assets")
            for path in asset_paths:
                lines.append(f"- {path}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _read_optional_file(path: Path) -> str | None:
        if not path.exists():
            return None
        content = path.read_text().strip()
        return content or None

    @staticmethod
    def _clear_run_artifacts(tasks_dir: Path) -> None:
        for name in (
            "deploy_url.txt",
            "user_reply.txt",
            "result_summary.md",
            "response.json",
            "summary_prompt.md",
        ):
            path = tasks_dir / name
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _read_response_contract(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data


    @staticmethod
    def _append_chat_log(tasks_dir: Path, role: str, text: str, timestamp: Any | None = None) -> None:
        append_log(tasks_dir, f"{role}_message", text, timestamp=timestamp)

    @staticmethod
    @staticmethod
    def _maybe_prepare_compaction(tasks_dir: Path, max_entries: int = 30, keep_last: int = 10) -> None:
        entries = read_logs(tasks_dir)
        if len(entries) <= max_entries:
            return
        summary_path = tasks_dir / "chat_summary.md"
        previous_summary = summary_path.read_text() if summary_path.exists() else ""
        to_summarize = entries[:-keep_last]
        prompt = build_summarization_prompt(previous_summary, to_summarize)
        summary_prompt_path = tasks_dir / "summary_prompt.md"
        summary_prompt_path.write_text(
            f"SYSTEM_PROMPT:\n{prompt.system_prompt}\n\nUSER_MESSAGE:\n{prompt.messages[0]['content']}\n"
        )

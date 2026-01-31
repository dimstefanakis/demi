from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from typing import Any

from claudius.agent.claude import AgentResult
from claudius.agent.claude import ClaudeAgent
from claudius.config import Settings
from claudius.runtime.docker_pool import DockerPool


@dataclass
class DockerAgent:
    supports_inflight_stream = False
    pool: DockerPool
    settings: Settings
    mount_path: str = "/workspace"
    forward_messages: bool = False

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
        runtime_env: dict[str, str] | None = None,
    ) -> AgentResult:
        tenant_root = getattr(workspace, "tenant_root", workspace.root)
        request_path = workspace.tasks_dir / "run_request.json"
        result_path = workspace.tasks_dir / "run_result.json"
        outbound_path = workspace.tasks_dir / "outbound_messages.jsonl"

        request_payload = self._build_request(
            tenant_root, workspace.root, task_path, message, session_id
        )
        request_path.write_text(json.dumps(request_payload, indent=2))

        env = self._build_env(runtime_env)
        slot = self.pool.pop_container_for_workspace(tenant_root)
        if slot:
            await self.pool.exec_in_container(
                slot,
                self._entrypoint_command(tenant_root, request_path),
                env=env,
            )
            await self.pool.retire_container(slot)
        else:
            await self.pool.run_in_fresh_container(
                tenant_root,
                self._entrypoint_command(tenant_root, request_path),
                env=env,
            )

        result = self._read_result(result_path)
        if not result:
            raise RuntimeError("agent_result_missing")
        if result.get("error"):
            raise RuntimeError(str(result.get("error")))
        self._maybe_update_deploy(db, tenant_id, workspace.tasks_dir)
        if self.forward_messages and messenger and outbound_path.exists():
            await self._forward_messages(outbound_path, messenger, message.tenant_external_id)
            try:
                outbound_path.unlink()
            except OSError:
                pass
        return AgentResult(
            session_id=result.get("session_id"),
            summary=result.get("summary"),
            total_cost_usd=result.get("total_cost_usd"),
            usage=result.get("usage"),
        )

    async def send_interaction_message(
        self,
        workspace,
        text: str,
        messenger: Any,
        tenant_id: int | None = None,
        db: Any | None = None,
        payments: Any | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        tenant_external_id: str | None = None,
    ) -> None:
        interaction_agent = ClaudeAgent()
        await interaction_agent.send_interaction_message(
            workspace=workspace,
            text=text,
            messenger=messenger,
            tenant_id=tenant_id,
            db=db,
            payments=payments,
            session_id=session_id,
            provider=provider,
            tenant_external_id=tenant_external_id,
        )

    async def send_interaction_instruction(
        self,
        workspace,
        instruction: str,
        messenger: Any,
        tenant_id: int | None = None,
        db: Any | None = None,
        payments: Any | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        tenant_external_id: str | None = None,
    ) -> None:
        interaction_agent = ClaudeAgent()
        await interaction_agent.send_interaction_instruction(
            workspace=workspace,
            instruction=instruction,
            messenger=messenger,
            tenant_id=tenant_id,
            db=db,
            payments=payments,
            session_id=session_id,
            provider=provider,
            tenant_external_id=tenant_external_id,
        )

    def _entrypoint_command(self, tenant_root: Path, request_path: Path) -> list[str]:
        container_request = self._container_path(tenant_root, request_path)
        return ["python", "-m", "claudius.runtime.agent_entrypoint", "--request", container_request]

    def _container_path(self, tenant_root: Path, host_path: Path) -> str:
        try:
            rel = host_path.relative_to(tenant_root)
        except ValueError:
            rel = host_path.name
        return str(Path(self.mount_path) / rel)

    def _build_request(
        self,
        tenant_root: Path,
        project_root: Path,
        task_path: Path,
        message,
        session_id: str | None,
    ) -> dict[str, Any]:
        try:
            relative_task = task_path.relative_to(tenant_root)
            container_task = str(Path(self.mount_path) / relative_task)
        except ValueError:
            container_task = str(Path(self.mount_path) / "tasks" / task_path.name)

        return {
            "workspace_root": self._container_path(tenant_root, project_root),
            "task_path": container_task,
            "session_id": session_id,
            "message": {
                "provider": message.provider,
                "provider_message_id": message.provider_message_id,
                "tenant_external_id": message.tenant_external_id,
                "received_at": message.received_at.isoformat(),
                "text": message.text,
                "images": [
                    {
                        "provider_file_id": img.provider_file_id,
                        "width": img.width,
                        "height": img.height,
                    }
                    for img in message.images
                ],
            },
        }

    def _build_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        allowlist = self._env_allowlist()
        env: dict[str, str] = {}
        for key in allowlist:
            value = os.getenv(key)
            if value:
                env[key] = value
                continue
            value = self._settings_fallback(key)
            if value:
                env[key] = value
        if extra_env:
            env.update(extra_env)
        return env

    def _env_allowlist(self) -> list[str]:
        raw = self.settings.docker_env_allowlist
        if raw:
            return [entry.strip() for entry in raw.split(",") if entry.strip()]
        return [
            "TELEGRAM_BOT_TOKEN",
            "UNSPLASH_ACCESS_KEY",
            "UNSPLASH_SECRET_KEY",
            "UNSPLASH_APP_ID",
            "CHROME_DEVTOOLS_MCP_ENABLED",
            "CHROME_DEVTOOLS_MCP_PROFILE_DIR",
            "CHROME_DEVTOOLS_MCP_COMMAND",
            "CHROME_DEVTOOLS_MCP_PACKAGE",
            "CHROME_DEVTOOLS_MCP_HEADLESS",
            "CHROME_DEVTOOLS_MCP_EXECUTABLE_PATH",
            "VERCEL_TOKEN",
            "VERCEL_SCOPE",
            "EVENT_URL",
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
            "STRIPE_SUCCESS_URL",
            "STRIPE_CANCEL_URL",
            "PUBLIC_BASE_URL",
            "SUPABASE_ACCESS_TOKEN",
            "SUPABASE_ORG_SLUG",
            "SUPABASE_ORG_ID",
            "SUPABASE_REGION_SELECTION",
            "SUPABASE_REGION",
            "SUPABASE_INSTANCE_SIZE",
            "SUPABASE_PROJECT_PREFIX",
            "CLAUDE_PLUGINS",
            "ANTHROPIC_API_KEY",
            "CLAUDE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ]

    def _settings_fallback(self, key: str) -> str | None:
        mapping = {
            "TELEGRAM_BOT_TOKEN": self.settings.telegram_bot_token,
            "UNSPLASH_ACCESS_KEY": self.settings.unsplash_access_key,
            "UNSPLASH_SECRET_KEY": self.settings.unsplash_secret_key,
            "UNSPLASH_APP_ID": self.settings.unsplash_app_id,
            "CHROME_DEVTOOLS_MCP_ENABLED": self.settings.chrome_devtools_mcp_enabled,
            "CHROME_DEVTOOLS_MCP_PROFILE_DIR": self.settings.chrome_devtools_mcp_profile_dir,
            "CHROME_DEVTOOLS_MCP_COMMAND": self.settings.chrome_devtools_mcp_command,
            "CHROME_DEVTOOLS_MCP_PACKAGE": self.settings.chrome_devtools_mcp_package,
            "CHROME_DEVTOOLS_MCP_HEADLESS": self.settings.chrome_devtools_mcp_headless,
            "CHROME_DEVTOOLS_MCP_EXECUTABLE_PATH": self.settings.chrome_devtools_mcp_executable_path,
            "VERCEL_TOKEN": self.settings.vercel_token,
            "VERCEL_SCOPE": self.settings.vercel_scope,
            "EVENT_URL": self.settings.event_url,
            "STRIPE_SECRET_KEY": self.settings.stripe_secret_key,
            "STRIPE_WEBHOOK_SECRET": self.settings.stripe_webhook_secret,
            "STRIPE_SUCCESS_URL": self.settings.stripe_success_url,
            "STRIPE_CANCEL_URL": self.settings.stripe_cancel_url,
            "PUBLIC_BASE_URL": self.settings.public_base_url,
            "SUPABASE_ACCESS_TOKEN": self.settings.supabase_access_token,
            "SUPABASE_ORG_SLUG": self.settings.supabase_org_slug,
            "SUPABASE_ORG_ID": self.settings.supabase_org_id,
            "SUPABASE_REGION_SELECTION": self.settings.supabase_region_selection,
            "SUPABASE_REGION": self.settings.supabase_region,
            "SUPABASE_INSTANCE_SIZE": self.settings.supabase_instance_size,
            "SUPABASE_PROJECT_PREFIX": self.settings.supabase_project_prefix,
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
            "CLAUDE_API_KEY": self.settings.claude_api_key,
            "GEMINI_API_KEY": self.settings.gemini_api_key,
            "GOOGLE_API_KEY": self.settings.google_api_key,
        }
        value = mapping.get(key)
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _read_result(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _maybe_update_deploy(db: Any | None, tenant_id: int | None, tasks_dir: Path) -> None:
        if db is None or tenant_id is None:
            return
        deploy_path = tasks_dir / "deploy_url.txt"
        if not deploy_path.exists():
            return
        deploy_url = deploy_path.read_text().strip()
        if deploy_url:
            db.update_tenant_deploy_url(tenant_id, deploy_url)

    @staticmethod
    async def _forward_messages(outbound_path: Path, messenger, tenant_external_id: str) -> None:
        for line in outbound_path.read_text().splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(payload.get("text", "")).strip()
            if text:
                await messenger.send_text(tenant_external_id, text)

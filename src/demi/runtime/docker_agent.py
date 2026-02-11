from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from typing import Any

from demi.agent.claude import AgentResult
from demi.agent.claude import ClaudeAgent
from demi.config import Settings
from demi.runtime.docker_pool import DockerPool

INTERACTION_SESSION_NAMESPACE = "interaction"
# Forwarded execution updates are instruction-mode interaction messages, so
# they should not reuse routing-mode session context.
INTERACTION_SESSION_KEY = "claude_instruction_session"


def load_env_file_values(settings: Settings) -> dict[str, str]:
    env_file = settings.model_config.get("env_file")
    if not env_file:
        return {}
    if isinstance(env_file, (list, tuple)):
        files = list(env_file)
    else:
        files = [env_file]
    values: dict[str, str] = {}
    for entry in files:
        path = Path(entry)
        if not path.is_absolute():
            path = (settings.root_dir / path).resolve()
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if raw.startswith("export "):
                raw = raw[len("export ") :].strip()
            if "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if (
                (value.startswith('"') and value.endswith('"'))
                or (value.startswith("'") and value.endswith("'"))
            ):
                value = value[1:-1]
            if value:
                values[key] = value
    return values


@dataclass
class DockerAgent:
    supports_inflight_stream = False
    supports_file_stream = True
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
        run_id: int | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> AgentResult:
        tenant_root = getattr(workspace, "tenant_root", workspace.root)
        result_path = workspace.tasks_dir / "run_result.json"
        outbound_path = workspace.tasks_dir / "outbound_messages.jsonl"
        if run_id is None:
            raise RuntimeError("run_id_required")

        env = self._build_env(runtime_env)
        slot = self.pool.pop_container_for_workspace(tenant_root)
        if slot:
            try:
                await self.pool.exec_in_container(
                    slot,
                    self._entrypoint_command(run_id),
                    env=env,
                )
            finally:
                await self.pool.retire_container(slot)
        else:
            await self.pool.run_in_fresh_container(
                tenant_root,
                self._entrypoint_command(run_id),
                env=env,
            )

        result = self._read_result(result_path)
        if not result:
            raise RuntimeError("agent_result_missing")
        if result.get("error"):
            raise RuntimeError(str(result.get("error")))
        self._maybe_update_deploy(db, tenant_id, workspace.tasks_dir)
        if self.forward_messages and messenger and outbound_path.exists():
            await self._forward_messages(
                outbound_path,
                workspace=workspace,
                messenger=messenger,
                tenant_id=tenant_id,
                db=db,
                provider=message.provider,
                tenant_external_id=message.tenant_external_id,
                session_id=session_id,
            )
            try:
                outbound_path.unlink()
            except OSError:
                pass
        return AgentResult(
            session_id=result.get("session_id"),
            summary=result.get("summary"),
            total_cost_usd=result.get("total_cost_usd"),
            usage=result.get("usage"),
            stop_reason=result.get("stop_reason"),
            result_subtype=result.get("result_subtype"),
        )

    async def cancel_run(self, workspace) -> int:
        tenant_root = getattr(workspace, "tenant_root", workspace.root)
        return await self.pool.cancel_workspace(tenant_root)

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
        run_id: int | None = None,
        message_id: int | None = None,
        reply_to_message_id: str | None = None,
        reply_to_text: str | None = None,
        asset_paths: list[str] | None = None,
        execution_bridge: Any | None = None,
    ) -> AgentResult | None:
        interaction_agent = ClaudeAgent()
        return await interaction_agent.send_interaction_message(
            workspace=workspace,
            text=text,
            messenger=messenger,
            tenant_id=tenant_id,
            db=db,
            payments=payments,
            session_id=session_id,
            provider=provider,
            tenant_external_id=tenant_external_id,
            run_id=run_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            asset_paths=asset_paths,
            execution_bridge=execution_bridge,
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
        run_id: int | None = None,
        message_id: int | None = None,
        asset_paths: list[str] | None = None,
        execution_bridge: Any | None = None,
    ) -> AgentResult | None:
        interaction_agent = ClaudeAgent()
        return await interaction_agent.send_interaction_instruction(
            workspace=workspace,
            instruction=instruction,
            messenger=messenger,
            tenant_id=tenant_id,
            db=db,
            payments=payments,
            session_id=session_id,
            provider=provider,
            tenant_external_id=tenant_external_id,
            run_id=run_id,
            message_id=message_id,
            asset_paths=asset_paths,
            execution_bridge=execution_bridge,
        )

    async def run_interaction_agent(
        self,
        *,
        workspace,
        message,
        messenger,
        tenant_id: int | None = None,
        db: Any | None = None,
        payments: Any | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        tenant_external_id: str | None = None,
        message_id: int | None = None,
        billing_checked: bool = False,
        asset_paths: list[str] | None = None,
        execution_bridge: Any | None = None,
        inflight_stream=None,
    ):
        interaction_agent = ClaudeAgent()
        runner = getattr(interaction_agent, "run_interaction_agent", None)
        if runner is None:
            runner = interaction_agent.route_interaction
        return await runner(
            workspace=workspace,
            message=message,
            messenger=messenger,
            tenant_id=tenant_id,
            db=db,
            payments=payments,
            session_id=session_id,
            provider=provider,
            tenant_external_id=tenant_external_id,
            message_id=message_id,
            billing_checked=billing_checked,
            asset_paths=asset_paths,
            execution_bridge=execution_bridge,
            inflight_stream=inflight_stream,
        )

    async def route_interaction(
        self,
        *,
        workspace,
        message,
        messenger,
        tenant_id: int | None = None,
        db: Any | None = None,
        payments: Any | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        tenant_external_id: str | None = None,
        message_id: int | None = None,
        billing_checked: bool = False,
        asset_paths: list[str] | None = None,
        execution_bridge: Any | None = None,
        inflight_stream=None,
    ):
        # Backward-compatible alias for older callers.
        return await self.run_interaction_agent(
            workspace=workspace,
            message=message,
            messenger=messenger,
            tenant_id=tenant_id,
            db=db,
            payments=payments,
            session_id=session_id,
            provider=provider,
            tenant_external_id=tenant_external_id,
            message_id=message_id,
            billing_checked=billing_checked,
            asset_paths=asset_paths,
            execution_bridge=execution_bridge,
            inflight_stream=inflight_stream,
        )

    def _entrypoint_command(self, run_id: int) -> list[str]:
        return [
            "python",
            "-m",
            "demi.runtime.agent_entrypoint",
            "--run-id",
            str(int(run_id)),
        ]

    def _build_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        allowlist = self._env_allowlist()
        allow_all = "*" in allowlist
        env: dict[str, str] = {}
        env_file_values = self._load_env_file_values()
        for key, value in env_file_values.items():
            if not value:
                continue
            if not allow_all and key not in allowlist:
                continue
            env[key] = self._rewrite_for_docker(key, value)
        if allow_all:
            # TODO: Consider server-side secret injection instead of forwarding host env into containers.
            blocked = {"PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
            for key, value in os.environ.items():
                if key in blocked:
                    continue
                if not value:
                    continue
                env[key] = self._rewrite_for_docker(key, value)
        for key in allowlist:
            if key == "*":
                continue
            value = os.getenv(key)
            if value:
                env[key] = self._rewrite_for_docker(key, value)
                continue
            value = self._settings_fallback(key)
            if value:
                env[key] = self._rewrite_for_docker(key, value)
        if extra_env:
            env.update(extra_env)
        return env

    @staticmethod
    def _rewrite_for_docker(key: str, value: str) -> str:
        if key != "MAIN_DB_SUPABASE_URL":
            return value
        cleaned = value.strip()
        if not cleaned:
            return value
        for needle in ("http://127.0.0.1", "http://localhost"):
            if cleaned.startswith(needle):
                return cleaned.replace(needle, "http://host.docker.internal", 1)
        return value

    def _env_allowlist(self) -> list[str]:
        raw = self.settings.docker_env_allowlist
        base = [
            "MAIN_DB_SUPABASE_URL",
            "MAIN_DB_SUPABASE_SERVICE_KEY",
            "TELEGRAM_BOT_TOKEN",
            "UNSPLASH_ACCESS_KEY",
            "UNSPLASH_SECRET_KEY",
            "UNSPLASH_APP_ID",
            "FIRECRAWL_API_KEY",
            "FIRECRAWL_NO_TELEMETRY",
            "SUPABASE_API_BASE_URL",
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
            "ASSISTANT_STRIPE_PRICE_ID",
            "PUBLIC_BASE_URL",
            "SUPABASE_ACCESS_TOKEN",
            "SUPABASE_ORG_SLUG",
            "SUPABASE_ORG_ID",
            "SUPABASE_REGION_SELECTION",
            "SUPABASE_REGION",
            "SUPABASE_INSTANCE_SIZE",
            "SUPABASE_PROJECT_PREFIX",
            "GITHUB_ORG",
            "GITHUB_APP_ID",
            "GITHUB_APP_CLIENT_ID",
            "GITHUB_APP_INSTALLATION_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_REPO_PREFIX",
            "GITHUB_REPO_VISIBILITY",
            "GITHUB_API_BASE_URL",
            "CLAUDE_PLUGINS",
            "ANTHROPIC_API_KEY",
            "CLAUDE_API_KEY",
            "EXECUTION_MODEL",
            "INTERACTION_MODEL",
            "INTERACTION_MAX_THINKING_TOKENS",
            "CLAUDE_ENABLE_TOOL_SEARCH",
            "CLAUDE_ENABLE_MEMORY_TOOL",
            "EXECUTION_STREAM_REALTIME_ENABLED",
            "EXECUTION_STREAM_POLL_INTERVAL",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ]
        if raw:
            requested = [entry.strip() for entry in raw.split(",") if entry.strip()]
            # Always include critical auth keys even if a custom allowlist is set.
            critical = [
                "MAIN_DB_SUPABASE_URL",
                "MAIN_DB_SUPABASE_SERVICE_KEY",
                "ANTHROPIC_API_KEY",
                "CLAUDE_API_KEY",
                "LMNR_PROJECT_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
            ]
            allowlist = list(dict.fromkeys(requested + critical))
            return allowlist
        return base

    def _load_env_file_values(self) -> dict[str, str]:
        return load_env_file_values(self.settings)

    def _settings_fallback(self, key: str) -> str | None:
        mapping = {
            "MAIN_DB_SUPABASE_URL": self.settings.main_db_supabase_url,
            "MAIN_DB_SUPABASE_SERVICE_KEY": self.settings.main_db_supabase_service_key,
            "TELEGRAM_BOT_TOKEN": self.settings.telegram_bot_token,
            "UNSPLASH_ACCESS_KEY": self.settings.unsplash_access_key,
            "UNSPLASH_SECRET_KEY": self.settings.unsplash_secret_key,
            "UNSPLASH_APP_ID": self.settings.unsplash_app_id,
            "SUPABASE_API_BASE_URL": self.settings.supabase_api_base_url,
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
            "GITHUB_ORG": self.settings.github_org,
            "GITHUB_APP_ID": self.settings.github_app_id,
            "GITHUB_APP_CLIENT_ID": self.settings.github_app_client_id,
            "GITHUB_APP_INSTALLATION_ID": self.settings.github_app_installation_id,
            "GITHUB_APP_PRIVATE_KEY": self.settings.github_app_private_key,
            "GITHUB_REPO_PREFIX": self.settings.github_repo_prefix,
            "GITHUB_REPO_VISIBILITY": self.settings.github_repo_visibility,
            "GITHUB_API_BASE_URL": self.settings.github_api_base_url,
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
            "CLAUDE_API_KEY": self.settings.claude_api_key,
            "LMNR_PROJECT_API_KEY": self.settings.lmnr_project_api_key,
            "EXECUTION_MODEL": self.settings.execution_model,
            "INTERACTION_MODEL": self.settings.interaction_model,
            "INTERACTION_MAX_THINKING_TOKENS": self.settings.interaction_max_thinking_tokens,
            "CLAUDE_ENABLE_TOOL_SEARCH": self.settings.claude_enable_tool_search,
            "CLAUDE_ENABLE_MEMORY_TOOL": self.settings.claude_enable_memory_tool,
            "EXECUTION_STREAM_REALTIME_ENABLED": self.settings.execution_stream_realtime_enabled,
            "EXECUTION_STREAM_POLL_INTERVAL": self.settings.execution_stream_poll_interval,
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
    async def _forward_messages(
        outbound_path: Path,
        *,
        workspace,
        messenger,
        tenant_id: int | None,
        db: Any | None,
        provider: str | None,
        tenant_external_id: str,
        session_id: str | None,
    ) -> None:
        interaction_agent = ClaudeAgent()
        interaction_session_id = session_id
        if db is not None and tenant_id is not None:
            try:
                payload = db.get_tenant_kv(
                    int(tenant_id),
                    INTERACTION_SESSION_NAMESPACE,
                    INTERACTION_SESSION_KEY,
                ) or {}
                interaction_session_id = str(payload.get("session_id") or "").strip() or None
            except Exception:
                pass
        for line in outbound_path.read_text().splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(payload.get("text", "")).strip()
            if not text:
                continue
            reply_to_message_id = str(payload.get("reply_to_message_id") or "").strip() or None
            reply_to_text = str(payload.get("reply_to_text") or "").strip() or None
            result = await interaction_agent.send_interaction_message(
                workspace=workspace,
                text=text,
                messenger=messenger,
                tenant_id=tenant_id,
                db=db,
                payments=None,
                session_id=interaction_session_id,
                provider=provider,
                tenant_external_id=tenant_external_id,
                reply_to_message_id=reply_to_message_id,
                reply_to_text=reply_to_text,
            )
            session_from_result = str(getattr(result, "session_id", "") or "").strip()
            if session_from_result and db is not None and tenant_id is not None:
                interaction_session_id = session_from_result
                try:
                    db.set_tenant_kv(
                        int(tenant_id),
                        INTERACTION_SESSION_NAMESPACE,
                        INTERACTION_SESSION_KEY,
                        {"session_id": interaction_session_id},
                    )
                except Exception:
                    pass

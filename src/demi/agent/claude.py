from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import mimetypes
from pathlib import Path
import os
import re
from typing import Any, Callable, Literal
from contextlib import suppress
import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, AgentDefinition
from claude_agent_sdk.types import ResultMessage, SystemMessage

from demi.agent.chat_tools import (
    CHAT_SERVER_NAME,
    SEND_MESSAGE_TOOL,
    ChatToolContext,
    build_chat_server,
)
from demi.agent.unsplash_tools import (
    UNSPLASH_SEARCH_TOOL,
    UNSPLASH_SERVER_NAME,
    UnsplashToolContext,
    build_unsplash_server,
)
from demi.agent.supabase_tools import (
    SUPABASE_SERVER_NAME,
    SupabaseToolContext,
    build_supabase_server,
)
from demi.agent.github_tools import (
    GITHUB_SERVER_NAME,
    GitHubToolContext,
    build_github_server,
)
from demi.agent.tool_logging import log_agent_event
from demi.config import Settings
from demi.agent.inflight import InflightTextStream
from demi.models import NormalizedMessage
from demi.runtime.tenant_tooling import bootstrap_tenant_tooling
from demi.workspace.core import Workspace


INTERACTION_IMAGE_MAX_BYTES = 5 * 1024 * 1024
INTERACTION_SESSION_NAMESPACE = "interaction"
INTERACTION_ROUTE_SESSION_KEY = "claude_route_session"
INTERACTION_INSTRUCTION_SESSION_KEY = "claude_instruction_session"
TOOL_SEARCH_ENV_KEY = "ENABLE_TOOL_SEARCH"
MEMORY_TOOL_ENABLED_ENV_KEY = "CLAUDE_CODE_ENABLE_MEMORY_TOOL"
MEMORY_TOOL_PATH_ENV_KEY = "CLAUDE_CODE_MEMORY_FILE_PATH"


@dataclass
class AgentResult:
    session_id: str | None
    summary: str | None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    result_subtype: str | None = None


@dataclass
class InteractionRouteResult:
    decision: dict[str, Any]
    session_id: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    result_subtype: str | None = None


def _sdk_message_log_data(msg: Any) -> dict[str, Any]:
    return {
        "class": msg.__class__.__name__,
        "subtype": getattr(msg, "subtype", None),
    }


def _cli_stderr_logger(tasks_dir: Path, tag: str) -> Callable[[str], None]:
    def _log(line: str) -> None:
        cleaned = line.rstrip()
        if not cleaned:
            return
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000] + "..."
        try:
            log_agent_event(tasks_dir, "cli_stderr", {"tag": tag, "line": cleaned})
        except Exception:
            pass

    return _log


class ClaudeAgent:
    supports_inflight_stream = True
    SUPABASE_MCP_SERVER_NAME = "supabase"
    SUPABASE_MCP_BASE_URL = "https://mcp.supabase.com/mcp"
    CHROME_DEVTOOLS_MCP_SERVER_NAME = "chrome-devtools"
    DEFAULT_ALLOWED_TOOLS = [
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "AskUserQuestion",
        "Task",
        "Skill",
        UNSPLASH_SEARCH_TOOL,
        f"mcp__{CHAT_SERVER_NAME}__decide_project",
        f"mcp__{CHAT_SERVER_NAME}__should_send_message",
        f"mcp__{CHAT_SERVER_NAME}__check_for_status",
        f"mcp__{CHAT_SERVER_NAME}__ack_inflight_updates",
        SEND_MESSAGE_TOOL,
        f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
        f"mcp__{CHAT_SERVER_NAME}__record_deploy",
        f"mcp__{CHAT_SERVER_NAME}__record_domain_quote",
        f"mcp__{CHAT_SERVER_NAME}__record_billing_status",
        f"mcp__{CHAT_SERVER_NAME}__set_testing_mode",
        f"mcp__{CHAT_SERVER_NAME}__register_scheduler_trigger",
        f"mcp__{CHAT_SERVER_NAME}__list_scheduler_triggers",
        f"mcp__{CHAT_SERVER_NAME}__unregister_scheduler_trigger",
        f"mcp__{CHAT_SERVER_NAME}__request_backend_subscription",
        f"mcp__{CHAT_SERVER_NAME}__request_assistant_subscription",
        f"mcp__{GITHUB_SERVER_NAME}__prepare_repo",
        f"mcp__{SUPABASE_SERVER_NAME}__provision_managed_backend",
        f"mcp__{SUPABASE_SERVER_NAME}__upgrade_managed_backend",
        f"mcp__{SUPABASE_MCP_SERVER_NAME}__*",
        f"mcp__{CHROME_DEVTOOLS_MCP_SERVER_NAME}__*",
    ]

    def __init__(
        self,
        allowed_tools: list[str] | None = None,
        permission_mode: str = "acceptEdits",
        system_prompt: str | None = "claude_code",
        setting_sources: list[str] | None = None,
        plugins: list[dict] | None = None,
        agents: dict[str, AgentDefinition] | None = None,
    ):
        settings = Settings()
        if allowed_tools is None:
            base_tools = list(self.DEFAULT_ALLOWED_TOOLS)
            if settings.claude_enable_memory_tool and "Memory" not in base_tools:
                base_tools.append("Memory")
            self.allowed_tools = base_tools
        else:
            self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.setting_sources = setting_sources or ["project", "local"]
        self.plugins = plugins if plugins is not None else self._load_plugins_from_env()
        self.agents = agents or self._default_agents()

    @staticmethod
    def _normalize_stop_reason(raw: Any) -> str | None:
        if raw is None:
            return None
        value = str(raw).strip()
        return value or None

    @staticmethod
    def _normalize_result_subtype(raw: Any) -> str | None:
        if raw is None:
            return None
        value = str(raw).strip()
        return value or None

    @classmethod
    def _stop_reason_status(
        cls,
        stop_reason: str | None,
        result_subtype: str | None,
    ) -> str:
        if result_subtype and result_subtype.startswith("error_"):
            return "error"
        if stop_reason in {"end_turn", "stop_sequence"}:
            return "completed"
        if stop_reason == "refusal":
            return "refusal"
        if stop_reason in {
            "max_tokens",
            "model_context_window_exceeded",
            "pause_turn",
            "tool_use",
        }:
            return "incomplete"
        if stop_reason is None:
            return "unknown"
        return "unexpected"

    @classmethod
    def _stop_reason_error(
        cls,
        stop_reason: str | None,
        result_subtype: str | None,
    ) -> str | None:
        if result_subtype and result_subtype.startswith("error_"):
            return f"agent_result_subtype_{result_subtype}"
        status = cls._stop_reason_status(stop_reason, result_subtype)
        if status == "completed":
            return None
        if status == "refusal":
            return "agent_stop_reason_refusal"
        if status == "incomplete":
            return f"agent_stop_reason_{stop_reason}"
        return None

    @classmethod
    def _record_stop_reason_event(
        cls,
        *,
        tasks_dir: Path,
        db: Any | None,
        tenant_id: int | None,
        context: str,
        stop_reason: str | None,
        result_subtype: str | None,
        session_id: str | None = None,
        run_id: int | None = None,
        message_id: int | None = None,
        project_name: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "context": context,
            "stop_reason": stop_reason,
            "result_subtype": result_subtype,
            "status": cls._stop_reason_status(stop_reason, result_subtype),
        }
        if session_id:
            payload["session_id"] = session_id
        if run_id is not None:
            payload["run_id"] = int(run_id)
        if message_id is not None:
            payload["message_id"] = int(message_id)
        if project_name:
            payload["project_name"] = project_name
        try:
            log_agent_event(tasks_dir, "agent_stop_reason", payload)
        except Exception:
            pass
        if db is not None and tenant_id is not None:
            try:
                db.record_tenant_event(int(tenant_id), "agent_stop_reason", payload)
            except Exception:
                pass

    @classmethod
    def _record_usage_event(
        cls,
        *,
        tasks_dir: Path,
        db: Any | None,
        tenant_id: int | None,
        context: str,
        total_cost_usd: float | None,
        usage: dict[str, Any] | None,
        session_id: str | None = None,
        run_id: int | None = None,
        message_id: int | None = None,
        project_name: str | None = None,
    ) -> None:
        if total_cost_usd is None and not usage:
            return
        payload: dict[str, Any] = {
            "context": context,
            "total_cost_usd": total_cost_usd,
            "usage": usage,
        }
        if session_id:
            payload["session_id"] = session_id
        if run_id is not None:
            payload["run_id"] = int(run_id)
        if message_id is not None:
            payload["message_id"] = int(message_id)
        if project_name:
            payload["project_name"] = project_name
        try:
            log_agent_event(tasks_dir, "agent_usage", payload)
        except Exception:
            pass
        if db is not None and tenant_id is not None:
            try:
                db.record_tenant_event(int(tenant_id), "agent_usage", payload)
            except Exception:
                pass

    @staticmethod
    def _is_resume_error(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        if not text:
            return False
        # Claude CLI resume failures are text-only across SDK layers.
        needles = (
            "resume",
            "invalid session",
            "session not found",
            "could not find session",
            "unknown session",
            "conversation not found",
        )
        return any(needle in text for needle in needles)

    @staticmethod
    def _clear_interaction_session_state(
        *,
        db: Any | None,
        tenant_id: int | None,
    ) -> None:
        if db is None or tenant_id is None:
            return
        for key in (INTERACTION_ROUTE_SESSION_KEY, INTERACTION_INSTRUCTION_SESSION_KEY):
            try:
                db.set_tenant_kv(
                    int(tenant_id),
                    INTERACTION_SESSION_NAMESPACE,
                    key,
                    None,
                )
            except Exception:
                continue

    @staticmethod
    def _load_env_file_values(settings: Settings) -> dict[str, str]:
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

    @staticmethod
    def _base_agent_env(
        *,
        settings: Settings,
        workspace: Workspace,
    ) -> dict[str, str]:
        # Explicitly hydrate SDK subprocess env from .env so interaction/runtime
        # agents do not depend on shell-exported variables.
        env = ClaudeAgent._load_env_file_values(settings)
        if settings.anthropic_api_key:
            # Interaction sessions use tenant-scoped HOME, so auth must come from env
            # instead of relying on per-home Claude CLI login state.
            env["ANTHROPIC_API_KEY"] = str(settings.anthropic_api_key)
        if settings.claude_api_key:
            env["CLAUDE_API_KEY"] = str(settings.claude_api_key)
        if settings.claude_enable_tool_search:
            # Enables MCP Tool Search in Claude Code / Agent SDK.
            env[TOOL_SEARCH_ENV_KEY] = "1"
        if settings.claude_enable_memory_tool:
            # Point memory tool writes at the project-local durable memory file.
            env[MEMORY_TOOL_ENABLED_ENV_KEY] = "true"
            env[MEMORY_TOOL_PATH_ENV_KEY] = str(workspace.memory_path)
        return env

    @classmethod
    def _execution_runtime_env(
        cls,
        *,
        settings: Settings,
        workspace: Workspace,
        runtime_env: dict[str, str] | None,
        tooling_bin_path: Path | None = None,
    ) -> dict[str, str]:
        env = cls._base_agent_env(settings=settings, workspace=workspace)
        if tooling_bin_path:
            env["PATH"] = cls._prepend_path(os.environ.get("PATH"), tooling_bin_path)
        if runtime_env:
            env.update({key: str(value) for key, value in runtime_env.items() if value is not None})
        return env

    @staticmethod
    def _prepend_path(existing: str | None, entry: Path) -> str:
        entry_text = str(entry)
        parts = [part for part in (entry_text, str(existing or "")) if part]
        return ":".join(parts)

    @classmethod
    def _interaction_runtime_env(
        cls,
        *,
        settings: Settings,
        workspace: Workspace,
        tenant_id: int | None,
    ) -> dict[str, str]:
        env = cls._base_agent_env(settings=settings, workspace=workspace)
        if tenant_id is None:
            return env
        # Keep interaction session/cache data tenant-scoped to prevent cross-tenant
        # leakage while still persisting across API/worker container restarts.
        home_dir = settings.resolved_interaction_session_cache_dir() / f"tenant-{int(tenant_id)}"
        try:
            home_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return env
        env["HOME"] = str(home_dir)
        return env

    @staticmethod
    def _interaction_add_dirs(workspace: Workspace) -> list[Path]:
        # Restrict interaction file access to the tenant workspace scope.
        tenant_root = getattr(workspace, "tenant_root", workspace.root)
        return [Path(tenant_root)]

    async def prepare_context(
        self,
        workspace: Workspace,
        task_path: Path,
        message: NormalizedMessage,
        messenger: Any,
        inflight_stream: InflightTextStream | None = None,
        tenant_id: int | None = None,
        db: Any | None = None,
        payments: Any | None = None,
        session_id: str | None = None,
        run_id: int | None = None,
        runtime_env: dict[str, str] | None = None,
    ) -> AgentResult:
        chat_server = build_chat_server(
            ChatToolContext(
                messenger=messenger,
                tenant_external_id=message.tenant_external_id,
                tenant_key=message.tenant_key,
                provider=message.provider,
                tasks_dir=workspace.tasks_dir,
                tenant_id=tenant_id,
                db=db,
                payments=payments,
                role="primary",
                run_id=run_id,
            )
        )
        settings = Settings()
        tooling_result = None
        try:
            tooling_result = await asyncio.to_thread(
                bootstrap_tenant_tooling,
                tenant_root=getattr(workspace, "tenant_root", workspace.root),
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            log_agent_event(
                workspace.tasks_dir,
                "tooling_bootstrap_failed",
                {"error": str(exc)},
            )
        unsplash_server = build_unsplash_server(
            UnsplashToolContext(
                access_key=settings.unsplash_access_key,
                tasks_dir=workspace.tasks_dir,
            )
        )
        supabase_server = build_supabase_server(
            SupabaseToolContext(
                tasks_dir=workspace.tasks_dir,
                db=db,
                tenant_id=tenant_id,
            )
        )
        github_server = build_github_server(
            GitHubToolContext(
                tasks_dir=workspace.tasks_dir,
                runtime_env=runtime_env,
            )
        )
        mcp_servers = {
            CHAT_SERVER_NAME: chat_server,
            UNSPLASH_SERVER_NAME: unsplash_server,
            SUPABASE_SERVER_NAME: supabase_server,
            GITHUB_SERVER_NAME: github_server,
        }
        supabase_mcp = self._build_supabase_mcp_config(
            workspace, settings, db=db, tenant_id=tenant_id
        )
        if supabase_mcp:
            mcp_servers[self.SUPABASE_MCP_SERVER_NAME] = supabase_mcp
        chrome_mcp = self._build_chrome_devtools_mcp_config(
            workspace=workspace,
            settings=settings,
            tenant_id=tenant_id,
            message=message,
        )
        if chrome_mcp:
            mcp_servers[self.CHROME_DEVTOOLS_MCP_SERVER_NAME] = chrome_mcp
        execution_env = self._execution_runtime_env(
            settings=settings,
            workspace=workspace,
            runtime_env=runtime_env,
            tooling_bin_path=tooling_result.bin_path if tooling_result is not None else None,
        )
        options = ClaudeAgentOptions(
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            system_prompt=self.system_prompt,
            setting_sources=self.setting_sources,
            model=settings.execution_model,
            cwd=workspace.root,
            add_dirs=[Path.cwd()],
            env=execution_env,
            plugins=self.plugins,
            agents=self.agents,
            mcp_servers=mcp_servers,
            stderr=_cli_stderr_logger(workspace.tasks_dir, "primary"),
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()

        prompt = self._build_prompt(task_path=task_path, memory_path=workspace.memory_path)
        log_agent_event(
            workspace.tasks_dir,
            "run_start",
            {
                "task_path": str(task_path),
                "session_id": session_id,
                "message": (message.text or "").strip(),
            },
        )
        stop_event = None
        if inflight_stream is not None:
            stop_event = asyncio.Event()
        log_agent_event(
            workspace.tasks_dir,
            "query_start",
            {
                "session_id": session_id or "default",
                "task_path": str(task_path),
            },
        )
        summary = None
        new_session_id = session_id
        total_cost_usd = None
        usage: dict[str, Any] | None = None
        stop_reason: str | None = None
        result_subtype: str | None = None

        query_task: asyncio.Task[None] | None = None
        try:
            query_task = asyncio.create_task(
                client.query(
                    self._prompt_stream(
                        prompt,
                        inflight_stream=inflight_stream,
                        stop_event=stop_event,
                    ),
                    session_id=session_id or "default",
                )
            )
            async for msg in client.receive_messages():
                log_agent_event(
                    workspace.tasks_dir,
                    "sdk_message",
                    _sdk_message_log_data(msg),
                )
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    new_session_id = msg.data.get("session_id", new_session_id)
                if isinstance(msg, ResultMessage):
                    new_session_id = msg.session_id or new_session_id
                    stop_reason = self._normalize_stop_reason(
                        getattr(msg, "stop_reason", None)
                    )
                    result_subtype = self._normalize_result_subtype(
                        getattr(msg, "subtype", None)
                    )
                    self._record_stop_reason_event(
                        tasks_dir=workspace.tasks_dir,
                        db=db,
                        tenant_id=tenant_id,
                        context="prepare_context",
                        stop_reason=stop_reason,
                        result_subtype=result_subtype,
                        session_id=new_session_id,
                        run_id=run_id,
                        project_name=workspace.project_name,
                    )
                    summary = msg.result
                    total_cost_usd = msg.total_cost_usd
                    usage = msg.usage
                    self._record_usage_event(
                        tasks_dir=workspace.tasks_dir,
                        db=db,
                        tenant_id=tenant_id,
                        context="prepare_context",
                        total_cost_usd=total_cost_usd,
                        usage=usage,
                        session_id=new_session_id,
                        run_id=run_id,
                        project_name=workspace.project_name,
                    )
                    stop_reason_error = self._stop_reason_error(stop_reason, result_subtype)
                    if stop_reason_error:
                        raise RuntimeError(stop_reason_error)
                    if stop_event is not None:
                        stop_event.set()
                    break
        finally:
            if stop_event is not None:
                stop_event.set()
            if query_task is not None:
                try:
                    await asyncio.wait_for(query_task, timeout=5.0)
                except asyncio.TimeoutError:
                    query_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await query_task
                except Exception:
                    pass
            log_agent_event(
                workspace.tasks_dir,
                "run_end",
                {
                    "session_id": new_session_id,
                    "summary": (summary or "")[:500],
                    "total_cost_usd": total_cost_usd,
                    "usage": usage,
                    "stop_reason": stop_reason,
                    "result_subtype": result_subtype,
                },
            )
            await client.disconnect()

        return AgentResult(
            session_id=new_session_id,
            summary=summary,
            total_cost_usd=total_cost_usd,
            usage=usage,
            stop_reason=stop_reason,
            result_subtype=result_subtype,
        )

    async def send_interaction_message(
        self,
        workspace: Workspace,
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
        settings = Settings()
        interaction_prompt = self._load_prompt_file(settings.interaction_prompt_path)
        interaction_env = self._interaction_runtime_env(
            settings=settings,
            workspace=workspace,
            tenant_id=tenant_id,
        )
        chat_server = build_chat_server(
            ChatToolContext(
                messenger=messenger,
                tenant_external_id=str(tenant_external_id or ""),
                tenant_key=(
                    f"{provider}:{tenant_external_id}" if provider and tenant_external_id else None
                ),
                provider=provider,
                tasks_dir=workspace.tasks_dir,
                tenant_id=tenant_id,
                db=db,
                payments=payments,
                execution_bridge=execution_bridge,
                role="interaction",
                run_id=run_id,
                message_id=message_id,
            )
        )

        async def _run_once(resume_session_id: str | None) -> AgentResult:
            # Interaction turns use SDK `resume` so the same tenant keeps conversational state.
            allowed_tools = [
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                f"mcp__{CHAT_SERVER_NAME}__find_execution_agent",
                f"mcp__{CHAT_SERVER_NAME}__stream_to_execution_agent",
                f"mcp__{CHAT_SERVER_NAME}__stop_execution_agent",
                SEND_MESSAGE_TOOL,
                f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
                f"mcp__{CHAT_SERVER_NAME}__set_testing_mode",
            ]
            if settings.claude_enable_memory_tool:
                allowed_tools.append("Memory")
            options = ClaudeAgentOptions(
                allowed_tools=allowed_tools,
                permission_mode=self.permission_mode,
                system_prompt=interaction_prompt,
                setting_sources=self.setting_sources,
                model=settings.interaction_model,
                max_thinking_tokens=settings.interaction_max_thinking_tokens,
                resume=resume_session_id or None,
                cwd=workspace.root,
                add_dirs=self._interaction_add_dirs(workspace),
                env=interaction_env,
                plugins=self.plugins,
                agents=None,
                mcp_servers={CHAT_SERVER_NAME: chat_server},
                stderr=_cli_stderr_logger(workspace.tasks_dir, "interaction_update"),
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            total_cost_usd = None
            usage: dict[str, Any] | None = None
            stop_reason: str | None = None
            result_subtype: str | None = None
            new_session_id = resume_session_id
            try:
                reply_id = str(reply_to_message_id or "").strip()
                reply_text = str(reply_to_text or "").strip()
                prompt = (
                    "Send the following user update if it fits the current context. "
                    "If it would be redundant, do not send.\n"
                )
                if reply_id or reply_text:
                    prompt += (
                        "If you send a message, use send_message with reply_to_message_id/reply_to_text "
                        "from the reply context.\n"
                        f"Reply context: id={reply_id or 'n/a'} "
                        f"text={reply_text or 'n/a'}\n"
                    )
                prompt += f"\nUPDATE:\n{text}"
                await client.query(
                    self._interaction_prompt_stream(prompt, asset_paths=asset_paths),
                    session_id=resume_session_id or "interaction",
                )
                async for msg in client.receive_messages():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        new_session_id = msg.data.get("session_id", new_session_id)
                    if isinstance(msg, ResultMessage):
                        new_session_id = msg.session_id or new_session_id
                        stop_reason = self._normalize_stop_reason(getattr(msg, "stop_reason", None))
                        result_subtype = self._normalize_result_subtype(getattr(msg, "subtype", None))
                        self._record_stop_reason_event(
                            tasks_dir=workspace.tasks_dir,
                            db=db,
                            tenant_id=tenant_id,
                            context="send_interaction_message",
                            stop_reason=stop_reason,
                            result_subtype=result_subtype,
                            session_id=new_session_id,
                            run_id=run_id,
                            message_id=message_id,
                            project_name=workspace.project_name,
                        )
                        total_cost_usd = msg.total_cost_usd
                        usage = msg.usage
                        self._record_usage_event(
                            tasks_dir=workspace.tasks_dir,
                            db=db,
                            tenant_id=tenant_id,
                            context="send_interaction_message",
                            total_cost_usd=total_cost_usd,
                            usage=usage,
                            session_id=new_session_id,
                            run_id=run_id,
                            message_id=message_id,
                            project_name=workspace.project_name,
                        )
                        break
            finally:
                await client.disconnect()
            return AgentResult(
                session_id=new_session_id,
                summary=None,
                total_cost_usd=total_cost_usd,
                usage=usage,
                stop_reason=stop_reason,
                result_subtype=result_subtype,
            )

        try:
            return await _run_once(session_id)
        except Exception as exc:
            if session_id and self._is_resume_error(exc):
                self._clear_interaction_session_state(db=db, tenant_id=tenant_id)
                return await _run_once(None)
            raise

    async def run_interaction_agent(
        self,
        *,
        workspace: Workspace,
        message: NormalizedMessage,
        messenger: Any,
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
        inflight_stream: InflightTextStream | None = None,
    ) -> InteractionRouteResult:
        settings = Settings()
        interaction_prompt = self._load_prompt_file(
            settings.resolved_interaction_routing_prompt_path()
        )
        interaction_env = self._interaction_runtime_env(
            settings=settings,
            workspace=workspace,
            tenant_id=tenant_id,
        )
        stream_close_event = asyncio.Event() if inflight_stream is not None else None

        def _on_interaction_message_sent() -> None:
            if stream_close_event is not None:
                stream_close_event.set()

        chat_server = build_chat_server(
            ChatToolContext(
                messenger=messenger,
                tenant_external_id=str(tenant_external_id or ""),
                tenant_key=(
                    f"{provider}:{tenant_external_id}" if provider and tenant_external_id else None
                ),
                provider=provider,
                tasks_dir=workspace.tasks_dir,
                tenant_id=tenant_id,
                db=db,
                payments=payments,
                execution_bridge=execution_bridge,
                role="interaction",
                message_id=message_id,
                on_interaction_message_sent=_on_interaction_message_sent,
            )
        )
        total_cost_usd = None
        usage: dict[str, Any] | None = None
        decision: dict[str, Any] = {}
        raw_output: str | None = None
        stop_reason: str | None = None
        result_subtype: str | None = None
        new_session_id: str | None = session_id

        def _decision_valid(payload: dict[str, Any]) -> bool:
            if not isinstance(payload, dict):
                return False
            if "should_run" not in payload:
                return False
            return isinstance(payload.get("should_run"), bool)

        def _build_options(resume_session_id: str | None) -> ClaudeAgentOptions:
            allowed_tools = [
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                f"mcp__{CHAT_SERVER_NAME}__decide_project",
                f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                f"mcp__{CHAT_SERVER_NAME}__find_execution_agent",
                f"mcp__{CHAT_SERVER_NAME}__stream_to_execution_agent",
                f"mcp__{CHAT_SERVER_NAME}__stop_execution_agent",
                SEND_MESSAGE_TOOL,
                f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
                f"mcp__{CHAT_SERVER_NAME}__request_assistant_subscription",
                f"mcp__{CHAT_SERVER_NAME}__set_testing_mode",
            ]
            if settings.claude_enable_memory_tool:
                allowed_tools.append("Memory")
            return ClaudeAgentOptions(
                allowed_tools=allowed_tools,
                permission_mode=self.permission_mode,
                system_prompt=interaction_prompt,
                setting_sources=self.setting_sources,
                model=settings.interaction_model,
                max_thinking_tokens=settings.interaction_max_thinking_tokens,
                output_format=self._interaction_agent_output_schema(),
                # Resume only from the tenant-scoped interaction session state.
                resume=resume_session_id or None,
                cwd=workspace.root,
                add_dirs=self._interaction_add_dirs(workspace),
                env=interaction_env,
                plugins=self.plugins,
                agents=None,
                mcp_servers={CHAT_SERVER_NAME: chat_server},
                stderr=_cli_stderr_logger(workspace.tasks_dir, "interaction_agent_routing"),
            )

        async def _query_router(
            *,
            resume_session_id: str | None,
            retry_note: str | None = None,
        ) -> None:
            nonlocal total_cost_usd, usage, decision, raw_output, stop_reason, result_subtype, new_session_id
            options = _build_options(resume_session_id)
            client = ClaudeSDKClient(options=options)
            await client.connect()
            stop_event = asyncio.Event() if inflight_stream is not None else None
            query_task: asyncio.Task[None] | None = None
            try:
                prompt = (
                    "ROUTING MODE\n"
                    "Handle the incoming user message. "
                    "Send any user reply if needed, then output only the routing JSON decision.\n"
                    "Use the interaction context files and tools as needed.\n\n"
                    f"Billing check already performed: {billing_checked}.\n"
                )
                if retry_note:
                    prompt += f"\n{retry_note}\n"
                query_task = asyncio.create_task(
                    client.query(
                        self._interaction_prompt_stream(
                            prompt,
                            asset_paths=asset_paths,
                            inflight_stream=inflight_stream,
                            stop_event=stop_event,
                            close_event=stream_close_event,
                        ),
                        session_id=resume_session_id or "interaction",
                    )
                )
                async for msg in client.receive_messages():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        new_session_id = msg.data.get("session_id", new_session_id)
                    if isinstance(msg, ResultMessage):
                        new_session_id = msg.session_id or new_session_id
                        stop_reason = self._normalize_stop_reason(getattr(msg, "stop_reason", None))
                        result_subtype = self._normalize_result_subtype(
                            getattr(msg, "subtype", None)
                        )
                        self._record_stop_reason_event(
                            tasks_dir=workspace.tasks_dir,
                            db=db,
                            tenant_id=tenant_id,
                            context="run_interaction_agent",
                            stop_reason=stop_reason,
                            result_subtype=result_subtype,
                            session_id=new_session_id,
                            message_id=message_id,
                            project_name=workspace.project_name,
                        )
                        total_cost_usd = msg.total_cost_usd
                        usage = msg.usage
                        self._record_usage_event(
                            tasks_dir=workspace.tasks_dir,
                            db=db,
                            tenant_id=tenant_id,
                            context="run_interaction_agent",
                            total_cost_usd=total_cost_usd,
                            usage=usage,
                            session_id=new_session_id,
                            message_id=message_id,
                            project_name=workspace.project_name,
                        )
                        raw_output = msg.result
                        structured_output = getattr(msg, "structured_output", None)
                        if isinstance(structured_output, dict):
                            decision = dict(structured_output)
                        else:
                            decision = self._parse_interaction_agent_json(msg.result)
                        if stop_event is not None:
                            stop_event.set()
                        break
            finally:
                if stop_event is not None:
                    stop_event.set()
                if query_task is not None:
                    try:
                        await asyncio.wait_for(query_task, timeout=5.0)
                    except asyncio.TimeoutError:
                        query_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await query_task
                    except Exception:
                        pass
                await client.disconnect()

        async def _query_router_with_resume(
            *,
            resume_session_id: str | None,
            retry_note: str | None = None,
        ) -> None:
            nonlocal new_session_id
            try:
                await _query_router(
                    resume_session_id=resume_session_id,
                    retry_note=retry_note,
                )
            except Exception as exc:
                if resume_session_id and self._is_resume_error(exc):
                    self._clear_interaction_session_state(db=db, tenant_id=tenant_id)
                    new_session_id = None
                    await _query_router(
                        resume_session_id=None,
                        retry_note=retry_note,
                    )
                else:
                    raise

        max_attempts = max(1, int(settings.interaction_routing_max_retries()) + 1)
        attempt = 1
        retry_note: str | None = None
        resume_session_id = session_id
        while True:
            await _query_router_with_resume(
                resume_session_id=resume_session_id,
                retry_note=retry_note,
            )
            if _decision_valid(decision):
                break
            if attempt >= max_attempts:
                raise RuntimeError("interaction_agent_invalid_output")
            attempt += 1
            resume_session_id = new_session_id
            retry_note = (
                "Your previous output was invalid. "
                "Return ONLY valid JSON with required boolean field `should_run` "
                "and do not include any prose or formatting.\n"
                f"Attempt {attempt}/{max_attempts}.\n"
                f"Previous output:\n{raw_output}"
            )
            try:
                log_agent_event(
                    workspace.tasks_dir,
                    "interaction_agent_retry",
                    {
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "raw_output": raw_output,
                    },
                )
            except Exception:
                pass
        return InteractionRouteResult(
            decision=decision,
            session_id=new_session_id,
            total_cost_usd=total_cost_usd,
            usage=usage,
            stop_reason=stop_reason,
            result_subtype=result_subtype,
        )

    async def route_interaction(
        self,
        *,
        workspace: Workspace,
        message: NormalizedMessage,
        messenger: Any,
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
        inflight_stream: InflightTextStream | None = None,
    ) -> InteractionRouteResult:
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

    async def send_interaction_instruction(
        self,
        workspace: Workspace,
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
        settings = Settings()
        interaction_prompt = self._load_prompt_file(settings.interaction_prompt_path)
        interaction_env = self._interaction_runtime_env(
            settings=settings,
            workspace=workspace,
            tenant_id=tenant_id,
        )
        chat_server = build_chat_server(
            ChatToolContext(
                messenger=messenger,
                tenant_external_id=str(tenant_external_id or ""),
                tenant_key=(
                    f"{provider}:{tenant_external_id}" if provider and tenant_external_id else None
                ),
                provider=provider,
                tasks_dir=workspace.tasks_dir,
                tenant_id=tenant_id,
                db=db,
                payments=payments,
                execution_bridge=execution_bridge,
                role="interaction",
                run_id=run_id,
                message_id=message_id,
            )
        )

        async def _run_once(resume_session_id: str | None) -> AgentResult:
            # Interaction turns use SDK `resume` so the same tenant keeps conversational state.
            allowed_tools = [
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                f"mcp__{CHAT_SERVER_NAME}__find_execution_agent",
                f"mcp__{CHAT_SERVER_NAME}__stream_to_execution_agent",
                f"mcp__{CHAT_SERVER_NAME}__stop_execution_agent",
                SEND_MESSAGE_TOOL,
                f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
                f"mcp__{CHAT_SERVER_NAME}__request_assistant_subscription",
                f"mcp__{CHAT_SERVER_NAME}__set_testing_mode",
            ]
            if settings.claude_enable_memory_tool:
                allowed_tools.append("Memory")
            options = ClaudeAgentOptions(
                allowed_tools=allowed_tools,
                permission_mode=self.permission_mode,
                system_prompt=interaction_prompt,
                setting_sources=self.setting_sources,
                model=settings.interaction_model,
                max_thinking_tokens=settings.interaction_max_thinking_tokens,
                resume=resume_session_id or None,
                cwd=workspace.root,
                add_dirs=self._interaction_add_dirs(workspace),
                env=interaction_env,
                plugins=self.plugins,
                agents=None,
                mcp_servers={CHAT_SERVER_NAME: chat_server},
                stderr=_cli_stderr_logger(workspace.tasks_dir, "interaction_instruction"),
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            total_cost_usd = None
            usage: dict[str, Any] | None = None
            stop_reason: str | None = None
            result_subtype: str | None = None
            new_session_id = resume_session_id
            try:
                prompt = (
                    "Follow the instruction below. If sending a message would be redundant, "
                    "do not send anything.\n\n"
                    f"INSTRUCTION:\n{instruction}"
                )
                await client.query(
                    self._interaction_prompt_stream(prompt, asset_paths=asset_paths),
                    session_id=resume_session_id or "interaction",
                )
                async for msg in client.receive_messages():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        new_session_id = msg.data.get("session_id", new_session_id)
                    if isinstance(msg, ResultMessage):
                        new_session_id = msg.session_id or new_session_id
                        stop_reason = self._normalize_stop_reason(getattr(msg, "stop_reason", None))
                        result_subtype = self._normalize_result_subtype(getattr(msg, "subtype", None))
                        self._record_stop_reason_event(
                            tasks_dir=workspace.tasks_dir,
                            db=db,
                            tenant_id=tenant_id,
                            context="send_interaction_instruction",
                            stop_reason=stop_reason,
                            result_subtype=result_subtype,
                            session_id=new_session_id,
                            run_id=run_id,
                            message_id=message_id,
                            project_name=workspace.project_name,
                        )
                        total_cost_usd = msg.total_cost_usd
                        usage = msg.usage
                        self._record_usage_event(
                            tasks_dir=workspace.tasks_dir,
                            db=db,
                            tenant_id=tenant_id,
                            context="send_interaction_instruction",
                            total_cost_usd=total_cost_usd,
                            usage=usage,
                            session_id=new_session_id,
                            run_id=run_id,
                            message_id=message_id,
                            project_name=workspace.project_name,
                        )
                        break
            finally:
                await client.disconnect()
            return AgentResult(
                session_id=new_session_id,
                summary=None,
                total_cost_usd=total_cost_usd,
                usage=usage,
                stop_reason=stop_reason,
                result_subtype=result_subtype,
            )

        try:
            return await _run_once(session_id)
        except Exception as exc:
            if session_id and self._is_resume_error(exc):
                self._clear_interaction_session_state(db=db, tenant_id=tenant_id)
                return await _run_once(None)
            raise

    @staticmethod
    def _parse_interaction_agent_json(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _interaction_agent_output_schema() -> dict[str, Any]:
        # Enforce structured interaction-agent output so invalid free-form prose
        # does not break orchestration during routing decisions.
        return {
            "type": "json_schema",
            "name": "interaction_agent_decision",
            "schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "project_name": {"type": ["string", "null"]},
                    "should_run": {"type": "boolean"},
                    "queue_run": {"type": "boolean"},
                    "supersede_active_run": {"type": "boolean"},
                    "dedupe": {"type": "boolean"},
                    "reply_sent": {"type": "boolean"},
                    "facts_only": {"type": "boolean"},
                    "billing_check": {"type": "boolean"},
                    "billing_checked": {"type": "boolean"},
                    "purpose": {"type": "string"},
                    "plan": {"type": "string"},
                    "repo_name": {"type": "string"},
                    "ask_questions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["should_run"],
                "additionalProperties": True,
            },
        }

    @staticmethod
    def _parse_router_json(raw: str | None) -> dict[str, Any]:
        # Backward-compatible alias for older call sites/tests.
        return ClaudeAgent._parse_interaction_agent_json(raw)

    @staticmethod
    def _router_output_schema() -> dict[str, Any]:
        # Backward-compatible alias for older call sites/tests.
        return ClaudeAgent._interaction_agent_output_schema()

    def _build_supabase_mcp_config(
        self,
        workspace: Workspace,
        settings: Settings,
        *,
        db: Any | None = None,
        tenant_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not settings.supabase_access_token:
            return None
        payload = self._load_supabase_project_state(workspace, db=db, tenant_id=tenant_id)
        if not payload:
            return None
        project_ref = str(payload.get("project_ref") or "").strip()
        if not project_ref:
            return None
        url = f"{self.SUPABASE_MCP_BASE_URL}?project_ref={project_ref}&read_only=true"
        return {
            "type": "http",
            "url": url,
            "headers": {"Authorization": f"Bearer {settings.supabase_access_token}"},
        }

    def _build_chrome_devtools_mcp_config(
        self,
        workspace: Workspace,
        settings: Settings,
        tenant_id: int | None,
        message: NormalizedMessage,
    ) -> dict[str, Any] | None:
        if not settings.chrome_devtools_mcp_enabled:
            return None

        tenant_slug = self._chrome_tenant_slug(tenant_id, message)
        base_dir = self._resolve_tenant_path(
            workspace, settings.chrome_devtools_mcp_profile_dir
        )
        profile_dir = base_dir / tenant_slug
        profile_dir.mkdir(parents=True, exist_ok=True)

        command = settings.chrome_devtools_mcp_command
        args = []
        if Path(command).name == "npx":
            args.append("-y")
        args.append(settings.chrome_devtools_mcp_package)
        if settings.chrome_devtools_mcp_headless:
            args.append("--headless=true")
        if settings.chrome_devtools_mcp_executable_path:
            args.append(
                f"--executablePath={settings.chrome_devtools_mcp_executable_path}"
            )
        args.append(f"--userDataDir={profile_dir}")

        return {
            "command": command,
            "args": args,
        }

    @staticmethod
    def _load_supabase_project_state(
        workspace: Workspace, *, db: Any | None = None, tenant_id: int | None = None
    ) -> dict[str, Any] | None:
        if db is not None and tenant_id is not None:
            try:
                payload = db.get_tenant_kv(tenant_id, "supabase", "project")
            except Exception:
                payload = None
            if payload:
                return payload
        path = workspace.tasks_dir / "supabase_project.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _build_prompt(task_path: Path, memory_path: Path) -> str:
        settings = Settings()
        template = ClaudeAgent._load_prompt_file(settings.claude_prompt_path)
        event_url = settings.event_url or "not configured"
        memory_snapshot = ClaudeAgent._read_memory_snapshot(memory_path)
        return (
            template.replace("<<TASK_PATH>>", str(task_path))
            .replace("<<MEMORY_PATH>>", str(memory_path))
            .replace("<<MEMORY_SNAPSHOT>>", memory_snapshot)
            .replace("<<EVENT_URL>>", event_url)
            .rstrip()
            + "\n"
        )

    @staticmethod
    def _read_memory_snapshot(memory_path: Path, max_bytes: int = 8000) -> str:
        try:
            content = memory_path.read_text(encoding="utf-8")
        except OSError:
            return "(memory missing)"
        content = content.strip()
        if not content:
            return "(memory empty)"
        if len(content) > max_bytes:
            trimmed = content[:max_bytes].rstrip()
            return f"{trimmed}\n\n...[truncated]"
        return content

    @staticmethod
    def _build_interaction_content(
        prompt: str,
        asset_paths: list[str] | None = None,
    ) -> str | list[dict[str, Any]]:
        assets = asset_paths or []
        if not assets:
            return prompt
        notes: list[str] = []
        images: list[dict[str, Any]] = []
        for raw_path in assets:
            path = Path(str(raw_path)).expanduser()
            if not path.exists():
                notes.append(f"- {raw_path} (missing)")
                continue
            media_type, _ = mimetypes.guess_type(str(path))
            if not media_type or not media_type.startswith("image/"):
                notes.append(f"- {path}")
                continue
            try:
                data = path.read_bytes()
            except OSError:
                notes.append(f"- {path} (unreadable)")
                continue
            if len(data) > INTERACTION_IMAGE_MAX_BYTES:
                notes.append(f"- {path} (too large)")
                continue
            images.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        text = prompt
        if images:
            text = f"{text}\n\nUser attachments are included below."
        if notes:
            text = f"{text}\n\nAttachments:\n" + "\n".join(notes)
        if not images:
            return text
        return [{"type": "text", "text": text}] + images

    @staticmethod
    async def _interaction_prompt_stream(
        prompt: str,
        asset_paths: list[str] | None = None,
        inflight_stream: InflightTextStream | None = None,
        stop_event=None,
        close_event=None,
    ):
        content = ClaudeAgent._build_interaction_content(prompt, asset_paths)
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
        if inflight_stream is None:
            return
        import time

        start = time.monotonic()
        max_window = 1800.0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if close_event is not None and close_event.is_set():
                break
            if time.monotonic() - start >= max_window:
                break
            try:
                update = await asyncio.wait_for(inflight_stream.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if update is None:
                break
            text = str(update).strip()
            if not text:
                continue
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "IN-FLIGHT UPDATE (new user message while routing; "
                        "incorporate this before final response): "
                        f"{text}"
                    ),
                },
            }
        inflight_stream.accepting = False

    @staticmethod
    async def _prompt_stream(
        prompt: str,
        inflight_stream: InflightTextStream | None = None,
        stop_event=None,
    ):
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        if inflight_stream is None:
            return
        import asyncio
        import time

        start = time.monotonic()
        max_window = 3600.0

        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if time.monotonic() - start >= max_window:
                break
            try:
                update = await asyncio.wait_for(inflight_stream.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if update is None:
                break
            text = str(update).strip()
            if not text:
                continue
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "IN-FLIGHT UPDATE (new user request while you're working; "
                        "acknowledge via interaction agent, then continue current task): "
                        f"{text}"
                    ),
                },
            }

        inflight_stream.accepting = False

    @staticmethod
    def _default_agents() -> dict[str, AgentDefinition]:
        settings = Settings()
        interaction_prompt = ClaudeAgent._load_prompt_file(settings.interaction_prompt_path)
        interaction_subagent_model = ClaudeAgent._agent_definition_model(
            settings.interaction_model
        )
        interaction_tools = [
            "Read",
            "Write",
            "Edit",
            "Grep",
            "Glob",
            f"mcp__{CHAT_SERVER_NAME}__check_for_status",
            f"mcp__{CHAT_SERVER_NAME}__should_send_message",
            f"mcp__{CHAT_SERVER_NAME}__find_execution_agent",
            f"mcp__{CHAT_SERVER_NAME}__stream_to_execution_agent",
            f"mcp__{CHAT_SERVER_NAME}__stop_execution_agent",
            SEND_MESSAGE_TOOL,
            f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
        ]
        if settings.claude_enable_memory_tool:
            interaction_tools.append("Memory")
        return {
            "interaction-helper": AgentDefinition(
                description="Creates friendly, concise user-facing chat updates and questions.",
                prompt=interaction_prompt,
                tools=interaction_tools,
                model=interaction_subagent_model,
            )
        }

    @staticmethod
    def _agent_definition_model(
        model_name: str | None,
    ) -> Literal["sonnet", "opus", "haiku"] | None:
        value = str(model_name or "").strip().lower()
        if not value:
            return None
        if "opus" in value:
            return "opus"
        if "sonnet" in value:
            return "sonnet"
        if "haiku" in value:
            return "haiku"
        return None

    @staticmethod
    def _load_prompt_file(path: Path) -> str:
        settings = Settings()
        resolved = path
        if resolved.is_absolute():
            return resolved.read_text(encoding="utf-8")

        candidate = (settings.root_dir / path).resolve()
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")

        base = Path(__file__).resolve()
        for parent in base.parents:
            fallback = (parent / path).resolve()
            if fallback.exists():
                return fallback.read_text(encoding="utf-8")
            if (parent / "pyproject.toml").exists():
                break

        raise FileNotFoundError(f"Prompt file not found: {path}")

    @staticmethod
    def _resolve_tenant_path(workspace: Workspace, path: Path) -> Path:
        if path.is_absolute():
            return path
        return (workspace.tenant_root / path).resolve()

    @staticmethod
    def _chrome_tenant_slug(tenant_id: int | None, message: NormalizedMessage) -> str:
        if tenant_id is not None:
            raw = f"tenant-{tenant_id}"
        else:
            raw = message.tenant_key or message.tenant_external_id or "tenant-unknown"
        cleaned = re.sub(r"[^a-z0-9._-]+", "-", raw.strip().lower())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
        return cleaned or "tenant-unknown"

    @staticmethod
    def _load_plugins_from_env() -> list[dict]:
        raw = os.getenv("CLAUDE_PLUGINS", "")
        if not raw:
            return []
        configs: list[dict] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            path = Path(entry).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            if not path.exists():
                continue
            configs.append({"type": "local", "path": str(path)})
        return configs

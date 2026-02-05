from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import os
import re
from typing import Any
from contextlib import asynccontextmanager
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
from demi.workspace.core import Workspace


_ENV_LOCK = asyncio.Lock()


@asynccontextmanager
async def _apply_runtime_env(runtime_env: dict[str, str] | None):
    if not runtime_env:
        yield
        return
    async with _ENV_LOCK:
        previous = {key: os.environ.get(key) for key in runtime_env}
        os.environ.update({key: str(value) for key, value in runtime_env.items()})
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@dataclass
class AgentResult:
    session_id: str | None
    summary: str | None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


@dataclass
class InteractionRouteResult:
    decision: dict[str, Any]
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


def _sdk_message_log_data(msg: Any) -> dict[str, Any]:
    return {
        "class": msg.__class__.__name__,
        "subtype": getattr(msg, "subtype", None),
    }


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
        self.allowed_tools = allowed_tools or self.DEFAULT_ALLOWED_TOOLS
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.setting_sources = setting_sources or ["project", "local"]
        self.plugins = plugins if plugins is not None else self._load_plugins_from_env()
        self.agents = agents or self._default_agents()

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
        options = ClaudeAgentOptions(
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            system_prompt=self.system_prompt,
            setting_sources=self.setting_sources,
            cwd=workspace.root,
            add_dirs=[Path.cwd()],
            plugins=self.plugins,
            agents=self.agents,
            mcp_servers=mcp_servers,
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

        try:
            async with _apply_runtime_env(runtime_env):
                await client.query(
                    self._prompt_stream(
                        prompt, inflight_stream=inflight_stream, stop_event=stop_event
                    ),
                    session_id=session_id or "default",
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
                        summary = msg.result
                        total_cost_usd = msg.total_cost_usd
                        usage = msg.usage
                        if stop_event is not None:
                            stop_event.set()
                        break
        finally:
            log_agent_event(
                workspace.tasks_dir,
                "run_end",
                {
                    "session_id": new_session_id,
                    "summary": (summary or "")[:500],
                    "total_cost_usd": total_cost_usd,
                    "usage": usage,
                },
            )
            await client.disconnect()

        return AgentResult(
            session_id=new_session_id,
            summary=summary,
            total_cost_usd=total_cost_usd,
            usage=usage,
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
    ) -> AgentResult | None:
        interaction_prompt = self._load_prompt_file(Settings().interaction_prompt_path)
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
                role="interaction",
                run_id=run_id,
                message_id=message_id,
            )
        )
        options = ClaudeAgentOptions(
            allowed_tools=[
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                SEND_MESSAGE_TOOL,
                f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
            ],
            permission_mode=self.permission_mode,
            system_prompt=interaction_prompt,
            setting_sources=self.setting_sources,
            cwd=workspace.root,
            add_dirs=[Path.cwd()],
            plugins=self.plugins,
            agents=None,
            mcp_servers={CHAT_SERVER_NAME: chat_server},
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        total_cost_usd = None
        usage: dict[str, Any] | None = None
        try:
            reply_to_message_id = str(reply_to_message_id or "").strip()
            reply_to_text = str(reply_to_text or "").strip()
            prompt = (
                "Send the following user update if it fits the current context. "
                "If it would be redundant, do not send.\n"
            )
            if reply_to_message_id or reply_to_text:
                prompt += (
                    "If you send a message, use send_message with reply_to_message_id/reply_to_text "
                    "from the reply context.\n"
                    f"Reply context: id={reply_to_message_id or 'n/a'} "
                    f"text={reply_to_text or 'n/a'}\n"
                )
            prompt += f"\nUPDATE:\n{text}"
            await client.query(prompt, session_id=session_id or "interaction")
            async for msg in client.receive_messages():
                if isinstance(msg, ResultMessage):
                    total_cost_usd = msg.total_cost_usd
                    usage = msg.usage
                    break
        finally:
            await client.disconnect()
        return AgentResult(
            session_id=None,
            summary=None,
            total_cost_usd=total_cost_usd,
            usage=usage,
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
    ) -> InteractionRouteResult:
        settings = Settings()
        interaction_prompt = self._load_prompt_file(settings.interaction_router_prompt_path)
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
                role="interaction",
                message_id=message_id,
            )
        )
        options = ClaudeAgentOptions(
            allowed_tools=[
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                f"mcp__{CHAT_SERVER_NAME}__decide_project",
                f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                SEND_MESSAGE_TOOL,
                f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
                f"mcp__{CHAT_SERVER_NAME}__request_assistant_subscription",
            ],
            permission_mode=self.permission_mode,
            system_prompt=interaction_prompt,
            setting_sources=self.setting_sources,
            cwd=workspace.root,
            add_dirs=[Path.cwd()],
            plugins=self.plugins,
            agents=None,
            mcp_servers={CHAT_SERVER_NAME: chat_server},
        )
        total_cost_usd = None
        usage: dict[str, Any] | None = None
        decision: dict[str, Any] = {}
        raw_output: str | None = None

        def _decision_valid(payload: dict[str, Any]) -> bool:
            if not isinstance(payload, dict):
                return False
            if "should_run" not in payload:
                return False
            return isinstance(payload.get("should_run"), bool)

        async def _query_router(retry_note: str | None = None) -> None:
            nonlocal total_cost_usd, usage, decision, raw_output
            client = ClaudeSDKClient(options=options)
            await client.connect()
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
                await client.query(prompt, session_id=session_id or "interaction-router")
                async for msg in client.receive_messages():
                    if isinstance(msg, ResultMessage):
                        total_cost_usd = msg.total_cost_usd
                        usage = msg.usage
                        raw_output = msg.result
                        decision = self._parse_router_json(msg.result)
                        break
            finally:
                await client.disconnect()

        await _query_router()
        if not _decision_valid(decision):
            retry_note = (
                "Your previous output was invalid. "
                "Return ONLY valid JSON with required boolean field `should_run` "
                "and do not include any prose or formatting.\n"
                f"Previous output:\n{raw_output}"
            )
            await _query_router(retry_note=retry_note)
            if not _decision_valid(decision):
                raise RuntimeError("interaction_router_invalid_output")
        return InteractionRouteResult(
            decision=decision,
            total_cost_usd=total_cost_usd,
            usage=usage,
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
    ) -> AgentResult | None:
        interaction_prompt = self._load_prompt_file(Settings().interaction_prompt_path)
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
                role="interaction",
                run_id=run_id,
                message_id=message_id,
            )
        )
        options = ClaudeAgentOptions(
            allowed_tools=[
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                SEND_MESSAGE_TOOL,
                f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
                f"mcp__{CHAT_SERVER_NAME}__request_assistant_subscription",
            ],
            permission_mode=self.permission_mode,
            system_prompt=interaction_prompt,
            setting_sources=self.setting_sources,
            cwd=workspace.root,
            add_dirs=[Path.cwd()],
            plugins=self.plugins,
            agents=None,
            mcp_servers={CHAT_SERVER_NAME: chat_server},
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        total_cost_usd = None
        usage: dict[str, Any] | None = None
        try:
            prompt = (
                "Follow the instruction below. If sending a message would be redundant, "
                "do not send anything.\n\n"
                f"INSTRUCTION:\n{instruction}"
            )
            await client.query(prompt, session_id=session_id or "interaction")
            async for msg in client.receive_messages():
                if isinstance(msg, ResultMessage):
                    total_cost_usd = msg.total_cost_usd
                    usage = msg.usage
                    break
        finally:
            await client.disconnect()
        return AgentResult(
            session_id=None,
            summary=None,
            total_cost_usd=total_cost_usd,
            usage=usage,
        )

    @staticmethod
    def _parse_router_json(raw: str | None) -> dict[str, Any]:
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
                        "acknowledge via interaction-agent, then continue current task): "
                        f"{text}"
                    ),
                },
            }

        inflight_stream.accepting = False

    @staticmethod
    def _default_agents() -> dict[str, AgentDefinition]:
        settings = Settings()
        interaction_prompt = ClaudeAgent._load_prompt_file(settings.interaction_prompt_path)
        return {
            "interaction-agent": AgentDefinition(
                description="Creates friendly, concise user-facing chat updates and questions.",
                prompt=interaction_prompt,
                tools=[
                    "Read",
                    "Write",
                    "Edit",
                    "Grep",
                    "Glob",
                    f"mcp__{CHAT_SERVER_NAME}__check_for_status",
                    f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                    SEND_MESSAGE_TOOL,
                    f"mcp__{CHAT_SERVER_NAME}__send_payment_link",
                ],
            )
        }

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

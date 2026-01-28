from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, AgentDefinition
from claude_agent_sdk.types import ResultMessage, SystemMessage

from claudius.agent.chat_tools import (
    CHAT_SERVER_NAME,
    SEND_MESSAGE_TOOL,
    ChatToolContext,
    build_chat_server,
)
from claudius.agent.unsplash_tools import (
    UNSPLASH_SEARCH_TOOL,
    UNSPLASH_SERVER_NAME,
    UnsplashToolContext,
    build_unsplash_server,
)
from claudius.agent.tool_logging import log_agent_event
from claudius.config import Settings
from claudius.agent.inflight import InflightTextStream
from claudius.models import NormalizedMessage
from claudius.workspace.core import Workspace


@dataclass
class AgentResult:
    session_id: str | None
    summary: str | None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


class ClaudeAgent:
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
        f"mcp__{CHAT_SERVER_NAME}__should_send_message",
        SEND_MESSAGE_TOOL,
        f"mcp__{CHAT_SERVER_NAME}__record_deploy",
        f"mcp__{CHAT_SERVER_NAME}__record_domain_quote",
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
    ) -> AgentResult:
        chat_server = build_chat_server(
            ChatToolContext(
                messenger=messenger,
                tenant_external_id=message.tenant_external_id,
                tasks_dir=workspace.tasks_dir,
                tenant_id=tenant_id,
                db=db,
                payments=payments,
            )
        )
        settings = Settings()
        unsplash_server = build_unsplash_server(
            UnsplashToolContext(
                access_key=settings.unsplash_access_key,
                tasks_dir=workspace.tasks_dir,
            )
        )
        options = ClaudeAgentOptions(
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            system_prompt=self.system_prompt,
            setting_sources=self.setting_sources,
            cwd=workspace.root,
            add_dirs=[Path.cwd()],
            plugins=self.plugins,
            agents=self.agents,
            mcp_servers={
                CHAT_SERVER_NAME: chat_server,
                UNSPLASH_SERVER_NAME: unsplash_server,
            },
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
            import asyncio

            stop_event = asyncio.Event()
        log_agent_event(
            workspace.tasks_dir,
            "query_start",
            {
                "session_id": session_id or "default",
                "task_path": str(task_path),
            },
        )
        await client.query(
            self._prompt_stream(
                prompt, inflight_stream=inflight_stream, stop_event=stop_event
            ),
            session_id=session_id or "default",
        )

        summary = None
        new_session_id = session_id
        total_cost_usd = None
        usage: dict[str, Any] | None = None

        try:
            async for msg in client.receive_messages():
                log_agent_event(
                    workspace.tasks_dir,
                    "sdk_message",
                    {
                        "class": msg.__class__.__name__,
                        "subtype": getattr(msg, "subtype", None),
                        "repr": repr(msg)[:1000],
                    },
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

    @staticmethod
    def _build_prompt(task_path: Path, memory_path: Path) -> str:
        settings = Settings()
        template = ClaudeAgent._load_prompt_file(settings.claude_prompt_path)
        return (
            template.replace("<<TASK_PATH>>", str(task_path))
            .replace("<<MEMORY_PATH>>", str(memory_path))
            .rstrip()
            + "\n"
        )

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
        last_activity = start
        idle_timeout = 3.0
        max_window = 20.0

        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if time.monotonic() - start >= max_window:
                break
            try:
                update = await asyncio.wait_for(inflight_stream.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                if time.monotonic() - last_activity >= idle_timeout:
                    break
                continue
            if update is None:
                break
            text = str(update).strip()
            if not text:
                continue
            last_activity = time.monotonic()
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": f"IN-FLIGHT UPDATE (same task, do not respond separately): {text}",
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
                    f"mcp__{CHAT_SERVER_NAME}__should_send_message",
                    SEND_MESSAGE_TOOL,
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
                if fallback.exists():
                    return fallback.read_text(encoding="utf-8")
                break

        raise FileNotFoundError(f"Prompt file not found: {path}")

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

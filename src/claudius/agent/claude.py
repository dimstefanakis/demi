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
        SEND_MESSAGE_TOOL,
        UNSPLASH_SEARCH_TOOL,
        f"mcp__{CHAT_SERVER_NAME}__should_send_message",
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
            UnsplashToolContext(access_key=settings.unsplash_access_key)
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
        stop_event = None
        if inflight_stream is not None:
            import asyncio

            stop_event = asyncio.Event()
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
            await client.disconnect()

        return AgentResult(
            session_id=new_session_id,
            summary=summary,
            total_cost_usd=total_cost_usd,
            usage=usage,
        )

    @staticmethod
    def _build_prompt(task_path: Path, memory_path: Path) -> str:
        return (
            "You are the project agent coordinating a Next.js site build or edit.\n"
            "Read the task brief and memory file. Update memory.md with stable facts or decisions.\n"
            "Create a concise design context file at tasks/design_context.md summarizing: business type,\n"
            "brand tone, key CTAs, required sections, and any constraints.\n"
            "If no Next.js app exists yet, create it inside the workspace site/ directory using:\n"
            "1) cd site\n"
            "2) bun create next-app@latest <app-name> --yes (choose a short, relevant name)\n"
            "3) cd <app-name>\n"
            "4) bunx --bun shadcn@latest init\n"
            "Use bun/bunx only (no npm/yarn/pnpm). Write the chosen app name to tasks/app_name.txt.\n"
            "Run Gemini CLI headlessly to implement design. The prompt MUST be the exact contents of\n"
            "DESIGN.md (treat it as the design system for this run), and pass context via stdin (task\n"
            "brief, memory.md, design_context.md, and current page file if present). If DESIGN.md is\n"
            "missing or empty, stop and ask for it before running Gemini.\n"
            "Use the -p/--prompt flag for DESIGN.md, and explicitly set the model to Gemini 3 Pro Preview.\n"
            "If the command fails due to limits, capacity, or model availability, retry once with Gemini\n"
            "3 Flash Preview. Example:\n"
            "(cat tasks/latest.md memory.md tasks/design_context.md; test -f app/page.tsx && cat app/page.tsx) | \\\n"
            "  gemini -p \"$(cat DESIGN.md)\" --model gemini-3-pro-preview --output-format text --approval-mode yolo \\\n"
            "  || (cat tasks/latest.md memory.md tasks/design_context.md; test -f app/page.tsx && cat app/page.tsx) | \\\n"
            "  gemini -p \"$(cat DESIGN.md)\" --model gemini-3-flash-preview --output-format text --approval-mode yolo\n"
            "After Gemini completes, replace any placeholder images with relevant Unsplash images.\n"
            "Find placeholder src values (e.g., placehold.co, via.placeholder.com, dummyimage, picsum,\n"
            "loremflickr, or obvious placeholder filenames). For each, infer a short query from nearby\n"
            "section text (hero, services, gallery, team) and call the Unsplash tool:\n"
            "  mcp__claudius-unsplash__search_photos {\"query\": \"barber shop\", \"count\": 1, \"orientation\": \"landscape\"}\n"
            "Replace the placeholder with the returned URL and set a meaningful alt. If using next/image,\n"
            "ensure next.config allows images.unsplash.com.\n"
            "After Gemini completes, run `bun run build` in the app root and fix any build errors.\n"
            "Deploy using Vercel CLI (prefer ./node_modules/.bin/vercel if available):\n"
            "  vercel --prod --yes [--token $VERCEL_TOKEN] [--scope $VERCEL_SCOPE]\n"
            "After deploying, call mcp__claudius-chat__record_deploy with the deploy_url. It does NOT send\n"
            "messages, so you must send the completion update yourself using should_send_message and\n"
            "send_message (include the live URL).\n"
            "If the user asks to buy a domain, do NOT purchase immediately. First quote availability and\n"
            "price using: printf \"n\\n\" | vercel domains buy <domain> [--token $VERCEL_TOKEN] [--scope $VERCEL_SCOPE].\n"
            "Parse the output to determine availability and price (e.g., 'Buy now for $1.99'). Then call\n"
            "mcp__claudius-chat__record_domain_quote with domain, available (true/false), price_usd,\n"
            "currency (USD), and optional message/raw_output. The tool returns JSON with a message and\n"
            "optional payment_url. Use mcp__claudius-chat__should_send_message and\n"
            "mcp__claudius-chat__send_message to deliver that message.\n"
            "Also write a short internal summary to tasks/result_summary.md.\n"
            "As your first step, spawn the chatty-agent and send a quick, context-aware acknowledgement\n"
            "using the mcp__claudius-chat__send_message tool so the user gets immediate feedback.\n"
            "Keep it short and not redundant. If you want to send more interim updates, use the same tool.\n"
            "Do NOT include the final deployment URL in interim messages; send it only after deploy.\n"
            "If you need more details from the user, ask a single clear question via should_send_message\n"
            "and send_message. No greetings, no internal notes, no technical jargon. Ask only for missing\n"
            "info; do not ask generic questions that repeat what the user just told you.\n"
            "Before crafting any user-facing messages, read tasks/chat_history.md and (if present)\n"
            "tasks/chat_summary.md to avoid repeating recent replies.\n"
            "If tasks/summary_prompt.md exists, use it to update tasks/chat_summary.md, then trim\n"
            "tasks/chat_log.jsonl to keep only the most recent 10 entries and delete summary_prompt.md.\n"
            "If tasks/inflight_updates.jsonl exists, read it before starting heavy steps (Gemini run,\n"
            "build, deploy). Decide whether to incorporate the updates now or stop and restart. If the\n"
            "updates materially change the request (e.g., user says \"ignore that\" or sends new assets),\n"
            "send a short message like \"Got your update—restarting now.\" via should_send_message and\n"
            "send_message, then exit without running build/deploy. Never interrupt mid-command; only stop\n"
            "between phases.\n"
            "You may receive IN-FLIGHT UPDATE messages during a run; treat them as clarifications for the\n"
            "current task (not new tasks). Incorporate them if safe, and do not send a separate response.\n"
            "If you tell the user you're doing something (e.g., \"Adding analytics now\"), you MUST\n"
            "send a completion confirmation when finished using the chat tools. First call\n"
            "mcp__claudius-chat__should_send_message to check redundancy, then call\n"
            "mcp__claudius-chat__send_message if appropriate. Do NOT rely on files for completion updates.\n"
            "Use the chatty-agent subagent via Task to craft all user-facing messages.\n"
            "Typical first site creation takes about 10 minutes; if you are starting a full build,\n"
            "you may mention this as a rough estimate (not a guarantee).\n"
            "Do not delegate deployment or design to other services.\n\n"
            f"Task brief: {task_path}\n"
            f"Memory file: {memory_path}\n"
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
        return {
            "chatty-agent": AgentDefinition(
                description="Creates friendly, concise user-facing chat updates and questions.",
                prompt=(
                    "You are a chat UX subagent. Craft short, friendly, non-technical updates for end users.\n"
                    "Before sending, call mcp__claudius-chat__should_send_message with the draft text and\n"
                    "only send if it returns send=true.\n"
                    "Use the mcp__claudius-chat__send_message tool to send updates.\n"
                    "Read tasks/chat_history.md and tasks/chat_summary.md (if present) before responding\n"
                    "so you do not repeat or echo recent replies.\n"
                    "If you need to ask the user a question, send a single-sentence question.\n"
                    "Questions must be direct and contain no greeting.\n"
                    "Avoid internal process details, stack traces, or technical jargon.\n"
                ),
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

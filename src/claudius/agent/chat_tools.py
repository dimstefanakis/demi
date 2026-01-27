from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from claudius.memory.logs import append_log, read_logs, write_chat_history


CHAT_SERVER_NAME = "claudius-chat"
SEND_MESSAGE_TOOL = f"mcp__{CHAT_SERVER_NAME}__send_message"


@dataclass(frozen=True)
class ChatToolContext:
    messenger: Any
    tenant_external_id: str
    tasks_dir: Path


def build_chat_tools(context: ChatToolContext) -> list[SdkMcpTool[Any]]:
    @tool(
        "send_message",
        "Send a user-facing chat update via the active messaging provider.",
        {"text": str},
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text", "")).strip()
        if not text:
            return {
                "content": [{"type": "text", "text": "Skipped empty message."}],
                "is_error": False,
            }

        if _is_duplicate_message(text, context.tasks_dir):
            return {
                "content": [{"type": "text", "text": "Skipped duplicate message."}],
                "is_error": False,
            }

        try:
            await context.messenger.send_text(context.tenant_external_id, text)
        except Exception as exc:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"Send failed: {exc}"}],
                "is_error": True,
            }

        append_log(context.tasks_dir, "assistant_message", text)
        write_chat_history(context.tasks_dir)
        return {
            "content": [{"type": "text", "text": "Sent."}],
            "is_error": False,
        }

    return [send_message]


def build_chat_server(context: ChatToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=CHAT_SERVER_NAME,
        version="1.0.0",
        tools=build_chat_tools(context),
    )


def _is_duplicate_message(text: str, tasks_dir: Path, max_entries: int = 12) -> bool:
    normalized = _normalize_tokens(text)
    if not normalized:
        return True

    context_blob = _load_context_blob(tasks_dir)
    if context_blob and text.lower() in context_blob:
        return True

    entries = read_logs(tasks_dir)
    for entry in reversed(entries[-max_entries:]):
        if entry.tag != "assistant_message":
            continue
        if _similar_tokens(normalized, _normalize_tokens(entry.payload)):
            return True
    return False


def _load_context_blob(tasks_dir: Path) -> str:
    parts: list[str] = []
    history_path = tasks_dir / "chat_history.md"
    summary_path = tasks_dir / "chat_summary.md"
    if history_path.exists():
        parts.append(history_path.read_text(encoding="utf-8").lower())
    if summary_path.exists():
        parts.append(summary_path.read_text(encoding="utf-8").lower())
    return "\n".join(parts).strip()


def _normalize_tokens(text: str) -> set[str]:
    cleaned = []
    for ch in text.lower():
        cleaned.append(ch if ch.isalnum() else " ")
    tokens = [token for token in "".join(cleaned).split() if token]
    return set(tokens)


def _similar_tokens(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    overlap = len(a & b)
    union = len(a | b)
    return (overlap / union) >= 0.6

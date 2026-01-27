from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

import json

from claudius.db.core import Database
from claudius.memory.logs import append_log, read_logs, write_chat_history


CHAT_SERVER_NAME = "claudius-chat"
SEND_MESSAGE_TOOL = f"mcp__{CHAT_SERVER_NAME}__send_message"


@dataclass(frozen=True)
class ChatToolContext:
    messenger: Any
    tenant_external_id: str
    tasks_dir: Path
    db: Database | None = None
    tenant_id: int | None = None
    payments: Any | None = None


def build_chat_tools(context: ChatToolContext) -> list[SdkMcpTool[Any]]:
    @tool(
        "should_send_message",
        "Check recent chat history to see if a message would be redundant.",
        {"text": str},
    )
    async def should_send_message(args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text", "")).strip()
        if not text:
            payload = {"send": False, "reason": "empty"}
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        decision = _should_send(text, context.tasks_dir)
        payload = {"send": decision, "reason": "ok" if decision else "duplicate"}
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

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

    @tool(
        "record_deploy",
        "Record a deployment URL for the tenant. Does not send messages.",
        {"deploy_url": str},
    )
    async def record_deploy(args: dict[str, Any]) -> dict[str, Any]:
        deploy_url = str(args.get("deploy_url", "")).strip()
        if not deploy_url:
            payload = {"ok": False, "error": "missing_deploy_url"}
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if context.db and context.tenant_id is not None:
            context.db.update_tenant_deploy_url(context.tenant_id, deploy_url)
        payload = {
            "ok": True,
            "deploy_url": deploy_url,
            "message": f"Your site is live: {deploy_url}",
        }
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "record_domain_quote",
        "Persist a domain quote and, if available, create a payment link. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "available": {"type": "boolean"},
                "price_usd": {"type": "number"},
                "currency": {"type": "string"},
                "message": {"type": "string"},
                "raw_output": {"type": "string"},
            },
            "required": ["domain", "available"],
        },
    )
    async def record_domain_quote(args: dict[str, Any]) -> dict[str, Any]:
        domain = str(args.get("domain", "")).strip().lower()
        available = bool(args.get("available", False))
        currency = str(args.get("currency") or "USD").upper()
        message = str(args.get("message") or "").strip()
        raw_output = str(args.get("raw_output") or "").strip()

        if not domain:
            payload = {"ok": False, "error": "missing_domain"}
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        price_usd = None
        if args.get("price_usd") is not None:
            try:
                price_usd = float(args.get("price_usd"))
            except (TypeError, ValueError):
                price_usd = None

        if context.db is None or context.tenant_id is None:
            payload = {"ok": False, "error": "missing_db_context"}
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        quote_json = {
            "available": available,
            "price_usd": price_usd,
            "currency": currency,
            "message": message or None,
            "raw_output": raw_output or None,
        }
        status = "quoted" if available else "unavailable"
        order_id = context.db.create_domain_order(
            tenant_id=context.tenant_id,
            domain=domain,
            status=status,
            price_usd=price_usd,
            currency=currency,
            quote_json=quote_json,
        )

        if not available:
            if not message:
                message = f"{domain} is not available right now."
            payload = {
                "ok": True,
                "status": "unavailable",
                "order_id": order_id,
                "available": False,
                "message": message,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        if price_usd is None:
            if not message:
                message = f"{domain} is available, but I couldn't parse the price. Try again."
            payload = {
                "ok": True,
                "status": "price_unavailable",
                "order_id": order_id,
                "available": True,
                "message": message,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        if context.payments is None:
            payload = {
                "ok": True,
                "status": "payments_unavailable",
                "order_id": order_id,
                "available": True,
                "price_usd": price_usd,
                "currency": currency,
                "message": (
                    f"{domain} is available for ${price_usd:.2f}/yr. "
                    "Payments are not configured yet."
                ),
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        try:
            amount_cents = int(round(price_usd * 100))
        except (TypeError, ValueError):
            payload = {"ok": False, "error": "invalid_price"}
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        try:
            session = await context.payments.create_checkout_session(
                amount_cents=amount_cents,
                currency=currency.lower(),
                domain=domain,
                tenant_id=context.tenant_id,
                order_id=order_id,
            )
        except Exception as exc:  # noqa: BLE001
            context.db.mark_domain_order_failed(order_id, f"stripe_error: {exc}")
            payload = {
                "ok": False,
                "status": "stripe_error",
                "order_id": order_id,
                "message": "I couldn't create the payment link right now. Try again later.",
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        context.db.update_domain_order_payment(
            order_id,
            stripe_session_id=session.session_id,
            stripe_payment_url=session.url,
        )
        payload = {
            "ok": True,
            "status": "payment_ready",
            "order_id": order_id,
            "available": True,
            "price_usd": price_usd,
            "currency": currency,
            "payment_url": session.url,
            "message": (
                f"{domain} is available for ${price_usd:.2f}/yr. "
                f"Pay here to proceed: {session.url}"
            ),
        }
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    return [should_send_message, send_message, record_deploy, record_domain_quote]


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


def _should_send(text: str, tasks_dir: Path) -> bool:
    return not _is_duplicate_message(text, tasks_dir)


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

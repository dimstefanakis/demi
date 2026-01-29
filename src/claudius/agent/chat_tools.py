from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

import json

from claudius.db.core import Database
from claudius.memory.logs import append_log, read_logs, write_chat_history
from claudius.agent.tool_logging import log_tool_run


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
    import time

    def _log(tool_name: str, args: dict[str, Any], result: Any | None = None, error: str | None = None, start: float | None = None) -> None:
        duration_ms = None
        if start is not None:
            duration_ms = (time.monotonic() - start) * 1000.0
        log_tool_run(
            context.tasks_dir,
            tool_name,
            args=args,
            result=result,
            error=error,
            duration_ms=duration_ms,
        )

    @tool(
        "should_send_message",
        "Check recent chat history to see if a message would be redundant.",
        {"text": str},
    )
    async def should_send_message(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        text = str(args.get("text", "")).strip()
        if not text:
            payload = {"send": False, "reason": "empty"}
            _log("should_send_message", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        decision = _should_send(text, context.tasks_dir)
        payload = {"send": decision, "reason": "ok" if decision else "duplicate"}
        _log("should_send_message", args, result=payload, start=start)
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
        start = time.monotonic()
        text = str(args.get("text", "")).strip()
        if not text:
            _log("send_message", args, result={"skipped": "empty"}, start=start)
            return {
                "content": [{"type": "text", "text": "Skipped empty message."}],
                "is_error": False,
            }

        try:
            await context.messenger.send_text(context.tenant_external_id, text)
        except Exception as exc:  # noqa: BLE001
            _log("send_message", args, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": f"Send failed: {exc}"}],
                "is_error": True,
            }

        append_log(context.tasks_dir, "assistant_message", text)
        write_chat_history(context.tasks_dir)
        _log("send_message", args, result={"sent": True}, start=start)
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
        start = time.monotonic()
        deploy_url = str(args.get("deploy_url", "")).strip()
        if not deploy_url:
            payload = {"ok": False, "error": "missing_deploy_url"}
            _log("record_deploy", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        try:
            (context.tasks_dir / "deploy_url.txt").write_text(deploy_url)
        except OSError:
            pass
        if context.db and context.tenant_id is not None:
            context.db.update_tenant_deploy_url(context.tenant_id, deploy_url)
        payload = {
            "ok": True,
            "deploy_url": deploy_url,
            "message": f"Your site is live: {deploy_url}",
        }
        _log("record_deploy", args, result=payload, start=start)
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
        start = time.monotonic()
        domain = str(args.get("domain", "")).strip().lower()
        available = bool(args.get("available", False))
        currency = str(args.get("currency") or "USD").upper()
        message = str(args.get("message") or "").strip()
        raw_output = str(args.get("raw_output") or "").strip()

        if not domain:
            payload = {"ok": False, "error": "missing_domain"}
            _log("record_domain_quote", args, result=payload, start=start)
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
            fallback_payload = {
                "ok": True,
                "status": "pending_record",
                "domain": domain,
                "available": available,
                "price_usd": price_usd,
                "currency": currency,
                "message": message or None,
            }
            try:
                (context.tasks_dir / "domain_quote.json").write_text(
                    json.dumps(fallback_payload, indent=2)
                )
            except OSError:
                pass
            _log("record_domain_quote", args, result=fallback_payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(fallback_payload)}],
                "is_error": False,
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
            _log("record_domain_quote", args, result=payload, start=start)
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
            _log("record_domain_quote", args, result=payload, start=start)
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
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        try:
            amount_cents = int(round(price_usd * 100))
        except (TypeError, ValueError):
            payload = {"ok": False, "error": "invalid_price"}
            _log("record_domain_quote", args, result=payload, start=start)
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
            _log("record_domain_quote", args, result=payload, error=str(exc), start=start)
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
        _log("record_domain_quote", args, result=payload, start=start)
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
    return False


def _should_send(text: str, tasks_dir: Path) -> bool:
    return True

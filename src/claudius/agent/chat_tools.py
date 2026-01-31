from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

import json
import re

from claudius.db.core import Database
from claudius.memory.logs import append_log, write_chat_history
from claudius.agent.tool_logging import log_tool_run
from claudius.config import Settings
from claudius.workspace.project_decider import decide_project as decide_project_for_tenant
from claudius.payments.stripe import StripeClient, StripeConfig, build_stripe_config
from claudius.tenant_db import ensure_tenant_db


CHAT_SERVER_NAME = "claudius-chat"
SEND_MESSAGE_TOOL = f"mcp__{CHAT_SERVER_NAME}__send_message"


@dataclass(frozen=True)
class ChatToolContext:
    messenger: Any
    tenant_external_id: str
    tasks_dir: Path
    tenant_key: str | None = None
    provider: str | None = None
    db: Database | None = None
    tenant_id: int | None = None
    payments: Any | None = None


def build_chat_tools(context: ChatToolContext) -> list[SdkMcpTool[Any]]:
    import time

    def _resolve_payments() -> StripeClient | None:
        if context.payments is not None:
            return context.payments
        settings = Settings()
        config = build_stripe_config(settings)
        if config is None:
            return None
        return StripeClient(config)

    def _log(
        tool_name: str,
        args: dict[str, Any],
        result: Any | None = None,
        error: str | None = None,
        start: float | None = None,
    ) -> None:
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
        decision, reason = _should_send(text, context.tasks_dir)
        payload = {"send": decision, "reason": reason}
        _log("should_send_message", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "decide_project",
        "Decide which project to work on based on chat history and project context.",
        {"text": str, "set_active": bool, "switch_context": bool},
    )
    async def decide_project(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        text = str(args.get("text", "")).strip()
        set_active = bool(args.get("set_active", False))
        switch_context = bool(args.get("switch_context", False))
        if not text:
            payload = {"ok": False, "status": "missing_text"}
            _log("decide_project", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        tenant_root = _tenant_root_from_tasks_dir(context.tasks_dir)
        decision = decide_project_for_tenant(tenant_root, text)
        if decision is None:
            payload = {"ok": False, "status": "no_decision"}
            _log("decide_project", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        active_updated = False
        if set_active:
            active_updated = _set_active_project(tenant_root, decision.project_name)
        project_root = (Path(tenant_root) / "projects" / decision.project_name).resolve()
        switched = False
        if switch_context and project_root.exists():
            switched = _migrate_current_task_context(context.tasks_dir, project_root / "tasks")
        payload = {
            "ok": True,
            "project_name": decision.project_name,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "scores": decision.scores,
            "active_updated": active_updated,
            "project_root": str(project_root),
            "switched": switched,
        }
        _log("decide_project", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "send_message",
        "Send a user-facing chat update via the active messaging provider.",
        {"text": str, "final": bool},
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        text = str(args.get("text", "")).strip()
        final = bool(args.get("final", False))
        if not text:
            _log("send_message", args, result={"skipped": "empty"}, start=start)
            return {
                "content": [{"type": "text", "text": "Skipped empty message."}],
                "is_error": False,
            }
        run_id = _current_run_id(context.tasks_dir)
        if run_id and _final_sent_for_run(context.tasks_dir, run_id):
            _log("send_message", args, result={"skipped": "final_already_sent"}, start=start)
            return {
                "content": [{"type": "text", "text": "Skipped after final message."}],
                "is_error": False,
            }
        if "checkout.stripe.com" in text:
            payload = {
                "ok": False,
                "error": "stripe_link_not_allowed",
                "message": "Use send_payment_link for Stripe URLs.",
            }
            _log("send_message", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
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
        if final and run_id:
            _set_final_sent(context.tasks_dir, run_id)
        _log("send_message", args, result={"sent": True}, start=start)
        return {
            "content": [{"type": "text", "text": "Sent."}],
            "is_error": False,
        }

    @tool(
        "send_payment_link",
        "Send a payment link message using the stored Stripe URL for a billing order.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "number"},
                "source": {"type": "string"},
                "text": {"type": "string"},
                "final": {"type": "boolean"},
            },
            "required": ["text"],
        },
    )
    async def send_payment_link(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        final = bool(args.get("final", False))
        order_id = None
        if args.get("order_id") is not None:
            try:
                order_id = int(args.get("order_id"))
            except (TypeError, ValueError):
                payload = {"ok": False, "status": "invalid_order_id"}
                _log("send_payment_link", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
        text = str(args.get("text", "")).strip()
        if not text:
            payload = {"ok": False, "status": "missing_text"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        payment_url = None
        if order_id is not None and context.db is not None and context.tenant_id is not None:
            order = context.db.get_billing_order(order_id)
            if order is None or int(order["tenant_id"]) != context.tenant_id:
                payload = {"ok": False, "status": "order_not_found"}
                _log("send_payment_link", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            payment_url = str(order["stripe_payment_url"] or "").strip() or None

        source = str(args.get("source") or "").strip().lower()
        if payment_url is None:
            payment_url = _load_payment_url(context.tasks_dir, source=source, order_id=order_id)
        if not payment_url:
            payload = {"ok": False, "status": "missing_payment_url"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        sanitized = _strip_urls(text).strip()
        if not sanitized:
            payload = {"ok": False, "status": "missing_text_after_sanitize"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        message = f"{sanitized}\n\n{payment_url}"

        try:
            await context.messenger.send_text(context.tenant_external_id, message)
        except Exception as exc:  # noqa: BLE001
            _log("send_payment_link", args, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": f"Send failed: {exc}"}],
                "is_error": True,
            }

        append_log(context.tasks_dir, "assistant_message", message)
        write_chat_history(context.tasks_dir)
        run_id = _current_run_id(context.tasks_dir)
        if final and run_id:
            _set_final_sent(context.tasks_dir, run_id)
        payload = {"ok": True, "order_id": order_id, "sent": True}
        _log("send_payment_link", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
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
            _persist_quote(context.tasks_dir, "domain_quote", fallback_payload)
            _log("record_domain_quote", args, result=fallback_payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(fallback_payload)}],
                "is_error": False,
            }

        status = "quoted" if available else "unavailable"
        order_id = context.db.create_billing_order(
            tenant_id=context.tenant_id,
            order_type="domain",
            status=status,
            price_usd=price_usd,
            currency=currency,
            metadata={
                "domain": domain,
                "available": available,
                "price_usd": price_usd,
                "currency": currency,
                "message": message or None,
                "raw_output": raw_output or None,
            },
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
            _persist_quote(context.tasks_dir, "domain_quote", payload)
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
            _persist_quote(context.tasks_dir, "domain_quote", payload)
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        payments = _resolve_payments()
        if payments is None:
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
            _persist_quote(context.tasks_dir, "domain_quote", payload)
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

        metadata = {
            "billing_order_id": str(order_id),
            "order_type": "domain",
            "domain": domain,
            "price_usd": f"{price_usd:.2f}",
            "currency": currency,
        }
        if context.tenant_id is not None:
            metadata["tenant_id"] = str(context.tenant_id)
        if context.tenant_key:
            metadata["tenant_key"] = context.tenant_key
        if context.provider:
            metadata["provider"] = context.provider
        if context.tenant_external_id:
            metadata["tenant_external_id"] = context.tenant_external_id
        try:
            session = await payments.create_checkout_session(
                amount_cents=amount_cents,
                currency=currency.lower(),
                product_name=f"Domain purchase: {domain}",
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            context.db.mark_billing_order_failed(order_id, f"stripe_error: {exc}")
            payload = {
                "ok": False,
                "status": "stripe_error",
                "order_id": order_id,
                "message": "I couldn't create the payment link right now. Try again later.",
            }
            _persist_quote(context.tasks_dir, "domain_quote", payload)
            _log("record_domain_quote", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        context.db.update_billing_order_payment(
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
        _persist_quote(context.tasks_dir, "domain_quote", payload)
        _log("record_domain_quote", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "record_billing_status",
        "Update a billing order status with optional metadata. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "number"},
                "status": {"type": "string"},
                "error": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["order_id", "status"],
        },
    )
    async def record_billing_status(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if context.db is None:
            payload = {"ok": False, "status": "missing_db"}
            _log("record_billing_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        try:
            order_id = int(args.get("order_id"))
        except (TypeError, ValueError):
            payload = {"ok": False, "status": "invalid_order_id"}
            _log("record_billing_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        status = str(args.get("status") or "").strip()
        if not status:
            payload = {"ok": False, "status": "missing_status"}
            _log("record_billing_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        error = str(args.get("error") or "").strip() or None
        metadata = args.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            metadata = None
        context.db.update_billing_order_status(
            order_id=order_id,
            status=status,
            error=error,
            metadata=metadata,
        )
        payload = {"ok": True, "order_id": order_id, "status": status}
        _log("record_billing_status", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "request_backend_subscription",
        "Create a recurring payment link for managed backend features. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "price_usd": {"type": "number"},
                "currency": {"type": "string"},
                "interval": {"type": "string"},
                "product_name": {"type": "string"},
                "use_case": {"type": "string"},
            },
            "required": ["price_usd", "currency", "interval", "product_name"],
        },
    )
    async def request_backend_subscription(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        price_usd = float(args.get("price_usd") or 0)
        currency = str(args.get("currency") or "").strip().upper()
        interval = str(args.get("interval") or "").strip().lower()
        product_name = str(args.get("product_name") or "").strip()[:200]
        use_case = str(args.get("use_case") or "").strip()[:500]
        if price_usd <= 0 or not currency or not interval or not product_name:
            payload = {
                "ok": False,
                "status": "invalid_input",
                "message": "Missing or invalid pricing inputs.",
            }
            _log("request_backend_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        payments = _resolve_payments()
        if payments is None:
            payload = {
                "ok": True,
                "status": "payments_unavailable",
                "price_usd": price_usd,
                "currency": currency,
                "message": "Payments are not configured yet.",
            }
            _log("request_backend_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        order_id = None
        if context.db is not None and context.tenant_id is not None:
            order_id = context.db.create_billing_order(
                tenant_id=context.tenant_id,
                order_type="managed_backend",
                status="quoted",
                price_usd=price_usd,
                currency=currency,
                metadata={
                    "backend_type": "supabase",
                    "use_case": use_case or None,
                    "interval": interval,
                    "product_name": product_name,
                },
            )

        metadata = {
            "backend_type": "supabase",
            "price_usd": f"{price_usd:.2f}",
            "currency": currency,
            "interval": interval,
            "product_name": product_name,
            "order_type": "managed_backend",
        }
        if order_id is not None:
            metadata["billing_order_id"] = str(order_id)
        if context.tenant_id is not None:
            metadata["tenant_id"] = str(context.tenant_id)
        if context.tenant_key:
            metadata["tenant_key"] = context.tenant_key
        if context.provider:
            metadata["provider"] = context.provider
        if context.tenant_external_id:
            metadata["tenant_external_id"] = context.tenant_external_id
        if use_case:
            metadata["use_case"] = use_case

        try:
            amount_cents = int(round(price_usd * 100))
        except (TypeError, ValueError):
            payload = {"ok": False, "error": "invalid_price"}
            _log("request_backend_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        try:
            session = await payments.create_recurring_checkout_session(
                amount_cents=amount_cents,
                currency=currency.lower(),
                product_name=product_name,
                interval=interval,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            if context.db is not None and order_id is not None:
                context.db.mark_billing_order_failed(order_id, f"stripe_error: {exc}")
            payload = {
                "ok": False,
                "status": "stripe_error",
                "order_id": order_id,
                "message": "I couldn't create the payment link right now. Try again later.",
            }
            _log("request_backend_subscription", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        if context.db is not None and order_id is not None:
            context.db.update_billing_order_payment(
                order_id,
                stripe_session_id=session.session_id,
                stripe_payment_url=session.url,
            )

        payload = {
            "ok": True,
            "status": "payment_ready",
            "order_id": order_id,
            "price_usd": price_usd,
            "currency": currency,
            "payment_url": session.url,
        }
        _persist_quote(context.tasks_dir, "backend_quote", payload)
        _log("request_backend_subscription", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    return [
        should_send_message,
        decide_project,
        send_message,
        send_payment_link,
        record_deploy,
        record_domain_quote,
        record_billing_status,
        request_backend_subscription,
    ]


def build_chat_server(context: ChatToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=CHAT_SERVER_NAME,
        version="1.0.0",
        tools=build_chat_tools(context),
    )


def _is_duplicate_message(text: str, tasks_dir: Path, max_entries: int = 12) -> bool:
    return False


def _should_send(text: str, tasks_dir: Path) -> tuple[bool, str]:
    if not text.strip():
        return False, "empty"
    run_id = _current_run_id(tasks_dir)
    if run_id and _final_sent_for_run(tasks_dir, run_id):
        return False, "final_already_sent"
    return True, "ok"


def _current_run_id(tasks_dir: Path) -> str | None:
    path = tasks_dir / "run_request.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return None
    run_id = str(message.get("provider_message_id") or "").strip()
    return run_id or None


def _final_sent_for_run(tasks_dir: Path, run_id: str) -> bool:
    db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
    payload = db.get_kv("system", "final_sent")
    if not isinstance(payload, dict):
        return False
    return str(payload.get("run_id") or "") == run_id


def _set_final_sent(tasks_dir: Path, run_id: str) -> None:
    db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
    db.set_kv(
        "system",
        "final_sent",
        {"run_id": run_id, "at": datetime.now(tz=timezone.utc).isoformat()},
    )


def _strip_urls(text: str) -> str:
    cleaned = re.sub(r"https?://\\S+", "", text)
    cleaned = re.sub(r"\\s{2,}", " ", cleaned)
    return cleaned.strip()


def _persist_quote(tasks_dir: Path, key: str, payload: dict[str, Any]) -> None:
    path = tasks_dir / f"{key}.json"
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass
    try:
        db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
        db.set_kv("billing", key, payload)
    except Exception:
        return


def _load_payment_url(
    tasks_dir: Path,
    *,
    source: str | None = None,
    order_id: int | None = None,
) -> str | None:
    candidates: list[Path] = []
    keys: list[str] = []
    if source == "backend":
        keys.append("backend_quote")
    elif source == "domain":
        keys.append("domain_quote")
    else:
        keys.extend(["backend_quote", "domain_quote"])

    try:
        db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
        for key in keys:
            payload = db.get_kv("billing", key)
            if not isinstance(payload, dict):
                continue
            if order_id is not None:
                try:
                    stored_id = int(payload.get("order_id"))
                except (TypeError, ValueError):
                    stored_id = None
                if stored_id is not None and stored_id != order_id:
                    continue
            url = str(payload.get("payment_url") or "").strip()
            if url:
                return url
    except Exception:
        pass

    if source == "backend":
        candidates.append(tasks_dir / "backend_quote.json")
    elif source == "domain":
        candidates.append(tasks_dir / "domain_quote.json")
    else:
        candidates.extend([tasks_dir / "backend_quote.json", tasks_dir / "domain_quote.json"])
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if order_id is not None:
            try:
                stored_id = int(payload.get("order_id"))
            except (TypeError, ValueError):
                stored_id = None
            if stored_id is not None and stored_id != order_id:
                continue
        url = str(payload.get("payment_url") or "").strip()
        if url:
            return url
    return None


def _tenant_root_from_tasks_dir(tasks_dir: Path) -> Path:
    project_root = tasks_dir.parent
    if project_root.parent.name == "projects":
        return project_root.parent.parent
    return project_root


def _set_active_project(tenant_root: Path, project_name: str) -> bool:
    projects_dir = Path(tenant_root) / "projects"
    try:
        projects_dir.mkdir(parents=True, exist_ok=True)
        active_path = projects_dir / "active.txt"
        active_path.write_text(project_name.strip() + "\n")
        return True
    except OSError:
        return False


def _migrate_current_task_context(tasks_dir: Path, target_tasks_dir: Path) -> bool:
    try:
        target_tasks_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    patterns = [
        "latest.md",
        "task-*.md",
        "chat_log.jsonl",
        "chat_history.md",
        "chat_summary.md",
        "summary_prompt.md",
        "memory_prompt.md",
        "inflight_updates.jsonl",
        "run_request.json",
        "run_result.json",
        "outbound_messages.jsonl",
        "tool_runs.jsonl",
        "agent_events.jsonl",
        "deploy_url.txt",
        "result_summary.md",
        "backend_quote.json",
        "domain_quote.json",
    ]
    moved = False
    for pattern in patterns:
        for path in tasks_dir.glob(pattern):
            try:
                destination = target_tasks_dir / path.name
                if destination.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                path.replace(destination)
                moved = True
            except OSError:
                continue
    return moved

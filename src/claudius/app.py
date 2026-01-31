from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
import json
from typing import Any
from fastapi import FastAPI, Request

from claudius.agent.claude import ClaudeAgent
from claudius.config import Settings
from claudius.db.factory import build_database
from claudius.messaging.telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramUpdateParser,
)
from claudius.orchestrator import Orchestrator
from claudius.payments.stripe import StripeClient, build_stripe_config, derive_stripe_urls
from claudius.events import normalize_event_type, verify_signature
from claudius.jobs.worker import EventWorker, EventWorkerConfig
from claudius.jobs.pending_worker import PendingWorker, PendingWorkerConfig
from claudius.runtime.docker_agent import DockerAgent
from claudius.runtime.docker_pool import DockerPool, DockerPoolConfig
from claudius.tenant_db import ensure_tenant_db
from claudius.workspace.core import WorkspaceManager


def create_app() -> FastAPI:
    settings = Settings()
    db = build_database(settings)
    db.init()
    logger = logging.getLogger("claudius.app")

    workspace_manager = WorkspaceManager(
        settings.resolved_data_dir(),
        template_root=settings.root_dir,
    )

    agent: Any = ClaudeAgent()
    pool: DockerPool | None = None
    workspace_allocator = None

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    messenger = TelegramClient(TelegramConfig(bot_token=settings.telegram_bot_token))

    stripe_client = None
    stripe_config = build_stripe_config(settings)
    if stripe_config:
        stripe_client = StripeClient(stripe_config)


    if settings.agent_runtime == "docker":
        pool_root = (settings.root_dir / settings.docker_pool_root).resolve()
        pool_config = DockerPoolConfig(
            image=settings.docker_image,
            pool_size=settings.docker_pool_size,
            pool_root=pool_root,
            mount_path=settings.docker_mount_path,
        )
        pool = DockerPool(pool_config)
        agent = DockerAgent(
            pool=pool,
            settings=settings,
            mount_path=settings.docker_mount_path,
            forward_messages=settings.docker_forward_messages,
        )
        workspace_allocator = pool

    worker: EventWorker | None = None
    pending_worker: PendingWorker | None = None
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
        payments=stripe_client,
        workspace_allocator=workspace_allocator,
    )
    if settings.events_worker_enabled:
        worker = EventWorker(
            db=db,
            orchestrator=orchestrator,
            config=EventWorkerConfig(
                poll_interval=settings.events_worker_poll_interval,
                batch_size=settings.events_worker_batch_size,
            ),
        )
    if settings.pending_worker_enabled:
        pending_worker = PendingWorker(
            db=db,
            orchestrator=orchestrator,
            config=PendingWorkerConfig(
                poll_interval=settings.pending_worker_poll_interval,
                batch_size=settings.pending_worker_batch_size,
            ),
        )

    app = FastAPI()

    if pool is not None:
        async def _warm_pool_background() -> None:
            try:
                await asyncio.wait_for(
                    pool.ensure_pool(),
                    timeout=settings.docker_pool_warm_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Docker pool warmup timed out after %.1fs; continuing without warm pool.",
                    settings.docker_pool_warm_timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Docker pool warmup failed; continuing without warm pool.")

        @app.on_event("startup")
        async def _warm_pool() -> None:
            app.state.pool_warm_task = asyncio.create_task(
                _warm_pool_background(),
                name="docker-pool-warmup",
            )

        @app.on_event("shutdown")
        async def _stop_pool_warmup() -> None:
            task = getattr(app.state, "pool_warm_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    if worker is not None:
        @app.on_event("startup")
        async def _start_events_worker() -> None:
            app.state.events_worker_task = asyncio.create_task(
                worker.run_forever(),
                name="events-worker",
            )

        @app.on_event("shutdown")
        async def _stop_events_worker() -> None:
            worker.stop()
            task = getattr(app.state, "events_worker_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    if pending_worker is not None:
        @app.on_event("startup")
        async def _start_pending_worker() -> None:
            app.state.pending_worker_task = asyncio.create_task(
                pending_worker.run_forever(),
                name="pending-worker",
            )

        @app.on_event("shutdown")
        async def _stop_pending_worker() -> None:
            pending_worker.stop()
            task = getattr(app.state, "pending_worker_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        payload = await request.json()
        msg = TelegramUpdateParser.parse(payload)
        if not msg:
            return {"status": "ignored"}
        asyncio.create_task(orchestrator.handle_message(msg))
        return {"status": "accepted"}

    @app.post("/events")
    async def events_webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("X-Signature")
        result = verify_signature(settings.events_signing_secret, body, signature)
        if not result.ok and settings.events_require_signature:
            return {"status": "invalid", "reason": result.reason}

        payload = await request.json()
        payload = _merge_event_identity(payload, request)
        tenant = _resolve_tenant_for_event(db, payload)
        if tenant is None:
            return {"status": "invalid", "reason": "tenant_not_found"}

        project_name = orchestrator._resolve_project_from_payload(payload)
        workspace = await orchestrator._resolve_workspace(tenant, project_name=project_name)
        tenant_db = ensure_tenant_db(workspace.root / "tenant.sqlite")
        event_type = normalize_event_type(payload)
        tenant_db.record_event(event_type, payload)
        job_payload = {
            "intent": payload.get("intent"),
            "event_type": event_type,
            "payload": payload,
        }
        db.create_event_job(tenant.id, job_type="event", payload=job_payload)

        return {"status": "accepted"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/stripe/webhook")
    async def stripe_webhook(request: Request):
        if not stripe_client:
            return {"status": "ignored"}
        payload = await request.body()
        signature = request.headers.get("Stripe-Signature", "")
        try:
            event = stripe_client.verify_webhook(payload, signature)
        except Exception:  # noqa: BLE001
            return {"status": "invalid"}

        event_type = event.get("type") or ""
        if event_type == "checkout.session.completed":
            session = (event.get("data") or {}).get("object") or {}
            metadata = session.get("metadata") or {}
            session_id = session.get("id")
            subscription_id = session.get("subscription")

            order_id = _parse_billing_order_id(metadata)
            order_row = None
            if order_id:
                order_row = db.get_billing_order(order_id)
            if order_row is None and session_id:
                order_row = db.get_billing_order_by_session(str(session_id))
                if order_row:
                    order_id = int(order_row["id"])

            tenant = None
            if order_row is not None and order_id is not None:
                tenant = db.get_tenant_by_id(int(order_row["tenant_id"]))
                db.mark_billing_order_paid(
                    order_id,
                    stripe_session_id=session_id,
                    stripe_subscription_id=subscription_id,
                )
            else:
                tenant = _resolve_tenant_for_metadata(db, metadata)
                if tenant is None:
                    return {"status": "invalid", "reason": "tenant_not_found"}
                order_type = _infer_order_type(metadata)
                order_id = db.create_billing_order(
                    tenant_id=tenant.id,
                    order_type=order_type,
                    status="paid",
                    price_usd=_parse_price_usd(metadata.get("price_usd")),
                    currency=_parse_currency(metadata.get("currency")),
                    metadata=metadata,
                )
                db.mark_billing_order_paid(
                    order_id,
                    stripe_session_id=session_id,
                    stripe_subscription_id=subscription_id,
                )
                order_row = db.get_billing_order(order_id)

            if tenant is None or order_row is None or order_id is None:
                return {"status": "ok"}

            event_payload = _build_billing_paid_payload(order_row, metadata, order_id)
            db.create_event_job(
                tenant.id,
                job_type="event",
                payload=event_payload,
            )
        elif event_type == "checkout.session.expired":
            session = (event.get("data") or {}).get("object") or {}
            _handle_billing_checkout_expired(db, session)
        elif event_type == "customer.subscription.updated":
            subscription = (event.get("data") or {}).get("object") or {}
            _handle_subscription_updated(db, subscription)
        elif event_type == "customer.subscription.deleted":
            subscription = (event.get("data") or {}).get("object") or {}
            _handle_subscription_deleted(db, subscription)
        elif event_type in (
            "invoice.payment_failed",
            "invoice.payment_action_required",
            "invoice.paid",
        ):
            invoice = (event.get("data") or {}).get("object") or {}
            _handle_invoice_event(db, event_type, invoice)

        return {"status": "ok"}

    @app.get("/stripe/success")
    async def stripe_success():
        return {"status": "ok", "message": "Payment received. You can close this tab."}

    @app.get("/stripe/cancel")
    async def stripe_cancel():
        return {"status": "ok", "message": "Payment canceled. You can close this tab."}

    @app.get("/stripe/health")
    async def stripe_health():
        success_url, cancel_url = derive_stripe_urls(settings)
        return {
            "configured": stripe_config is not None,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }

    return app


def _resolve_tenant_for_event(db: Database, payload: dict) -> Any | None:
    tenant_key = payload.get("tenant_key")
    if isinstance(tenant_key, str) and tenant_key.strip():
        return db.get_tenant_by_key(tenant_key.strip())
    tenant_id = payload.get("tenant_id")
    if tenant_id is not None:
        try:
            return db.get_tenant_by_id(int(tenant_id))
        except (TypeError, ValueError):
            return None
    provider = payload.get("provider") or "telegram"
    external_id = payload.get("tenant_external_id")
    if isinstance(external_id, str) and external_id.strip():
        return db.get_tenant_by_external(provider, external_id.strip())
    return None


def _merge_event_identity(payload: dict, request: Request) -> dict:
    merged = dict(payload or {})
    if not merged.get("tenant_key"):
        header_key = request.headers.get("X-Tenant-Key")
        query_key = request.query_params.get("tenant_key")
        if header_key:
            merged["tenant_key"] = header_key
        elif query_key:
            merged["tenant_key"] = query_key
    if not merged.get("tenant_id"):
        query_id = request.query_params.get("tenant_id")
        if query_id:
            merged["tenant_id"] = query_id
    if not merged.get("tenant_external_id"):
        header_external = request.headers.get("X-Tenant-External-Id")
        query_external = request.query_params.get("tenant_external_id")
        if header_external:
            merged["tenant_external_id"] = header_external
        elif query_external:
            merged["tenant_external_id"] = query_external
    if not merged.get("provider"):
        header_provider = request.headers.get("X-Tenant-Provider")
        query_provider = request.query_params.get("provider")
        if header_provider:
            merged["provider"] = header_provider
        elif query_provider:
            merged["provider"] = query_provider
    return merged


def _resolve_tenant_for_metadata(db: Database, metadata: dict[str, Any]) -> Any | None:
    payload = {
        "tenant_key": metadata.get("tenant_key"),
        "tenant_id": metadata.get("tenant_id"),
        "tenant_external_id": metadata.get("tenant_external_id"),
        "provider": metadata.get("provider"),
    }
    return _resolve_tenant_for_event(db, payload)


def _parse_price_usd(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_currency(value: Any) -> str | None:
    if not value:
        return None
    return str(value).strip().upper() or None


def _parse_billing_order_id(metadata: dict[str, Any]) -> int | None:
    for key in ("billing_order_id", "backend_order_id", "domain_order_id", "order_id"):
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return None


def _infer_order_type(metadata: dict[str, Any]) -> str:
    order_type = str(metadata.get("order_type") or "").strip().lower()
    if order_type:
        return order_type
    if metadata.get("backend_type") or metadata.get("use_case"):
        return "managed_backend"
    if metadata.get("domain"):
        return "domain"
    return "unknown"


def _extract_order_metadata(order_row: Any) -> dict[str, Any]:
    if order_row is None:
        return {}
    raw = None
    try:
        raw = order_row["metadata_json"]
    except (KeyError, TypeError):
        raw = None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_billing_paid_payload(
    order_row: Any,
    metadata: dict[str, Any],
    order_id: int,
) -> dict[str, Any]:
    order_type = str(order_row["order_type"] or "").strip().lower()
    if order_type == "domain":
        return _build_domain_paid_payload(order_row, metadata, order_id)
    if order_type == "managed_backend":
        return _build_backend_paid_payload(order_row, metadata, order_id)
    payload = {
        "summary": "Payment received",
        "billing_order_id": order_id,
        "order_type": order_type or "unknown",
        "price_usd": order_row["price_usd"] or _parse_price_usd(metadata.get("price_usd")),
        "currency": order_row["currency"] or _parse_currency(metadata.get("currency")),
    }
    return {"event_type": "billing_paid", "intent": "billing_paid", "payload": payload}


def _build_domain_paid_payload(
    order_row: Any,
    metadata: dict[str, Any],
    order_id: int,
) -> dict[str, Any]:
    order_meta = _extract_order_metadata(order_row)
    domain = order_meta.get("domain") or metadata.get("domain")
    payload = {
        "summary": "Domain payment received",
        "billing_order_id": order_id,
        "domain": domain,
        "price_usd": order_row["price_usd"] or _parse_price_usd(metadata.get("price_usd")),
        "currency": order_row["currency"] or _parse_currency(metadata.get("currency")),
    }
    return {"event_type": "domain_paid", "intent": "domain_purchase_paid", "payload": payload}


def _build_backend_paid_payload(
    order_row: Any,
    metadata: dict[str, Any],
    order_id: int,
) -> dict[str, Any]:
    order_meta = _extract_order_metadata(order_row)
    payload = {
        "summary": "Backend upgrade payment received",
        "billing_order_id": order_id,
        "backend_type": order_meta.get("backend_type") or metadata.get("backend_type"),
        "price_usd": order_row["price_usd"] or _parse_price_usd(metadata.get("price_usd")),
        "currency": order_row["currency"] or _parse_currency(metadata.get("currency")),
        "use_case": order_meta.get("use_case") or metadata.get("use_case"),
        "interval": order_meta.get("interval") or metadata.get("interval"),
        "product_name": order_meta.get("product_name") or metadata.get("product_name"),
    }
    return {
        "event_type": "backend_paid",
        "intent": "managed_backend_paid",
        "payload": payload,
    }


def _handle_billing_checkout_expired(db: Database, session: dict[str, Any]) -> None:
    metadata = session.get("metadata") or {}
    order_id = _parse_billing_order_id(metadata)
    session_id = session.get("id")
    order_row = None
    if order_id:
        order_row = db.get_billing_order(order_id)
    if order_row is None and session_id:
        order_row = db.get_billing_order_by_session(str(session_id))
        if order_row:
            order_id = int(order_row["id"])
    if order_row is None or order_id is None:
        return
    db.update_billing_order_status(order_id, "checkout_expired")
    tenant = db.get_tenant_by_id(int(order_row["tenant_id"]))
    if tenant is None:
        return
    order_type = str(order_row["order_type"] or "").strip().lower()
    event_type = "billing_checkout_expired"
    intent = "billing_checkout_expired"
    if order_type == "domain":
        event_type = "domain_checkout_expired"
        intent = "domain_checkout_expired"
    elif order_type == "managed_backend":
        event_type = "backend_checkout_expired"
        intent = "managed_backend_checkout_expired"
    db.create_event_job(
        tenant.id,
        job_type="event",
        payload={
            "event_type": event_type,
            "intent": intent,
            "payload": {
                "summary": event_type.replace("_", " ").title(),
                "billing_order_id": order_id,
                "order_type": order_type,
            },
        },
    )


def _handle_subscription_updated(db: Database, subscription: dict[str, Any]) -> None:
    subscription_id = subscription.get("id")
    if not subscription_id:
        return
    order_row = db.get_billing_order_by_subscription(str(subscription_id))
    if order_row is None or order_row["order_type"] != "managed_backend":
        return
    order_id = int(order_row["id"])
    cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
    cancel_at = subscription.get("cancel_at")
    current_period_end = subscription.get("current_period_end")
    status = subscription.get("status")
    metadata = {
        "subscription_status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "cancel_at": cancel_at,
        "current_period_end": current_period_end,
    }
    if cancel_at_period_end:
        db.update_billing_order_status(order_id, "cancel_scheduled", metadata=metadata)
        _emit_billing_event(
            db,
            order_row,
            "backend_cancel_scheduled",
            "managed_backend_cancel_scheduled",
        )
        return
    if status in {"canceled", "unpaid", "incomplete_expired"}:
        db.update_billing_order_status(order_id, "canceled", metadata=metadata)
        _emit_billing_event(
            db,
            order_row,
            "backend_canceled",
            "managed_backend_canceled",
        )
        return
    db.update_billing_order_status(order_id, "active", metadata=metadata)


def _handle_subscription_deleted(db: Database, subscription: dict[str, Any]) -> None:
    subscription_id = subscription.get("id")
    if not subscription_id:
        return
    order_row = db.get_billing_order_by_subscription(str(subscription_id))
    if order_row is None or order_row["order_type"] != "managed_backend":
        return
    order_id = int(order_row["id"])
    metadata = {
        "subscription_status": subscription.get("status"),
        "canceled_at": subscription.get("canceled_at"),
    }
    db.update_billing_order_status(order_id, "canceled", metadata=metadata)
    _emit_billing_event(
        db,
        order_row,
        "backend_canceled",
        "managed_backend_canceled",
    )


def _handle_invoice_event(db: Database, event_type: str, invoice: dict[str, Any]) -> None:
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return
    order_row = db.get_billing_order_by_subscription(str(subscription_id))
    if order_row is None or order_row["order_type"] != "managed_backend":
        return
    order_id = int(order_row["id"])
    if event_type == "invoice.payment_failed":
        db.update_billing_order_status(order_id, "payment_failed")
        _emit_billing_event(
            db,
            order_row,
            "backend_payment_failed",
            "managed_backend_payment_failed",
        )
        return
    if event_type == "invoice.payment_action_required":
        db.update_billing_order_status(order_id, "payment_action_required")
        _emit_billing_event(
            db,
            order_row,
            "backend_payment_action_required",
            "managed_backend_payment_action_required",
        )
        return
    if event_type == "invoice.paid":
        if order_row["status"] == "cancel_scheduled":
            return
        db.update_billing_order_status(order_id, "active")


def _emit_billing_event(db: Database, order_row: Any, event_type: str, intent: str) -> None:
    tenant = db.get_tenant_by_id(int(order_row["tenant_id"]))
    if tenant is None:
        return
    db.create_event_job(
        tenant.id,
        job_type="event",
        payload={
            "event_type": event_type,
            "intent": intent,
            "payload": {
                "summary": event_type.replace("_", " ").title(),
                "billing_order_id": int(order_row["id"]),
                "order_type": order_row["order_type"],
            },
        },
    )


app = create_app()

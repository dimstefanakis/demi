from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
import json
from typing import Any
from fastapi import FastAPI, Request, HTTPException

from demi.agent.claude import ClaudeAgent
from demi.config import Settings
from demi.db.factory import build_database
from demi.messaging.telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramUpdateParser,
)
from demi.orchestrator import Orchestrator
from demi.payments.stripe import StripeClient, build_stripe_config, derive_stripe_urls
from demi.events import normalize_event_type, verify_signature
from demi.jobs.worker import EventWorker, EventWorkerConfig
from demi.jobs.pending_worker import PendingWorker, PendingWorkerConfig
from demi.jobs.outbox_worker import OutboxWorker, OutboxWorkerConfig
from demi.jobs.scheduler_worker import SchedulerWorker, SchedulerWorkerConfig
from demi.observability import initialize_laminar
from demi.runtime.docker_agent import DockerAgent, load_env_file_values
from demi.runtime.docker_pool import DockerPool, DockerPoolConfig
from demi.workspace.core import WorkspaceManager


def create_app() -> FastAPI:
    settings = Settings()
    initialize_laminar(settings.lmnr_project_api_key)
    db = build_database(settings)
    db.init()
    logger = logging.getLogger("demi.app")

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
            extra_env=load_env_file_values(settings),
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
    outbox_worker: OutboxWorker | None = None
    scheduler_worker: SchedulerWorker | None = None
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
        payments=stripe_client,
        workspace_allocator=workspace_allocator,
    )
    embedded_workers_enabled = bool(settings.api_embedded_workers_enabled)
    if embedded_workers_enabled and settings.events_worker_enabled:
        worker = EventWorker(
            db=db,
            orchestrator=orchestrator,
            config=EventWorkerConfig(
                poll_interval=settings.events_worker_poll_interval,
                batch_size=settings.events_worker_batch_size,
            ),
        )
    if embedded_workers_enabled and settings.pending_worker_enabled:
        pending_worker = PendingWorker(
            db=db,
            orchestrator=orchestrator,
            config=PendingWorkerConfig(
                poll_interval=settings.pending_worker_poll_interval,
                batch_size=settings.pending_worker_batch_size,
                active_check_interval_seconds=settings.pending_worker_active_check_interval,
            ),
        )
    if embedded_workers_enabled and settings.outbox_worker_enabled:
        outbox_worker = OutboxWorker(
            db=db,
            messenger=messenger,
            config=OutboxWorkerConfig(
                poll_interval=settings.outbox_worker_poll_interval,
                batch_size=settings.outbox_worker_batch_size,
                interaction_send_timeout_seconds=settings.outbox_send_timeout_seconds,
                max_attempts=settings.outbox_max_attempts,
                retry_base_seconds=settings.outbox_retry_base_seconds,
                retry_max_seconds=settings.outbox_retry_max_seconds,
                fallback_scan_interval_seconds=settings.outbox_fallback_scan_interval,
                stale_sending_seconds=settings.outbox_stale_sending_seconds,
            ),
            interaction_agent=orchestrator.agent,
            workspace_manager=workspace_manager,
        )
    if embedded_workers_enabled and settings.scheduler_worker_enabled:
        scheduler_worker = SchedulerWorker(
            db=db,
            config=SchedulerWorkerConfig(
                poll_interval=settings.scheduler_worker_poll_interval,
                batch_size=settings.scheduler_worker_batch_size,
            ),
        )

    app = FastAPI()

    def _log_background_task_result(task: asyncio.Task[Any], *, label: str) -> None:
        try:
            _ = task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Background task failed: %s", label)

    def _require_admin(request: Request) -> None:
        token = settings.admin_api_token
        if not token:
            raise HTTPException(status_code=404, detail="not_found")
        auth = request.headers.get("Authorization", "")
        candidate = ""
        if auth.lower().startswith("bearer "):
            candidate = auth.split(" ", 1)[1].strip()
        if not candidate:
            candidate = request.headers.get("X-Admin-Token", "").strip()
        if candidate != token:
            raise HTTPException(status_code=403, detail="forbidden")

    @app.on_event("startup")
    async def _migrate_legacy_queue() -> None:
        try:
            migrated = await orchestrator.migrate_legacy_queue()
            if migrated:
                logger.info("Migrated %s legacy queued messages to run_inputs", migrated)
        except Exception:  # noqa: BLE001
            logger.exception("Legacy queue migration failed")

    if pool is not None and settings.docker_pool_size > 0:
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

    if outbox_worker is not None:
        @app.on_event("startup")
        async def _start_outbox_worker() -> None:
            app.state.outbox_worker_task = asyncio.create_task(
                outbox_worker.run_forever(),
                name="outbox-worker",
            )

        @app.on_event("shutdown")
        async def _stop_outbox_worker() -> None:
            outbox_worker.stop()
            task = getattr(app.state, "outbox_worker_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    if scheduler_worker is not None:
        @app.on_event("startup")
        async def _start_scheduler_worker() -> None:
            app.state.scheduler_worker_task = asyncio.create_task(
                scheduler_worker.run_forever(),
                name="scheduler-worker",
            )

        @app.on_event("shutdown")
        async def _stop_scheduler_worker() -> None:
            scheduler_worker.stop()
            task = getattr(app.state, "scheduler_worker_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        payload = await request.json()
        msg = TelegramUpdateParser.parse(payload)
        try:
            update_id = payload.get("update_id") if isinstance(payload, dict) else None
            parse_status = "parsed" if msg is not None else "ignored"
            parse_error = None if msg is not None else "not_message_update_or_missing_fields"
            db.record_webhook_update(
                provider="telegram",
                update_id=update_id,
                payload=payload if isinstance(payload, dict) else {"raw": payload},
                parse_status=parse_status,
                parse_error=parse_error,
                tenant_external_id=(msg.tenant_external_id if msg else None),
                provider_message_id=(msg.provider_message_id if msg else None),
                message_text=(msg.text if msg else None),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record telegram webhook audit row")
        if not msg:
            return {"status": "ignored"}
        task = asyncio.create_task(orchestrator.handle_message(msg))
        task.add_done_callback(
            lambda t: _log_background_task_result(t, label="telegram_webhook.handle_message")
        )
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

        event_type = normalize_event_type(payload)
        try:
            db.record_tenant_event(tenant.id, event_type, payload)
        except Exception:
            return {"status": "invalid", "reason": "event_record_failed"}
        job_payload = {
            "intent": payload.get("intent"),
            "event_type": event_type,
            "payload": payload,
        }
        db.create_event_job(tenant.id, job_type="event", payload=job_payload)

        return {"status": "accepted"}

    @app.post("/billing/status")
    async def billing_status(request: Request):
        payload = await request.json()
        payload = _merge_event_identity(payload, request)
        tenant = _resolve_tenant_for_event(db, payload)
        if tenant is None:
            return {"status": "unknown", "payment_required": False, "message": "tenant_not_found"}
        if _tenant_testing_mode_enabled(db, int(tenant.id)):
            purpose = _normalize_billing_purpose(payload.get("purpose") or payload.get("purpose_label"))
            return _testing_mode_billing_payload(purpose=purpose)
        purpose = payload.get("purpose")
        purpose_label = payload.get("purpose_label")
        create_if_missing = bool(payload.get("create_if_missing", False))
        return await _build_assistant_billing_status(
            db=db,
            tenant=tenant,
            settings=settings,
            stripe_client=stripe_client,
            purpose=purpose,
            purpose_label=purpose_label,
            create_if_missing=create_if_missing,
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/admin/runs/{run_id}/cancel")
    async def cancel_run(run_id: int, request: Request):
        _require_admin(request)
        reason = "admin_cancelled"
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            provided = str(payload.get("reason") or "").strip()
            if provided:
                reason = provided
        result = await orchestrator.cancel_run(run_id, reason=reason, notify=True)
        return {"status": result.status, "detail": result.detail}

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
    if order_row is None:
        return
    order_type = order_row["order_type"]
    emit_events = order_type in {"managed_backend"}
    if not emit_events and not _is_assistant_order_type(order_type):
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
        if emit_events:
            _emit_billing_event(
                db,
                order_row,
                "backend_cancel_scheduled",
                "managed_backend_cancel_scheduled",
            )
        return
    if status in {"canceled", "unpaid", "incomplete_expired"}:
        db.update_billing_order_status(order_id, "canceled", metadata=metadata)
        if emit_events:
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
    if order_row is None:
        return
    order_type = order_row["order_type"]
    emit_events = order_type in {"managed_backend"}
    if not emit_events and not _is_assistant_order_type(order_type):
        return
    order_id = int(order_row["id"])
    metadata = {
        "subscription_status": subscription.get("status"),
        "canceled_at": subscription.get("canceled_at"),
    }
    db.update_billing_order_status(order_id, "canceled", metadata=metadata)
    if emit_events:
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
    if order_row is None:
        return
    order_type = order_row["order_type"]
    emit_events = order_type in {"managed_backend"}
    if not emit_events and not _is_assistant_order_type(order_type):
        return
    order_id = int(order_row["id"])
    if event_type == "invoice.payment_failed":
        db.update_billing_order_status(order_id, "payment_failed")
        if emit_events:
            _emit_billing_event(
                db,
                order_row,
                "backend_payment_failed",
                "managed_backend_payment_failed",
            )
        return
    if event_type == "invoice.payment_action_required":
        db.update_billing_order_status(order_id, "payment_action_required")
        if emit_events:
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


def _assistant_allow_first_build(tenant: Any) -> bool:
    return not getattr(tenant, "last_deploy_url", None)


def _tenant_testing_mode_enabled(db: Any, tenant_id: int) -> bool:
    try:
        payload = db.get_tenant_kv(tenant_id, "system", "testing_mode")
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("enabled"))


def _testing_mode_billing_payload(purpose: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "testing_mode",
        "payment_required": False,
        "allow_first_build": True,
        "plan": "testing",
        "testing_mode": True,
        "message": "testing mode active",
    }
    if purpose:
        payload["purpose"] = purpose
    return payload


def _assistant_paid_statuses() -> set[str]:
    return {"paid", "active", "cancel_scheduled"}


def _assistant_order_type() -> str:
    return "assistant_subscription"


def _assistant_plan_name(settings: Settings) -> str:
    return "assistant_monthly"


def _assistant_currency(settings: Settings) -> str:
    return (settings.assistant_currency or "USD").upper()


def _assistant_order_type_for_purpose(purpose: str | None) -> str:
    if purpose:
        return f"assistant_subscription:{purpose}"
    return _assistant_order_type()


def _is_assistant_order_type(order_type: str | None) -> bool:
    return bool(order_type and order_type.startswith("assistant_subscription"))


def _assistant_usage_threshold(settings: Settings) -> float | None:
    raw = settings.assistant_usage_threshold_usd
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _normalize_billing_purpose(value: Any | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    return slug[:60] if slug else None


def _row_value(row: Any, key: str) -> Any | None:
    if row is None:
        return None
    if hasattr(row, "get"):
        try:
            return row.get(key)
        except Exception:
            return None
    try:
        return row[key]
    except Exception:
        return None


async def _build_assistant_billing_status(
    *,
    db: Database,
    tenant: Any,
    settings: Settings,
    stripe_client: StripeClient | None,
    purpose: str | None = None,
    purpose_label: str | None = None,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    allow_first_build = _assistant_allow_first_build(tenant)
    normalized_purpose = _normalize_billing_purpose(purpose or purpose_label)
    order_type = _assistant_order_type_for_purpose(normalized_purpose)
    order_row = db.get_latest_billing_order(int(tenant.id), order_type)
    cached_label = purpose_label
    default_currency = _assistant_currency(settings)
    default_price = settings.assistant_price_usd
    price_id = (settings.assistant_stripe_price_id or "").strip()
    pricing_configured = bool(price_id) or default_price is not None
    if order_row is not None:
        status = str(_row_value(order_row, "status") or "").strip().lower()
        order_id = int(_row_value(order_row, "id") or 0)
        metadata = _extract_order_metadata(order_row)
        cached_label = cached_label or metadata.get("purpose_label")
        normalized_purpose = normalized_purpose or metadata.get("purpose")
        price_usd = _row_value(order_row, "price_usd") or default_price
        currency = _row_value(order_row, "currency") or default_currency
        if status in _assistant_paid_statuses():
            return {
                "status": status,
                "payment_required": False,
                "allow_first_build": False,
                "plan": _assistant_plan_name(settings),
                "order_id": order_id,
                "purpose": normalized_purpose,
                "purpose_label": cached_label,
                "price_usd": price_usd,
                "currency": currency,
            }
        session_id = str(_row_value(order_row, "stripe_session_id") or "").strip() or None
        payment_url = str(_row_value(order_row, "stripe_payment_url") or "").strip() or None
        if status in {"pending_payment", "quoted"} and session_id and stripe_client is not None:
            try:
                session = await stripe_client.get_checkout_session(session_id)
            except Exception:
                session = None
            if session:
                session_status = str(session.get("status") or "").strip().lower()
                if session_status == "complete":
                    subscription_id = session.get("subscription")
                    db.mark_billing_order_paid(
                        order_id,
                        stripe_session_id=session_id,
                        stripe_subscription_id=subscription_id,
                    )
                    return {
                        "status": "paid",
                        "payment_required": False,
                        "allow_first_build": False,
                        "plan": _assistant_plan_name(settings),
                        "order_id": order_id,
                        "purpose": normalized_purpose,
                        "purpose_label": cached_label,
                        "price_usd": price_usd,
                        "currency": currency,
                    }
                if session_status == "expired":
                    db.update_billing_order_status(order_id, "checkout_expired")
                    order_row = None
                elif session_status and session_status != "open":
                    db.update_billing_order_status(
                        order_id, f"checkout_{session_status}"
                    )
                    order_row = None

        if order_row is not None and payment_url and status in {"pending_payment", "quoted"}:
            return {
                "status": status,
                "payment_required": True,
                "allow_first_build": allow_first_build,
                "plan": _assistant_plan_name(settings),
                "order_id": order_id,
                "payment_url": payment_url,
                "purpose": normalized_purpose,
                "purpose_label": cached_label,
                "price_usd": price_usd,
                "currency": currency,
            }

    usage_threshold = _assistant_usage_threshold(settings)
    if usage_threshold is not None and pricing_configured:
        paid_order = db.get_latest_paid_billing_order(
            int(tenant.id),
            order_type_prefix="assistant_subscription",
        )
        if paid_order is None:
            usage_total = db.sum_run_costs(int(tenant.id))
            if usage_total >= usage_threshold:
                return {
                    "status": "usage_limit",
                    "payment_required": True,
                    "allow_first_build": allow_first_build,
                    "plan": _assistant_plan_name(settings),
                    "message": "usage_threshold_exceeded",
                    "purpose": normalized_purpose,
                    "purpose_label": cached_label,
                    "price_usd": default_price,
                    "currency": default_currency,
                    "usage_total_usd": usage_total,
                    "usage_threshold_usd": usage_threshold,
                }

    if not create_if_missing:
        return {
            "status": "ready",
            "payment_required": False,
            "allow_first_build": allow_first_build,
            "plan": _assistant_plan_name(settings),
            "message": "billing_deferred",
            "purpose": normalized_purpose,
            "purpose_label": cached_label,
            "price_usd": default_price,
            "currency": default_currency,
        }

    if stripe_client is None:
        return {
            "status": "unconfigured",
            "payment_required": False,
            "allow_first_build": allow_first_build,
            "plan": _assistant_plan_name(settings),
            "message": "stripe_not_configured",
            "purpose": normalized_purpose,
            "purpose_label": cached_label,
            "price_usd": default_price,
            "currency": default_currency,
        }

    price_usd = settings.assistant_price_usd
    product_name = (settings.assistant_product_name or "Hire me").strip() or "Hire me"
    currency = _assistant_currency(settings)

    if not price_id and price_usd is None:
        return {
            "status": "unconfigured",
            "payment_required": False,
            "allow_first_build": allow_first_build,
            "plan": _assistant_plan_name(settings),
            "message": "assistant_pricing_missing",
            "purpose": normalized_purpose,
            "purpose_label": cached_label,
            "price_usd": default_price,
            "currency": default_currency,
        }

    order_id = db.create_billing_order(
        tenant_id=int(tenant.id),
        order_type=order_type,
        status="quoted",
        price_usd=price_usd,
        currency=currency,
        metadata={
            "plan": _assistant_plan_name(settings),
            "price_id": price_id or None,
            "product_name": product_name,
            "purpose": normalized_purpose,
            "purpose_label": cached_label,
        },
    )
    metadata = {
        "billing_order_id": str(order_id),
        "order_type": order_type,
        "plan": _assistant_plan_name(settings),
        "price_usd": f"{price_usd:.2f}" if price_usd is not None else "",
        "currency": currency,
        "purpose": normalized_purpose or "",
        "purpose_label": cached_label or "",
    }
    if getattr(tenant, "id", None) is not None:
        metadata["tenant_id"] = str(tenant.id)
    if getattr(tenant, "key", None):
        metadata["tenant_key"] = str(tenant.key)
    if getattr(tenant, "provider", None):
        metadata["provider"] = str(tenant.provider)
    if getattr(tenant, "external_id", None):
        metadata["tenant_external_id"] = str(tenant.external_id)

    if price_id:
        session = await stripe_client.create_subscription_checkout_session_for_price(
            price_id=price_id,
            metadata=metadata,
        )
    else:
        amount_cents = int(round(float(price_usd or 0) * 100))
        session = await stripe_client.create_recurring_checkout_session(
            amount_cents=amount_cents,
            currency=currency.lower(),
            product_name=product_name,
            interval="month",
            metadata=metadata,
        )

    db.update_billing_order_payment(
        order_id,
        stripe_session_id=session.session_id,
        stripe_payment_url=session.url,
        status="pending_payment",
    )
    return {
        "status": "pending_payment",
        "payment_required": True,
        "allow_first_build": allow_first_build,
        "plan": _assistant_plan_name(settings),
        "order_id": order_id,
        "payment_url": session.url,
        "purpose": normalized_purpose,
        "purpose_label": cached_label,
        "price_usd": price_usd,
        "currency": currency,
    }


app = create_app()

from __future__ import annotations

import asyncio
from typing import Any
from fastapi import FastAPI, Request

from claudius.agent.claude import ClaudeAgent
from claudius.config import Settings
from claudius.db.core import Database
from claudius.domains.service import DomainService
from claudius.messaging.telegram import TelegramClient, TelegramConfig, TelegramUpdateParser
from claudius.orchestrator import Orchestrator
from claudius.payments.stripe import StripeClient, StripeConfig
from claudius.events import normalize_event_type, verify_signature
from claudius.jobs.worker import EventWorker, EventWorkerConfig
from claudius.runtime.docker_agent import DockerAgent
from claudius.runtime.docker_pool import DockerPool, DockerPoolConfig
from claudius.tenant_db import ensure_tenant_db
from claudius.workspace.core import WorkspaceManager


def create_app() -> FastAPI:
    settings = Settings()
    db = Database(settings.resolved_db_path())
    db.init()

    workspace_manager = WorkspaceManager(settings.resolved_data_dir(), template_root=settings.root_dir)

    agent: Any = ClaudeAgent()
    pool: DockerPool | None = None
    workspace_allocator = None

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    messenger = TelegramClient(TelegramConfig(bot_token=settings.telegram_bot_token))

    stripe_client = None
    if (
        settings.stripe_secret_key
        and settings.stripe_webhook_secret
        and settings.stripe_success_url
        and settings.stripe_cancel_url
    ):
        stripe_client = StripeClient(
            StripeConfig(
                secret_key=settings.stripe_secret_key,
                webhook_secret=settings.stripe_webhook_secret,
                success_url=settings.stripe_success_url,
                cancel_url=settings.stripe_cancel_url,
            )
        )

    domain_service = DomainService(db=db, settings=settings, messenger=messenger)

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

    app = FastAPI()

    if pool is not None:
        @app.on_event("startup")
        async def _warm_pool() -> None:
            await pool.ensure_pool()

    if worker is not None:
        @app.on_event("startup")
        async def _start_events_worker() -> None:
            asyncio.create_task(worker.run_forever())

        @app.on_event("shutdown")
        async def _stop_events_worker() -> None:
            worker.stop()

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

        workspace = workspace_manager.ensure_workspace(tenant.key)
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

        if event.get("type") == "checkout.session.completed":
            session = (event.get("data") or {}).get("object") or {}
            metadata = session.get("metadata") or {}
            order_id_raw = metadata.get("domain_order_id")
            session_id = session.get("id")
            if order_id_raw:
                try:
                    order_id = int(order_id_raw)
                except ValueError:
                    order_id = None
                if order_id:
                    db.mark_domain_order_paid(order_id, stripe_session_id=session_id)
                    await domain_service.purchase_paid_order(order_id)

        return {"status": "ok"}

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


app = create_app()

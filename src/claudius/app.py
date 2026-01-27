from __future__ import annotations

import asyncio
from fastapi import FastAPI, Request

from claudius.agent.claude import ClaudeAgent
from claudius.config import Settings
from claudius.db.core import Database
from claudius.domains.service import DomainService
from claudius.messaging.telegram import TelegramClient, TelegramConfig, TelegramUpdateParser
from claudius.orchestrator import Orchestrator
from claudius.payments.stripe import StripeClient, StripeConfig
from claudius.workspace.core import WorkspaceManager


def create_app() -> FastAPI:
    settings = Settings()
    db = Database(settings.resolved_db_path())
    db.init()

    workspace_manager = WorkspaceManager(settings.resolved_data_dir(), template_root=settings.root_dir)

    agent = ClaudeAgent()

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

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
        payments=stripe_client,
    )

    app = FastAPI()

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        payload = await request.json()
        msg = TelegramUpdateParser.parse(payload)
        if not msg:
            return {"status": "ignored"}
        asyncio.create_task(orchestrator.handle_message(msg))
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


app = create_app()

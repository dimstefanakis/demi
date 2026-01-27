from __future__ import annotations

import asyncio
from fastapi import FastAPI, Request

from claudius.agent.claude import ClaudeAgent
from claudius.config import Settings
from claudius.db.core import Database
from claudius.messaging.telegram import TelegramClient, TelegramConfig, TelegramUpdateParser
from claudius.orchestrator import Orchestrator
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

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
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

    return app


app = create_app()

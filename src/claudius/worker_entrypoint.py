from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from typing import Any

from claudius.agent.claude import ClaudeAgent
from claudius.config import Settings
from claudius.db.factory import build_database
from claudius.jobs.pending_worker import PendingWorker, PendingWorkerConfig
from claudius.jobs.outbox_worker import OutboxWorker, OutboxWorkerConfig
from claudius.jobs.worker import EventWorker, EventWorkerConfig
from claudius.messaging.telegram import TelegramClient, TelegramConfig
from claudius.orchestrator import Orchestrator
from claudius.payments.stripe import StripeClient, build_stripe_config
from claudius.runtime.docker_agent import DockerAgent
from claudius.runtime.docker_pool import DockerPool, DockerPoolConfig
from claudius.workspace.core import WorkspaceManager


async def _run_workers() -> None:
    settings = Settings()
    db = build_database(settings)
    db.init()

    workspace_manager = WorkspaceManager(
        settings.resolved_data_dir(),
        template_root=settings.root_dir,
    )

    agent: Any = ClaudeAgent()
    pool: DockerPool | None = None
    workspace_allocator = None

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

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    messenger = TelegramClient(TelegramConfig(bot_token=settings.telegram_bot_token))

    stripe_client = None
    stripe_config = build_stripe_config(settings)
    if stripe_config:
        stripe_client = StripeClient(stripe_config)

    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=messenger,
        payments=stripe_client,
        workspace_allocator=workspace_allocator,
    )

    tasks: list[asyncio.Task] = []
    workers: list[Any] = []

    if settings.events_worker_enabled:
        event_worker = EventWorker(
            db=db,
            orchestrator=orchestrator,
            config=EventWorkerConfig(
                poll_interval=settings.events_worker_poll_interval,
                batch_size=settings.events_worker_batch_size,
            ),
        )
        workers.append(event_worker)
        tasks.append(asyncio.create_task(event_worker.run_forever(), name="events-worker"))

    if settings.pending_worker_enabled:
        pending_worker = PendingWorker(
            db=db,
            orchestrator=orchestrator,
            config=PendingWorkerConfig(
                poll_interval=settings.pending_worker_poll_interval,
                batch_size=settings.pending_worker_batch_size,
            ),
        )
        workers.append(pending_worker)
        tasks.append(
            asyncio.create_task(pending_worker.run_forever(), name="pending-worker")
        )

    if settings.outbox_worker_enabled:
        outbox_worker = OutboxWorker(
            db=db,
            messenger=messenger,
            config=OutboxWorkerConfig(
                poll_interval=settings.outbox_worker_poll_interval,
                batch_size=settings.outbox_worker_batch_size,
            ),
        )
        workers.append(outbox_worker)
        tasks.append(
            asyncio.create_task(outbox_worker.run_forever(), name="outbox-worker")
        )

    if not tasks:
        raise RuntimeError(
            "No workers enabled. Set EVENTS_WORKER_ENABLED, PENDING_WORKER_ENABLED, "
            "or OUTBOX_WORKER_ENABLED"
        )

    stop_event = asyncio.Event()

    def _stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    await stop_event.wait()
    for worker in workers:
        try:
            worker.stop()
        except Exception:
            pass
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_workers())


if __name__ == "__main__":
    main()

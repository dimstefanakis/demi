from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from typing import Any

from demi.agent.claude import ClaudeAgent
from demi.config import Settings
from demi.db.factory import build_database
from demi.jobs.pending_worker import PendingWorker, PendingWorkerConfig
from demi.jobs.outbox_worker import OutboxWorker, OutboxWorkerConfig
from demi.jobs.scheduler_worker import SchedulerWorker, SchedulerWorkerConfig
from demi.jobs.pm_worker import PMWorker, PMWorkerConfig
from demi.jobs.worker import EventWorker, EventWorkerConfig
from demi.messaging.telegram import TelegramClient, TelegramConfig
from demi.observability import initialize_laminar
from demi.orchestrator import Orchestrator
from demi.payments.stripe import StripeClient, build_stripe_config
from demi.runtime.docker_agent import DockerAgent, load_env_file_values
from demi.runtime.docker_pool import DockerPool, DockerPoolConfig
from demi.workspace.core import WorkspaceManager

logger = logging.getLogger(__name__)


async def _run_workers() -> None:
    settings = Settings()
    initialize_laminar(settings.lmnr_project_api_key)
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
    try:
        activated = await asyncio.to_thread(orchestrator.backfill_pm_for_existing_tenants)
        if activated:
            logger.info("PM backfill activated for %s tenant(s)", activated)
    except Exception:
        logger.exception("PM backfill failed")

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
                active_check_interval_seconds=settings.pending_worker_active_check_interval,
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
        workers.append(outbox_worker)
        tasks.append(
            asyncio.create_task(outbox_worker.run_forever(), name="outbox-worker")
        )

    if settings.scheduler_worker_enabled:
        scheduler_worker = SchedulerWorker(
            db=db,
            config=SchedulerWorkerConfig(
                poll_interval=settings.scheduler_worker_poll_interval,
                batch_size=settings.scheduler_worker_batch_size,
                pm_worker_enabled=settings.pm_worker_enabled,
            ),
        )
        workers.append(scheduler_worker)
        tasks.append(
            asyncio.create_task(scheduler_worker.run_forever(), name="scheduler-worker")
        )

    if settings.pm_worker_enabled:
        pm_worker = PMWorker(
            db=db,
            orchestrator=orchestrator,
            workspace_manager=workspace_manager,
            config=PMWorkerConfig(
                poll_interval=settings.pm_worker_poll_interval,
                batch_size=settings.pm_worker_batch_size,
                cooldown_between_user_messages_seconds=settings.pm_message_cooldown_seconds,
                idle_threshold_hours=settings.pm_idle_threshold_hours,
                first_heartbeat_min_messages=settings.pm_first_heartbeat_min_messages,
                default_trigger_timezone=settings.pm_timezone,
                outbox_max_attempts=settings.outbox_max_attempts,
            ),
        )
        workers.append(pm_worker)
        tasks.append(asyncio.create_task(pm_worker.run_forever(), name="pm-worker"))

    if not tasks:
        raise RuntimeError(
            "No workers enabled. Set EVENTS_WORKER_ENABLED, PENDING_WORKER_ENABLED, "
            "OUTBOX_WORKER_ENABLED, SCHEDULER_WORKER_ENABLED, or PM_WORKER_ENABLED"
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

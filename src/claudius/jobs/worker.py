from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from claudius.db.core import Database


@dataclass
class EventWorkerConfig:
    poll_interval: float = 1.5
    batch_size: int = 20


@dataclass
class EventWorker:
    db: Database
    orchestrator: Any
    config: EventWorkerConfig
    _running: bool = False

    async def run_forever(self) -> None:
        self._running = True
        while self._running:
            jobs = self.db.fetch_pending_event_jobs(self.config.batch_size)
            if not jobs:
                await asyncio.sleep(self.config.poll_interval)
                continue
            for job in jobs:
                await self._handle_job(job)

    def stop(self) -> None:
        self._running = False

    async def _handle_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        self.db.mark_event_job_running(job_id)
        try:
            payload = json.loads(job["payload_json"]) if job.get("payload_json") else {}
            job_type = job.get("job_type")
            handled = True
            if job_type == "event":
                handled = await self._handle_event(job, payload)
            if handled:
                self.db.mark_event_job_done(job_id)
        except Exception as exc:  # noqa: BLE001
            self.db.mark_event_job_failed(job_id, error=str(exc), retry_after_seconds=5)

    async def _handle_event(self, job: dict[str, Any], payload: dict[str, Any]) -> bool:
        tenant_id = int(job["tenant_id"])
        result = await self.orchestrator.handle_event_job(
            tenant_id=tenant_id,
            payload=payload,
            job_id=int(job["id"]),
        )
        if result.status == "busy":
            self.db.mark_event_job_failed(
                int(job["id"]),
                error="tenant_busy",
                retry_after_seconds=5,
            )
            return False
        if result.status not in ("accepted", "duplicate"):
            self.db.mark_event_job_failed(
                int(job["id"]),
                error=result.detail or result.status,
                retry_after_seconds=10,
            )
            return False
        return True

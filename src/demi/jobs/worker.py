from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from demi.db.core import Database


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

    async def _db_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def run_forever(self) -> None:
        self._running = True
        idle_streak = 0
        try:
            while self._running:
                jobs = await self._db_call(
                    self.db.fetch_pending_event_jobs,
                    self.config.batch_size,
                )
                if not jobs:
                    idle_streak = min(idle_streak + 1, 6)
                    await asyncio.sleep(self.config.poll_interval * (2**idle_streak))
                    continue
                idle_streak = 0
                for job in jobs:
                    try:
                        await self._handle_job(job)
                    except Exception:
                        # Keep failure backoff bounded and recover quickly on success.
                        idle_streak = min(idle_streak + 1, 3)
                        await asyncio.sleep(self.config.poll_interval)
                        break
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def _handle_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        await self._db_call(self.db.mark_event_job_running, job_id)
        try:
            raw_payload = job.get("payload_json") if isinstance(job, dict) else None
            if isinstance(raw_payload, dict):
                payload = raw_payload
            elif raw_payload:
                payload = json.loads(raw_payload)
            else:
                payload = {}
            job_type = job.get("job_type")
            handled = True
            if job_type == "event":
                handled = await self._handle_event(job, payload)
            if handled:
                await self._db_call(self.db.mark_event_job_done, job_id)
        except Exception as exc:  # noqa: BLE001
            await self._db_call(
                self.db.mark_event_job_failed,
                job_id,
                error=str(exc),
                retry_after_seconds=5,
            )

    async def _handle_event(self, job: dict[str, Any], payload: dict[str, Any]) -> bool:
        tenant_id = int(job["tenant_id"])
        result = await self.orchestrator.handle_event_job(
            tenant_id=tenant_id,
            payload=payload,
            job_id=int(job["id"]),
        )
        if result.status == "busy":
            await self._db_call(
                self.db.mark_event_job_failed,
                int(job["id"]),
                error="tenant_busy",
                retry_after_seconds=5,
            )
            return False
        if result.status not in ("accepted", "duplicate"):
            await self._db_call(
                self.db.mark_event_job_failed,
                int(job["id"]),
                error=result.detail or result.status,
                retry_after_seconds=10,
            )
            return False
        return True

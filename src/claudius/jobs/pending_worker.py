from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
import sqlite3
from typing import Any

from claudius.db.core import Database


@dataclass
class PendingWorkerConfig:
    poll_interval: float = 2.5
    batch_size: int = 20
    processing_grace_seconds: float = 60.0
    run_stale_seconds: float = 900.0


@dataclass
class PendingWorker:
    db: Database
    orchestrator: Any
    config: PendingWorkerConfig
    _running: bool = False

    async def _db_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _resolve_inflight_run(
        self,
        tenant: Any,
        project_name: str | None,
        inflight: Any,
    ) -> bool:
        try:
            workspace = await self.orchestrator._resolve_workspace(
                tenant, project_name=project_name
            )
            if self.orchestrator._reconcile_inflight_run(tenant, inflight, workspace):
                return False
            stale_seconds = max(1, int(self.config.run_stale_seconds))
            if self.orchestrator._is_run_stale(inflight, max_age_seconds=stale_seconds):
                await self.orchestrator._finalize_stale_run(tenant, inflight)
                return False
        except Exception:
            return True
        return True

    async def run_forever(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    await self._requeue_processing_groups()
                    groups = await self._db_call(
                        self.db.fetch_pending_message_groups,
                        self.config.batch_size,
                    )
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower():
                        await asyncio.sleep(self.config.poll_interval)
                        continue
                    raise
                if not groups:
                    await asyncio.sleep(self.config.poll_interval)
                    continue
                for group in groups:
                    try:
                        await self._handle_group(group)
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower():
                            await asyncio.sleep(self.config.poll_interval)
                            break
                        raise
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def _handle_group(self, group: dict[str, Any]) -> None:
        tenant_id = int(group.get("tenant_id") or 0)
        if not tenant_id:
            return
        tenant = await self._db_call(self.db.get_tenant_by_id, tenant_id)
        if tenant is None:
            return
        project_name = group.get("project_name")
        await self._db_call(
            self.db.expire_stale_runs,
            tenant.id,
            project_name,
            self.orchestrator._now(),
        )
        inflight = await self._db_call(self.db.get_inflight_run, tenant.id, project_name)
        if inflight:
            still_inflight = await self._resolve_inflight_run(
                tenant, project_name, inflight
            )
            if still_inflight:
                return
        await self.orchestrator._drain_pending_messages(tenant, project_name=project_name)

    async def _requeue_processing_groups(self) -> None:
        groups = await self._db_call(
            self.db.fetch_processing_message_groups,
            self.config.batch_size,
        )
        if not groups:
            return
        now = self.orchestrator._now()
        for group in groups:
            tenant_id = int(group.get("tenant_id") or 0)
            if not tenant_id:
                continue
            tenant = await self._db_call(self.db.get_tenant_by_id, tenant_id)
            if tenant is None:
                continue
            project_name = group.get("project_name")
            await self._db_call(
                self.db.expire_stale_runs,
                tenant.id,
                project_name,
                now,
            )
            inflight = await self._db_call(self.db.get_inflight_run, tenant.id, project_name)
            if inflight:
                still_inflight = await self._resolve_inflight_run(
                    tenant, project_name, inflight
                )
                if still_inflight:
                    continue
            oldest_raw = group.get("oldest_received_at")
            if not oldest_raw:
                continue
            try:
                oldest = datetime.fromisoformat(str(oldest_raw))
            except ValueError:
                continue
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            age = (now - oldest).total_seconds()
            if age < self.config.processing_grace_seconds:
                continue
            requeued = await self._db_call(
                self.db.requeue_processing_messages,
                tenant.id,
                project_name,
            )
            if requeued:
                await self.orchestrator._drain_pending_messages(
                    tenant,
                    project_name=project_name,
                )

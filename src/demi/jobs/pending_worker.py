from __future__ import annotations

import asyncio
from datetime import timedelta
import time
from dataclasses import dataclass
from typing import Any

from demi.db.core import Database


@dataclass
class PendingWorkerConfig:
    poll_interval: float = 2.5
    batch_size: int = 20
    processing_grace_seconds: float = 60.0
    run_stale_seconds: float = 900.0
    active_check_interval_seconds: float = 10.0


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
        idle_streak = 0
        next_active_check = 0.0
        next_received_recovery_check = 0.0
        try:
            while self._running:
                groups = await self._db_call(
                    self.db.fetch_queued_run_input_groups,
                    self.config.batch_size,
                )
                if not groups:
                    now = time.monotonic()
                    if now >= next_active_check:
                        await self._check_active_runs()
                        next_active_check = now + max(
                            self.config.poll_interval,
                            self.config.active_check_interval_seconds,
                        )
                    if now >= next_received_recovery_check:
                        await self._recover_stale_received_messages()
                        next_received_recovery_check = now + max(
                            self.config.poll_interval,
                            self.config.active_check_interval_seconds,
                        )
                    idle_streak = min(idle_streak + 1, 6)
                    backoff_sleep = self.config.poll_interval * (2**idle_streak)
                    max_idle_sleep = max(
                        self.config.poll_interval,
                        self.config.active_check_interval_seconds,
                    )
                    await asyncio.sleep(min(backoff_sleep, max_idle_sleep))
                    continue
                idle_streak = 0
                for group in groups:
                    try:
                        await self._handle_group(group)
                    except Exception:
                        await asyncio.sleep(self.config.poll_interval)
                        break
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
                sleep_for = self.config.poll_interval
                if sleep_for <= 0:
                    sleep_for = 0.1
                await asyncio.sleep(sleep_for)
                return
        await self.orchestrator._drain_run_inputs(tenant, project_name=project_name)

    async def _check_active_runs(self) -> None:
        rows = await self._db_call(self.db.list_active_runs, self.config.batch_size)
        if not rows:
            return
        for row in rows:
            tenant_id = row.get("tenant_id")
            if tenant_id is None:
                continue
            tenant = await self._db_call(self.db.get_tenant_by_id, int(tenant_id))
            if tenant is None:
                continue
            project_name = row.get("project_name")
            run_id = row.get("run_id")
            if run_id is None:
                await self._db_call(self.db.clear_active_run, tenant.id, project_name)
                continue
            run_row = await self._db_call(self.db.get_run, int(run_id))
            if not run_row:
                await self._db_call(self.db.clear_active_run, tenant.id, project_name)
                continue
            status = run_row.get("status") if hasattr(run_row, "get") else None
            if status and status != "running":
                await self._db_call(self.db.clear_active_run, tenant.id, project_name)
                continue
            await self._resolve_inflight_run(tenant, project_name, run_row)

    async def _recover_stale_received_messages(self) -> None:
        grace_seconds = max(0.0, float(self.config.processing_grace_seconds))
        cutoff = self.orchestrator._now() - timedelta(seconds=grace_seconds)
        rows = await self._db_call(
            self.db.fetch_stale_received_messages,
            before=cutoff,
            limit=self.config.batch_size,
        )
        if not rows:
            return
        for row in rows:
            message_id = row.get("id")
            try:
                message_id = int(message_id) if message_id is not None else None
            except (TypeError, ValueError):
                message_id = None
            tenant_id = row.get("tenant_id")
            if tenant_id is None:
                continue
            tenant = await self._db_call(self.db.get_tenant_by_id, int(tenant_id))
            if tenant is None:
                continue
            msg = self.orchestrator._message_from_message_row(tenant, row)
            if msg is None:
                continue
            try:
                result = await self.orchestrator.handle_message(
                    msg,
                    allow_existing_received=True,
                    allow_interaction_stream=False,
                )
                # If stale-recovery returns while the row is still "received",
                # fail it to avoid repeated high-cost recovery loops.
                if message_id is not None:
                    latest = await self._db_call(self.db.get_message, int(message_id))
                    latest_status = ""
                    if latest is not None:
                        try:
                            latest_status = str(
                                latest.get("status")
                                if hasattr(latest, "get")
                                else latest["status"]
                                or ""
                            ).strip().lower()
                        except Exception:
                            latest_status = ""
                    if latest_status == "received":
                        await self._db_call(
                            self.db.update_message_status,
                            int(message_id),
                            "failed",
                        )
            except Exception:
                if message_id is not None:
                    try:
                        await self._db_call(
                            self.db.update_message_status,
                            int(message_id),
                            "failed",
                        )
                    except Exception:
                        pass
                continue

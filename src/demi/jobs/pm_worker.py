from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from demi.db.core import Database
from demi.jobs.worker import EventWorker
from demi.pm.constants import PM_ENABLED_KEY, PM_LAST_HEARTBEAT_KEY, PM_NAMESPACE


logger = logging.getLogger(__name__)
TERMINAL_OUTBOX_ERROR_PREFIXES = (
    "invalid_payload_",
    "invalid_pm_suggestion_",
)
PROJECT_MANAGER_ROLE = "project_manager"
LEAD_PROJECT_MANAGER_ROLE = "lead_project_manager"
DEFAULT_LEAD_PM_CONTEXT = "Lead Project Manager"


@dataclass
class PMWorkerConfig:
    poll_interval: float = 5.0
    batch_size: int = 10
    health_stale_message_minutes: int = 5
    health_stale_input_minutes: int = 30
    health_stale_run_seconds: int = 900
    outbox_max_attempts: int = 12


@dataclass
class PMWorker:
    db: Database
    orchestrator: Any
    workspace_manager: Any
    config: PMWorkerConfig
    _running: bool = False

    async def _db_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def run_forever(self) -> None:
        self._running = True
        idle_streak = 0
        try:
            while self._running:
                try:
                    processed = await self.process_once()
                    if not processed:
                        idle_streak = min(idle_streak + 1, 6)
                        await asyncio.sleep(self.config.poll_interval * (2**idle_streak))
                    else:
                        idle_streak = 0
                        await asyncio.sleep(self.config.poll_interval)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("PMWorker loop failed; continuing")
                    idle_streak = min(idle_streak + 1, 6)
                    await asyncio.sleep(
                        max(0.25, float(self.config.poll_interval)) * (2**idle_streak)
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    async def process_once(self) -> bool:
        jobs = await self._db_call(
            self.db.fetch_pending_event_jobs,
            self.config.batch_size,
            "pm_trigger",
        )
        if not jobs:
            return False
        for job in jobs:
            await self._handle_job(job)
        return True

    async def _handle_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        await self._db_call(self.db.mark_event_job_running, job_id)
        raw_payload = job.get("payload_json") if isinstance(job, dict) else None
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        try:
            await self.process_pm_trigger(
                tenant_id=int(job["tenant_id"]),
                payload=raw_payload,
                trigger_event_id=job_id,
            )
            await self._db_call(self.db.mark_event_job_done, job_id)
        except Exception as exc:  # noqa: BLE001
            retry_after, max_attempts = EventWorker._event_retry_policy(
                raw_payload,
                default_retry_after=30,
            )
            await self._db_call(
                self.db.mark_event_job_failed,
                job_id,
                error=str(exc),
                retry_after_seconds=retry_after,
                max_attempts=max_attempts,
            )

    async def process_pm_trigger(
        self,
        *,
        tenant_id: int,
        payload: dict[str, Any],
        trigger_event_id: int | None,
    ) -> None:
        tenant = await self._db_call(self.db.get_tenant_by_id, int(tenant_id))
        if tenant is None:
            return
        if not await self._is_pm_enabled(int(tenant_id)):
            return

        trigger = self._extract_trigger_type(payload)
        now = datetime.now(tz=timezone.utc)
        source_payload = self._extract_source_payload(payload)

        # Prompt-first path queues a per-project PM run after each execution run.
        # Skip the scheduler's generic run-completed PM trigger when no explicit
        # PM role is requested to avoid duplicate post-run PM executions.
        requested_role = str(payload.get("role") or "").strip().lower()
        source_run_role = str(source_payload.get("run_role") or "").strip().lower()
        if (
            trigger == "run_completed"
            and not requested_role
            and source_run_role in {"", "execution", LEAD_PROJECT_MANAGER_ROLE}
        ):
            await self._db_call(
                self.db.set_tenant_kv,
                int(tenant_id),
                PM_NAMESPACE,
                PM_LAST_HEARTBEAT_KEY,
                {"at": now.isoformat(), "trigger": trigger, "skipped": "prompt_first_handoff"},
            )
            return

        if trigger in {"health_check", "pm_health_check"}:
            await self._handle_health_check(tenant=tenant, now=now)
        else:
            await self._dispatch_pm_run(
                tenant=tenant,
                trigger=trigger,
                payload=payload,
                trigger_event_id=trigger_event_id,
                now=now,
            )

        await self._db_call(
            self.db.set_tenant_kv,
            int(tenant_id),
            PM_NAMESPACE,
            PM_LAST_HEARTBEAT_KEY,
            {"at": now.isoformat(), "trigger": trigger},
        )

    async def _is_pm_enabled(self, tenant_id: int) -> bool:
        payload = await self._db_call(self.db.get_tenant_kv, int(tenant_id), PM_NAMESPACE, PM_ENABLED_KEY)
        if payload is None:
            return False
        if isinstance(payload, dict):
            if "enabled" in payload:
                return bool(payload.get("enabled"))
            return True
        return bool(payload)

    @staticmethod
    def _extract_trigger_type(payload: dict[str, Any]) -> str:
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = {}
        trigger = str(event_payload.get("trigger") or "").strip().lower()
        if trigger:
            return trigger
        intent = str(payload.get("intent") or "").strip().lower()
        if intent.startswith("pm_"):
            return intent.replace("pm_", "", 1)
        event_type = str(payload.get("event_type") or "").strip().lower()
        return event_type or "pm_trigger"

    @staticmethod
    def _extract_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Extract the originating event payload from a scheduler-delivered trigger.

        For webhook_condition triggers (run_completed, run_failed, deploy_completed),
        the scheduler embeds the matched tenant_event payload at
        ``payload.payload._scheduler.source_payload``.
        """
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            return {}
        scheduler = event_payload.get("_scheduler")
        if not isinstance(scheduler, dict):
            return {}
        source = scheduler.get("source_payload")
        return dict(source) if isinstance(source, dict) else {}

    @staticmethod
    def _normalize_pm_dispatch_role(raw: Any | None) -> str:
        value = str(raw or "").strip().lower()
        if value == PROJECT_MANAGER_ROLE:
            return PROJECT_MANAGER_ROLE
        return LEAD_PROJECT_MANAGER_ROLE

    async def _dispatch_pm_run(
        self,
        *,
        tenant: Any,
        trigger: str,
        payload: dict[str, Any],
        trigger_event_id: int | None,
        now: datetime,
    ) -> None:
        role = self._normalize_pm_dispatch_role(payload.get("role"))
        raw_event_payload = payload.get("payload")
        event_payload = dict(raw_event_payload) if isinstance(raw_event_payload, dict) else {}
        event_payload.setdefault("trigger", trigger)
        if role == LEAD_PROJECT_MANAGER_ROLE and not str(event_payload.get("summary") or "").strip():
            event_payload["summary"] = f"CONTEXT MANAGEMENT RUN ({trigger})"

        source_payload = self._extract_source_payload(payload)
        if source_payload and "source_payload" not in event_payload:
            event_payload["source_payload"] = source_payload

        execution_context = str(payload.get("execution_context") or "").strip()
        if role == PROJECT_MANAGER_ROLE:
            if not execution_context:
                execution_context = (
                    str(event_payload.get("source_context") or "").strip() or "Main project"
                )
        else:
            execution_context = DEFAULT_LEAD_PM_CONTEXT

        dispatch_payload = {
            "event_type": str(payload.get("event_type") or "pm_trigger").strip() or "pm_trigger",
            "intent": str(payload.get("intent") or f"pm_{trigger}").strip() or f"pm_{trigger}",
            "role": role,
            "execution_context": execution_context,
            "execution_agent_id": payload.get("execution_agent_id"),
            "payload": event_payload,
        }

        result = await self.orchestrator.handle_event_job(
            int(getattr(tenant, "id")),
            dispatch_payload,
            int(trigger_event_id or int(now.timestamp())),
        )
        status = str(getattr(result, "status", "")).strip().lower()
        if status not in {"accepted", "busy", "duplicate", "blocked"}:
            raise RuntimeError(f"pm_dispatch_failed:{status or 'unknown'}")

    async def _handle_health_check(self, *, tenant: Any, now: datetime) -> None:
        snapshot = await self._collect_health_snapshot(tenant_id=int(tenant.id), now=now)
        fixed: dict[str, int] = {
            "stale_messages": 0,
            "zombie_runs": 0,
            "failed_outbox": 0,
            "stale_run_inputs": 0,
        }
        if int(snapshot.get("stale_messages") or 0) > 0:
            fixed["stale_messages"] = await self._fix_stale_received_messages(tenant=tenant)
        if int(snapshot.get("zombie_runs") or 0) > 0:
            fixed["zombie_runs"] = await self._fix_zombie_runs(tenant=tenant)
        if int(snapshot.get("failed_outbox") or 0) > 0:
            fixed["failed_outbox"] = await self._fix_failed_outbox(tenant=tenant)
        if int(snapshot.get("stale_run_inputs") or 0) > 0:
            fixed["stale_run_inputs"] = await self._fix_stale_run_inputs(tenant=tenant)

        logger.info(
            "pm_health_check tenant_id=%s snapshot=%s fixed=%s",
            int(tenant.id),
            snapshot,
            fixed,
        )

    async def _collect_health_snapshot(self, *, tenant_id: int, now: datetime) -> dict[str, Any]:
        stale_before = now - timedelta(minutes=max(1, int(self.config.health_stale_message_minutes)))
        stale_messages = await self._db_call(
            self.db.fetch_stale_received_messages,
            tenant_id=int(tenant_id),
            before=stale_before,
            limit=50,
        )
        inflight = await self._db_call(self.db.list_tenant_inflight_runs, int(tenant_id), limit=50)
        zombie_runs = [
            row
            for row in inflight
            if self.orchestrator._is_run_stale(
                row,
                max_age_seconds=max(60, int(self.config.health_stale_run_seconds)),
            )
        ]
        failed_outbox = await self._db_call(self.db.list_outbox, int(tenant_id), "failed", 100)
        requeueable_failed_outbox = [
            row for row in (failed_outbox or []) if self._is_requeueable_failed_outbox_row(row)
        ]
        run_inputs = await self._db_call(self.db.fetch_run_inputs, int(tenant_id), None, "queued", 200)
        stale_input_cutoff = now - timedelta(minutes=max(1, int(self.config.health_stale_input_minutes)))
        stale_run_inputs: list[dict[str, Any]] = []
        for row in run_inputs:
            created_at = self._parse_dt(row.get("created_at"))
            if created_at and created_at < stale_input_cutoff:
                stale_run_inputs.append(row)
        return {
            "stale_messages": len(stale_messages or []),
            "zombie_runs": len(zombie_runs),
            "failed_outbox": len(requeueable_failed_outbox),
            "stale_run_inputs": len(stale_run_inputs),
        }

    async def _fix_stale_received_messages(self, *, tenant: Any) -> int:
        before = datetime.now(tz=timezone.utc) - timedelta(
            minutes=max(1, int(self.config.health_stale_message_minutes))
        )
        rows = await self._db_call(
            self.db.fetch_stale_received_messages,
            tenant_id=int(tenant.id),
            before=before,
            limit=100,
        )
        recovered = 0
        for row in rows or []:
            # Intentionally replay all stale `received` rows, including
            # provider=`event`, so crashed event-ingestion paths can recover.
            # For provider=`event`, route the replay through the tenant's
            # canonical provider key so `handle_message` targets the owning
            # tenant instead of creating/using an `event:<external_id>` tenant.
            # `allow_existing_received=True` then reloads the original row and
            # preserves event-provider semantics during processing.
            msg = self.orchestrator._message_from_message_row(tenant, row)
            if msg is None:
                continue
            row_provider = ""
            try:
                row_provider = str(
                    row.get("provider") if hasattr(row, "get") else row["provider"]
                ).strip().lower()
            except Exception:
                row_provider = ""
            if row_provider == "event":
                tenant_provider = str(getattr(tenant, "provider", "") or "").strip()
                if tenant_provider and tenant_provider != "event":
                    try:
                        from dataclasses import replace

                        msg = replace(
                            msg,
                            provider=tenant_provider,
                            tenant_external_id=str(getattr(tenant, "external_id", "") or ""),
                        )
                    except Exception:
                        if isinstance(msg, dict):
                            msg = dict(msg)
                            msg["provider"] = tenant_provider
                            msg["tenant_external_id"] = str(
                                getattr(tenant, "external_id", "") or ""
                            )
            try:
                result = await self.orchestrator.handle_message(
                    msg,
                    allow_existing_received=True,
                    allow_interaction_stream=False,
                )
            except Exception:
                continue
            if str(getattr(result, "status", "")).strip().lower() in {"accepted", "busy", "duplicate"}:
                recovered += 1
        return recovered

    async def _fix_zombie_runs(self, *, tenant: Any) -> int:
        rows = await self._db_call(self.db.list_tenant_inflight_runs, int(tenant.id), limit=100)
        fixed = 0
        for row in rows or []:
            if not self.orchestrator._is_run_stale(
                row,
                max_age_seconds=max(60, int(self.config.health_stale_run_seconds)),
            ):
                continue
            try:
                # PendingWorker owns user-facing stale-timeout messaging to avoid duplicate
                # notifications from PM health checks and active-run reconciliation.
                await self.orchestrator._finalize_stale_run(tenant, row, notify=False)
                fixed += 1
            except Exception:
                continue
        return fixed

    async def _fix_failed_outbox(self, *, tenant: Any) -> int:
        rows = await self._db_call(self.db.list_outbox, int(tenant.id), "failed", 200)
        fixed = 0
        for row in rows or []:
            if not self._is_requeueable_failed_outbox_row(row):
                continue
            outbox_id = str(row.get("id") or "").strip()
            if not outbox_id:
                continue
            try:
                await self._db_call(self.db.update_outbox_status, outbox_id, "queued", "pm_requeue", 0.0)
                fixed += 1
            except Exception:
                continue
        return fixed

    def _is_requeueable_failed_outbox_row(self, row: dict[str, Any] | Any) -> bool:
        if not isinstance(row, dict):
            return False
        last_error = str(row.get("last_error") or "").strip().lower()
        if any(last_error.startswith(prefix) for prefix in TERMINAL_OUTBOX_ERROR_PREFIXES):
            return False
        try:
            attempts = int(row.get("attempt_count") or 0)
        except (TypeError, ValueError):
            attempts = 0
        max_attempts = max(1, int(self.config.outbox_max_attempts))
        if attempts >= max_attempts and attempts > 0:
            return False
        return True

    async def _fix_stale_run_inputs(self, *, tenant: Any) -> int:
        rows = await self._db_call(self.db.fetch_run_inputs, int(tenant.id), None, "queued", 200)
        stale_cutoff = datetime.now(tz=timezone.utc) - timedelta(
            minutes=max(1, int(self.config.health_stale_input_minutes))
        )
        stale_count = 0
        for row in rows or []:
            created_at = self._parse_dt(row.get("created_at"))
            if created_at and created_at < stale_cutoff:
                stale_count += 1
        if stale_count:
            try:
                drained = await self.orchestrator._drain_run_inputs(
                    tenant,
                    max_inputs=int(stale_count),
                )
                return int(drained or 0)
            except TypeError:
                # Backward compatibility for orchestrators without scoped drain support.
                try:
                    await self.orchestrator._drain_run_inputs(tenant)
                except Exception:
                    return 0
                return int(stale_count)
            except Exception:
                return 0
        return 0

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

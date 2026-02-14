from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any
import uuid

import httpx

from demi.db.core import Database
from demi.jobs.worker import EventWorker
from demi.pm.constants import (
    PM_ENABLED_KEY,
    PM_FIRST_HEARTBEAT_TRIGGER_KEY,
    PM_LAST_HEARTBEAT_KEY,
    PM_NAMESPACE,
    PM_NEEDS_ONBOARDING_KEY,
    SCHEDULER_NAMESPACE,
)


logger = logging.getLogger(__name__)
TERMINAL_OUTBOX_ERROR_PREFIXES = (
    "invalid_payload_",
    "invalid_pm_suggestion_",
)


@dataclass
class PMWorkerConfig:
    poll_interval: float = 5.0
    batch_size: int = 10
    triage_model: str = "rule_based_v1"
    action_model: str = "rule_based_v1"
    session_cost_rotation_threshold_usd: float = 5.0
    session_age_rotation_days: int = 30
    max_actions_per_heartbeat: int = 3
    cooldown_between_user_messages_seconds: int = 3600
    idle_threshold_hours: int = 48
    # Legacy compatibility knob. First-heartbeat onboarding no longer waits for
    # a message-count threshold; PM should be considered onboarded immediately
    # and stay silent unless a concrete action is warranted.
    first_heartbeat_min_messages: int = 5
    qa_timeout_seconds: float = 12.0
    health_stale_message_minutes: int = 5
    health_stale_input_minutes: int = 30
    health_stale_run_seconds: int = 900
    default_trigger_timezone: str = "America/New_York"
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

        now = datetime.now(tz=timezone.utc)
        trigger = self._extract_trigger_type(payload)
        pm_state = self._read_pm_state(tenant)
        triage = await self._run_triage(
            tenant=tenant,
            trigger=trigger,
            payload=payload,
            pm_state=pm_state,
            now=now,
        )
        heartbeat_id = await self._db_call(
            self.db.create_pm_heartbeat,
            tenant_id=int(tenant_id),
            trigger_type=trigger,
            trigger_event_id=trigger_event_id,
            triage_result=triage,
            action_needed=bool(triage.get("action_needed")),
            triage_cost_usd=0.0,
            triage_model=self.config.triage_model,
            total_cost_usd=0.0,
        )

        metrics = pm_state.setdefault("metrics", {})
        metrics["total_heartbeats"] = int(metrics.get("total_heartbeats") or 0) + 1
        metrics["total_cost_usd"] = float(metrics.get("total_cost_usd") or 0.0)
        action_results: list[dict[str, Any]] = []
        executed_actions: list[dict[str, Any]] = []

        if bool(triage.get("action_needed")):
            actions = triage.get("suggested_actions")
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    result = await self._apply_action(
                        tenant=tenant,
                        heartbeat_id=heartbeat_id,
                        action=action,
                        pm_state=pm_state,
                        now=now,
                    )
                    executed_actions.append(action)
                    action_results.append(result)
                    metrics["total_actions_taken"] = int(metrics.get("total_actions_taken") or 0) + 1
                    if result.get("autonomy_tier") == "suggest":
                        metrics["total_suggestions_made"] = int(
                            metrics.get("total_suggestions_made") or 0
                        ) + 1
            if (
                trigger == "first_heartbeat"
                and self._first_heartbeat_onboarding_succeeded(
                    actions=executed_actions,
                    action_results=action_results,
                )
            ):
                await self._disable_first_heartbeat_trigger(int(tenant_id))
                await self._db_call(
                    self.db.set_tenant_kv,
                    int(tenant_id),
                    PM_NAMESPACE,
                    PM_NEEDS_ONBOARDING_KEY,
                    None,
                )

        if heartbeat_id is not None:
            await self._db_call(
                self.db.update_pm_heartbeat,
                int(heartbeat_id),
                action_result={"actions": action_results},
                action_cost_usd=0.0,
                action_model=self.config.action_model,
                total_cost_usd=0.0,
                completed_at=now.isoformat(),
            )
        await self._db_call(
            self.db.set_tenant_kv,
            int(tenant_id),
            PM_NAMESPACE,
            PM_LAST_HEARTBEAT_KEY,
            {"at": now.isoformat()},
        )
        pm_state["updated_at"] = now.isoformat()
        self._write_pm_state(tenant, pm_state)

    async def _is_pm_enabled(self, tenant_id: int) -> bool:
        payload = await self._db_call(self.db.get_tenant_kv, int(tenant_id), PM_NAMESPACE, PM_ENABLED_KEY)
        if payload is None:
            return False
        if isinstance(payload, dict):
            if "enabled" in payload:
                return bool(payload.get("enabled"))
            return True
        return bool(payload)

    def _extract_trigger_type(self, payload: dict[str, Any]) -> str:
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

    async def _run_triage(
        self,
        *,
        tenant: Any,
        trigger: str,
        payload: dict[str, Any],
        pm_state: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        last_message = await self._db_call(self.db.get_latest_user_message, int(tenant.id))
        last_user_message_at = self._parse_dt(last_message.get("received_at") if last_message else None)
        if last_user_message_at:
            convo = pm_state.setdefault("conversation_state", {})
            convo["last_user_message_at"] = last_user_message_at.isoformat()
        cooldown_ok = self._message_cooldown_ok(pm_state, now)
        actions: list[dict[str, Any]] = []
        reason = "no action needed"
        priority = "low"

        has_deploy = bool(str(getattr(tenant, "last_deploy_url", "") or "").strip())
        deploy_url = str(getattr(tenant, "last_deploy_url", "") or "").strip() or None

        if trigger == "first_heartbeat":
            # First-heartbeat onboarding is immediate (no message-volume gate).
            # Keep this silent-by-default: if nothing is deployed yet, do not
            # emit suggestions; just wait for deploy-related triggers.
            if not has_deploy:
                return {
                    "action_needed": False,
                    "reason": "first heartbeat waiting for deploy",
                    "priority": "low",
                    "suggested_actions": [],
                }
            if deploy_url:
                actions.append(
                    {
                        "type": "auto_execute",
                        "action": "qa_check",
                        "description": "Check that the deployed project loads correctly.",
                        "context": {"url": deploy_url},
                    }
                )
            if cooldown_ok and last_user_message_at is not None:
                actions.append(
                    {
                        "type": "suggest_to_user",
                        "action": "status_report",
                        "description": (
                            "i reviewed your live project and found a few high-impact polish ideas. "
                            "want me to prepare a prioritized improvement pass?"
                        ),
                        "context": {"priority": "medium"},
                    }
                )
            reason = "first heartbeat ready"
            priority = "medium"
        elif trigger in {"daily_heartbeat", "post_run", "run_completed", "deploy_completed"}:
            # Autonomy-first: do not suppress deploy QA with hardcoded freshness
            # heuristics. Allow PM actioning to decide each heartbeat.
            if deploy_url:
                actions.append(
                    {
                        "type": "auto_execute",
                        "action": "qa_check",
                        "description": "Verify project health after recent changes.",
                        "context": {"url": deploy_url},
                    }
                )
                reason = "deploy-related trigger requested QA evaluation"
                priority = "medium"
        elif trigger in {"idle_check", "user_idle"}:
            if last_user_message_at and cooldown_ok:
                age_hours = (now - last_user_message_at).total_seconds() / 3600.0
                if age_hours >= float(self.config.idle_threshold_hours):
                    actions.append(
                        {
                            "type": "suggest_to_user",
                            "action": "follow_up",
                            "description": (
                                "quick check-in: want me to keep iterating on your project this week?"
                            ),
                            "context": {"priority": "low"},
                        }
                    )
                    reason = "user has been idle"
                    priority = "low"
        elif trigger in {"health_check", "pm_health_check"}:
            snapshot = await self._collect_health_snapshot(tenant_id=int(tenant.id), now=now)
            pm_state.setdefault("health", {}).update(
                {
                    "last_check_at": now.isoformat(),
                    "snapshot": snapshot,
                }
            )
            if int(snapshot.get("stale_messages") or 0) > 0:
                actions.append(
                    {
                        "type": "auto_execute",
                        "action": "fix_stale_received",
                        "description": "Recover unprocessed incoming messages.",
                        "context": {"count": int(snapshot.get("stale_messages") or 0)},
                    }
                )
            if int(snapshot.get("zombie_runs") or 0) > 0:
                actions.append(
                    {
                        "type": "auto_execute",
                        "action": "fix_zombie_runs",
                        "description": "Clean up stuck background tasks.",
                        "context": {"count": int(snapshot.get("zombie_runs") or 0)},
                    }
                )
            if int(snapshot.get("failed_outbox") or 0) > 0:
                actions.append(
                    {
                        "type": "auto_execute",
                        "action": "fix_failed_outbox",
                        "description": "Retry failed message deliveries.",
                        "context": {"count": int(snapshot.get("failed_outbox") or 0)},
                    }
                )
            if int(snapshot.get("stale_run_inputs") or 0) > 0:
                actions.append(
                    {
                        "type": "auto_execute",
                        "action": "fix_stale_run_inputs",
                        "description": "Process queued requests that got stuck.",
                        "context": {"count": int(snapshot.get("stale_run_inputs") or 0)},
                    }
                )
            if actions:
                reason = "system health issues detected"
                priority = "high"
        elif trigger in {"run_failed"} and cooldown_ok:
            source = self._extract_source_payload(payload)
            error_detail = str(source.get("error") or "").strip()
            user_request = str(source.get("execution_context") or "").strip()
            run_id = source.get("run_id")
            message_id = source.get("message_id")

            # Try to get the original user message text for even richer context.
            original_text = ""
            if message_id:
                try:
                    msg_row = await self._db_call(self.db.get_message, int(message_id))
                    if msg_row:
                        original_text = str(msg_row.get("text") or "").strip()
                except Exception:
                    pass

            # Build a context-rich description the interaction agent can use
            # to craft a natural, specific follow-up message.
            desc_parts = [
                "i noticed something went wrong while working on your request.",
            ]
            if original_text:
                desc_parts.append(f'you asked: "{original_text}"')
            elif user_request:
                desc_parts.append(f"task: {user_request}")
            if error_detail:
                # Keep it short — interaction agent will humanize this.
                short_error = error_detail[:200]
                desc_parts.append(f"what went wrong: {short_error}")
            desc_parts.append("want me to give it another shot with a different approach?")

            actions.append(
                {
                    "type": "suggest_to_user",
                    "action": "follow_up",
                    "description": " ".join(desc_parts),
                    "context": {
                        "priority": "high",
                        "error": error_detail[:500] if error_detail else None,
                        "user_request": original_text or user_request or None,
                        "failed_run_id": run_id,
                    },
                }
            )
            reason = "recent task failed"
            priority = "high"

        actions = actions[: max(1, int(self.config.max_actions_per_heartbeat))]
        return {
            "action_needed": bool(actions),
            "reason": reason,
            "priority": priority,
            "suggested_actions": actions,
            "trigger": trigger,
            "scheduler_payload": payload.get("payload"),
        }

    @staticmethod
    def _first_heartbeat_onboarding_succeeded(
        *,
        actions: list[dict[str, Any]],
        action_results: list[dict[str, Any]],
    ) -> bool:
        if not actions:
            return False
        needs_qa = any(str(action.get("action") or "").strip() == "qa_check" for action in actions)
        needs_suggestion = any(
            str(action.get("type") or "").strip() == "suggest_to_user"
            for action in actions
        )
        if needs_qa:
            qa_completed = any(
                str(result.get("action") or "").strip() == "qa_check"
                and str(result.get("status") or "").strip().lower() == "completed"
                for result in action_results
            )
            if not qa_completed:
                return False
        if needs_suggestion:
            suggestion_sent = any(
                str(result.get("autonomy_tier") or "").strip().lower() == "suggest"
                and str(result.get("outbox_id") or "").strip()
                for result in action_results
            )
            if not suggestion_sent:
                return False
        return needs_qa or needs_suggestion

    async def _apply_action(
        self,
        *,
        tenant: Any,
        heartbeat_id: int | None,
        action: dict[str, Any],
        pm_state: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        action_type = str(action.get("action") or "unknown").strip()
        autonomy_tier = "auto"
        if str(action.get("type") or "").strip() == "suggest_to_user":
            autonomy_tier = "suggest"
        description = str(action.get("description") or "").strip()
        context = action.get("context") if isinstance(action.get("context"), dict) else {}
        if autonomy_tier == "suggest":
            outbox_id = await self._enqueue_pm_suggestion(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                context=context,
                now=now,
                pm_state=pm_state,
            )
            action_id = await self._db_call(
                self.db.create_pm_action,
                tenant_id=int(tenant.id),
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                autonomy_tier="suggest",
                description=description,
                outbox_id=outbox_id,
                status="pending",
            )
            return {
                "action_id": action_id,
                "action": action_type,
                "autonomy_tier": "suggest",
                "status": "pending",
                "outbox_id": outbox_id,
            }

        if action_type == "qa_check":
            return await self._run_qa_check(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                context=context,
                pm_state=pm_state,
                now=now,
            )
        if action_type == "fix_stale_received":
            fixed = await self._fix_stale_received_messages(tenant=tenant)
            return await self._record_auto_action(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                status="completed",
                result_summary=f"recovered {fixed} unprocessed message(s)",
            )
        if action_type == "fix_zombie_runs":
            fixed = await self._fix_zombie_runs(tenant=tenant)
            return await self._record_auto_action(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                status="completed",
                result_summary=f"cleaned up {fixed} stuck task(s)",
            )
        if action_type == "fix_failed_outbox":
            fixed = await self._fix_failed_outbox(tenant=tenant)
            return await self._record_auto_action(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                status="completed",
                result_summary=f"retried {fixed} failed delivery(ies)",
            )
        if action_type == "fix_stale_run_inputs":
            fixed = await self._fix_stale_run_inputs(tenant=tenant)
            return await self._record_auto_action(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                status="completed",
                result_summary=f"processed {fixed} stuck queued request(s)",
            )
        return await self._record_auto_action(
            tenant=tenant,
            heartbeat_id=heartbeat_id,
            action_type=action_type,
            description=description,
            status="completed",
            result_summary="no-op",
        )

    async def _record_auto_action(
        self,
        *,
        tenant: Any,
        heartbeat_id: int | None,
        action_type: str,
        description: str,
        status: str,
        result_summary: str,
        execution_run_id: int | None = None,
    ) -> dict[str, Any]:
        action_id = await self._db_call(
            self.db.create_pm_action,
            tenant_id=int(tenant.id),
            heartbeat_id=heartbeat_id,
            action_type=action_type,
            autonomy_tier="auto",
            description=description,
            execution_run_id=execution_run_id,
            status=status,
            result_summary=result_summary,
        )
        return {
            "action_id": action_id,
            "action": action_type,
            "autonomy_tier": "auto",
            "status": status,
            "result_summary": result_summary,
            "execution_run_id": execution_run_id,
        }

    async def _run_qa_check(
        self,
        *,
        tenant: Any,
        heartbeat_id: int | None,
        action_type: str,
        description: str,
        context: dict[str, Any],
        pm_state: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        deploy_url = str(context.get("url") or getattr(tenant, "last_deploy_url") or "").strip()
        if not deploy_url:
            return await self._record_auto_action(
                tenant=tenant,
                heartbeat_id=heartbeat_id,
                action_type=action_type,
                description=description,
                status="failed",
                result_summary="missing_deploy_url",
            )
        status, summary = await self._check_site_health(deploy_url)
        project_key = str(context.get("project_name") or "default").strip() or "default"
        project = pm_state.setdefault("projects", {}).setdefault(project_key, {})
        project["last_deploy_url"] = deploy_url
        project["last_qa_at"] = now.isoformat()
        project["qa_status"] = status
        action_status = "completed" if status == "pass" else "failed"
        result = await self._record_auto_action(
            tenant=tenant,
            heartbeat_id=heartbeat_id,
            action_type=action_type,
            description=description,
            status=action_status,
            result_summary=summary,
        )
        project["open_issues"] = [] if status == "pass" else [summary]
        return result

    async def _check_site_health(self, url: str) -> tuple[str, str]:
        timeout = max(1.0, float(self.config.qa_timeout_seconds))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
            if 200 <= int(response.status_code) < 400:
                return "pass", f"reachable ({response.status_code})"
            return "fail", f"returned status {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            return "fail", f"health check failed: {type(exc).__name__}"

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
                await self.orchestrator._finalize_stale_run(tenant, row, notify=True)
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

    async def _enqueue_pm_suggestion(
        self,
        *,
        tenant: Any,
        heartbeat_id: int | None,
        action_type: str,
        description: str,
        context: dict[str, Any],
        now: datetime,
        pm_state: dict[str, Any],
    ) -> str:
        correlation_id = f"pm-{heartbeat_id or 'na'}-{action_type}-{uuid.uuid4().hex[:8]}"
        payload = {
            "type": "pm_suggestion",
            "action": "pm_suggestion",
            "text": description,
            "priority": str(context.get("priority") or "medium"),
            "pm_action_type": action_type,
            "pm_heartbeat_id": heartbeat_id,
            "autonomy_tier": "suggest",
            "tenant_external_id": str(getattr(tenant, "external_id", "") or ""),
            "provider": str(getattr(tenant, "provider", "") or ""),
            "context": context,
        }
        outbox_id = await self._db_call(
            self.db.enqueue_outbox,
            tenant_id=int(tenant.id),
            run_id=None,
            project_name=None,
            correlation_id=correlation_id,
            payload=payload,
        )
        outbox_id = str(outbox_id or "").strip()
        if not outbox_id:
            raise RuntimeError("pm_suggestion_enqueue_failed")
        pm_state.setdefault("conversation_state", {})["last_pm_message_at"] = now.isoformat()
        return outbox_id

    def _message_cooldown_ok(self, pm_state: dict[str, Any], now: datetime) -> bool:
        convo = pm_state.get("conversation_state")
        if not isinstance(convo, dict):
            return True
        last_pm_message_at = self._parse_dt(convo.get("last_pm_message_at"))
        if last_pm_message_at is None:
            return True
        elapsed = (now - last_pm_message_at).total_seconds()
        return elapsed >= float(self.config.cooldown_between_user_messages_seconds)

    async def _disable_first_heartbeat_trigger(self, tenant_id: int) -> None:
        trigger = await self._db_call(
            self.db.get_tenant_kv,
            int(tenant_id),
            SCHEDULER_NAMESPACE,
            PM_FIRST_HEARTBEAT_TRIGGER_KEY,
        )
        if not isinstance(trigger, dict):
            return
        trigger = dict(trigger)
        trigger["enabled"] = False
        trigger["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        await self._db_call(
            self.db.set_tenant_kv,
            int(tenant_id),
            SCHEDULER_NAMESPACE,
            PM_FIRST_HEARTBEAT_TRIGGER_KEY,
            trigger,
        )

    def _read_pm_state(self, tenant: Any) -> dict[str, Any]:
        path = self._pm_state_path(tenant)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                return payload
        now = datetime.now(tz=timezone.utc).isoformat()
        return {
            "version": 1,
            "tenant_id": int(getattr(tenant, "id")),
            "created_at": now,
            "updated_at": now,
            "projects": {},
            "research": {},
            "conversation_state": {},
            "plan": {"current_phase": "monitoring", "milestones": []},
            "health": {
                "last_check_at": None,
                "status": "unknown",
                "issues_detected_24h": 0,
                "issues_auto_fixed_24h": 0,
            },
            "metrics": {
                "total_heartbeats": 0,
                "total_actions_taken": 0,
                "total_suggestions_made": 0,
                "total_suggestions_approved": 0,
                "total_suggestions_rejected": 0,
                "total_research_runs": 0,
                "total_cost_usd": 0.0,
                "session_rotations": 0,
            },
        }

    def _write_pm_state(self, tenant: Any, state: dict[str, Any]) -> None:
        path = self._pm_state_path(tenant)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")
        except OSError:
            return

    def _pm_state_path(self, tenant: Any) -> Path:
        workspace_path = getattr(tenant, "workspace_path", None)
        if workspace_path:
            try:
                tenant_root = self.workspace_manager.infer_tenant_root(Path(workspace_path))
                return tenant_root / "pm_state.json"
            except Exception:
                pass
        tenant_root = self.workspace_manager.tenant_root_for_key(str(getattr(tenant, "key") or ""))
        return Path(tenant_root) / "pm_state.json"

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

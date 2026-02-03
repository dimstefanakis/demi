from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import json
import re
import asyncio
import httpx

from demi.db.core import Database
from demi.models import Attachment, NormalizedMessage, OrchestratorResult
from demi.workspace.core import Workspace, WorkspaceManager
from demi.messaging.telegram import TelegramUpdateParser
from demi.agent.inflight import InflightTextStream
from demi.memory import build_memory_prompt, build_summarization_prompt, read_logs
from demi.memory.logs import append_log, write_chat_history

from demi.failure_guard import clear_block, get_block, record_hard_failure
from demi.tenant_db import ensure_tenant_db
from demi.workspace.project_decider import decide_project
from demi.domains.github_app import GitHubAppConfig, GitHubRepoManager
from demi.config import Settings


@dataclass
class Orchestrator:
    db: Database
    workspace_manager: WorkspaceManager
    agent: Any
    messenger: Any
    payments: Any | None = None
    inflight_text_queues: dict[str, InflightTextStream] | None = None
    workspace_allocator: Any | None = None

    async def handle_message(self, msg: NormalizedMessage) -> OrchestratorResult:
        tenant = self.db.get_or_create_tenant(msg.provider, msg.tenant_external_id)

        message_id, inserted = self.db.record_message(tenant.id, msg)
        if not inserted:
            return OrchestratorResult(status="duplicate", detail="message already processed")

        msg, project_name = self._resolve_project_for_tenant(tenant, msg)
        if project_name:
            self.db.update_message_project(message_id, project_name)
        user_payload = (msg.text or "").strip()
        if not user_payload and msg.images:
            user_payload = "(attachment)"
        workspace = await self._resolve_workspace(tenant, project_name=project_name)

        if self._is_reset_command(user_payload):
            self._append_chat_log(workspace.tasks_dir, "user", user_payload, msg.received_at)
            write_chat_history(workspace.tasks_dir)
            self._reset_state(workspace, tenant, project_name=project_name)
            self.db.update_message_status(message_id, "processed")
            await self._send_interaction_message(
                workspace,
                tenant,
                msg,
                "Reset done. I cleared stuck runs and pending requests. "
                "Send your request again and I’ll pick it up.",
            )
            return OrchestratorResult(status="accepted", detail="reset")

        if await self._handle_blocked(workspace.tasks_dir, tenant, user_payload=user_payload):
            self.db.update_message_status(message_id, "processed")
            return OrchestratorResult(status="blocked", detail="system_blocked")
        self._append_chat_log(workspace.tasks_dir, "user", user_payload, msg.received_at)
        write_chat_history(workspace.tasks_dir)

        settings = Settings()
        self.db.expire_stale_runs(tenant.id, project_name, self._now())
        inflight_run = self.db.get_inflight_run(tenant.id, project_name)
        if inflight_run:
            if self._reconcile_inflight_run(tenant, inflight_run, workspace):
                inflight_run = None
        if inflight_run and self._is_run_stale(inflight_run, max_age_seconds=900):
            await self._finalize_stale_run(tenant, inflight_run)
            inflight_run = None

        active_run = self.db.get_active_run(tenant.id, project_name)
        if active_run:
            if not inflight_run or int(active_run["run_id"]) != int(inflight_run["id"]):
                self.db.clear_active_run(tenant.id, project_name)
                active_run = None

        if active_run:
            self.db.update_message_status(message_id, "queued")
            self._enqueue_run_input(
                tenant_id=tenant.id,
                run_id=int(active_run["run_id"]),
                project_name=project_name,
                message_id=message_id,
                msg=msg,
                status="queued",
            )
            self._write_request_status(workspace, tenant)
            await self._send_busy_ack(workspace, tenant, msg)
            return OrchestratorResult(status="busy", detail="tenant already running")

        lease_expires = (self._now() + timedelta(seconds=settings.run_lease_seconds)).isoformat()
        run_id = self.db.create_run(
            tenant.id,
            message_id=message_id,
            project_name=workspace.project_name,
            lease_seconds=settings.run_lease_seconds,
        )
        self.db.set_active_run(tenant.id, workspace.project_name or "main", run_id, lease_expires)
        run_input_id = self._enqueue_run_input(
            tenant_id=tenant.id,
            run_id=run_id,
            project_name=project_name,
            message_id=message_id,
            msg=msg,
            status="claimed",
        )
        return await self._run_message(
            tenant=tenant,
            msg=msg,
            message_id=message_id,
            message_ids=[message_id],
            run_id=run_id,
            run_input_ids=[run_input_id],
            process_queue=True,
            project_name=project_name,
        )

    async def handle_event_job(
        self,
        tenant_id: int,
        payload: dict[str, Any],
        job_id: int,
    ) -> OrchestratorResult:
        tenant = self.db.get_tenant_by_id(tenant_id)
        if tenant is None:
            return OrchestratorResult(status="invalid", detail="tenant_not_found")

        project_name = self._resolve_project_from_payload(payload)
        workspace = await self._resolve_workspace(tenant, project_name=project_name)
        event_intent = str(payload.get("intent") or "").strip()
        event_type = str(payload.get("event_type") or "").strip()
        if event_intent == "system_blocked" or event_type == "system_blocked":
            if not get_block(workspace.tasks_dir, "system"):
                return OrchestratorResult(status="accepted", detail="block_cleared")
        else:
            if await self._handle_blocked(workspace.tasks_dir, tenant, notify=False):
                return OrchestratorResult(status="blocked", detail="system_blocked")
        self.db.expire_stale_runs(tenant.id, project_name, self._now())
        inflight_run = self.db.get_inflight_run(tenant.id, project_name)
        if inflight_run:
            if self._reconcile_inflight_run(tenant, inflight_run, workspace):
                inflight_run = None
        if inflight_run and self._is_run_stale(inflight_run, max_age_seconds=900):
            await self._finalize_stale_run(tenant, inflight_run)
            inflight_run = None

        active_run = self.db.get_active_run(tenant.id, project_name)
        if active_run:
            if not inflight_run or int(active_run["run_id"]) != int(inflight_run["id"]):
                self.db.clear_active_run(tenant.id, project_name)
                active_run = None

        event_type = str(payload.get("event_type") or "event").strip()
        event_payload = payload.get("payload") or {}
        intent = payload.get("intent")
        event_text = self._build_event_text(event_type, event_payload, intent=intent)
        msg = NormalizedMessage(
            provider="event",
            provider_message_id=f"event-{job_id}",
            tenant_external_id=tenant.external_id,
            received_at=self._now(),
            text=event_text,
            images=[],
            raw={"event": payload},
            project_name=project_name,
        )
        message_id, inserted = self.db.record_message(tenant.id, msg)
        if not inserted:
            return OrchestratorResult(status="duplicate", detail="event already processed")
        if active_run:
            self.db.update_message_status(message_id, "queued")
            self._enqueue_run_input(
                tenant_id=tenant.id,
                run_id=int(active_run["run_id"]),
                project_name=project_name,
                message_id=message_id,
                msg=msg,
                status="queued",
            )
            self._write_request_status(workspace, tenant)
            return OrchestratorResult(status="busy", detail="tenant already running")

        settings = Settings()
        lease_expires = (self._now() + timedelta(seconds=settings.run_lease_seconds)).isoformat()
        run_id = self.db.create_run(
            tenant.id,
            message_id=message_id,
            project_name=workspace.project_name,
            lease_seconds=settings.run_lease_seconds,
        )
        self.db.set_active_run(tenant.id, workspace.project_name or "main", run_id, lease_expires)
        run_input_id = self._enqueue_run_input(
            tenant_id=tenant.id,
            run_id=run_id,
            project_name=project_name,
            message_id=message_id,
            msg=msg,
            status="claimed",
        )
        return await self._run_message(
            tenant=tenant,
            msg=msg,
            message_id=message_id,
            message_ids=[message_id],
            run_id=run_id,
            run_input_ids=[run_input_id],
            process_queue=True,
            project_name=project_name,
        )

    async def _run_message(
        self,
        tenant,
        msg: NormalizedMessage,
        message_id: int,
        message_ids: list[int] | None,
        process_queue: bool,
        project_name: str | None = None,
        run_id: int | None = None,
        run_input_ids: list[str] | None = None,
    ) -> OrchestratorResult:
        workspace = await self._resolve_workspace(tenant, project_name=project_name)
        message_ids = message_ids or ([message_id] if message_id else [])
        if message_ids:
            self.db.update_message_statuses(message_ids, "processing")
        self._write_request_status(workspace, tenant)

        asset_paths = []
        if msg.images and hasattr(self.messenger, "download_images"):
            asset_paths = await self.messenger.download_images(msg.images, workspace.assets_dir)

        billing_status = None
        if msg.provider != "event":
            billing_status = await self._fetch_billing_status(
                tenant=tenant,
                msg=msg,
                project_name=workspace.project_name,
                tasks_dir=workspace.tasks_dir,
            )
            if billing_status is not None:
                self._write_billing_status(workspace.tasks_dir, billing_status)

        task_content = self._build_task_content(msg, asset_paths, billing_status)
        task_path = workspace.write_task(task_content)
        self._write_run_request(
            workspace=workspace,
            task_path=task_path,
            msg=msg,
            session_id=getattr(tenant, "session_id", None),
        )

        self._clear_run_artifacts(workspace.tasks_dir)
        self._maybe_prepare_compaction(workspace.tasks_dir)
        self._maybe_prepare_memory_update(workspace.tasks_dir, workspace.memory_path)

        settings = Settings()
        if run_id is None:
            run_id = self.db.create_run(
                tenant.id,
                message_id=message_id,
                project_name=workspace.project_name,
                lease_seconds=settings.run_lease_seconds,
            )
        lease_expires = (self._now() + timedelta(seconds=settings.run_lease_seconds)).isoformat()
        self.db.set_active_run(tenant.id, workspace.project_name or "main", run_id, lease_expires)
        inflight_stream = self._ensure_inflight_stream(tenant.key, workspace.project_name)
        monitor = None
        monitor_task = None
        try:
            monitor = RunActivityMonitor(
                db=self.db,
                run_id=run_id,
                tasks_dir=workspace.tasks_dir,
                lease_seconds=settings.run_lease_seconds,
                poll_interval=settings.run_activity_poll_interval,
            )
            monitor_task = asyncio.create_task(monitor.run())
            runtime_env = await self._github_runtime_env(settings, workspace, tenant)
            agent_result = await self.agent.prepare_context(
                workspace=workspace,
                task_path=task_path,
                message=msg,
                messenger=self.messenger,
                inflight_stream=inflight_stream,
                tenant_id=tenant.id,
                db=self.db,
                payments=self.payments,
                session_id=tenant.session_id,
                runtime_env=runtime_env,
            )
            if agent_result.session_id:
                self.db.update_tenant_session(tenant.id, agent_result.session_id)
            self.db.update_run_usage(
                run_id,
                total_cost_usd=getattr(agent_result, "total_cost_usd", None),
                usage=getattr(agent_result, "usage", None),
            )
            self.db.finish_run(run_id, status="completed")
            if message_ids:
                self.db.update_message_statuses(message_ids, "processed")
            if run_input_ids:
                self.db.update_run_inputs_statuses(run_input_ids, "handled")
            self._clear_inflight_stream(tenant.key, workspace.project_name)
            self.db.clear_active_run(tenant.id, workspace.project_name)
            await self._maybe_send_interaction_request(workspace, tenant, msg, run_id=run_id)
            if process_queue:
                await self._drain_run_inputs(tenant, project_name=workspace.project_name)
            self._write_request_status(workspace, tenant)
            return OrchestratorResult(status="accepted")
        except Exception as exc:  # noqa: BLE001
            self.db.finish_run(run_id, status="failed", error=str(exc))
            if message_ids:
                self.db.update_message_statuses(message_ids, "failed")
            if run_input_ids:
                self.db.update_run_inputs_statuses(run_input_ids, "queued")
            self._clear_inflight_stream(tenant.key, workspace.project_name)
            self.db.clear_active_run(tenant.id, workspace.project_name)
            self._write_request_status(workspace, tenant)
            raise
        finally:
            if monitor is not None:
                monitor.stop()
            if monitor_task is not None:
                try:
                    await monitor_task
                except Exception:
                    pass

    async def _drain_run_inputs(self, tenant, project_name: str | None = None) -> None:
        while True:
            rows = self.db.claim_run_inputs_for_project(
                tenant.id,
                project_name,
                limit=10,
            )
            if not rows:
                return
            run_input_ids = [str(row["id"]) for row in rows]
            combined, message_ids = self._combine_run_inputs(rows)
            if not combined:
                self.db.update_run_inputs_statuses(run_input_ids, "queued")
                return
            primary_message_id = message_ids[0] if message_ids else 0
            await self._run_message(
                tenant=tenant,
                msg=combined,
                message_id=primary_message_id,
                message_ids=message_ids,
                run_input_ids=run_input_ids,
                process_queue=False,
                project_name=project_name,
            )

    @staticmethod
    def _is_run_stale(
        row: Any,
        max_age_seconds: int = 1800,
    ) -> bool:
        from datetime import datetime, timezone

        def _parse_dt(value: Any) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed

        now = datetime.now(tz=timezone.utc)
        lease_expires_at = None
        try:
            lease_expires_at = row["lease_expires_at"]
        except (KeyError, TypeError):
            lease_expires_at = None
        lease_dt = _parse_dt(lease_expires_at)
        if lease_dt is not None:
            return lease_dt <= now

        last_activity_at = None
        last_heartbeat_at = None
        started_at = None
        try:
            last_activity_at = row["last_activity_at"]
            last_heartbeat_at = row["last_heartbeat_at"]
            started_at = row["started_at"]
        except (KeyError, TypeError):
            pass

        for candidate in (last_activity_at, last_heartbeat_at, started_at):
            candidate_dt = _parse_dt(candidate)
            if candidate_dt is None:
                continue
            age = (now - candidate_dt).total_seconds()
            return age > max_age_seconds
        return False

    async def _finalize_stale_run(self, tenant, row: Any) -> None:
        self.db.finish_run(row["id"], status="failed", error="stale_run_timeout")
        message_id = row.get("message_id") if hasattr(row, "get") else row["message_id"]
        if message_id:
            self.db.update_message_status(int(message_id), "processed")
        project_name = row["project_name"] if row and "project_name" in row.keys() else None
        self._clear_inflight_stream(tenant.key, project_name)

    @staticmethod
    def _build_task_content(
        msg: NormalizedMessage,
        asset_paths: list[str] | None = None,
        billing_status: dict[str, Any] | None = None,
    ) -> str:
        message_text = (msg.text or "").strip()
        if not message_text and msg.images:
            message_text = "(attachment only)"
        lines = ["# Task", ""]
        if msg.project_name:
            lines.append(f"Project: {msg.project_name}")
            lines.append("")
        lines.append(f"Message: {message_text}".strip())
        if msg.images:
            lines.append("\n## Images")
            for image in msg.images:
                lines.append(f"- {image.provider_file_id} ({image.width}x{image.height})")
        if asset_paths:
            lines.append("\n## Saved Assets")
            for path in asset_paths:
                lines.append(f"- {path}")
        if billing_status:
            lines.append("\n## Billing")
            status = billing_status.get("status")
            if status:
                lines.append(f"Status: {status}")
            purpose = billing_status.get("purpose")
            if purpose:
                lines.append(f"Purpose: {purpose}")
            purpose_label = billing_status.get("purpose_label")
            if purpose_label:
                lines.append(f"Purpose label: {purpose_label}")
            payment_required = billing_status.get("payment_required")
            if payment_required is not None:
                lines.append(f"Payment required: {payment_required}")
            allow_first_build = billing_status.get("allow_first_build")
            if allow_first_build is not None:
                lines.append(f"Allow first build: {allow_first_build}")
            plan = billing_status.get("plan") or billing_status.get("tier")
            if plan:
                lines.append(f"Plan: {plan}")
            payment_url = billing_status.get("payment_url")
            if payment_url:
                lines.append(f"Payment URL: {payment_url}")
            message = billing_status.get("message")
            if message:
                lines.append(f"Message: {message}")
            lines.append("Full payload: tasks/billing_status.json")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _write_run_request(
        *,
        workspace: Workspace,
        task_path: Path,
        msg: NormalizedMessage,
        session_id: str | None,
    ) -> None:
        payload = {
            "workspace_root": str(workspace.root),
            "task_path": str(task_path),
            "session_id": session_id,
            "message": {
                "provider": msg.provider,
                "provider_message_id": msg.provider_message_id,
                "tenant_external_id": msg.tenant_external_id,
                "received_at": msg.received_at.isoformat(),
                "text": msg.text,
                "images": [
                    {
                        "provider_file_id": image.provider_file_id,
                        "width": image.width,
                        "height": image.height,
                    }
                    for image in msg.images
                ],
            },
        }
        path = workspace.tasks_dir / "run_request.json"
        try:
            path.write_text(json.dumps(payload, indent=2))
        except OSError:
            return

    def _resolve_project_from_message(
        self, msg: NormalizedMessage
    ) -> tuple[NormalizedMessage, str | None]:
        project_name = getattr(msg, "project_name", None)
        if not project_name:
            project_name = self._resolve_project_from_payload(msg.raw or {})
        text = msg.text
        if not project_name:
            project_name, text = self._extract_project_directive(text)
        if text != msg.text:
            msg = replace(msg, text=text)
        if project_name:
            project_name = self.workspace_manager.normalize_project_name(project_name)
        return msg, project_name

    def _resolve_project_for_tenant(
        self, tenant: Any, msg: NormalizedMessage
    ) -> tuple[NormalizedMessage, str | None]:
        msg, project_name = self._resolve_project_from_message(msg)
        if project_name:
            if msg.project_name != project_name:
                msg = replace(msg, project_name=project_name)
            return msg, project_name
        tenant_root = self._tenant_root_for(tenant)
        decision = decide_project(tenant_root, msg.text, payload=msg.raw or {})
        inferred = decision.project_name if decision else None
        if inferred and msg.project_name != inferred:
            msg = replace(msg, project_name=inferred)
        if inferred:
            self.workspace_manager.set_active_project(tenant_root, inferred)
        return msg, inferred

    def _tenant_root_for(self, tenant: Any) -> Path:
        workspace_path = getattr(tenant, "workspace_path", None)
        if workspace_path:
            return self.workspace_manager.infer_tenant_root(Path(workspace_path))
        return self.workspace_manager.tenant_root_for_key(tenant.key)

    @staticmethod
    def _extract_project_directive(text: str | None) -> tuple[str | None, str | None]:
        if not text:
            return None, text
        lines = text.splitlines()
        if not lines:
            return None, text
        first = lines[0].strip()
        match = re.match(r"^project\s*[:=]\s*(.+)$", first, flags=re.IGNORECASE)
        if match is None:
            match = re.match(r"^/project\s+(.+)$", first, flags=re.IGNORECASE)
        if not match:
            return None, text
        project = match.group(1).strip()
        remaining = "\n".join(lines[1:]).strip()
        return project, remaining or None

    @staticmethod
    def _is_reset_command(text: str | None) -> bool:
        if not text:
            return False
        return bool(re.match(r"^/reset(?:@\w+)?(?:\s|$)", text.strip(), flags=re.IGNORECASE))

    @staticmethod
    def _resolve_project_from_payload(payload: dict[str, Any]) -> str | None:
        def _pick(data: dict[str, Any] | None) -> str | None:
            if not data:
                return None
            for key in ("project_name", "project", "project_id", "projectId"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        project = _pick(payload)
        if project:
            return project
        project = _pick(
            payload.get("payload") if isinstance(payload.get("payload"), dict) else None
        )
        if project:
            return project
        return _pick(payload.get("event") if isinstance(payload.get("event"), dict) else None)

    @staticmethod
    def _build_event_text(
        event_type: str,
        payload: dict[str, Any],
        intent: str | None = None,
    ) -> str:
        summary = payload.get("summary") or payload.get("message") or ""
        summary = str(summary).strip()
        header = f"EVENT ({event_type})"
        if summary:
            header = f"{header}: {summary}"
        notify = bool(payload.get("notify") or payload.get("notify_text"))
        intent_line = (
            f"- Intent: {intent.strip()}\n"
            if isinstance(intent, str) and intent.strip()
            else ""
        )
        notify_line = "- This event requests a user notification.\n" if notify else ""
        return (
            f"{header}\n\n"
            "Context:\n"
            f"{intent_line}"
            f"{notify_line}"
            "- The full event payload was stored in the project SQLite DB "
            "(tenant.sqlite in the project root)\n"
            "- Table: events (columns: event_type, payload_json, received_at)\n"
            "- You may query or update this DB if needed.\n"
        )

    @staticmethod
    def _now():
        from datetime import datetime, timezone

        return datetime.now(tz=timezone.utc)

    @staticmethod
    def _append_inflight_update(
        tasks_dir: Path, msg: NormalizedMessage, asset_paths: list[str], message_id: int
    ) -> None:
        updates_path = tasks_dir / "inflight_updates.jsonl"
        payload = {
            "timestamp": msg.received_at.isoformat(),
            "text": msg.text or "",
            "assets": asset_paths or [],
            "provider_message_id": msg.provider_message_id,
            "message_id": message_id,
        }
        updates_path.parent.mkdir(parents=True, exist_ok=True)
        with updates_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _consume_inflight_updates(self, tasks_dir: Path) -> list[int]:
        consumed_path = tasks_dir / "inflight_consumed.jsonl"
        if not consumed_path.exists():
            return []
        message_ids: list[int] = []
        for line in consumed_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            raw_id = payload.get("message_id")
            try:
                message_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            message_ids.append(message_id)
        if message_ids:
            self.db.update_message_statuses(message_ids, "processed")
        try:
            consumed_path.unlink()
        except OSError:
            pass
        return message_ids

    def _write_request_status(self, workspace: Workspace, tenant: Any) -> None:
        try:
            queued = self.db.fetch_run_inputs(
                tenant.id,
                workspace.project_name,
                status="queued",
                limit=25,
            )
            inflight = self.db.get_inflight_run(tenant.id, project_name=workspace.project_name)
            lines = [
                "# Request Status",
                "",
                f"Project: {workspace.project_name}",
                f"Run: {inflight['id'] if inflight else 'none'}",
                f"Run Status: {inflight['status'] if inflight else 'idle'}",
                "",
                "## Queued Run Inputs",
            ]
            if not queued:
                lines.append("- None")
            else:
                for row in queued:
                    payload = row.get("payload_json") if isinstance(row, dict) else None
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = {}
                    if not isinstance(payload, dict):
                        payload = {}
                    text = str(payload.get("text") or "").strip().replace("\n", " ")
                    received_at = payload.get("received_at") or row.get("created_at")
                    lines.append(f"- {received_at}: {text}")
            status_path = workspace.tasks_dir / "request_status.md"
            status_path.write_text("\n".join(lines) + "\n")
        except Exception:
            pass

    async def _send_busy_ack(
        self, workspace: Workspace, tenant: Any, msg: NormalizedMessage
    ) -> None:
        user_text = (msg.text or "").strip()
        if not user_text and msg.images:
            user_text = "(attachment)"
        instruction = (
            "Acknowledge the user's new request while another task is in progress. "
            "Say it's queued and you'll handle it next. "
            "Keep it brief, friendly, and non-technical."
        )
        if user_text:
            instruction = f"{instruction}\n\nUser message:\n{user_text}"
        sent = await self._send_interaction_instruction(
            workspace=workspace,
            tenant=tenant,
            msg=msg,
            instruction=instruction,
            run_id=None,
        )
        if sent:
            return
        try:
            append_log(
                workspace.tasks_dir,
                "system",
                "busy_ack_failed: interaction_agent_unavailable",
            )
        except Exception:
            pass

    async def _send_interaction_instruction(
        self,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage | None,
        instruction: str,
        run_id: int | None = None,
    ) -> bool:
        sender = getattr(self.agent, "send_interaction_instruction", None)
        if sender is None:
            return False
        try:
            result = await sender(
                workspace=workspace,
                instruction=instruction,
                messenger=self.messenger,
                tenant_id=tenant.id,
                db=self.db,
                payments=self.payments,
                session_id=getattr(tenant, "session_id", None),
                provider=getattr(msg, "provider", None) or getattr(tenant, "provider", None),
                tenant_external_id=getattr(msg, "tenant_external_id", None)
                or getattr(tenant, "external_id", None),
            )
            self._record_interaction_usage(run_id, result, workspace.tasks_dir)
            return True
        except Exception as exc:  # noqa: BLE001
            try:
                append_log(
                    workspace.tasks_dir,
                    "system",
                    f"interaction_instruction_failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        return False

    def _enqueue_outbox_message(
        self,
        *,
        tenant: Any,
        text: str,
        correlation_id: str,
        run_id: int | None,
        project_name: str | None,
    ) -> None:
        payload = {
            "tenant_external_id": getattr(tenant, "external_id", None),
            "provider": getattr(tenant, "provider", None),
            "text": text,
        }
        try:
            self.db.enqueue_outbox(
                tenant_id=int(tenant.id),
                run_id=run_id,
                project_name=project_name,
                correlation_id=correlation_id,
                payload=payload,
            )
        except Exception:
            return

    async def _send_interaction_message(
        self,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage | None,
        text: str,
        run_id: int | None = None,
    ) -> None:
        sender = getattr(self.agent, "send_interaction_message", None)
        if sender is None:
            return
        try:
            result = await sender(
                workspace=workspace,
                text=text,
                messenger=self.messenger,
                tenant_id=tenant.id,
                db=self.db,
                payments=self.payments,
                session_id=getattr(tenant, "session_id", None),
                provider=getattr(msg, "provider", None) or getattr(tenant, "provider", None),
                tenant_external_id=getattr(msg, "tenant_external_id", None)
                or getattr(tenant, "external_id", None),
            )
            self._record_interaction_usage(run_id, result, workspace.tasks_dir)
        except Exception as exc:  # noqa: BLE001
            try:
                append_log(
                    workspace.tasks_dir,
                    "system",
                    f"interaction_send_failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass

    async def _maybe_send_interaction_request(
        self,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage,
        run_id: int | None = None,
    ) -> None:
        path = workspace.tasks_dir / "interaction_request.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = None
        try:
            path.unlink()
        except OSError:
            pass
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or "").strip().lower()
        if kind == "send_message":
            text = str(payload.get("text") or "").strip()
            if not text:
                return
            final = bool(payload.get("final", False))
            instruction = (
                "Send the message below to the user. "
                f"Set final to {str(final).lower()}.\n\n"
                f"MESSAGE:\n{text}"
            )
            await self._send_interaction_instruction(
                workspace=workspace,
                tenant=tenant,
                msg=msg,
                instruction=instruction,
                run_id=run_id,
            )
            return
        if kind == "send_payment_link":
            text = str(payload.get("text") or "").strip()
            order_id = payload.get("order_id")
            source = str(payload.get("source") or "").strip()
            final = bool(payload.get("final", False))
            parts = [
                "Send a payment link to the user using send_payment_link.",
                f"Set final to {str(final).lower()}.",
            ]
            if order_id is not None:
                parts.append(f"Order ID: {order_id}")
            if source:
                parts.append(f"Source: {source}")
            if text:
                parts.append(f"Message text:\n{text}")
            instruction = "\n".join(parts)
            await self._send_interaction_instruction(
                workspace=workspace,
                tenant=tenant,
                msg=msg,
                instruction=instruction,
                run_id=run_id,
            )
            return

    def _record_interaction_usage(
        self,
        run_id: int | None,
        result: Any,
        tasks_dir: Path,
    ) -> None:
        if run_id is None or result is None:
            return
        total_cost = getattr(result, "total_cost_usd", None)
        usage = getattr(result, "usage", None)
        if total_cost is None and not usage:
            return
        if isinstance(usage, dict):
            usage_payload = dict(usage)
        elif usage is None:
            usage_payload = {}
        else:
            usage_payload = {"raw": usage}
        if total_cost is not None:
            usage_payload["total_cost_usd"] = total_cost
        try:
            self.db.add_run_usage(
                run_id,
                total_cost_usd=total_cost,
                usage=usage_payload,
                usage_key="interaction",
            )
        except Exception as exc:  # noqa: BLE001
            try:
                append_log(
                    tasks_dir,
                    "system",
                    f"interaction_usage_failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass

    def _reset_state(
        self,
        workspace: Workspace,
        tenant: Any,
        project_name: str | None = None,
    ) -> None:
        self.db.finish_running_runs(tenant.id, project_name, error="user_reset")
        self.db.clear_pending_and_processing_messages(tenant.id, project_name)
        self.db.clear_active_run(tenant.id, project_name)
        self.db.cancel_run_inputs(tenant.id, project_name)
        self._clear_inflight_stream(tenant.key, project_name)
        self._clear_run_artifacts(workspace.tasks_dir)
        for name in (
            "inflight_updates.jsonl",
            "inflight_consumed.jsonl",
            "run_request.json",
            "run_result.json",
        ):
            path = workspace.tasks_dir / name
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        self._write_request_status(workspace, tenant)

    async def migrate_legacy_queue(self) -> int:
        migrated = 0
        tenants = self.db.list_tenants()
        if not tenants:
            return 0
        now = self._now()
        for tenant in tenants:
            rows = self.db.fetch_messages_by_statuses(
                tenant.id,
                ["pending", "processing"],
            )
            if not rows:
                continue
            grouped: dict[str | None, list[Any]] = {}
            for row in rows:
                project_name = row["project_name"] if "project_name" in row.keys() else None
                grouped.setdefault(project_name, []).append(row)
            for project_name, group_rows in grouped.items():
                self.db.expire_stale_runs(tenant.id, project_name, now)
                inflight = self.db.get_inflight_run(tenant.id, project_name)
                if inflight:
                    continue
                self.db.finish_running_runs(tenant.id, project_name, error="migrated")
                self.db.clear_active_run(tenant.id, project_name)
                message_ids: list[int] = []
                for row in group_rows:
                    message_id = int(row["id"])
                    message_ids.append(message_id)
                    msg = self._message_from_row(row)
                    if not msg:
                        try:
                            received_raw = row["received_at"] if "received_at" in row.keys() else None
                            received_at = (
                                datetime.fromisoformat(str(received_raw))
                                if received_raw
                                else self._now()
                            )
                            if received_at.tzinfo is None:
                                received_at = received_at.replace(tzinfo=timezone.utc)
                        except Exception:
                            received_at = self._now()
                        msg = NormalizedMessage(
                            provider=str(row.get("provider") if hasattr(row, "get") else row["provider"]),
                            provider_message_id=str(
                                row.get("provider_message_id")
                                if hasattr(row, "get")
                                else row["provider_message_id"]
                            ),
                            tenant_external_id=str(tenant.external_id),
                            received_at=received_at,
                            text=str(
                                row.get("text") if hasattr(row, "get") else row["text"] or ""
                            )
                            or None,
                            images=[],
                            raw={},
                            project_name=project_name,
                        )
                    self._enqueue_run_input(
                        tenant_id=tenant.id,
                        run_id=None,
                        project_name=project_name,
                        message_id=message_id,
                        msg=msg,
                        status="queued",
                    )
                    migrated += 1
                if message_ids:
                    self.db.update_message_statuses(message_ids, "processed")
        return migrated

    def _combine_pending_messages(self, rows: list[Any]) -> NormalizedMessage | None:
        messages: list[NormalizedMessage] = []
        for row in rows:
            msg = self._message_from_row(row)
            if msg:
                messages.append(msg)
        if not messages:
            return None

        combined_text = "\n".join(
            [m.text.strip() for m in messages if m.text and m.text.strip()]
        ).strip()
        combined_images = []
        for m in messages:
            combined_images.extend(m.images)

        latest = messages[-1]
        return NormalizedMessage(
            provider=latest.provider,
            provider_message_id=latest.provider_message_id,
            tenant_external_id=latest.tenant_external_id,
            received_at=latest.received_at,
            text=combined_text or None,
            images=combined_images,
            raw=latest.raw,
            project_name=latest.project_name,
        )

    @staticmethod
    def _build_run_input_payload(msg: NormalizedMessage, message_id: int) -> dict[str, Any]:
        return {
            "message_id": message_id,
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "tenant_external_id": msg.tenant_external_id,
            "received_at": msg.received_at.isoformat(),
            "text": msg.text,
            "images": [
                {
                    "provider_file_id": img.provider_file_id,
                    "width": img.width,
                    "height": img.height,
                }
                for img in msg.images
            ],
            "raw": msg.raw,
            "project_name": msg.project_name,
        }

    def _enqueue_run_input(
        self,
        *,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        message_id: int,
        msg: NormalizedMessage,
        status: str = "queued",
    ) -> str:
        payload = self._build_run_input_payload(msg, message_id)
        return self.db.create_run_input(
            tenant_id=tenant_id,
            run_id=run_id,
            project_name=project_name,
            source=msg.provider,
            provider_message_id=msg.provider_message_id,
            payload=payload,
            status=status,
        )

    def _message_from_run_input(self, row: Any) -> NormalizedMessage | None:
        try:
            raw_value = row.get("payload_json") if hasattr(row, "get") else row["payload_json"]
        except (AttributeError, KeyError, TypeError):
            raw_value = None
        if isinstance(raw_value, str):
            try:
                payload = json.loads(raw_value)
            except json.JSONDecodeError:
                payload = {}
        elif isinstance(raw_value, dict):
            payload = raw_value
        else:
            payload = {}
        try:
            received_raw = payload.get("received_at")
            received_at = datetime.fromisoformat(received_raw) if received_raw else self._now()
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except Exception:
            received_at = self._now()
        images = []
        for img in payload.get("images") or []:
            try:
                images.append(
                    Attachment(
                        provider_file_id=str(img.get("provider_file_id")),
                        width=img.get("width"),
                        height=img.get("height"),
                    )
                )
            except Exception:
                continue
        return NormalizedMessage(
            provider=str(payload.get("provider") or "event"),
            provider_message_id=str(payload.get("provider_message_id") or ""),
            tenant_external_id=str(payload.get("tenant_external_id") or ""),
            received_at=received_at,
            text=payload.get("text"),
            images=images,
            raw=payload.get("raw") or {},
            project_name=payload.get("project_name"),
        )

    def _combine_run_inputs(self, rows: list[Any]) -> tuple[NormalizedMessage | None, list[int]]:
        messages: list[NormalizedMessage] = []
        message_ids: list[int] = []
        for row in rows:
            msg = self._message_from_run_input(row)
            if msg:
                messages.append(msg)
            try:
                raw_value = row.get("payload_json") if hasattr(row, "get") else row["payload_json"]
            except (AttributeError, KeyError, TypeError):
                raw_value = None
            payload = None
            if isinstance(raw_value, dict):
                payload = raw_value
            elif isinstance(raw_value, str):
                try:
                    payload = json.loads(raw_value)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, dict):
                raw_id = payload.get("message_id")
                try:
                    message_id = int(raw_id)
                except (TypeError, ValueError):
                    message_id = 0
                if message_id:
                    message_ids.append(message_id)
        if not messages:
            return None, message_ids

        combined_text = "\n".join(
            [m.text.strip() for m in messages if m.text and m.text.strip()]
        ).strip()
        combined_images = []
        for m in messages:
            combined_images.extend(m.images)

        latest = messages[-1]
        return (
            NormalizedMessage(
                provider=latest.provider,
                provider_message_id=latest.provider_message_id,
                tenant_external_id=latest.tenant_external_id,
                received_at=latest.received_at,
                text=combined_text or None,
                images=combined_images,
                raw=latest.raw,
                project_name=latest.project_name,
            ),
            message_ids,
        )

    def _inflight_updates_path(self, tasks_dir: Path) -> Path:
        return tasks_dir / "inflight_updates.jsonl"

    async def _resolve_workspace(self, tenant, project_name: str | None = None) -> Workspace:
        if getattr(tenant, "workspace_path", None):
            tenant_root = self.workspace_manager.infer_tenant_root(
                Path(tenant.workspace_path)
            )
            return self.workspace_manager.ensure_project_for_tenant_root(
                tenant_root, project_name=project_name
            )
        if self.workspace_allocator is not None:
            tenant_root = await self._allocate_workspace(tenant)
            self.db.update_tenant_workspace(tenant.id, str(tenant_root))
            tenant.workspace_path = str(tenant_root)
            return self.workspace_manager.ensure_project_for_tenant_root(
                tenant_root, project_name=project_name
            )
        workspace = self.workspace_manager.ensure_workspace(tenant.key, project_name=project_name)
        if tenant.workspace_path != str(workspace.tenant_root):
            self.db.update_tenant_workspace(tenant.id, str(workspace.tenant_root))
            tenant.workspace_path = str(workspace.tenant_root)
        return workspace

    async def _allocate_workspace(self, tenant) -> Path:
        allocator = self.workspace_allocator
        if allocator is None:
            raise RuntimeError("workspace allocator not configured")
        allocate = getattr(allocator, "allocate_workspace", None)
        if allocate is None:
            raise RuntimeError("workspace allocator missing allocate_workspace")
        result = allocate(tenant)
        if hasattr(result, "__await__"):
            result = await result
        return Path(result)

    def _ensure_inflight_stream(
        self, tenant_key: str, project_name: str | None
    ) -> InflightTextStream:
        if self.inflight_text_queues is None:
            self.inflight_text_queues = {}
        stream_key = self._stream_key(tenant_key, project_name)
        stream = self.inflight_text_queues.get(stream_key)
        if stream is None:
            import asyncio

            stream = InflightTextStream(queue=asyncio.Queue())
            self.inflight_text_queues[stream_key] = stream
        return stream

    def _get_inflight_stream(
        self, tenant_key: str, project_name: str | None
    ) -> InflightTextStream | None:
        if not self.inflight_text_queues:
            return None
        return self.inflight_text_queues.get(self._stream_key(tenant_key, project_name))

    def _clear_inflight_stream(self, tenant_key: str, project_name: str | None) -> None:
        if not self.inflight_text_queues:
            return
        self.inflight_text_queues.pop(self._stream_key(tenant_key, project_name), None)

    @staticmethod
    def _stream_key(tenant_key: str, project_name: str | None) -> str:
        return f"{tenant_key}:{project_name or 'main'}"

    async def _handle_blocked(
        self,
        tasks_dir: Path,
        tenant: Any,
        notify: bool = True,
        user_payload: str | None = None,
    ) -> bool:
        self._ingest_tool_failures(tasks_dir)
        block = get_block(tasks_dir, "system")
        if user_payload and block:
            if notify and self._should_notify_block(tasks_dir, block):
                payload = {
                    "event_type": "system_blocked",
                    "intent": "system_blocked",
                    "payload": {
                        "summary": "System blocked",
                        "reason": block.get("reason"),
                        "count": block.get("count"),
                    },
                }
                self.db.create_event_job(tenant.id, job_type="event", payload=payload)
                self._mark_block_notified(tasks_dir, block)
            self._clear_block_state(tasks_dir)
            return False
        if not block:
            return False
        if notify and self._should_notify_block(tasks_dir, block):
            payload = {
                "event_type": "system_blocked",
                "intent": "system_blocked",
                "payload": {
                    "summary": "System blocked",
                    "reason": block.get("reason"),
                    "count": block.get("count"),
                },
            }
            self.db.create_event_job(tenant.id, job_type="event", payload=payload)
            self._mark_block_notified(tasks_dir, block)
        return True

    def _clear_block_state(self, tasks_dir: Path) -> None:
        clear_block(tasks_dir, "system")
        db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
        db.set_kv("system", "block_notified", None)

    def _reconcile_inflight_run(self, tenant: Any, run: Any, workspace: Workspace) -> bool:
        result_path = workspace.tasks_dir / "run_result.json"
        if not result_path.exists():
            return False
        try:
            started_raw = run["started_at"]
        except (KeyError, TypeError):
            started_raw = None
        if started_raw:
            try:
                started_at = datetime.fromisoformat(started_raw)
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                result_time = datetime.fromtimestamp(
                    result_path.stat().st_mtime, tz=timezone.utc
                )
                if result_time < started_at:
                    return False
            except (ValueError, OSError):
                pass
        try:
            payload = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        error = payload.get("error")
        status = "failed" if error else "completed"
        run_id = run["id"] if hasattr(run, "keys") else run.get("id")
        total_cost = payload.get("total_cost_usd")
        usage = payload.get("usage")
        if total_cost is not None or usage is not None:
            self.db.update_run_usage(run_id, total_cost_usd=total_cost, usage=usage)
        self.db.finish_run(run_id, status=status, error=error)
        session_id = payload.get("session_id")
        if session_id:
            self.db.update_tenant_session(tenant.id, session_id)
        message_id = run["message_id"] if hasattr(run, "keys") else run.get("message_id")
        if message_id:
            self.db.update_message_status(
                int(message_id), "processed" if status == "completed" else "failed"
            )
        project_name = (
            run["project_name"]
            if hasattr(run, "keys") and "project_name" in run.keys()
            else None
        )
        self._clear_inflight_stream(tenant.key, project_name)
        return True

    def _supports_inflight_stream(self) -> bool:
        return bool(getattr(self.agent, "supports_inflight_stream", False))

    async def _github_runtime_env(
        self,
        settings: Settings,
        workspace: Workspace,
        tenant: Any,
    ) -> dict[str, str] | None:
        config = GitHubAppConfig.from_settings(settings)
        if not config or not config.enabled:
            return None
        manager = GitHubRepoManager(config)
        repo_name = self._default_repo_name(
            prefix=config.repo_prefix,
            tenant_id=getattr(tenant, "id", None),
            project_name=workspace.project_name,
        )
        try:
            repo = await manager.ensure_repo(
                project_root=workspace.root,
                repo_name=repo_name,
            )
            token = await manager.create_repo_token(repo)
        except Exception as exc:  # noqa: BLE001
            append_log(
                workspace.tasks_dir,
                "system",
                f"github_setup_failed: {type(exc).__name__}: {exc}",
            )
            return None
        env: dict[str, str] = {
            "GITHUB_TOKEN": token,
            "GITHUB_REPO_FULL_NAME": repo.full_name,
            "GITHUB_REPO_NAME": repo.name,
            "GITHUB_REPO_OWNER": config.org,
        }
        if repo.clone_url:
            env["GITHUB_REPO_HTTP_URL"] = repo.clone_url
            env["GITHUB_REPO_URL"] = repo.clone_url
        if repo.ssh_url:
            env["GITHUB_REPO_SSH_URL"] = repo.ssh_url
        if repo.default_branch:
            env["GITHUB_REPO_DEFAULT_BRANCH"] = repo.default_branch
        return env

    @staticmethod
    def _default_repo_name(
        *,
        prefix: str | None,
        tenant_id: Any,
        project_name: str | None,
    ) -> str:
        base_prefix = (prefix or "site").strip() or "site"
        tenant_part = str(tenant_id or "tenant")
        project_part = (project_name or "main").strip() or "main"
        return f"{base_prefix}-{tenant_part}-{project_part}"

    def _ingest_tool_failures(self, tasks_dir: Path) -> None:
        path = tasks_dir / "tool_runs.jsonl"
        if not path.exists():
            return
        try:
            db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
            cursor = db.get_kv("system", "tool_runs_cursor") or {}
        except Exception:
            return
        last_ts = cursor.get("timestamp")
        latest_ts = last_ts

        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("timestamp")
            if last_ts and ts and ts <= last_ts:
                continue
            if ts:
                latest_ts = ts
            result = entry.get("result")
            if isinstance(result, dict):
                status = str(result.get("status") or "").strip().lower()
                if status in {"missing_token", "missing_org", "missing_context", "blocked"}:
                    record_hard_failure(
                        tasks_dir,
                        "system",
                        reason=f"{entry.get('tool')}:{status}",
                        max_failures=2,
                    )
        if latest_ts and latest_ts != last_ts:
            try:
                db.set_kv("system", "tool_runs_cursor", {"timestamp": latest_ts})
            except Exception:
                return

    def _should_notify_block(self, tasks_dir: Path, block: dict[str, Any]) -> bool:
        try:
            db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
            cursor = db.get_kv("system", "block_notified") or {}
        except Exception:
            return True
        last_at = cursor.get("at")
        block_at = block.get("at")
        if not block_at:
            return True
        return last_at != block_at

    def _mark_block_notified(self, tasks_dir: Path, block: dict[str, Any]) -> None:
        try:
            db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
            db.set_kv("system", "block_notified", {"at": block.get("at")})
        except Exception:
            return

    @staticmethod
    def _read_optional_file(path: Path) -> str | None:
        if not path.exists():
            return None
        content = path.read_text().strip()
        return content or None

    @staticmethod
    def _clear_run_artifacts(tasks_dir: Path) -> None:
        for name in (
            "deploy_url.txt",
            "user_reply.txt",
            "result_summary.md",
            "summary_prompt.md",
            "memory_prompt.md",
            "interaction_request.json",
        ):
            path = tasks_dir / name
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _message_from_row(row: Any) -> NormalizedMessage | None:
        try:
            raw_value = row["raw_json"] if row and "raw_json" in row.keys() else None
        except (AttributeError, KeyError, TypeError):
            raw_value = None
        if isinstance(raw_value, dict):
            raw = raw_value
        elif raw_value:
            try:
                raw = json.loads(raw_value)
            except (TypeError, json.JSONDecodeError):
                raw = None
        else:
            raw = None
        provider = row["provider"] if "provider" in row.keys() else None
        if provider == "telegram" and raw:
            msg = TelegramUpdateParser.parse(raw)
            if msg and "project_name" in row.keys():
                msg.project_name = row["project_name"]
            return msg
        return None

    @staticmethod
    def _append_chat_log(
        tasks_dir: Path, role: str, text: str, timestamp: Any | None = None
    ) -> None:
        append_log(tasks_dir, f"{role}_message", text, timestamp=timestamp)

    async def _fetch_billing_status(
        self,
        *,
        tenant: Any,
        msg: NormalizedMessage,
        project_name: str | None,
        tasks_dir: Path,
    ) -> dict[str, Any] | None:
        settings = Settings()
        url = settings.billing_status_url
        if not url and settings.public_base_url:
            base = settings.public_base_url.rstrip("/")
            url = f"{base}/billing/status"
        if not url:
            return None
        purpose_payload = self._derive_billing_purpose(msg)
        payload = {
            "tenant_id": getattr(tenant, "id", None),
            "tenant_key": getattr(tenant, "key", None),
            "provider": getattr(msg, "provider", None),
            "tenant_external_id": getattr(msg, "tenant_external_id", None),
            "project_name": project_name,
            "provider_message_id": getattr(msg, "provider_message_id", None),
            "received_at": msg.received_at.isoformat() if msg.received_at else None,
        }
        if purpose_payload:
            payload.update(purpose_payload)
        headers = {}
        if settings.billing_status_token:
            headers["Authorization"] = f"Bearer {settings.billing_status_token}"
        try:
            async with httpx.AsyncClient(
                timeout=settings.billing_status_timeout_seconds
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                append_log(
                    tasks_dir,
                    "system",
                    f"billing_status_http_error: {response.status_code}",
                )
                return None
            data = response.json()
            if not isinstance(data, dict):
                append_log(tasks_dir, "system", "billing_status_invalid_response")
                return None
            return data
        except Exception as exc:  # noqa: BLE001
            append_log(
                tasks_dir,
                "system",
                f"billing_status_failed: {type(exc).__name__}: {exc}",
            )
            return None

    @staticmethod
    def _write_billing_status(tasks_dir: Path, payload: dict[str, Any]) -> None:
        try:
            path = tasks_dir / "billing_status.json"
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=True))
        except OSError:
            return

    @staticmethod
    def _derive_billing_purpose(msg: NormalizedMessage) -> dict[str, str] | None:
        text = (msg.text or "").strip()
        if not text:
            if msg.images:
                label = "attachment"
            else:
                return None
        else:
            label = text.splitlines()[0].strip()
        label = re.sub(r"\\s+", " ", label).strip()
        if not label:
            return None
        label = label[:160]
        lower = label.lower()
        if not re.search(
            r"\\b(link|checkout|invoice|subscribe|subscription|hire|card)\\b",
            lower,
        ):
            return None
        if "link" in lower:
            category = "link"
        elif "invoice" in lower:
            category = "invoice"
        elif "checkout" in lower:
            category = "checkout"
        elif "subscribe" in lower or "subscription" in lower:
            category = "subscription"
        elif "price" in lower or "pricing" in lower or "cost" in lower:
            category = "pricing"
        elif "hire" in lower:
            category = "hire"
        elif "card" in lower:
            category = "card"
        else:
            category = "payment"
        suffix = str(getattr(msg, "provider_message_id", "") or "").strip()
        purpose = f"{category}-{suffix}" if suffix else category
        return {
            "purpose": purpose[:40],
            "purpose_label": label,
        }

    @staticmethod
    def _maybe_prepare_compaction(
        tasks_dir: Path, max_entries: int = 30, keep_last: int = 10
    ) -> None:
        entries = read_logs(tasks_dir)
        if len(entries) <= max_entries:
            return
        summary_path = tasks_dir / "chat_summary.md"
        previous_summary = summary_path.read_text() if summary_path.exists() else ""
        to_summarize = entries[:-keep_last]
        prompt = build_summarization_prompt(previous_summary, to_summarize)
        summary_prompt_path = tasks_dir / "summary_prompt.md"
        summary_prompt_path.write_text(
            f"SYSTEM_PROMPT:\n{prompt.system_prompt}\n\nUSER_MESSAGE:\n"
            f"{prompt.messages[0]['content']}\n"
        )

    @staticmethod
    def _maybe_prepare_memory_update(
        tasks_dir: Path, memory_path: Path, max_entries: int = 40
    ) -> None:
        entries = read_logs(tasks_dir)
        if not entries:
            return
        recent = entries[-max_entries:]
        try:
            previous_memory = memory_path.read_text(encoding="utf-8")
        except OSError:
            previous_memory = ""
        prompt = build_memory_prompt(previous_memory, recent)
        memory_prompt_path = tasks_dir / "memory_prompt.md"
        memory_prompt_path.write_text(
            f"SYSTEM_PROMPT:\n{prompt.system_prompt}\n\nUSER_MESSAGE:\n"
            f"{prompt.messages[0]['content']}\n"
        )


@dataclass
class RunActivityMonitor:
    db: Database
    run_id: int
    tasks_dir: Path
    lease_seconds: int
    poll_interval: float = 2.5
    _running: bool = True

    async def run(self) -> None:
        last_mtime = self._latest_activity_mtime()
        last_heartbeat = datetime.now(tz=timezone.utc)
        heartbeat_interval = self._heartbeat_interval()
        while self._running:
            await asyncio.sleep(self.poll_interval)
            now = datetime.now(tz=timezone.utc)
            latest = self._latest_activity_mtime()
            activity_changed = latest > last_mtime
            if activity_changed:
                last_mtime = latest
            if activity_changed or (now - last_heartbeat).total_seconds() >= heartbeat_interval:
                lease_expires = (now + timedelta(seconds=self.lease_seconds)).isoformat()
                self.db.update_run_lease(
                    self.run_id,
                    lease_expires_at=lease_expires,
                    last_activity_at=now.isoformat() if activity_changed else None,
                    last_heartbeat_at=now.isoformat(),
                )
                last_heartbeat = now

    def stop(self) -> None:
        self._running = False

    def _latest_activity_mtime(self) -> float:
        paths = [
            self.tasks_dir / "tool_runs.jsonl",
            self.tasks_dir / "agent_events.jsonl",
            self.tasks_dir / "outbound_messages.jsonl",
            self.tasks_dir / "run_result.json",
        ]
        latest = 0.0
        for path in paths:
            try:
                if path.exists():
                    latest = max(latest, path.stat().st_mtime)
            except OSError:
                continue
        return latest

    def _heartbeat_interval(self) -> float:
        if self.lease_seconds <= 0:
            return 15.0
        return max(5.0, min(30.0, self.lease_seconds / 4))

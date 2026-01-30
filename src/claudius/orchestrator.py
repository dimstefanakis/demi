from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json

from claudius.db.core import Database
from claudius.models import NormalizedMessage, OrchestratorResult
from claudius.workspace.core import Workspace, WorkspaceManager
from claudius.messaging.telegram import TelegramUpdateParser
from claudius.agent.inflight import InflightTextStream
from claudius.memory import build_summarization_prompt, read_logs
from claudius.memory.logs import append_log, write_chat_history

from claudius.failure_guard import clear_block, get_block, record_hard_failure
from claudius.tenant_db import ensure_tenant_db


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

        user_payload = (msg.text or "").strip()
        if not user_payload and msg.images:
            user_payload = "(attachment)"
        workspace = await self._resolve_workspace(tenant)
        if await self._handle_blocked(workspace.tasks_dir, tenant, user_payload=user_payload):
            self.db.update_message_status(message_id, "processed")
            return OrchestratorResult(status="blocked", detail="system_blocked")
        self._append_chat_log(workspace.tasks_dir, "user", user_payload, msg.received_at)
        write_chat_history(workspace.tasks_dir)

        inflight_run = self.db.get_inflight_run(tenant.id)
        if inflight_run:
            if self._reconcile_inflight_run(tenant, inflight_run, workspace):
                inflight_run = None
        if inflight_run and self._is_run_stale(
            inflight_run,
            max_age_seconds=900,
        ):
            await self._finalize_stale_run(tenant, inflight_run)
            inflight_run = None

        if inflight_run:
            supports_stream = self._supports_inflight_stream()
            stream = self._get_inflight_stream(tenant.key)
            if supports_stream and stream is not None and stream.accepting and msg.text and not msg.images:
                await stream.queue.put(msg.text)
                self.db.update_message_status(message_id, "processed")
                return OrchestratorResult(status="busy", detail="streamed to in-flight run")

            self.db.update_message_status(message_id, "pending")
            asset_paths = []
            if msg.images and hasattr(self.messenger, "download_images"):
                asset_paths = await self.messenger.download_images(msg.images, workspace.assets_dir)
            self._append_inflight_update(workspace.tasks_dir, msg, asset_paths)
            return OrchestratorResult(status="busy", detail="tenant already running")

        return await self._run_message(
            tenant=tenant,
            msg=msg,
            message_id=message_id,
            process_pending=True,
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

        workspace = await self._resolve_workspace(tenant)
        event_intent = str(payload.get("intent") or "").strip()
        event_type = str(payload.get("event_type") or "").strip()
        if event_intent == "system_blocked" or event_type == "system_blocked":
            if not get_block(workspace.tasks_dir, "system"):
                return OrchestratorResult(status="accepted", detail="block_cleared")
        else:
            if await self._handle_blocked(workspace.tasks_dir, tenant, notify=False):
                return OrchestratorResult(status="blocked", detail="system_blocked")
        inflight_run = self.db.get_inflight_run(tenant.id)
        if inflight_run:
            if self._reconcile_inflight_run(tenant, inflight_run, workspace):
                inflight_run = None
        if inflight_run and not self._is_run_stale(inflight_run, max_age_seconds=900):
            return OrchestratorResult(status="busy", detail="tenant already running")
        if inflight_run and self._is_run_stale(inflight_run, max_age_seconds=900):
            await self._finalize_stale_run(tenant, inflight_run)

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
        )
        message_id, inserted = self.db.record_message(tenant.id, msg)
        if not inserted:
            return OrchestratorResult(status="duplicate", detail="event already processed")

        return await self._run_message(
            tenant=tenant,
            msg=msg,
            message_id=message_id,
            process_pending=True,
        )

    async def _run_message(
        self,
        tenant,
        msg: NormalizedMessage,
        message_id: int,
        process_pending: bool,
    ) -> OrchestratorResult:
        workspace = await self._resolve_workspace(tenant)

        self.db.update_message_status(message_id, "processing")

        asset_paths = []
        if msg.images and hasattr(self.messenger, "download_images"):
            asset_paths = await self.messenger.download_images(msg.images, workspace.assets_dir)

        task_content = self._build_task_content(msg, asset_paths)
        task_path = workspace.write_task(task_content)

        self._clear_run_artifacts(workspace.tasks_dir)
        self._maybe_prepare_compaction(workspace.tasks_dir)

        run_id = self.db.create_run(tenant.id, message_id=message_id)
        inflight_stream = self._ensure_inflight_stream(tenant.key)
        try:
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
            )
            if agent_result.session_id:
                self.db.update_tenant_session(tenant.id, agent_result.session_id)
            self.db.update_run_usage(
                run_id,
                total_cost_usd=getattr(agent_result, "total_cost_usd", None),
                usage=getattr(agent_result, "usage", None),
            )
            self.db.finish_run(run_id, status="completed")
            self.db.update_message_status(message_id, "processed")
            self._clear_inflight_stream(tenant.key)
            if process_pending:
                await self._drain_pending_messages(tenant)
            return OrchestratorResult(status="accepted")
        except Exception as exc:  # noqa: BLE001
            self.db.finish_run(run_id, status="failed", error=str(exc))
            self.db.update_message_status(message_id, "failed")
            self._clear_inflight_stream(tenant.key)
            raise

    async def _drain_pending_messages(self, tenant) -> None:
        rows = self.db.get_pending_messages(tenant.id)
        if not rows:
            return

        message_ids = [int(row["id"]) for row in rows]
        combined = self._combine_pending_messages(rows)
        if not combined:
            self.db.update_message_statuses(message_ids, "failed")
            return

        self.db.update_message_statuses(message_ids, "processing")
        workspace = await self._resolve_workspace(tenant)
        inflight_path = self._inflight_updates_path(workspace.tasks_dir)
        if inflight_path.exists():
            try:
                inflight_path.unlink()
            except OSError:
                pass

        try:
            await self._run_message(
                tenant=tenant,
                msg=combined,
                message_id=message_ids[0],
                process_pending=False,
            )
        except Exception:  # noqa: BLE001
            self.db.update_message_statuses(message_ids, "failed")
            raise
        else:
            self.db.update_message_statuses(message_ids, "processed")

    @staticmethod
    def _is_run_stale(
        row: Any,
        max_age_seconds: int = 1800,
    ) -> bool:
        try:
            started_at = row["started_at"]
        except (KeyError, TypeError):
            return False
        if not started_at:
            return False
        from datetime import datetime, timezone

        try:
            started_dt = datetime.fromisoformat(started_at)
        except ValueError:
            return False
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - started_dt).total_seconds()
        if age > max_age_seconds:
            return True
        return False

    async def _finalize_stale_run(self, tenant, row: Any) -> None:
        self.db.finish_run(row["id"], status="failed", error="stale_run_timeout")
        message_id = row.get("message_id") if hasattr(row, "get") else row["message_id"]
        if message_id:
            self.db.update_message_status(int(message_id), "processed")
        self._clear_inflight_stream(tenant.key)

    @staticmethod
    def _build_task_content(msg: NormalizedMessage, asset_paths: list[str] | None = None) -> str:
        message_text = (msg.text or "").strip()
        if not message_text and msg.images:
            message_text = "(attachment only)"
        lines = ["# Task", "", f"Message: {message_text}".strip()]
        if msg.images:
            lines.append("\n## Images")
            for image in msg.images:
                lines.append(f"- {image.provider_file_id} ({image.width}x{image.height})")
        if asset_paths:
            lines.append("\n## Saved Assets")
            for path in asset_paths:
                lines.append(f"- {path}")
        return "\n".join(lines) + "\n"

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
        intent_line = f"- Intent: {intent.strip()}\n" if isinstance(intent, str) and intent.strip() else ""
        notify_line = "- This event requests a user notification.\n" if notify else ""
        return (
            f"{header}\n\n"
            "Context:\n"
            f"{intent_line}"
            f"{notify_line}"
            "- The full event payload was stored in the tenant SQLite DB at /workspace/tenant.sqlite\n"
            "- Table: events (columns: event_type, payload_json, received_at)\n"
            "- You may query or update this DB if needed.\n"
        )

    @staticmethod
    def _now():
        from datetime import datetime, timezone

        return datetime.now(tz=timezone.utc)

    @staticmethod
    def _append_inflight_update(
        tasks_dir: Path, msg: NormalizedMessage, asset_paths: list[str]
    ) -> None:
        updates_path = tasks_dir / "inflight_updates.jsonl"
        payload = {
            "timestamp": msg.received_at.isoformat(),
            "text": msg.text or "",
            "assets": asset_paths or [],
            "provider_message_id": msg.provider_message_id,
        }
        updates_path.parent.mkdir(parents=True, exist_ok=True)
        with updates_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

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
        )

    def _inflight_updates_path(self, tasks_dir: Path) -> Path:
        return tasks_dir / "inflight_updates.jsonl"

    async def _resolve_workspace(self, tenant) -> Workspace:
        if getattr(tenant, "workspace_path", None):
            root = Path(tenant.workspace_path)
            return self.workspace_manager.ensure_workspace_at_path(root)
        if self.workspace_allocator is not None:
            root = await self._allocate_workspace(tenant)
            self.db.update_tenant_workspace(tenant.id, str(root))
            tenant.workspace_path = str(root)
            return self.workspace_manager.ensure_workspace_at_path(root)
        workspace = self.workspace_manager.ensure_workspace(tenant.key)
        if tenant.workspace_path != str(workspace.root):
            self.db.update_tenant_workspace(tenant.id, str(workspace.root))
            tenant.workspace_path = str(workspace.root)
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

    def _ensure_inflight_stream(self, tenant_key: str) -> InflightTextStream:
        if self.inflight_text_queues is None:
            self.inflight_text_queues = {}
        stream = self.inflight_text_queues.get(tenant_key)
        if stream is None:
            import asyncio

            stream = InflightTextStream(queue=asyncio.Queue())
            self.inflight_text_queues[tenant_key] = stream
        return stream

    def _get_inflight_stream(self, tenant_key: str) -> InflightTextStream | None:
        if not self.inflight_text_queues:
            return None
        return self.inflight_text_queues.get(tenant_key)

    def _clear_inflight_stream(self, tenant_key: str) -> None:
        if not self.inflight_text_queues:
            return
        self.inflight_text_queues.pop(tenant_key, None)

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
        run_id = run.get("id") if hasattr(run, "get") else run["id"]
        total_cost = payload.get("total_cost_usd")
        usage = payload.get("usage")
        if total_cost is not None or usage is not None:
            self.db.update_run_usage(run_id, total_cost_usd=total_cost, usage=usage)
        self.db.finish_run(run_id, status=status, error=error)
        session_id = payload.get("session_id")
        if session_id:
            self.db.update_tenant_session(tenant.id, session_id)
        message_id = run.get("message_id") if hasattr(run, "get") else run["message_id"]
        if message_id:
            self.db.update_message_status(
                int(message_id), "processed" if status == "completed" else "failed"
            )
        self._clear_inflight_stream(tenant.key)
        return True

    def _supports_inflight_stream(self) -> bool:
        return bool(getattr(self.agent, "supports_inflight_stream", False))

    def _ingest_tool_failures(self, tasks_dir: Path) -> None:
        path = tasks_dir / "tool_runs.jsonl"
        if not path.exists():
            return
        db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
        cursor = db.get_kv("system", "tool_runs_cursor") or {}
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
            db.set_kv("system", "tool_runs_cursor", {"timestamp": latest_ts})

    def _should_notify_block(self, tasks_dir: Path, block: dict[str, Any]) -> bool:
        db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
        cursor = db.get_kv("system", "block_notified") or {}
        last_at = cursor.get("at")
        block_at = block.get("at")
        if not block_at:
            return True
        return last_at != block_at

    def _mark_block_notified(self, tasks_dir: Path, block: dict[str, Any]) -> None:
        db = ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")
        db.set_kv("system", "block_notified", {"at": block.get("at")})

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
            raw = json.loads(row["raw_json"]) if row["raw_json"] else None
        except (TypeError, json.JSONDecodeError, KeyError):
            raw = None
        provider = row["provider"] if "provider" in row.keys() else None
        if provider == "telegram" and raw:
            return TelegramUpdateParser.parse(raw)
        return None
    @staticmethod
    def _append_chat_log(tasks_dir: Path, role: str, text: str, timestamp: Any | None = None) -> None:
        append_log(tasks_dir, f"{role}_message", text, timestamp=timestamp)

    @staticmethod
    def _maybe_prepare_compaction(tasks_dir: Path, max_entries: int = 30, keep_last: int = 10) -> None:
        entries = read_logs(tasks_dir)
        if len(entries) <= max_entries:
            return
        summary_path = tasks_dir / "chat_summary.md"
        previous_summary = summary_path.read_text() if summary_path.exists() else ""
        to_summarize = entries[:-keep_last]
        prompt = build_summarization_prompt(previous_summary, to_summarize)
        summary_prompt_path = tasks_dir / "summary_prompt.md"
        summary_prompt_path.write_text(
            f"SYSTEM_PROMPT:\n{prompt.system_prompt}\n\nUSER_MESSAGE:\n{prompt.messages[0]['content']}\n"
        )

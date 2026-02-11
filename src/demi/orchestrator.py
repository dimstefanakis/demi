from __future__ import annotations

from dataclasses import dataclass, replace, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import json
import re
import asyncio
import httpx
import uuid
import inspect

from demi.db.core import Database
from demi.models import Attachment, NormalizedMessage, OrchestratorResult
from demi.workspace.core import Workspace, WorkspaceManager
from demi.messaging.telegram import TelegramUpdateParser
from demi.agent.inflight import InflightTextStream
from demi.memory.logs import append_log, write_chat_history

from demi.failure_guard import clear_block, get_block, record_hard_failure
from demi.domains.github_app import GitHubAppConfig, GitHubRepoManager, MAX_REPO_NAME_LENGTH
from demi.config import Settings

INTERACTION_SESSION_NAMESPACE = "interaction"
# Keep routing and instruction sessions separate so prior instruction-mode
# messages cannot leak into routing decisions for new user input.
INTERACTION_ROUTE_SESSION_KEY = "claude_route_session"
INTERACTION_INSTRUCTION_SESSION_KEY = "claude_instruction_session"

EXECUTION_SESSION_NAMESPACE = "execution"
EXECUTION_SESSION_KEY_PREFIX = "claude_session:"


@dataclass
class InteractionRouteSession:
    tenant_id: int
    tenant_key: str
    project_name: str | None
    workspace: Workspace
    stream: InflightTextStream = field(default_factory=lambda: InflightTextStream(queue=asyncio.Queue()))
    message_ids: set[int] = field(default_factory=set)
    messages: dict[int, NormalizedMessage] = field(default_factory=dict)
    db_session_id: int | None = None


@dataclass
class Orchestrator:
    MAX_RETRY_FAILURE_NOTICES = 2

    db: Database
    workspace_manager: WorkspaceManager
    agent: Any
    messenger: Any
    payments: Any | None = None
    inflight_text_queues: dict[str, InflightTextStream] | None = None
    workspace_allocator: Any | None = None
    interaction_locks: dict[int, asyncio.Lock] | None = None
    interaction_route_sessions: dict[int, InteractionRouteSession] | None = None

    def _interaction_lock_for(self, tenant_id: int) -> asyncio.Lock:
        if self.interaction_locks is None:
            self.interaction_locks = {}
        lock = self.interaction_locks.get(tenant_id)
        if lock is None:
            lock = asyncio.Lock()
            self.interaction_locks[tenant_id] = lock
        return lock

    def _interaction_session_for(self, tenant_id: int) -> InteractionRouteSession | None:
        if self.interaction_route_sessions is None:
            return None
        return self.interaction_route_sessions.get(tenant_id)

    def _register_interaction_session(self, session: InteractionRouteSession) -> None:
        if self.interaction_route_sessions is None:
            self.interaction_route_sessions = {}
        self.interaction_route_sessions[session.tenant_id] = session

    @staticmethod
    def _interaction_session_key(mode: str) -> str:
        if mode == "instruction":
            return INTERACTION_INSTRUCTION_SESSION_KEY
        return INTERACTION_ROUTE_SESSION_KEY

    def _get_interaction_session_id(self, tenant_id: int, *, mode: str = "route") -> str | None:
        try:
            payload = self.db.get_tenant_kv(
                int(tenant_id),
                INTERACTION_SESSION_NAMESPACE,
                self._interaction_session_key(mode),
            ) or {}
        except Exception:
            return None
        value = str(payload.get("session_id") or "").strip()
        return value or None

    def _set_interaction_session_id(
        self,
        tenant_id: int,
        session_id: str | None,
        *,
        mode: str = "route",
    ) -> None:
        # Interaction and execution sessions must be tracked independently so a stale
        # execution resume token cannot contaminate user-facing interaction routing.
        cleaned = str(session_id or "").strip()
        payload = {"session_id": cleaned} if cleaned else None
        try:
            self.db.set_tenant_kv(
                int(tenant_id),
                INTERACTION_SESSION_NAMESPACE,
                self._interaction_session_key(mode),
                payload,
            )
        except Exception:
            pass

    def _close_interaction_session(self, tenant_id: int, *, status: str = "completed") -> None:
        if self.interaction_route_sessions is None:
            return
        session = self.interaction_route_sessions.pop(tenant_id, None)
        if session is None:
            return
        session.stream.accepting = False
        if session.db_session_id is not None:
            try:
                self.db.finish_interaction_session(session.db_session_id, status=status)
            except Exception:
                pass

    def _get_execution_session_id(
        self, tenant_id: int, project_name: str | None
    ) -> str | None:
        key = f"{EXECUTION_SESSION_KEY_PREFIX}{project_name or 'main'}"
        try:
            payload = self.db.get_tenant_kv(
                int(tenant_id),
                EXECUTION_SESSION_NAMESPACE,
                key,
            ) or {}
        except Exception:
            return None
        value = str(payload.get("session_id") or "").strip()
        return value or None

    def _set_execution_session_id(
        self,
        tenant_id: int,
        project_name: str | None,
        session_id: str | None,
    ) -> None:
        key = f"{EXECUTION_SESSION_KEY_PREFIX}{project_name or 'main'}"
        cleaned = str(session_id or "").strip()
        payload = {"session_id": cleaned} if cleaned else None
        try:
            self.db.set_tenant_kv(
                int(tenant_id),
                EXECUTION_SESSION_NAMESPACE,
                key,
                payload,
            )
        except Exception:
            pass

    def _open_interaction_session(
        self,
        *,
        tenant: Any,
        workspace: Workspace,
        project_name: str | None,
        messages: list[tuple[int, NormalizedMessage]],
    ) -> InteractionRouteSession:
        session = InteractionRouteSession(
            tenant_id=int(tenant.id),
            tenant_key=str(tenant.key),
            project_name=project_name,
            workspace=workspace,
        )
        for mid, message in messages:
            session.message_ids.add(int(mid))
            session.messages[int(mid)] = message
        try:
            session.db_session_id = self.db.create_interaction_session(
                tenant_id=int(tenant.id),
                project_name=project_name,
                status="running",
            )
        except Exception:
            session.db_session_id = None
        self._register_interaction_session(session)
        return session

    async def _stream_into_interaction_session(
        self,
        *,
        session: InteractionRouteSession,
        message_id: int,
        msg: NormalizedMessage,
    ) -> bool:
        if not session.stream.accepting:
            return False
        text = (msg.text or "").strip()
        asset_paths = await self._download_message_assets(msg, session.workspace)
        if not text and (asset_paths or msg.images):
            text = "(attachment)"
        if asset_paths:
            items = "\n".join(f"- {path}" for path in asset_paths if path)
            if items:
                if text:
                    text = f"{text}\n\nAttachments:\n{items}"
                else:
                    text = f"Attachments:\n{items}"
        if not text:
            return False
        if not session.stream.accepting:
            return False
        session.message_ids.add(int(message_id))
        session.messages[int(message_id)] = msg
        if session.db_session_id is not None:
            try:
                self.db.record_interaction_session_input(
                    session_id=session.db_session_id,
                    message_id=message_id,
                    provider_message_id=str(msg.provider_message_id or "") or None,
                    text=text,
                    assets=asset_paths,
                    status="streamed",
                )
            except Exception:
                pass
        try:
            session.stream.queue.put_nowait(text)
            return True
        except Exception:
            return False

    def _merge_interaction_session_messages(
        self,
        *,
        batched_messages: list[tuple[int, NormalizedMessage]],
        session: InteractionRouteSession | None,
    ) -> list[tuple[int, NormalizedMessage]]:
        if session is None:
            return batched_messages
        merged: dict[int, NormalizedMessage] = {}
        for mid, message in batched_messages:
            merged[int(mid)] = message
        for mid, message in session.messages.items():
            merged[int(mid)] = message
        ordered = list(merged.items())
        ordered.sort(key=lambda item: (item[1].received_at, item[0]))
        return ordered

    def _rollback_interaction_processing_messages(self, session: InteractionRouteSession) -> None:
        for message_id in sorted(session.message_ids):
            row = None
            try:
                row = self.db.get_message(int(message_id))
            except Exception:
                row = None
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status != "processing":
                continue
            try:
                self.db.update_message_status(int(message_id), "received")
            except Exception:
                continue

    def _message_from_message_row(
        self, tenant: Any, row: Any
    ) -> NormalizedMessage | None:
        if row is None:
            return None
        raw_payload = None
        try:
            raw_payload = row.get("raw_json") if hasattr(row, "get") else row["raw_json"]
        except Exception:
            raw_payload = None
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                raw_payload = {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}

        provider = None
        provider_message_id = None
        text = None
        received_raw = None
        project_name = None
        try:
            provider = row.get("provider") if hasattr(row, "get") else row["provider"]
        except Exception:
            provider = None
        try:
            provider_message_id = (
                row.get("provider_message_id")
                if hasattr(row, "get")
                else row["provider_message_id"]
            )
        except Exception:
            provider_message_id = None
        try:
            text = row.get("text") if hasattr(row, "get") else row["text"]
        except Exception:
            text = None
        try:
            received_raw = row.get("received_at") if hasattr(row, "get") else row["received_at"]
        except Exception:
            received_raw = None
        try:
            project_name = (
                row.get("project_name") if hasattr(row, "get") else row["project_name"]
            )
        except Exception:
            project_name = None

        received_at = None
        if received_raw:
            try:
                received_at = datetime.fromisoformat(str(received_raw))
            except (TypeError, ValueError):
                received_at = None
        if received_at is None:
            received_at = self._now()
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)

        parsed = None
        if str(provider or "") == "telegram":
            try:
                parsed = TelegramUpdateParser.parse(raw_payload)
            except Exception:
                parsed = None
        if parsed is not None:
            candidate = parsed
            if provider_message_id and str(candidate.provider_message_id) != str(provider_message_id):
                candidate = replace(candidate, provider_message_id=str(provider_message_id))
            if text is not None and candidate.text != text:
                candidate = replace(candidate, text=text)
            if candidate.received_at != received_at:
                candidate = replace(candidate, received_at=received_at)
            if project_name and candidate.project_name != project_name:
                candidate = replace(candidate, project_name=project_name)
            return candidate

        tenant_external_id = str(getattr(tenant, "external_id", "") or "")
        return NormalizedMessage(
            provider=str(provider or "event"),
            provider_message_id=str(provider_message_id or ""),
            tenant_external_id=tenant_external_id,
            received_at=received_at,
            text=text,
            images=[],
            raw=raw_payload,
            project_name=str(project_name) if project_name else None,
        )

    @staticmethod
    def _combine_received_messages(
        messages: list[tuple[int, NormalizedMessage]]
    ) -> NormalizedMessage:
        if not messages:
            raise ValueError("messages required")
        ordered = [m for _mid, m in messages]
        combined_lines: list[str] = []
        for m in ordered:
            text = (m.text or "").strip()
            if not text and m.images:
                text = "(attachment)"
            if text:
                combined_lines.append(text)
        combined_text = "\n".join(combined_lines).strip() or None

        seen_file_ids: set[str] = set()
        combined_images: list[Attachment] = []
        for m in ordered:
            for img in m.images:
                file_id = str(img.provider_file_id or "").strip()
                if not file_id or file_id in seen_file_ids:
                    continue
                seen_file_ids.add(file_id)
                combined_images.append(img)

        latest = ordered[-1]
        raw_value: dict[str, Any] = {}
        if isinstance(latest.raw, dict):
            raw_value = dict(latest.raw)
        raw_value.setdefault("_batched_message_ids", [mid for mid, _m in messages])
        return NormalizedMessage(
            provider=latest.provider,
            provider_message_id=latest.provider_message_id,
            tenant_external_id=latest.tenant_external_id,
            received_at=latest.received_at,
            text=combined_text,
            images=combined_images,
            raw=raw_value,
            project_name=latest.project_name,
        )

    async def handle_message(
        self,
        msg: NormalizedMessage,
        *,
        allow_existing_received: bool = False,
        allow_interaction_stream: bool = True,
    ) -> OrchestratorResult:
        tenant = self.db.get_or_create_tenant(msg.provider, msg.tenant_external_id)

        message_id, inserted = self.db.record_message(tenant.id, msg)
        if not inserted:
            if not allow_existing_received:
                return OrchestratorResult(status="duplicate", detail="message already processed")
            existing_row = None
            try:
                existing_row = self.db.get_message(int(message_id))
            except Exception:
                existing_row = None
            existing_status = ""
            if existing_row is not None:
                try:
                    existing_status = str(
                        existing_row.get("status")
                        if hasattr(existing_row, "get")
                        else existing_row["status"]
                        or ""
                    ).strip().lower()
                except Exception:
                    existing_status = ""
            if existing_status != "received":
                return OrchestratorResult(status="duplicate", detail="message already handled")
            recovered = self._message_from_message_row(tenant, existing_row)
            if recovered is not None:
                msg = recovered
        existing_session = self._interaction_session_for(tenant.id)
        if allow_interaction_stream and existing_session is not None and existing_session.stream.accepting:
            streamed = await self._stream_into_interaction_session(
                session=existing_session,
                message_id=int(message_id),
                msg=msg,
            )
            if streamed:
                try:
                    self.db.update_message_status(int(message_id), "processing")
                except Exception:
                    pass
                return OrchestratorResult(status="accepted", detail="interaction_streamed")

        run_message_args: dict[str, Any] | None = None
        lock = self._interaction_lock_for(tenant.id)
        async with lock:
            row = None
            try:
                row = self.db.get_message(message_id)
            except Exception:
                row = None
            if row is not None:
                status = str(row.get("status") or "")
                if status and status != "received":
                    return OrchestratorResult(status="duplicate", detail="message already handled")
            existing_session = self._interaction_session_for(tenant.id)
            if (
                allow_interaction_stream
                and existing_session is not None
                and existing_session.stream.accepting
            ):
                streamed = await self._stream_into_interaction_session(
                    session=existing_session,
                    message_id=int(message_id),
                    msg=msg,
                )
                if streamed:
                    try:
                        self.db.update_message_status(int(message_id), "processing")
                    except Exception:
                        pass
                    return OrchestratorResult(status="accepted", detail="interaction_streamed")

            settings = Settings()
            batched_messages: list[tuple[int, NormalizedMessage]] = [(int(message_id), msg)]
            message_ids = [mid for mid, _ in batched_messages]
            latest_message_id = message_ids[-1]
            combined_msg = self._combine_received_messages(batched_messages)

            # Resolve project from latest message + directive extraction.
            combined_msg, project_name = self._resolve_project_for_tenant(tenant, combined_msg)
            if project_name:
                for mid in message_ids:
                    try:
                        self.db.update_message_project(mid, project_name)
                    except Exception:
                        continue

            user_payloads: list[tuple[str, datetime]] = []
            for _mid, m in batched_messages:
                text = (m.text or "").strip()
                if not text and m.images:
                    text = "(attachment)"
                if text:
                    user_payloads.append((text, m.received_at))
            combined_user_payload = (combined_msg.text or "").strip()
            if not combined_user_payload and combined_msg.images:
                combined_user_payload = "(attachment)"

            workspace = await self._resolve_workspace(tenant, project_name=project_name)

            # Reset should apply for the current interaction turn.
            if any(self._is_reset_command((m.text or "").strip()) for _mid, m in batched_messages):
                for payload, received_at in user_payloads:
                    self._append_chat_log(workspace.tasks_dir, "user", payload, received_at)
                write_chat_history(workspace.tasks_dir)
                self._reset_state(workspace, tenant, project_name=project_name)
                self.db.update_message_statuses(message_ids, "processed")
                await self._send_interaction_instruction(
                    workspace=workspace,
                    tenant=tenant,
                    msg=combined_msg,
                    instruction=(
                        "Let the user know the reset is complete and they can resend their request."
                    ),
                    run_id=None,
                    message_id=latest_message_id,
                )
                return OrchestratorResult(status="accepted", detail="reset")

            testing_mode = self._extract_testing_mode_command(
                [str((m.text or "").strip()) for _mid, m in batched_messages]
            )
            if testing_mode is not None:
                now_iso = self._now().isoformat()
                self.db.set_tenant_kv(
                    int(tenant.id),
                    "system",
                    "testing_mode",
                    {"enabled": testing_mode, "updated_at": now_iso},
                )
                if testing_mode:
                    self._write_billing_status(
                        workspace.tasks_dir,
                        {
                            "status": "testing_mode",
                            "payment_required": False,
                            "allow_first_build": True,
                            "plan": "testing",
                            "testing_mode": True,
                            "message": "testing mode active",
                        },
                    )
                self.db.update_message_statuses(message_ids, "processed")
                await self._send_interaction_instruction(
                    workspace=workspace,
                    tenant=tenant,
                    msg=combined_msg,
                    instruction=(
                        "Confirm testing mode is now "
                        + ("enabled." if testing_mode else "disabled.")
                    ),
                    run_id=None,
                    message_id=latest_message_id,
                )
                return OrchestratorResult(status="accepted", detail="testing_mode_updated")

            if await self._handle_blocked(
                workspace.tasks_dir, tenant, user_payload=combined_user_payload
            ):
                for payload, received_at in user_payloads:
                    self._append_chat_log(workspace.tasks_dir, "user", payload, received_at)
                write_chat_history(workspace.tasks_dir)
                self.db.update_message_statuses(message_ids, "processed")
                return OrchestratorResult(status="blocked", detail="system_blocked")

            inflight_run = self.db.get_inflight_run(tenant.id, project_name)
            if inflight_run:
                if self._reconcile_inflight_run(tenant, inflight_run, workspace):
                    inflight_run = None
            # Reconciliation must run before stale-expiration so completed runs
            # with persisted run_result.json are not mislabeled as lease_expired.
            self.db.expire_stale_runs(tenant.id, project_name, self._now())
            inflight_run = self.db.get_inflight_run(tenant.id, project_name)
            if inflight_run and self._is_run_stale(inflight_run, max_age_seconds=900):
                await self._finalize_stale_run(tenant, inflight_run, notify=False)
                inflight_run = None

            active_run = self.db.get_active_run(tenant.id, project_name)
            if active_run:
                if not inflight_run or int(active_run["run_id"]) != int(inflight_run["id"]):
                    self.db.clear_active_run(tenant.id, project_name)
                    active_run = None

            # Download attachments for this interaction turn.
            asset_paths = await self._download_message_assets(combined_msg, workspace)
            if not project_name and workspace.project_name:
                project_name = workspace.project_name
                combined_msg.project_name = project_name
                for mid in message_ids:
                    try:
                        self.db.update_message_project(mid, project_name)
                    except Exception:
                        continue

            billing_status: dict[str, Any] | None = None
            billing_checked_at: str | None = None
            self._write_interaction_context(
                workspace=workspace,
                tenant=tenant,
                msg=combined_msg,
                message_id=latest_message_id,
                project_name=project_name,
                active_run=active_run,
                inflight_run=inflight_run,
                billing_status=None,
                billing_checked_at=None,
                asset_paths=asset_paths,
            )
            interaction_session = self._open_interaction_session(
                tenant=tenant,
                workspace=workspace,
                project_name=project_name,
                messages=batched_messages,
            )
            interaction_session_status = "failed"
            try:
                decision_result = await self._run_interaction_agent(
                    workspace=workspace,
                    tenant=tenant,
                    msg=combined_msg,
                    message_id=latest_message_id,
                    asset_paths=asset_paths,
                    inflight_stream=interaction_session.stream,
                )
                if not self._interaction_decision_valid(decision_result):
                    append_log(workspace.tasks_dir, "system", "interaction_agent_invalid_decision")
                    try:
                        self.db.update_message_statuses(message_ids, "failed")
                    except Exception:
                        pass
                    raise RuntimeError("interaction_agent_invalid_decision")
                decision, interaction_usage = self._normalize_interaction_decision(
                    decision_result, project_name=project_name, billing_checked=False
                )

                if decision.get("billing_check") and not decision.get("billing_checked"):
                    billing_status = await self._fetch_billing_status(
                        tenant=tenant,
                        msg=combined_msg,
                        project_name=project_name,
                        tasks_dir=workspace.tasks_dir,
                    )
                    if billing_status is not None:
                        if not active_run and not inflight_run:
                            self._write_billing_status(workspace.tasks_dir, billing_status)
                    billing_checked_at = self._now().isoformat()
                    self._write_interaction_context(
                        workspace=workspace,
                        tenant=tenant,
                        msg=combined_msg,
                        message_id=latest_message_id,
                        project_name=project_name,
                        active_run=active_run,
                        inflight_run=inflight_run,
                        billing_status=billing_status,
                        billing_checked_at=billing_checked_at,
                        asset_paths=asset_paths,
                    )
                    decision_result = await self._run_interaction_agent(
                        workspace=workspace,
                        tenant=tenant,
                        msg=combined_msg,
                        message_id=latest_message_id,
                        billing_checked=True,
                        asset_paths=asset_paths,
                        inflight_stream=interaction_session.stream,
                    )
                    if not self._interaction_decision_valid(decision_result):
                        append_log(
                            workspace.tasks_dir, "system", "interaction_agent_invalid_decision"
                        )
                        try:
                            self.db.update_message_statuses(message_ids, "failed")
                        except Exception:
                            pass
                        raise RuntimeError("interaction_agent_invalid_decision")
                    decision, interaction_usage = self._normalize_interaction_decision(
                        decision_result, project_name=project_name, billing_checked=True
                    )

                decision_project = decision.get("project_name") or project_name or workspace.project_name
                if decision_project:
                    decision_project = self.workspace_manager.normalize_project_name(decision_project)
                    if project_name != decision_project:
                        for mid in message_ids:
                            try:
                                self.db.update_message_project(mid, decision_project)
                            except Exception:
                                continue
                    project_name = decision_project
                    combined_msg.project_name = decision_project
                if project_name and project_name != workspace.project_name:
                    workspace = await self._resolve_workspace(tenant, project_name=project_name)
                    inflight_run = self.db.get_inflight_run(tenant.id, project_name)
                    active_run = self.db.get_active_run(tenant.id, project_name)
                    self._write_interaction_context(
                        workspace=workspace,
                        tenant=tenant,
                        msg=combined_msg,
                        message_id=latest_message_id,
                        project_name=project_name,
                        active_run=active_run,
                        inflight_run=inflight_run,
                        billing_status=billing_status,
                        billing_checked_at=billing_checked_at,
                        asset_paths=asset_paths,
                    )

                # Stop accepting new streamed messages before snapshotting the session.
                interaction_session.stream.accepting = False
                batched_messages = self._merge_interaction_session_messages(
                    batched_messages=batched_messages,
                    session=interaction_session,
                )
                message_ids = [mid for mid, _m in batched_messages]
                latest_message_id = batched_messages[-1][0]
                combined_msg = self._combine_received_messages(batched_messages)
                if project_name:
                    combined_msg.project_name = project_name
                    for mid in message_ids:
                        try:
                            self.db.update_message_project(mid, project_name)
                        except Exception:
                            continue
                user_payloads = []
                for _mid, m in batched_messages:
                    text = (m.text or "").strip()
                    if not text and m.images:
                        text = "(attachment)"
                    if text:
                        user_payloads.append((text, m.received_at))

                # Use the final merged interaction payload and final workspace before running.
                asset_paths = await self._download_message_assets(combined_msg, workspace)
                self._write_interaction_context(
                    workspace=workspace,
                    tenant=tenant,
                    msg=combined_msg,
                    message_id=latest_message_id,
                    project_name=project_name,
                    active_run=active_run,
                    inflight_run=inflight_run,
                    billing_status=billing_status,
                    billing_checked_at=billing_checked_at,
                    asset_paths=asset_paths,
                )

                for payload, received_at in user_payloads:
                    self._append_chat_log(workspace.tasks_dir, "user", payload, received_at)
                write_chat_history(workspace.tasks_dir)

                if decision.get("repo_name") and decision.get("should_run"):
                    self._write_repo_name(workspace.tasks_dir, str(decision.get("repo_name")))

                if decision.get("dedupe") or not decision.get("should_run"):
                    self.db.update_message_statuses(message_ids, "processed")
                    interaction_session_status = "completed"
                    return OrchestratorResult(status="accepted", detail="no_run")

                if active_run and decision.get("supersede_active_run"):
                    superseded_run_id = None
                    try:
                        cancel_project = (
                            active_run.get("project_name") if isinstance(active_run, dict) else None
                        )
                    except Exception:
                        cancel_project = None
                    cancel_project = cancel_project or project_name
                    try:
                        workspace_for_cancel = await self._resolve_workspace(
                            tenant, project_name=cancel_project
                        )
                        if hasattr(self.agent, "cancel_run"):
                            await self.agent.cancel_run(workspace_for_cancel)
                    except Exception:
                        pass
                    try:
                        superseded_run_id = int(active_run["run_id"])
                        self.db.finish_run(superseded_run_id, status="failed", error="superseded")
                    except Exception:
                        pass
                    if superseded_run_id is not None:
                        try:
                            run_row = self.db.get_run(superseded_run_id)
                        except Exception:
                            run_row = None
                        superseded_message_id = None
                        if run_row is not None:
                            try:
                                superseded_message_id = (
                                    run_row.get("message_id")
                                    if hasattr(run_row, "get")
                                    else run_row["message_id"]
                                )
                            except Exception:
                                superseded_message_id = None
                        if superseded_message_id:
                            try:
                                self.db.update_message_status(int(superseded_message_id), "failed")
                            except Exception:
                                pass
                        try:
                            self.db.cancel_run_inputs_for_run(superseded_run_id)
                        except Exception:
                            pass
                    self.db.clear_active_run(tenant.id, project_name)
                    self._clear_inflight_stream(tenant.key, project_name)
                    active_run = None

                if active_run:
                    self.db.update_message_statuses(message_ids, "queued")
                    for mid, m in batched_messages:
                        self._enqueue_run_input(
                            tenant_id=tenant.id,
                            run_id=int(active_run["run_id"]),
                            project_name=project_name,
                            message_id=mid,
                            msg=m,
                            status="queued",
                            routing_decision=decision,
                        )
                    should_send_busy_ack = True
                    if decision.get("reply_sent"):
                        # Treat "reply_sent" as advisory. Only suppress the fallback
                        # ack if we can prove an outbound message was persisted.
                        since = combined_msg.received_at.isoformat()
                        try:
                            has_outbound = self.db.has_outbound_message_event_since(
                                tenant.id, since
                            )
                        except Exception:
                            has_outbound = False
                        should_send_busy_ack = not has_outbound
                    if should_send_busy_ack:
                        await self._send_busy_ack(
                            workspace=workspace,
                            tenant=tenant,
                            msg=combined_msg,
                            message_id=latest_message_id,
                            asset_paths=asset_paths,
                        )
                    self._write_request_status(workspace, tenant)
                    interaction_session_status = "completed"
                    return OrchestratorResult(status="busy", detail="tenant already running")

                lease_expires = (self._now() + timedelta(seconds=settings.run_lease_seconds)).isoformat()
                run_id = self.db.create_run(
                    tenant.id,
                    message_id=latest_message_id,
                    project_name=workspace.project_name,
                    lease_seconds=settings.run_lease_seconds,
                )
                if interaction_usage is not None:
                    self._record_interaction_usage(run_id, interaction_usage, workspace.tasks_dir)
                self.db.update_run_decision(run_id, decision)
                self.db.set_active_run(
                    tenant.id, workspace.project_name or "main", run_id, lease_expires
                )
                run_input_ids: list[str] = []
                for mid, m in batched_messages:
                    run_input_ids.append(
                        self._enqueue_run_input(
                            tenant_id=tenant.id,
                            run_id=run_id,
                            project_name=project_name,
                            message_id=mid,
                            msg=m,
                            status="claimed",
                            routing_decision=decision,
                        )
                    )
                # Ensure concurrent tasks don't re-route inputs already merged into this turn.
                try:
                    self.db.update_message_statuses(message_ids, "processing")
                except Exception:
                    pass
                should_send_starting_ack = True
                if decision.get("reply_sent"):
                    since = combined_msg.received_at.isoformat()
                    try:
                        has_outbound = self.db.has_outbound_message_event_since(
                            tenant.id, since
                        )
                    except Exception:
                        has_outbound = False
                    should_send_starting_ack = not has_outbound
                if should_send_starting_ack:
                    await self._send_interaction_instruction(
                        workspace=workspace,
                        tenant=tenant,
                        msg=combined_msg,
                        instruction=(
                            "Acknowledge the user's request and say you're starting the work. "
                            "Keep it brief and non-technical."
                        ),
                        run_id=run_id,
                        message_id=latest_message_id,
                        asset_paths=asset_paths,
                    )

                run_message_args = {
                    "tenant": tenant,
                    "msg": combined_msg,
                    "message_id": latest_message_id,
                    "message_ids": message_ids,
                    "run_id": run_id,
                    "run_input_ids": run_input_ids,
                    "process_queue": True,
                    "project_name": project_name,
                    "billing_status": billing_status,
                    "routing_decision": decision,
                    "asset_paths": asset_paths,
                }
                interaction_session_status = "completed"
            finally:
                if interaction_session_status != "completed":
                    self._rollback_interaction_processing_messages(interaction_session)
                self._close_interaction_session(tenant.id, status=interaction_session_status)

        if run_message_args is not None:
            return await self._run_message(**run_message_args)

        return OrchestratorResult(status="accepted")

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
        if not project_name and workspace.project_name:
            project_name = workspace.project_name
        event_intent = str(payload.get("intent") or "").strip()
        event_type = str(payload.get("event_type") or "").strip()
        if event_intent == "system_blocked" or event_type == "system_blocked":
            if not get_block(self.db, tenant.id, "system"):
                return OrchestratorResult(status="accepted", detail="block_cleared")
        else:
            if await self._handle_blocked(workspace.tasks_dir, tenant, notify=False):
                return OrchestratorResult(status="blocked", detail="system_blocked")
        inflight_run = self.db.get_inflight_run(tenant.id, project_name)
        if inflight_run:
            if self._reconcile_inflight_run(tenant, inflight_run, workspace):
                inflight_run = None
        # Reconcile first, then expire stale rows so completed runs are not
        # incorrectly transitioned to lease_expired.
        self.db.expire_stale_runs(tenant.id, project_name, self._now())
        inflight_run = self.db.get_inflight_run(tenant.id, project_name)
        if inflight_run and self._is_run_stale(inflight_run, max_age_seconds=900):
            await self._finalize_stale_run(tenant, inflight_run, notify=False)
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

    async def cancel_run(
        self,
        run_id: int,
        *,
        reason: str = "admin_cancelled",
        notify: bool = True,
    ) -> OrchestratorResult:
        run = self.db.get_run(run_id)
        if not run:
            return OrchestratorResult(status="not_found", detail="run_not_found")
        tenant = self.db.get_tenant_by_id(int(run["tenant_id"]))
        if tenant is None:
            return OrchestratorResult(status="not_found", detail="tenant_not_found")
        project_name = run["project_name"] if run and "project_name" in run.keys() else None
        self.db.finish_run(run_id, status="cancelled", error=reason)
        message_id = run.get("message_id") if hasattr(run, "get") else run["message_id"]
        if message_id:
            self.db.update_message_status(int(message_id), "processed")
        self.db.clear_active_run(tenant.id, project_name)
        self._clear_inflight_stream(tenant.key, project_name)
        self.db.cancel_run_inputs(tenant.id, project_name)
        workspace = await self._resolve_workspace(tenant, project_name=project_name)
        if hasattr(self.agent, "cancel_run"):
            try:
                await self.agent.cancel_run(workspace)
            except Exception:
                pass
        if notify:
            await self._send_interaction_instruction(
                workspace=workspace,
                tenant=tenant,
                msg=None,
                instruction=(
                    "Let the user know the current run was stopped and they can resend "
                    "their request when ready."
                ),
                run_id=run_id,
                message_id=int(message_id) if message_id else None,
            )
        return OrchestratorResult(status="accepted", detail="run_cancelled")

    async def _download_message_assets(
        self, msg: NormalizedMessage, workspace: Workspace
    ) -> list[str]:
        if not msg.images or not hasattr(self.messenger, "download_images"):
            return []
        try:
            return await self.messenger.download_images(msg.images, workspace.assets_dir)
        except Exception:
            return []

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
        billing_status: dict[str, Any] | None = None,
        routing_decision: dict[str, Any] | None = None,
        asset_paths: list[str] | None = None,
    ) -> OrchestratorResult:
        workspace = await self._resolve_workspace(tenant, project_name=project_name)
        message_ids = message_ids or ([message_id] if message_id else [])
        if message_ids:
            self.db.update_message_statuses(message_ids, "processing")
        self._write_request_status(workspace, tenant)

        asset_paths = asset_paths or []
        if not asset_paths and msg.images and hasattr(self.messenger, "download_images"):
            asset_paths = await self.messenger.download_images(msg.images, workspace.assets_dir)

        if billing_status is None and msg.provider != "event":
            billing_status = await self._fetch_billing_status(
                tenant=tenant,
                msg=msg,
                project_name=workspace.project_name,
                tasks_dir=workspace.tasks_dir,
            )

        task_content = self._build_task_content(
            msg,
            asset_paths,
            billing_status,
            routing_decision=routing_decision,
        )
        task_path = workspace.write_task(task_content)

        self._clear_run_artifacts(workspace.tasks_dir)

        settings = Settings()
        if run_id is None:
            run_id = self.db.create_run(
                tenant.id,
                message_id=message_id,
                project_name=workspace.project_name,
                lease_seconds=settings.run_lease_seconds,
            )
        can_update_billing_status = True
        inflight_run = self.db.get_inflight_run(tenant.id, workspace.project_name)
        if inflight_run:
            inflight_id = None
            try:
                inflight_id = int(inflight_run["id"])
            except (TypeError, ValueError, KeyError):
                inflight_id = None
            if inflight_id is None or run_id is None or inflight_id != int(run_id):
                can_update_billing_status = False
        if can_update_billing_status:
            billing_status_path = self._billing_status_path(workspace.tasks_dir)
            if billing_status_path.exists():
                try:
                    billing_status_path.unlink()
                except OSError:
                    pass
        if billing_status is not None:
            if run_id is not None:
                self._write_billing_status(workspace.tasks_dir, billing_status, run_id=run_id)
            if can_update_billing_status:
                self._write_billing_status(workspace.tasks_dir, billing_status)
        task_path_value = task_path
        try:
            task_path_value = task_path.relative_to(workspace.tenant_root)
        except Exception:
            task_path_value = task_path
        try:
            self.db.update_run_context(
                run_id,
                task_path=str(task_path_value),
                session_id=self._get_execution_session_id(tenant.id, workspace.project_name),
            )
        except Exception:
            pass
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
                session_id=self._get_execution_session_id(tenant.id, workspace.project_name),
                run_id=run_id,
                runtime_env=runtime_env,
            )
            result_total_cost = getattr(agent_result, "total_cost_usd", None)
            result_usage = getattr(agent_result, "usage", None)
            has_usage_signal = result_total_cost is not None or result_usage is not None
            if has_usage_signal and not self._usage_has_activity(result_total_cost, result_usage):
                raise RuntimeError("agent_result_no_usage_activity")
            if agent_result.session_id:
                self._set_execution_session_id(
                    tenant.id, workspace.project_name, agent_result.session_id
                )
                self.db.update_tenant_session(tenant.id, agent_result.session_id)
            self.db.add_run_usage(
                run_id,
                total_cost_usd=result_total_cost,
                usage=result_usage,
                usage_key="primary",
            )
            self.db.update_run_result_summary(run_id, getattr(agent_result, "summary", None))
            tool_summary = self._build_tool_summary(workspace.tasks_dir)
            self.db.update_run_tool_summary(run_id, tool_summary)
            tool_runs = self._read_tool_runs(workspace.tasks_dir)
            self.db.update_run_tool_runs(run_id, tool_runs)
            self._write_run_result(
                workspace.tasks_dir,
                run_id=run_id,
                result=agent_result,
                tool_summary=tool_summary,
            )
            self.db.finish_run(run_id, status="completed")
            if message_ids:
                self.db.update_message_statuses(message_ids, "processed")
            if run_input_ids:
                self.db.update_run_inputs_statuses(run_input_ids, "handled")
            self._clear_inflight_stream(tenant.key, workspace.project_name)
            self.db.clear_active_run(tenant.id, workspace.project_name)
            if process_queue:
                await self._drain_run_inputs(tenant, project_name=workspace.project_name)
            self._write_request_status(workspace, tenant)
            return OrchestratorResult(status="accepted")
        except Exception as exc:  # noqa: BLE001
            retry_run_inputs = bool(run_input_ids)
            if not self._primary_usage_recorded(run_id, None, None):
                self._set_execution_session_id(tenant.id, workspace.project_name, None)
                try:
                    self.db.update_tenant_session(tenant.id, "")
                except Exception:
                    pass
            self.db.finish_run(run_id, status="failed", error=str(exc))
            if message_ids:
                self.db.update_message_statuses(message_ids, "failed")
            if run_input_ids:
                self.db.update_run_inputs_statuses(
                    run_input_ids,
                    "queued" if retry_run_inputs else "failed",
                )
            self._clear_inflight_stream(tenant.key, workspace.project_name)
            self.db.clear_active_run(tenant.id, workspace.project_name)
            if msg.provider != "event" and self._should_send_failure_notice(
                tenant_id=tenant.id,
                project_name=workspace.project_name,
                message_id=message_id,
                retrying=retry_run_inputs,
            ):
                await self._send_interaction_instruction(
                    workspace=workspace,
                    tenant=tenant,
                    msg=msg,
                    instruction=(
                        "Let the user know there was a problem while working on their request "
                        "and to try again shortly."
                    ),
                    run_id=run_id,
                    message_id=message_id,
                )
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
            combined, message_ids, routing_decision = self._combine_run_inputs(rows)
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
                routing_decision=routing_decision,
            )

    def _should_send_failure_notice(
        self,
        *,
        tenant_id: int,
        project_name: str | None,
        message_id: int,
        retrying: bool,
    ) -> bool:
        if not retrying:
            return True
        try:
            failed_runs = self.db.count_failed_runs_for_message(
                tenant_id=tenant_id,
                message_id=message_id,
                project_name=project_name,
            )
        except Exception:
            return True
        return failed_runs <= self.MAX_RETRY_FAILURE_NOTICES

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

    async def _finalize_stale_run(
        self, tenant, row: Any, *, notify: bool = True
    ) -> None:
        run_id = row["id"]
        self.db.finish_run(run_id, status="failed", error="stale_run_timeout")
        message_id = row.get("message_id") if hasattr(row, "get") else row["message_id"]
        if message_id:
            self.db.update_message_status(int(message_id), "processed")
        project_name = row["project_name"] if row and "project_name" in row.keys() else None
        self.db.clear_active_run(tenant.id, project_name)
        self._clear_inflight_stream(tenant.key, project_name)
        self.db.cancel_run_inputs(tenant.id, project_name)
        workspace = None
        try:
            workspace = await self._resolve_workspace(tenant, project_name=project_name)
        except Exception:
            workspace = None
        if workspace is not None:
            if hasattr(self.agent, "cancel_run"):
                try:
                    await self.agent.cancel_run(workspace)
                except Exception:
                    pass
            if notify:
                await self._send_interaction_instruction(
                    workspace=workspace,
                    tenant=tenant,
                    msg=None,
                    instruction=(
                        "Let the user know the run took too long and was stopped. "
                        "Ask them to try again."
                    ),
                    run_id=run_id,
                    message_id=int(message_id) if message_id else None,
                )

    @staticmethod
    def _build_task_content(
        msg: NormalizedMessage,
        asset_paths: list[str] | None = None,
        billing_status: dict[str, Any] | None = None,
        routing_decision: dict[str, Any] | None = None,
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
        if routing_decision:
            facts_only = bool(routing_decision.get("facts_only"))
            purpose = str(routing_decision.get("purpose") or "").strip()
            plan = str(routing_decision.get("plan") or "").strip()
            notes = str(routing_decision.get("notes") or "").strip()
            lines.append("\n## Routing")
            lines.append(f"Facts-only: {facts_only}")
            if purpose:
                lines.append(f"Purpose: {purpose}")
            if plan:
                lines.append(f"Plan: {plan}")
            if notes:
                lines.append(f"Notes: {notes}")
            if facts_only:
                lines.append("Constraint: facts only, no build/edit/deploy. Reply snappy.")
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

    async def _run_interaction_agent(
        self,
        *,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage,
        message_id: int,
        billing_checked: bool = False,
        asset_paths: list[str] | None = None,
        inflight_stream: InflightTextStream | None = None,
    ) -> Any:
        interaction_runner = getattr(self.agent, "run_interaction_agent", None)
        if interaction_runner is None:
            interaction_runner = getattr(self.agent, "route_interaction", None)
        if interaction_runner is None:
            append_log(workspace.tasks_dir, "system", "interaction_agent_missing")
            try:
                self.db.update_message_status(message_id, "failed")
            except Exception:
                pass
            raise RuntimeError("interaction_agent_missing")
        try:
            runner_args = dict(
                workspace=workspace,
                message=msg,
                messenger=self.messenger,
                tenant_id=tenant.id,
                db=self.db,
                payments=self.payments,
                session_id=self._get_interaction_session_id(int(tenant.id), mode="route"),
                provider=msg.provider,
                tenant_external_id=msg.tenant_external_id,
                message_id=message_id,
                billing_checked=billing_checked,
                asset_paths=asset_paths,
                execution_bridge=self,
            )
            try:
                if "inflight_stream" in inspect.signature(interaction_runner).parameters:
                    runner_args["inflight_stream"] = inflight_stream
            except (TypeError, ValueError):
                pass
            result = await interaction_runner(**runner_args)
            session_from_result = ""
            if isinstance(result, dict):
                session_from_result = str(result.get("session_id") or "").strip()
            else:
                session_from_result = str(getattr(result, "session_id", "") or "").strip()
            if session_from_result:
                self._set_interaction_session_id(
                    int(tenant.id),
                    session_from_result,
                    mode="route",
                )
            return result
        except Exception as exc:  # noqa: BLE001
            try:
                append_log(
                    workspace.tasks_dir,
                    "system",
                    f"interaction_agent_failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            try:
                self.db.update_message_status(message_id, "failed")
            except Exception:
                pass
            raise

    async def _route_interaction(
        self,
        *,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage,
        message_id: int,
        billing_checked: bool = False,
        asset_paths: list[str] | None = None,
        inflight_stream: InflightTextStream | None = None,
    ) -> Any:
        # Backward-compatible alias for older call sites.
        return await self._run_interaction_agent(
            workspace=workspace,
            tenant=tenant,
            msg=msg,
            message_id=message_id,
            billing_checked=billing_checked,
            asset_paths=asset_paths,
            inflight_stream=inflight_stream,
        )

    def _normalize_interaction_decision(
        self,
        decision_result: Any,
        *,
        project_name: str | None = None,
        billing_checked: bool = False,
    ) -> tuple[dict[str, Any], Any | None]:
        usage_result = None
        decision: dict[str, Any] = {}
        if decision_result is None:
            decision = {}
        elif isinstance(decision_result, dict):
            decision = dict(decision_result)
        else:
            decision = dict(getattr(decision_result, "decision", {}) or {})
            if getattr(decision_result, "total_cost_usd", None) is not None or getattr(
                decision_result, "usage", None
            ):
                usage_result = decision_result
        decision.setdefault("project_name", project_name)
        decision.setdefault("should_run", False)
        decision.setdefault("queue_run", False)
        decision.setdefault("dedupe", False)
        decision.setdefault("supersede_active_run", False)
        decision.setdefault("ask_questions", [])
        decision.setdefault("billing_check", False)
        decision.setdefault("billing_checked", billing_checked)
        decision.setdefault("reply_sent", False)
        decision.setdefault("facts_only", False)
        if decision.get("dedupe"):
            decision["should_run"] = False
        return decision, usage_result

    @staticmethod
    def _interaction_decision_valid(decision_result: Any) -> bool:
        if decision_result is None:
            return False
        if isinstance(decision_result, dict):
            payload = decision_result
        else:
            payload = dict(getattr(decision_result, "decision", {}) or {})
        if not isinstance(payload, dict):
            return False
        if "should_run" not in payload:
            return False
        return isinstance(payload.get("should_run"), bool)

    def _write_interaction_context(
        self,
        *,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage,
        message_id: int,
        project_name: str | None,
        active_run: Any | None,
        inflight_run: Any | None,
        billing_status: dict[str, Any] | None,
        billing_checked_at: str | None,
        asset_paths: list[str] | None = None,
    ) -> None:
        try:
            queued = self.db.count_run_inputs(
                tenant.id, project_name=project_name, status="queued"
            )
        except Exception:
            queued = 0
        try:
            rows = self.db.list_recent_runs(
                tenant.id,
                project_name=project_name,
                limit=5,
            )
            recent_runs = [dict(row) for row in rows or []]
        except Exception:
            recent_runs = []
        payload = {
            "message_id": message_id,
            "message": {
                "provider": msg.provider,
                "provider_message_id": msg.provider_message_id,
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
                "assets": asset_paths or [],
            },
            "project_name": project_name,
            "active_run": dict(active_run) if active_run else None,
            "inflight_run": dict(inflight_run) if inflight_run else None,
            "queued_inputs": queued,
            "recent_runs": recent_runs,
            "billing_status": billing_status,
            "billing_checked_at": billing_checked_at,
            "execution_session_exists": bool(
                self._get_execution_session_id(tenant.id, project_name)
            ) if project_name else False,
        }
        path = workspace.tasks_dir / "interaction_context.json"
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
            tenant_root = self._tenant_root_for(tenant)
            self.workspace_manager.set_active_project(tenant_root, project_name)
        return msg, project_name

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
    def _extract_testing_mode_command(lines: list[str]) -> bool | None:
        for raw in lines:
            text = str(raw or "").strip()
            if not text:
                continue
            match = re.match(r"^/testing(?:@\w+)?(?:\s+(.+))?$", text, flags=re.IGNORECASE)
            if match is None:
                match = re.match(r"^testing\s*[:=]\s*(.+)$", text, flags=re.IGNORECASE)
            if not match:
                continue
            value = str(match.group(1) or "").strip().lower()
            if value in {"on", "true", "1", "enable", "enabled"}:
                return True
            if value in {"off", "false", "0", "disable", "disabled"}:
                return False
            return True
        return None

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
            "- The full event payload was stored in Supabase (tenant_events table).\n"
            "- Columns: event_type, payload_json, received_at.\n"
            "- You may query or update this data if needed.\n"
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
        self,
        workspace: Workspace,
        tenant: Any,
        msg: NormalizedMessage,
        *,
        message_id: int | None = None,
        asset_paths: list[str] | None = None,
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
            message_id=message_id,
            asset_paths=asset_paths,
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
        message_id: int | None = None,
        asset_paths: list[str] | None = None,
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
                session_id=self._get_interaction_session_id(int(tenant.id), mode="instruction"),
                provider=getattr(msg, "provider", None) or getattr(tenant, "provider", None),
                tenant_external_id=getattr(msg, "tenant_external_id", None)
                or getattr(tenant, "external_id", None),
                run_id=run_id,
                message_id=message_id,
                asset_paths=asset_paths,
                execution_bridge=self,
            )
            self._record_interaction_usage(run_id, result, workspace.tasks_dir)
            session_from_result = str(getattr(result, "session_id", "") or "").strip()
            if session_from_result:
                self._set_interaction_session_id(
                    int(tenant.id),
                    session_from_result,
                    mode="instruction",
                )
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
        message_id: int | None = None,
        asset_paths: list[str] | None = None,
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
                session_id=self._get_interaction_session_id(int(tenant.id), mode="instruction"),
                provider=getattr(msg, "provider", None) or getattr(tenant, "provider", None),
                tenant_external_id=getattr(msg, "tenant_external_id", None)
                or getattr(tenant, "external_id", None),
                run_id=run_id,
                message_id=message_id,
                asset_paths=asset_paths,
                execution_bridge=self,
            )
            self._record_interaction_usage(run_id, result, workspace.tasks_dir)
            session_from_result = str(getattr(result, "session_id", "") or "").strip()
            if session_from_result:
                self._set_interaction_session_id(
                    int(tenant.id),
                    session_from_result,
                    mode="instruction",
                )
        except Exception as exc:  # noqa: BLE001
            try:
                append_log(
                    workspace.tasks_dir,
                    "system",
                    f"interaction_send_failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass

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
        active_project = project_name or workspace.project_name
        self.db.finish_running_runs(tenant.id, active_project, error="user_reset")
        self.db.clear_pending_and_processing_messages(tenant.id, active_project)
        self.db.clear_active_run(tenant.id, active_project)
        self.db.cancel_run_inputs(tenant.id, active_project)
        self._clear_inflight_stream(tenant.key, active_project)
        self._set_execution_session_id(tenant.id, active_project, None)
        self._clear_run_artifacts(workspace.tasks_dir)
        for name in (
            "inflight_updates.jsonl",
            "inflight_consumed.jsonl",
            "interaction_updates.jsonl",
            "interaction_request.json",
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
                            received_raw = (
                                row["received_at"] if "received_at" in row.keys() else None
                            )
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
                            provider=str(
                                row.get("provider") if hasattr(row, "get") else row["provider"]
                            ),
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
                    try:
                        self._enqueue_run_input(
                            tenant_id=tenant.id,
                            run_id=None,
                            project_name=project_name,
                            message_id=message_id,
                            msg=msg,
                            status="queued",
                        )
                        migrated += 1
                    except Exception as exc:  # noqa: BLE001
                        if not self._is_run_input_duplicate(exc):
                            raise
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
    def _build_run_input_payload(
        msg: NormalizedMessage,
        message_id: int,
        routing_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
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
        if routing_decision is not None:
            payload["routing_decision"] = routing_decision
        return payload

    def _enqueue_run_input(
        self,
        *,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        message_id: int,
        msg: NormalizedMessage,
        status: str = "queued",
        routing_decision: dict[str, Any] | None = None,
    ) -> str:
        payload = self._build_run_input_payload(
            msg,
            message_id,
            routing_decision=routing_decision,
        )
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

    def _combine_run_inputs(
        self, rows: list[Any]
    ) -> tuple[NormalizedMessage | None, list[int], dict[str, Any] | None]:
        messages: list[NormalizedMessage] = []
        message_ids: list[int] = []
        routing_decision: dict[str, Any] | None = None
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
                raw_decision = payload.get("routing_decision")
                if isinstance(raw_decision, dict):
                    routing_decision = dict(raw_decision)
        if not messages:
            return None, message_ids, routing_decision

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
            routing_decision,
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
        self._ingest_tool_failures(tasks_dir, tenant.id)
        block = get_block(self.db, tenant.id, "system")
        if user_payload and block:
            if notify and self._should_notify_block(tenant.id, block):
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
                self._mark_block_notified(tenant.id, block)
            self._clear_block_state(tenant.id)
            return False
        if not block:
            return False
        if notify and self._should_notify_block(tenant.id, block):
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
            self._mark_block_notified(tenant.id, block)
        return True

    def _clear_block_state(self, tenant_id: int) -> None:
        clear_block(self.db, tenant_id, "system")
        self.db.set_tenant_kv(tenant_id, "system", "block_notified", None)

    @staticmethod
    def _looks_like_raw_usage_payload(payload: dict[str, Any]) -> bool:
        token_keys = {
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "service_tier",
            "server_tool_use",
            "cache_creation",
        }
        return any(key in payload for key in token_keys)

    def _usage_payload_has_primary(self, raw_usage: Any) -> bool:
        if raw_usage is None:
            return False
        payload = raw_usage
        if isinstance(raw_usage, str):
            try:
                payload = json.loads(raw_usage)
            except json.JSONDecodeError:
                return False
        if isinstance(payload, dict):
            if "primary" in payload:
                return True
            return self._looks_like_raw_usage_payload(payload)
        return True

    def _primary_usage_recorded(
        self,
        run_id: int,
        total_cost: float | None,
        usage: Any | None,
    ) -> bool:
        run_row = self.db.get_run(run_id)
        if not run_row:
            return False
        raw_usage = (
            run_row["usage_json"] if hasattr(run_row, "keys") else run_row.get("usage_json")
        )
        if self._usage_payload_has_primary(raw_usage):
            return True
        if usage is None and total_cost is not None:
            existing_total = (
                run_row["total_cost_usd"]
                if hasattr(run_row, "keys")
                else run_row.get("total_cost_usd")
            )
            if existing_total is not None and existing_total >= total_cost - 1e-6:
                return True
        return False

    @staticmethod
    def _numeric_usage_value(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        if numeric > 0:
            return numeric
        return 0.0

    @classmethod
    def _usage_has_activity(cls, total_cost: Any, usage: Any) -> bool:
        if cls._numeric_usage_value(total_cost) > 0:
            return True
        if not isinstance(usage, dict):
            return False
        token_keys = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        for key in token_keys:
            if cls._numeric_usage_value(usage.get(key)) > 0:
                return True
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                if cls._numeric_usage_value(cache_creation.get(key)) > 0:
                    return True
        return False

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
        stop_reason = str(payload.get("stop_reason") or "").strip() or None
        result_subtype = str(payload.get("result_subtype") or "").strip() or None
        if not error and result_subtype and result_subtype.startswith("error_"):
            error = f"agent_result_subtype:{result_subtype}"
        if (
            not error
            and stop_reason
            and stop_reason
            in {
                "max_tokens",
                "refusal",
                "model_context_window_exceeded",
                "pause_turn",
                "tool_use",
            }
        ):
            error = f"agent_stop_reason:{stop_reason}"
        if not error and stop_reason and stop_reason not in {"end_turn", "stop_sequence"}:
            error = f"agent_stop_reason:{stop_reason}"
        status = "failed" if error else "completed"
        run_id = run["id"] if hasattr(run, "keys") else run.get("id")
        total_cost = payload.get("total_cost_usd")
        usage = payload.get("usage")
        summary = payload.get("summary")
        tool_summary = payload.get("tool_summary")
        has_usage_signal = total_cost is not None or usage is not None
        has_usage_activity = self._usage_has_activity(total_cost, usage)
        if not error and has_usage_signal and not has_usage_activity:
            error = "agent_result_no_usage_activity"
            status = "failed"
        if (total_cost is not None or usage is not None) and not self._primary_usage_recorded(
            run_id, total_cost, usage
        ):
            self.db.add_run_usage(
                run_id,
                total_cost_usd=total_cost,
                usage=usage,
                usage_key="primary",
            )
        if summary is not None:
            self.db.update_run_result_summary(run_id, summary)
        if tool_summary is not None:
            self.db.update_run_tool_summary(run_id, tool_summary)
        tool_runs = self._read_tool_runs(workspace.tasks_dir)
        if tool_runs is not None:
            self.db.update_run_tool_runs(run_id, tool_runs)
        self.db.finish_run(run_id, status=status, error=error)
        project_name = (
            run["project_name"]
            if hasattr(run, "keys") and "project_name" in run.keys()
            else None
        )
        session_id = payload.get("session_id")
        if status == "completed" and session_id:
            self._set_execution_session_id(tenant.id, project_name, session_id)
            self.db.update_tenant_session(tenant.id, session_id)
        elif status == "failed" and has_usage_signal and not has_usage_activity:
            self._set_execution_session_id(tenant.id, project_name, None)
            try:
                self.db.update_tenant_session(tenant.id, "")
            except Exception:
                pass
        message_id = run["message_id"] if hasattr(run, "keys") else run.get("message_id")
        if message_id:
            self.db.update_message_status(
                int(message_id), "processed" if status == "completed" else "failed"
            )
        self.db.clear_active_run(tenant.id, project_name)
        self._clear_inflight_stream(tenant.key, project_name)
        return True

    def _supports_inflight_stream(self) -> bool:
        return bool(
            getattr(self.agent, "supports_inflight_stream", False)
            or getattr(self.agent, "supports_file_stream", False)
        )

    def _supports_file_streaming(self) -> bool:
        return bool(getattr(self.agent, "supports_file_stream", False))

    def list_execution_agents(
        self,
        *,
        tenant_id: int,
        tenant_key: str | None,
        project_name: str | None = None,
    ) -> list[dict[str, Any]]:
        active_rows: list[dict[str, Any]] = []
        if project_name:
            active = self.db.get_active_run(tenant_id, project_name)
            if isinstance(active, dict):
                active_rows.append(active)
        else:
            try:
                listed = self.db.list_active_runs(limit=200)
            except Exception:
                listed = []
            for row in listed:
                if not isinstance(row, dict):
                    continue
                try:
                    row_tenant_id = int(row.get("tenant_id"))
                except (TypeError, ValueError):
                    continue
                if row_tenant_id != int(tenant_id):
                    continue
                if project_name and str(row.get("project_name") or "") != str(project_name):
                    continue
                active_rows.append(row)
            if not active_rows:
                active = self.db.get_active_run(tenant_id, project_name)
                if isinstance(active, dict):
                    active_rows.append(active)
        inflight = self.db.get_inflight_run(tenant_id, project_name)
        seen: set[int] = set()
        agents: list[dict[str, Any]] = []

        def _append(
            source: str,
            run_row: dict[str, Any] | None,
            active_row: dict[str, Any] | None,
        ):
            if not run_row and not active_row:
                return
            run_id = None
            project = None
            if active_row:
                try:
                    run_id = int(active_row.get("run_id"))
                except (TypeError, ValueError):
                    run_id = None
                project = active_row.get("project_name")
            if run_row and run_id is None:
                try:
                    run_id = int(run_row.get("id"))
                except (TypeError, ValueError):
                    run_id = None
            if run_row and not project:
                project = run_row.get("project_name")
            if run_id is None or run_id in seen:
                return
            seen.add(run_id)
            stream = None
            if tenant_key and getattr(self.agent, "supports_inflight_stream", False):
                stream = self._get_inflight_stream(tenant_key, project)
            stream_supported = self._supports_inflight_stream()
            stream_active = stream is not None or (
                stream_supported and self._supports_file_streaming()
            )
            stream_accepting = bool(stream.accepting) if stream else stream_supported
            agents.append(
                {
                    "run_id": run_id,
                    "project_name": project,
                    "status": run_row.get("status") if run_row else None,
                    "started_at": run_row.get("started_at") if run_row else None,
                    "lease_expires_at": (
                        active_row.get("lease_expires_at") if active_row else None
                    ),
                    "source": source,
                    "stream_supported": stream_supported,
                    "stream_active": stream_active,
                    "stream_accepting": stream_accepting,
                }
            )

        active_by_project: dict[str, dict[str, Any]] = {}
        for active in active_rows:
            active_run_row = None
            try:
                active_run_id = int(active.get("run_id"))
            except (TypeError, ValueError):
                active_run_id = None
            if active_run_id is not None:
                try:
                    active_run_row = self.db.get_run(active_run_id)
                except Exception:
                    active_run_row = None
            _append("active_run", active_run_row, active)
            key = str(active.get("project_name") or "")
            if key not in active_by_project:
                active_by_project[key] = active
        inflight_active = None
        if isinstance(inflight, dict):
            inflight_active = active_by_project.get(str(inflight.get("project_name") or ""))
        _append("inflight_run", inflight, inflight_active)
        return agents

    async def stream_to_execution_agent(
        self,
        *,
        tenant_key: str,
        project_name: str | None,
        text: str,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        if not self._supports_inflight_stream():
            return {"ok": False, "status": "unsupported"}
        if not tenant_key:
            return {"ok": False, "status": "missing_tenant_key"}
        provider, external_id = self._split_tenant_key(tenant_key)
        if not provider or not external_id:
            return {"ok": False, "status": "missing_tenant_key"}
        tenant = self.db.get_tenant_by_external(provider, external_id)
        if tenant is None:
            return {"ok": False, "status": "tenant_not_found"}
        tenant_id = int(tenant.id)
        explicit_run_id = run_id is not None
        run_row = None
        if explicit_run_id:
            try:
                run_row = self.db.get_run(int(run_id))
            except Exception:
                run_row = None
            if not isinstance(run_row, dict):
                return {"ok": False, "status": "not_found"}
            try:
                run_tenant_id = int(run_row.get("tenant_id"))
            except (TypeError, ValueError):
                run_tenant_id = None
            if run_tenant_id != tenant_id:
                return {"ok": False, "status": "not_found"}
        if run_row and isinstance(run_row, dict):
            project_name = str(run_row.get("project_name") or "").strip() or project_name

        def _is_active_run_for_tenant(target_run_id: int) -> bool:
            try:
                active_rows = self.db.list_active_runs(limit=200)
            except Exception:
                active_rows = []
            for active in active_rows or []:
                if not isinstance(active, dict):
                    continue
                try:
                    active_tenant_id = int(active.get("tenant_id"))
                    active_run_id = int(active.get("run_id"))
                except (TypeError, ValueError):
                    continue
                if active_tenant_id == tenant_id and active_run_id == int(target_run_id):
                    return True
            if project_name:
                try:
                    active = self.db.get_active_run(tenant_id, project_name)
                except Exception:
                    active = None
                if isinstance(active, dict):
                    try:
                        return int(active.get("run_id")) == int(target_run_id)
                    except (TypeError, ValueError):
                        return False
            return False

        if run_id is not None and run_row is None:
            try:
                run_row = self.db.get_run(int(run_id))
            except Exception:
                run_row = None
            if not isinstance(run_row, dict):
                return {"ok": False, "status": "not_found"}
            try:
                run_tenant_id = int(run_row.get("tenant_id"))
            except (TypeError, ValueError):
                run_tenant_id = None
            if run_tenant_id != tenant_id:
                return {"ok": False, "status": "not_found"}
        if run_row and isinstance(run_row, dict):
            run_status = str(run_row.get("status") or "").strip().lower()
            if run_status != "running":
                return {"ok": False, "status": "no_active_run"}
            if not _is_active_run_for_tenant(int(run_id or 0)):
                return {"ok": False, "status": "no_active_run"}
        if run_row is None:
            candidates = self.list_execution_agents(
                tenant_id=tenant_id,
                tenant_key=tenant_key,
                project_name=project_name,
            )
            if project_name:
                filtered = [
                    candidate
                    for candidate in candidates
                    if str(candidate.get("project_name") or "").strip() == str(project_name)
                ]
                if filtered:
                    candidates = filtered
            if len(candidates) > 1:
                return {
                    "ok": False,
                    "status": "ambiguous_run",
                    "candidates": [
                        {
                            "run_id": candidate.get("run_id"),
                            "project_name": candidate.get("project_name"),
                            "status": candidate.get("status"),
                        }
                        for candidate in candidates
                    ],
                }
            if len(candidates) == 1:
                candidate = candidates[0]
                try:
                    run_id = int(candidate.get("run_id"))
                except (TypeError, ValueError):
                    run_id = None
                if not project_name:
                    project_name = str(candidate.get("project_name") or "").strip() or None
        if run_id is not None and run_row is None:
            try:
                run_row = self.db.get_run(int(run_id))
            except Exception:
                run_row = None
            if not isinstance(run_row, dict):
                return {"ok": False, "status": "no_active_run"}
            try:
                run_tenant_id = int(run_row.get("tenant_id"))
            except (TypeError, ValueError):
                run_tenant_id = None
            if run_tenant_id != tenant_id:
                return {"ok": False, "status": "not_found"}
            run_status = str(run_row.get("status") or "").strip().lower()
            if run_status != "running":
                return {"ok": False, "status": "no_active_run"}
            if not _is_active_run_for_tenant(int(run_id)):
                return {"ok": False, "status": "no_active_run"}
        persisted_stream_id = None
        if run_id is not None:
            try:
                persisted_stream_id = self.db.enqueue_execution_stream_input(
                    tenant_id=tenant_id,
                    run_id=int(run_id),
                    project_name=project_name,
                    message_id=None,
                    provider_message_id=None,
                    text=str(text or "").strip(),
                    assets=[],
                    status="pending",
                )
            except Exception:
                persisted_stream_id = None
        stream = None
        if getattr(self.agent, "supports_inflight_stream", False):
            stream = self._get_inflight_stream(tenant_key, project_name)
        if stream and stream.accepting:
            payload = str(text or "").strip()
            if not payload:
                return {"ok": False, "status": "empty"}
            try:
                stream.queue.put_nowait(payload)
            except Exception:
                return {"ok": False, "status": "enqueue_failed"}
            return {
                "ok": True,
                "status": "streamed",
                "run_id": run_id,
                "project_name": project_name,
                "stream_input_id": persisted_stream_id,
            }
        if run_id is None:
            return {"ok": False, "status": "no_active_run"}
        if persisted_stream_id is not None:
            return {
                "ok": True,
                "status": "db_stream",
                "run_id": run_id,
                "project_name": project_name,
                "stream_input_id": persisted_stream_id,
            }
        if self._supports_file_streaming():
            return {
                "ok": True,
                "status": "file_stream",
                "run_id": run_id,
                "project_name": project_name,
            }
        return {"ok": False, "status": "no_stream"}

    async def stop_execution_agent(
        self,
        *,
        run_id: int,
        reason: str = "user_requested",
        notify: bool = True,
    ) -> OrchestratorResult:
        return await self.cancel_run(run_id, reason=reason, notify=notify)

    @staticmethod
    def _split_tenant_key(tenant_key: str) -> tuple[str | None, str | None]:
        if not tenant_key:
            return None, None
        if ":" not in tenant_key:
            return None, None
        provider, external_id = tenant_key.split(":", 1)
        provider = provider.strip()
        external_id = external_id.strip()
        if not provider or not external_id:
            return None, None
        return provider, external_id

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
        repo_name = self._preferred_repo_name(
            workspace=workspace,
            tenant=tenant,
            prefix=config.repo_prefix,
        )
        attempts = 0
        while True:
            try:
                repo = await manager.ensure_repo(
                    project_root=workspace.root,
                    repo_name=repo_name,
                )
                token = await manager.create_repo_token(repo)
                break
            except Exception as exc:  # noqa: BLE001
                if self._is_repo_name_conflict(exc) and attempts < 2:
                    repo_name = self._next_repo_name(repo_name)
                    attempts += 1
                    continue
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

    @staticmethod
    def _slug_repo_name(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
        if len(cleaned) > MAX_REPO_NAME_LENGTH:
            cleaned = cleaned[:MAX_REPO_NAME_LENGTH].rstrip("-_")
        return cleaned

    @staticmethod
    def _read_repo_name(tasks_dir: Path) -> str | None:
        for name in ("repo_name.txt", "app_name.txt"):
            path = tasks_dir / name
            if not path.exists():
                continue
            try:
                value = path.read_text().strip()
            except OSError:
                continue
            if value:
                return value
        return None

    def _preferred_repo_name(
        self,
        *,
        workspace: Workspace,
        tenant: Any,
        prefix: str | None,
    ) -> str:
        preferred = self._read_repo_name(workspace.tasks_dir)
        if preferred:
            slugged = self._slug_repo_name(preferred)
            if slugged:
                return slugged
        return self._default_repo_name(
            prefix=prefix,
            tenant_id=getattr(tenant, "id", None),
            project_name=workspace.project_name,
        )

    @staticmethod
    def _next_repo_name(base: str) -> str:
        suffix = uuid.uuid4().hex[:6]
        limit = MAX_REPO_NAME_LENGTH - len(suffix) - 1
        trimmed = base[:limit].rstrip("-_")
        return f"{trimmed}-{suffix}"

    @staticmethod
    def _write_repo_name(tasks_dir: Path, repo_name: str) -> None:
        slugged = Orchestrator._slug_repo_name(repo_name)
        if not slugged:
            return
        path = tasks_dir / "repo_name.txt"
        try:
            path.write_text(slugged + "\n")
        except OSError:
            return

    @staticmethod
    def _is_repo_name_conflict(error: Exception) -> bool:
        message = str(error).lower()
        return "github_repo_name_conflict" in message or "name_conflict" in message

    @staticmethod
    def _is_run_input_duplicate(error: Exception) -> bool:
        message = str(error).lower()
        if "run_inputs_dedupe_idx" in message:
            return True
        if "duplicate key value" in message:
            return True
        if "already exists" in message:
            return True
        return False

    def _ingest_tool_failures(self, tasks_dir: Path, tenant_id: int) -> None:
        path = tasks_dir / "tool_runs.jsonl"
        if not path.exists():
            return
        try:
            cursor = self.db.get_tenant_kv(tenant_id, "system", "tool_runs_cursor") or {}
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
                        self.db,
                        tenant_id,
                        "system",
                        reason=f"{entry.get('tool')}:{status}",
                        max_failures=2,
                    )
        if latest_ts and latest_ts != last_ts:
            try:
                self.db.set_tenant_kv(
                    tenant_id, "system", "tool_runs_cursor", {"timestamp": latest_ts}
                )
            except Exception:
                return

    def _should_notify_block(self, tenant_id: int, block: dict[str, Any]) -> bool:
        try:
            cursor = self.db.get_tenant_kv(tenant_id, "system", "block_notified") or {}
        except Exception:
            return True
        last_at = cursor.get("at")
        block_at = block.get("at")
        if not block_at:
            return True
        return last_at != block_at

    def _mark_block_notified(self, tenant_id: int, block: dict[str, Any]) -> None:
        try:
            self.db.set_tenant_kv(tenant_id, "system", "block_notified", {"at": block.get("at")})
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
            "tool_runs.jsonl",
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
        if self._tenant_testing_mode_enabled(int(getattr(tenant, "id", 0) or 0)):
            purpose_payload = self._derive_billing_purpose(msg)
            payload: dict[str, Any] = {
                "status": "testing_mode",
                "payment_required": False,
                "allow_first_build": True,
                "plan": "testing",
                "testing_mode": True,
                "message": "testing mode active",
            }
            if purpose_payload:
                payload.update(purpose_payload)
            return payload
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

    def _tenant_testing_mode_enabled(self, tenant_id: int) -> bool:
        if tenant_id <= 0:
            return False
        try:
            payload = self.db.get_tenant_kv(tenant_id, "system", "testing_mode")
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        return bool(payload.get("enabled"))

    @staticmethod
    def _billing_status_path(tasks_dir: Path, run_id: int | None = None) -> Path:
        if run_id is None:
            return tasks_dir / "billing_status.json"
        return tasks_dir / f"billing_status_{run_id}.json"

    @staticmethod
    def _write_billing_status(
        tasks_dir: Path, payload: dict[str, Any], *, run_id: int | None = None
    ) -> None:
        try:
            path = Orchestrator._billing_status_path(tasks_dir, run_id=run_id)
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
    def _build_tool_summary(tasks_dir: Path) -> dict[str, Any] | None:
        path = tasks_dir / "tool_runs.jsonl"
        if not path.exists():
            return None
        tools: dict[str, dict[str, Any]] = {}
        total = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            tool_name = str(record.get("tool") or "").strip()
            if not tool_name:
                continue
            total += 1
            entry = tools.setdefault(tool_name, {"count": 0, "error_count": 0})
            entry["count"] += 1
            if record.get("error"):
                entry["error_count"] += 1
        return {"count": total, "tools": tools}

    @staticmethod
    def _read_tool_runs(tasks_dir: Path) -> list[dict[str, Any]] | None:
        path = tasks_dir / "tool_runs.jsonl"
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        runs: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                runs.append(record)
        return runs

    @staticmethod
    def _write_run_result(
        tasks_dir: Path,
        *,
        run_id: int,
        result: Any,
        tool_summary: dict[str, Any] | None,
    ) -> None:
        payload = {
            "run_id": run_id,
            "session_id": getattr(result, "session_id", None),
            "summary": getattr(result, "summary", None),
            "total_cost_usd": getattr(result, "total_cost_usd", None),
            "usage": getattr(result, "usage", None),
            "stop_reason": getattr(result, "stop_reason", None),
            "result_subtype": getattr(result, "result_subtype", None),
            "tool_summary": tool_summary,
        }
        path = tasks_dir / "run_result.json"
        try:
            path.write_text(json.dumps(payload, indent=2))
        except OSError:
            return


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

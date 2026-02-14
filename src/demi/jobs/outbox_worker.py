from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from pathlib import Path
import time
import logging

from demi.db.core import Database

INTERACTION_SESSION_NAMESPACE = "interaction"
INTERACTION_SESSION_KEY = "claude_session"
logger = logging.getLogger(__name__)


@dataclass
class OutboxWorkerConfig:
    poll_interval: float = 1.0
    batch_size: int = 50
    interaction_send_timeout_seconds: float = 600.0
    max_attempts: int = 12
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    fallback_scan_interval_seconds: float = 60.0
    stale_sending_seconds: int = 120


@dataclass
class OutboxWorker:
    db: Database
    messenger: Any
    config: OutboxWorkerConfig
    interaction_agent: Any | None = None
    workspace_manager: Any | None = None
    _running: bool = False
    _next_fallback_scan_monotonic: float = 0.0

    async def _db_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _get_interaction_session_id(self, tenant_id: int) -> str | None:
        try:
            payload = self.db.get_tenant_kv(
                int(tenant_id),
                INTERACTION_SESSION_NAMESPACE,
                INTERACTION_SESSION_KEY,
            ) or {}
        except Exception:
            return None
        value = str(payload.get("session_id") or "").strip()
        return value or None

    def _set_interaction_session_id(self, tenant_id: int, session_id: str | None) -> None:
        # Keep interaction continuity isolated from execution session IDs.
        cleaned = str(session_id or "").strip()
        payload = {"session_id": cleaned} if cleaned else None
        try:
            self.db.set_tenant_kv(
                int(tenant_id),
                INTERACTION_SESSION_NAMESPACE,
                INTERACTION_SESSION_KEY,
                payload,
            )
        except Exception:
            pass

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
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("OutboxWorker loop failed; continuing")
                    idle_streak = min(idle_streak + 1, 6)
                    await asyncio.sleep(
                        max(0.25, float(self.config.poll_interval)) * (2**idle_streak)
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def process_once(self) -> bool:
        fallback_processed = False
        now_monotonic = time.monotonic()
        if now_monotonic >= self._next_fallback_scan_monotonic:
            fallback_processed = await self._drain_interaction_fallbacks()
            interval = max(1.0, float(self.config.fallback_scan_interval_seconds))
            self._next_fallback_scan_monotonic = now_monotonic + interval

        rows = await self._db_call(
            self.db.claim_outbox,
            self.config.batch_size,
            self.config.stale_sending_seconds,
        )
        if not rows:
            return fallback_processed
        for row in rows:
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            payload = row.get("payload_json") if isinstance(row, dict) else None
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload_type = str(payload.get("type") or "user_message").strip().lower()
            if payload_type not in {"interaction_update", "pm_suggestion"}:
                text = str(payload.get("text") or "").strip()
                tenant_external_id = str(payload.get("tenant_external_id") or "").strip()
                if not text or not tenant_external_id:
                    await self._db_call(
                        self.db.update_outbox_status,
                        row_id,
                        "failed",
                        "invalid_payload_missing_text_or_tenant",
                        None,
                    )
                    continue
                payload = {
                    **payload,
                    "type": "interaction_update",
                    "action": "send_message",
                    "text": text,
                    "tenant_external_id": tenant_external_id,
                }
                payload_type = "interaction_update"
            if payload_type == "pm_suggestion":
                text = str(payload.get("text") or "").strip()
                tenant_external_id = str(payload.get("tenant_external_id") or "").strip()
                if not text or not tenant_external_id:
                    await self._db_call(
                        self.db.update_outbox_status,
                        row_id,
                        "failed",
                        "invalid_pm_suggestion_missing_text_or_tenant",
                        None,
                    )
                    continue
            ok, error = await self._handle_interaction_update(row, payload)
            if ok:
                await self._db_call(self.db.update_outbox_status, row_id, "sent", None, None)
            else:
                attempts = 1
                try:
                    attempts = int(row.get("attempt_count") or 1)
                except (TypeError, ValueError):
                    attempts = 1
                max_attempts = max(1, int(self.config.max_attempts))
                if attempts >= max_attempts:
                    await self._db_call(
                        self.db.update_outbox_status,
                        row_id,
                        "failed",
                        error,
                        None,
                    )
                    continue

                retry_seconds = self._compute_retry_seconds(attempts)
                await self._db_call(
                    self.db.update_outbox_status,
                    row_id,
                    "queued",
                    error,
                    retry_seconds,
                )
        return True

    async def _drain_interaction_fallbacks(self) -> bool:
        if self.interaction_agent is None or self.workspace_manager is None:
            return False
        try:
            tenants = await self._db_call(self.db.list_tenants)
        except Exception:
            return False
        processed = False
        for tenant in tenants or []:
            tenant_root = self._resolve_tenant_root(tenant)
            if tenant_root is None:
                continue
            for project_root in self._project_roots(tenant_root):
                tasks_dir = project_root / "tasks"
                if not tasks_dir.exists():
                    continue
                workspace = self.workspace_manager.ensure_workspace_at_path(
                    project_root,
                    tenant_root=tenant_root,
                )
                processed = (
                    await self._consume_interaction_files(tenant, workspace, tasks_dir)
                    or processed
                )
        return processed

    def _resolve_tenant_root(self, tenant: Any) -> Path | None:
        if self.workspace_manager is None:
            return None
        workspace_path = getattr(tenant, "workspace_path", None)
        if workspace_path:
            try:
                return self.workspace_manager.infer_tenant_root(Path(workspace_path))
            except Exception:
                return None
        try:
            return self.workspace_manager.tenant_root_for_key(getattr(tenant, "key"))
        except Exception:
            return None

    @staticmethod
    def _project_roots(tenant_root: Path) -> list[Path]:
        projects_dir = tenant_root / "projects"
        if projects_dir.exists():
            return [entry for entry in projects_dir.iterdir() if entry.is_dir()]
        if (tenant_root / "tasks").exists():
            return [tenant_root]
        return []

    async def _consume_interaction_files(
        self, tenant: Any, workspace: Any, tasks_dir: Path
    ) -> bool:
        processed = False
        updates_path = tasks_dir / "interaction_updates.jsonl"
        claimed_updates, payloads = self._read_update_jsonl(updates_path)
        if claimed_updates is not None:
            remaining: list[dict[str, Any]] = []
            for payload in payloads:
                sent, _error = await self._send_interaction_update(
                    tenant,
                    workspace,
                    payload,
                    self._coerce_run_id(payload.get("run_id")),
                )
                if sent:
                    processed = True
                else:
                    remaining.append(payload)
            self._finalize_update_jsonl(claimed_updates, remaining)

        request_path = tasks_dir / "interaction_request.json"
        claimed_request, payload = self._read_update_json(request_path)
        if claimed_request is not None:
            sent = False
            if isinstance(payload, dict):
                sent, _error = await self._send_interaction_update(
                    tenant,
                    workspace,
                    payload,
                    self._coerce_run_id(payload.get("run_id")),
                )
                if sent:
                    processed = True
            self._finalize_update_json(claimed_request, payload if not sent else None)
        return processed

    @staticmethod
    def _claim_update_path(path: Path) -> Path | None:
        claimed = path.with_name(path.name + ".processing")
        if not path.exists():
            return claimed if claimed.exists() else None
        try:
            path.replace(claimed)
            return claimed
        except OSError:
            return path

    def _read_update_jsonl(self, path: Path) -> tuple[Path | None, list[dict[str, Any]]]:
        claimed = self._claim_update_path(path)
        if claimed is None or not claimed.exists():
            return None, []
        try:
            lines = claimed.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        payloads: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
        return claimed, payloads

    def _read_update_json(self, path: Path) -> tuple[Path | None, dict[str, Any] | None]:
        claimed = self._claim_update_path(path)
        if claimed is None or not claimed.exists():
            return None, None
        try:
            payload = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        return claimed, payload if isinstance(payload, dict) else None

    @staticmethod
    def _original_update_path(path: Path) -> Path:
        name = path.name
        if name.endswith(".processing"):
            return path.with_name(name[: -len(".processing")])
        return path

    def _finalize_update_jsonl(self, claimed: Path, remaining: list[dict[str, Any]]) -> None:
        if not remaining:
            try:
                claimed.unlink()
            except OSError:
                pass
            return
        target = self._original_update_path(claimed)
        try:
            with target.open("w", encoding="utf-8") as handle:
                for payload in remaining:
                    handle.write(json.dumps(payload) + "\n")
        except OSError:
            return
        if target == claimed:
            return
        try:
            claimed.unlink()
        except OSError:
            pass

    def _finalize_update_json(self, claimed: Path, payload: dict[str, Any] | None) -> None:
        if payload is None:
            try:
                claimed.unlink()
            except OSError:
                pass
            return
        target = self._original_update_path(claimed)
        try:
            target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            return
        if target == claimed:
            return
        try:
            claimed.unlink()
        except OSError:
            pass

    async def _handle_interaction_update(
        self, row: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[bool, str | None]:
        if self.interaction_agent is None or self.workspace_manager is None:
            return False, "interaction_agent_unavailable"
        tenant_id = row.get("tenant_id")
        if tenant_id is None:
            return False, "missing_tenant_id"
        tenant = await self._db_call(self.db.get_tenant_by_id, int(tenant_id))
        if tenant is None:
            return False, "tenant_not_found"
        # Project routing is prompt-only; outbox delivery uses tenant-scoped workspace.
        if getattr(tenant, "workspace_path", None):
            tenant_root = self.workspace_manager.infer_tenant_root(Path(tenant.workspace_path))
            workspace = self.workspace_manager.ensure_workspace_at_path(
                tenant_root, tenant_root=tenant_root
            )
        else:
            workspace = self.workspace_manager.ensure_workspace(tenant.key)
        if "correlation_id" not in payload:
            correlation_id = str(row.get("correlation_id") or "").strip()
            if correlation_id:
                payload["correlation_id"] = correlation_id
        run_id = self._coerce_run_id(payload.get("run_id") or row.get("run_id"))
        return await self._send_interaction_update(tenant, workspace, payload, run_id)

    @staticmethod
    def _coerce_run_id(value: Any) -> int | None:
        if value is None:
            return None
        try:
            run_id = int(value)
        except (TypeError, ValueError):
            return None
        return run_id if run_id > 0 else None

    def _record_interaction_usage(
        self,
        run_id: int | None,
        result: Any,
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
        except Exception:
            return

    def _compute_retry_seconds(self, attempts: int) -> float:
        # Exponential backoff with a bounded cap to keep retries responsive
        # while preventing hot-loop retries on repeated failures.
        base = max(0.25, float(self.config.retry_base_seconds))
        max_wait = max(base, float(self.config.retry_max_seconds))
        exponent = max(0, int(attempts) - 1)
        delay = base * (2**exponent)
        return min(delay, max_wait)

    async def _send_interaction_update(
        self,
        tenant: Any,
        workspace: Any,
        payload: dict[str, Any],
        run_id: int | None = None,
    ) -> tuple[bool, str | None]:
        if self.interaction_agent is None:
            return False, "interaction_agent_unavailable"
        action = str(payload.get("action") or payload.get("type") or "send_message").strip()
        action = action.lower()
        if action == "interaction_update":
            action = str(payload.get("action") or "send_message").strip().lower()
        instruction = ""
        correlation_id = str(payload.get("correlation_id") or "").strip()
        if action == "pm_suggestion":
            priority = str(payload.get("priority") or "medium").strip().lower()
            pm_action_type = str(payload.get("pm_action_type") or "").strip() or "suggestion"
            autonomy_tier = str(payload.get("autonomy_tier") or "suggest").strip().lower()
            lines = [
                "Proactive suggestion for the user based on background analysis.",
                "Rewrite this into a short, natural Telegram message and send only if useful.",
                "It should be one idea, phrased as a helpful question — not pushy.",
                f"Category: {pm_action_type}",
                f"Urgency: {priority}",
                f"Correlation ID: {correlation_id or 'n/a'} (include as correlation_id if you send)",
                "",
                f"SUGGESTION:\n{payload.get('text')}",
            ]
            instruction = "\n".join(lines)
        elif action == "send_payment_link":
            reply_to_message_id = str(payload.get("reply_to_message_id") or "").strip()
            reply_to_text = str(payload.get("reply_to_text") or "").strip()
            lines = [
                "A payment link needs to be sent based on an execution update.",
                f"Order ID: {payload.get('order_id')}",
                f"Source: {payload.get('source')}",
                f"Correlation ID: {correlation_id or 'n/a'} (include as correlation_id if you send)",
                f"Text: {payload.get('text')}",
                f"Final: {bool(payload.get('final', False))}",
            ]
            if reply_to_message_id or reply_to_text:
                lines.append(
                    f"Reply context: id={reply_to_message_id or 'n/a'} "
                    f"text={reply_to_text or 'n/a'} (use reply_to_message_id/reply_to_text)"
                )
            lines.append("Use send_payment_link if you decide to send.")
            instruction = "\n".join(lines)
        else:
            reply_to_message_id = str(payload.get("reply_to_message_id") or "").strip()
            reply_to_text = str(payload.get("reply_to_text") or "").strip()
            final_flag = bool(payload.get("final", False))
            lines = [
                "Execution update (decide if the user should be notified now).",
                "If yes, send a short, friendly status update.",
                f"Correlation ID: {correlation_id or 'n/a'} (include as correlation_id if you send)",
                f"Final: {final_flag}. If final, set final: true on send_message.",
            ]
            if reply_to_message_id or reply_to_text:
                lines.append(
                    f"Reply context: id={reply_to_message_id or 'n/a'} "
                    f"text={reply_to_text or 'n/a'} (use reply_to_message_id/reply_to_text)"
                )
            lines.append("")
            lines.append(f"UPDATE:\n{payload.get('text')}")
            instruction = "\n".join(lines)
        try:
            # Keep this configurable for fast-fail retries and deterministic
            # tests while still guarding against invalid/zero values.
            timeout_seconds = max(0.01, float(self.config.interaction_send_timeout_seconds))
            result = await asyncio.wait_for(
                self.interaction_agent.send_interaction_instruction(
                    workspace=workspace,
                    instruction=instruction,
                    messenger=self.messenger,
                    tenant_id=tenant.id,
                    db=self.db,
                    payments=None,
                    session_id=self._get_interaction_session_id(int(tenant.id)),
                    provider=payload.get("provider") or tenant.provider,
                    tenant_external_id=payload.get("tenant_external_id") or tenant.external_id,
                    run_id=run_id,
                ),
                timeout=timeout_seconds,
            )
            self._record_interaction_usage(run_id, result)
            session_from_result = str(getattr(result, "session_id", "") or "").strip()
            if session_from_result:
                self._set_interaction_session_id(int(tenant.id), session_from_result)
            return True, None
        except asyncio.TimeoutError:
            return False, "interaction_update_timeout"
        except Exception as exc:  # noqa: BLE001
            return False, f"interaction_update_failed:{type(exc).__name__}"

    def stop(self) -> None:
        self._running = False

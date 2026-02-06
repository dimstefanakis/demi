from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

import json
import re
import uuid

from demi.db.core import Database
from demi.memory.logs import append_log, write_chat_history
from demi.agent.tool_logging import log_tool_run
from demi.config import Settings
from demi.workspace.project_decider import decide_project as decide_project_for_tenant
from demi.payments.stripe import StripeClient, StripeConfig, build_stripe_config


CHAT_SERVER_NAME = "demi-chat"
SEND_MESSAGE_TOOL = f"mcp__{CHAT_SERVER_NAME}__send_message"


@dataclass(frozen=True)
class ChatToolContext:
    messenger: Any
    tenant_external_id: str
    tasks_dir: Path
    tenant_key: str | None = None
    provider: str | None = None
    db: Database | None = None
    tenant_id: int | None = None
    payments: Any | None = None
    execution_bridge: Any | None = None
    role: str = "primary"
    run_id: int | None = None
    message_id: int | None = None
    on_interaction_message_sent: Callable[[], None] | None = None


def build_chat_tools(context: ChatToolContext) -> list[SdkMcpTool[Any]]:
    import time

    def _resolve_payments() -> StripeClient | None:
        if context.payments is not None:
            return context.payments
        settings = Settings()
        config = build_stripe_config(settings)
        if config is None:
            return None
        return StripeClient(config)

    def _normalize_assistant_purpose(value: Any | None) -> str | None:
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
        return slug[:60] if slug else None

    def _assistant_order_type(purpose: str | None) -> str:
        if purpose:
            return f"assistant_subscription:{purpose}"
        return "assistant_subscription"

    def _log(
        tool_name: str,
        args: dict[str, Any],
        result: Any | None = None,
        error: str | None = None,
        start: float | None = None,
    ) -> None:
        duration_ms = None
        if start is not None:
            duration_ms = (time.monotonic() - start) * 1000.0
        log_tool_run(
            context.tasks_dir,
            tool_name,
            args=args,
            result=result,
            error=error,
            duration_ms=duration_ms,
        )

    def _payment_required() -> bool:
        path = context.tasks_dir / "billing_status.json"
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        return bool(payload.get("payment_required"))

    def _resolve_tenant_id() -> int | None:
        if context.tenant_id is not None:
            return context.tenant_id
        if context.db is None or not context.provider or not context.tenant_external_id:
            return None
        try:
            tenant = context.db.get_tenant_by_external(context.provider, context.tenant_external_id)
        except Exception:
            return None
        if tenant is None:
            return None
        return int(getattr(tenant, "id", None) or tenant["id"])

    def _record_outbound_event(
        *,
        text: str,
        message_type: str,
        reply_to_message_id: str | None = None,
        reply_to_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if context.db is None:
            return
        tenant_id = _resolve_tenant_id()
        if tenant_id is None:
            return
        run_id = _current_run_id(context)
        current_message = _current_message(context) or {}
        provider_message_id = current_message.get("provider_message_id")
        payload = dict(metadata or {})
        if reply_to_text:
            payload["reply_to_text"] = reply_to_text
        if current_message.get("text"):
            payload.setdefault("context_message_text", current_message.get("text"))
        try:
            context.db.record_message_event(
                tenant_id=tenant_id,
                direction="outbound",
                provider=context.provider,
                tenant_external_id=context.tenant_external_id,
                message_type=message_type,
                text=text,
                project_name=_project_name_from_tasks_dir(context.tasks_dir),
                run_id=run_id,
                reply_to_message_id=reply_to_message_id or None,
                provider_message_id=str(provider_message_id or "") or None,
                metadata=payload or None,
            )
        except Exception:
            return

    def _interaction_context_payload() -> dict[str, Any] | None:
        path = context.tasks_dir / "interaction_context.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _interaction_message_meta() -> dict[str, Any]:
        payload = _interaction_context_payload() or {}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        return {
            "message_id": payload.get("message_id"),
            "provider_message_id": message.get("provider_message_id"),
            "assets": message.get("assets") or [],
            "project_name": payload.get("project_name"),
        }

    def _append_inflight_update_file(
        *,
        text: str,
        assets: list[str],
        message_id: int | None,
        provider_message_id: str | None,
        run_id: int | None,
    ) -> None:
        path = context.tasks_dir / "inflight_updates.jsonl"
        entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "text": text,
            "assets": assets or [],
        }
        if run_id is not None:
            entry["run_id"] = int(run_id)
        if message_id is not None:
            entry["message_id"] = message_id
        if provider_message_id:
            entry["provider_message_id"] = provider_message_id
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except OSError:
            return

    def _format_stream_text(text: str, assets: list[str]) -> str:
        text = text.strip()
        if assets:
            suffix = "\n".join(f"- {path}" for path in assets if path)
            if suffix:
                header = "\n\nAttachments:\n"
                if text:
                    text = f"{text}{header}{suffix}"
                else:
                    text = f"Attachments:\n{suffix}"
        return text

    def _enqueue_interaction_update(payload: dict[str, Any]) -> bool:
        if context.db is None:
            return _write_interaction_fallback(payload)
        tenant_id = context.tenant_id
        if tenant_id is None and context.provider and context.tenant_external_id:
            try:
                tenant = context.db.get_tenant_by_external(
                    context.provider, context.tenant_external_id
                )
            except Exception:
                tenant = None
            if tenant is not None:
                tenant_id = tenant.id
        if tenant_id is None:
            return _write_interaction_fallback(payload)
        try:
            correlation_id = payload.get("correlation_id")
            if not correlation_id:
                correlation_id = f"interaction-update:{uuid.uuid4()}"
                payload["correlation_id"] = correlation_id
            context.db.enqueue_outbox(
                tenant_id=tenant_id,
                run_id=None,
                project_name=payload.get("project_name"),
                correlation_id=str(correlation_id),
                payload=payload,
            )
            return True
        except Exception:
            return _write_interaction_fallback(payload)

    def _write_interaction_fallback(payload: dict[str, Any]) -> bool:
        path = context.tasks_dir / "interaction_updates.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if "correlation_id" not in payload:
                payload["correlation_id"] = f"interaction-update:{uuid.uuid4()}"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
            return True
        except OSError:
            return False

    @tool(
        "should_send_message",
        "Check recent chat history to see if a message would be redundant.",
        {"text": str, "reply_to_message_id": str, "reply_to_text": str},
    )
    async def should_send_message(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        text = str(args.get("text", "")).strip()
        if not text:
            payload = {"send": False, "reason": "empty"}
            _log("should_send_message", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        # Intentionally ignore reply_to_* to allow late updates from long-running runs.
        decision, reason = _should_send(text, context)
        payload = {"send": decision, "reason": reason}
        _log("should_send_message", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "decide_project",
        "Decide which project to work on based on chat history and project context.",
        {"text": str, "set_active": bool, "switch_context": bool},
    )
    async def decide_project(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        text = str(args.get("text", "")).strip()
        set_active = bool(args.get("set_active", False))
        switch_context = bool(args.get("switch_context", False))
        if not text:
            payload = {"ok": False, "status": "missing_text"}
            _log("decide_project", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        tenant_root = _tenant_root_from_tasks_dir(context.tasks_dir)
        decision = decide_project_for_tenant(tenant_root, text)
        if decision is None:
            payload = {"ok": False, "status": "no_decision"}
            _log("decide_project", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        active_updated = False
        if set_active:
            active_updated = _set_active_project(tenant_root, decision.project_name)
        project_root = (Path(tenant_root) / "projects" / decision.project_name).resolve()
        switched = False
        if switch_context and project_root.exists():
            switched = _migrate_current_task_context(context.tasks_dir, project_root / "tasks")
        payload = {
            "ok": True,
            "project_name": decision.project_name,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "scores": decision.scores,
            "active_updated": active_updated,
            "project_root": str(project_root),
            "switched": switched,
        }
        _log("decide_project", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "check_for_status",
        "Fetch the current run status and queued inputs for this tenant/project.",
        {"project_name": str},
    )
    async def check_for_status(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if context.db is None or context.tenant_id is None:
            payload = {"ok": False, "status": "missing_db"}
            _log("check_for_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        project_name = str(args.get("project_name") or "").strip() or None
        run = context.db.get_inflight_run(context.tenant_id, project_name)
        active = context.db.get_active_run(context.tenant_id, project_name)
        queued = context.db.count_run_inputs(
            context.tenant_id, project_name=project_name, status="queued"
        )
        payload = {
            "ok": True,
            "project_name": project_name,
            "active_run": dict(active) if active else None,
            "inflight_run": dict(run) if run else None,
            "queued_inputs": queued,
        }
        _log("check_for_status", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "find_execution_agent",
        "Find active execution agent runs for this tenant/project.",
        {"project_name": str},
    )
    async def find_execution_agent(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if str(context.role or "primary") != "interaction":
            payload = {"ok": False, "status": "interaction_only"}
            _log("find_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if context.execution_bridge is None:
            payload = {"ok": False, "status": "bridge_unavailable"}
            _log("find_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        tenant_id = _resolve_tenant_id()
        if tenant_id is None:
            payload = {"ok": False, "status": "missing_tenant"}
            _log("find_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        project_name = str(args.get("project_name") or "").strip() or None
        if not project_name:
            meta = _interaction_message_meta()
            project_name = str(meta.get("project_name") or "").strip() or None
        try:
            agents = context.execution_bridge.list_execution_agents(
                tenant_id=tenant_id,
                tenant_key=context.tenant_key,
                project_name=project_name,
            )
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "status": "error", "error": str(exc)}
            _log("find_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        payload = {"ok": True, "count": len(agents), "agents": agents}
        _log("find_execution_agent", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "stream_to_execution_agent",
        "Stream a new user message to an active execution agent.",
        {
            "text": str,
            "project_name": str,
            "run_id": int,
            "assets": list,
        },
    )
    async def stream_to_execution_agent(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if str(context.role or "primary") != "interaction":
            payload = {"ok": False, "status": "interaction_only"}
            _log("stream_to_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if context.execution_bridge is None:
            payload = {"ok": False, "status": "bridge_unavailable"}
            _log("stream_to_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        text = str(args.get("text") or "").strip()
        raw_assets = args.get("assets") or []
        assets = [str(item).strip() for item in raw_assets if str(item).strip()]
        if not assets:
            meta = _interaction_message_meta()
            meta_assets = meta.get("assets") or []
            assets = [str(item).strip() for item in meta_assets if str(item).strip()]
        if not text and not assets:
            payload = {"ok": False, "status": "empty"}
            _log("stream_to_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        project_name = str(args.get("project_name") or "").strip() or None
        run_id = args.get("run_id")
        try:
            run_id = int(run_id) if run_id is not None else None
        except (TypeError, ValueError):
            run_id = None
        if not project_name:
            meta = _interaction_message_meta()
            project_name = str(meta.get("project_name") or "").strip() or None
        stream_text = _format_stream_text(text, assets)
        tenant_key = context.tenant_key
        if not tenant_key and context.provider and context.tenant_external_id:
            tenant_key = f"{context.provider}:{context.tenant_external_id}"
        if not tenant_key:
            payload = {"ok": False, "status": "missing_tenant_key"}
            _log("stream_to_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if run_id is None:
            tenant_id = _resolve_tenant_id()
            if tenant_id is None:
                tenant_id = 0
            agents: list[dict[str, Any]] = []
            try:
                listed = context.execution_bridge.list_execution_agents(
                    tenant_id=tenant_id,
                    tenant_key=tenant_key,
                    project_name=project_name,
                )
                agents = [agent for agent in listed if isinstance(agent, dict)]
            except Exception:
                agents = []
            candidates = agents
            if project_name:
                project_filtered = [
                    agent
                    for agent in candidates
                    if str(agent.get("project_name") or "").strip() == project_name
                ]
                if project_filtered:
                    candidates = project_filtered
            if len(candidates) == 1:
                candidate = candidates[0]
                try:
                    run_id = int(candidate.get("run_id"))
                except (TypeError, ValueError):
                    run_id = None
                if not project_name:
                    project_name = str(candidate.get("project_name") or "").strip() or None
            elif len(candidates) > 1:
                payload = {
                    "ok": False,
                    "status": "ambiguous_run",
                    "candidates": [
                        {
                            "run_id": agent.get("run_id"),
                            "project_name": agent.get("project_name"),
                            "status": agent.get("status"),
                        }
                        for agent in candidates
                    ],
                }
                _log("stream_to_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
        try:
            result = await context.execution_bridge.stream_to_execution_agent(
                tenant_key=tenant_key,
                project_name=project_name,
                run_id=run_id,
                text=stream_text,
            )
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "status": "error", "error": str(exc)}
            _log("stream_to_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if result.get("ok") and str(result.get("status") or "") == "file_stream":
            meta = _interaction_message_meta()
            message_id = meta.get("message_id")
            try:
                message_id = int(message_id) if message_id is not None else None
            except (TypeError, ValueError):
                message_id = None
            provider_message_id = meta.get("provider_message_id")
            result_run_id = result.get("run_id")
            try:
                result_run_id = int(result_run_id) if result_run_id is not None else run_id
            except (TypeError, ValueError):
                result_run_id = run_id
            _append_inflight_update_file(
                text=text or "(attachment)",
                assets=assets,
                message_id=message_id,
                provider_message_id=str(provider_message_id or "").strip() or None,
                run_id=result_run_id,
            )
        payload = dict(result)
        _log("stream_to_execution_agent", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": not bool(result.get("ok")),
        }

    @tool(
        "stop_execution_agent",
        "Stop an active execution agent run.",
        {"run_id": int, "project_name": str, "reason": str, "notify": bool},
    )
    async def stop_execution_agent(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if str(context.role or "primary") != "interaction":
            payload = {"ok": False, "status": "interaction_only"}
            _log("stop_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if context.execution_bridge is None:
            payload = {"ok": False, "status": "bridge_unavailable"}
            _log("stop_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        run_id = args.get("run_id")
        try:
            run_id = int(run_id) if run_id is not None else None
        except (TypeError, ValueError):
            run_id = None
        tenant_id = _resolve_tenant_id()
        project_name = str(args.get("project_name") or "").strip() or None
        run_row = None
        if run_id is not None:
            if context.db is None or tenant_id is None:
                payload = {"ok": False, "status": "missing_db"}
                _log("stop_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            try:
                run_row = context.db.get_run(run_id)
            except Exception:
                run_row = None
            if not isinstance(run_row, dict):
                payload = {"ok": False, "status": "not_found"}
                _log("stop_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            try:
                run_tenant_id = int(run_row.get("tenant_id"))
            except (TypeError, ValueError):
                run_tenant_id = None
            if run_tenant_id != int(tenant_id):
                payload = {"ok": False, "status": "not_found"}
                _log("stop_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            if not project_name:
                project_name = str(run_row.get("project_name") or "").strip() or None
        if run_id is None:
            if tenant_id is None or context.db is None:
                payload = {"ok": False, "status": "missing_run"}
                _log("stop_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            if not project_name:
                meta = _interaction_message_meta()
                project_name = str(meta.get("project_name") or "").strip() or None
            active = context.db.get_active_run(tenant_id, project_name)
            if active:
                try:
                    run_id = int(active["run_id"])
                except (TypeError, ValueError, KeyError):
                    run_id = None
                try:
                    run_row = context.db.get_run(int(run_id)) if run_id is not None else None
                except Exception:
                    run_row = None
        if run_id is None:
            payload = {"ok": False, "status": "not_found"}
            _log("stop_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if context.db is not None and tenant_id is not None and run_row is None:
            try:
                run_row = context.db.get_run(int(run_id))
            except Exception:
                run_row = None
            if not isinstance(run_row, dict):
                payload = {"ok": False, "status": "not_found"}
                _log("stop_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            try:
                run_tenant_id = int(run_row.get("tenant_id"))
            except (TypeError, ValueError):
                run_tenant_id = None
            if run_tenant_id != int(tenant_id):
                payload = {"ok": False, "status": "not_found"}
                _log("stop_execution_agent", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
        reason = str(args.get("reason") or "user_requested").strip()
        notify = bool(args.get("notify", True))
        try:
            result = await context.execution_bridge.stop_execution_agent(
                run_id=run_id,
                reason=reason,
                notify=notify,
            )
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "status": "error", "error": str(exc)}
            _log("stop_execution_agent", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        payload = {
            "ok": True,
            "status": getattr(result, "status", "accepted"),
            "detail": getattr(result, "detail", None),
            "run_id": run_id,
        }
        _log("stop_execution_agent", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "send_message",
        "Send a user-facing chat update via the active messaging provider.",
        {"text": str, "final": bool, "reply_to_message_id": str, "reply_to_text": str},
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if str(context.role or "primary") != "interaction":
            text = str(args.get("text", "")).strip()
            update_payload = {
                "type": "interaction_update",
                "action": "send_message",
                "text": text,
                "final": bool(args.get("final", False)),
                "reply_to_message_id": str(args.get("reply_to_message_id", "")).strip(),
                "reply_to_text": str(args.get("reply_to_text", "")).strip(),
                "tenant_external_id": context.tenant_external_id,
                "provider": context.provider,
                "project_name": _project_name_from_tasks_dir(context.tasks_dir),
                "run_id": _current_run_id(context),
            }
            if not _enqueue_interaction_update(update_payload):
                payload = {"queued": False, "status": "interaction_update_failed"}
                _log("send_message", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            payload = {"queued": True, "status": "interaction_required"}
            _log("send_message", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        text = str(args.get("text", "")).strip()
        final = bool(args.get("final", False))
        reply_to_message_id = str(args.get("reply_to_message_id") or "").strip() or None
        reply_to_text = str(args.get("reply_to_text") or "").strip() or None
        allowed_reply, _ = _reply_context_allowed(
            context, reply_to_message_id=reply_to_message_id, reply_to_text=reply_to_text
        )
        if not allowed_reply:
            reply_to_message_id = None
            reply_to_text = None
        if not text:
            _log("send_message", args, result={"skipped": "empty"}, start=start)
            return {
                "content": [{"type": "text", "text": "Skipped empty message."}],
                "is_error": False,
            }
        run_id = _current_run_id(context)
        if "checkout.stripe.com" in text:
            payload = {
                "ok": False,
                "error": "stripe_link_not_allowed",
                "message": "Use send_payment_link for Stripe URLs.",
            }
            _log("send_message", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        send_ok, send_reason = _should_send(text, context)
        if not send_ok:
            _log("send_message", args, result={"skipped": send_reason}, start=start)
            return {
                "content": [{"type": "text", "text": f"Skipped: {send_reason}."}],
                "is_error": False,
            }
        try:
            await context.messenger.send_text(
                context.tenant_external_id, text, reply_to_message_id=reply_to_message_id
            )
        except Exception as exc:  # noqa: BLE001
            _log("send_message", args, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": f"Send failed: {exc}"}],
                "is_error": True,
            }

        append_log(context.tasks_dir, "assistant_message", text)
        write_chat_history(context.tasks_dir)
        if final and run_id:
            _set_final_sent(context, run_id)
        if str(context.role or "primary") == "interaction" and context.on_interaction_message_sent:
            try:
                context.on_interaction_message_sent()
            except Exception:
                pass
        _record_outbound_event(
            text=text,
            message_type="message",
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            metadata={"final": final},
        )
        _log("send_message", args, result={"sent": True}, start=start)
        return {
            "content": [{"type": "text", "text": "Sent."}],
            "is_error": False,
        }

    @tool(
        "ack_inflight_updates",
        "Record that in-flight updates were consumed so they won't be re-queued.",
        {"message_ids": list, "provider_message_ids": list, "updates": list, "clear": bool},
    )
    async def ack_inflight_updates(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        updates: list[dict[str, Any]] = []
        raw_updates = args.get("updates")
        if isinstance(raw_updates, list):
            for item in raw_updates:
                if not isinstance(item, dict):
                    continue
                updates.append(item)
        else:
            message_ids = args.get("message_ids") or []
            provider_ids = args.get("provider_message_ids") or []
            if not isinstance(message_ids, list):
                message_ids = [message_ids]
            if not isinstance(provider_ids, list):
                provider_ids = [provider_ids]
            for idx, raw_id in enumerate(message_ids):
                entry: dict[str, Any] = {"message_id": raw_id}
                if idx < len(provider_ids):
                    entry["provider_message_id"] = provider_ids[idx]
                updates.append(entry)
        clear = bool(args.get("clear", True))

        if not updates:
            payload = {"ok": False, "status": "no_updates"}
            _log("ack_inflight_updates", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        consumed_path = context.tasks_dir / "inflight_consumed.jsonl"
        consumed_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(tz=timezone.utc).isoformat()
        written = 0
        with consumed_path.open("a", encoding="utf-8") as handle:
            for update in updates:
                if not isinstance(update, dict):
                    continue
                entry = {"timestamp": timestamp}
                if "message_id" in update:
                    entry["message_id"] = update.get("message_id")
                if "provider_message_id" in update:
                    entry["provider_message_id"] = update.get("provider_message_id")
                handle.write(json.dumps(entry) + "\n")
                written += 1

        if clear:
            inflight_path = context.tasks_dir / "inflight_updates.jsonl"
            if inflight_path.exists():
                try:
                    inflight_path.unlink()
                except OSError:
                    pass

        payload = {"ok": True, "count": written}
        _log("ack_inflight_updates", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "send_payment_link",
        "Send a payment link message using the stored Stripe URL for a billing order.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "number"},
                "source": {"type": "string"},
                "text": {"type": "string"},
                "final": {"type": "boolean"},
                "reply_to_message_id": {"type": "string"},
                "reply_to_text": {"type": "string"},
            },
            "required": ["text"],
        },
    )
    async def send_payment_link(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if str(context.role or "primary") != "interaction":
            update_payload = {
                "type": "interaction_update",
                "action": "send_payment_link",
                "order_id": args.get("order_id"),
                "source": str(args.get("source") or "").strip(),
                "text": str(args.get("text", "")).strip(),
                "final": bool(args.get("final", False)),
                "reply_to_message_id": str(args.get("reply_to_message_id") or "").strip(),
                "reply_to_text": str(args.get("reply_to_text") or "").strip(),
                "tenant_external_id": context.tenant_external_id,
                "provider": context.provider,
                "project_name": _project_name_from_tasks_dir(context.tasks_dir),
                "run_id": _current_run_id(context),
            }
            if not _enqueue_interaction_update(update_payload):
                payload = {"queued": False, "status": "interaction_update_failed"}
                _log("send_payment_link", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            payload = {"queued": True, "status": "interaction_required"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        final = bool(args.get("final", False))
        reply_to_message_id = str(args.get("reply_to_message_id") or "").strip() or None
        reply_to_text = str(args.get("reply_to_text") or "").strip() or None
        allowed_reply, _ = _reply_context_allowed(
            context, reply_to_message_id=reply_to_message_id, reply_to_text=reply_to_text
        )
        if not allowed_reply:
            reply_to_message_id = None
            reply_to_text = None
        order_id = None
        if args.get("order_id") is not None:
            try:
                order_id = int(args.get("order_id"))
            except (TypeError, ValueError):
                payload = {"ok": False, "status": "invalid_order_id"}
                _log("send_payment_link", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
        text = str(args.get("text", "")).strip()
        if not text:
            payload = {"ok": False, "status": "missing_text"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        payment_url = None
        if order_id is not None and context.db is not None and context.tenant_id is not None:
            order = context.db.get_billing_order(order_id)
            if order is None or int(order["tenant_id"]) != context.tenant_id:
                payload = {"ok": False, "status": "order_not_found"}
                _log("send_payment_link", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            payment_url = str(order["stripe_payment_url"] or "").strip() or None

        source = str(args.get("source") or "").strip().lower()
        if payment_url is None:
            payment_url = _load_payment_url(context, source=source, order_id=order_id)
        if not payment_url:
            payload = {"ok": False, "status": "missing_payment_url"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        sanitized = _strip_urls(text).strip()
        if not sanitized:
            payload = {"ok": False, "status": "missing_text_after_sanitize"}
            _log("send_payment_link", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        message = f"{sanitized}\n\n{payment_url}"

        try:
            await context.messenger.send_text(
                context.tenant_external_id,
                message,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log("send_payment_link", args, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": f"Send failed: {exc}"}],
                "is_error": True,
            }

        append_log(context.tasks_dir, "assistant_message", message)
        write_chat_history(context.tasks_dir)
        run_id = _current_run_id(context)
        if final and run_id:
            _set_final_sent(context, run_id)
        if str(context.role or "primary") == "interaction" and context.on_interaction_message_sent:
            try:
                context.on_interaction_message_sent()
            except Exception:
                pass
        _record_outbound_event(
            text=message,
            message_type="payment_link",
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            metadata={
                "final": final,
                "order_id": order_id,
                "source": source or None,
            },
        )
        payload = {"ok": True, "order_id": order_id, "sent": True}
        _log("send_payment_link", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "record_deploy",
        "Record a deployment URL for the tenant. Does not send messages.",
        {"deploy_url": str},
    )
    async def record_deploy(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        deploy_url = str(args.get("deploy_url", "")).strip()
        if not deploy_url:
            payload = {"ok": False, "error": "missing_deploy_url"}
            _log("record_deploy", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        try:
            (context.tasks_dir / "deploy_url.txt").write_text(deploy_url)
        except OSError:
            pass
        if context.db and context.tenant_id is not None:
            context.db.update_tenant_deploy_url(context.tenant_id, deploy_url)
        payload = {
            "ok": True,
            "deploy_url": deploy_url,
            "message": f"Your site is live: {deploy_url}",
        }
        _log("record_deploy", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "record_domain_quote",
        "Persist a domain quote and, if available, create a payment link. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "available": {"type": "boolean"},
                "price_usd": {"type": "number"},
                "currency": {"type": "string"},
                "message": {"type": "string"},
                "raw_output": {"type": "string"},
            },
            "required": ["domain", "available"],
        },
    )
    async def record_domain_quote(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        domain = str(args.get("domain", "")).strip().lower()
        available = bool(args.get("available", False))
        currency = str(args.get("currency") or "USD").upper()
        message = str(args.get("message") or "").strip()
        raw_output = str(args.get("raw_output") or "").strip()

        if not domain:
            payload = {"ok": False, "error": "missing_domain"}
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        price_usd = None
        if args.get("price_usd") is not None:
            try:
                price_usd = float(args.get("price_usd"))
            except (TypeError, ValueError):
                price_usd = None

        if context.db is None or context.tenant_id is None:
            fallback_payload = {
                "ok": True,
                "status": "pending_record",
                "domain": domain,
                "available": available,
                "price_usd": price_usd,
                "currency": currency,
                "message": message or None,
            }
            _persist_quote(context, "domain_quote", fallback_payload)
            _log("record_domain_quote", args, result=fallback_payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(fallback_payload)}],
                "is_error": False,
            }

        status = "quoted" if available else "unavailable"
        order_id = context.db.create_billing_order(
            tenant_id=context.tenant_id,
            order_type="domain",
            status=status,
            price_usd=price_usd,
            currency=currency,
            metadata={
                "domain": domain,
                "available": available,
                "price_usd": price_usd,
                "currency": currency,
                "message": message or None,
                "raw_output": raw_output or None,
            },
        )

        if not available:
            if not message:
                message = f"{domain} is not available right now."
            payload = {
                "ok": True,
                "status": "unavailable",
                "order_id": order_id,
                "available": False,
                "message": message,
            }
            _persist_quote(context, "domain_quote", payload)
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }
        if price_usd is None:
            if not message:
                message = f"{domain} is available, but I couldn't parse the price. Try again."
            payload = {
                "ok": True,
                "status": "price_unavailable",
                "order_id": order_id,
                "available": True,
                "message": message,
            }
            _persist_quote(context, "domain_quote", payload)
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        payments = _resolve_payments()
        if payments is None:
            payload = {
                "ok": True,
                "status": "payments_unavailable",
                "order_id": order_id,
                "available": True,
                "price_usd": price_usd,
                "currency": currency,
                "message": (
                    f"{domain} is available for ${price_usd:.2f}/yr. "
                    "Payments are not configured yet."
                ),
            }
            _persist_quote(context, "domain_quote", payload)
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        try:
            amount_cents = int(round(price_usd * 100))
        except (TypeError, ValueError):
            payload = {"ok": False, "error": "invalid_price"}
            _log("record_domain_quote", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        metadata = {
            "billing_order_id": str(order_id),
            "order_type": "domain",
            "domain": domain,
            "price_usd": f"{price_usd:.2f}",
            "currency": currency,
        }
        if context.tenant_id is not None:
            metadata["tenant_id"] = str(context.tenant_id)
        if context.tenant_key:
            metadata["tenant_key"] = context.tenant_key
        if context.provider:
            metadata["provider"] = context.provider
        if context.tenant_external_id:
            metadata["tenant_external_id"] = context.tenant_external_id
        try:
            session = await payments.create_checkout_session(
                amount_cents=amount_cents,
                currency=currency.lower(),
                product_name=f"Domain purchase: {domain}",
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            context.db.mark_billing_order_failed(order_id, f"stripe_error: {exc}")
            payload = {
                "ok": False,
                "status": "stripe_error",
                "order_id": order_id,
                "message": "I couldn't create the payment link right now. Try again later.",
            }
            _persist_quote(context, "domain_quote", payload)
            _log("record_domain_quote", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        context.db.update_billing_order_payment(
            order_id,
            stripe_session_id=session.session_id,
            stripe_payment_url=session.url,
        )
        payload = {
            "ok": True,
            "status": "payment_ready",
            "order_id": order_id,
            "available": True,
            "price_usd": price_usd,
            "currency": currency,
            "payment_url": session.url,
            "message": (
                f"{domain} is available for ${price_usd:.2f}/yr. "
                f"Pay here to proceed: {session.url}"
            ),
        }
        _persist_quote(context, "domain_quote", payload)
        _log("record_domain_quote", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "record_billing_status",
        "Update a billing order status with optional metadata. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "number"},
                "status": {"type": "string"},
                "error": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["order_id", "status"],
        },
    )
    async def record_billing_status(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if context.db is None:
            payload = {"ok": False, "status": "missing_db"}
            _log("record_billing_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        try:
            order_id = int(args.get("order_id"))
        except (TypeError, ValueError):
            payload = {"ok": False, "status": "invalid_order_id"}
            _log("record_billing_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        status = str(args.get("status") or "").strip()
        if not status:
            payload = {"ok": False, "status": "missing_status"}
            _log("record_billing_status", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        error = str(args.get("error") or "").strip() or None
        metadata = args.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            metadata = None
        context.db.update_billing_order_status(
            order_id=order_id,
            status=status,
            error=error,
            metadata=metadata,
        )
        payload = {"ok": True, "order_id": order_id, "status": status}
        _log("record_billing_status", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "request_backend_subscription",
        "Create a recurring payment link for managed backend features. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "price_usd": {"type": "number"},
                "currency": {"type": "string"},
                "interval": {"type": "string"},
                "product_name": {"type": "string"},
                "use_case": {"type": "string"},
            },
            "required": ["price_usd", "currency", "interval", "product_name"],
        },
    )
    async def request_backend_subscription(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        price_usd = float(args.get("price_usd") or 0)
        currency = str(args.get("currency") or "").strip().upper()
        interval = str(args.get("interval") or "").strip().lower()
        product_name = str(args.get("product_name") or "").strip()[:200]
        use_case = str(args.get("use_case") or "").strip()[:500]
        if price_usd <= 0 or not currency or not interval or not product_name:
            payload = {
                "ok": False,
                "status": "invalid_input",
                "message": "Missing or invalid pricing inputs.",
            }
            _log("request_backend_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        payments = _resolve_payments()
        if payments is None:
            payload = {
                "ok": True,
                "status": "payments_unavailable",
                "price_usd": price_usd,
                "currency": currency,
                "message": "Payments are not configured yet.",
            }
            _log("request_backend_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        order_id = None
        if context.db is not None and context.tenant_id is not None:
            order_id = context.db.create_billing_order(
                tenant_id=context.tenant_id,
                order_type="managed_backend",
                status="quoted",
                price_usd=price_usd,
                currency=currency,
                metadata={
                    "backend_type": "supabase",
                    "use_case": use_case or None,
                    "interval": interval,
                    "product_name": product_name,
                },
            )

        metadata = {
            "backend_type": "supabase",
            "price_usd": f"{price_usd:.2f}",
            "currency": currency,
            "interval": interval,
            "product_name": product_name,
            "order_type": "managed_backend",
        }
        if order_id is not None:
            metadata["billing_order_id"] = str(order_id)
        if context.tenant_id is not None:
            metadata["tenant_id"] = str(context.tenant_id)
        if context.tenant_key:
            metadata["tenant_key"] = context.tenant_key
        if context.provider:
            metadata["provider"] = context.provider
        if context.tenant_external_id:
            metadata["tenant_external_id"] = context.tenant_external_id
        if use_case:
            metadata["use_case"] = use_case

        try:
            amount_cents = int(round(price_usd * 100))
        except (TypeError, ValueError):
            payload = {"ok": False, "error": "invalid_price"}
            _log("request_backend_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        try:
            session = await payments.create_recurring_checkout_session(
                amount_cents=amount_cents,
                currency=currency.lower(),
                product_name=product_name,
                interval=interval,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            if context.db is not None and order_id is not None:
                context.db.mark_billing_order_failed(order_id, f"stripe_error: {exc}")
            payload = {
                "ok": False,
                "status": "stripe_error",
                "order_id": order_id,
                "message": "I couldn't create the payment link right now. Try again later.",
            }
            _log("request_backend_subscription", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        if context.db is not None and order_id is not None:
            context.db.update_billing_order_payment(
                order_id,
                stripe_session_id=session.session_id,
                stripe_payment_url=session.url,
            )

        payload = {
            "ok": True,
            "status": "payment_ready",
            "order_id": order_id,
            "price_usd": price_usd,
            "currency": currency,
            "payment_url": session.url,
        }
        _persist_quote(context, "backend_quote", payload)
        _log("request_backend_subscription", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "request_assistant_subscription",
        "Create a recurring payment link for the assistant subscription. Does not send messages.",
        {
            "type": "object",
            "properties": {
                "purpose": {"type": "string"},
                "purpose_label": {"type": "string"},
            },
        },
    )
    async def request_assistant_subscription(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        if context.db is None or context.tenant_id is None:
            payload = {"ok": False, "status": "missing_db"}
            _log("request_assistant_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        settings = Settings()
        price_id = str(settings.assistant_stripe_price_id or "").strip()
        price_usd = settings.assistant_price_usd
        currency = (settings.assistant_currency or "USD").upper()
        product_name = (settings.assistant_product_name or "Hire me").strip() or "Hire me"

        if not price_id and price_usd is None:
            payload = {
                "ok": True,
                "status": "pricing_missing",
                "message": "Assistant pricing is not configured.",
            }
            _log("request_assistant_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        payments = _resolve_payments()
        if payments is None:
            payload = {
                "ok": True,
                "status": "payments_unavailable",
                "price_usd": price_usd,
                "currency": currency,
                "message": "Payments are not configured yet.",
            }
            _log("request_assistant_subscription", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        purpose_label = str(args.get("purpose_label") or "").strip()[:160] or None
        normalized_purpose = _normalize_assistant_purpose(
            args.get("purpose") or purpose_label
        )
        order_type = _assistant_order_type(normalized_purpose)

        existing = context.db.get_latest_billing_order(context.tenant_id, order_type)
        if existing is not None:
            if hasattr(existing, "get"):
                status_raw = existing.get("status")
                payment_url_raw = existing.get("stripe_payment_url")
                price_usd_raw = existing.get("price_usd")
                currency_raw = existing.get("currency")
                order_id_raw = existing.get("id")
            else:
                status_raw = existing["status"]
                payment_url_raw = existing["stripe_payment_url"]
                price_usd_raw = existing["price_usd"]
                currency_raw = existing["currency"]
                order_id_raw = existing["id"]
            status = str(status_raw or "").strip().lower()
            payment_url = str(payment_url_raw or "").strip()
            if status in {"pending_payment", "quoted"} and payment_url:
                payload = {
                    "ok": True,
                    "status": "payment_ready",
                    "order_id": int(order_id_raw),
                    "price_usd": price_usd_raw,
                    "currency": currency_raw,
                    "payment_url": payment_url,
                }
                _persist_quote(context, "assistant_quote", payload)
                _log("request_assistant_subscription", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": False,
                }

        order_id = context.db.create_billing_order(
            tenant_id=context.tenant_id,
            order_type=order_type,
            status="quoted",
            price_usd=price_usd,
            currency=currency,
            metadata={
                "plan": "assistant_monthly",
                "price_id": price_id or None,
                "product_name": product_name,
                "purpose": normalized_purpose,
                "purpose_label": purpose_label,
            },
        )

        metadata = {
            "billing_order_id": str(order_id),
            "order_type": order_type,
            "plan": "assistant_monthly",
            "price_usd": f"{price_usd:.2f}" if price_usd is not None else "",
            "currency": currency,
            "purpose": normalized_purpose or "",
            "purpose_label": purpose_label or "",
            "product_name": product_name,
        }
        if context.tenant_id is not None:
            metadata["tenant_id"] = str(context.tenant_id)
        if context.tenant_key:
            metadata["tenant_key"] = context.tenant_key
        if context.provider:
            metadata["provider"] = context.provider
        if context.tenant_external_id:
            metadata["tenant_external_id"] = context.tenant_external_id

        try:
            if price_id:
                session = await payments.create_subscription_checkout_session_for_price(
                    price_id=price_id,
                    metadata=metadata,
                )
            else:
                amount_cents = int(round(float(price_usd or 0) * 100))
                session = await payments.create_recurring_checkout_session(
                    amount_cents=amount_cents,
                    currency=currency.lower(),
                    product_name=product_name,
                    interval="month",
                    metadata=metadata,
                )
        except Exception as exc:  # noqa: BLE001
            context.db.mark_billing_order_failed(order_id, f"stripe_error: {exc}")
            payload = {
                "ok": False,
                "status": "stripe_error",
                "order_id": order_id,
                "message": "I couldn't create the payment link right now. Try again later.",
            }
            _log(
                "request_assistant_subscription",
                args,
                result=payload,
                error=str(exc),
                start=start,
            )
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        context.db.update_billing_order_payment(
            order_id,
            stripe_session_id=session.session_id,
            stripe_payment_url=session.url,
        )

        payload = {
            "ok": True,
            "status": "payment_ready",
            "order_id": order_id,
            "price_usd": price_usd,
            "currency": currency,
            "payment_url": session.url,
        }
        _persist_quote(context, "assistant_quote", payload)
        _log("request_assistant_subscription", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    return [
        should_send_message,
        decide_project,
        check_for_status,
        find_execution_agent,
        stream_to_execution_agent,
        stop_execution_agent,
        send_message,
        send_payment_link,
        record_deploy,
        record_domain_quote,
        record_billing_status,
        request_backend_subscription,
        request_assistant_subscription,
    ]


def build_chat_server(context: ChatToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=CHAT_SERVER_NAME,
        version="1.0.0",
        tools=build_chat_tools(context),
    )


def _is_duplicate_message(text: str, tasks_dir: Path, max_entries: int = 12) -> bool:
    text = str(text or "").strip()
    if not text:
        return False

    def _norm(value: str) -> str:
        value = value.lower()
        value = re.sub(r"https?://\S+", "<url>", value)
        value = re.sub(r"[^a-z0-9_<>\s]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _words(value: str) -> set[str]:
        tokens = []
        for token in value.split():
            token = token.strip()
            if len(token) < 3:
                continue
            tokens.append(token)
        return set(tokens)

    def _similar(a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        if a in b or b in a:
            return True
        wa = _words(a)
        wb = _words(b)
        if not wa or not wb:
            return False
        union = wa | wb
        if not union:
            return False
        jaccard = len(wa & wb) / len(union)
        if jaccard >= 0.78:
            return True
        overlap = len(wa & wb) / max(len(wa), len(wb))
        return overlap >= 0.9

    normalized = _norm(text)
    if not normalized:
        return False

    log_path = tasks_dir / "chat_log.jsonl"
    if not log_path.exists():
        return False
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    now = datetime.now(tz=timezone.utc)
    window = timedelta(minutes=10)
    recent = lines[-max_entries:]
    for line in reversed(recent):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("tag") or "") != "assistant_message":
            continue
        ts_raw = payload.get("timestamp")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if now - ts > window:
                    break
            except Exception:
                pass
        previous = _norm(str(payload.get("payload") or ""))
        if _similar(normalized, previous):
            return True
    return False


def _project_name_from_tasks_dir(tasks_dir: Path) -> str | None:
    try:
        if tasks_dir.name == "tasks":
            return tasks_dir.parent.name
    except Exception:
        return None
    return None


def _should_send(text: str, context: ChatToolContext) -> tuple[bool, str]:
    if not text.strip():
        return False, "empty"
    # NOTE: intentionally not blocking sends after a run is finalized.
    # run_id = _current_run_id(context)
    # if run_id and _final_sent_for_run(context, run_id):
    #     return False, "final_already_sent"
    if _is_duplicate_message(text, context.tasks_dir):
        return False, "duplicate"
    return True, "ok"


def _current_run_id(context: ChatToolContext) -> int | None:
    if context.run_id is not None:
        try:
            return int(context.run_id)
        except (TypeError, ValueError):
            return None
    if context.db is None or context.tenant_id is None:
        return None
    project_name = _project_name_from_tasks_dir(context.tasks_dir)
    active = context.db.get_active_run(context.tenant_id, project_name)
    if not active:
        return None
    try:
        return int(active["run_id"])
    except (TypeError, ValueError, KeyError):
        return None


def _current_message(context: ChatToolContext) -> dict[str, Any] | None:
    row = _current_message_row(context)
    if not row:
        return None
    text = str(row.get("text") or "").strip()
    raw = _normalize_raw_message(row.get("raw_json"))
    if not text and isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
    if not text and isinstance(raw, dict) and raw.get("images"):
        text = "(attachment)"
    return {
        "provider_message_id": str(row.get("provider_message_id") or "").strip(),
        "text": text,
    }


def _current_message_row(context: ChatToolContext) -> dict[str, Any] | None:
    if context.db is None:
        return None
    message_id = context.message_id
    if message_id is None and context.run_id is not None:
        try:
            run_row = context.db.get_run(int(context.run_id))
        except Exception:
            run_row = None
        if run_row:
            message_id = run_row.get("message_id")
    if message_id is None:
        return None
    try:
        return context.db.get_message(int(message_id))
    except Exception:
        return None


def _normalize_raw_message(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def _reply_context_allowed(
    context: ChatToolContext,
    reply_to_message_id: str | None,
    reply_to_text: str | None,
) -> tuple[bool, str]:
    if not reply_to_message_id and not reply_to_text:
        return True, "no_reply_context"
    if context.db is None or context.tenant_id is None:
        return False, "missing_db"
    row = _current_message_row(context)
    if not row:
        return False, "message_context_missing"
    received_at = _parse_datetime_value(row.get("received_at"))
    if received_at is None:
        return False, "missing_received_at"
    last_seen = _latest_activity_timestamp(context)
    if last_seen is not None:
        idle_minutes = (datetime.now(tz=timezone.utc) - last_seen).total_seconds() / 60.0
        if idle_minutes >= 20:
            return True, "idle_gap"
    if _message_gap_exceeds(context, received_at, threshold=4):
        return True, "message_gap"
    return False, "recent_context"


def _parse_datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _latest_activity_timestamp(context: ChatToolContext) -> datetime | None:
    if context.db is None or context.tenant_id is None:
        return None
    latest_message = _parse_datetime_value(
        context.db.get_latest_message_received_at(context.tenant_id)
    )
    latest_event = _parse_datetime_value(
        context.db.get_latest_message_event_at(context.tenant_id)
    )
    candidates = [ts for ts in (latest_message, latest_event) if ts is not None]
    if not candidates:
        return None
    return max(candidates)


def _message_gap_exceeds(
    context: ChatToolContext,
    since: datetime,
    *,
    threshold: int,
) -> bool:
    if context.db is None or context.tenant_id is None:
        return False
    max_needed = threshold + 1
    since_iso = since.isoformat()
    inbound = context.db.list_messages_since(
        context.tenant_id, since_iso, limit=max_needed
    )
    if len(inbound) >= max_needed:
        return True
    remaining = max_needed - len(inbound)
    if remaining <= 0:
        return True
    outbound = context.db.list_message_events_since(
        context.tenant_id, since_iso, limit=remaining
    )
    return (len(inbound) + len(outbound)) > threshold


def _reply_to_matches(
    context: ChatToolContext,
    *,
    reply_to_message_id: str,
    reply_to_text: str,
) -> tuple[bool, str]:
    def _normalize(text: str) -> str:
        if not text:
            return ""
        normalized = (
            text.replace("\u2019", "'")
            .replace("\u2018", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    current = _current_message(context)
    if current is None:
        return False, "message_context_missing"
    current_id = current.get("provider_message_id") or ""
    current_text = str(current.get("text") or "").strip()
    if reply_to_message_id and reply_to_message_id != current_id:
        return False, "reply_to_message_id_mismatch"
    if reply_to_text and _normalize(reply_to_text) != _normalize(current_text):
        return False, "reply_to_text_mismatch"
    return True, "ok"


def _final_sent_for_run(context: ChatToolContext, run_id: int) -> bool:
    if context.db is None:
        return False
    try:
        return context.db.is_run_final_sent(int(run_id))
    except Exception:
        return False


def _set_final_sent(context: ChatToolContext, run_id: int) -> None:
    if context.db is None:
        return
    try:
        context.db.set_run_final_sent(int(run_id))
    except Exception:
        return


def _strip_urls(text: str) -> str:
    cleaned = re.sub(r"https?://\\S+", "", text)
    cleaned = re.sub(r"\\s{2,}", " ", cleaned)
    return cleaned.strip()


def _persist_quote(context: ChatToolContext, key: str, payload: dict[str, Any]) -> None:
    path = context.tasks_dir / f"{key}.json"
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass
    if context.db is None or context.tenant_id is None:
        return
    try:
        context.db.set_tenant_kv(context.tenant_id, "billing", key, payload)
    except Exception:
        return


def _load_payment_url(
    context: ChatToolContext,
    *,
    source: str | None = None,
    order_id: int | None = None,
) -> str | None:
    def _extract_payment_url(path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if order_id is not None:
            try:
                stored_id = int(payload.get("order_id"))
            except (TypeError, ValueError):
                stored_id = None
            if stored_id != order_id:
                return None
        url = str(payload.get("payment_url") or "").strip()
        return url or None

    run_id = _current_run_id(context)
    if run_id is not None:
        url = _extract_payment_url(context.tasks_dir / f"billing_status_{run_id}.json")
        if url:
            return url

    url = _extract_payment_url(context.tasks_dir / "billing_status.json")
    if url:
        return url

    candidates: list[Path] = []
    keys: list[str] = []
    if source == "backend":
        keys.append("backend_quote")
    elif source == "domain":
        keys.append("domain_quote")
    elif source == "assistant":
        keys.append("assistant_quote")
    else:
        keys.extend(["assistant_quote", "backend_quote", "domain_quote"])

    if context.db is not None and context.tenant_id is not None:
        try:
            for key in keys:
                payload = context.db.get_tenant_kv(context.tenant_id, "billing", key)
                if not isinstance(payload, dict):
                    continue
                if order_id is not None:
                    try:
                        stored_id = int(payload.get("order_id"))
                    except (TypeError, ValueError):
                        stored_id = None
                    if stored_id is not None and stored_id != order_id:
                        continue
                url = str(payload.get("payment_url") or "").strip()
                if url:
                    return url
        except Exception:
            pass

    if source == "backend":
        candidates.append(context.tasks_dir / "backend_quote.json")
    elif source == "domain":
        candidates.append(context.tasks_dir / "domain_quote.json")
    elif source == "assistant":
        candidates.append(context.tasks_dir / "assistant_quote.json")
    else:
        candidates.extend(
            [
                context.tasks_dir / "assistant_quote.json",
                context.tasks_dir / "backend_quote.json",
                context.tasks_dir / "domain_quote.json",
            ]
        )
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if order_id is not None:
            try:
                stored_id = int(payload.get("order_id"))
            except (TypeError, ValueError):
                stored_id = None
            if stored_id is not None and stored_id != order_id:
                continue
        url = str(payload.get("payment_url") or "").strip()
        if url:
            return url
    return None


def _tenant_root_from_tasks_dir(tasks_dir: Path) -> Path:
    project_root = tasks_dir.parent
    if project_root.parent.name == "projects":
        return project_root.parent.parent
    return project_root


def _set_active_project(tenant_root: Path, project_name: str) -> bool:
    projects_dir = Path(tenant_root) / "projects"
    try:
        projects_dir.mkdir(parents=True, exist_ok=True)
        active_path = projects_dir / "active.txt"
        active_path.write_text(project_name.strip() + "\n")
        return True
    except OSError:
        return False


def _migrate_current_task_context(tasks_dir: Path, target_tasks_dir: Path) -> bool:
    try:
        if tasks_dir.resolve() == target_tasks_dir.resolve():
            return False
    except OSError:
        return False
    try:
        target_tasks_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    patterns = [
        "latest.md",
        "task-*.md",
        "chat_log.jsonl",
        "chat_history.md",
        "chat_summary.md",
        "summary_prompt.md",
        "memory_prompt.md",
        "inflight_updates.jsonl",
        "inflight_consumed.jsonl",
        "interaction_updates.jsonl",
        "interaction_request.json",
        "run_result.json",
        "outbound_messages.jsonl",
        "tool_runs.jsonl",
        "agent_events.jsonl",
        "deploy_url.txt",
        "result_summary.md",
        "backend_quote.json",
        "domain_quote.json",
    ]
    moved = False
    for pattern in patterns:
        for path in tasks_dir.glob(pattern):
            try:
                destination = target_tasks_dir / path.name
                if destination.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                path.replace(destination)
                moved = True
            except OSError:
                continue
    return moved

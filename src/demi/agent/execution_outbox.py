from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FINAL_OUTBOX_CORRELATION_PREFIX = "execution-final"


def execution_final_correlation_id(run_id: int | None) -> str:
    if run_id is None:
        return f"{FINAL_OUTBOX_CORRELATION_PREFIX}:unknown"
    return f"{FINAL_OUTBOX_CORRELATION_PREFIX}:{int(run_id)}"


def _iter_recent_jsonl(path: Path, *, max_lines: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    recent = lines[-max(1, int(max_lines)) :]
    records: list[dict] = []
    for line in reversed(recent):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def tool_runs_show_final_interaction_update(
    tasks_dir: Path,
    *,
    correlation_id: str | None = None,
    max_lines: int = 250,
) -> bool:
    """True if tool_runs show a queued final interaction update."""

    path = tasks_dir / "tool_runs.jsonl"
    for record in _iter_recent_jsonl(path, max_lines=max_lines):
        tool = str(record.get("tool") or "").strip()
        if tool not in {"send_message", "send_payment_link"}:
            continue
        args = record.get("args")
        if not isinstance(args, dict) or not bool(args.get("final", False)):
            continue
        if correlation_id and str(args.get("correlation_id") or "").strip() != correlation_id:
            continue
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("queued") is True:
            return True
    return False


def interaction_update_file_shows_final(
    tasks_dir: Path,
    *,
    correlation_id: str | None = None,
    max_lines: int = 250,
) -> bool:
    """True if interaction_updates.jsonl contains a final payload (fallback path)."""

    path = tasks_dir / "interaction_updates.jsonl"
    for payload in _iter_recent_jsonl(path, max_lines=max_lines):
        if not isinstance(payload, dict):
            continue
        if str(payload.get("type") or "").strip().lower() != "interaction_update":
            continue
        if not bool(payload.get("final", False)):
            continue
        if correlation_id and str(payload.get("correlation_id") or "").strip() != correlation_id:
            continue
        action = str(payload.get("action") or "").strip().lower()
        if action in {"send_message", "send_payment_link"}:
            return True
    return False


def has_final_interaction_update(
    tasks_dir: Path,
    *,
    correlation_id: str | None = None,
) -> bool:
    return tool_runs_show_final_interaction_update(
        tasks_dir, correlation_id=correlation_id
    ) or interaction_update_file_shows_final(tasks_dir, correlation_id=correlation_id)


def ensure_execution_final_outbox(
    *,
    tasks_dir: Path,
    db: Any | None,
    tenant_id: int | None,
    run_id: int | None,
    project_name: str | None,
    summary_text: str | None,
    provider: str | None = None,
    tenant_external_id: str | None = None,
) -> bool:
    """Ensure a final interaction update exists for the given execution run.

    Returns:
        True if a final update was enqueued/written, False if one already exists.
    """

    correlation_id = execution_final_correlation_id(run_id)
    if has_final_interaction_update(tasks_dir, correlation_id=correlation_id):
        return False

    text = str(summary_text or "").strip() or "Execution finished."
    payload = {
        "type": "interaction_update",
        "action": "send_message",
        "text": text,
        "final": True,
        "correlation_id": correlation_id,
        "run_id": int(run_id) if run_id is not None else None,
        "project_name": project_name,
        "provider": provider,
        "tenant_external_id": tenant_external_id,
        "source": "execution_final_safety_net",
    }

    if db is not None and tenant_id is not None:
        try:
            db.enqueue_outbox(
                tenant_id=int(tenant_id),
                run_id=int(run_id) if run_id is not None else None,
                project_name=project_name,
                correlation_id=correlation_id,
                payload=payload,
            )
            return True
        except Exception:
            pass

    path = tasks_dir / "interaction_updates.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
        return True
    except OSError:
        return False

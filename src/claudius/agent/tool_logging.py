from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json


def log_tool_run(
    tasks_dir: Path,
    tool_name: str,
    *,
    args: dict[str, Any] | None = None,
    result: Any | None = None,
    error: str | None = None,
    duration_ms: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    record: dict[str, Any] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "tool": tool_name,
    }
    if args is not None:
        record["args"] = args
    if result is not None:
        record["result"] = result
    if error is not None:
        record["error"] = error
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 2)
    if extra:
        record.update(extra)

    path = tasks_dir / "tool_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")


def log_agent_event(tasks_dir: Path, event: str, data: dict[str, Any] | None = None) -> None:
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "event": event,
        "data": data or {},
    }
    path = tasks_dir / "agent_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")

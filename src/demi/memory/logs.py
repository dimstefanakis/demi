from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List
import json


@dataclass(frozen=True)
class LogEntry:
    index: int
    tag: str
    payload: str
    timestamp: str


def append_log(tasks_dir: Path, tag: str, payload: str, timestamp: datetime | None = None) -> LogEntry:
    log_path = tasks_dir / "chat_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    count = _count_lines(log_path)
    index = count + 1
    ts = (timestamp or datetime.now(tz=timezone.utc)).isoformat()
    entry = LogEntry(index=index, tag=tag, payload=payload, timestamp=ts)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.__dict__) + "\n")
    return entry


def read_logs(tasks_dir: Path) -> List[LogEntry]:
    log_path = tasks_dir / "chat_log.jsonl"
    if not log_path.exists():
        return []
    entries: List[LogEntry] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if not all(key in data for key in ("index", "tag", "payload", "timestamp")):
            continue
        entries.append(
            LogEntry(
                index=int(data["index"]),
                tag=str(data["tag"]),
                payload=str(data["payload"]),
                timestamp=str(data["timestamp"]),
            )
        )
    return entries


def rewrite_logs(tasks_dir: Path, entries: Iterable[LogEntry]) -> None:
    log_path = tasks_dir / "chat_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry.__dict__) + "\n")

def write_chat_history(tasks_dir: Path, last_n: int = 12) -> None:
    entries = read_logs(tasks_dir)
    history_path = tasks_dir / "chat_history.md"
    if not entries:
        history_path.write_text("# Recent Messages\n\n- No items.\n")
        return
    recent = entries[-last_n:]
    lines = ["# Recent Messages", ""]
    for entry in recent:
        lines.append(f"[{entry.timestamp}] {entry.tag}: {entry.payload}")
    history_path.write_text("\n".join(lines) + "\n")


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())

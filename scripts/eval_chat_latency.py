#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromtimestamp(0)


def load_entries(path: Path) -> list[dict]:
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if "timestamp" not in data:
            continue
        entries.append(data)
    entries.sort(key=lambda item: parse_ts(str(item.get("timestamp"))))
    return entries


def evaluate(entries: list[dict], threshold_seconds: int) -> list[dict]:
    results: list[dict] = []
    for idx, entry in enumerate(entries):
        if entry.get("tag") != "user_message":
            continue
        user_ts = parse_ts(str(entry.get("timestamp")))
        user_text = str(entry.get("payload", ""))
        assistant_ts = None
        assistant_text = None
        for follow in entries[idx + 1 :]:
            if follow.get("tag") == "assistant_message":
                assistant_ts = parse_ts(str(follow.get("timestamp")))
                assistant_text = str(follow.get("payload", ""))
                break
            if follow.get("tag") == "user_message":
                break
        if assistant_ts is None:
            results.append(
                {
                    "status": "missing",
                    "user_text": user_text,
                    "user_timestamp": entry.get("timestamp"),
                }
            )
            continue
        delta = (assistant_ts - user_ts).total_seconds()
        status = "ok" if delta <= threshold_seconds else "slow"
        results.append(
            {
                "status": status,
                "delta_seconds": round(delta, 2),
                "user_text": user_text,
                "assistant_text": assistant_text,
                "user_timestamp": entry.get("timestamp"),
                "assistant_timestamp": follow.get("timestamp"),
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate response latency in chat logs.")
    parser.add_argument("--tasks-dir", type=Path, help="Path to tasks directory")
    parser.add_argument("--chat-log", type=Path, help="Path to chat_log.jsonl")
    parser.add_argument("--threshold", type=int, default=30, help="Seconds to consider slow")
    args = parser.parse_args()

    if args.chat_log:
        chat_log = args.chat_log
    elif args.tasks_dir:
        chat_log = args.tasks_dir / "chat_log.jsonl"
    else:
        parser.error("Provide --tasks-dir or --chat-log")
        return

    if not chat_log.exists():
        raise SystemExit(f"chat log not found: {chat_log}")

    entries = load_entries(chat_log)
    results = evaluate(entries, args.threshold)

    slow = [r for r in results if r.get("status") == "slow"]
    missing = [r for r in results if r.get("status") == "missing"]

    print(f"Total user messages: {len(results)}")
    print(f"Slow responses (> {args.threshold}s): {len(slow)}")
    print(f"Missing responses: {len(missing)}")

    for item in slow:
        print(
            f"SLOW {item['delta_seconds']}s | {item['user_text']} -> {item['assistant_text']}"
        )
    for item in missing:
        print(f"MISSING | {item['user_text']}")


if __name__ == "__main__":
    main()

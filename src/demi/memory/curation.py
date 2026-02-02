from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent
from typing import Dict, List

from demi.memory.logs import LogEntry


@dataclass(frozen=True)
class MemoryPrompt:
    system_prompt: str
    messages: List[Dict[str, str]]


_SYSTEM_PROMPT = dedent(
    """
    You are the assistant's memory curator. Update memory.md to reflect stable,
    long-term context about the tenant, including core interactions, preferences,
    and decisions that should persist across sessions.

    FORMAT - Preserve existing headings if present. If the file is empty or
    unstructured, use this default format:

    # Memory

    ## Business
    - <facts>

    ## Brand & Voice
    - <facts>

    ## Decisions & Preferences
    - <facts>

    ## Links & Assets
    - <facts>

    ## Notes
    - <facts>

    If a section has no content, output a single bullet "- None."

    RULES - Obey all of these:
    1. Rewrite the entire memory from scratch using existing memory + logs.
    2. Keep only stable facts, preferences, and decisions. Exclude transient tasks.
    3. Merge new info and remove items that are obsolete or contradicted.
    4. Do not invent details; omit anything uncertain.
    5. Keep it concise (<= 200 lines).
    """
).strip()


def _format_existing_memory(previous_memory: str) -> str:
    memory = (previous_memory or "").strip()
    return memory if memory else "None"


def _format_log_entries(entries: List[LogEntry]) -> str:
    lines: List[str] = []
    for entry in entries:
        label = entry.tag.replace("_", " ")
        payload = entry.payload.strip()
        index = entry.index if entry.index >= 0 else "?"
        timestamp = entry.timestamp
        if payload:
            lines.append(f"[{index} {timestamp}] {label}: {payload}")
        else:
            lines.append(f"[{index} {timestamp}] {label}: (empty)")
    return "\n".join(lines) if lines else "(no new logs)"


def build_memory_prompt(previous_memory: str, entries: List[LogEntry]) -> MemoryPrompt:
    content = dedent(
        f"""
        Existing memory:
        {_format_existing_memory(previous_memory)}

        Recent conversation logs to merge:
        {_format_log_entries(entries)}
        """
    ).strip()

    messages = [{"role": "user", "content": content}]
    return MemoryPrompt(system_prompt=_SYSTEM_PROMPT, messages=messages)


__all__ = ["MemoryPrompt", "build_memory_prompt"]

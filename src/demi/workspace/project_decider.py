from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

from demi.workspace.core import PROJECTS_DIRNAME, ACTIVE_PROJECT_FILE


STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "into",
    "your",
    "you",
    "our",
    "their",
    "they",
    "them",
    "then",
    "than",
    "when",
    "where",
    "what",
    "which",
    "who",
    "how",
    "was",
    "were",
    "are",
    "is",
    "be",
    "to",
    "of",
    "in",
    "on",
    "at",
    "as",
    "an",
    "a",
}


@dataclass(frozen=True)
class ProjectDecision:
    project_name: str
    confidence: float
    reason: str
    scores: dict[str, int]


@dataclass(frozen=True)
class ProjectCandidate:
    name: str
    description: str
    memory: str
    chat_summary: str
    chat_history: str

    @property
    def context(self) -> str:
        parts = [self.name, self.description, self.memory, self.chat_summary, self.chat_history]
        return "\n".join([part for part in parts if part])


def decide_project(
    tenant_root: Path,
    message_text: str | None,
    payload: dict[str, Any] | None = None,
    max_bytes: int = 4000,
) -> ProjectDecision | None:
    text = (message_text or "").strip()
    if not text:
        return None
    tenant_root = Path(tenant_root)
    projects_dir = tenant_root / PROJECTS_DIRNAME
    if not projects_dir.exists():
        return None
    candidates = _load_candidates(projects_dir, max_bytes=max_bytes)
    if not candidates:
        return None

    active = _read_active_project(projects_dir)
    text_tokens = _tokenize(text)
    if payload:
        payload_text = _flatten_payload(payload, max_bytes=max_bytes)
        if payload_text:
            text_tokens |= _tokenize(payload_text)

    scores: dict[str, int] = {}
    matches: dict[str, set[str]] = {}
    for candidate in candidates:
        score, matched = _score_candidate(text, text_tokens, candidate)
        if active and candidate.name == active:
            score += 1
        scores[candidate.name] = score
        matches[candidate.name] = matched

    top = max(scores.items(), key=lambda item: item[1])
    top_name, top_score = top
    if top_score <= 0:
        return None
    sorted_scores = sorted(scores.values(), reverse=True)
    second = sorted_scores[1] if len(sorted_scores) > 1 else 0
    confidence = top_score / max(1, top_score + second)
    matched_tokens = sorted(matches.get(top_name, set()))
    reason = "matched: " + ", ".join(matched_tokens[:6]) if matched_tokens else "best match"
    return ProjectDecision(
        project_name=top_name,
        confidence=round(confidence, 3),
        reason=reason,
        scores=scores,
    )


def _load_candidates(projects_dir: Path, max_bytes: int) -> list[ProjectCandidate]:
    candidates: list[ProjectCandidate] = []
    for child in projects_dir.iterdir():
        if not child.is_dir():
            continue
        description = _read_text(child / "DESCRIPTION.md", max_bytes=max_bytes)
        memory = _read_text(child / "memory.md", max_bytes=max_bytes)
        chat_summary = _read_text(child / "tasks" / "chat_summary.md", max_bytes=max_bytes)
        chat_history = _read_text(child / "tasks" / "chat_history.md", max_bytes=max_bytes)
        candidates.append(
            ProjectCandidate(
                name=child.name,
                description=description,
                memory=memory,
                chat_summary=chat_summary,
                chat_history=chat_history,
            )
        )
    return candidates


def _read_text(path: Path, max_bytes: int) -> str:
    try:
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return data[:max_bytes]


def _read_active_project(projects_dir: Path) -> str | None:
    path = Path(projects_dir) / ACTIVE_PROJECT_FILE
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _flatten_payload(payload: dict[str, Any], max_bytes: int) -> str:
    try:
        import json

        raw = json.dumps(payload)
    except Exception:  # noqa: BLE001
        return ""
    return raw[:max_bytes]


def _score_candidate(
    text: str, text_tokens: set[str], candidate: ProjectCandidate
) -> tuple[int, set[str]]:
    name_tokens = _tokenize(candidate.name.replace("-", " ").replace("_", " "))
    context_tokens = _tokenize(candidate.context)
    score = 0
    lowered = text.lower()
    matched: set[str] = set()

    if candidate.name and candidate.name.lower() in lowered:
        score += 8
    name_overlap = text_tokens & name_tokens
    if name_overlap:
        score += 4 * len(name_overlap)
        matched |= name_overlap
    context_overlap = text_tokens & context_tokens
    if context_overlap:
        score += 2 * len(context_overlap)
        matched |= context_overlap
    return score, matched


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in STOP_WORDS
    }

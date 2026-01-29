from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventSignature:
    ok: bool
    reason: str | None = None


def verify_signature(secret: str | None, body: bytes, signature: str | None) -> EventSignature:
    if not secret:
        return EventSignature(ok=True)
    if not signature:
        return EventSignature(ok=False, reason="missing_signature")
    raw = signature.strip()
    if raw.startswith("sha256="):
        raw = raw.split("=", 1)[1]
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(digest, raw):
        return EventSignature(ok=True)
    return EventSignature(ok=False, reason="invalid_signature")


def normalize_event_type(payload: dict[str, Any]) -> str:
    for key in ("type", "event", "name", "action"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "event"

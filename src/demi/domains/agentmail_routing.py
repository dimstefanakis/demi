from __future__ import annotations

from typing import Any, Callable, Iterable


def is_agentmail_inbound_message_event(event_type: Any) -> bool:
    """Return true when the webhook event represents an inbound email message."""
    normalized = str(event_type or "").strip().lower()
    return normalized == "message.received"


def hydrate_agentmail_tenant_from_pod(
    *,
    db: Any,
    pod_id: Any,
    thread_id: Any,
    tenants: Iterable[Any],
    ensure_pod_sync: Callable[[Any], dict[str, Any] | None] | None = None,
) -> Any | None:
    """Resolve tenant via pod_id, optionally bootstrapping pod mappings first."""
    normalized_pod_id = str(pod_id or "").strip()
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_pod_id:
        return None

    tenant = db.get_tenant_by_agentmail_pod(normalized_pod_id)
    if tenant is None and ensure_pod_sync is not None:
        for candidate in tenants:
            try:
                ensure_pod_sync(candidate)
            except Exception:
                continue
            tenant = db.get_tenant_by_agentmail_pod(normalized_pod_id)
            if tenant is not None:
                break
    if tenant is None:
        return None

    if normalized_thread_id:
        try:
            db.set_agentmail_thread_tenant(normalized_thread_id, int(tenant.id))
        except Exception:
            pass
    return tenant


def resolve_agentmail_tenant_and_thread(*, db: Any, message: Any) -> tuple[Any | None, str]:
    """Resolve AgentMail tenant by thread mapping with pod-based fallback/backfill."""
    message_payload = message if isinstance(message, dict) else {}
    thread_id = str(message_payload.get("thread_id") or "").strip()
    if not thread_id:
        return None, ""
    tenant = db.get_tenant_by_agentmail_thread(thread_id)
    if tenant is not None:
        return tenant, thread_id
    pod_id = str(message_payload.get("pod_id") or "").strip()
    if not pod_id:
        return None, thread_id
    tenant = db.get_tenant_by_agentmail_pod(pod_id)
    if tenant is None:
        return None, thread_id
    db.set_agentmail_thread_tenant(thread_id, int(tenant.id))
    return tenant, thread_id

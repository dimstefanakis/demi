from __future__ import annotations

from dataclasses import dataclass

from demi.domains.agentmail_routing import (
    hydrate_agentmail_tenant_from_pod,
    is_agentmail_inbound_message_event,
    resolve_agentmail_tenant_and_thread,
)


@dataclass
class _Tenant:
    id: int


class _FakeDB:
    def __init__(self) -> None:
        self.by_thread: dict[str, _Tenant] = {}
        self.by_pod: dict[str, _Tenant] = {}
        self.set_calls: list[tuple[str, int]] = []

    def get_tenant_by_agentmail_thread(self, thread_id: str):
        return self.by_thread.get(thread_id)

    def get_tenant_by_agentmail_pod(self, pod_id: str):
        return self.by_pod.get(pod_id)

    def set_agentmail_thread_tenant(self, thread_id: str, tenant_id: int) -> None:
        self.set_calls.append((thread_id, tenant_id))


def test_resolve_agentmail_tenant_prefers_existing_thread_mapping():
    db = _FakeDB()
    tenant = _Tenant(id=7)
    db.by_thread["thread-1"] = tenant
    db.by_pod["pod-1"] = _Tenant(id=99)

    resolved, thread_id = resolve_agentmail_tenant_and_thread(
        db=db,
        message={"thread_id": "thread-1", "pod_id": "pod-1"},
    )

    assert thread_id == "thread-1"
    assert resolved is tenant
    assert db.set_calls == []


def test_resolve_agentmail_tenant_backfills_thread_mapping_from_pod():
    db = _FakeDB()
    tenant = _Tenant(id=11)
    db.by_pod["pod-11"] = tenant

    resolved, thread_id = resolve_agentmail_tenant_and_thread(
        db=db,
        message={"thread_id": "thread-11", "pod_id": "pod-11"},
    )

    assert thread_id == "thread-11"
    assert resolved is tenant
    assert db.set_calls == [("thread-11", 11)]


def test_resolve_agentmail_tenant_returns_none_without_thread_or_mapping():
    db = _FakeDB()

    resolved_missing_thread, missing_thread_id = resolve_agentmail_tenant_and_thread(
        db=db,
        message={"pod_id": "pod-1"},
    )
    resolved_missing_mapping, mapped_thread_id = resolve_agentmail_tenant_and_thread(
        db=db,
        message={"thread_id": "thread-x"},
    )

    assert missing_thread_id == ""
    assert resolved_missing_thread is None
    assert mapped_thread_id == "thread-x"
    assert resolved_missing_mapping is None
    assert db.set_calls == []


def test_agentmail_inbound_event_filter_only_accepts_message_received():
    assert is_agentmail_inbound_message_event("message.received") is True
    assert is_agentmail_inbound_message_event("MESSAGE.RECEIVED") is True
    assert is_agentmail_inbound_message_event("message.sent") is False
    assert is_agentmail_inbound_message_event("message.delivered") is False
    assert is_agentmail_inbound_message_event("message.bounced") is False


def test_hydrate_agentmail_tenant_from_pod_bootstraps_missing_mapping():
    db = _FakeDB()
    tenant = _Tenant(id=13)
    ensure_calls: list[int] = []

    def _ensure_pod_sync(candidate):
        ensure_calls.append(int(candidate.id))
        db.by_pod["pod-13"] = candidate

    resolved = hydrate_agentmail_tenant_from_pod(
        db=db,
        pod_id="pod-13",
        thread_id="thread-13",
        tenants=[tenant],
        ensure_pod_sync=_ensure_pod_sync,
    )

    assert resolved is tenant
    assert ensure_calls == [13]
    assert db.set_calls == [("thread-13", 13)]

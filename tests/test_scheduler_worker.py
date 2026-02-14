from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from demi.jobs.scheduler_worker import SchedulerWorker, SchedulerWorkerConfig


@dataclass
class _Tenant:
    id: int


class _FakeDB:
    def __init__(self) -> None:
        self.tenants = [_Tenant(id=1)]
        self.kv: dict[tuple[int, str, str], dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.event_jobs: list[dict[str, Any]] = []
        self._job_id = 1

    def list_tenants(self):
        return self.tenants

    def list_tenant_kv_namespace(
        self,
        tenant_id: int,
        namespace: str,
        key_prefix: str | None = None,
        limit: int = 100,
    ):
        rows: list[dict[str, Any]] = []
        for (row_tenant, row_namespace, row_key), value in sorted(self.kv.items()):
            if row_tenant != tenant_id or row_namespace != namespace:
                continue
            if key_prefix and not row_key.startswith(key_prefix):
                continue
            rows.append({"key": row_key, "value_json": value, "updated_at": value.get("updated_at")})
            if len(rows) >= limit:
                break
        return rows

    def set_tenant_kv(self, tenant_id: int, namespace: str, key: str, value: dict[str, Any] | None) -> None:
        if value is None:
            self.kv.pop((tenant_id, namespace, key), None)
            return
        self.kv[(tenant_id, namespace, key)] = dict(value)

    def get_tenant_kv(self, tenant_id: int, namespace: str, key: str) -> dict[str, Any] | None:
        payload = self.kv.get((tenant_id, namespace, key))
        return dict(payload) if isinstance(payload, dict) else None

    def list_tenant_events(
        self,
        tenant_id: int,
        event_type: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ):
        rows: list[dict[str, Any]] = []
        for row in self.events:
            if int(row["tenant_id"]) != int(tenant_id):
                continue
            if event_type and row.get("event_type") != event_type:
                continue
            if after_id is not None and int(row["id"]) <= int(after_id):
                continue
            rows.append(dict(row))
            if len(rows) >= limit:
                break
        return rows

    def create_event_job(
        self,
        tenant_id: int,
        job_type: str,
        payload: dict[str, Any],
        run_after: str | None = None,
    ) -> int:
        row = {
            "id": self._job_id,
            "tenant_id": tenant_id,
            "job_type": job_type,
            "payload_json": payload,
            "run_after": run_after,
        }
        self.event_jobs.append(row)
        self._job_id += 1
        return int(row["id"])


def _run(worker: SchedulerWorker) -> int:
    return asyncio.run(worker.process_once())


def test_scheduler_worker_fires_due_cron_trigger():
    db = _FakeDB()
    now = datetime.now(tz=timezone.utc)
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:cron-a",
        {
            "id": "cron-a",
            "name": "hourly",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "* * * * *",
            "state": {"next_run_at": (now - timedelta(minutes=1)).isoformat()},
            "payload": {"summary": "tick"},
        },
    )
    worker = SchedulerWorker(db=db, config=SchedulerWorkerConfig(poll_interval=0.01, batch_size=20))

    fired = _run(worker)

    assert fired == 1
    assert len(db.event_jobs) == 1
    payload = db.event_jobs[0]["payload_json"]
    assert payload["event_type"] == "scheduled_trigger"
    trigger_state = db.get_tenant_kv(1, "scheduler", "trigger:cron-a")["state"]
    assert trigger_state.get("next_run_at")


def test_scheduler_worker_webhook_condition_matches_events():
    db = _FakeDB()
    now = datetime.now(tz=timezone.utc).isoformat()
    db.events = [
        {"id": 1, "tenant_id": 1, "event_type": "lead", "payload_json": {"plan": "basic"}, "received_at": now},
        {"id": 2, "tenant_id": 1, "event_type": "lead", "payload_json": {"plan": "pro"}, "received_at": now},
    ]
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:webhook-a",
        {
            "id": "webhook-a",
            "enabled": True,
            "trigger_type": "webhook_condition",
            "event_type": "lead",
            "condition": {"path": "plan", "equals": "pro"},
            "state": {"last_event_id": 0},
            "payload": {"summary": "lead matched"},
        },
    )
    worker = SchedulerWorker(db=db, config=SchedulerWorkerConfig(poll_interval=0.01, batch_size=20))

    fired = _run(worker)

    assert fired == 1
    assert len(db.event_jobs) == 1
    state = db.get_tenant_kv(1, "scheduler", "trigger:webhook-a")["state"]
    assert state["last_event_id"] == 2
    assert state["last_match_event_id"] == 2


def test_scheduler_worker_state_change_fires_on_change_only():
    db = _FakeDB()
    db.set_tenant_kv(1, "metrics", "snapshot", {"value": 1})
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:state-a",
        {
            "id": "state-a",
            "enabled": True,
            "trigger_type": "state_change",
            "watch": {"namespace": "metrics", "key": "snapshot", "path": "value"},
            "state": {},
            "payload": {"summary": "state changed"},
        },
    )
    worker = SchedulerWorker(db=db, config=SchedulerWorkerConfig(poll_interval=0.01, batch_size=20))

    first_fired = _run(worker)
    db.set_tenant_kv(1, "metrics", "snapshot", {"value": 2})
    second_fired = _run(worker)

    assert first_fired == 0
    assert second_fired == 1
    assert len(db.event_jobs) == 1


def test_scheduler_worker_applies_time_window_to_run_after():
    db = _FakeDB()
    now = datetime.now(tz=timezone.utc)
    future = now + timedelta(hours=1)
    window = future.strftime("%H:%M")
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:window-a",
        {
            "id": "window-a",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "* * * * *",
            "time_window": {"start": window, "end": window, "timezone": "UTC"},
            "state": {"next_run_at": (now - timedelta(minutes=1)).isoformat()},
        },
    )
    worker = SchedulerWorker(db=db, config=SchedulerWorkerConfig(poll_interval=0.01, batch_size=20))

    fired = _run(worker)
    run_after = datetime.fromisoformat(str(db.event_jobs[0]["run_after"]))

    assert fired == 1
    assert run_after > now


def test_scheduler_worker_routes_pm_trigger_to_pm_job_type():
    db = _FakeDB()
    now = datetime.now(tz=timezone.utc)
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:pm-a",
        {
            "id": "pm-a",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "* * * * *",
            "output_event_type": "pm_trigger",
            "state": {"next_run_at": (now - timedelta(minutes=1)).isoformat()},
            "payload": {"trigger": "daily_heartbeat"},
        },
    )
    worker = SchedulerWorker(db=db, config=SchedulerWorkerConfig(poll_interval=0.01, batch_size=20))

    fired = _run(worker)

    assert fired == 1
    assert len(db.event_jobs) == 1
    assert db.event_jobs[0]["job_type"] == "pm_trigger"


def test_scheduler_worker_skips_pm_trigger_when_pm_worker_disabled():
    db = _FakeDB()
    now = datetime.now(tz=timezone.utc)
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:pm-disabled",
        {
            "id": "pm-disabled",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "* * * * *",
            "output_event_type": "pm_trigger",
            "state": {"next_run_at": (now - timedelta(minutes=1)).isoformat()},
            "payload": {"trigger": "daily_heartbeat"},
        },
    )
    worker = SchedulerWorker(
        db=db,
        config=SchedulerWorkerConfig(
            poll_interval=0.01,
            batch_size=20,
            pm_worker_enabled=False,
        ),
    )

    fired = _run(worker)

    assert fired == 1
    assert len(db.event_jobs) == 0

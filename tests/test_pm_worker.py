from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from demi.jobs.pm_worker import PMWorker, PMWorkerConfig
from demi.models import NormalizedMessage
from demi.pm.constants import PM_LAST_HEARTBEAT_KEY, PM_NAMESPACE
from demi.workspace.core import WorkspaceManager


@dataclass
class _Tenant:
    id: int
    key: str
    provider: str
    external_id: str
    last_deploy_url: str | None = None
    workspace_path: str | None = None


class _FakeDB:
    def __init__(self) -> None:
        self.tenants: dict[int, _Tenant] = {
            1: _Tenant(
                id=1,
                key="telegram:tenant-1",
                provider="telegram",
                external_id="tenant-1",
                last_deploy_url="https://example.vercel.app",
            )
        }
        self.kv: dict[tuple[int, str, str], dict[str, Any]] = {}
        self.event_jobs: list[dict[str, Any]] = []
        self.outbox_rows: list[dict[str, Any]] = []
        self.stale_received: list[dict[str, Any]] = []
        self.last_stale_received_query: dict[str, Any] | None = None
        self.inflight_runs: list[dict[str, Any]] = []
        self.run_inputs: list[dict[str, Any]] = []
        self.outbox_status_updates: list[tuple[str, str, str | None, float | None]] = []

    def fetch_pending_event_jobs(
        self,
        limit: int = 25,
        job_type_filter: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = [row for row in self.event_jobs if row.get("status") == "pending"]
        if isinstance(job_type_filter, str) and job_type_filter:
            rows = [row for row in rows if str(row.get("job_type")) == job_type_filter]
        return [dict(row) for row in rows[:limit]]

    def mark_event_job_running(self, job_id: int) -> None:
        for row in self.event_jobs:
            if int(row["id"]) == int(job_id):
                row["status"] = "running"
                return

    def mark_event_job_done(self, job_id: int) -> None:
        for row in self.event_jobs:
            if int(row["id"]) == int(job_id):
                row["status"] = "completed"
                return

    def mark_event_job_failed(
        self,
        job_id: int,
        error: str,
        retry_after_seconds: int | None = None,
        max_attempts: int = 5,
    ) -> None:
        del retry_after_seconds, max_attempts
        for row in self.event_jobs:
            if int(row["id"]) == int(job_id):
                row["status"] = "failed"
                row["last_error"] = error
                return

    def get_tenant_by_id(self, tenant_id: int) -> _Tenant | None:
        return self.tenants.get(int(tenant_id))

    def set_tenant_kv(self, tenant_id: int, namespace: str, key: str, value: dict[str, Any] | None) -> None:
        lookup = (int(tenant_id), namespace, key)
        if value is None:
            self.kv.pop(lookup, None)
            return
        self.kv[lookup] = dict(value)

    def get_tenant_kv(self, tenant_id: int, namespace: str, key: str) -> dict[str, Any] | None:
        value = self.kv.get((int(tenant_id), namespace, key))
        return dict(value) if isinstance(value, dict) else None

    def list_outbox(
        self,
        tenant_id: int | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.outbox_rows)
        if tenant_id is not None:
            rows = [row for row in rows if int(row["tenant_id"]) == int(tenant_id)]
        if status:
            rows = [row for row in rows if str(row.get("status")) == status]
        if limit is not None:
            rows = rows[: int(limit)]
        return [dict(row) for row in rows]

    def update_outbox_status(
        self,
        outbox_id: str,
        status: str,
        error: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.outbox_status_updates.append((str(outbox_id), str(status), error, retry_after_seconds))
        for row in self.outbox_rows:
            if str(row["id"]) == str(outbox_id):
                row["status"] = status
                row["last_error"] = error
                return

    def fetch_stale_received_messages(
        self,
        *,
        tenant_id: int | None = None,
        before: datetime,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        del before
        self.last_stale_received_query = {"tenant_id": tenant_id, "limit": int(limit)}
        rows = list(self.stale_received)
        if tenant_id is not None:
            rows = [row for row in rows if int(row.get("tenant_id", -1)) == int(tenant_id)]
        return [dict(row) for row in rows[:limit]]

    def list_tenant_inflight_runs(self, tenant_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        del tenant_id
        return [dict(row) for row in self.inflight_runs[:limit]]

    def fetch_run_inputs(
        self,
        tenant_id: int,
        project_name: str | None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        del tenant_id, project_name
        rows = list(self.run_inputs)
        if status:
            rows = [row for row in rows if str(row.get("status")) == status]
        if limit is not None:
            rows = rows[: int(limit)]
        return [dict(row) for row in rows]


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.dispatched_jobs: list[tuple[int, dict[str, Any], int]] = []
        self.dispatch_status = "accepted"
        self.drain_calls = 0
        self.last_drain_max_inputs: int | None = None
        self.finalize_calls: list[dict[str, Any]] = []

    @staticmethod
    def _is_run_stale(row: dict[str, Any], max_age_seconds: int = 900) -> bool:
        del max_age_seconds
        return bool(row.get("stale"))

    @staticmethod
    def _message_from_message_row(_tenant: Any, _row: Any):
        return None

    async def handle_event_job(self, tenant_id: int, payload: dict[str, Any], job_id: int):
        self.dispatched_jobs.append((int(tenant_id), dict(payload), int(job_id)))
        return SimpleNamespace(status=self.dispatch_status)

    async def handle_message(self, *_args, **_kwargs):
        return SimpleNamespace(status="accepted")

    async def _finalize_stale_run(self, _tenant: Any, row: dict[str, Any], *, notify: bool = True):
        self.finalize_calls.append({"run_id": row.get("id"), "notify": bool(notify)})
        row["finalized"] = True

    async def _drain_run_inputs(self, _tenant: Any, *, max_inputs: int | None = None):
        self.drain_calls += 1
        self.last_drain_max_inputs = max_inputs
        return int(max_inputs or 0)


def _run(worker: PMWorker) -> bool:
    return asyncio.run(worker.process_once())


def _build_worker(tmp_path: Path, db: _FakeDB, orchestrator: _FakeOrchestrator) -> PMWorker:
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    return PMWorker(
        db=db,
        orchestrator=orchestrator,
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
        ),
    )


def test_pm_worker_idle_trigger_dispatches_lead_pm_run(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.event_jobs.append(
        {
            "id": 1,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_idle_check",
                "payload": {"trigger": "idle_check"},
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert len(orchestrator.dispatched_jobs) == 1
    _, dispatched, _ = orchestrator.dispatched_jobs[0]
    assert dispatched["role"] == "lead_project_manager"
    assert dispatched["execution_context"] == "Lead Project Manager"
    assert dispatched["payload"]["trigger"] == "idle_check"
    assert "CONTEXT MANAGEMENT RUN" in str(dispatched["payload"].get("summary"))
    heartbeat = db.get_tenant_kv(1, PM_NAMESPACE, PM_LAST_HEARTBEAT_KEY)
    assert heartbeat is not None
    assert heartbeat.get("trigger") == "idle_check"


def test_pm_worker_project_manager_trigger_dispatches_project_role(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.event_jobs.append(
        {
            "id": 2,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_post_execution_update",
                "role": "project_manager",
                "execution_context": "Restaurant Booking Site",
                "payload": {
                    "trigger": "post_execution_update",
                    "summary": "Updated booking flow and deployed successfully.",
                },
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert len(orchestrator.dispatched_jobs) == 1
    _, dispatched, _ = orchestrator.dispatched_jobs[0]
    assert dispatched["role"] == "project_manager"
    assert dispatched["execution_context"] == "Restaurant Booking Site"
    assert dispatched["payload"]["summary"] == "Updated booking flow and deployed successfully."


def test_pm_worker_skips_duplicate_lead_dispatch_for_run_completed(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.event_jobs.append(
        {
            "id": 22,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_post_run_completed",
                "payload": {
                    "trigger": "run_completed",
                    "_scheduler": {
                        "source_payload": {
                            "event_type": "run_completed",
                            "run_id": 2001,
                            "run_role": "execution",
                            "execution_context": "Main project",
                        }
                    },
                },
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert orchestrator.dispatched_jobs == []
    heartbeat = db.get_tenant_kv(1, PM_NAMESPACE, PM_LAST_HEARTBEAT_KEY)
    assert heartbeat is not None
    assert heartbeat.get("trigger") == "run_completed"
    assert heartbeat.get("skipped") == "prompt_first_handoff"


def test_pm_worker_skips_duplicate_lead_dispatch_for_lead_pm_run_completed(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.event_jobs.append(
        {
            "id": 23,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_post_run_completed",
                "payload": {
                    "trigger": "run_completed",
                    "_scheduler": {
                        "source_payload": {
                            "event_type": "run_completed",
                            "run_id": 2002,
                            "run_role": "lead_project_manager",
                            "execution_context": "Lead Project Manager",
                        }
                    },
                },
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert orchestrator.dispatched_jobs == []
    heartbeat = db.get_tenant_kv(1, PM_NAMESPACE, PM_LAST_HEARTBEAT_KEY)
    assert heartbeat is not None
    assert heartbeat.get("trigger") == "run_completed"
    assert heartbeat.get("skipped") == "prompt_first_handoff"


def test_pm_worker_dispatch_failure_marks_event_job_failed(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.event_jobs.append(
        {
            "id": 3,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_idle_check",
                "payload": {"trigger": "idle_check"},
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    orchestrator.dispatch_status = "invalid"
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "failed"
    assert "pm_dispatch_failed" in str(db.event_jobs[0].get("last_error"))


def test_pm_worker_health_check_requeues_failed_outbox(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.outbox_rows.append(
        {
            "id": "outbox-1",
            "tenant_id": 1,
            "status": "failed",
            "payload_json": {"type": "interaction_update", "text": "hi"},
        }
    )
    db.event_jobs.append(
        {
            "id": 4,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_health_check",
                "payload": {"trigger": "health_check"},
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert db.outbox_rows[0]["status"] == "queued"
    assert orchestrator.dispatched_jobs == []


def test_pm_worker_health_check_finalizes_zombies_without_notify(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.inflight_runs.append({"id": 77, "tenant_id": 1, "stale": True})
    db.event_jobs.append(
        {
            "id": 6,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_health_check",
                "payload": {"trigger": "health_check"},
            },
        }
    )
    orchestrator = _FakeOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert orchestrator.finalize_calls == [{"run_id": 77, "notify": False}]


def test_pm_worker_health_check_skips_terminal_failed_outbox_rows(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.outbox_rows.extend(
        [
            {
                "id": "outbox-terminal-invalid",
                "tenant_id": 1,
                "status": "failed",
                "last_error": "invalid_payload_missing_text_or_tenant",
                "attempt_count": 1,
                "payload_json": {"type": "interaction_update"},
            },
            {
                "id": "outbox-terminal-attempts",
                "tenant_id": 1,
                "status": "failed",
                "last_error": "interaction_update_timeout",
                "attempt_count": 12,
                "payload_json": {"type": "interaction_update", "text": "hi"},
            },
            {
                "id": "outbox-requeueable",
                "tenant_id": 1,
                "status": "failed",
                "last_error": "interaction_update_timeout",
                "attempt_count": 3,
                "payload_json": {"type": "interaction_update", "text": "hello"},
            },
        ]
    )
    orchestrator = _FakeOrchestrator()
    worker = PMWorker(
        db=db,
        orchestrator=orchestrator,
        workspace_manager=WorkspaceManager(root_dir=tmp_path / "data"),
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            outbox_max_attempts=12,
        ),
    )
    tenant = db.get_tenant_by_id(1)
    assert tenant is not None

    fixed = asyncio.run(worker._fix_failed_outbox(tenant=tenant))
    snapshot = asyncio.run(worker._collect_health_snapshot(tenant_id=1, now=datetime.now(tz=timezone.utc)))

    assert fixed == 1
    statuses = {str(row["id"]): str(row["status"]) for row in db.outbox_rows}
    assert statuses["outbox-terminal-invalid"] == "failed"
    assert statuses["outbox-terminal-attempts"] == "failed"
    assert statuses["outbox-requeueable"] == "queued"
    assert snapshot["failed_outbox"] == 0
    assert db.outbox_status_updates == [
        ("outbox-requeueable", "queued", "pm_requeue", 0.0),
    ]


def test_pm_worker_stale_run_inputs_drains_only_stale_candidate_count(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    now = datetime.now(tz=timezone.utc)
    db.run_inputs = [
        {"id": "in-1", "status": "queued", "created_at": (now - timedelta(minutes=90)).isoformat()},
        {"id": "in-2", "status": "queued", "created_at": (now - timedelta(minutes=70)).isoformat()},
        {"id": "in-3", "status": "queued", "created_at": (now - timedelta(minutes=5)).isoformat()},
    ]
    orchestrator = _FakeOrchestrator()
    worker = PMWorker(
        db=db,
        orchestrator=orchestrator,
        workspace_manager=WorkspaceManager(root_dir=tmp_path / "data"),
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            health_stale_input_minutes=30,
        ),
    )
    tenant = db.get_tenant_by_id(1)
    assert tenant is not None

    fixed = asyncio.run(worker._fix_stale_run_inputs(tenant=tenant))

    assert fixed == 2
    assert orchestrator.drain_calls == 1
    assert orchestrator.last_drain_max_inputs == 2


def test_pm_worker_stale_received_recovery_scoped_to_tenant(tmp_path: Path):
    db = _FakeDB()
    tenant = db.get_tenant_by_id(1)
    assert tenant is not None
    db.stale_received = [
        {"id": 101, "tenant_id": 1, "status": "received"},
        {"id": 202, "tenant_id": 2, "status": "received"},
    ]

    class _RecoveringOrchestrator(_FakeOrchestrator):
        def __init__(self) -> None:
            super().__init__()
            self.replayed: list[dict[str, Any]] = []

        @staticmethod
        def _message_from_message_row(_tenant: Any, row: Any):
            return dict(row)

        async def handle_message(self, msg: Any, *_args: Any, **_kwargs: Any):
            self.replayed.append(dict(msg))
            return SimpleNamespace(status="accepted")

    orchestrator = _RecoveringOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    recovered = asyncio.run(worker._fix_stale_received_messages(tenant=tenant))

    assert recovered == 1
    assert [int(row["tenant_id"]) for row in orchestrator.replayed] == [1]
    assert db.last_stale_received_query is not None
    assert int(db.last_stale_received_query["tenant_id"]) == 1


def test_pm_worker_stale_event_rows_recover_under_tenant_provider(tmp_path: Path):
    db = _FakeDB()
    tenant = db.get_tenant_by_id(1)
    assert tenant is not None
    db.stale_received = [
        {
            "id": 303,
            "tenant_id": 1,
            "status": "received",
            "provider": "event",
            "provider_message_id": "event-303",
            "text": "event payload",
            "received_at": datetime.now(tz=timezone.utc).isoformat(),
            "raw_json": {"event": {"event_type": "run_completed"}},
        },
    ]

    class _RecoveringOrchestrator(_FakeOrchestrator):
        def __init__(self) -> None:
            super().__init__()
            self.replayed: list[tuple[Any, dict[str, Any]]] = []

        @staticmethod
        def _message_from_message_row(tenant: Any, row: Any):
            return NormalizedMessage(
                provider=str(row.get("provider") or ""),
                provider_message_id=str(row.get("provider_message_id") or ""),
                tenant_external_id=str(getattr(tenant, "external_id", "") or ""),
                received_at=datetime.now(tz=timezone.utc),
                text=row.get("text"),
                images=[],
                raw=row.get("raw_json") or {},
            )

        async def handle_message(self, msg: Any, *_args: Any, **kwargs: Any):
            self.replayed.append((msg, dict(kwargs)))
            return SimpleNamespace(status="accepted")

    orchestrator = _RecoveringOrchestrator()
    worker = _build_worker(tmp_path, db, orchestrator)

    recovered = asyncio.run(worker._fix_stale_received_messages(tenant=tenant))

    assert recovered == 1
    assert len(orchestrator.replayed) == 1
    replayed_msg, replay_kwargs = orchestrator.replayed[0]
    assert replayed_msg.provider == "telegram"
    assert replayed_msg.tenant_external_id == "tenant-1"
    assert replay_kwargs.get("allow_existing_received") is True
    assert replay_kwargs.get("allow_interaction_stream") is False


def test_pm_worker_extract_source_payload_from_scheduler_wrapper(tmp_path: Path):
    del tmp_path
    payload = {
        "payload": {
            "_scheduler": {
                "source_payload": {
                    "event_type": "run_completed",
                    "run_id": 123,
                }
            }
        }
    }

    extracted = PMWorker._extract_source_payload(payload)

    assert extracted == {"event_type": "run_completed", "run_id": 123}

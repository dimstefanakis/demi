from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from demi.jobs.pm_worker import PMWorker, PMWorkerConfig
from demi.models import NormalizedMessage
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
        self.pm_heartbeats: list[dict[str, Any]] = []
        self.pm_actions: list[dict[str, Any]] = []
        self.latest_user_message: dict[str, Any] | None = None
        self.message_count: int = 0
        self.stale_received: list[dict[str, Any]] = []
        self.last_stale_received_query: dict[str, Any] | None = None
        self.inflight_runs: list[dict[str, Any]] = []
        self.run_inputs: list[dict[str, Any]] = []
        self.enqueue_outbox_error: Exception | None = None
        self.outbox_status_updates: list[tuple[str, str, str | None, float | None]] = []
        self._pm_heartbeat_id = 1
        self._pm_action_id = 1
        self._outbox_id = 1

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

    def create_pm_heartbeat(self, **payload: Any) -> int:
        row = dict(payload)
        row["id"] = self._pm_heartbeat_id
        self._pm_heartbeat_id += 1
        self.pm_heartbeats.append(row)
        return int(row["id"])

    def update_pm_heartbeat(self, heartbeat_id: int, **updates: Any) -> None:
        for row in self.pm_heartbeats:
            if int(row["id"]) == int(heartbeat_id):
                row.update({k: v for k, v in updates.items() if v is not None})
                return

    def create_pm_action(self, **payload: Any) -> int:
        row = dict(payload)
        row["id"] = self._pm_action_id
        self._pm_action_id += 1
        self.pm_actions.append(row)
        return int(row["id"])

    def enqueue_outbox(
        self,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> str:
        if self.enqueue_outbox_error is not None:
            raise self.enqueue_outbox_error
        row = {
            "id": f"outbox-{self._outbox_id}",
            "tenant_id": int(tenant_id),
            "run_id": run_id,
            "project_name": project_name,
            "correlation_id": correlation_id,
            "payload_json": dict(payload),
            "status": "queued",
        }
        self._outbox_id += 1
        self.outbox_rows.append(row)
        return str(row["id"])

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

    def get_latest_user_message(self, tenant_id: int) -> dict[str, Any] | None:
        del tenant_id
        if self.latest_user_message is None:
            return None
        return dict(self.latest_user_message)

    def count_tenant_messages(
        self,
        tenant_id: int,
        *,
        statuses: list[str] | None = None,
    ) -> int:
        del tenant_id, statuses
        return int(self.message_count)

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
        self.drain_calls = 0
        self.last_drain_max_inputs: int | None = None

    @staticmethod
    def _is_run_stale(row: dict[str, Any], max_age_seconds: int = 900) -> bool:
        del max_age_seconds
        return bool(row.get("stale"))

    @staticmethod
    def _message_from_message_row(_tenant: Any, _row: Any):
        return None

    async def handle_message(self, *_args, **_kwargs):
        return SimpleNamespace(status="accepted")

    async def _finalize_stale_run(self, _tenant: Any, row: dict[str, Any], *, notify: bool = True):
        del notify
        row["finalized"] = True

    async def _drain_run_inputs(self, _tenant: Any, *, max_inputs: int | None = None):
        self.drain_calls += 1
        self.last_drain_max_inputs = max_inputs
        return int(max_inputs or 0)


def _run(worker: PMWorker) -> bool:
    return asyncio.run(worker.process_once())


def test_pm_worker_idle_trigger_enqueues_pm_suggestion(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.latest_user_message = {
        "id": 10,
        "received_at": (datetime.now(tz=timezone.utc) - timedelta(hours=72)).isoformat(),
        "text": "thanks",
    }
    db.message_count = 10
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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            cooldown_between_user_messages_seconds=1,
            idle_threshold_hours=48,
        ),
    )

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert db.pm_heartbeats
    assert db.pm_heartbeats[0]["action_needed"] is True
    assert db.outbox_rows
    payload = db.outbox_rows[0]["payload_json"]
    assert payload["type"] == "pm_suggestion"
    assert "check-in" in payload["text"] or "want me" in payload["text"]
    pm_state_path = tmp_path / "data" / "telegram:tenant-1" / "pm_state.json"
    assert pm_state_path.exists()
    saved = json.loads(pm_state_path.read_text(encoding="utf-8"))
    assert saved["conversation_state"]["last_pm_message_at"]


def test_pm_worker_enqueue_failure_marks_event_job_failed(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.latest_user_message = {
        "id": 10,
        "received_at": (datetime.now(tz=timezone.utc) - timedelta(hours=72)).isoformat(),
        "text": "thanks",
    }
    db.message_count = 10
    db.enqueue_outbox_error = RuntimeError("outbox_enqueue_failed")
    db.event_jobs.append(
        {
            "id": 10,
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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            cooldown_between_user_messages_seconds=1,
            idle_threshold_hours=48,
        ),
    )

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "failed"
    assert "outbox_enqueue_failed" in str(db.event_jobs[0].get("last_error"))
    assert db.pm_actions == []
    assert db.outbox_rows == []


def test_pm_worker_health_check_requeues_failed_outbox(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.latest_user_message = None
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
            "id": 2,
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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
        ),
    )

    processed = _run(worker)

    assert processed is True
    assert db.event_jobs[0]["status"] == "completed"
    assert db.outbox_rows[0]["status"] == "queued"
    assert any(row.get("action_type") == "fix_failed_outbox" for row in db.pm_actions)


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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=orchestrator,
        workspace_manager=workspace_manager,
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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=orchestrator,
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
        ),
    )

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
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=orchestrator,
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
        ),
    )

    recovered = asyncio.run(worker._fix_stale_received_messages(tenant=tenant))

    assert recovered == 1
    assert len(orchestrator.replayed) == 1
    replayed_msg, replay_kwargs = orchestrator.replayed[0]
    assert replayed_msg.provider == "telegram"
    assert replayed_msg.tenant_external_id == "tenant-1"
    assert replay_kwargs.get("allow_existing_received") is True
    assert replay_kwargs.get("allow_interaction_stream") is False


def test_pm_worker_daily_heartbeat_requests_qa_without_freshness_gate(tmp_path: Path):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    tenant = db.get_tenant_by_id(1)
    assert tenant is not None
    now = datetime.now(tz=timezone.utc)
    pm_state = {
        "projects": {
            "main": {
                "last_deploy_url": str(tenant.last_deploy_url or ""),
                "last_qa_at": now.isoformat(),
                "qa_status": "pass",
            }
        },
        "conversation_state": {},
    }
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
        ),
    )

    triage = asyncio.run(
        worker._run_triage(
            tenant=tenant,
            trigger="daily_heartbeat",
            payload={"payload": {"trigger": "daily_heartbeat"}},
            pm_state=pm_state,
            now=now,
        )
    )

    actions = triage.get("suggested_actions") if isinstance(triage, dict) else []
    assert isinstance(actions, list)
    assert any(str(action.get("action") or "") == "qa_check" for action in actions)


def test_pm_worker_first_heartbeat_disables_first_trigger_when_ready(tmp_path: Path, monkeypatch):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:pm-first-heartbeat",
        {
            "id": "pm-first-heartbeat",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "0 */2 * * *",
            "payload": {"trigger": "first_heartbeat"},
            "state": {},
        },
    )
    db.latest_user_message = {
        "id": 11,
        "received_at": datetime.now(tz=timezone.utc).isoformat(),
        "text": "hello",
    }
    db.message_count = 12
    db.event_jobs.append(
        {
            "id": 3,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_first_heartbeat",
                "payload": {"trigger": "first_heartbeat"},
            },
        }
    )
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            first_heartbeat_min_messages=5,
        ),
    )

    async def _fake_check_site_health(url: str) -> tuple[str, str]:
        del url
        return "pass", "reachable (200)"

    monkeypatch.setattr(worker, "_check_site_health", _fake_check_site_health)

    processed = _run(worker)

    assert processed is True
    trigger = db.get_tenant_kv(1, "scheduler", "trigger:pm-first-heartbeat")
    assert trigger is not None
    assert trigger["enabled"] is False
    assert db.get_tenant_kv(1, "pm", "needs_onboarding") is None
    assert any(row.get("action_type") == "qa_check" for row in db.pm_actions)


def test_pm_worker_first_heartbeat_does_not_require_user_message_threshold(tmp_path: Path, monkeypatch):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:pm-first-heartbeat",
        {
            "id": "pm-first-heartbeat",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "0 */2 * * *",
            "payload": {"trigger": "first_heartbeat"},
            "state": {},
        },
    )
    db.latest_user_message = None
    db.message_count = 0
    db.event_jobs.append(
        {
            "id": 30,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_first_heartbeat",
                "payload": {"trigger": "first_heartbeat"},
            },
        }
    )
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            first_heartbeat_min_messages=999,
        ),
    )

    async def _fake_check_site_health(url: str) -> tuple[str, str]:
        del url
        return "pass", "reachable (200)"

    monkeypatch.setattr(worker, "_check_site_health", _fake_check_site_health)

    processed = _run(worker)

    assert processed is True
    trigger = db.get_tenant_kv(1, "scheduler", "trigger:pm-first-heartbeat")
    assert trigger is not None
    assert trigger["enabled"] is False
    assert db.get_tenant_kv(1, "pm", "needs_onboarding") is None
    assert any(row.get("action_type") == "qa_check" for row in db.pm_actions)
    assert not any(str(row.get("autonomy_tier")) == "suggest" for row in db.pm_actions)


def test_pm_worker_first_heartbeat_keeps_onboarding_when_qa_fails(tmp_path: Path, monkeypatch):
    db = _FakeDB()
    db.set_tenant_kv(1, "pm", "enabled", {"enabled": True})
    db.set_tenant_kv(1, "pm", "needs_onboarding", {"enabled": True})
    db.set_tenant_kv(
        1,
        "scheduler",
        "trigger:pm-first-heartbeat",
        {
            "id": "pm-first-heartbeat",
            "enabled": True,
            "trigger_type": "cron",
            "cron": "0 */2 * * *",
            "payload": {"trigger": "first_heartbeat"},
            "state": {},
        },
    )
    db.latest_user_message = {
        "id": 12,
        "received_at": datetime.now(tz=timezone.utc).isoformat(),
        "text": "hello",
    }
    db.message_count = 12
    db.event_jobs.append(
        {
            "id": 4,
            "tenant_id": 1,
            "job_type": "pm_trigger",
            "status": "pending",
            "payload_json": {
                "event_type": "pm_trigger",
                "intent": "pm_first_heartbeat",
                "payload": {"trigger": "first_heartbeat"},
            },
        }
    )
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    worker = PMWorker(
        db=db,
        orchestrator=_FakeOrchestrator(),
        workspace_manager=workspace_manager,
        config=PMWorkerConfig(
            poll_interval=0.01,
            batch_size=5,
            first_heartbeat_min_messages=5,
        ),
    )

    async def _fake_check_site_health(url: str) -> tuple[str, str]:
        del url
        return "fail", "returned status 503"

    monkeypatch.setattr(worker, "_check_site_health", _fake_check_site_health)

    processed = _run(worker)

    assert processed is True
    trigger = db.get_tenant_kv(1, "scheduler", "trigger:pm-first-heartbeat")
    assert trigger is not None
    assert trigger["enabled"] is True
    onboarding = db.get_tenant_kv(1, "pm", "needs_onboarding")
    assert onboarding is not None

from __future__ import annotations

import json
from datetime import datetime, timezone

from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.workspace.core import WorkspaceManager
from tests.utils import build_test_db, unique_external_id


class _NoopMessenger:
    async def send_text(self, tenant_external_id, text, reply_to_message_id=None):
        del tenant_external_id, text, reply_to_message_id
        return None


class _NoopAgent:
    pass


def test_interaction_context_compacts_recent_run_summaries(tmp_path):
    db = build_test_db()
    workspace_manager = WorkspaceManager(root_dir=tmp_path / "data")
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=_NoopAgent(),
        messenger=_NoopMessenger(),
    )

    tenant = db.get_or_create_tenant("telegram", unique_external_id("tenant"))
    workspace = workspace_manager.ensure_workspace(tenant.key)
    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id=unique_external_id("msg"),
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="status update?",
        images=[],
        raw={},
    )

    run_id = db.create_run(tenant.id, message_id=0, project_name=None)
    db.finish_run(run_id, status="completed")
    db.update_run_result_summary(run_id, "x" * 5000)
    db.update_run_tool_summary(
        run_id,
        {
            "count": 25,
            "tools": {
                f"tool_{idx}": {"count": idx, "error_count": 0}
                for idx in range(25)
            },
        },
    )
    run_row = db.get_run(run_id)

    orchestrator._write_interaction_context(
        workspace=workspace,
        tenant=tenant,
        msg=msg,
        message_id=123,
        active_run=run_row,
        inflight_run=run_row,
        billing_status=None,
        billing_checked_at=None,
    )

    payload = json.loads((workspace.tasks_dir / "interaction_context.json").read_text())
    assert payload.get("recent_runs")
    recent = payload["recent_runs"][0]
    assert "result_summary" in recent
    assert len(recent["result_summary"]) < 1200
    assert recent.get("tool_summary", {}).get("tools_truncated", 0) > 0
    assert "tool_summary_json" not in recent
    assert "usage_json" not in recent

    active = payload.get("active_run") or {}
    assert active.get("id") == run_id
    assert "tool_summary_json" not in active
    assert "usage_json" not in active

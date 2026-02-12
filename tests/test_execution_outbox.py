import json

from demi.agent.execution_outbox import (
    execution_final_correlation_id,
    has_final_interaction_update,
    tool_runs_show_final_interaction_update,
)


def test_execution_final_correlation_id_is_deterministic():
    assert execution_final_correlation_id(77) == "execution-final:77"
    assert execution_final_correlation_id(None) == "execution-final:unknown"


def test_has_final_interaction_update_detects_tool_run(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    correlation_id = "execution-final:77"
    (tasks_dir / "tool_runs.jsonl").write_text(
        json.dumps(
            {
                "tool": "send_message",
                "args": {"text": "Done", "final": True, "correlation_id": correlation_id},
                "result": {"queued": True, "status": "interaction_required"},
            }
        )
        + "\n"
    )

    assert tool_runs_show_final_interaction_update(tasks_dir) is True
    assert tool_runs_show_final_interaction_update(tasks_dir, correlation_id=correlation_id) is True
    assert tool_runs_show_final_interaction_update(tasks_dir, correlation_id="execution-final:88") is False
    assert has_final_interaction_update(tasks_dir) is True


def test_has_final_interaction_update_detects_fallback_file(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    correlation_id = "execution-final:77"
    (tasks_dir / "interaction_updates.jsonl").write_text(
        json.dumps(
            {
                "type": "interaction_update",
                "action": "send_message",
                "text": "All set.",
                "final": True,
                "correlation_id": correlation_id,
            }
        )
        + "\n"
    )

    assert has_final_interaction_update(tasks_dir) is True
    assert has_final_interaction_update(tasks_dir, correlation_id=correlation_id) is True
    assert has_final_interaction_update(tasks_dir, correlation_id="execution-final:88") is False


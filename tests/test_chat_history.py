from datetime import datetime, timezone

from claudius.memory.logs import append_log, read_logs, write_chat_history
from claudius.orchestrator import Orchestrator


def test_chat_history_written(tmp_path):
    tasks_dir = tmp_path / "tasks"
    append_log(tasks_dir, "user_message", "Hi", datetime.now(tz=timezone.utc))
    append_log(tasks_dir, "assistant_message", "Hello", datetime.now(tz=timezone.utc))

    write_chat_history(tasks_dir, last_n=2)
    history = (tasks_dir / "chat_history.md").read_text()
    assert "user_message" in history
    assert "assistant_message" in history


def test_compaction_prompt_created(tmp_path):
    tasks_dir = tmp_path / "tasks"
    for i in range(35):
        append_log(tasks_dir, "user_message", f"msg {i}")

    Orchestrator._maybe_prepare_compaction(tasks_dir, max_entries=30, keep_last=10)
    prompt_path = tasks_dir / "summary_prompt.md"
    assert prompt_path.exists()
    content = prompt_path.read_text()
    assert "SYSTEM_PROMPT:" in content
    assert "USER_MESSAGE:" in content


def test_memory_prompt_created(tmp_path):
    tasks_dir = tmp_path / "tasks"
    memory_path = tmp_path / "memory.md"
    memory_path.write_text("# Memory\n\n- Existing detail\n")
    for i in range(3):
        append_log(tasks_dir, "user_message", f"msg {i}")

    Orchestrator._maybe_prepare_memory_update(tasks_dir, memory_path, max_entries=2)
    prompt_path = tasks_dir / "memory_prompt.md"
    assert prompt_path.exists()
    content = prompt_path.read_text()
    assert "SYSTEM_PROMPT:" in content
    assert "USER_MESSAGE:" in content
    assert "Existing memory:" in content

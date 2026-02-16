from datetime import datetime, timezone

from demi.memory.logs import append_log, write_chat_history


def test_chat_history_written(tmp_path):
    tasks_dir = tmp_path / "tasks"
    append_log(tasks_dir, "user_message", "Hi", datetime.now(tz=timezone.utc))
    append_log(tasks_dir, "assistant_message", "Hello", datetime.now(tz=timezone.utc))

    write_chat_history(tasks_dir, last_n=2)
    history = (tasks_dir / "chat_history.md").read_text()
    assert "user_message" in history
    assert "assistant_message" in history


def test_chat_history_truncates_large_entries(tmp_path):
    tasks_dir = tmp_path / "tasks"
    large_payload = "A" * 300
    append_log(tasks_dir, "assistant_message", large_payload, datetime.now(tz=timezone.utc))

    write_chat_history(tasks_dir, last_n=1, max_entry_chars=80)
    history = (tasks_dir / "chat_history.md").read_text()
    assert "...[truncated" in history
    assert large_payload not in history

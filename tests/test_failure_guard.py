from pathlib import Path

from claudius.failure_guard import get_block, record_hard_failure


def test_failure_guard_blocks_after_two(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    first = record_hard_failure(
        tasks_dir,
        "managed_backend",
        reason="missing_org",
        max_failures=2,
    )
    assert first["blocked"] is False

    second = record_hard_failure(
        tasks_dir,
        "managed_backend",
        reason="missing_org",
        max_failures=2,
    )
    assert second["blocked"] is True
    block = get_block(tasks_dir, "managed_backend")
    assert block is not None

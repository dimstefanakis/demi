from demi.agent.claude import ClaudeAgent
from pathlib import Path


def test_prompt_includes_core_instructions(tmp_path):
    task = tmp_path / "task.md"
    memory = tmp_path / "memory.md"
    task.write_text("Task")
    memory.write_text("Memory contents for snapshot")

    prompt = ClaudeAgent._build_prompt(task_path=task, memory_path=memory)

    assert "bun-next-shadcn" in prompt
    assert "tasks/app_name.txt" in prompt
    assert "gemini-3-pro-preview" in prompt
    assert "record_deploy" in prompt
    assert "record_domain_quote" in prompt
    assert "Domain Search & Purchase (Vercel CLI Only)" in prompt
    assert "interaction agent" in prompt
    assert "unsplash-backfill" in prompt
    assert "Memory contents for snapshot" in prompt

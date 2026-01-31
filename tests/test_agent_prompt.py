from claudius.agent.claude import ClaudeAgent
from pathlib import Path


def test_prompt_includes_bun_commands(tmp_path):
    task = tmp_path / "task.md"
    memory = tmp_path / "memory.md"
    task.write_text("Task")
    memory.write_text("Memory contents for snapshot")

    prompt = ClaudeAgent._build_prompt(task_path=task, memory_path=memory)

    assert "bun create next-app@latest" in prompt
    assert "bunx --bun shadcn@latest init" in prompt
    assert "tasks/app_name.txt" in prompt
    assert "gemini -p" in prompt
    assert "record_deploy" in prompt
    assert "record_domain_quote" in prompt
    assert "interaction-agent" in prompt
    assert "mcp__claudius-unsplash__search_photos" in prompt
    assert "Memory contents for snapshot" in prompt

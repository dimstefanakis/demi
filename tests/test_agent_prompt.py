from pathlib import Path

from demi.agent.claude import ClaudeAgent


def test_prompt_includes_core_instructions(tmp_path):
    task = tmp_path / "task.md"
    memory = tmp_path / "memory.md"
    task.write_text("Task")
    memory.write_text("Memory contents for snapshot")

    prompt = ClaudeAgent._build_prompt(task_path=task, memory_path=memory)

    assert "bun-next-shadcn" in prompt
    assert "tasks/app_name.txt" in prompt
    assert "product-designer" in prompt
    assert "software-engineer" in prompt
    assert "devops-engineer" in prompt
    assert "record_deploy" in prompt
    assert "record_domain_quote" in prompt
    assert "Domain Search & Purchase (Vercel CLI Only)" in prompt
    assert "interaction agent" in prompt
    assert "Memory contents for snapshot" in prompt
    assert "Your email is not configured." in prompt


def test_prompt_includes_agent_email_from_env(tmp_path, monkeypatch):
    task = tmp_path / "task.md"
    memory = tmp_path / "memory.md"
    task.write_text("Task")
    memory.write_text("Memory")
    monkeypatch.setenv("AGENT_EMAIL", "demi-bot@example.com")

    prompt = ClaudeAgent._build_prompt(task_path=task, memory_path=memory)

    assert "Your email is demi-bot@example.com." in prompt
    assert "<<AGENT_EMAIL>>" not in prompt

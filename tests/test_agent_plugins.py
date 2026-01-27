from pathlib import Path

from claudius.agent.claude import ClaudeAgent


def test_plugins_loaded_from_env(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "my-plugin"
    plugin_dir.mkdir()

    monkeypatch.setenv("CLAUDE_PLUGINS", str(plugin_dir))

    agent = ClaudeAgent()
    assert agent.plugins == [{"type": "local", "path": str(plugin_dir)}]


def test_plugins_ignore_missing_paths(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setenv("CLAUDE_PLUGINS", f"{missing}")

    agent = ClaudeAgent()
    assert agent.plugins == []


def test_plugins_relative_paths(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "relative-plugin"
    plugin_dir.mkdir()
    rel_path = Path(plugin_dir.name)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGINS", str(rel_path))

    agent = ClaudeAgent()
    assert agent.plugins == [{"type": "local", "path": str(plugin_dir)}]

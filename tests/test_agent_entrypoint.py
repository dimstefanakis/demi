import demi.runtime.agent_entrypoint as agent_entrypoint


def test_load_runtime_env_from_process_collects_github_runtime_keys(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_short_lived")
    monkeypatch.setenv("GITHUB_REPO_FULL_NAME", "acme/site")
    monkeypatch.setenv("GITHUB_REPO_NAME", "site")
    monkeypatch.setenv("GITHUB_REPO_URL", "https://github.com/acme/site")
    monkeypatch.setenv("UNRELATED_SECRET", "should-not-be-collected")

    runtime_env = agent_entrypoint._load_runtime_env_from_process()

    assert runtime_env is not None
    assert runtime_env["GITHUB_TOKEN"] == "ghs_short_lived"
    assert runtime_env["GITHUB_REPO_FULL_NAME"] == "acme/site"
    assert runtime_env["GITHUB_REPO_NAME"] == "site"
    assert runtime_env["GITHUB_REPO_URL"] == "https://github.com/acme/site"
    assert "UNRELATED_SECRET" not in runtime_env


def test_load_runtime_env_from_process_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO_FULL_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REPO_NAME", raising=False)

    runtime_env = agent_entrypoint._load_runtime_env_from_process()

    assert runtime_env is None

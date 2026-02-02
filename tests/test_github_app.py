import asyncio

import pytest

from claudius.domains.github_app import GitHubAppConfig, GitHubRepo, GitHubRepoManager


def _config(*, auto_create_repo: bool = True) -> GitHubAppConfig:
    return GitHubAppConfig(
        org="acme",
        app_id="1",
        installation_id="2",
        private_key="test-key",
        auto_create_repo=auto_create_repo,
    )


def _repo(name: str, full_name: str, repo_id: int) -> GitHubRepo:
    return GitHubRepo(
        id=repo_id,
        name=name,
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        clone_url=f"https://github.com/{full_name}.git",
        ssh_url=f"git@github.com:{full_name}.git",
        default_branch="main",
        private=True,
    )


class FakeGitHubAppClient:
    def __init__(self, *, remote_repo: GitHubRepo | None, created_repo: GitHubRepo | None):
        self.remote_repo = remote_repo
        self.created_repo = created_repo
        self.get_calls: list[str] = []
        self.create_calls: list[tuple[str, bool]] = []

    async def get_repo(self, full_name: str) -> GitHubRepo | None:
        self.get_calls.append(full_name)
        return self.remote_repo

    async def create_repo(self, name: str, private: bool) -> GitHubRepo:
        self.create_calls.append((name, private))
        if self.created_repo is None:
            raise RuntimeError("create_repo_not_configured")
        return self.created_repo


def test_ensure_repo_creates_repo_when_stored_repo_is_stale(tmp_path):
    manager = GitHubRepoManager(_config(auto_create_repo=True))
    stale_repo = _repo("old-repo", "acme/old-repo", repo_id=1)
    manager.write_repo(tmp_path, stale_repo)

    new_repo = _repo("fresh-repo", "acme/fresh-repo", repo_id=2)
    manager.client = FakeGitHubAppClient(remote_repo=None, created_repo=new_repo)

    repo = asyncio.run(manager.ensure_repo(tmp_path, repo_name="fresh-repo"))

    assert repo == new_repo
    assert manager.client.get_calls == ["acme/old-repo", "acme/fresh-repo"]
    assert manager.client.create_calls == [("fresh-repo", True)]
    assert manager.load_repo(tmp_path) == new_repo


def test_ensure_repo_reuses_existing_remote_repo_from_metadata(tmp_path):
    manager = GitHubRepoManager(_config(auto_create_repo=True))
    existing_remote = _repo("fresh-repo", "acme/fresh-repo", repo_id=2)
    manager.write_repo(tmp_path, existing_remote)
    manager.client = FakeGitHubAppClient(remote_repo=existing_remote, created_repo=None)

    repo = asyncio.run(manager.ensure_repo(tmp_path, repo_name="fresh-repo"))

    assert repo == existing_remote
    assert manager.client.get_calls == ["acme/fresh-repo"]
    assert manager.client.create_calls == []
    assert manager.load_repo(tmp_path) == existing_remote


def test_ensure_repo_rejects_existing_remote_repo_without_metadata(tmp_path):
    manager = GitHubRepoManager(_config(auto_create_repo=True))
    existing_remote = _repo("fresh-repo", "acme/fresh-repo", repo_id=2)
    manager.client = FakeGitHubAppClient(remote_repo=existing_remote, created_repo=None)

    with pytest.raises(RuntimeError, match="github_repo_name_conflict"):
        asyncio.run(manager.ensure_repo(tmp_path, repo_name="fresh-repo"))

    assert manager.client.get_calls == ["acme/fresh-repo"]
    assert manager.client.create_calls == []
    assert manager.load_repo(tmp_path) is None


def test_ensure_repo_clears_stale_metadata_when_creation_is_disabled(tmp_path):
    manager = GitHubRepoManager(_config(auto_create_repo=False))
    stale_repo = _repo("old-repo", "acme/old-repo", repo_id=1)
    manager.write_repo(tmp_path, stale_repo)
    manager.client = FakeGitHubAppClient(remote_repo=None, created_repo=None)

    with pytest.raises(RuntimeError, match="github_repo_missing"):
        asyncio.run(manager.ensure_repo(tmp_path))

    assert manager.client.get_calls == ["acme/old-repo"]
    assert manager.load_repo(tmp_path) is None

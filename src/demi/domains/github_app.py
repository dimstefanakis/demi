from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import base64
import json
import re
import time

import httpx
import jwt

from demi.config import Settings


GITHUB_API_VERSION = "2022-11-28"
REPO_FILE_NAME = "github_repo.json"
MAX_REPO_NAME_LENGTH = 100


@dataclass(frozen=True)
class GitHubRepo:
    id: int | None
    name: str
    full_name: str
    html_url: str | None
    clone_url: str | None
    ssh_url: str | None
    default_branch: str | None
    private: bool | None

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> GitHubRepo:
        return GitHubRepo(
            id=int(payload["id"]) if payload.get("id") is not None else None,
            name=str(payload.get("name") or ""),
            full_name=str(payload.get("full_name") or ""),
            html_url=_string_or_none(payload.get("html_url")),
            clone_url=_string_or_none(payload.get("clone_url")),
            ssh_url=_string_or_none(payload.get("ssh_url")),
            default_branch=_string_or_none(payload.get("default_branch")),
            private=bool(payload.get("private")) if payload.get("private") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "full_name": self.full_name,
            "html_url": self.html_url,
            "clone_url": self.clone_url,
            "ssh_url": self.ssh_url,
            "default_branch": self.default_branch,
            "private": self.private,
        }


@dataclass(frozen=True)
class GitHubAppConfig:
    org: str
    app_id: str
    installation_id: str
    private_key: str
    api_base_url: str = "https://api.github.com"
    repo_prefix: str = "site"
    repo_visibility: str = "private"
    auto_create_repo: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.org and self.app_id and self.installation_id and self.private_key)

    @staticmethod
    def from_settings(settings: Settings) -> GitHubAppConfig | None:
        if (
            not settings.github_org
            or not settings.github_app_id
            or not settings.github_app_installation_id
            or not settings.github_app_private_key
        ):
            return None
        private_key = _normalize_private_key(settings.github_app_private_key)
        if not private_key:
            return None
        return GitHubAppConfig(
            org=settings.github_org,
            app_id=str(settings.github_app_id),
            installation_id=str(settings.github_app_installation_id),
            private_key=private_key,
            api_base_url=settings.github_api_base_url,
            repo_prefix=settings.github_repo_prefix,
            repo_visibility=settings.github_repo_visibility,
        )

    def repo_is_private(self) -> bool:
        return str(self.repo_visibility).lower() != "public"


@dataclass
class GitHubAppClient:
    config: GitHubAppConfig
    timeout_seconds: float = 30.0

    def _create_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": self.config.app_id,
        }
        token = jwt.encode(payload, self.config.private_key, algorithm="RS256")
        return token.decode("utf-8") if isinstance(token, bytes) else token

    async def create_installation_token(
        self,
        repositories: list[str] | None = None,
        repository_ids: list[int] | None = None,
    ) -> str:
        jwt_token = self._create_jwt()
        payload: dict[str, Any] = {}
        if repositories:
            payload["repositories"] = repositories
        if repository_ids:
            payload["repository_ids"] = repository_ids
        data = await self._request_json(
            "POST",
            f"/app/installations/{self.config.installation_id}/access_tokens",
            headers=self._jwt_headers(jwt_token),
            json_payload=payload or None,
        )
        token = data.get("token")
        if not token:
            raise RuntimeError("github_installation_token_missing")
        return str(token)

    async def get_repo(self, full_name: str) -> GitHubRepo | None:
        token = await self.create_installation_token()
        try:
            data = await self._request_json(
                "GET",
                f"/repos/{full_name}",
                headers=self._token_headers(token),
            )
        except GitHubNotFound:
            return None
        return GitHubRepo.from_payload(data)

    async def create_repo(self, name: str, private: bool) -> GitHubRepo:
        token = await self.create_installation_token()
        payload = {
            "name": name,
            "private": private,
            "auto_init": False,
        }
        data = await self._request_json(
            "POST",
            f"/orgs/{self.config.org}/repos",
            headers=self._token_headers(token),
            json_payload=payload,
        )
        return GitHubRepo.from_payload(data)

    def _jwt_headers(self, jwt_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def _token_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.config.api_base_url,
            timeout=self.timeout_seconds,
        ) as client:
            resp = await client.request(method, path, headers=headers, json=json_payload)
        if resp.status_code == 404:
            raise GitHubNotFound()
        if resp.status_code >= 400:
            message = _extract_github_error(resp)
            raise RuntimeError(f"github_api_error:{resp.status_code}:{message}")
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError("github_api_invalid_json") from None


@dataclass
class GitHubRepoManager:
    config: GitHubAppConfig
    client: GitHubAppClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = GitHubAppClient(self.config)

    def repo_file_path(self, project_root: Path) -> Path:
        return Path(project_root) / REPO_FILE_NAME

    def load_repo(self, project_root: Path) -> GitHubRepo | None:
        path = self.repo_file_path(project_root)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        name = str(payload.get("name") or "")
        full_name = str(payload.get("full_name") or "")
        if not name or not full_name:
            return None
        return GitHubRepo(
            id=int(payload["id"]) if payload.get("id") is not None else None,
            name=name,
            full_name=full_name,
            html_url=_string_or_none(payload.get("html_url")),
            clone_url=_string_or_none(payload.get("clone_url")),
            ssh_url=_string_or_none(payload.get("ssh_url")),
            default_branch=_string_or_none(payload.get("default_branch")),
            private=bool(payload.get("private")) if payload.get("private") is not None else None,
        )

    def write_repo(self, project_root: Path, repo: GitHubRepo) -> None:
        path = self.repo_file_path(project_root)
        payload = repo.to_dict()
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
        except OSError:
            pass

    async def ensure_repo(
        self,
        project_root: Path,
        repo_name: str | None = None,
    ) -> GitHubRepo:
        existing = self.load_repo(project_root)
        if existing:
            remote = await self.client.get_repo(existing.full_name)
            if remote:
                self.write_repo(project_root, remote)
                return remote
            # The local metadata is stale if the remote repo no longer exists.
            # Drop it so we can recover by creating a new repo.
            try:
                self.repo_file_path(project_root).unlink(missing_ok=True)
            except OSError:
                pass
            existing = None
        repo_name = _slug(repo_name or "")
        if not repo_name:
            if not self.config.auto_create_repo:
                raise RuntimeError("github_repo_missing")
            raise RuntimeError("github_repo_name_required")
        if len(repo_name) > MAX_REPO_NAME_LENGTH:
            repo_name = repo_name[:MAX_REPO_NAME_LENGTH].rstrip("-_")
        full_name = f"{self.config.org}/{repo_name}"
        remote = await self.client.get_repo(full_name)
        if remote:
            if existing and existing.full_name == full_name:
                self.write_repo(project_root, remote)
                return remote
            raise RuntimeError("github_repo_name_conflict")
        if not self.config.auto_create_repo:
            raise RuntimeError("github_repo_missing")
        repo = await self.client.create_repo(repo_name, private=self.config.repo_is_private())
        self.write_repo(project_root, repo)
        return repo

    async def create_repo_token(self, repo: GitHubRepo) -> str:
        return await self.client.create_installation_token(repositories=[repo.name])


class GitHubNotFound(RuntimeError):
    pass


def _extract_github_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:200]
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return json.dumps(payload, ensure_ascii=True)[:200]


def _normalize_private_key(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    value = value.replace("\\n", "\n")
    if "-----BEGIN" in value:
        return value
    try:
        decoded = base64.b64decode(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value
    decoded = decoded.replace("\\n", "\n")
    return decoded if decoded else value


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    return cleaned


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None

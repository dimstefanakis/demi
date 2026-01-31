from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Attachment:
    provider_file_id: str
    width: int | None = None
    height: int | None = None


@dataclass
class NormalizedMessage:
    provider: str
    provider_message_id: str
    tenant_external_id: str
    received_at: datetime
    text: str | None
    images: list[Attachment]
    raw: dict[str, Any]
    project_name: str | None = None

    @property
    def tenant_key(self) -> str:
        return f"{self.provider}:{self.tenant_external_id}"


@dataclass
class Tenant:
    id: int
    provider: str
    external_id: str
    key: str
    workspace_path: str
    session_id: str | None
    vercel_project_id: str | None
    last_deploy_url: str | None
    created_at: str
    updated_at: str


@dataclass
class OrchestratorResult:
    status: str
    detail: str | None = None

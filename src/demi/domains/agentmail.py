from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agentmail import AgentMail

logger = logging.getLogger(__name__)


@dataclass
class AgentMailManager:
    """Thin wrapper around the AgentMail SDK for pod lifecycle management.

    Architecture:
    - Single shared inbox (``demi@agentmail.to``) — already exists, never created here.
    - Per-tenant pods for isolation — created lazily when a tenant needs email operations.
    - Single global webhook for the shared inbox.
    """

    api_key: str
    _client: AgentMail = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = AgentMail(api_key=self.api_key)

    def ensure_tenant_pod(self, tenant_id: int, tenant_key: str) -> dict[str, Any]:
        """Create or retrieve a pod for the given tenant (idempotent via client_id)."""
        client_id = f"tenant-{tenant_id}"
        pod = self._client.pods.create(
            name=f"Tenant {tenant_key}",
            client_id=client_id,
        )
        return {
            "pod_id": pod.pod_id,
            "client_id": pod.client_id,
        }

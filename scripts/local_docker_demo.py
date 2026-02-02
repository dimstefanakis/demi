from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from demi.config import Settings
from demi.db.core import Database
from demi.models import NormalizedMessage
from demi.orchestrator import Orchestrator
from demi.runtime.docker_agent import DockerAgent
from demi.runtime.docker_pool import DockerPool, DockerPoolConfig
from demi.workspace.core import WorkspaceManager


class NoopMessenger:
    async def send_text(self, tenant_external_id: str, text: str) -> None:
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local docker-backed agent demo.")
    parser.add_argument("--tenant", default="local-demo", help="Tenant external id")
    parser.add_argument("--message", default="Build me a barber site", help="Message text")
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    settings = Settings()

    db = Database(settings.resolved_db_path())
    db.init()

    workspace_manager = WorkspaceManager(settings.resolved_data_dir(), template_root=settings.root_dir)
    pool = DockerPool(
        DockerPoolConfig(
            image=settings.docker_image,
            pool_size=settings.docker_pool_size,
            pool_root=(settings.root_dir / settings.docker_pool_root).resolve(),
            mount_path=settings.docker_mount_path,
        )
    )
    await pool.ensure_pool()

    agent = DockerAgent(
        pool=pool,
        settings=settings,
        mount_path=settings.docker_mount_path,
        forward_messages=settings.docker_forward_messages,
    )
    orchestrator = Orchestrator(
        db=db,
        workspace_manager=workspace_manager,
        agent=agent,
        messenger=NoopMessenger(),
        workspace_allocator=pool,
    )

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id=str(int(datetime.now(tz=timezone.utc).timestamp())),
        tenant_external_id=args.tenant,
        received_at=datetime.now(tz=timezone.utc),
        text=args.message,
        images=[],
        raw={},
    )

    result = await orchestrator.handle_message(msg)
    print(f"Run status: {result.status}")
    tenant = db.get_or_create_tenant("telegram", args.tenant)
    print(f"Workspace: {tenant.workspace_path}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import uuid

from demi.config import Settings

@dataclass(frozen=True)
class DockerPoolConfig:
    image: str
    pool_size: int = 1
    pool_root: Path = Path("data/pool")
    mount_path: str = "/workspace"
    container_prefix: str = "demi-pool"
    extra_env: dict[str, str] | None = None


@dataclass
class ContainerSlot:
    container_id: str
    name: str
    workspace_path: Path


class DockerPool:
    def __init__(self, config: DockerPoolConfig):
        self.config = config
        self._idle: list[ContainerSlot] = []
        self._assigned: dict[Path, ContainerSlot] = {}
        self._lock = asyncio.Lock()
        self._scanned = False

    async def ensure_pool(self) -> None:
        await self._scan_existing()
        async with self._lock:
            while len(self._idle) < self.config.pool_size:
                slot = await self._start_idle_container()
                self._idle.append(slot)

    async def allocate_workspace(self, tenant: object | None = None) -> Path:
        await self._scan_existing()
        async with self._lock:
            slot = None
            while self._idle:
                candidate = self._idle.pop(0)
                if await self._is_container_running(candidate.container_id):
                    slot = candidate
                    break
            if slot is None:
                slot = await self._start_idle_container()
            self._assigned[slot.workspace_path] = slot
        asyncio.create_task(self.ensure_pool())
        return slot.workspace_path

    def pop_container_for_workspace(self, workspace_path: Path) -> ContainerSlot | None:
        key = Path(workspace_path)
        return self._assigned.pop(key, None)

    async def retire_container(self, slot: ContainerSlot) -> None:
        await self._run_cmd(["docker", "rm", "-f", slot.container_id])

    async def exec_in_container(
        self,
        slot: ContainerSlot,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        args = ["docker", "exec"]
        args.extend(self._env_args(env))
        args.extend([slot.container_id, *command])
        await self._run_cmd(args)

    async def run_in_fresh_container(
        self,
        workspace_path: Path,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        args = [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,src={workspace_path},dst={self.config.mount_path}",
        ]
        args.extend(self._env_args(self.config.extra_env))
        args.extend(self._env_args(env))
        args.extend([self.config.image, *command])
        await self._run_cmd(args)

    async def cancel_workspace(self, workspace_path: Path) -> int:
        await self._scan_existing()
        target = Path(workspace_path).resolve()
        to_stop: dict[str, ContainerSlot] = {}
        for slot in list(self._idle) + list(self._assigned.values()):
            try:
                slot_path = slot.workspace_path.resolve()
            except OSError:
                slot_path = slot.workspace_path
            if slot_path == target:
                to_stop[slot.container_id] = slot

        ids_raw = await self._run_cmd(
            [
                "docker",
                "ps",
                "--filter",
                f"name=^{self.config.container_prefix}-",
                "--format",
                "{{.ID}}",
            ]
        )
        ids = [line.strip() for line in ids_raw.splitlines() if line.strip()]
        for container_id in ids:
            if container_id in to_stop:
                continue
            info = await self._inspect_container(container_id)
            if not info:
                continue
            mounts = info.get("Mounts") or []
            mount_path = self._mount_source(mounts, self.config.mount_path)
            if not mount_path:
                continue
            try:
                mount_path = mount_path.resolve()
            except OSError:
                pass
            if mount_path == target:
                name = str(info.get("Name", "")).lstrip("/") or container_id
                to_stop[container_id] = ContainerSlot(
                    container_id=container_id,
                    name=name,
                    workspace_path=mount_path,
                )

        if not to_stop:
            return 0

        stopped = 0
        for container_id in list(to_stop.keys()):
            try:
                await self._run_cmd(["docker", "rm", "-f", container_id])
            except RuntimeError:
                continue
            stopped += 1

        self._idle = [slot for slot in self._idle if slot.container_id not in to_stop]
        self._assigned = {
            path: slot
            for path, slot in self._assigned.items()
            if slot.container_id not in to_stop
        }
        return stopped

    async def _start_idle_container(self) -> ContainerSlot:
        workspace_path = self._next_workspace_path()
        workspace_path.mkdir(parents=True, exist_ok=True)
        name = f"{self.config.container_prefix}-{uuid.uuid4().hex[:8]}"
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--mount",
            f"type=bind,src={workspace_path},dst={self.config.mount_path}",
        ]
        args.extend(self._env_args(self.config.extra_env))
        args.extend([self.config.image, "sleep", "infinity"])
        container_id = (await self._run_cmd(args)).strip()
        return ContainerSlot(container_id=container_id, name=name, workspace_path=workspace_path)

    async def _scan_existing(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        ids_raw = await self._run_cmd(
            [
                "docker",
                "ps",
                "--filter",
                f"name=^{self.config.container_prefix}-",
                "--format",
                "{{.ID}}",
            ]
        )
        ids = [line.strip() for line in ids_raw.splitlines() if line.strip()]
        if not ids:
            return
        for container_id in ids:
            info = await self._inspect_container(container_id)
            if not info:
                continue
            name = str(info.get("Name", "")).lstrip("/") or container_id
            mounts = info.get("Mounts") or []
            workspace_path = self._mount_source(mounts, self.config.mount_path)
            if workspace_path is None:
                continue
            if not self._is_within_pool(workspace_path):
                continue
            if workspace_path in self._assigned:
                continue
            if any(slot.workspace_path == workspace_path for slot in self._idle):
                continue
            self._idle.append(
                ContainerSlot(
                    container_id=container_id,
                    name=name,
                    workspace_path=workspace_path,
                )
            )

    async def _inspect_container(self, container_id: str) -> dict[str, Any]:
        output = await self._run_cmd(["docker", "inspect", container_id])
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return {}
        if not data:
            return {}
        return data[0] if isinstance(data, list) else data

    def _mount_source(self, mounts: list[dict[str, Any]], mount_path: str) -> Path | None:
        for mount in mounts:
            if mount.get("Destination") == mount_path:
                source = mount.get("Source")
                if source:
                    return Path(source)
        return None

    def _is_within_pool(self, workspace_path: Path) -> bool:
        try:
            workspace_path.resolve().relative_to(self.config.pool_root.resolve())
            return True
        except ValueError:
            return False

    async def _is_container_running(self, container_id: str) -> bool:
        try:
            output = await self._run_cmd(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_id]
            )
        except RuntimeError:
            return False
        return output.strip().lower() == "true"

    def _next_workspace_path(self) -> Path:
        base = Path(self.config.pool_root)
        base.mkdir(parents=True, exist_ok=True)
        return base / f"slot-{uuid.uuid4().hex}"

    @staticmethod
    async def _run_cmd(args: list[str]) -> str:
        settings = Settings()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        if settings.docker_command_timeout_seconds and settings.docker_command_timeout_seconds > 0:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=settings.docker_command_timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                redacted = DockerPool._redact_args(args)
                raise RuntimeError(
                    f"command timed out after {settings.docker_command_timeout_seconds}s: "
                    f"{' '.join(redacted)}"
                ) from None
        else:
            stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            redacted = DockerPool._redact_args(args)
            raise RuntimeError(
                f"command failed ({proc.returncode}): {' '.join(redacted)}\n{stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    @staticmethod
    def _redact_args(args: list[str]) -> list[str]:
        redacted: list[str] = []
        skip_next = False
        for idx, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if arg in {"-e", "--env"} and idx + 1 < len(args):
                key_val = args[idx + 1]
                key = key_val.split("=", 1)[0]
                redacted.extend([arg, f"{key}=***"])
                skip_next = True
                continue
            redacted.append(arg)
        return redacted

    @staticmethod
    def _env_args(env: dict[str, str] | None) -> list[str]:
        if not env:
            return []
        args: list[str] = []
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        return args

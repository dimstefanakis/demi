from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

from demi.agent.claude import ClaudeAgent
from demi.agent.inflight import InflightTextStream
from demi.config import Settings
from demi.db.factory import build_database
from demi.messaging.file import FileMessenger
from demi.messaging.telegram import TelegramClient, TelegramConfig, TelegramUpdateParser
from demi.models import NormalizedMessage
from demi.workspace.core import WorkspaceManager


class RunLeaseHeartbeat:
    def __init__(
        self,
        *,
        db,
        run_id: int,
        tasks_dir: Path,
        lease_seconds: int,
        poll_interval: float,
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.tasks_dir = tasks_dir
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self._running = True

    async def run(self) -> None:
        last_mtime = self._latest_activity_mtime()
        last_heartbeat = datetime.now(tz=timezone.utc)
        heartbeat_interval = self._heartbeat_interval()
        while self._running:
            await asyncio.sleep(self.poll_interval)
            now = datetime.now(tz=timezone.utc)
            latest = self._latest_activity_mtime()
            activity_changed = latest > last_mtime
            if activity_changed:
                last_mtime = latest
            if activity_changed or (now - last_heartbeat).total_seconds() >= heartbeat_interval:
                lease_expires = (now + timedelta(seconds=self.lease_seconds)).isoformat()
                try:
                    self.db.update_run_lease(
                        self.run_id,
                        lease_expires_at=lease_expires,
                        last_activity_at=now.isoformat() if activity_changed else None,
                        last_heartbeat_at=now.isoformat(),
                    )
                except Exception:
                    pass
                last_heartbeat = now

    def stop(self) -> None:
        self._running = False

    def _latest_activity_mtime(self) -> float:
        paths = [
            self.tasks_dir / "tool_runs.jsonl",
            self.tasks_dir / "agent_events.jsonl",
            self.tasks_dir / "outbound_messages.jsonl",
            self.tasks_dir / "interaction_updates.jsonl",
            self.tasks_dir / "run_result.json",
            self.tasks_dir / "deploy_url.txt",
            self.tasks_dir / "gemini_output.txt",
            self.tasks_dir / "gemini_context.txt",
            self.tasks_dir / "gemini_run.jsonl",
        ]
        latest = 0.0
        for path in paths:
            try:
                if path.exists():
                    latest = max(latest, path.stat().st_mtime)
            except OSError:
                continue
        return latest

    def _heartbeat_interval(self) -> float:
        if self.lease_seconds <= 0:
            return 15.0
        return max(5.0, min(30.0, self.lease_seconds / 4))


async def _pump_inflight_updates(
    tasks_dir: Path,
    inflight_stream: InflightTextStream,
    stop_event: asyncio.Event,
    *,
    db=None,
    run_id: int | None = None,
) -> None:
    path = tasks_dir / "inflight_updates.jsonl"
    while not stop_event.is_set():
        if db is not None and run_id is not None:
            try:
                rows = db.claim_execution_stream_inputs(run_id=run_id, limit=25)
            except Exception:
                rows = []
            for row in rows or []:
                text = str(row.get("text") or "").strip()
                assets = row.get("assets_json") or row.get("assets") or []
                attachments = "\n".join(f"- {item}" for item in assets if item)
                if attachments:
                    if text:
                        text = f"{text}\n\nAttachments:\n{attachments}"
                    else:
                        text = f"Attachments:\n{attachments}"
                if not text:
                    continue
                try:
                    inflight_stream.queue.put_nowait(text)
                except Exception:
                    pass
        snapshot_path = tasks_dir / f".inflight_updates.{uuid.uuid4().hex}.jsonl"
        lines: list[str] = []
        if path.exists():
            try:
                path.replace(snapshot_path)
            except OSError:
                snapshot_path = None
            if snapshot_path is not None:
                try:
                    lines = snapshot_path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                finally:
                    try:
                        snapshot_path.unlink()
                    except OSError:
                        pass
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload_run_id = payload.get("run_id")
            if run_id is not None:
                try:
                    payload_run_id = int(payload_run_id)
                except (TypeError, ValueError):
                    payload_run_id = None
                if payload_run_id != int(run_id):
                    continue
            text = str(payload.get("text") or "").strip()
            assets = payload.get("assets") or []
            attachments = "\n".join(f"- {item}" for item in assets if item)
            if attachments:
                if text:
                    text = f"{text}\n\nAttachments:\n{attachments}"
                else:
                    text = f"Attachments:\n{attachments}"
            if not text:
                continue
            try:
                inflight_stream.queue.put_nowait(text)
            except Exception:
                pass
        await asyncio.sleep(0.05)

_GITHUB_RUNTIME_ENV_KEYS = (
    "GITHUB_TOKEN",
    "GITHUB_REPO_FULL_NAME",
    "GITHUB_REPO_NAME",
    "GITHUB_REPO_OWNER",
    "GITHUB_REPO_URL",
    "GITHUB_REPO_HTTP_URL",
    "GITHUB_REPO_HTML_URL",
    "GITHUB_REPO_SSH_URL",
    "GITHUB_REPO_DEFAULT_BRANCH",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single agent task inside a container.")
    parser.add_argument("--run-id", required=True, type=int, help="Run ID to execute")
    return parser.parse_args()


def _parse_iso_datetime(value: str | None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _load_message_row(message_row: dict, tenant_external_id: str) -> NormalizedMessage:
    provider = str(message_row.get("provider") or "telegram")
    raw = message_row.get("raw_json")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if provider == "telegram" and isinstance(raw, dict):
        parsed = TelegramUpdateParser.parse(raw)
        if parsed is not None:
            if parsed.tenant_external_id != tenant_external_id:
                parsed = NormalizedMessage(
                    provider=parsed.provider,
                    provider_message_id=parsed.provider_message_id,
                    tenant_external_id=tenant_external_id,
                    received_at=parsed.received_at,
                    text=parsed.text,
                    images=parsed.images,
                    raw=parsed.raw,
                )
            return parsed

    received_at = _parse_iso_datetime(str(message_row.get("received_at") or ""))
    text = message_row.get("text")
    return NormalizedMessage(
        provider=provider,
        provider_message_id=str(message_row.get("provider_message_id") or "unknown"),
        tenant_external_id=tenant_external_id,
        received_at=received_at,
        text=text,
        images=[],
        raw=raw or {},
    )


def _resolve_paths(
    *,
    settings: Settings,
    run_row: dict,
    tenant_workspace_path: str | None,
    project_name: str | None,
) -> tuple[Path, Path]:
    mount_root = Path(settings.docker_mount_path or "/workspace")
    raw_task = run_row.get("task_path")
    task_path: Path | None = None
    if raw_task:
        raw_path = Path(str(raw_task))
        if raw_path.is_absolute():
            if tenant_workspace_path:
                try:
                    rel = raw_path.relative_to(Path(tenant_workspace_path))
                except ValueError:
                    rel = None
                if rel is not None:
                    task_path = mount_root / rel
            if task_path is None:
                if str(raw_path).startswith(str(mount_root)):
                    task_path = raw_path
                else:
                    task_path = raw_path
        else:
            task_path = mount_root / raw_path

    project = project_name or "main"
    if task_path is None:
        task_path = mount_root / "projects" / project / "tasks" / "latest.md"

    if task_path.parent.name == "tasks":
        workspace_root = task_path.parent.parent
    else:
        workspace_root = mount_root / "projects" / project
    return workspace_root, task_path


def _load_runtime_env_from_process() -> dict[str, str] | None:
    runtime_env: dict[str, str] = {}
    for key in _GITHUB_RUNTIME_ENV_KEYS:
        value = str(os.getenv(key) or "").strip()
        if value:
            runtime_env[key] = value
    return runtime_env or None


def _write_run_result_error(tasks_dir: Path, exc: Exception) -> None:
    try:
        error_path = tasks_dir / "run_result.json"
        error_payload = {"error": str(exc)}
        error_path.write_text(json.dumps(error_payload, indent=2))
    except OSError:
        return


async def _run(run_id: int) -> int:
    settings = Settings()
    tasks_dir: Path | None = None
    try:
        design_prompt_path = settings.resolved_design_prompt_path()
        if not design_prompt_path.exists():
            raise RuntimeError(f"design_prompt_template_missing:{design_prompt_path}")
        try:
            if design_prompt_path.stat().st_size <= 0:
                raise RuntimeError(f"design_prompt_template_empty:{design_prompt_path}")
        except OSError as exc:
            raise RuntimeError(f"design_prompt_template_unreadable:{design_prompt_path}") from exc

        db = build_database(settings)
        db.init()

        run_row = db.get_run(run_id)
        if not run_row:
            raise RuntimeError("run_not_found")

        project_name = run_row.get("project_name")
        tenant_workspace_path = None
        tenant_id = int(run_row.get("tenant_id") or 0)
        if tenant_id:
            tenant = db.get_tenant_by_id(tenant_id)
            if tenant is not None:
                tenant_workspace_path = getattr(tenant, "workspace_path", None)

        workspace_root, task_path = _resolve_paths(
            settings=settings,
            run_row=run_row,
            tenant_workspace_path=tenant_workspace_path,
            project_name=project_name,
        )
        tenant_root = Path(settings.docker_mount_path or "/workspace")
        workspace_manager = WorkspaceManager(
            root_dir=tenant_root,
            template_root=settings.root_dir,
        )
        workspace = workspace_manager.ensure_workspace_at_path(
            workspace_root,
            tenant_root=tenant_root,
            project_name=project_name,
        )
        tasks_dir = workspace.tasks_dir

        if not tenant_id:
            raise RuntimeError("tenant_missing")
        tenant = db.get_tenant_by_id(tenant_id)
        if tenant is None:
            raise RuntimeError("tenant_missing")

        message_id = int(run_row.get("message_id") or 0)
        if message_id:
            message_row = db.get_message(message_id)
        else:
            message_row = None
        if message_row:
            message = _load_message_row(message_row, tenant.external_id)
        else:
            message = NormalizedMessage(
                provider="event",
                provider_message_id=f"run-{run_id}",
                tenant_external_id=tenant.external_id,
                received_at=datetime.now(tz=timezone.utc),
                text=None,
                images=[],
                raw={},
                project_name=project_name,
            )

        if settings.telegram_bot_token:
            messenger = TelegramClient(TelegramConfig(bot_token=settings.telegram_bot_token))
        else:
            messenger = FileMessenger(tasks_dir=workspace.tasks_dir)

        agent = ClaudeAgent()
        runtime_env = _load_runtime_env_from_process()
        session_id = run_row.get("session_id") or getattr(tenant, "session_id", None)

        monitor = RunLeaseHeartbeat(
            db=db,
            run_id=run_id,
            tasks_dir=workspace.tasks_dir,
            lease_seconds=settings.run_lease_seconds,
            poll_interval=settings.run_activity_poll_interval,
        )
        monitor_task = asyncio.create_task(monitor.run())
        inflight_stream = InflightTextStream(queue=asyncio.Queue())
        inflight_stop = asyncio.Event()
        inflight_task = asyncio.create_task(
            _pump_inflight_updates(
                tasks_dir,
                inflight_stream,
                inflight_stop,
                db=db,
                run_id=run_id,
            )
        )
        try:
            result = await agent.prepare_context(
                workspace=workspace,
                task_path=task_path,
                message=message,
                messenger=messenger,
                inflight_stream=inflight_stream,
                tenant_id=tenant.id,
                db=db,
                payments=None,
                session_id=session_id,
                run_id=run_id,
                runtime_env=runtime_env,
            )

            result_path = workspace.tasks_dir / "run_result.json"
            result_payload = asdict(result)
            result_path.write_text(json.dumps(result_payload, indent=2))
            return 0
        finally:
            inflight_stop.set()
            inflight_task.cancel()
            monitor.stop()
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            with contextlib.suppress(asyncio.CancelledError):
                await inflight_task
    except Exception as exc:  # noqa: BLE001
        if tasks_dir is not None:
            _write_run_result_error(tasks_dir, exc)
        raise


def main() -> None:
    args = _parse_args()
    try:
        exit_code = asyncio.run(_run(int(args.run_id)))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

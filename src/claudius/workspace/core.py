from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import shutil


@dataclass
class Workspace:
    root: Path
    tasks_dir: Path
    assets_dir: Path
    site_dir: Path
    memory_path: Path

    def write_task(self, content: str) -> Path:
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        task_path = self.tasks_dir / f"task-{timestamp}.md"
        task_path.write_text(content)
        latest = self.tasks_dir / "latest.md"
        latest.write_text(content)
        return task_path


class WorkspaceManager:
    def __init__(self, root_dir: Path, template_root: Path | None = None):
        self.root_dir = Path(root_dir)
        self.template_root = Path(template_root) if template_root else None

    def ensure_workspace(self, tenant_key: str) -> Workspace:
        root = self.root_dir / tenant_key
        return self.ensure_workspace_at_path(root)

    def ensure_workspace_at_path(self, root: Path) -> Workspace:
        root = Path(root)
        tasks_dir = root / "tasks"
        assets_dir = root / "assets"
        site_dir = root / "site"
        memory_path = root / "memory.md"

        tasks_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
        site_dir.mkdir(parents=True, exist_ok=True)
        if self.template_root:
            self._sync_project_files(root)
        if not memory_path.exists():
            memory_path.write_text("# Memory\n\n")
        return Workspace(
            root=root,
            tasks_dir=tasks_dir,
            assets_dir=assets_dir,
            site_dir=site_dir,
            memory_path=memory_path,
        )

    def _sync_project_files(self, workspace_root: Path) -> None:
        if not self.template_root:
            return

        claude_dir = self.template_root / ".claude"
        if claude_dir.exists():
            target = workspace_root / ".claude"
            if not target.exists():
                shutil.copytree(claude_dir, target)

        claude_md = self.template_root / "CLAUDE.md"
        if claude_md.exists():
            target = workspace_root / "CLAUDE.md"
            if not target.exists():
                shutil.copy2(claude_md, target)

        design_md = self.template_root / "DESIGN.md"
        if design_md.exists():
            target = workspace_root / "DESIGN.md"
            if not target.exists():
                shutil.copy2(design_md, target)

        env_example = self.template_root / ".env.example"
        if env_example.exists():
            target = workspace_root / ".env.example"
            if not target.exists():
                shutil.copy2(env_example, target)

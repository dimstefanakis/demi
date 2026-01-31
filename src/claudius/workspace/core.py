from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil


DEFAULT_PROJECT_NAME = "main"
PROJECTS_DIRNAME = "projects"
ACTIVE_PROJECT_FILE = "active.txt"


@dataclass
class Workspace:
    root: Path
    tenant_root: Path
    project_name: str
    tasks_dir: Path
    assets_dir: Path
    site_dir: Path
    memory_path: Path
    description_path: Path

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

    def ensure_workspace(self, tenant_key: str, project_name: str | None = None) -> Workspace:
        tenant_root = self._tenant_root(tenant_key)
        selected = self._resolve_project_name(tenant_root, project_name)
        return self.ensure_project(tenant_root, selected)

    def ensure_project_for_tenant_root(
        self, tenant_root: Path, project_name: str | None = None
    ) -> Workspace:
        selected = self._resolve_project_name(tenant_root, project_name)
        return self.ensure_project(tenant_root, selected)

    def ensure_project(self, tenant_root: Path, project_name: str | None = None) -> Workspace:
        tenant_root = Path(tenant_root)
        selected = self._normalize_project_name(project_name or DEFAULT_PROJECT_NAME)
        projects_dir = tenant_root / PROJECTS_DIRNAME
        project_root = projects_dir / selected
        self._migrate_legacy_layout(tenant_root, project_root)
        return self.ensure_workspace_at_path(
            project_root,
            tenant_root=tenant_root,
            project_name=selected,
        )

    def ensure_workspace_at_path(
        self,
        root: Path,
        tenant_root: Path | None = None,
        project_name: str | None = None,
    ) -> Workspace:
        root = Path(root)
        inferred_tenant_root = tenant_root or self._infer_tenant_root(root)
        inferred_project_name = project_name or root.name

        tasks_dir = root / "tasks"
        assets_dir = root / "assets"
        site_dir = root / "site"
        memory_path = root / "memory.md"
        description_path = root / "DESCRIPTION.md"

        tasks_dir.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
        site_dir.mkdir(parents=True, exist_ok=True)
        if self.template_root:
            self._sync_project_files(root)
        if not memory_path.exists():
            memory_path.write_text("# Memory\n\n")
        if not description_path.exists():
            description_path.write_text("# Project Description\n\n")
        return Workspace(
            root=root,
            tenant_root=inferred_tenant_root,
            project_name=inferred_project_name,
            tasks_dir=tasks_dir,
            assets_dir=assets_dir,
            site_dir=site_dir,
            memory_path=memory_path,
            description_path=description_path,
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

    def _tenant_root(self, tenant_key: str) -> Path:
        root = self.root_dir / tenant_key
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolve_project_name(self, tenant_root: Path, project_name: str | None) -> str:
        if project_name:
            selected = self._normalize_project_name(project_name)
            self._set_active_project(tenant_root, selected)
            return selected
        active = self._get_active_project(tenant_root)
        if active:
            return active
        self._set_active_project(tenant_root, DEFAULT_PROJECT_NAME)
        return DEFAULT_PROJECT_NAME

    def _get_active_project(self, tenant_root: Path) -> str | None:
        path = Path(tenant_root) / PROJECTS_DIRNAME / ACTIVE_PROJECT_FILE
        if not path.exists():
            return None
        try:
            value = path.read_text().strip()
        except OSError:
            return None
        return self._normalize_project_name(value) if value else None

    def _set_active_project(self, tenant_root: Path, project_name: str) -> None:
        projects_dir = Path(tenant_root) / PROJECTS_DIRNAME
        projects_dir.mkdir(parents=True, exist_ok=True)
        path = projects_dir / ACTIVE_PROJECT_FILE
        try:
            path.write_text(project_name.strip() + "\n")
        except OSError:
            pass

    @staticmethod
    def _normalize_project_name(name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
        return cleaned or DEFAULT_PROJECT_NAME

    def normalize_project_name(self, name: str) -> str:
        return self._normalize_project_name(name)

    def infer_tenant_root(self, project_root: Path) -> Path:
        return self._infer_tenant_root(Path(project_root))

    def tenant_root_for_key(self, tenant_key: str) -> Path:
        return self._tenant_root(tenant_key)

    def get_active_project(self, tenant_root: Path) -> str | None:
        return self._get_active_project(Path(tenant_root))

    def set_active_project(self, tenant_root: Path, project_name: str) -> None:
        self._set_active_project(Path(tenant_root), self._normalize_project_name(project_name))

    @staticmethod
    def _infer_tenant_root(project_root: Path) -> Path:
        if project_root.parent.name == PROJECTS_DIRNAME:
            return project_root.parent.parent
        return project_root

    def _migrate_legacy_layout(self, tenant_root: Path, project_root: Path) -> None:
        tenant_root = Path(tenant_root)
        project_root = Path(project_root)
        legacy_items = [
            "tasks",
            "assets",
            "site",
            "memory.md",
            "DESCRIPTION.md",
            "tenant.sqlite",
            ".claude",
            "CLAUDE.md",
            "DESIGN.md",
            ".env.example",
        ]
        has_legacy = any((tenant_root / item).exists() for item in legacy_items)
        if not has_legacy:
            return
        project_root.mkdir(parents=True, exist_ok=True)
        for item in legacy_items:
            src = tenant_root / item
            if not src.exists():
                continue
            dest = project_root / item
            if dest.exists():
                continue
            shutil.move(str(src), str(dest))

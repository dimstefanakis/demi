#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from demi.config import Settings
from demi.db.factory import build_database


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _path_in_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _running_pool_container_ids(prefix: str) -> list[str]:
    result = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"name=^{prefix}-",
            "--format",
            "{{.ID}}",
        ]
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _mounted_pool_paths(pool_root: Path) -> set[Path]:
    ids_result = _run(["docker", "ps", "-q"])
    if ids_result.returncode != 0:
        return set()
    ids = [line.strip() for line in ids_result.stdout.splitlines() if line.strip()]
    if not ids:
        return set()
    inspect_result = _run(["docker", "inspect", *ids])
    if inspect_result.returncode != 0:
        return set()
    try:
        containers = json.loads(inspect_result.stdout)
    except json.JSONDecodeError:
        return set()
    keep: set[Path] = set()
    for container in containers:
        mounts = container.get("Mounts") or []
        for mount in mounts:
            source = mount.get("Source")
            if not source:
                continue
            source_path = Path(source).resolve()
            if _path_in_root(source_path, pool_root):
                keep.add(source_path)
    return keep


def _tenant_pool_paths(settings: Settings, pool_root: Path) -> tuple[set[Path], str | None]:
    try:
        db = build_database(settings)
        db.init()
        tenants = db.list_tenants()
    except Exception as exc:  # noqa: BLE001
        return set(), str(exc)

    keep: set[Path] = set()
    for tenant in tenants:
        workspace_path = getattr(tenant, "workspace_path", None)
        if not workspace_path:
            continue
        path = Path(str(workspace_path)).expanduser().resolve()
        if _path_in_root(path, pool_root):
            keep.add(path)
    return keep, None


def _remove_pool_containers(prefix: str) -> int:
    ids = _running_pool_container_ids(prefix)
    removed = 0
    for container_id in ids:
        result = _run(["docker", "rm", "-f", container_id])
        if result.returncode == 0:
            removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove stale Docker pool workspace directories."
    )
    parser.add_argument(
        "--stop-pool-containers",
        action="store_true",
        help="Stop and remove running pool containers before pruning folders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stale workspace folders without deleting them.",
    )
    args = parser.parse_args()

    settings = Settings()
    pool_root = (settings.root_dir / settings.docker_pool_root).resolve()
    if not pool_root.exists():
        print(f"Pool root not found: {pool_root}")
        return 0

    if args.stop_pool_containers:
        removed = _remove_pool_containers("demi-pool")
        print(f"Removed running pool containers: {removed}")

    tenant_keep, tenant_error = _tenant_pool_paths(settings, pool_root)
    if tenant_error:
        print(f"Skipping cleanup: tenant workspace lookup failed ({tenant_error})")
        return 0

    mounted_keep = _mounted_pool_paths(pool_root)
    keep = tenant_keep | mounted_keep

    stale_paths: list[Path] = []
    for child in pool_root.iterdir():
        if not child.exists():
            continue
        child_resolved = child.resolve()
        if not _path_in_root(child_resolved, pool_root):
            continue
        if child_resolved in keep:
            continue
        stale_paths.append(child_resolved)

    if not stale_paths:
        print("No stale pool workspaces found.")
        return 0

    if args.dry_run:
        for path in stale_paths:
            print(f"stale: {path}")
        return 0

    removed_dirs = 0
    removed_files = 0
    for path in stale_paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
            removed_dirs += 1
        else:
            path.unlink(missing_ok=True)
            removed_files += 1

    print(
        f"Removed stale pool paths: dirs={removed_dirs} files={removed_files} "
        f"kept={len(keep)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

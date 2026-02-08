from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from demi.config import Settings


TOOLING_LOCK_VERSION = 1
_RANGE_TOKEN_PATTERN = re.compile(r"[~^*<>]|x|\|", flags=re.IGNORECASE)


@dataclass(frozen=True)
class ToolingBootstrapResult:
    tooling_root: Path
    lock_path: Path
    bin_path: Path | None
    packages: dict[str, str]
    installed: bool


def bootstrap_tenant_tooling(*, tenant_root: Path, settings: Settings) -> ToolingBootstrapResult:
    tooling_root = Path(tenant_root) / settings.tenant_tooling_dirname
    tooling_root.mkdir(parents=True, exist_ok=True)
    lock_path = Path(tenant_root) / settings.tenant_tooling_lock_file
    package_json_path = tooling_root / "package.json"
    bun_lock_path = tooling_root / "bun.lock"
    node_modules_path = tooling_root / "node_modules"
    bin_path = tooling_root / "node_modules" / ".bin"

    if not settings.tenant_tooling_enabled:
        return ToolingBootstrapResult(
            tooling_root=tooling_root,
            lock_path=lock_path,
            bin_path=bin_path if bin_path.exists() else None,
            packages={},
            installed=False,
        )

    existing_lock = _read_lock(lock_path)
    lock_packages = _lock_packages(existing_lock)
    configured_packages = _parse_packages(settings.tenant_tooling_packages)
    packages = lock_packages or configured_packages

    package_payload = _build_package_json(packages)
    package_changed = _write_if_changed(package_json_path, package_payload)

    lock_checksums = _lock_checksums(existing_lock)
    package_checksum = _sha256_file(package_json_path)
    bun_lock_checksum = _sha256_file(bun_lock_path)
    checksums_match = (
        package_checksum
        and bun_lock_checksum
        and lock_checksums.get("package_json") == package_checksum
        and lock_checksums.get("bun_lock") == bun_lock_checksum
    )

    should_install = bool(packages) and (
        package_changed
        or not bun_lock_path.exists()
        or not node_modules_path.exists()
        or not checksums_match
    )

    installed = False
    if should_install:
        use_frozen = bun_lock_path.exists() and not package_changed
        _run_bun_install(tooling_root=tooling_root, frozen=use_frozen)
        installed = True
        bun_lock_checksum = _sha256_file(bun_lock_path)
        package_checksum = _sha256_file(package_json_path)

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    generated_at = now_iso
    if not should_install and not package_changed and existing_lock:
        existing_generated_at = existing_lock.get("generated_at")
        if isinstance(existing_generated_at, str) and existing_generated_at.strip():
            generated_at = existing_generated_at.strip()

    lock_payload = {
        "version": TOOLING_LOCK_VERSION,
        "generated_at": generated_at,
        "packages": packages,
        "checksums": {
            "package_json": package_checksum,
            "bun_lock": bun_lock_checksum,
        },
        "bins": _discover_bins(bin_path),
    }
    _write_if_changed(lock_path, json.dumps(lock_payload, indent=2, ensure_ascii=True) + "\n")

    return ToolingBootstrapResult(
        tooling_root=tooling_root,
        lock_path=lock_path,
        bin_path=bin_path if bin_path.exists() else None,
        packages=packages,
        installed=installed,
    )


def _read_lock(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _lock_packages(payload: dict[str, object] | None) -> dict[str, str]:
    if not payload:
        return {}
    raw = payload.get("packages")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name = key.strip()
        version = value.strip()
        if not name or not version:
            continue
        if not _is_pinned_version(version):
            continue
        normalized[name] = version
    return dict(sorted(normalized.items()))


def _lock_checksums(payload: dict[str, object] | None) -> dict[str, str]:
    if not payload:
        return {}
    raw = payload.get("checksums")
    if not isinstance(raw, dict):
        return {}
    checksums: dict[str, str] = {}
    for key in ("package_json", "bun_lock"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            checksums[key] = value.strip()
    return checksums


def _parse_packages(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    text = raw.strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            result: dict[str, str] = {}
            for key, value in payload.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                name = key.strip()
                version = value.strip()
                if not name or not version or not _is_pinned_version(version):
                    continue
                result[name] = version
            return dict(sorted(result.items()))

    if text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            specs = [entry for entry in payload if isinstance(entry, str)]
            return _parse_package_specs(specs)

    specs = [part.strip() for part in text.split(",") if part.strip()]
    return _parse_package_specs(specs)


def _parse_package_specs(specs: list[str]) -> dict[str, str]:
    packages: dict[str, str] = {}
    for spec in specs:
        name, version = _split_spec(spec)
        if not name or not version:
            continue
        if not _is_pinned_version(version):
            continue
        packages[name] = version
    return dict(sorted(packages.items()))


def _split_spec(spec: str) -> tuple[str, str | None]:
    text = spec.strip()
    if not text:
        return "", None
    if text.startswith("@"):
        idx = text.rfind("@")
        if idx <= 0:
            return text, None
        return text[:idx], text[idx + 1 :]
    idx = text.rfind("@")
    if idx < 0:
        return text, None
    return text[:idx], text[idx + 1 :]


def _is_pinned_version(version: str) -> bool:
    value = version.strip().lower()
    if not value:
        return False
    if value in {"latest", "next", "canary"}:
        return False
    return _RANGE_TOKEN_PATTERN.search(value) is None


def _build_package_json(packages: dict[str, str]) -> str:
    payload = {
        "name": "demi-tenant-tooling",
        "private": True,
        "packageManager": "bun@1",
        "dependencies": packages,
    }
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _run_bun_install(*, tooling_root: Path, frozen: bool) -> None:
    command = ["bun", "install", "--cwd", str(tooling_root), "--production"]
    if frozen:
        command.append("--frozen-lockfile")
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    if completed.returncode == 0:
        return
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    detail = stderr or stdout or "unknown_error"
    raise RuntimeError(f"tenant_tooling_install_failed: {detail}")


def _discover_bins(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    names = []
    for child in sorted(path.iterdir(), key=lambda entry: entry.name):
        if child.name.startswith("."):
            continue
        names.append(child.name)
    return names

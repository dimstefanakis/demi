from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from claudius.config import Settings


@dataclass(frozen=True)
class VercelEnvResult:
    name: str
    success: bool
    output: str


def set_env_var(
    project_dir: Path,
    name: str,
    value: str,
    settings: Settings,
    target: str = "production",
    sensitive: bool = False,
) -> VercelEnvResult:
    cmd = [settings.resolved_vercel_cmd(), "env", "add", name, target, "--force"]
    if sensitive:
        cmd.append("--sensitive")
    if settings.vercel_token:
        cmd.extend(["--token", settings.vercel_token])
    if settings.vercel_scope:
        cmd.extend(["--scope", settings.vercel_scope])
    completed = subprocess.run(
        cmd,
        input=f"{value}\n",
        text=True,
        capture_output=True,
        check=False,
        cwd=project_dir,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    output = "\n".join([stdout, stderr]).strip()
    return VercelEnvResult(name=name, success=completed.returncode == 0, output=output)


def set_env_vars(
    project_dir: Path,
    values: dict[str, str],
    settings: Settings,
    target: str = "production",
) -> list[VercelEnvResult]:
    results: list[VercelEnvResult] = []
    for name, value in values.items():
        sensitive = _is_sensitive_env(name)
        results.append(
            set_env_var(
                project_dir=project_dir,
                name=name,
                value=value,
                settings=settings,
                target=target,
                sensitive=sensitive,
            )
        )
    return results


def _is_sensitive_env(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("SECRET", "SERVICE_ROLE", "PRIVATE", "TOKEN"))

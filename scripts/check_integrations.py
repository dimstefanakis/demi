#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import argparse
import sys
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

try:  # optional: script can run without local deps
    from demi.config import Settings  # type: ignore
    from demi.runtime.docker_agent import DockerAgent  # type: ignore
except Exception:  # noqa: BLE001
    Settings = None  # type: ignore[assignment]
    DockerAgent = None  # type: ignore[assignment]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        os.environ.setdefault(key, value)


def _present(value: str | None) -> bool:
    return bool(value and str(value).strip())


def _status(value: bool) -> str:
    return "OK" if value else "MISSING"


def _run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


@dataclass(frozen=True)
class IntegrationCheck:
    name: str
    keys: tuple[str, ...]
    required: tuple[str, ...] | None = None

    def evaluate(self, settings: object | None) -> tuple[bool, list[str]]:
        missing: list[str] = []
        required = self.required or self.keys
        for key in required:
            value = os.getenv(key)
            if not _present(value):
                if settings is not None:
                    value = getattr(settings, key.lower(), None)
            if not _present(str(value) if value is not None else ""):
                missing.append(key)
        return len(missing) == 0, missing


def _settings_value(settings: object | None, attr: str) -> str | None:
    if settings is None:
        return None
    return getattr(settings, attr, None)


def _print_section(title: str) -> None:
    print("\n" + title)
    print("-" * len(title))


def _extract_allowlist_fallback() -> list[str]:
    path = SRC_DIR / "demi" / "runtime" / "docker_agent.py"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    allowlist: list[str] = []
    in_block = False
    for line in lines:
        if "def _env_allowlist" in line:
            in_block = True
            continue
        if in_block and line.strip().startswith("return ["):
            # start collecting after this line
            continue
        if in_block:
            if "]" in line:
                break
            for token in line.split("\""):
                if token and token.strip() and token.strip().isupper():
                    allowlist.append(token.strip())
    return allowlist


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ci", action="store_true", help="fail on missing core integrations")
    args = parser.parse_args()

    _load_env_file(Path(".env"))
    settings = Settings() if Settings else None

    _print_section("Core Integrations")
    checks = [
        IntegrationCheck("Telegram", ("TELEGRAM_BOT_TOKEN",)),
        IntegrationCheck(
            "Main DB (Supabase)",
            ("MAIN_DB_SUPABASE_URL", "MAIN_DB_SUPABASE_SERVICE_KEY"),
        ),
        IntegrationCheck(
            "Vercel", ("VERCEL_TOKEN",), required=("VERCEL_TOKEN",)
        ),
        IntegrationCheck(
            "Gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), required=("GEMINI_API_KEY",)
        ),
        IntegrationCheck(
            "Unsplash",
            ("UNSPLASH_ACCESS_KEY", "UNSPLASH_SECRET_KEY", "UNSPLASH_APP_ID"),
            required=("UNSPLASH_ACCESS_KEY",),
        ),
        IntegrationCheck(
            "Supabase Provisioning",
            ("SUPABASE_ACCESS_TOKEN", "SUPABASE_ORG_SLUG", "SUPABASE_ORG_ID"),
            required=("SUPABASE_ACCESS_TOKEN",),
        ),
        IntegrationCheck(
            "GitHub App",
            (
                "GITHUB_ORG",
                "GITHUB_APP_ID",
                "GITHUB_APP_CLIENT_ID",
                "GITHUB_APP_INSTALLATION_ID",
                "GITHUB_APP_PRIVATE_KEY",
            ),
            required=("GITHUB_ORG", "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID"),
        ),
        IntegrationCheck(
            "Stripe",
            (
                "STRIPE_SECRET_KEY",
                "STRIPE_WEBHOOK_SECRET",
                "STRIPE_SUCCESS_URL",
                "STRIPE_CANCEL_URL",
            ),
            required=("STRIPE_SECRET_KEY",),
        ),
    ]

    missing_required: list[str] = []
    for check in checks:
        ok, missing = check.evaluate(settings)
        status = _status(ok)
        detail = "" if ok else f" (missing: {', '.join(missing)})"
        print(f"{check.name}: {status}{detail}")
        if not ok:
            missing_required.extend(missing)

    _print_section("Agent Container Env Allowlist")
    if DockerAgent and settings is not None:
        agent = DockerAgent(pool=None, settings=settings)  # type: ignore[arg-type]
        allowlist = agent._env_allowlist()
    else:
        allowlist = _extract_allowlist_fallback()
    configured = [
        key
        for key in allowlist
        if _present(os.getenv(key))
        or _present(str(_settings_value(settings, key.lower()) or ""))
    ]
    missing_allowlist = [key for key in allowlist if key not in configured]
    print(f"Forwarded keys configured: {len(configured)} / {len(allowlist)}")
    if missing_allowlist:
        print("Not configured:")
        for key in missing_allowlist:
            print(f"- {key}")

    _print_section("Agent Image CLI Checks")
    default_image = "demi-agent:local"
    if settings is not None:
        default_image = settings.docker_image
    image = os.getenv("DOCKER_IMAGE", default_image)
    cli_checks = {
        "gemini": ["gemini", "--version"],
        "vercel": ["vercel", "--version"],
        "supabase": ["supabase", "--version"],
        "supabase_bootstrap_help": ["supabase", "bootstrap", "--help"],
        "bun": ["bun", "--version"],
        "node": ["node", "--version"],
        "git": ["git", "--version"],
        "curl": ["curl", "--version"],
        "uv": ["uv", "--version"],
    }
    cli_failures: list[str] = []
    for name, cmd in cli_checks.items():
        code, output = _run(["docker", "run", "--rm", image, *cmd])
        status = _status(code == 0)
        line = output.splitlines()[0] if output else ""
        print(f"{name}: {status} {line}")
        if code != 0:
            cli_failures.append(name)

    if args.ci:
        if missing_required or cli_failures:
            print("\nCI checks failed.")
            if missing_required:
                print(
                    f"Missing required env: {', '.join(sorted(set(missing_required)))}"
                )
            if cli_failures:
                print(f"CLI failures: {', '.join(cli_failures)}")
            raise SystemExit(1)

    # Optional: Supabase CLI auth smoke test (read-only).
    if _present(os.getenv("SUPABASE_ACCESS_TOKEN")):
        _print_section("Supabase CLI Auth Check")
        code, output = _run(
            [
                "docker",
                "run",
                "--rm",
                "--env-file",
                ".env",
                image,
                "sh",
                "-lc",
                "supabase projects list --output json",
            ]
        )
        status = _status(code == 0)
        line = output.splitlines()[0] if output else ""
        print(f"supabase projects list: {status} {line}")
        if args.ci and code != 0:
            raise SystemExit(1)

    # Optional: Supabase API smoke test (tenant provisioning integration).
    if _present(os.getenv("SUPABASE_ACCESS_TOKEN")):
        _print_section("Supabase API Check")
        base_url = os.getenv("SUPABASE_API_BASE_URL") or "https://api.supabase.com"
        url = base_url.rstrip("/") + "/v1/projects"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {os.getenv('SUPABASE_ACCESS_TOKEN')}",
                "User-Agent": "curl/8.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                status = response.status
            ok = 200 <= status < 300
            print(f"GET {url}: {_status(ok)} (status {status})")
            if args.ci and not ok:
                raise SystemExit(1)
        except urllib.error.HTTPError as exc:
            print(f"GET {url}: MISSING (status {exc.code})")
            if args.ci:
                raise SystemExit(1)
        except Exception as exc:  # noqa: BLE001
            print(f"GET {url}: MISSING ({type(exc).__name__})")
            if args.ci:
                raise SystemExit(1)


if __name__ == "__main__":
    main()

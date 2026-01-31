from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    root_dir: Path = Path(".")
    data_dir: Path = Path("data")
    db_path: Path | None = None
    main_db_backend: str = "sqlite"
    main_db_supabase_url: str | None = None
    main_db_supabase_service_key: str | None = None

    telegram_bot_token: str | None = None

    gemini_cmd: str = "gemini"
    design_prompt_path: Path = Path("DESIGN.md")
    claude_prompt_path: Path = Path("prompts/claude_agent.md")
    interaction_prompt_path: Path = Path("prompts/interaction_agent.md")

    vercel_cmd: str = "vercel"
    vercel_token: str | None = None
    vercel_scope: str | None = None

    github_org: str | None = None
    github_app_id: str | None = None
    github_app_client_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = None
    github_repo_prefix: str = "claudius"
    github_repo_visibility: str = "private"
    github_api_base_url: str = "https://api.github.com"

    public_base_url: str | None = None

    supabase_access_token: str | None = None
    supabase_org_slug: str | None = None
    supabase_org_id: str | None = None
    supabase_region_selection: str = "americas"
    supabase_region: str | None = None
    supabase_instance_size: str = "micro"
    supabase_project_prefix: str = "claudius"
    supabase_api_base_url: str = "https://api.supabase.com"

    agent_runtime: str = "local"
    docker_image: str = "claudius-agent:local"
    docker_pool_size: int = 1
    docker_pool_root: Path = Path("data/pool")
    docker_mount_path: str = "/workspace"
    docker_env_allowlist: str | None = None
    docker_forward_messages: bool = False
    docker_command_timeout_seconds: float = 0.0
    docker_pool_warm_timeout_seconds: float = 45.0

    anthropic_api_key: str | None = None
    claude_api_key: str | None = None
    gemini_api_key: str | None = None
    google_api_key: str | None = None

    events_signing_secret: str | None = None
    events_require_signature: bool = True
    events_worker_enabled: bool = True
    events_worker_poll_interval: float = 1.5
    events_worker_batch_size: int = 20
    event_url: str | None = None

    pending_worker_enabled: bool = True
    pending_worker_poll_interval: float = 2.5
    pending_worker_batch_size: int = 20

    run_lease_seconds: int = 600
    run_activity_poll_interval: float = 2.5

    sqlite_timeout_seconds: float = 0.5
    sqlite_busy_timeout_ms: int = 500
    sqlite_journal_mode: str = "WAL"
    sqlite_synchronous: str = "NORMAL"

    unsplash_app_id: str | None = None
    unsplash_access_key: str | None = None
    unsplash_secret_key: str | None = None

    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url: str | None = None
    stripe_cancel_url: str | None = None

    def resolved_data_dir(self) -> Path:
        return (self.root_dir / self.data_dir).resolve()

    def resolved_db_path(self) -> Path:
        if self.db_path:
            return self.db_path
        return self.resolved_data_dir() / "claudius.sqlite"

    def resolved_gemini_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "gemini").resolve()
        return str(local_cmd) if local_cmd.exists() else self.gemini_cmd

    def resolved_vercel_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "vercel").resolve()
        return str(local_cmd) if local_cmd.exists() else self.vercel_cmd

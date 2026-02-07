from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    root_dir: Path = Path(".")
    data_dir: Path = Path("data")
    main_db_supabase_url: str | None = None
    main_db_supabase_service_key: str | None = None

    telegram_bot_token: str | None = None

    gemini_cmd: str = "gemini"
    design_prompt_path: Path = Path("docs/DESIGN.md")
    claude_prompt_path: Path = Path("prompts/claude_agent.md")
    interaction_prompt_path: Path = Path("prompts/interaction_agent.md")
    interaction_router_prompt_path: Path = Path("prompts/interaction_agent.md")
    execution_model: str = "claude-sonnet-4-5-20250929"
    interaction_model: str = "claude-opus-4-6"
    interaction_max_thinking_tokens: int | None = 4096
    interaction_session_cache_dir: Path = Path("data/interaction_sessions")

    chrome_devtools_mcp_enabled: bool = False
    chrome_devtools_mcp_profile_dir: Path = Path("chrome_profiles")
    chrome_devtools_mcp_command: str = "bunx"
    chrome_devtools_mcp_package: str = "chrome-devtools-mcp@latest"
    chrome_devtools_mcp_headless: bool = True
    chrome_devtools_mcp_executable_path: str | None = None

    vercel_cmd: str = "vercel"
    vercel_token: str | None = None
    vercel_scope: str | None = None

    github_org: str | None = None
    github_app_id: str | None = None
    github_app_client_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = None
    github_repo_prefix: str = ""
    github_repo_visibility: str = "private"
    github_api_base_url: str = "https://api.github.com"

    public_base_url: str | None = None

    supabase_access_token: str | None = None
    supabase_org_slug: str | None = None
    supabase_org_id: str | None = None
    supabase_region_selection: str = "americas"
    supabase_region: str | None = None
    supabase_instance_size: str = "micro"
    supabase_project_prefix: str = "site"
    supabase_api_base_url: str = "https://api.supabase.com"

    agent_runtime: str = "local"
    docker_image: str = "demi-agent:local"
    docker_pool_size: int = 0
    docker_pool_root: Path = Path("data/pool")
    docker_mount_path: str = "/workspace"
    # NOTE: all secrets are considered safe for agents to consume in this runtime.
    docker_env_allowlist: str | None = "*"
    docker_forward_messages: bool = False
    docker_command_timeout_seconds: float = 1800.0
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

    outbox_worker_enabled: bool = True
    outbox_worker_poll_interval: float = 1.0
    outbox_worker_batch_size: int = 50

    run_lease_seconds: int = 600
    run_activity_poll_interval: float = 2.5

    unsplash_app_id: str | None = None
    unsplash_access_key: str | None = None
    unsplash_secret_key: str | None = None

    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url: str | None = None
    stripe_cancel_url: str | None = None

    billing_status_url: str | None = None
    billing_status_token: str | None = None
    billing_status_timeout_seconds: float = 3.0

    admin_api_token: str | None = None

    assistant_stripe_price_id: str | None = None
    assistant_product_name: str = "Hire me"
    assistant_price_usd: float | None = None
    assistant_currency: str = "USD"
    assistant_usage_threshold_usd: float | None = 3.0

    def resolved_data_dir(self) -> Path:
        return (self.root_dir / self.data_dir).resolve()

    def resolved_gemini_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "gemini").resolve()
        return str(local_cmd) if local_cmd.exists() else self.gemini_cmd

    def resolved_design_prompt_path(self) -> Path:
        path = self.design_prompt_path
        if path.is_absolute():
            return path
        return (self.root_dir / path).resolve()

    def resolved_interaction_session_cache_dir(self) -> Path:
        path = self.interaction_session_cache_dir
        if path.is_absolute():
            return path
        return (self.root_dir / path).resolve()

    def resolved_vercel_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "vercel").resolve()
        return str(local_cmd) if local_cmd.exists() else self.vercel_cmd

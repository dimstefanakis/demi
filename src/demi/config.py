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
    planner_prompt_path: Path = Path("prompts/planner_agent.md")
    product_designer_prompt_path: Path = Path("prompts/product_designer_agent.md")
    software_engineer_prompt_path: Path = Path("prompts/software_engineer_agent.md")
    devops_engineer_prompt_path: Path = Path("prompts/devops_engineer_agent.md")
    reviewer_prompt_path: Path = Path("prompts/reviewer_agent.md")
    interaction_agent_routing_prompt_path: Path | None = None
    # Legacy alias kept for backward compatibility.
    interaction_router_prompt_path: Path | None = None
    interaction_helper_prompt_path: Path = Path("prompts/interaction_helper.md")
    execution_model: str = "claude-sonnet-4-5-20250929"
    execution_max_thinking_tokens: int | None = 2048
    interaction_model: str = "claude-opus-4-6"
    interaction_max_thinking_tokens: int | None = 4096
    interaction_agent_routing_max_retries: int | None = None
    # Legacy alias kept for backward compatibility.
    interaction_router_max_retries: int | None = None
    interaction_agent_routing_max_cost_usd: float | None = 0.25
    # Legacy alias kept for backward compatibility.
    interaction_router_max_cost_usd: float | None = None
    interaction_session_cache_dir: Path = Path("data/interaction_sessions")
    claude_enable_tool_search: bool = True
    claude_enable_memory_tool: bool = True
    agent_email: str | None = None

    chrome_devtools_mcp_enabled: bool = False
    chrome_devtools_mcp_profile_dir: Path = Path("chrome_profiles")
    chrome_devtools_mcp_command: str = "bunx"
    chrome_devtools_mcp_package: str = "chrome-devtools-mcp@latest"
    chrome_devtools_mcp_headless: bool = True
    chrome_devtools_mcp_executable_path: str | None = None

    vercel_cmd: str = "vercel"
    vercel_token: str | None = None
    vercel_scope: str | None = None

    firecrawl_cmd: str = "firecrawl"

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
    # Hard timeout for long-running container commands (agent execution via `docker run` and
    # `docker exec`).
    # Set to 0 to disable and rely on run leases/heartbeats instead.
    docker_container_command_timeout_seconds: float = 0.0
    docker_command_timeout_seconds: float = 900.0
    docker_pool_warm_timeout_seconds: float = 45.0
    tenant_tooling_enabled: bool = True
    tenant_tooling_packages: str | None = None
    tenant_tooling_dirname: str = "tooling"
    tenant_tooling_lock_file: str = "tooling.lock"

    anthropic_api_key: str | None = None
    claude_api_key: str | None = None
    gemini_api_key: str | None = None
    google_api_key: str | None = None
    lmnr_project_api_key: str | None = None

    events_signing_secret: str | None = None
    events_require_signature: bool = True
    events_worker_enabled: bool = True
    events_worker_poll_interval: float = 1.5
    events_worker_batch_size: int = 20
    event_url: str | None = None
    # In production blue/green deployments, disable embedded workers on API
    # containers and run them only in the dedicated worker process.
    api_embedded_workers_enabled: bool = True

    pending_worker_enabled: bool = True
    pending_worker_poll_interval: float = 2.5
    pending_worker_batch_size: int = 20
    pending_worker_active_check_interval: float = 10.0

    outbox_worker_enabled: bool = True
    outbox_worker_poll_interval: float = 1.0
    outbox_worker_batch_size: int = 50
    # How long to allow interaction-agent delivery (LLM turn + optional send) before
    # considering the attempt timed out and retrying.
    outbox_send_timeout_seconds: float = 600.0
    # Number of outbox send attempts before marking an item permanently failed.
    outbox_max_attempts: int = 12
    outbox_retry_base_seconds: float = 2.0
    # Cap exponential backoff to keep retries responsive.
    outbox_retry_max_seconds: float = 60.0
    outbox_fallback_scan_interval: float = 60.0
    outbox_stale_sending_seconds: int = 120
    scheduler_worker_enabled: bool = True
    scheduler_worker_poll_interval: float = 5.0
    scheduler_worker_batch_size: int = 50
    pm_worker_enabled: bool = True
    pm_worker_poll_interval: float = 5.0
    pm_worker_batch_size: int = 10
    pm_heartbeat_cron: str = "0 10 * * *"
    pm_idle_check_cron: str = "0 */6 * * *"
    pm_health_check_cron: str = "30 * * * *"
    pm_first_heartbeat_cron: str = "0 */2 * * *"
    pm_timezone: str = "America/New_York"
    pm_message_cooldown_seconds: int = 3600
    pm_idle_threshold_hours: int = 48
    pm_first_heartbeat_min_messages: int = 5

    run_lease_seconds: int = 300
    run_activity_poll_interval: float = 2.5
    execution_stream_realtime_enabled: bool = True
    execution_stream_poll_interval: float = 5.0

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

    def resolved_interaction_prompt_path(self) -> Path:
        path = self.interaction_prompt_path
        if path.is_absolute():
            return path
        return (self.root_dir / path).resolve()

    def resolved_interaction_routing_prompt_path(self) -> Path:
        path = (
            self.interaction_agent_routing_prompt_path
            or self.interaction_router_prompt_path
            or self.interaction_prompt_path
        )
        if path.is_absolute():
            return path
        return (self.root_dir / path).resolve()

    def interaction_routing_max_retries(self) -> int:
        value = self.interaction_agent_routing_max_retries
        if value is None:
            value = self.interaction_router_max_retries
        if value is None:
            value = 8
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 8

    def interaction_routing_max_cost_usd(self) -> float | None:
        value = self.interaction_agent_routing_max_cost_usd
        if value is None:
            value = self.interaction_router_max_cost_usd
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def execution_thinking_max_tokens(self) -> int | None:
        value = self.execution_max_thinking_tokens
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        # Anthropic extended-thinking budgets must be >= 1024 when enabled.
        return max(1024, parsed)

    def resolved_interaction_session_cache_dir(self) -> Path:
        path = self.interaction_session_cache_dir
        if path.is_absolute():
            return path
        return (self.root_dir / path).resolve()

    def resolved_vercel_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "vercel").resolve()
        return str(local_cmd) if local_cmd.exists() else self.vercel_cmd

    def resolved_firecrawl_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "firecrawl").resolve()
        return str(local_cmd) if local_cmd.exists() else self.firecrawl_cmd

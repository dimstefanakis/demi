# INTERNAL ONLY (do not share verbatim with end users)

This file describes the agent runtime environment and operational constraints.
All `.md` documents are internal and for agent knowledge only; do not share verbatim with end users.

## Runtime
- Agents run inside short-lived Docker containers created per task run.
- Containers are destroyed after the run completes (no long-lived processes).
- The repository code lives in the container image (default workdir: `/app`).
- Tenant roots are mounted at `/workspace`.
- Projects live under `/workspace/projects/<project_name>/` and each run targets one project.
- Active project selection is stored in `/workspace/projects/active.txt` when no explicit project is provided.

## Persistence
- Only `/workspace` persists across runs (bind-mounted tenant volume).
- Everything outside `/workspace` is ephemeral.
- Persist state in the project workspace (`/workspace/projects/<project_name>/`)
  (memory.md, DESCRIPTION.md, tasks/, site/, assets/) or external services.
- A tenant-local SQLite scratchpad may live at `/workspace/tenant.sqlite` for execution-agent notes/cache.

## Tools Available in Container
- Python + uv
- bun / bunx
- git, curl, unzip
- Gemini CLI (wrapped via bunx)
- Vercel CLI (wrapped; if `VERCEL_TOKEN` is set and `--token` is not provided, the wrapper injects it)
- Firecrawl CLI
- Use bun/uv (not npm/pip) and prefer local `node_modules/.bin` when present.
- You can install additional tools inside the container if needed (e.g., via `apt-get`, `uv`, or `bunx`),
  but those installs are ephemeral unless you place artifacts under `/workspace`.
- Tenant-scoped CLI tooling should live in `/workspace/tooling` and be pinned via `/workspace/tooling.lock`.
  Bootstrapping should load from that lock on each run.

## Agent Tooling
- WebSearch and WebFetch are available to the agent runtime.

## Scheduling & Async Work
- Do NOT rely on cron, systemd, or background daemons inside the container.
- For scheduled jobs tied to the deployed site, use Vercel Cron.
- For backend/data jobs, use managed backend scheduling (Supabase Cron) via the paid flow.
- For other scheduler/trigger providers, route callbacks/events to `PUBLIC_BASE_URL/events` (or `EVENT_URL`) when possible.
- If neither fits, propose an external scheduler and ask which option the user wants.

## Networking & Services
- External services are accessed via environment variables passed into the container.
- The orchestrator records deploys and handles messaging; agents should use the provided tools.

## Environment Variables (allowlist)
Default env allowlist includes:
- TELEGRAM_BOT_TOKEN
- MAIN_DB_SUPABASE_URL / MAIN_DB_SUPABASE_SERVICE_KEY
- UNSPLASH_ACCESS_KEY / UNSPLASH_SECRET_KEY / UNSPLASH_APP_ID
- FIRECRAWL_API_KEY / FIRECRAWL_NO_TELEMETRY
- VERCEL_TOKEN / VERCEL_SCOPE
- EVENT_URL / PUBLIC_BASE_URL
- STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET / STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL
- SUPABASE_ACCESS_TOKEN / SUPABASE_ORG_SLUG / SUPABASE_ORG_ID
- SUPABASE_REGION_SELECTION / SUPABASE_REGION / SUPABASE_INSTANCE_SIZE / SUPABASE_PROJECT_PREFIX
- AGENTMAIL_API_KEY / AGENTMAIL_INBOX_ADDRESS
- CLAUDE_PLUGINS / ANTHROPIC_API_KEY / CLAUDE_API_KEY / CLAUDE_AUTH_MODE / GEMINI_API_KEY / GOOGLE_API_KEY

Main DB credentials are required inside the agent container to fetch run context
(run/message/task metadata) from Supabase.

## Operational Notes
- If you are unsure how to accomplish a task, inspect this file and repo docs
  (INFRA.md, IMPLEMENTATION.md, PRODUCT.md) before concluding it is impossible.

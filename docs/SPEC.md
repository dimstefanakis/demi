# Demi Specification

A Telegram-first (WhatsApp later) chat agent that builds, deploys, and edits SMB websites via conversation. It combines a FastAPI orchestrator, per-tenant workspaces, and a Claude Agent SDK runtime that drives Gemini and Vercel tooling.

---

## Table of Contents

1. Architecture
2. Folder Structure
3. Configuration
4. Workspace & Memory
5. Session Management
6. Message Flow
7. Commands
8. Background Jobs
9. MCP Servers
10. Deployment
11. Security Considerations

---

## Architecture

```
                                ┌────────────────────────────────────┐
                                │            Telegram Users           │
                                └─────────────────────┬──────────────┘
                                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                         API (FastAPI)                                      │
│  /telegram/webhook, /events, /stripe/webhook, /health, /admin/runs/:id/cancel│
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           Orchestrator                                     │
│  de-dupe + routing + task briefs + memory prompts                           │
└───────────────┬───────────────────────────────┬────────────────────────────┘
                │ writes/reads                  │ uses
                ▼                               ▼
┌──────────────────────────────┐   ┌────────────────────────────────────────┐
│ Main DB (Supabase)           │   │ Workspace (data/.../projects/...)       │
│ tenants, messages, runs      │   │ tasks/, assets/, site/, memory.md       │
│ (tool_summary_json,          │   │ tenant.sqlite (scratchpad), CLAUDE.md   │
│ tool_runs_json), run_inputs, │   │                                         │
│ outbox, billing, message_events, │ │                                      │
│ interaction_sessions,        │   │                                         │
│ interaction_session_inputs,  │   │                                         │
│ execution_stream_inputs,     │   │                                         │
│ execution_agents (role),     │   │                                         │
│ tenant_state, tenant_events, │   │                                         │
│ event_jobs                   │   │                                         │
└───────────────┬──────────────┘   └────────────────────────────────────────┘
                │
        ┌───────▼────────────────────────────────────────────────────────────┐
        │                  Interaction Agent Routing                         │
        │   - replies to the user                                            │
        │   - decides if execution should run                                │
        │   - can queue into active run                                      │
        └───────┬────────────────────────────────────────────────────────────┘
                │
                ▼
    ┌──────────────────────────────┐
    │ Agent Runtime                │
    │ - Local Claude Agent SDK     │
    │ - or Docker (demi-agent) │
    └──────────────┬───────────────┘
                   ▼
    ┌──────────────────────────────┐
    │ Execution Subagents          │
    │ - planner (writes tasks/prd) │
    │ - product-designer (Gemini)  │
    │ - software-engineer (TDD)    │
    │ - reviewer (tests + gaps)    │
    │ - devops-engineer (release)  │
    └──────────────┬───────────────┘
                   ▼
    ┌──────────────────────────────┐
    │ MCP + Tools                  │
    │ chat | unsplash | supabase   │
    │ github | chrome-devtools     │
    └───────┬───────────┬─────────┘
            │           │
            ▼           ▼
   ┌────────────────┐   ┌──────────────────────────────┐
   │ Telegram reply │   │ Assets / Repo / Backend ops   │
   │ via interaction│   │ (Unsplash, GitHub, Supabase)  │
   │ agent + outbox │   │                              │
   └───────┬────────┘   └──────────────────────────────┘
           ▼
   ┌──────────────────────────────┐
   │ Firecrawl CLI + Gemini CLI    │
   │ + Vercel CLI                  │
   └──────────────┬───────────────┘
                  ▼
        ┌──────────────────────────┐
        │ Deployment URL -> Telegram│
        └──────────────────────────┘

Background workers (poll main DB):
- EventWorker -> orchestrator (event_jobs)
- PendingWorker -> drains run_inputs into next run
- OutboxWorker -> sends deferred Telegram messages with retry/backoff and stale-send reclaim
- SchedulerWorker -> evaluates trigger mesh and enqueues event_jobs
- PMWorker -> consumes `pm_trigger` jobs for PM dispatch and health self-healing
```

### Technology Stack

- API: FastAPI (`src/demi/app.py`)
- Orchestration: Python (`src/demi/orchestrator.py`)
- Agent runtime: Claude Agent SDK (`claude_agent_sdk`) with optional Docker isolation
- Observability: Laminar (`lmnr[claude-agent-sdk]`) for Claude Agent SDK traces
- Web data: Firecrawl CLI (optional; scrape/search/crawl/map)
- Design: Gemini CLI driven by `docs/DESIGN.md` template (runtime path `/app/docs/DESIGN.md`), editing app files directly (auto-edit mode)
- Deploy: Vercel CLI
- Messaging: Telegram Bot API
- Storage: Supabase main DB, plus per-tenant local SQLite scratchpads in workspaces

---

## Folder Structure

```
demi/
├── AGENTS.md
├── PRODUCT.md / PRD.md / IMPLEMENTATION.md
├── docs/
│   └── SPEC.md
├── prompts/
│   ├── claude_agent.md
│   ├── devops_engineer_agent.md
│   ├── interaction_agent.md
│   ├── planner_agent.md
│   ├── product_designer_agent.md
│   ├── reviewer_agent.md
│   └── software_engineer_agent.md
├── src/
│   └── demi/
│       ├── app.py                     # FastAPI entrypoint
│       ├── orchestrator.py            # Message routing + task flow
│       ├── agent/                     # Claude Agent SDK + MCP tools
│       ├── runtime/                   # Docker agent + pool
│       ├── messaging/                 # Telegram + file messenger
│       ├── workspace/                 # Workspace layout + project routing
│       ├── memory/                    # Chat logs + memory prompts
│       ├── db/                        # Main DB (Supabase)
│       ├── jobs/                      # Background workers
│       └── payments/                  # Stripe helpers
├── docker/
│   ├── agent.Dockerfile
│   └── app.Dockerfile
├── data/                              # Per-tenant workspaces
├── tests/
└── scripts/
```

---

## Configuration

Configuration is managed by `src/demi/config.py` (`Settings`). Environment variables are read from `.env`.

**Required**
- `TELEGRAM_BOT_TOKEN`

**Core tooling**
- `ANTHROPIC_API_KEY` or `CLAUDE_API_KEY`
- `GEMINI_API_KEY` / `GOOGLE_API_KEY`
- `AGENT_EMAIL` (execution-agent identity injected into prompt/env; also used for git author/committer email defaults)
- `VERCEL_TOKEN` (Vercel CLI auth; non-interactive deploys must pass `--token` or rely on the agent-image wrapper)

**Optional integrations**
- Unsplash: `UNSPLASH_ACCESS_KEY`, `UNSPLASH_SECRET_KEY`, `UNSPLASH_APP_ID`
- Stripe: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`
- Billing gate: `BILLING_STATUS_URL`, `BILLING_STATUS_TOKEN`
- Assistant pricing: `ASSISTANT_STRIPE_PRICE_ID` (preferred) or `ASSISTANT_PRICE_USD` + `ASSISTANT_PRODUCT_NAME` + `ASSISTANT_CURRENCY`
- Supabase main DB: `MAIN_DB_SUPABASE_URL`, `MAIN_DB_SUPABASE_SERVICE_KEY`
- Supabase managed backend: `SUPABASE_ACCESS_TOKEN`, `SUPABASE_ORG_*`
- GitHub App: `GITHUB_ORG`, `GITHUB_APP_*`
- Chrome DevTools MCP: `CHROME_DEVTOOLS_MCP_*`
- Admin API: `ADMIN_API_TOKEN` (required for `/admin/runs/:id/cancel`)
- Laminar tracing: `LMNR_PROJECT_API_KEY` (enables optional Claude Agent SDK tracing)

**Agent runtime mode**
- `AGENT_RUNTIME=local` (default) runs Claude in-process
- `AGENT_RUNTIME=docker` runs Claude inside `demi-agent` containers
- `DOCKER_POOL_SIZE` controls pre-warmed idle pool containers (default `0`, disabled)
- `DOCKER_ENV_ALLOWLIST` controls which env vars can be forwarded to containers
- `API_EMBEDDED_WORKERS_ENABLED` controls whether API processes run background workers
  in-process; production blue/green deploys should disable this on API and enable workers
  only in the dedicated worker container
- Model routing defaults:
  - `EXECUTION_MODEL=claude-sonnet-4-5-20250929`
  - `INTERACTION_MODEL=claude-opus-4-6`
  - `PROJECT_MANAGER_MODEL=claude-sonnet-4-5-20250929`
  - `LEAD_PROJECT_MANAGER_MODEL=claude-opus-4-6`
- Interaction calls can use adaptive thinking via `INTERACTION_MAX_THINKING_TOKENS`
- Execution calls can use adaptive thinking via `EXECUTION_MAX_THINKING_TOKENS`
  (default `2048`; values <=0 disable it, values between 1-1023 are clamped to 1024)
- PM role runs can use adaptive thinking via:
  - `PROJECT_MANAGER_MAX_THINKING_TOKENS` (default `10000`)
  - `LEAD_PROJECT_MANAGER_MAX_THINKING_TOKENS` (default `10000`)
- Role-specific execution prompts:
  - `prompts/claude_agent.md` for role `execution`
  - `prompts/project_manager_agent.md` for role `project_manager`
  - `prompts/lead_project_manager_agent.md` for role `lead_project_manager`
- Execution subagents:
  - `planner` runs before implementation to write `tasks/prd.md` and `tasks/test_plan.md`
  - `product-designer` handles Gemini-driven design/application of visual direction
  - `software-engineer` handles TDD-backed implementation and end-to-end wiring
  - `reviewer` runs after implementation and before release to write `tasks/review.md`
  - `devops-engineer` handles git hygiene, build/deploy, and release recording, and only runs
    after reviewer status is `PASS`
  - Execution should send interaction updates between subagent handoffs via `interaction-helper`
    + `demi-chat.send_message` so long-running runs remain visible to users
- Interaction-agent routing controls:
  - `INTERACTION_AGENT_ROUTING_MAX_RETRIES` (legacy `INTERACTION_ROUTER_MAX_RETRIES` still accepted)
  - `INTERACTION_AGENT_ROUTING_MAX_COST_USD` (legacy `INTERACTION_ROUTER_MAX_COST_USD` still accepted)
  - `INTERACTION_AGENT_ROUTING_PROMPT_PATH` (legacy `INTERACTION_ROUTER_PROMPT_PATH` still accepted)
  - Routing retries include a repeated-invalid-output circuit breaker to avoid multi-attempt
    token burn when the agent returns the same non-JSON diagnostic output repeatedly.
- `INTERACTION_SESSION_CACHE_DIR` stores tenant-scoped Claude interaction session/cache files
  for SDK `resume` continuity (default `data/interaction_sessions`)
- `CLAUDE_ENABLE_TOOL_SEARCH=true` enables MCP tool search inside Claude Code sessions
- `CLAUDE_ENABLE_MEMORY_TOOL=true` enables Claude Code memory tool (persisted to project `memory.md`)
- Tenant tooling bootstrap controls:
  - `TENANT_TOOLING_ENABLED`
  - `TENANT_TOOLING_PACKAGES`
  - `TENANT_TOOLING_DIRNAME` (default `tooling`)
  - `TENANT_TOOLING_LOCK_FILE` (default `tooling.lock`)
- Execution stream wake controls:
  - `EXECUTION_STREAM_REALTIME_ENABLED` (default `true`) subscribes runtime containers to Supabase
    realtime `INSERT` events on `execution_stream_inputs` scoped by `run_id`
  - `EXECUTION_STREAM_POLL_INTERVAL` interval for explicit polling mode (`EXECUTION_STREAM_REALTIME_ENABLED=false`)
- Scheduler worker controls:
  - `SCHEDULER_WORKER_ENABLED`
  - `SCHEDULER_WORKER_POLL_INTERVAL`
  - `SCHEDULER_WORKER_BATCH_SIZE`
- PM worker controls:
  - `PM_WORKER_ENABLED`
  - `PM_WORKER_POLL_INTERVAL`
  - `PM_WORKER_BATCH_SIZE`
  - `PM_HEARTBEAT_CRON`
  - `PM_IDLE_CHECK_CRON`
  - `PM_HEALTH_CHECK_CRON`
  - `PM_FIRST_HEARTBEAT_CRON`
  - `PM_TIMEZONE`
- Outbox retry controls:
  - `OUTBOX_SEND_TIMEOUT_SECONDS` (default `600`)
  - `OUTBOX_MAX_ATTEMPTS` (default `12`)
  - `OUTBOX_RETRY_BASE_SECONDS` (default `2`)
  - `OUTBOX_RETRY_MAX_SECONDS` (default `60`)
  - `OUTBOX_FALLBACK_SCAN_INTERVAL`
  - `OUTBOX_STALE_SENDING_SECONDS`

**Billing status endpoint (when `BILLING_STATUS_URL` is set)**

Built-in endpoint: `POST /billing/status` (same API server). If `BILLING_STATUS_URL` is not set, the orchestrator will default to `PUBLIC_BASE_URL + /billing/status` when `PUBLIC_BASE_URL` is available.

Purpose scoping:
- The orchestrator attaches `purpose` and `purpose_label` to each billing check.
- The billing system stores distinct assistant subscription orders per purpose.
- Responses return the same purpose fields so the agent can explain what the hire covers.
- If tenant testing mode is enabled (`tenant_state(system, testing_mode).enabled=true`),
  billing checks short-circuit to an authorized payload (`payment_required=false`, `plan=testing`).
- Usage threshold: if aggregate run cost exceeds `ASSISTANT_USAGE_THRESHOLD_USD` and there is
  no active assistant subscription, the billing status will set `payment_required=true`
  and include `usage_total_usd` + `usage_threshold_usd`. The agent should then request
  an assistant subscription and send the payment link.

Request payload (POST JSON):
```json
{
  "tenant_id": 123,
  "tenant_key": "telegram:123456",
  "provider": "telegram",
  "tenant_external_id": "123456",
  "project_name": "main",
  "provider_message_id": "42",
  "received_at": "2026-02-02T12:34:56Z",
  "purpose": "pricing-request-42",
  "purpose_label": "Pricing request"
}
```

Expected response (example):
```json
{
  "status": "unpaid",
  "payment_required": true,
  "allow_first_build": false,
  "plan": "assistant_monthly",
  "order_id": 1234,
  "payment_url": "https://checkout.stripe.com/...",
  "message": "Hire me for $50/month to continue.",
  "purpose": "pricing-request-42",
  "purpose_label": "Pricing request",
  "price_usd": 50,
  "currency": "USD",
  "usage_total_usd": 3.25,
  "usage_threshold_usd": 3.0
}
```

---

## Workspace & Memory

Each tenant has a workspace rooted under `data/<tenant_key>/` in local runtime mode. The `tenant_key` is `provider:external_id`.

Workspace roots by runtime:
- Local runtime: `data/<tenant_key>/`
- Docker runtime (persistent tenant workspace): `data/pool/tenant-<id>/`
- Optional idle pool slots: `data/pool/idle-<n>/` (only when `DOCKER_POOL_SIZE > 0`)
- On each execution run, the agent entrypoint syncs template artifacts
  (`.claude/`, `CLAUDE.md`, `DESIGN.md`, `.env.example`) into the project workspace if missing.
  The canonical Gemini prompt template remains `/app/docs/DESIGN.md`; project `DESIGN.md`
  is a workspace copy and not the source of truth.

```
data/<tenant_key>/
├── MEMORY.md (optional)
├── tooling/
│   ├── package.json
│   ├── bun.lock
│   └── node_modules/.bin/*
├── tooling.lock
└── projects/
    ├── active.txt
    └── <project_name>/
        ├── CONTEXT.md (optional)
        ├── tasks/
        │   ├── task-YYYYMMDD-HHMMSS.md
        │   ├── latest.md
        │   ├── chat_log.jsonl
        │   ├── chat_history.md
        │   ├── chat_summary.md
        │   ├── request_status.md
        │   ├── billing_status.json
        │   ├── billing_status_<run_id>.json
        │   ├── interaction_context.json
        │   ├── interaction_updates.jsonl
        │   ├── retry_policy.json
        │   ├── repo_name.txt
        │   ├── tool_runs.jsonl
        │   ├── agent_events.jsonl
        │   ├── deploy_url.txt
        │   └── run_result.json
        ├── assets/
        ├── site/
        ├── memory.md
        ├── DESCRIPTION.md
        ├── tenant.sqlite
        ├── CLAUDE.md
        └── DESIGN.md
```

**Memory files**
- `MEMORY.md` (tenant root, optional) stores cross-project durable notes.
- `CONTEXT.md` (project root, optional) stores the current working brief for the project.
- `memory.md` stores durable business facts and decisions.
- `chat_log.jsonl` captures conversation events.
- `chat_history.md` keeps a short recent transcript.
- `chat_summary.md` is a rolling compact summary that execution updates directly when needed.
- Compaction is handled by Claude session compaction + workspace continuity files; the orchestrator
  no longer generates `summary_prompt.md` / `memory_prompt.md`.
- `tenant.sqlite` is an execution-agent scratchpad database. It is not used for orchestration or queues.
- Execution runs bootstrap tenant CLI tooling from `tooling.lock` into `tooling/` and prepend
  `tooling/node_modules/.bin` to PATH. This keeps tenant CLI dependencies deterministic across runs.

**Project routing**
- Each tenant can have multiple projects.
- `projects/active.txt` stores the current default project (best-effort; may be stale).
- The orchestrator applies explicit project directives from message payload/text (`project:` or `/project`).
- Prompt-only routing contract:
  - The interaction agent should attempt to infer the best-fit project by inspecting filesystem context
    (`CONTEXT.md`, `DESCRIPTION.md`, `memory.md`, and recent `tasks/chat_summary.md` when present) and set
    `project_name` in its routing decision.
  - The execution agent treats the filesystem as state, keeps context files fresh, and applies stale-data
    hygiene (update/archive/delete obsolete artifacts when safe).
  - Storage conventions are guidance only; agents may adapt the layout when it improves clarity.

---

## Session Management

Execution and interaction sessions are tracked separately:
- Execution session IDs: `tenants.session_id` (used by `prepare_context` runs).
- Interaction session IDs (routing and instruction unified):
  `tenant_state(namespace='interaction', key='claude_session')`.
- Execution agents are role-scoped:
  - `execution_agents.role` is one of `execution`, `project_manager`, `lead_project_manager`
  - uniqueness is `(tenant_id, context, role)` so each role has an isolated session lane
- Runs persist their lane in `runs.run_role` and resolve role-specific prompts/models at runtime.
- Only `run_role='execution'` updates `tenants.session_id`; PM-role runs persist session state on
  their own execution-agent rows.
- Execution session cache files are written under `<tenant_root>/.execution_home/` by setting
  `HOME` for execution runs. In Docker, this resolves to `/workspace/.execution_home/` and is
  persisted via the tenant workspace bind mount so SDK `resume` works across fresh containers.
- Interaction session cache files are written under `INTERACTION_SESSION_CACHE_DIR/tenant-<id>/`.
  This path is not mounted into execution containers.

Flow:
1. The orchestrator passes execution `session_id` into `ClaudeAgent.prepare_context`.
2. Interaction routing and instruction/update delivery share a single unified
   interaction session ID with SDK `resume`.
3. Both execution and interaction agents return updated session IDs.
4. The orchestrator/workers persist each session ID under its unified key.
5. If interaction resume fails (stale/missing session), interaction session state is cleared and retried fresh once.
6. If execution returns result subtype `error_during_execution`, execution is retried once with a fresh
   SDK session before surfacing the failure.
7. Execution session IDs are only persisted when the run reports usage/cost activity (non-zero token/cost signal).
   Zero-activity execution outcomes are treated as failed transport/runtime outcomes and clear stored execution
   session state to avoid resume loops on poisoned sessions.

---

## Run Cost Tracking

- `runs.total_cost_usd` represents the cumulative cost per request, including interaction agent calls.
- `runs.usage_json` stores structured usage data:
- `primary`: the main agent usage payload.
- `interaction`: a list of interaction agent usage payloads.
- `tenant_events` also records `event_type='agent_usage'` for per-turn usage/cost observability,
  including interaction turns that are not attached to a run.

**Admin views (Supabase main DB)**
- `admin_run_costs`: per-run cost + token breakdown (primary + interaction).
- `admin_tenant_overview`: last message + aggregate cost per tenant.
- `admin_tenant_costs_daily`: daily rollups for finance/analytics.

---

## Streaming Telemetry (Supabase)

These tables capture interaction/execution in-flight streaming so operations can inspect exactly what
happened in Supabase dashboards.

- `interaction_sessions`
- One row per interaction routing loop.
- Columns: `tenant_id`, `project_name`, `status` (`running|completed|failed`), `started_at`, `finished_at`.

- `interaction_session_inputs`
- Inputs streamed into a live interaction routing loop.
- Columns: `session_id`, `message_id`, `provider_message_id`, `text`, `assets_json`, `status`, `created_at`, `streamed_at`.

- `execution_stream_inputs`
- Inputs targeted at a running execution agent.
- Columns: `tenant_id`, `run_id`, `project_name`, `message_id`, `provider_message_id`, `text`, `assets_json`,
  `status` (`pending|streamed`), `created_at`, `streamed_at`.

- `tenant_events` (`event_type='agent_stop_reason'`)
- Claude Agent SDK result stop telemetry persisted to Supabase for observability.
- Payload fields include `context` (`prepare_context|run_interaction_agent|send_interaction_message|send_interaction_instruction`),
  `stop_reason`, `result_subtype`, derived `status`, and run/message metadata when available.

- `tenant_events` (`event_type='agent_usage'`)
- Claude Agent SDK usage/cost telemetry persisted per turn.
- Payload fields include `context`, `total_cost_usd`, raw `usage`, and run/message metadata when available.

- Laminar traces (optional)
- When `LMNR_PROJECT_API_KEY` is configured, API/worker/runtime entrypoints initialize Laminar once.
- `ClaudeAgent` wraps `prepare_context`, `run_interaction_agent`, `send_interaction_message`,
  and `send_interaction_instruction` with `@observe(...)` parent spans so each Claude SDK turn
  is visible in Laminar trace views (including subagent/tool activity captured by the integration).

Execution runtime consumption:
- Agent runtime claims `execution_stream_inputs.status='pending'` for its `run_id`,
  pushes payloads into the in-memory inflight stream, then marks rows `streamed`.
- Runtime uses Supabase realtime `INSERT` notifications (single channel per run process) to wake
  claim loops without constant polling.
- When realtime mode is enabled, runtime does not silently fall back to DB polling if realtime
  subscription setup fails; this is intentional so failures are visible.
- `inflight_updates.jsonl` is only written for true file-stream fallback cases to avoid duplicate runtime ingestion
  when DB-backed streaming is available.
- File-fallback entries include `run_id`; runtime only accepts matching `run_id` entries and drains the file via
  snapshot-rename to avoid replaying stale updates across later runs.

Run selection when streaming to execution:
- Interaction agent first calls `find_execution_agent`.
- `stream_to_execution_agent` targets `run_id` directly when provided.
- Without `run_id`, it only auto-selects when exactly one active run exists for the tenant.
  If multiple candidates remain, it returns `ambiguous_run` with candidates and does not stream.

---

## Message Flow

1. Telegram sends an update to `POST /telegram/webhook`.
2. The webhook parser normalizes the message into a `NormalizedMessage`.
3. The orchestrator records the message (idempotent by `provider_message_id`).
4. Interaction routing is serialized per tenant (single interaction loop per tenant).
   Each inbound message starts its own interaction turn unless a turn is already running for the tenant.
   In that case, the new message is streamed into the active interaction turn as an in-flight update.
   If an interaction turn crashes before queue/run creation, streamed rows that fall back to
   `status='received'` are automatically re-processed by PendingWorker after a grace window.
5. Project selection is resolved from explicit directives when provided; otherwise active project is used.
6. If configured, the orchestrator calls the billing status endpoint during routing.
   It writes `tasks/billing_status.json` when no other run is active; if a run is already in flight it writes
   `tasks/billing_status_<run_id>.json` for the new run to avoid clobbering the in-flight payload. Billing
   status no longer creates assistant orders by default; the agent explicitly requests payment links.
   If tenant testing mode is enabled, this status is treated as authorized and payment prompts are bypassed.
7. The orchestrator merges any attachments for the current interaction turn, saves them under `assets/`, writes
   `tasks/interaction_context.json`, and calls the interaction agent in routing mode with an explicit,
   authoritative incoming-message snapshot (message id, provider message id, timestamp, text, image count).
8. The interaction agent replies to the user (if needed) and returns a routing decision (run/no-run, queue vs new run).
   - Interaction routing avoids implicit project switching; execution flow performs project-fit checks
     by reading per-project markdown context.
   - Interaction `send_message`/`send_payment_link` calls are correlation-id idempotent: repeated
     sends with the same `correlation_id` are skipped to prevent duplicate user messages when
     routing retries occur.
   - If the decision includes a repo name, the orchestrator stores it in `tasks/repo_name.txt` for GitHub setup.
   - GitHub repo linkage is recovered from `github_repo.json` and, if missing, from local `site/.git` origin
     before creating a new repo name.
9. If no run is needed, the messages included in the turn are marked processed.
   If the routing decision reports `reply_sent=false`, the orchestrator verifies whether any outbound message
   was actually persisted for that turn and sends a fallback interaction reply when needed to avoid silent drops.
10. If routing decides `queue_run=true`, messages included in the turn are queued in `run_inputs`
   for the selected active run. The interaction agent handles the queued ack (orchestrator only
   falls back if no reply was sent).
   When streaming is supported, the interaction agent can optionally stream the new input to the
   target active execution agent instead of queueing.
11. If routing decides `should_run=true`, a new run is created and leased even when other runs are
   already active (parallel execution is allowed). The orchestrator updates the run context in
   Supabase (task_path, session_id). Standard acks are only sent as a fallback when no reply was sent.
   - If routing includes `parallel_runs`, the orchestrator also starts one additional run per
     listed execution context (fan-out) in the same interaction turn.
12. The agent runtime executes. Execution agents do not send user-facing messages directly; they emit updates that the interaction agent delivers.
    - Execution agents emit interaction updates that are delivered by the interaction agent via outbox.
    - Execution completion always enqueues a final interaction update (outbox) as a safety-net so successful runs cannot finish silently even if the model never calls `send_message`.
    - Git versioning is scoped to deployable app files within the project (for example `site/` or another app root);
      orchestration metadata files are excluded from website commits.
    - If a run fails while draining queued `run_inputs`, retry behavior is policy-driven:
      - default for `provider='event'`: terminal (do not requeue),
      - default for non-event providers: requeue,
      - agents can override by writing `tasks/retry_policy.json` (`retryable`, `terminal`, `dedupe_key`, `max_requeues`).
    - Retry dedupe is enforced via tenant state keys to prevent re-dispatch loops for the same failing unit of work.
      User-facing "try again shortly" notifications are capped to the first two failed attempts
      for the same message to avoid repeated spam during prolonged failures.
    - The agent can create an assistant subscription order by calling `request_assistant_subscription`
      after delivering value, then sends the payment link via the interaction agent.
13. Results are persisted (`run_result.json`, `deploy_url.txt`, DB updates).
    - `run_result.json` includes Claude SDK stop metadata (`stop_reason`, `result_subtype`) and optional `retry_policy`.
    - Terminal stop reasons (`end_turn`, `stop_sequence`) are treated as successful completion unless
      `result_subtype` indicates an SDK error.
    - Execution outcomes that provide usage/cost fields but report zero activity are marked failed
      (`agent_result_no_usage_activity`) only when there is no non-usage run activity
      (for example tool runs/outbound updates). Wrapped usage payloads are treated as activity.
    - Non-terminal execution stop reasons (for example `max_tokens`, `refusal`, `model_context_window_exceeded`)
      fail the run instead of being treated as successful completion.
14. Any queued `run_inputs` are drained into the next run.
15. All outbound user messages are recorded in `message_events` for replay/debugging.

---

## Commands

Supported user-level commands in Telegram messages:

- `/reset` clears stuck runs and queued inputs for the active project.
- `project: <name>` or `/project <name>` sets the active project for the tenant.
- `/testing on|off` (or `testing: enabled|disabled`) toggles tenant testing mode.

---

## Background Jobs

Background workers poll the main DB and run in the API process or the worker container.

- EventWorker: Consumes `event_jobs` from the main DB, relies on `/events` to persist payloads in `tenant_events` (Supabase), and triggers the orchestrator with a normalized event message.
- PendingWorker: Drains queued `run_inputs` once a project is idle and reclaims stale runs after lease expiry.
  Idle polling uses exponential backoff but caps sleep to the active-run check interval to avoid
  multi-minute delays when new queued inputs arrive after an idle period.
  It also scans for stale `messages.status='received'` rows and re-invokes orchestrator handling
  in recovery mode so webhook-accepted messages do not get stranded. Recovery bypasses interaction
  stream handoff and terminalizes unresolved rows (`messages.status='failed'`) to avoid repeated
  high-cost routing loops on a single stale row.
- OutboxWorker: Sends deferred messages from the `outbox` table for busy acknowledgments and fallback notifications.
  Also drains `tasks/interaction_updates.jsonl` when execution agents cannot access the main DB.
  Uses bounded retries, stale `sending` reclaim, and throttled fallback file scans to prevent
  head-of-line stalls and excessive polling.
- SchedulerWorker: Evaluates tenant trigger definitions stored in
  `tenant_state(namespace='scheduler', key='trigger:*')` and enqueues `event_jobs`.
  Triggers with `output_event_type='pm_trigger'` are enqueued with `job_type='pm_trigger'`
  so PM processing can be isolated from normal event execution.
  When PM worker is disabled, scheduler suppresses `pm_trigger` enqueues so unclaimed
  PM jobs do not accumulate indefinitely.
  Trigger mesh supports:
  - `cron` schedules
  - `webhook_condition` matches against `tenant_events` payloads
  - `state_change` watches tenant state values (`namespace/key/path`)
  - optional time windows (`start/end/timezone`)
  - optional retry windows/backoff metadata attached to event payloads
- PMWorker: Claims `event_jobs` with `job_type='pm_trigger'` and acts as a thin dispatcher.
  Non-health triggers are routed into orchestrator event execution with explicit roles:
  - `role='project_manager'` for per-project PM updates
  - `role='lead_project_manager'` for tenant-root PM heartbeat analysis
  - Scheduler `run_completed` PM triggers without an explicit role are skipped to avoid
    duplicate PM runs when prompt-first project PM handoff already fired.
  Health checks stay code-level and auto-fix:
  - stale `received` messages
  - zombie runs
  - failed outbox rows (excluding terminal failures)
  - stale queued run inputs
  API/worker startup also runs an idempotent PM backfill for tenants with existing deploy URLs.
  PM webhook-condition triggers are initialized with the current tenant event cursor so enabling
  PM does not replay historical `tenant_events` backlog.
- Worker loop resilience: Event/Pending/Outbox/Scheduler/PM loops treat transient DB/API errors
  as recoverable, log the exception, back off, and continue polling instead of exiting.

## Webhook Diagnostics

Inbound Telegram webhook payloads are persisted to `webhook_updates` with parse metadata
(`parsed`/`ignored`, parse error, tenant/message ids when available). This makes dropped or
ignored message investigations concrete instead of relying on container logs.

---

## MCP Servers

MCP servers are registered per agent run.

- `demi-chat`: `send_message`, `should_send_message`, `check_for_status`, `find_execution_agent`,
  `stream_to_execution_agent`, `stop_execution_agent`, `ack_inflight_updates`, `record_deploy`,
  `record_domain_quote`, `record_billing_status`, `send_payment_link`,
  `request_backend_subscription`, `request_assistant_subscription`, `decide_project`,
  `find_project_manager`, `trigger_project_manager`,
  `set_testing_mode`, `register_scheduler_trigger`, `list_scheduler_triggers`,
  `unregister_scheduler_trigger`
- `demi-unsplash`: `search_photos` (Unsplash sourcing)
- `demi-supabase`: `provision_managed_backend`, `upgrade_managed_backend`
- `demi-github`: `prepare_repo` (GitHub App provisioning)
- `supabase` (remote MCP): Enabled when configured for Supabase MCP access
- `chrome-devtools` (optional): Browser automation via Chrome DevTools MCP

---

## Deployment

Primary deployment target is a single VM with Docker Compose and optional blue/green routing.

- API: `uv run uvicorn demi.app:app --reload` for local dev
- Worker: `python -m demi.worker_entrypoint`
- Production: Docker Compose with `nginx`, `api_blue`, `api_green`, `worker`
- Agent runtime uses Docker socket to run `demi-agent` images
- Long-running agent container commands are not hard-timed-out by default (prevents aborting active runs).
  Configure `DOCKER_CONTAINER_COMMAND_TIMEOUT_SECONDS` to enforce a hard limit if needed.

See `DEPLOY.md` for the full GCE blue/green process and required secrets.

---

## Security Considerations

- Docker isolation for agent runs when `AGENT_RUNTIME=docker` is enabled.
- Environment forwarding into agent containers is allowlist-based.
- Event webhook signatures are verified when `EVENTS_SIGNING_SECRET` is set.
- Admin run-cancel endpoint is protected by `ADMIN_API_TOKEN`.
- Runs are lease-based with heartbeats to prevent stuck tasks; docker execution runs maintain leases by watching
  task artifacts (including Gemini output) and updating `last_activity_at`/`last_heartbeat_at`.
- Tenant workspaces are isolated by path and only mounted per run.

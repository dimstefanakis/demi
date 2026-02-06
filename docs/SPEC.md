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
│ execution_stream_inputs      │   │                                         │
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
   │ Gemini CLI + Vercel CLI       │
   └──────────────┬───────────────┘
                  ▼
        ┌──────────────────────────┐
        │ Deployment URL -> Telegram│
        └──────────────────────────┘

Background workers (poll main DB):
- EventWorker -> orchestrator (event_jobs)
- PendingWorker -> drains run_inputs into next run
- OutboxWorker -> sends deferred Telegram messages
```

### Technology Stack

- API: FastAPI (`src/demi/app.py`)
- Orchestration: Python (`src/demi/orchestrator.py`)
- Agent runtime: Claude Agent SDK (`claude_agent_sdk`) with optional Docker isolation
- Design: Gemini CLI driven by `DESIGN.md`, editing app files directly (auto-edit mode)
- Deploy: Vercel CLI
- Messaging: Telegram Bot API
- Storage: SQLite by default, Supabase optional for main DB

---

## Folder Structure

```
demi/
├── AGENTS.md
├── PRODUCT.md / PRD.md / IMPLEMENTATION.md / DESIGN.md
├── docs/
│   └── SPEC.md
├── prompts/
│   ├── claude_agent.md
│   └── interaction_agent.md
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
- `VERCEL_TOKEN`

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

**Agent runtime mode**
- `AGENT_RUNTIME=local` (default) runs Claude in-process
- `AGENT_RUNTIME=docker` runs Claude inside `demi-agent` containers
- `DOCKER_POOL_SIZE` controls pre-warmed idle pool containers (default `0`, disabled)
- `DOCKER_ENV_ALLOWLIST` controls which env vars can be forwarded to containers

**Billing status endpoint (when `BILLING_STATUS_URL` is set)**

Built-in endpoint: `POST /billing/status` (same API server). If `BILLING_STATUS_URL` is not set, the orchestrator will default to `PUBLIC_BASE_URL + /billing/status` when `PUBLIC_BASE_URL` is available.

Purpose scoping:
- The orchestrator attaches `purpose` and `purpose_label` to each billing check.
- The billing system stores distinct assistant subscription orders per purpose.
- Responses return the same purpose fields so the agent can explain what the hire covers.
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

```
data/<tenant_key>/
└── projects/
    ├── active.txt
    └── <project_name>/
        ├── tasks/
        │   ├── task-YYYYMMDD-HHMMSS.md
        │   ├── latest.md
        │   ├── chat_log.jsonl
        │   ├── chat_history.md
        │   ├── chat_summary.md
        │   ├── summary_prompt.md
        │   ├── memory_prompt.md
        │   ├── request_status.md
        │   ├── billing_status.json
        │   ├── billing_status_<run_id>.json
        │   ├── interaction_context.json
        │   ├── interaction_updates.jsonl
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
- `memory.md` stores durable business facts and decisions.
- `chat_log.jsonl` captures conversation events.
- `chat_history.md` keeps a short recent transcript.
- `chat_summary.md` is the rolling summary the agent maintains when prompted.
- `summary_prompt.md` and `memory_prompt.md` are generated prompts the agent uses to refresh summaries and memory.
- `tenant.sqlite` is an execution-agent scratchpad database. It is not used for orchestration or queues.

**Project routing**
- Each tenant can have multiple projects.
- `projects/active.txt` stores the current default project.
- The orchestrator can infer a project from recent context or explicit directives.

---

## Session Management

Claude Agent SDK session IDs are stored per tenant in the main DB (`tenants.session_id`).

Flow:
1. The orchestrator passes the stored `session_id` into `ClaudeAgent.prepare_context`.
2. The agent returns an updated `session_id` after each run.
3. The orchestrator persists it for continuity across messages.

---

## Run Cost Tracking

- `runs.total_cost_usd` represents the cumulative cost per request, including interaction-agent calls.
- `runs.usage_json` stores structured usage data:
- `primary`: the main agent usage payload.
- `interaction`: a list of interaction-agent usage payloads.

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
- Payload fields include `context` (`prepare_context|route_interaction|send_interaction_message|send_interaction_instruction`),
  `stop_reason`, `result_subtype`, derived `status`, and run/message metadata when available.

Execution runtime consumption:
- Agent runtime claims `execution_stream_inputs.status='pending'` for its `run_id`,
  pushes payloads into the in-memory inflight stream, then marks rows `streamed`.
- If direct memory streaming is unavailable, rows remain visible and are still consumed by the runtime poller.
- `inflight_updates.jsonl` is only written for true file-stream fallback cases to avoid duplicate runtime ingestion
  when DB-backed streaming is available.
- File-fallback entries include `run_id`; runtime only accepts matching `run_id` entries and drains the file via
  snapshot-rename to avoid replaying stale updates across later runs.

Run selection when streaming to execution:
- Interaction agent first calls `find_execution_agent`.
- `stream_to_execution_agent` targets `run_id` directly when provided.
- Without `run_id`, it resolves by `project_name` when unique; if multiple candidates remain, it returns
  `ambiguous_run` with candidates and does not stream.

---

## Message Flow

1. Telegram sends an update to `POST /telegram/webhook`.
2. The webhook parser normalizes the message into a `NormalizedMessage`.
3. The orchestrator records the message (idempotent by `provider_message_id`).
4. Interaction routing is serialized per tenant (single interaction loop per tenant).
   Each inbound message starts its own interaction turn unless a turn is already running for the tenant.
   In that case, the new message is streamed into the active interaction turn as an in-flight update.
5. Project selection is resolved (explicit directive or inferred from context).
6. If configured, the orchestrator calls the billing status endpoint during routing.
   It writes `tasks/billing_status.json` when no other run is active; if a run is already in flight it writes
   `tasks/billing_status_<run_id>.json` for the new run to avoid clobbering the in-flight payload. Billing
   status no longer creates assistant orders by default; the agent explicitly requests payment links.
7. The orchestrator merges any attachments for the current interaction turn, saves them under `assets/`, writes
   `tasks/interaction_context.json`, and calls the interaction agent in routing mode.
8. The interaction agent replies to the user (if needed) and returns a routing decision (run/no-run, queue vs new run).
   - If the decision includes a repo name, the orchestrator stores it in `tasks/repo_name.txt` for GitHub setup.
9. If no run is needed, the messages included in the turn are marked processed. The interaction agent already replied.
10. If a run is in flight for the same project, messages included in the turn are queued in `run_inputs`. The interaction agent handles the queued ack (orchestrator only falls back if no reply was sent).
   When streaming is supported, the interaction agent can optionally stream the new input to the
   active execution agent instead of queueing a new run.
11. Otherwise a new run is created and leased. The orchestrator updates the run context in Supabase (task_path, session_id). Standard acks are only sent as a fallback when no reply was sent.
12. The agent runtime executes. Execution agents do not send user-facing messages directly; they emit updates that the interaction agent delivers.
    - Execution agents emit interaction updates that are delivered by the interaction agent via outbox.
    - The agent can create an assistant subscription order by calling `request_assistant_subscription`
      after delivering value, then sends the payment link via the interaction agent.
13. Results are persisted (`run_result.json`, `deploy_url.txt`, DB updates).
    - `run_result.json` includes Claude SDK stop metadata (`stop_reason`, `result_subtype`).
    - Terminal stop reasons (`end_turn`, `stop_sequence`) are treated as successful completion unless
      `result_subtype` indicates an SDK error.
    - Non-terminal execution stop reasons (for example `max_tokens`, `refusal`, `model_context_window_exceeded`)
      fail the run instead of being treated as successful completion.
14. Any queued `run_inputs` are drained into the next run.
15. All outbound user messages are recorded in `message_events` for replay/debugging.

---

## Commands

Supported user-level commands in Telegram messages:

- `/reset` clears stuck runs and queued inputs for the active project.
- `project: <name>` or `/project <name>` sets the active project for the tenant.

---

## Background Jobs

Background workers poll the main DB and run in the API process or the worker container.

- EventWorker: Consumes `event_jobs` from the main DB, relies on `/events` to persist payloads in `tenant_events` (Supabase), and triggers the orchestrator with a normalized event message.
- PendingWorker: Drains queued `run_inputs` once a project is idle and reclaims stale runs after lease expiry.
- OutboxWorker: Sends deferred messages from the `outbox` table for busy acknowledgments and fallback notifications.
  Also drains `tasks/interaction_updates.jsonl` when execution agents cannot access the main DB.

---

## MCP Servers

MCP servers are registered per agent run.

- `demi-chat`: `send_message`, `should_send_message`, `check_for_status`, `find_execution_agent`,
  `stream_to_execution_agent`, `stop_execution_agent`, `ack_inflight_updates`, `record_deploy`,
  `record_domain_quote`, `record_billing_status`, `send_payment_link`,
  `request_backend_subscription`, `request_assistant_subscription`, `decide_project`
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

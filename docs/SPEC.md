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
│  /telegram/webhook, /events, /stripe/webhook, /health                       │
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           Orchestrator                                     │
│  de-dupe + routing + task briefs + memory prompts                           │
└───────────────┬───────────────────────────────┬────────────────────────────┘
                │ writes/reads                  │ uses
                ▼                               ▼
┌──────────────────────────────┐   ┌────────────────────────────────────────┐
│ Main DB (SQLite/Supabase)    │   │ Workspace (data/.../projects/...)       │
│ tenants, messages, runs,     │   │ tasks/, assets/, site/, memory.md       │
│ run_inputs, outbox, billing  │   │ tenant.sqlite, CLAUDE.md, DESIGN.md     │
└───────────────┬──────────────┘   └────────────────────────────────────────┘
                │
        ┌───────▼────────────────────────────────────────────────────────────┐
        │                         Run Decision                               │
        │   busy -> queue run_input + Interaction Agent ack                  │
        │          (restricted tools) -> Telegram/outbox                     │
        │   idle -> start run (lease)                                        │
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
   │ via chat tools │   │ (Unsplash, GitHub, Supabase)  │
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
- Design: Gemini CLI driven by `DESIGN.md`
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
│       ├── db/                        # Main DB (SQLite/Supabase)
│       ├── jobs/                      # Background workers
│       └── payments/                  # Stripe helpers
├── docker/
│   ├── agent.Dockerfile
│   └── app.Dockerfile
├── data/                              # Per-tenant workspaces + main DB
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
- Supabase main DB: `MAIN_DB_BACKEND=supabase`, `MAIN_DB_SUPABASE_URL`, `MAIN_DB_SUPABASE_SERVICE_KEY`
- Supabase managed backend: `SUPABASE_ACCESS_TOKEN`, `SUPABASE_ORG_*`
- GitHub App: `GITHUB_ORG`, `GITHUB_APP_*`
- Chrome DevTools MCP: `CHROME_DEVTOOLS_MCP_*`

**Agent runtime mode**
- `AGENT_RUNTIME=local` (default) runs Claude in-process
- `AGENT_RUNTIME=docker` runs Claude inside `demi-agent` containers
- `DOCKER_ENV_ALLOWLIST` controls which env vars can be forwarded to containers

**Billing status endpoint (when `BILLING_STATUS_URL` is set)**

Built-in endpoint: `POST /billing/status` (same API server). If `BILLING_STATUS_URL` is not set, the orchestrator will default to `PUBLIC_BASE_URL + /billing/status` when `PUBLIC_BASE_URL` is available.

Purpose scoping:
- The orchestrator attaches `purpose` and `purpose_label` to each billing check.
- The billing system stores distinct assistant subscription orders per purpose.
- Responses return the same purpose fields so the agent can explain what the hire covers.

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
  "currency": "USD"
}
```

---

## Workspace & Memory

Each tenant has a workspace rooted under `data/<tenant_key>/` in local runtime mode. The `tenant_key` is `provider:external_id`.

Workspace roots by runtime:
- Local runtime: `data/<tenant_key>/`
- Docker pool runtime: `data/pool/slot-<uuid>/` (assigned per tenant and persisted as `tenants.workspace_path`)

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
        │   ├── interaction_request.json
        │   ├── tool_runs.jsonl
        │   ├── agent_events.jsonl
        │   ├── deploy_url.txt
        │   └── run_request.json / run_result.json
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

## Message Flow

1. Telegram sends an update to `POST /telegram/webhook`.
2. The webhook parser normalizes the message into a `NormalizedMessage`.
3. The orchestrator records the message (idempotent by `provider_message_id`).
4. Project selection is resolved (explicit directive or inferred from context).
5. If configured, the orchestrator calls the billing status endpoint and writes `tasks/billing_status.json`.
   Billing status no longer creates assistant orders by default; the agent explicitly requests payment links.
6. A task brief is written to `tasks/` and logs are updated.
7. The orchestrator writes `tasks/run_request.json` for interaction-agent reply matching (and the Docker runtime
   reuses it when running containerized agents).
8. If a run is in flight for the same project, the message is queued in `run_inputs` and a short acknowledgment is sent via the interaction agent.
9. Otherwise a new run is created and leased.
10. The agent runtime executes, reads `memory.md`, `DESCRIPTION.md`, `tasks/latest.md`, `DESIGN.md`, uses Gemini CLI and Vercel CLI as needed, and sends user-facing messages through MCP tools.
   - If billing requires payment, primary-agent send requests are queued in `tasks/interaction_request.json`
     and delivered via the interaction agent after the run.
   - The agent can create an assistant subscription order by calling `request_assistant_subscription`
     after delivering value, then sends the payment link via the interaction agent.
11. Results are persisted (`run_result.json`, `deploy_url.txt`, DB updates).
12. Any queued `run_inputs` are drained into the next run.

---

## Commands

Supported user-level commands in Telegram messages:

- `/reset` clears stuck runs and queued inputs for the active project.
- `project: <name>` or `/project <name>` sets the active project for the tenant.

---

## Background Jobs

Background workers poll the main DB and run in the API process or the worker container.

- EventWorker: Consumes `event_jobs` from the main DB, relies on `/events` to persist payloads in `tenant.sqlite`, and triggers the orchestrator with a normalized event message.
- PendingWorker: Drains queued `run_inputs` once a project is idle and reclaims stale runs after lease expiry.
- OutboxWorker: Sends deferred messages from the `outbox` table for busy acknowledgments and fallback notifications.

---

## MCP Servers

MCP servers are registered per agent run.

- `demi-chat`: `send_message`, `should_send_message`, `ack_inflight_updates`, `record_deploy`, `record_domain_quote`, `record_billing_status`, `send_payment_link`, `request_backend_subscription`, `request_assistant_subscription`, `decide_project`
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
- Runs are lease-based with heartbeats to prevent stuck tasks.
- Tenant workspaces are isolated by path and only mounted per run.

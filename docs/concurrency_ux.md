# Concurrency + UX Architecture (Tenant-Safe)

## Goals
- Single source of truth for message flow (no split-brain between files and DB).
- Excellent UX: immediate acknowledgment, continuous progress, no duplicate replies.
- Strong tenant isolation: a tenant agent can never read or affect other tenants.
- Robust recovery: retries without double-charging or duplicate work.

## Root Cause (Current System)
- New messages are both:
  - Written to `tasks/inflight_updates.jsonl` (file), and
  - Stored in the main DB as pending/processing (queue).
- The running agent reads the file and replies, but the DB row is never marked
  processed, so a second run is launched later (duplicate work + duplicate UX).

## Proposed Architecture (Single Source of Truth)
### Core idea
Move all inflight updates into the DB and **atomically claim** them.
The running agent consumes updates from the DB (via a tenant-scoped API),
so there is no file/DB mismatch.

### Data Model (Postgres + Supabase, Partial UUID Migration)
```
-- All tables include tenant_id; RLS enforced (see Tenant Isolation).
-- Existing core tables keep BIGINT PKs for now; new orchestration tables use UUID PKs.

-- Existing runs table (BIGINT PK) remains as-is.

create table run_inputs (
  id uuid primary key default gen_random_uuid(),
  tenant_id bigint not null,
  run_id bigint references runs(id),
  source text not null, -- telegram, event, etc.
  provider_message_id text,
  payload_json jsonb not null,
  status text not null, -- queued, claimed, handled, cancelled
  claimed_at timestamptz,
  handled_at timestamptz,
  created_at timestamptz not null
);
create unique index run_inputs_dedupe_idx
  on run_inputs(tenant_id, provider_message_id) where provider_message_id is not null;
create index run_inputs_queue_idx on run_inputs(tenant_id, run_id, status, created_at);

create table outbox (
  id uuid primary key default gen_random_uuid(),
  tenant_id bigint not null,
  run_id bigint references runs(id),
  correlation_id text not null,
  payload_json jsonb not null,
  status text not null, -- queued, sent, failed
  created_at timestamptz not null,
  sent_at timestamptz
);
create unique index outbox_dedupe_idx on outbox(tenant_id, correlation_id);

create table active_runs (
  tenant_id bigint primary key,
  run_id bigint not null references runs(id),
  lease_expires_at timestamptz not null,
  updated_at timestamptz not null
);
```

## Runtime Flow
### When a user message arrives
1) Orchestrator resolves tenant and project.
2) If there is an active run:
   - Enqueue `run_inputs` row for that run (status = queued).
   - Enqueue a short ack into `outbox`.
3) If no active run:
   - Create a run and set `active_runs`.
   - Create a `run_inputs` row (status = queued) for the initial message.
   - Start the agent with run_id.

### While the agent is running
- Agent requests “next inputs” through a **tenant-scoped API**:
  - `claim_run_inputs(run_id, limit)` does:
    - `UPDATE run_inputs SET status='claimed', claimed_at=now()`
      `WHERE tenant_id=? AND run_id=? AND status='queued'`
      `RETURNING *`
- Agent acknowledges user promptly (outbox entry), then continues.
- Agent marks inputs as handled via `mark_run_inputs_handled(ids)`.

### Message delivery
- A dedicated sender worker reads `outbox` and sends messages.
- Exactly-once: `correlation_id` prevents duplicates.

### Run completion
- Agent writes final summary.
- Orchestrator marks run completed and clears `active_runs`.
- Any remaining `run_inputs` are carried into the next run if desired.

## Tenant Isolation (Hard Requirements)
The agent **must not** access other tenants’ data. Enforce at multiple layers:

### 1) Supabase Row-Level Security (RLS)
- Every table includes `tenant_id` (non-null).
- We do **not** rely on end-user auth (Telegram/WhatsApp IDs are our identity source).
- The server maps `provider + external_id -> tenant_id` and **mints an internal JWT**
  for the agent/API with a `tenant_id` claim (no user login required).
- RLS policies then enforce:
  - `tenant_id = (current_setting('request.jwt.claims', true)::jsonb->>'tenant_id')::bigint`
  - Optionally also check `role = 'tenant'` in JWT claims.
- Never use the Supabase service key inside the agent container.

**Alternative (if you don't want JWT at all):**
- Use a direct Postgres connection from the server (not PostgREST).
- Set a custom GUC per transaction: `SET LOCAL app.tenant_id = <tenant_id>`.
- RLS policy checks `tenant_id = current_setting('app.tenant_id')::bigint`.
- The agent still never connects to DB directly; it only calls the server.

### 2) No Service-Key DB Access Inside Agent
- Do not pass Supabase service key into agent containers.
- All DB access flows through a tenant-scoped API controlled by the server.
- The agent only gets a short-lived token scoped to its tenant.

### 3) File System Isolation
- Container mounts **only** the tenant root (already in place).
- No shared mount for other tenant data.

### 4) API Guards
- Every internal API call requires a tenant-scoped token.
- The server validates token tenant_id and applies it to DB queries.

## UX Guarantees
- Immediate ack via outbox on message receipt.
- “Working” heartbeat only when no progress for a while.
- Completion message always sent at the end of a run.
- Exactly-once delivery (no accidental double replies).

## Failure Modes + Recovery
- Lease expiry: if the run stalls, a coordinator marks it failed and clears active_runs.
- New run can be resumed with the same run_id (optional) or restarted cleanly.
- Inputs already handled are not re-claimed.

## Local SQLite (Agent Autonomy)
- Keep `tenant.sqlite` per tenant for fully autonomous, local operations:
  - scratchpad data, cached checks, tool logs, or lightweight structured notes.
- Do not use SQLite to orchestrate runs or queues; orchestration stays in Supabase.
- This keeps autonomy while preventing cross-tenant leakage and split-brain bugs.

## Minimal Migration Plan (Low-Churn)
1) Add tables (`runs`, `run_inputs`, `outbox`, `active_runs`) + RLS policies.
2) Create new orchestrator path:
   - New messages go to `run_inputs` instead of `messages/pending`.
3) Add agent endpoints:
   - claim_run_inputs
   - mark_run_inputs_handled
   - enqueue_outbox
4) Switch prompts/tools to use DB-backed inflight inputs (no files).
5) Delete old pending worker once stable.

## Why This Fixes the Debt Risk
- Each user message is claimed once; retries do not re-run the same work.
- Outbox is idempotent; user gets one consistent reply.
- Strong tenant isolation prevents data leakage and cross-tenant access.

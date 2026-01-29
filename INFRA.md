# Infrastructure Plan (MVP)

This document solidifies the MVP infrastructure for a chat-first website agent with instant acknowledgments, strong tenant isolation, and low per-tenant cost.

## Core Decisions
- **Single VPS + Docker** as the control plane.
- **Per-task agent containers** with **per-tenant volumes** (hard filesystem isolation).
- **Hot pool** of pre-warmed containers to guarantee instant first replies.
- **GitHub App** for fully automated **per-tenant repos** (versioning + export).
- **Shared Vercel org** with **per-tenant projects**.
- **SQLite** for MVP state (tenants, runs, idempotency, deploy URLs).

## Component Overview
1) **Orchestrator API (FastAPI)**
   - Receives Telegram webhooks and events.
   - Validates idempotency, resolves tenant, dispatches runs.
   - Never runs LLM or filesystem tools directly.

2) **Pool Manager**
   - Maintains `pool_size = N` idle agent containers.
   - Each idle container is started with a dedicated empty volume mounted at `/workspace`.
   - When a new tenant arrives, an idle container is assigned immediately and a new idle container is spawned to refill the pool.

3) **Agent Runner (in container)**
   - LLM runtime + tools (bun/uv/vercel/gemini) baked into base image.
   - Only `/workspace` is mounted; no host access.
   - Initializes tenant workspace and runs the full design/build/deploy flow.

4) **Worker**
   - Background jobs (repo pushes, notifications, cleanup, retries).

## Tenant Workspace (Persistent)
```
data/tenants/<tenant_id>/
  memory.md
  tasks/
  site/
  assets/
  tooling/
  tooling.lock
```
- `tooling/` persists tenant-specific CLIs; base image provides common tools.

## Hot-Pool Assignment Flow (First Message)
1) Webhook hits orchestrator.
2) Orchestrator pops an idle container from the pool.
3) Container seeds `/workspace` and starts LLM immediately.
4) LLM sends contextual acknowledgement (no generic “please wait”).
5) Pool manager spawns a new idle container in the background.

## GitHub Repo Automation (Per Tenant)
- GitHub App installed org-wide (one-time setup).
- On tenant creation:
  1) Create repo via API.
  2) Install App on repo.
  3) Initialize repo in `site/` and push.
- After each successful deploy, push updated `site/`.
- `.gitignore` excludes `memory.md`, `tasks/`, `assets/`.

## Vercel Deployment
- One Vercel project per tenant under a shared org.
- Deploy from within container using Vercel CLI.
- Store project ID + latest deploy URL in SQLite.

## Event / Ping Service
- Tenant sites post events to `/events` on orchestrator.
- Orchestrator validates HMAC + rate limits per tenant.
- Worker delivers notifications (Telegram/WhatsApp/SMS/email).
- Aggregation supported (e.g., “5 signups in 10 minutes”).

## Isolation & Safety
- Containers run as non-root with CPU/memory/time limits.
- Only the tenant’s volume is mounted (no parent directory mounts).
- Orchestrator enforces safe path resolution; no filesystem tools are exposed.

## Scaling Path (No Redesign)
1) Add more VPS nodes; scheduler assigns runs to worker nodes.
2) Move SQLite → Postgres.
3) Add Redis/queue for higher throughput.
4) Move assets to object storage (S3/GCS) if needed.

# IMPLEMENTATION.md

## Goal (MVP)
Ship a minimal working system where:
- A user messages a Telegram bot (WhatsApp later)
- A website is built or edited autonomously
- The site is deployed live
- The user receives a link and can iterate by chat

This document outlines the architecture and runtime strategy at a high level.

---

## High-Level Architecture
The system consists of:
1. A chat-facing orchestrator
2. A per-tenant workspace + container
3. An agent runtime (Claude Agent SDK)
4. A deployment target (Vercel)
5. A lightweight persistence layer (SQLite)

All components can run on a single VPS for MVP.

---

## Core Components

### 1) Chat Orchestrator (Control Layer)
Responsibilities:
- Receive Telegram (later WhatsApp) messages
- Normalize to a single message schema
- Identify tenant (chat ID / phone)
- Decide: new build vs edit
- Write a task brief
- Route work to the agent runtime service
- Return results + links back to the user

The orchestrator does not build sites itself.

---

### 2) Tenant Workspace (Per Client)
Persistent on disk:
- `memory.md` for stable facts and decisions
- `tasks/` for task briefs and results
- `assets/` for uploaded and sourced images
- `site/` for the website codebase

---

### 3) Per-Tenant Container (Execution)
Each tenant has a dedicated container:
- Created on first interaction, reused across sessions
- Only mounted to that tenant's workspace
- Hard CPU/memory limits
- Uses a single shared account for Claude, Gemini, and Vercel (MVP)

---

### 4) Claude Agent SDK (Primary Agent Runtime)
Claude is the primary coordinator for task execution.

Responsibilities:
- Read `memory.md` and task brief
- Decide changes and file edits
- Invoke Gemini CLI with `DESIGN.md` (headless) to implement design
- Update `memory.md` when stable facts change
- Produce a short result summary for the user
- Deploy using Vercel CLI

New site setup (Claude-managed commands):
- `bun create next-app@latest <app-name> --yes`
- `cd <app-name>`
- `bunx --bun shadcn@latest init`
- Store `<app-name>` in `tasks/app_name.txt`

Runtime choices:
- Streaming input mode for multi-turn chat and images
- Store per-tenant `session_id` to resume context
- Use `claude_code` preset system prompt
- Restrict tools via allowlist + permission mode
- Load plugins via `CLAUDE_PLUGINS` (comma-separated paths)
- Use skills from `.claude/skills` (vercel-cli, bun-next-shadcn)
- Use subagents (Task tool) for interaction UX updates.

---

### 5) Design + Deploy (Claude-driven)
Gemini and Vercel are invoked directly by Claude:

- Gemini CLI runs headlessly with `-p "$(cat DESIGN.md)"`.
- Context is provided via stdin (task, `memory.md`, `design_context.md`, current page).
- Gemini decides where to write files (no fixed output path).
- Claude runs `bun run build` to validate.
- Claude deploys via `vercel --prod --yes` and writes the URL to `tasks/deploy_url.txt`.

---

## Persistence Strategy (MVP)

### SQLite
Used for:
- Tenants and chat identifiers
- Message idempotency
- Execution runs and status
- Vercel project IDs and deployment URLs

### Markdown Memory (`memory.md`)
Stores:
- Business name + location
- Brand tone and preferences
- Language choices
- Important decisions (pricing, booking link)

---

## Core Flows

### Flow 1: First Message -> New Website
1. Message arrives
2. Orchestrator creates tenant workspace + container
3. Orchestrator writes task brief
4. Claude creates/boots the Next.js app (bun + shadcn)
5. Claude runs Gemini CLI headlessly with `DESIGN.md`
6. Claude validates with `bun run build`
7. Claude deploys via Vercel CLI and writes `tasks/deploy_url.txt`
8. User receives link + suggested next steps

### Flow 2: Edit Existing Website
1. Message arrives
2. Orchestrator loads memory + context
3. Claude runs Gemini CLI headlessly with `DESIGN.md`
4. Claude validates with `bun run build`
5. Claude redeploys via Vercel CLI, records the URL via `record_deploy`, and sends the link
6. User receives confirmation + link

---

## Messaging Reliability (MVP)
Handle:
- Duplicate messages
- Out-of-order delivery
- Provider retries

Minimum approach:
- Idempotency key per message (provider message ID + tenant)
- Store last processed ID or timestamp
- Ignore duplicates once processed

## Chatty UX (MVP)
- Claude sends a context-aware “on it” message via `mcp__claudius-chat__send_message`.
- Claude can send interim updates using the same tool (after `should_send_message`).
- Claude sends the final completion + live URL itself (after calling `record_deploy`).
- Orchestrator maintains `tasks/chat_history.md` (last N messages) and `tasks/chat_summary.md` (compact memory).
- When logs exceed ~30 entries, it writes `tasks/summary_prompt.md` for the agent to refresh summary and prune logs.

---

## Concurrency Model (MVP)
- One active task per tenant
- Global concurrency cap
- Orchestrator manages concurrent inputs and routes to agent runtime
- No per-tenant queue beyond "one in-flight task"

---

## What We Are Not Solving Yet
- Billing / subscriptions
- Rate limiting / cost enforcement
- Advanced security guardrails
- User dashboards
- Custom domains
- Analytics

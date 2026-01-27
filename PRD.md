# PRD.md

## Product Summary
A Telegram-first (WhatsApp-following) chat interface that builds, deploys, and edits SMB websites through conversation. Users describe what they want; the system generates a live site and iterates via chat without dashboards or templates.

---

## Goals (MVP)
- Ship a working Telegram bot that can create and edit live websites via chat.
- Deliver a first live site within minutes.
- Make edits feel effortless and conversational.
- Keep the system simple, hackable, and easy to operate on a single VPS.

## Non-Goals (MVP)
- Billing, subscriptions, or hard cost enforcement
- User dashboards or visual editors
- Custom domains
- Analytics dashboards
- Advanced SEO tooling
- Multi-user access per tenant
- Marketing automation

---

## Target Users
### Primary
- SMB owners (barbers, salons, gyms, restaurants, clinics, local services)
- Non-technical operators
- Founders who prefer chat-based workflows

### Secondary (later)
- Solo founders
- Indie hackers
- Agencies managing multiple client sites

---

## Core Value Proposition
Describe what you want in Telegram (later WhatsApp) → get a live website → edit it by texting.

Users never:
- open an editor
- choose templates
- manage hosting
- touch code

---

## MVP Scope
### Messaging Interface
- Telegram as initial chat surface
- WhatsApp support after initial MVP
- Text messages + image uploads
- Short, guided responses (no long explanations)

### Website Capabilities
- One-page or simple multi-page sites
- Dynamically generated layouts (no templates)
- Common sections generated as needed:
  - Hero
  - Services
  - Pricing
  - Gallery
  - Testimonials
  - Location / Map
  - Contact / WhatsApp or Telegram CTA
- Mobile-first and SEO-friendly by default

### Image Strategy
- Pull images from Unsplash (no AI image generation)
- Auto resize/crop for web
- Store in tenant assets
- Allow user overrides via chat uploads

### Deployment
- Automated deploy after first build and every edit
- Vercel hosting under a single MVP account
- Store and reuse deployment URLs

---

## Success Metrics (Early)
- Time to first live site
- % of users requesting edits
- Average edits per user
- Repeat usage after first deploy
- Qualitative “wow” reactions

---

## Product Principles
1. Chat is the interface
2. No templates, no choices
3. Fast first result
4. Editability is the core loop
5. Opinionated by default

---

## System Overview (MVP)
All components can run on a single VPS.

### 1) Chat Orchestrator (Control Layer)
Responsibilities:
- Receive Telegram (later WhatsApp) messages
- Normalize into a single message schema
- Identify tenant (chat ID or phone)
- Decide: new build vs edit
- Write a task brief
- Trigger execution in tenant environment
- Return result message and link

### 2) Tenant Workspace (Per Client)
Persistent on disk:
- `memory.md` (long-term facts and decisions)
- `tasks/` (task briefs + results)
- `assets/` (uploaded/sourced images)
- `site/` (website codebase)

### 3) Per-Tenant Container (Execution)
Dedicated container per tenant:
- Created on first interaction, reused across sessions
- Mounted only to that tenant’s workspace
- Hard CPU/memory limits
- Single shared API accounts for MVP (Claude/Gemini/Vercel)

### 4) Deployment Target
- Vercel projects per tenant
- Created once and reused
- URL stored in DB

---

## Agent Runtime Strategy (Claude Agent SDK)
We can replace the “CC service” with Claude Agent SDK.

### Why Agent SDK
- Streaming input mode supports multi-turn chat + images
- Sessions provide continuity and resumable context
- Built-in tools (read/write/edit/bash) match codegen needs
- Permissions and hooks provide safe defaults
- MCP and custom tools allow controlled extensions
- Structured outputs enable deterministic summaries

### Requirements
- Use streaming input (not single-message) for chat UX
- Store `session_id` per tenant and `resume` for edits
- Set `systemPrompt` to `claude_code` preset
- Provide `settingSources` if using CLAUDE.md
- Restrict tools with allowlist and permission mode

### Tooling & Extensions
- Built-in tools: file edits, bash, search
- MCP tools (optional) for deploy or image fetch
- Hooks for logging, policy enforcement, and approvals
- Skills loaded from `.claude/skills` (e.g., `vercel-cli`, `bun-next-shadcn`)
- Subagents used for parallelizable tasks (e.g., chatty UX updates).

---

## Design Generation Strategy (Gemini CLI + DESIGN.md)
We will use Gemini CLI for the actual site design implementation.

Approach:
- The design prompt is always `DESIGN.md`.
- Claude does not craft a separate prompt; it provides context via workspace files (task brief, `memory.md`, assets).
- Claude invokes Gemini CLI headlessly with `-p "$(cat DESIGN.md)"`.
- Gemini decides where to write files (no fixed output path).
- Claude validates with `bun run build`, then deploys via Vercel CLI.

Goal:
- Produce a “magic moment” with truly unique, high‑quality visual designs.

### DESIGN.md Prompt Contract
`DESIGN.md` should define:
- Required context sources (task brief, `memory.md`, assets)
- Output expectations (mobile-first, unique layout, include CSS variables)
- Do/avoid constraints (e.g. “no templates”, “minimal copy”)

---

## Persistence Strategy
### SQLite
Track:
- Tenants
- Latest deployment URL
- Vercel project IDs
- Message idempotency
- Execution runs / status

### Markdown Memory (`memory.md`)
Stores:
- Business name + location
- Brand tone and style cues
- Language choices
- Important decisions (pricing, booking link)

---

## Core Flows
### 1) First Message → New Website
1. Message arrives
2. Orchestrator creates tenant workspace + container
3. Task brief written
4. Agent builds site, updates memory, fetches images
5. Deploy to Vercel
6. Return live link + next steps

### 2) Edit Existing Website
1. Message arrives
2. Orchestrator loads memory + task context
3. Agent applies change + updates memory
4. Redeploy
5. Return confirmation + link

---

## Messaging Reliability (MVP)
Must handle:
- Duplicate messages
- Out-of-order delivery
- Retries from chat provider

Minimum approach:
- Idempotency key per message (provider message ID + tenant)
- Ignore duplicates once processed
- Store last processed message timestamp or ID

## Chatty UX (MVP)
- Agent writes a context-aware “on it” message to `tasks/outbox.jsonl` at the start of each run.
- Agent may enqueue additional user-facing updates via `tasks/outbox.jsonl`.
- Orchestrator streams outbox updates during the run, then sends the final response via `tasks/response.json`.
- Chatty subagent reads `tasks/chat_history.md` and `tasks/chat_summary.md` to avoid duplicate replies.
- After ~30 messages, a compaction prompt is generated to refresh `tasks/chat_summary.md`.

---

## Concurrency Model (MVP)
- One active task per tenant
- Global concurrency cap
- Orchestrator routes inputs to the agent runtime service
- No per-tenant queue required beyond “one in-flight task”

---

## Security & Safety (MVP)
- Per-tenant container isolation and volume mounts
- Hard CPU/memory limits
- Strict tool allowlist in Agent SDK
- Secrets injected per container (single MVP account keys)
- Logs retained per execution

---

## Risks / Open Questions
- WhatsApp approval timeline and reliability
- Image licensing edge cases and user-supplied content
- Agent tool misuse or runaway tasks
- Handling complex edits without an explicit planning agent

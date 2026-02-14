# PM Agent Spec: Autonomous Project Manager

## Overview

A per-tenant background agent with a persistent session that proactively drives work forward. Instead of waiting for the user to initiate every action, the PM agent wakes on events and periodic heartbeats, assesses the tenant's current state, and either auto-executes low-risk actions or proposes high-risk ones through the interaction agent.

The PM agent is the user's partner — it thinks ahead, follows up, catches problems, and keeps projects moving.

## Motivation

Today's flow is purely reactive:

```
User speaks → Interaction routes → Execution works → User gets result
```

The gap: after every completed step, the system goes dormant until the user thinks of the next thing to say. This creates friction for users who:

- Don't know what to ask for next
- Forget to follow up on stalled conversations
- Miss issues with their deployed sites
- Want a "set it and forget it" experience for ongoing site management

The PM agent fills this gap:

```
Event/Timer fires → PM triages → PM plans → PM acts or suggests → User informed
```

## Architecture

### Position in the Agent Hierarchy

```
┌─────────────────────────────────────────────────────────┐
│                        USER                              │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│              INTERACTION AGENT                           │
│  (User relationship owner — routing + messaging)        │
└────────┬───────────────────────────────┬────────────────┘
         │                               │
         ▼                               ▼
┌─────────────────────┐   ┌──────────────────────────────┐
│  EXECUTION AGENTS   │   │         PM AGENT              │
│  (Build/deploy)     │   │  (Proactive planning +       │
│                     │   │   autonomous operations)      │
└─────────────────────┘   └──────────────────────────────┘
```

The PM agent sits alongside execution agents, not above them. It communicates with the user exclusively through the interaction agent (via the outbox → instruction session flow). It can kick off execution runs through the orchestrator, just like the interaction agent does today.

### Scope: Per-Tenant

One PM agent instance per tenant. It has visibility across all of that tenant's projects, can prioritize between them, and maintains a holistic view of the tenant's goals and state.

## Trigger System

The PM agent wakes on two categories of triggers, both built on the existing SchedulerWorker infrastructure.

### Event-Driven Triggers

These fire in response to meaningful state changes:

| Event | Source | What PM Does |
|---|---|---|
| `run_completed` | PendingWorker finalizes run | Post-deploy QA, suggest next steps, update PM plan |
| `run_failed` | PendingWorker finalizes run | Diagnose failure, decide retry vs. escalate to user |
| `user_idle` | Cron checks last message time | Follow up if conversation was mid-flow |
| `deploy_completed` | DevOps records deploy URL | Health check the live site, verify functionality |
| `billing_event` | Stripe webhook | Adjust plan based on subscription tier changes |
| `message_received` | Orchestrator records message | Update PM awareness (passive — no action unless relevant) |

### Periodic Triggers

| Trigger | Frequency | Purpose |
|---|---|---|
| `daily_heartbeat` | Daily (configurable) | Strategic review: plan progress, research staleness, suggestions |
| `health_check` | Hourly | System health: stuck messages, zombie runs, failed deliveries |
| `idle_check` | Every 6 hours | User engagement: follow up on stalled conversations |
| `first_heartbeat` | Every 2 hours (temporary) | Onboarding: fires until PM has enough context, then disables |

Implementation: Each trigger is a `webhook_condition` entry in `tenant_state(namespace='scheduler')` that watches `tenant_events` for the matching `event_type`.

### Periodic Cron Trigger

A single daily heartbeat (configurable per tenant) that runs regardless of events:

```json
{
  "trigger_id": "pm-heartbeat",
  "trigger_type": "cron",
  "name": "PM Agent Daily Heartbeat",
  "enabled": true,
  "cron": "0 10 * * *",
  "intent": "pm_heartbeat",
  "output_event_type": "pm_trigger",
  "payload": { "trigger": "daily_heartbeat" },
  "time_window": {
    "start": "09:00",
    "end": "18:00",
    "timezone": "America/New_York"
  }
}
```

The time window respects the tenant's timezone to avoid sending messages at 3am.

### Trigger Registration

When a tenant is first onboarded (or opts into PM features), the orchestrator registers the full set of PM triggers:

```python
async def register_pm_triggers(self, tenant_id: int, config: PMConfig):
    # Daily heartbeat
    self.db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-heartbeat", {
        "trigger_type": "cron",
        "cron": config.heartbeat_cron,  # default: "0 10 * * *"
        "enabled": True,
        "output_event_type": "pm_trigger",
        "intent": "pm_heartbeat",
        "payload": {"trigger": "daily_heartbeat"},
        "time_window": config.time_window,
    })

    # Post-run trigger
    self.db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-post-run", {
        "trigger_type": "webhook_condition",
        "event_type": "agent_usage",  # fires on run completion
        "enabled": True,
        "output_event_type": "pm_trigger",
        "intent": "pm_post_run",
        "payload": {"trigger": "post_run"},
    })

    # User idle trigger (handled by cron checking last message time)
    self.db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-idle-check", {
        "trigger_type": "cron",
        "cron": "0 */6 * * *",  # Check every 6 hours
        "enabled": True,
        "output_event_type": "pm_trigger",
        "intent": "pm_idle_check",
        "payload": {"trigger": "idle_check"},
    })

    # Conversation health check (hourly)
    self.db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-health-check", {
        "trigger_type": "cron",
        "cron": "30 * * * *",  # Every hour at :30
        "enabled": True,
        "output_event_type": "pm_trigger",
        "intent": "pm_health_check",
        "payload": {"trigger": "health_check"},
    })

    # First heartbeat (onboarding — fires frequently until ready, then self-disables)
    self.db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-first-heartbeat", {
        "trigger_type": "cron",
        "cron": "0 */2 * * *",  # Every 2 hours until ready
        "enabled": True,
        "output_event_type": "pm_trigger",
        "intent": "pm_first_heartbeat",
        "payload": {"trigger": "first_heartbeat"},
    })
```

## Two-Phase Heartbeat Pipeline

Every PM trigger flows through the same two-phase pipeline. This is the core cost-efficiency mechanism.

### Phase 1: Triage (Cheap)

**Model**: `claude-sonnet-4-5` (reliable judgment at low cost)
**Purpose**: "Is there anything worth doing right now?"
**Max cost per triage**: ~$0.01-0.02

The triage phase reads a compact context snapshot and returns a structured decision:

```
Input:
  - pm_state.json (PM's own state: current plan, tracked items, pending suggestions)
  - trigger metadata (what fired and why)
  - project_summary.json (generated periodically: deploy status, last run result, site health)
  - last_interaction_summary (when user last spoke, what about, any pending questions)
  - billing_status.json

Output (JSON):
  {
    "action_needed": true|false,
    "reason": "Site deployed 2h ago, no QA run yet",
    "priority": "low|medium|high",
    "suggested_actions": [
      {
        "type": "auto_execute|suggest_to_user",
        "action": "qa_check|follow_up|suggest_feature|health_check|...",
        "description": "Run QA check on deployed site",
        "context": { ... }
      }
    ]
  }
```

If `action_needed` is false, the PM goes back to sleep. No further cost.

### Phase 2: Planning & Action (Full)

**Model**: `claude-opus-4-6` (highest judgment)
**Purpose**: Plan and execute the actions identified in triage
**Only runs when**: Triage says `action_needed: true`

The PM is the highest-judgment role in the system. Execution agents do mechanical work (build, deploy). The PM does strategic thinking — what should this user's site become, when to speak up vs. shut up, how to synthesize research into a compelling recommendation. This is where model intelligence pays for itself directly: fewer annoying messages, higher suggestion approval rates, more coherent long-term plans.

This phase uses the PM agent's persistent session and has access to the full tenant context. It can:

1. **Auto-execute** low-risk actions (see Tiered Autonomy below)
2. **Conduct research** (competitor analysis, SEO audits, industry context)
3. **Draft suggestions** informed by research and send them through the interaction agent
4. **Update PM state** (plan progress, tracked items, decisions made)
5. **Kick off execution runs** via the orchestrator

```
Input:
  - Full PM session context (persistent)
  - Triage output (actions to take)
  - Project workspace files (memory.md, DESCRIPTION.md, deploy status, etc.)

Output:
  - Actions taken (logged to pm_actions)
  - Messages enqueued (via outbox → interaction agent → user)
  - PM state updated (pm_state.json)
  - Execution runs created (if auto-approved)
```

### Cost Model

| Phase | Model | Avg tokens | Avg cost | Frequency |
|---|---|---|---|---|
| Triage (no action) | Sonnet 4.5 | ~2K in / 200 out | ~$0.01 | Per trigger |
| Triage (action needed) | Sonnet 4.5 | ~2K in / 500 out | ~$0.015 | Per trigger |
| Planning/Action | Opus 4.6 | ~10K in / 2K out | ~$0.20 | Only when action needed |
| Research (auto) | Opus 4.6 + tools | ~15K in / 3K out | ~$0.30 | Weekly or on-demand |

**Estimated monthly cost per active tenant** (~50 triggers/month: 30 daily heartbeats + ~20 event triggers):
- Mostly-idle tenant: ~$0.50/mo (triage only, rarely acts)
- Active tenant: ~$5-10/mo (triage + 2-3 action phases/week + weekly research)

**Why this model split**:

| Role | Model | Why |
|---|---|---|
| Triage | Sonnet | Needs real judgment ("when to shut up"), but not full strategic thinking. Sonnet catches ambiguous cases that Haiku would miss. Net cheaper than Haiku because fewer false positives → fewer wasted Opus calls. |
| Action | Opus | Highest-judgment role in the system. Strategic thinking, research synthesis, compelling suggestion framing. Every bad suggestion erodes trust. The cost difference vs Sonnet (~4x) is justified because PM runs infrequently and quality directly drives retention. |
| Execution | Sonnet | Mechanical work (build, deploy, code). Sonnet is excellent at this. No judgment calls about user relationship. |

## Tiered Autonomy

The mental model: **auto tier gathers intelligence, suggest tier acts on it.** Research flows into suggestions, but the user only sees the actionable output.

### Auto-Execute (Low Risk)

Actions the PM can take without user approval. These are all read-only or informational — they never modify the user's site.

**Validation & Monitoring:**

| Action | Description | How |
|---|---|---|
| **QA Check** | Hit deployed URL, verify it loads, check for console errors | Lightweight execution run with browser check |
| **Health Monitor** | Check if site is up, SSL valid, no broken images | HTTP checks via execution or direct |
| **Build Validation** | Re-run build after dependency updates | Execution run (build only, no deploy) |
| **Tech Audit** | Check dependency vulnerabilities, framework version freshness, bundle size | Execution run with analysis tools |

**Research & Intelligence:**

| Action | Description | How | Frequency |
|---|---|---|---|
| **Competitor Analysis** | Scrape 3-5 competitor sites, analyze features/sections they have that tenant doesn't | Firecrawl + Opus synthesis | Weekly |
| **SEO/Perf Audit** | Run Lighthouse, check meta tags, missing alt text, Core Web Vitals | Execution run with Lighthouse | Weekly |
| **Content Gap Analysis** | Check for missing privacy policy, FAQ, contact info, business hours | Read site files + industry standards | Post-deploy |
| **Industry Research** | Look up what's standard for this business type (dentist, restaurant, etc.) | Web research + Opus synthesis | On first deploy, then monthly |

Research results are written to `pm_state.json` under a `research` key. The research itself runs silently, but the PM should follow up research with a **research-informed suggestion** in the same heartbeat when findings are actionable. Research without follow-through is wasted intelligence.

**Communication & State:**

| Action | Description | How |
|---|---|---|
| **Follow-Up** | Remind user about unanswered questions after idle period | Outbox → interaction agent |
| **Memory Update** | Update PM state, project summaries, tracked items | Write to pm_state.json |
| **Status Report** | Send periodic progress summary to user | Outbox → interaction agent |

### Suggest to User (High Risk)

Actions that require explicit user approval. These are informed by auto-tier research — the PM doesn't just suggest generically, it brings receipts.

| Action | Description | Example (research-informed) | How |
|---|---|---|---|
| **New Feature** | Add functionality the site is missing | "I checked your top 3 competitors — all have online booking. Want me to add it?" | Outbox → interaction agent → user decides |
| **Design Change** | Improve visual or UX quality | "Your Lighthouse performance score is 45. Main culprit is unoptimized images. Want me to fix?" | Outbox → interaction agent → user decides |
| **Content Addition** | Add missing content or pages | "Your site has no privacy policy. For GDPR compliance you probably need one. Want me to draft it?" | Outbox → interaction agent → user decides |
| **Deploy** | Push changes live | "I fixed the build error. Ready to redeploy?" | Outbox → interaction agent → user approves |
| **Schema Change** | Database or backend modifications | "Adding a contact form needs a database table" | Outbox → interaction agent → user approves |
| **New Project** | Create additional sites/properties | "Want me to set up a blog alongside your main site? Your competitors all have one." | Outbox → interaction agent → user approves |
| **Billing Action** | Usage or payment related | "You're approaching your usage limit" | Outbox → interaction agent → user decides |

### Approval Flow

When the PM wants to suggest something:

1. PM enqueues a suggestion via outbox with `type: "pm_suggestion"`
2. Interaction agent receives it, rewrites for user, sends via Telegram
3. User responds (yes/no/modify)
4. Orchestrator routes user response normally
5. Interaction agent recognizes it's a response to a PM suggestion (via context)
6. If approved: interaction agent routes to execution with PM's context
7. PM gets notified of the outcome via `run_completed` event

The PM does NOT wait synchronously for approval. It enqueues the suggestion and moves on. The response comes back asynchronously through the normal message flow.

## Session Management

### Persistent Session

The PM agent maintains a persistent Claude SDK session, stored in:

```
tenant_state(namespace='pm', key='session_id') → session UUID
```

Session cache files stored at:

```
PM_SESSION_CACHE_DIR/tenant-<tenant_id>/
```

This gives the PM continuity across heartbeats — it remembers what it planned, what it suggested, what the user said, and what happened.

### Session Lifecycle

```
Tenant onboarded → PM session created (first heartbeat)
                 → Session persists across heartbeats (months)
                 → Session rotated when cost threshold hit
                 → Session cleared on /reset or explicit user request
```

### Session Rotation

To prevent unbounded context growth and cost:

- **Cost cap**: If cumulative PM session cost exceeds threshold (configurable, default $5), rotate session. Write a comprehensive handoff summary to `pm_state.json` before clearing.
- **Time cap**: If session is older than 30 days, rotate with handoff.
- **Failure recovery**: If session resume fails, start fresh with `pm_state.json` as bootstrap context.

### Session Context Structure

The PM session accumulates:

```
[System prompt - PM agent identity and capabilities]
[Turn 1 - First heartbeat: read project state, made initial plan]
[Turn 2 - Post-deploy trigger: ran QA, found issue, suggested fix]
[Turn 3 - User approved fix, execution completed]
[Turn 4 - Daily heartbeat: all clear, updated plan]
...
```

Each heartbeat is a single turn in the session. The Claude SDK handles context compaction automatically when the window fills.

## PM State File

In addition to the persistent session, the PM maintains a structured state file that survives session rotations:

```
data/<tenant_key>/pm_state.json
```

```json
{
  "version": 1,
  "tenant_id": 123,
  "created_at": "2026-02-13T10:00:00Z",
  "updated_at": "2026-02-13T16:00:00Z",

  "projects": {
    "main": {
      "status": "active",
      "last_deploy_url": "https://example.vercel.app",
      "last_deploy_at": "2026-02-13T14:00:00Z",
      "last_qa_at": "2026-02-13T14:05:00Z",
      "qa_status": "pass",
      "open_issues": [],
      "planned_improvements": [
        {
          "id": "imp-1",
          "description": "Add testimonials section",
          "priority": "medium",
          "status": "suggested",
          "suggested_at": "2026-02-13T10:00:00Z",
          "user_response": null
        }
      ]
    }
  },

  "research": {
    "last_competitor_scan_at": "2026-02-12T10:00:00Z",
    "competitors": [
      {
        "url": "https://competitor-a.com",
        "features": ["booking", "testimonials", "blog", "FAQ"],
        "scanned_at": "2026-02-12T10:00:00Z"
      }
    ],
    "last_seo_audit_at": "2026-02-13T14:05:00Z",
    "seo_audit": {
      "lighthouse_performance": 45,
      "lighthouse_seo": 82,
      "missing_meta_tags": ["og:image"],
      "missing_alt_text": 3,
      "core_web_vitals": {"LCP": 3.2, "FID": 120, "CLS": 0.15}
    },
    "content_gaps": ["privacy_policy", "FAQ"],
    "industry_standards": {
      "business_type": "dentist",
      "expected_features": ["booking", "testimonials", "services", "team", "contact", "FAQ"],
      "researched_at": "2026-02-12T10:00:00Z"
    },
    "declined_suggestions": [
      {
        "description": "Add a blog",
        "declined_at": "2026-02-13T12:00:00Z",
        "reason": "user said 'not now'"
      }
    ]
  },

  "conversation_state": {
    "last_user_message_at": "2026-02-13T12:00:00Z",
    "pending_questions": [
      "Do you have customer testimonials you'd like to include?"
    ],
    "last_pm_message_at": "2026-02-13T14:05:00Z"
  },

  "plan": {
    "current_phase": "post-launch-optimization",
    "milestones": [
      {"name": "Initial site live", "status": "done", "completed_at": "2026-02-12T18:00:00Z"},
      {"name": "QA pass", "status": "done", "completed_at": "2026-02-13T14:05:00Z"},
      {"name": "SEO basics", "status": "pending"},
      {"name": "Analytics setup", "status": "pending"}
    ]
  },

  "metrics": {
    "total_heartbeats": 5,
    "total_actions_taken": 3,
    "total_suggestions_made": 1,
    "total_suggestions_approved": 1,
    "total_suggestions_rejected": 0,
    "total_research_runs": 2,
    "total_cost_usd": 1.50,
    "session_rotations": 0
  }
}
```

## Database Changes

### New Table: `pm_heartbeats`

Tracks every PM wake-up for observability and cost tracking:

```sql
CREATE TABLE IF NOT EXISTS pm_heartbeats (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    trigger_type TEXT NOT NULL,          -- 'daily_heartbeat', 'post_run', 'idle_check', etc.
    trigger_event_id BIGINT,             -- event_job id that triggered this, if any

    -- Triage phase
    triage_result JSONB,                 -- Full triage output
    action_needed BOOLEAN NOT NULL,
    triage_cost_usd DOUBLE PRECISION,
    triage_model TEXT,

    -- Action phase (null if triage said no action)
    action_result JSONB,
    action_cost_usd DOUBLE PRECISION,
    action_model TEXT,

    -- Totals
    total_cost_usd DOUBLE PRECISION NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_pm_heartbeats_tenant ON pm_heartbeats (tenant_id, created_at DESC);
```

### New Table: `pm_actions`

Tracks individual actions taken or proposed:

```sql
CREATE TABLE IF NOT EXISTS pm_actions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    heartbeat_id BIGINT REFERENCES pm_heartbeats(id),
    action_type TEXT NOT NULL,           -- 'qa_check', 'follow_up', 'suggest_feature', etc.
    autonomy_tier TEXT NOT NULL,         -- 'auto' or 'suggest'
    description TEXT NOT NULL,

    -- For auto-executed actions
    execution_run_id BIGINT REFERENCES runs(id),

    -- For suggestions
    outbox_id UUID REFERENCES outbox(id),
    user_response TEXT,                  -- 'approved', 'rejected', 'modified', null (pending)
    user_response_at TIMESTAMPTZ,

    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'executing', 'completed', 'rejected', 'failed'
    result_summary TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pm_actions_tenant ON pm_actions (tenant_id, created_at DESC);
CREATE INDEX idx_pm_actions_status ON pm_actions (status) WHERE status IN ('pending', 'executing');
```

### Tenant State Keys (namespace: "pm")

```
pm:session_id          → PM agent Claude SDK session UUID
pm:config              → PM configuration (heartbeat cron, autonomy overrides, etc.)
pm:enabled             → boolean, whether PM is active for this tenant
pm:last_heartbeat_at   → timestamp of last heartbeat
pm:session_cost_usd    → cumulative session cost (for rotation tracking)
```

## Worker: PMWorker

A new worker alongside EventWorker, PendingWorker, OutboxWorker, and SchedulerWorker.

### Responsibilities

1. **Claim PM trigger event_jobs**: Filter event_jobs where `payload.trigger` starts with PM prefix
2. **Run triage phase**: Call cheap model with compact context
3. **Run action phase**: If triage says action needed, call full model with persistent session
4. **Record heartbeat**: Log to `pm_heartbeats` table
5. **Execute auto actions**: Create execution runs via orchestrator
6. **Enqueue suggestions**: Write to outbox for interaction agent delivery

### Configuration

```python
PM_WORKER_ENABLED=true

@dataclass
class PMWorkerConfig:
    poll_interval: float = 5.0
    batch_size: int = 10
    triage_model: str = "claude-sonnet-4-5"
    action_model: str = "claude-opus-4-6"
    triage_max_tokens: int = 1024
    action_max_thinking_tokens: int = 2048
    session_cost_rotation_threshold_usd: float = 5.0
    session_age_rotation_days: int = 30
    max_actions_per_heartbeat: int = 3
    cooldown_between_user_messages_seconds: int = 3600  # Don't message user more than once/hr
    idle_threshold_hours: int = 48  # Consider user idle after this
```

### Processing Flow

```python
async def process_pm_trigger(self, tenant_id: int, trigger_payload: dict):
    # 1. Check if PM is enabled for tenant
    enabled = self.db.get_tenant_kv(tenant_id, "pm", "enabled")
    if not enabled:
        return

    # 2. Load compact context for triage
    context = await self._build_triage_context(tenant_id, trigger_payload)

    # 3. Run triage (cheap model)
    triage_result = await self._run_triage(tenant_id, context)

    # 4. Record heartbeat
    heartbeat_id = self.db.create_pm_heartbeat(
        tenant_id=tenant_id,
        trigger_type=trigger_payload["trigger"],
        triage_result=triage_result,
        action_needed=triage_result["action_needed"],
        triage_cost_usd=triage_result["cost"],
    )

    if not triage_result["action_needed"]:
        return  # Go back to sleep

    # 5. Run action phase (full model, persistent session)
    session_id = self.db.get_tenant_kv(tenant_id, "pm", "session_id")
    action_result = await self._run_action_phase(
        tenant_id, session_id, triage_result
    )

    # 6. Process each action
    for action in action_result["actions"]:
        if action["type"] == "auto_execute":
            await self._auto_execute(tenant_id, heartbeat_id, action)
        elif action["type"] == "suggest_to_user":
            await self._suggest_to_user(tenant_id, heartbeat_id, action)

    # 7. Update PM state
    await self._update_pm_state(tenant_id, action_result)

    # 8. Update heartbeat record with action results
    self.db.update_pm_heartbeat(heartbeat_id, action_result=action_result, ...)
```

## PM Agent Prompts

### Triage Prompt

```markdown
You are the PM triage agent for tenant {{tenant_id}}.

Your job: quickly decide if any action is needed right now. Be conservative —
only flag actions when there's clear value. The user is paying for your attention.

## Trigger
{{trigger_type}}: {{trigger_description}}

## Current State
{{compact_project_summary}}

## Recent Activity
- Last user message: {{last_user_message_at}} ("{{last_user_message_preview}}")
- Last PM message: {{last_pm_message_at}}
- Last deploy: {{last_deploy_at}} → {{last_deploy_url}}
- Last run: {{last_run_status}} at {{last_run_at}}
- Open issues: {{open_issues_count}}

## PM Plan
{{current_plan_summary}}

## Research State
- Last competitor scan: {{last_competitor_scan_at}} ({{competitor_count}} competitors tracked)
- Last SEO audit: {{last_seo_audit_at}} (Lighthouse: {{lighthouse_score}})
- Content gaps: {{content_gaps}}
- Declined suggestions: {{declined_suggestions}}

## Instructions
Return JSON only. No explanation.
{
  "action_needed": bool,
  "reason": "one line why",
  "priority": "low|medium|high",
  "suggested_actions": [
    {
      "type": "auto_execute|suggest_to_user",
      "action": "action_name",
      "description": "what and why",
      "context": {}
    }
  ]
}

Rules:
- Never suggest more than 3 actions per heartbeat
- Don't message the user more than once per hour
- Don't suggest things the user already declined (see declined_suggestions above)
- If the trigger is daily_heartbeat and everything is fine, return action_needed: false
- QA checks after deploys are always auto_execute
- Research actions (competitor_scan, seo_audit, content_gap_analysis) are auto_execute
- Research should refresh weekly at most — check last_*_at timestamps
- New features and design changes are always suggest_to_user
- Suggestions must be backed by research — if no research exists yet, run research first
```

### Action Phase Prompt (System)

```markdown
You are Demi's Project Manager agent for {{tenant_name}}.

You are the user's proactive partner. You think ahead, catch problems, and keep
their projects moving forward. You never speak to the user directly — all
communication goes through the interaction agent via the outbox.

## Your Capabilities

### Auto-Execute (do these without asking)

**Validation & Monitoring:**
- QA checks on deployed sites (hit URL, verify it loads)
- Health monitoring (uptime, broken links, missing images)
- Build validation after changes
- Tech audits (dependency vulnerabilities, framework versions)

**Research & Intelligence:**
- Competitor analysis — scrape competitor sites, catalog their features
- SEO/performance audits — Lighthouse scores, Core Web Vitals, meta tags
- Content gap analysis — missing privacy policy, FAQ, standard pages
- Industry research — what's standard for this business type
- Write all research findings to pm_state.json under the `research` key

**Communication & State:**
- Follow-up reminders when user went idle mid-conversation
- Updating your own state, plans, and research findings
- Status reports the user opted into

### Suggest to User (always ask first)
Suggestions should be INFORMED BY RESEARCH. Don't suggest generically —
bring receipts. "I checked your top 3 competitors" is better than "you should
consider adding."

- New features or pages (backed by competitor/industry research)
- Design/performance improvements (backed by Lighthouse/audit data)
- Content additions (backed by content gap analysis)
- Deployments
- Database/backend changes
- New project creation
- Anything that costs the user money or changes their site

## Communication Rules
- Send suggestions through the outbox with type "pm_suggestion"
- Keep messages SHORT. One idea per message. No walls of text.
- Frame suggestions as questions, not commands: "Want me to...?" not "I'm going to..."
- Include just enough context for the user to say yes/no — cite specific research
- If the user previously rejected a similar suggestion, don't repeat it
- Respect the cooldown — no more than 1 message per hour unless urgent
- Don't message the user just to say "I did research" — but DO follow up research
  with an actionable suggestion in the same heartbeat when findings warrant it.
  Research → immediate suggestion is the ideal flow. "I checked your competitors and
  found they all have booking — want me to add it?" is one message, not two.

## State Management
- Read and update pm_state.json each heartbeat
- Track what you've suggested, what was approved/rejected
- Maintain a lightweight project plan with milestones
- Record QA results, research findings, and issues found
- Track declined suggestions so you never repeat them

## Available Tools
- send_pm_update(text, priority, action_type) → enqueues to outbox
- create_execution_run(context, message) → kicks off execution agent
- read_project_state(project_name) → reads project files
- update_pm_state(updates) → updates pm_state.json
- check_site_health(url) → HTTP check + basic validation
- scrape_url(url) → firecrawl scrape for competitor/research
- run_lighthouse(url) → performance/SEO audit
- web_search(query) → search for industry standards, best practices
```

## Integration with Existing Systems

### How Event Jobs Route to PM

The existing SchedulerWorker already creates event_jobs when triggers fire. The PM triggers are configured with `output_event_type: "pm_trigger"`. The PMWorker filters for these:

```python
# In PMWorker
async def poll(self):
    jobs = self.db.fetch_pending_event_jobs(
        batch_size=self.config.batch_size,
        job_type_filter="pm_trigger"  # Only PM events
    )
    for job in jobs:
        await self.process_pm_trigger(job.tenant_id, job.payload_json)
```

Alternatively, the EventWorker can be extended to recognize PM events and delegate to the PMWorker, keeping the single-consumer pattern.

### How PM Creates Execution Runs

The PM uses the same orchestrator path that interaction routing uses:

```python
async def _auto_execute(self, tenant_id, heartbeat_id, action):
    # Create a synthetic message for the execution run
    run_id = self.orchestrator.create_pm_run(
        tenant_id=tenant_id,
        execution_context=action["context"].get("execution_context", "PM: " + action["action"]),
        message=action["description"],
        pm_heartbeat_id=heartbeat_id,
    )

    # Record the action
    self.db.create_pm_action(
        tenant_id=tenant_id,
        heartbeat_id=heartbeat_id,
        action_type=action["action"],
        autonomy_tier="auto",
        description=action["description"],
        execution_run_id=run_id,
        status="executing",
    )
```

### How PM Sends Suggestions

Suggestions go through the same outbox → interaction agent pipeline:

```python
async def _suggest_to_user(self, tenant_id, heartbeat_id, action):
    correlation_id = f"pm-{heartbeat_id}-{action['action']}"

    outbox_id = self.db.enqueue_outbox(
        tenant_id=tenant_id,
        run_id=None,  # Not tied to a specific run
        project_name=action["context"].get("project_name"),
        correlation_id=correlation_id,
        payload={
            "type": "pm_suggestion",
            "action": "send_message",
            "text": action["description"],
            "priority": action.get("priority", "medium"),
            "pm_action_type": action["action"],
            "pm_heartbeat_id": heartbeat_id,
        }
    )

    self.db.create_pm_action(
        tenant_id=tenant_id,
        heartbeat_id=heartbeat_id,
        action_type=action["action"],
        autonomy_tier="suggest",
        description=action["description"],
        outbox_id=outbox_id,
        status="pending",
    )
```

The OutboxWorker already handles different payload types. It needs a small extension to recognize `type: "pm_suggestion"` and format the instruction appropriately for the interaction agent.

### How User Responses Route Back

When a user responds to a PM suggestion:

1. User says "yes, add testimonials" in Telegram
2. Orchestrator receives message, routes to interaction agent
3. Interaction agent has context from chat_history that the PM suggested testimonials
4. Interaction agent routes normally: `should_run: true`, `execution_context: "Add testimonials section"`
5. The execution runs
6. On `run_completed`, the PM's event trigger fires
7. PM triage recognizes this was a response to its suggestion
8. PM updates `pm_actions` status to "completed" and adjusts its plan

No special routing needed — the existing flow handles it naturally because the interaction agent has conversational context.

## Configuration & Opt-in

### Tenant-Level Config

```json
{
  "pm_enabled": true,
  "heartbeat_cron": "0 10 * * *",
  "timezone": "America/New_York",
  "quiet_hours": { "start": "21:00", "end": "08:00" },
  "autonomy_overrides": {
    "qa_check": "auto",
    "health_monitor": "auto",
    "follow_up": "auto",
    "suggest_feature": "suggest",
    "deploy": "suggest"
  },
  "max_daily_cost_usd": 0.50,
  "max_messages_per_day": 3
}
```

### Activation

**New tenants**: PM activates automatically after first successful deploy. The first deploy is the moment Demi has enough context (business type, site content, user communication style) to be useful.

**Existing tenants**: A one-time backfill migration creates PM agents for all existing tenants who have at least one successful deploy. The migration:

```python
async def backfill_pm_agents():
    """One-time migration to create PM agents for existing tenants."""
    tenants = await db.fetch_tenants_with_deploys()
    for tenant in tenants:
        if db.get_tenant_kv(tenant.id, "pm", "enabled") is not None:
            continue  # Already has PM config

        config = PMConfig.default_for_tenant(tenant)
        await register_pm_triggers(tenant.id, config)
        db.set_tenant_kv(tenant.id, "pm", "enabled", True)

        # Mark as needing first-heartbeat (research + plan creation)
        db.set_tenant_kv(tenant.id, "pm", "needs_onboarding", True)
```

**Can be disabled** via `/nopm` command or equivalent.

### First Heartbeat: The WOW Moment

The PM's first heartbeat should NOT wait for the weekly cron. It should fire soon after activation (within hours, not days) so the user experiences proactive value immediately.

But it also shouldn't fire blindly. The PM needs enough context to say something useful. The first heartbeat checks:

1. **Has the user had at least a few conversations?** (rapport established)
2. **Is there a deployed site to analyze?** (something concrete to research)
3. **Has the interaction agent built enough chat history?** (PM can read the relationship)

If all three are true, the first heartbeat runs a full cycle: research competitors, audit the site, and send a research-informed suggestion. This is the user's first "wow, it's thinking ahead for me" moment.

```python
# First heartbeat trigger — fires once, soon after PM creation
db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-first-heartbeat", {
    "trigger_type": "cron",
    "cron": "0 */2 * * *",  # Check every 2 hours until ready
    "enabled": True,
    "output_event_type": "pm_trigger",
    "intent": "pm_first_heartbeat",
    "payload": {"trigger": "first_heartbeat"},
})

# In PMWorker, first_heartbeat handler:
async def _handle_first_heartbeat(self, tenant_id):
    messages_count = self.db.count_tenant_messages(tenant_id)
    has_deploy = self.db.get_last_deploy_url(tenant_id) is not None
    chat_history = self._read_chat_history(tenant_id)

    if messages_count < 5 or not has_deploy or not chat_history:
        return  # Not ready yet — try again next interval

    # Ready! Run full research + suggestion cycle
    await self._run_onboarding_heartbeat(tenant_id)

    # Disable the frequent check, switch to normal daily cadence
    self.db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-first-heartbeat",
                          {**existing, "enabled": False})
    self.db.set_tenant_kv(tenant_id, "pm", "needs_onboarding", None)
```

## Conversation Health & Self-Healing

The PM has a unique vantage point: it can see the full system state across all tables, not just the workspace files. This makes it the ideal agent for detecting and correcting system-level issues that no other agent can see.

### Data Access

The PM's triage and action phases have read access to:

| Table | What PM Sees | Why |
|---|---|---|
| `messages` | Full chat history with statuses | Detect unprocessed messages, gaps in conversation |
| `runs` | All run statuses, durations, errors | Detect stuck/failed runs, cost anomalies |
| `run_inputs` | Queued inputs and their statuses | Detect stale queued messages never picked up |
| `outbox` | All outbound messages and delivery status | Detect failed deliveries the user never received |
| `active_runs` | Currently running executions | Detect zombie runs, lease expirations |
| `interaction_sessions` | Routing loop history | Detect confused or failed interaction loops |
| `execution_stream_inputs` | Mid-run user messages | Detect messages that were never streamed |
| `tenant_events` | Full audit log | Correlate events with outcomes |

### Health Check Beat

A dedicated hourly trigger specifically for conversation health:

```python
db.set_tenant_kv(tenant_id, "scheduler", "trigger:pm-health-check", {
    "trigger_type": "cron",
    "cron": "30 * * * *",  # Every hour at :30
    "enabled": True,
    "output_event_type": "pm_trigger",
    "intent": "pm_health_check",
    "payload": {"trigger": "health_check"},
})
```

### What the Health Check Detects

**Message-level issues:**
- Messages stuck in `received` status for > 5 min (never processed)
- Messages stuck in `processing` status for > 10 min (interaction loop hung)
- Gaps in conversation (user sent message but never got a response)
- Duplicate message processing (same provider_message_id processed twice)

**Run-level issues:**
- Runs stuck in `running` status with expired leases (zombie runs)
- Runs that completed but never sent a final update to the user
- Run failures that the user was never informed about
- Cost anomalies (run cost >> typical for this tenant)

**Queue-level issues:**
- `run_inputs` stuck in `queued` status for > 30 min (never claimed)
- `outbox` entries stuck in `sending` for > 5 min (delivery hung)
- `outbox` entries that failed all retry attempts (user never got the message)
- `execution_stream_inputs` stuck in `pending` (mid-run message never delivered)

**Interaction-level issues:**
- Interaction session that started but never completed (hung routing)
- Multiple interaction sessions overlapping (concurrency leak)
- Interaction agent returned invalid routing decision

### Health Check Actions

All health check actions are **auto-execute** — they fix system issues, not user-facing content:

| Issue | Auto-Fix |
|---|---|
| Stuck `received` message | Re-enqueue through orchestrator.handle_message() |
| Zombie run (expired lease) | Mark as `lease_expired`, notify user via outbox |
| Failed outbox delivery | Re-enqueue with fresh retry count |
| User never got run result | Enqueue a catch-up summary via outbox |
| Stale run_inputs | Force-drain via orchestrator._drain_run_inputs() |
| Hung interaction session | Mark as `failed`, clear session lock |

When the PM detects AND fixes an issue, it sends a brief, natural message to the user via the interaction agent — not "System error #4521 resolved" but "Sorry about the delay — I noticed your last request got stuck. I've kicked it off again."

### Health State in pm_state.json

```json
{
  "health": {
    "last_check_at": "2026-02-13T16:30:00Z",
    "status": "healthy",
    "issues_detected_24h": 0,
    "issues_auto_fixed_24h": 0,
    "last_issue": {
      "type": "stuck_outbox",
      "detected_at": "2026-02-13T10:30:00Z",
      "auto_fixed": true,
      "description": "Outbox delivery for run #456 failed 12 times. Re-enqueued."
    }
  }
}
```

### Triage Context for Health Checks

The health check triage gets a compact system health snapshot:

```markdown
## System Health Snapshot
- Messages: {{received_count}} received, {{processing_count}} processing, {{stuck_count}} stuck
- Runs: {{running_count}} running, {{zombie_count}} zombie (expired lease)
- Outbox: {{queued_count}} queued, {{sending_count}} sending, {{failed_count}} failed
- Run Inputs: {{queued_inputs_count}} queued, {{stale_inputs_count}} stale (>30min)
- Last successful interaction: {{last_successful_interaction_at}}
- Last successful run: {{last_successful_run_at}}
```

If everything is zeros/healthy, triage returns `action_needed: false` immediately. Cheap.

## Failure Modes & Safeguards

| Failure | Safeguard |
|---|---|
| PM session corrupted | Resume fails → start fresh with pm_state.json bootstrap |
| Triage model hallucinates action | Action phase validates against actual project state before executing |
| PM suggests something user already rejected | pm_state.json tracks rejections; triage prompt explicitly forbids repeats |
| PM costs spiral | Per-heartbeat cost cap, daily cost cap, session rotation threshold |
| PM messages too much | Cooldown enforcement (1hr default), daily message cap |
| PM auto-executes something destructive | Auto tier is strictly limited to read-only and validation actions; anything that modifies the site requires user approval |
| PM and user act simultaneously | PM runs create their own execution context; active_runs table prevents conflicts within same project |
| Trigger storm (many events at once) | Debounce: if a PM heartbeat ran within last N minutes, skip |
| PM health check fixes cause more issues | Health fixes are idempotent; re-enqueue is safe, marking runs as expired is safe. PM never deletes data. |
| PM health check creates noise | Health-related user messages only sent when there was a visible impact (user was waiting for something). Silent fixes for background issues. |

## Observability

### Admin Views

```sql
-- PM cost overview
CREATE VIEW admin_pm_costs AS
SELECT
    t.key as tenant,
    COUNT(h.id) as heartbeats,
    SUM(h.total_cost_usd) as total_cost,
    AVG(h.total_cost_usd) as avg_cost_per_heartbeat,
    SUM(CASE WHEN h.action_needed THEN 1 ELSE 0 END) as actions_taken,
    MAX(h.created_at) as last_heartbeat
FROM pm_heartbeats h
JOIN tenants t ON t.id = h.tenant_id
GROUP BY t.key;

-- PM action effectiveness
CREATE VIEW admin_pm_actions_summary AS
SELECT
    action_type,
    autonomy_tier,
    COUNT(*) as total,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
    SUM(CASE WHEN user_response = 'approved' THEN 1 ELSE 0 END) as approved,
    SUM(CASE WHEN user_response = 'rejected' THEN 1 ELSE 0 END) as rejected
FROM pm_actions
GROUP BY action_type, autonomy_tier;
```

### Logging

Every PM heartbeat logs:
- Trigger type and metadata
- Triage decision and reasoning
- Actions taken/proposed
- Cost breakdown (triage vs action phase)
- Duration

Stored in `pm_heartbeats` table + tenant_events for the standard observability pipeline.

## Rollout Plan

### Phase 1: Foundation
- [ ] Database migrations (pm_heartbeats, pm_actions)
- [ ] PMWorker skeleton (poll, claim, process)
- [ ] PM trigger registration on tenant onboard
- [ ] Triage prompt + Sonnet integration
- [ ] pm_state.json read/write
- [ ] Backfill migration for existing tenants

### Phase 2: Conversation Health (quick win, immediate value)
- [ ] Hourly health check trigger
- [ ] System health snapshot query (messages, runs, outbox, run_inputs)
- [ ] Auto-fix: stuck messages, zombie runs, failed deliveries
- [ ] User-facing recovery messages via outbox ("sorry about the delay")
- [ ] Health state in pm_state.json

### Phase 3: Auto Actions + First Heartbeat
- [ ] Post-deploy QA check (auto)
- [ ] Site health monitoring (auto)
- [ ] Idle follow-up (auto)
- [ ] Action phase with persistent Opus session
- [ ] First heartbeat onboarding flow (readiness check → research → suggestion)
- [ ] First heartbeat self-disabling after successful onboarding

### Phase 4: Research & Suggestions
- [ ] Competitor analysis (firecrawl integration)
- [ ] SEO/performance audits (Lighthouse)
- [ ] Content gap analysis
- [ ] Industry research
- [ ] Research → immediate suggestion flow (same heartbeat)
- [ ] Outbox extension for pm_suggestion type
- [ ] Interaction agent context for PM suggestions
- [ ] User response tracking in pm_actions
- [ ] Suggestion → execution flow

### Phase 5: Intelligence
- [ ] Multi-project awareness and prioritization
- [ ] Research-informed feature suggestions
- [ ] Analytics-driven recommendations (if site has analytics)
- [ ] Suggestion quality tracking (approval rate by action type)

### Phase 6: Polish
- [ ] Admin dashboard for PM activity + health metrics
- [ ] User-facing PM controls (frequency, autonomy preferences)
- [ ] Cost optimization (batch triggers, skip idle tenants)
- [ ] A/B testing PM vs no-PM tenant engagement
- [ ] `/nopm` command to disable

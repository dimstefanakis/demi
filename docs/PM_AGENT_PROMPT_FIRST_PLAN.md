# Prompt-First PM Agents and Context Management Implementation Plan (Detailed)

## 1. Product Decision and Invariants

### 1.1 Primary decision

- We keep prompt-first PM handoff as the primary mechanism.
- After each execution run, the execution agent is expected to call PM tools.
- System-level code may audit and recover missed handoffs, but must remain secondary.

### 1.2 Architectural invariants

- No templates.
- Fast first result.
- Edit-by-chat loop stays intact.
- Unsplash-only image sourcing policy remains unchanged.
- Tenant root remains authoritative root for cross-project context.
- Project roots remain authoritative for project-local context and memory.

## 2. Why This Change Is Needed

### 2.1 Current failure mode

- PM worker uses rule-based generic triage.
- Context files often stay placeholders because ownership is diffuse.
- PM suggestions are either low value or skipped due to lack of grounded context.

### 2.2 Desired state

- Two persistent PM roles:
  - `project_manager` for per-project context maintenance.
  - `lead_project_manager` for tenant-root cross-project planning.
- Prompted PM behavior grounded in local files and code state.
- Strong separation of execution lanes to avoid run collision.

## 3. Scope and Non-Goals

### 3.1 In scope

- DB role model for execution agents and runs.
- PM-specific prompt files and role-aware model selection.
- PM tooling for execution agent handoff.
- PM worker simplification to role-based dispatch.
- Interaction prompt updates for context hygiene.

### 3.2 Out of scope

- Major redesign of interaction routing decision format.
- New product features unrelated to PM/context.
- Billing model changes.

## 4. Role Model and Execution Lanes

### 4.1 Roles

- `execution`: normal build/edit/deploy run lane.
- `project_manager`: per-project PM lane.
- `lead_project_manager`: tenant-root PM lane.

### 4.2 Required isolation model

- Runs must be role-scoped.
- Inflight checks must be role-aware.
- PM event jobs must not queue into an active `execution` run.
- User execution work must not be queued into PM runs.

This is mandatory to avoid PM jobs hijacking active user work.

## 5. Data Model Changes

## 5.1 Migration A: execution_agents role

File: `supabase/migrations/YYYYMMDDHHMM00_pm_roles.sql`

1. Add `role TEXT NOT NULL DEFAULT 'execution'`.
2. Backfill existing rows automatically via default.
3. Replace unique index:
   - old: `(tenant_id, context)`
   - new: `(tenant_id, context, role)`
4. Replace status index:
   - old: `(tenant_id, status, updated_at DESC)`
   - new: `(tenant_id, role, status, updated_at DESC)`

### 5.2 Migration B: runs run_role

1. Add `run_role TEXT NOT NULL DEFAULT 'execution'` to `runs`.
2. Add inflight index for lane isolation:
   - `(tenant_id, status, run_role, started_at DESC)`
3. Optionally add `(tenant_id, run_role, execution_context, status, started_at DESC)` if same-role context lanes are needed later.

### 5.3 Compatibility requirements

- Code must tolerate old schema caches during rollout.
- Follow same compatibility style used for `execution_agent_id` and `execution_context`.
- Fallback behavior:
  - if `role` or `run_role` columns are unavailable, degrade to `execution`.

## 6. DB Layer Refactor

File: `src/demi/db/supabase_db.py`

### 6.1 Execution agent methods

Update signatures:

1. `list_execution_agents(tenant_id, include_inactive=False, limit=50, role: str | None = None)`
2. `get_execution_agent_by_context(tenant_id, context, role: str = 'execution')`
3. `create_execution_agent(..., role: str = 'execution')`
4. `ensure_execution_agent(..., role: str = 'execution')`

Implementation details:

- Normalize `role` to a strict set (`execution`, `project_manager`, `lead_project_manager`).
- Include `role` in selects/inserts/uniqueness lookups.
- Include `role` in legacy fallback row payload.

### 6.2 Run methods

1. `create_run(..., run_role: str = 'execution')`
2. `update_run_context(..., run_role: str | None = None)` if needed for late binding.
3. Role-aware inflight readers:
   - `get_inflight_run(tenant_id, project_name=None, run_role: str | None = None)`
   - `list_tenant_inflight_runs(tenant_id, limit=20, run_role: str | None = None)`

Compatibility:

- If `run_role` column missing, fallback query without role filter.

## 7. Orchestrator Role Plumbing and Lane Isolation

File: `src/demi/orchestrator.py`

### 7.1 Role normalization

Add helper:

- `_normalize_run_role(value: Any | None) -> str`
- Default: `execution`

### 7.2 Agent resolution

Update:

- `_resolve_execution_agent_for_decision(..., decision=...)` to read role from decision/payload and resolve by `(tenant, context, role)`.
- `_resolve_execution_agent_id_for_context(..., execution_context, role)` similarly.

### 7.3 Run creation

Ensure all `create_run` calls pass `run_role`.

Paths:

- User-triggered execution path: `run_role='execution'`.
- Event/PM dispatch path: role from payload (`project_manager` or `lead_project_manager`).

### 7.4 Inflight behavior (critical)

Current tenant-wide inflight checks must become role-aware:

1. User message handling should only consider inflight `execution` lane.
2. PM event handling should only consider inflight PM lane for same role.
3. Do not cross-queue between role lanes.

### 7.5 Event job handling

`handle_event_job` must read:

- `payload.execution_context`
- `payload.execution_agent_id`
- `payload.role`

and create a run in the correct lane.

### 7.6 Context payload hygiene

`_write_interaction_context` currently lists execution agents for interaction decisions.

- Keep this view execution-only by default (`role='execution'`) to avoid PM rows polluting interaction route context.

## 8. Prompt-First PM Handoff Contract

File: `src/demi/agent/chat_tools.py`, `prompts/claude_agent.md`, `src/demi/agent/claude.py`

### 8.1 New tools

1. `find_project_manager`
   - Input: `context`
   - Behavior: `ensure_execution_agent(..., role='project_manager')`

2. `trigger_project_manager`
   - Input:
     - `context`
     - `summary`
   - Behavior:
     - enqueue `pm_trigger` event job with explicit PM dispatch metadata.

Suggested payload shape:

```json
{
  "intent": "pm_post_execution_update",
  "event_type": "pm_trigger",
  "role": "project_manager",
  "execution_context": "Project Name",
  "payload": {
    "trigger": "post_execution_update",
    "summary": "what changed, deploy state, decisions",
    "source_run_id": 1234
  }
}
```

### 8.2 Allowed tools

Add PM tools to execution allowed tools in `ClaudeAgent.DEFAULT_ALLOWED_TOOLS`.

### 8.3 Execution prompt update (keep prompt-first)

In `prompts/claude_agent.md`, add a hard requirement section:

1. After implementation/review/devops completion, call `find_project_manager`.
2. Then call `trigger_project_manager` with a concise grounded summary.
3. If PM tool call fails, write failure reason in `tasks/result_summary.md`.

This keeps PM dispatch primarily prompt-driven by the execution agent.

## 9. PM Prompts and Role-Specific Runtime Behavior

Files:

- `prompts/project_manager_agent.md`
- `prompts/lead_project_manager_agent.md`
- `src/demi/agent/claude.py`
- `src/demi/config.py`

### 9.1 Prompt files

Project PM prompt must:

- Maintain project `DESCRIPTION.md`, `CONTEXT.md`, and `memory.md`.
- Use local files and code inspection only.
- Store stable facts only.

Lead PM prompt must:

- Maintain tenant-root `DESCRIPTION.md` and `memory.md`.
- Read per-project context files.
- Write short plan to `tasks/pm_plans/latest.json`.
- Message user only when specific and grounded.

### 9.2 Runtime selection

In `ClaudeAgent.prepare_context`, select prompt/model/thinking budget by role:

- `execution` -> existing execution prompt/model/tokens.
- `project_manager` -> PM prompt + PM model + PM thinking budget.
- `lead_project_manager` -> lead PM prompt + lead model + lead thinking budget.

### 9.3 Workspace mapping by role

Mandatory:

- `execution`: tenant root workspace (current behavior).
- `project_manager`: project root workspace for the specific context.
- `lead_project_manager`: tenant root workspace.

Without this, PM memory writes will land in the wrong `memory.md`.

## 10. PM Worker Simplification with Role Dispatch

File: `src/demi/jobs/pm_worker.py`

### 10.1 Keep

- Health snapshot + auto-fixes:
  - stale received messages
  - zombie runs
  - failed outbox
  - stale run inputs

### 10.2 Remove

- rule-based triage/actions for suggestion generation
- `pm_state.json` read/write lifecycle
- generic suggestion cooldown state handling

### 10.3 New dispatch behavior

1. Parse trigger type.
2. If health trigger:
   - run code-level health fixes.
3. Else:
   - derive target role/context from payload.
   - dispatch PM run through orchestrator with explicit role metadata.

Role defaults:

- scheduler heartbeat/idle/deploy/run triggers -> `lead_project_manager`
- prompt-driven post-execution updates -> `project_manager`

## 11. Interaction Prompt Context Hygiene

File: `prompts/interaction_agent.md`

Add explicit write-back policy:

1. Persist durable facts to memory tool immediately.
2. Update tenant-root `DESCRIPTION.md` when scope/milestones change.
3. If routed project description is placeholder and enough facts exist, write a real description.

Guardrails:

- never write secrets to memory files.
- only write durable facts.

## 12. Prompt-First Reliability Guardrails (Secondary, Non-Authoritative)

Prompt-first remains primary. Add lightweight safety:

1. Observe PM tool-call compliance from tool logs.
2. If missing on completed execution run, emit a `tenant_event` marker:
   - `pm_handoff_missing`
3. Optional scheduled catch-up can dispatch a PM run later.

Rules:

- no immediate orchestrator-forced PM run on every completion.
- no replacement of prompt-first execution behavior.

## 13. Runtime Interface Alignment (Local and Docker)

Files:

- `src/demi/agent/claude.py`
- `src/demi/runtime/docker_agent.py`
- `src/demi/runtime/agent_entrypoint.py`

Requirements:

1. Keep method signatures aligned for `prepare_context` across runtime implementations.
2. Pass both `execution_context` and `run_role` through local and docker paths.
3. Ensure role-aware prompt/model selection works in docker runtime too.

## 14. Detailed File-by-File Change List

### 14.1 New files

1. `prompts/project_manager_agent.md`
2. `prompts/lead_project_manager_agent.md`
3. `supabase/migrations/YYYYMMDDHHMM00_pm_roles.sql`
4. `supabase/migrations/YYYYMMDDHHMM10_run_role.sql`

### 14.2 Updated files

1. `src/demi/db/supabase_db.py`
2. `src/demi/orchestrator.py`
3. `src/demi/agent/chat_tools.py`
4. `src/demi/agent/claude.py`
5. `src/demi/jobs/pm_worker.py`
6. `src/demi/config.py`
7. `src/demi/runtime/docker_agent.py`
8. `src/demi/runtime/agent_entrypoint.py`
9. `prompts/claude_agent.md`
10. `prompts/interaction_agent.md`
11. `docs/SPEC.md`

## 15. Testing Plan

## 15.1 Database tests

1. role uniqueness `(tenant, context, role)` works.
2. existing `execution` rows remain readable.
3. run lane queries respect `run_role`.

## 15.2 Orchestrator tests

1. PM triggers do not queue into active execution lane.
2. user execution messages do not queue into PM lanes.
3. role propagation persists from event payload to run row.
4. session updates target matching `(context, role)` agent row.

## 15.3 Chat tools tests

1. `find_project_manager` creates/fetches role-specific PM agent row.
2. `trigger_project_manager` writes correct job payload.
3. execution context listing remains execution-only by default.

## 15.4 PM worker tests

1. health checks still perform code-level fixes.
2. non-health triggers dispatch PM runs by role.
3. no rule-based generic suggestion path remains.

## 15.5 Agent runtime tests

1. role-specific prompt/model selection in local runtime.
2. role-specific selection preserved in docker runtime.
3. PM memory writes land in project `memory.md` for project PM.
4. lead PM writes land in tenant-root `memory.md`.

## 16. Rollout Strategy

### 16.1 Deploy order

1. Schema migrations first.
2. DB compatibility code.
3. Orchestrator role plumbing and lane isolation.
4. PM tools + prompt updates.
5. PM worker simplification and PM role dispatch.
6. Role-specific prompt/model routing.

### 16.2 Feature gating

- Keep PM worker enablement flag (`PM_WORKER_ENABLED`) for controlled rollout.
- If needed, add temporary flag for role dispatch path.

### 16.3 Monitoring during rollout

1. PM handoff compliance rate from completed execution runs.
2. count of `pm_handoff_missing` events.
3. collision anomalies:
   - event jobs failing with busy due to wrong lane
   - unexpected queueing into foreign role lane
4. context freshness:
   - placeholder rate for `DESCRIPTION.md` and `CONTEXT.md`

## 17. Rollback Plan

If role dispatch causes instability:

1. Disable PM worker via config.
2. Keep schema intact; default behavior continues with `execution`.
3. Revert orchestrator role selection to `execution` while retaining compatibility code.
4. Keep prompt files present; they are inert without role dispatch.

## 18. Definition of Done

All conditions must hold:

1. Execution prompt reliably triggers PM via tools (primary path).
2. PM runs are isolated from user execution lane.
3. Project PM updates project-local context files and memory.
4. Lead PM updates tenant-level context and emits grounded plans.
5. Health auto-fixes still function.
6. No generic PM suggestion spam.
7. `docs/SPEC.md` updated to reflect new PM architecture and run-role model.

## 19. Implementation Log (2026-02-16)

### Completed

1. Added role lanes at DB level:
   - `execution_agents.role` (default `execution`)
   - `runs.run_role` (default `execution`)
   - role-aware indexes for `execution_agents` and `runs`.
2. Updated DB access layer for role-aware execution-agent and run queries, including compatibility fallbacks for missing columns.
3. Added PM MCP tools for execution runs:
   - `find_project_manager`
   - `trigger_project_manager`
4. Added role-lane plumbing in orchestrator for:
   - role-aware agent resolution
   - role-aware inflight/stale checks
   - role-aware run creation and event payload propagation.
5. Added PM workspace routing in orchestrator:
   - `project_manager` -> project workspace
   - `lead_project_manager` -> tenant root workspace.
6. Added PM handoff section to `prompts/claude_agent.md`.
7. Added tenant context maintenance rules to `prompts/interaction_agent.md`.
8. Added new prompt files:
   - `prompts/project_manager_agent.md`
   - `prompts/lead_project_manager_agent.md`
9. Added role-specific prompt/model/thinking selection in `ClaudeAgent.prepare_context`.
10. Added Docker runtime env passthrough for PM model and thinking-token settings.
11. Replaced PM worker triage state machine with thin dispatch + health self-healing.
12. Updated `docs/SPEC.md` to reflect role-lane architecture and PM worker behavior.
13. Updated PM worker tests to match thin-dispatch architecture.

### Validation Snapshot

- Passing targeted suites:
  - `tests/test_chat_tools.py`
  - `tests/test_orchestrator_flow.py`
  - `tests/test_pm_worker.py`
  - `tests/test_claude_stop_reasons.py`
  - `tests/test_claude_interaction_env.py`

### Notes

- Backward-compatibility fallback for schema-cache/missing-column behavior was tightened so missing
  `run_role` no longer drops `execution_context`/`execution_agent_id` writes.

### Follow-up Fixes (Review Pass)

1. PM artifact preservation:
   - `_clear_run_artifacts` now supports `preserve` set.
   - PM-role runs preserve `tasks/result_summary.md` and `tasks/deploy_url.txt` so project PM prompts can read prior run outputs.
2. Duplicate PM trigger suppression:
   - PM worker now skips generic scheduler `run_completed` trigger dispatch (no explicit role + execution source role), relying on prompt-first project PM handoff.
3. PM config cleanup:
   - Removed unused fields from `PMWorkerConfig` and removed obsolete wiring in app/worker entrypoint.
4. Chat tools readability:
   - Added role/run-role compatibility shim helpers for DB calls to remove repeated nested `try/except TypeError` blocks.
5. Legacy table cleanup:
   - Added migration `supabase/migrations/20260216123000_drop_legacy_pm_tables.sql` dropping `pm_heartbeats` and `pm_actions`.

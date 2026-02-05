# PRD: Remove run_request.json & SQLite; Supabase-Only Runtime State

## Steps & Progress
- [x] Audit all `run_request.json` readers/writers and define DB replacements
- [x] Define Supabase schema changes for run context + interaction state
- [x] Update runtime entrypoints to fetch run context from Supabase
- [x] Replace sqlite core DB with Supabase DB in app + workers
- [x] Migrate tenant state (final_sent, block_notified, quotes) to Supabase
- [x] Update tests to use local Supabase + pgTAP, drop sqlite fixtures
- [x] Rollout and cleanup (remove run_request.json, remove sqlite code)

Progress: 100% (tests passing)

## Summary
We will remove `tasks/run_request.json` everywhere and replace all usage with Supabase-backed run context. SQLite-backed orchestration (`src/demi/db/core.py`, tenant DB usage) is removed; the system will run solely on Supabase for stateful orchestration (local in dev/test, hosted in prod). A tenant-local `tenant.sqlite` scratchpad remains for execution-agent notes/cache. This addresses recurrent correctness bugs caused by file overwrites while runs are active and standardizes state in one authoritative DB.

## Goals
- Eliminate `run_request.json` usage from orchestration, runtime, and tools.
- Store run/request context in Supabase and retrieve it reliably by `run_id`.
- Remove sqlite from orchestration code and tests (keep optional tenant scratchpad file).
- Ensure interaction updates and "final_sent" semantics are keyed by `run_id`.

## Non-Goals
- Changing product flows or agent prompts beyond necessary context access.
- Reworking the entire message/interaction system beyond run-context storage.

## Current Pain
- `run_request.json` gets overwritten when messages queue, corrupting run attribution.
- "final sent" tracking uses provider message ids and the file, causing skipped replies.
- SQLite divergence between local and hosted behavior.

## Proposed Design
### 1) Supabase as Source of Truth for Run Context
- Store run request context in Supabase, keyed by `run_id`.
- Use existing `runs.message_id` to join to `messages` for request metadata.
- Add `runs.task_path` (relative to `tasks_dir`) so runtime loads the correct task.
- Add `runs.session_id` if needed to preserve agent session continuity.

### 2) Runtime Entry Points (No Files)
- `agent_entrypoint` and `docker_agent` take `run_id` instead of a request file.
- They query Supabase to assemble `NormalizedMessage` (from `messages` + `raw_json`).
- Task path resolved via `runs.task_path`.
- Workspace root derived from `tenant.key` + `project_name` using `WorkspaceManager`.

### 3) Interaction Tools Use DB Context
- Replace `_run_id_from_request` and `_current_run_message` with DB lookups:
  - Active run: `active_runs` table (already in DB).
  - Current message: `runs.message_id` or `messages` table by `message_id`.
- "final_sent" becomes `run_id`-keyed in Supabase (new table or column).

### 4) Remove SQLite
- Delete `src/demi/db/core.py` and migrate all imports to `SupabaseDatabase`.
- Replace `tenant_db` usage with Supabase-backed `tenant_state` (KV table).
- Tests use local Supabase via CLI. No sqlite usage.

## Schema Changes (Supabase)
- `runs.task_path TEXT` (relative to workspace root or tasks dir).
- `runs.session_id TEXT` (optional).
- `tenant_state` table (if needed for KV):
  - `tenant_id INT`, `namespace TEXT`, `key TEXT`, `value_json JSONB`, `updated_at TIMESTAMPTZ`.
- `run_flags` or `run_state` table for `final_sent` keyed by `run_id`.

## Testing (per Supabase docs)
- Two approaches:
  - App-level tests using the Supabase client in the app test framework.
  - SQL-level tests via Supabase CLI + pgTAP in `supabase/tests/database/*.sql`.
- CLI requirements: Supabase CLI v1.11.4+.
- SQL tests should follow the pgTAP pattern (`begin;`, `select plan(n);`, assertions, `select * from finish();`, `rollback;`).
- Run SQL tests with `supabase test db` (local Supabase required).
- App-level tests should use unique user IDs per test case (no reliance on DB resets) and avoid transactional isolation limits.

## Rollout
- Land schema changes first.
- Update runtime entrypoints to use DB.
- Update orchestrator + chat tools.
- Remove `run_request.json` writes and delete file usage.
- Remove sqlite codepaths and fix tests.

## Risks & Mitigations
- Runtime missing context: add guards + explicit errors if run context missing.
- Supabase latency: keep lookups minimal and cache where safe.
- Migration complexity: deploy in phases with feature flags if needed.

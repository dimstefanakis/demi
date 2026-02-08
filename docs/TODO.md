# TODO

## Completed

- [x] Make tenant CLI tooling deterministic and persistent under workspace storage.
  Install per-tenant tools under `/workspace/tooling`, pin versions/checksums in a lock file, and bootstrap from lock each run.
- [x] Add a scheduler + trigger mesh in the worker control plane.
  Support cron, webhook-conditional triggers, time windows, retry windows, and "run when X changes" triggers in one layer.
- [x] Add a tenant testing mode that bypasses all payment gates.
  In testing mode, the agent must never ask for payment and should treat the tenant as subscribed/authorized for paid flows.
- [x] Add explicit human-in-the-loop escalation for hard-blocking steps.
  If a step requires human-only input or approval, the agent must pause autonomous execution, request the required input/approval from the tenant, then resume when it is provided.

## Remaining

- [ ] Update billing flow for assistant retainer plans.
  Retainer includes one micro Supabase backend (when required) without extra payment prompt; when monthly usage cap is reached, issue a one-time $50 overtime payment link that raises limits for the current billing month only (no rollover), repeatable each time the cap is reached.
- [ ] Add observability + replay foundations for autonomous actions.
  Implement end-to-end traces, replayable run records, and policy audit trails; defer expensive regression evals for now.
- [ ] Harden compensation/rollback workflows with restart safety and low complexity.
  Ensure partial failures across interaction/execution loops recover safely after edge cases or server restarts, while keeping implementation minimal to avoid codebase bloat.

ALTER TABLE execution_agents
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'execution';

UPDATE execution_agents
SET role = 'execution'
WHERE role IS NULL;

DROP INDEX IF EXISTS execution_agents_tenant_context_idx;
CREATE UNIQUE INDEX IF NOT EXISTS execution_agents_tenant_context_role_idx
    ON execution_agents (tenant_id, context, role);

DROP INDEX IF EXISTS execution_agents_tenant_status_idx;
CREATE INDEX IF NOT EXISTS execution_agents_tenant_role_status_idx
    ON execution_agents (tenant_id, role, status, updated_at DESC);

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS run_role TEXT NOT NULL DEFAULT 'execution';

UPDATE runs
SET run_role = 'execution'
WHERE run_role IS NULL;

CREATE INDEX IF NOT EXISTS runs_tenant_status_role_started_idx
    ON runs (tenant_id, status, run_role, started_at DESC);

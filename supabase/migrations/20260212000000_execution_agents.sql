CREATE TABLE IF NOT EXISTS execution_agents (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    context TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    session_id TEXT,
    last_run_id BIGINT,
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS execution_agents_tenant_context_idx
    ON execution_agents (tenant_id, context);

CREATE INDEX IF NOT EXISTS execution_agents_tenant_status_idx
    ON execution_agents (tenant_id, status, updated_at DESC);

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS execution_agent_id BIGINT;

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS execution_context TEXT;

CREATE INDEX IF NOT EXISTS runs_execution_agent_idx
    ON runs (tenant_id, status, execution_agent_id, started_at DESC);

UPDATE runs
SET execution_context = 'Main project'
WHERE execution_context IS NULL;

INSERT INTO execution_agents (
    tenant_id,
    context,
    status,
    created_at,
    updated_at
)
SELECT t.id, 'Main project', 'active', NOW(), NOW()
FROM tenants t
ON CONFLICT (tenant_id, context) DO NOTHING;

UPDATE runs r
SET execution_agent_id = ea.id
FROM execution_agents ea
WHERE r.tenant_id = ea.tenant_id
  AND r.execution_agent_id IS NULL
  AND COALESCE(NULLIF(TRIM(r.execution_context), ''), 'Main project') = ea.context;

-- One-time hydration for existing users:
-- populate default execution-agent session from tenant_state execution keys.
WITH session_source AS (
    SELECT DISTINCT ON (ts.tenant_id)
        ts.tenant_id,
        NULLIF(TRIM(ts.value_json ->> 'session_id'), '') AS session_id
    FROM tenant_state ts
    WHERE ts.namespace = 'execution'
      AND ts.key IN ('claude_session', 'claude_session:main')
      AND NULLIF(TRIM(ts.value_json ->> 'session_id'), '') IS NOT NULL
    ORDER BY
      ts.tenant_id,
      CASE ts.key
        WHEN 'claude_session' THEN 0
        ELSE 1
      END,
      ts.updated_at DESC
)
UPDATE execution_agents ea
SET
    session_id = src.session_id,
    updated_at = NOW()
FROM session_source src
WHERE src.tenant_id = ea.tenant_id
  AND ea.context = 'Main project'
  AND COALESCE(NULLIF(TRIM(ea.session_id), ''), '') = '';

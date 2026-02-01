-- Run input queue + outbox + active runs (UUID PKs, partial migration).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS run_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id BIGINT NOT NULL,
    run_id BIGINT,
    project_name TEXT,
    source TEXT NOT NULL,
    provider_message_id TEXT,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL,
    claimed_at TIMESTAMPTZ,
    handled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS run_inputs_dedupe_idx
    ON run_inputs (tenant_id, provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS run_inputs_queue_idx
    ON run_inputs (tenant_id, project_name, status, created_at);

CREATE TABLE IF NOT EXISTS outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id BIGINT NOT NULL,
    run_id BIGINT,
    project_name TEXT,
    correlation_id TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS outbox_dedupe_idx
    ON outbox (tenant_id, correlation_id);

CREATE TABLE IF NOT EXISTS active_runs (
    tenant_id BIGINT NOT NULL,
    project_name TEXT NOT NULL,
    run_id BIGINT NOT NULL,
    lease_expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, project_name)
);

CREATE INDEX IF NOT EXISTS active_runs_run_idx
    ON active_runs (run_id);

CREATE OR REPLACE FUNCTION claim_run_inputs_for_project(
    p_tenant_id BIGINT,
    p_project_name TEXT,
    p_limit INT DEFAULT 10
) RETURNS SETOF run_inputs
LANGUAGE SQL
AS $$
    WITH cte AS (
        SELECT id
        FROM run_inputs
        WHERE tenant_id = p_tenant_id
          AND status = 'queued'
          AND (p_project_name IS NULL OR project_name = p_project_name)
        ORDER BY created_at ASC
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE run_inputs r
    SET status = 'claimed',
        claimed_at = NOW()
    FROM cte
    WHERE r.id = cte.id
    RETURNING r.*;
$$;

CREATE OR REPLACE FUNCTION claim_outbox(
    p_limit INT DEFAULT 25
) RETURNS SETOF outbox
LANGUAGE SQL
AS $$
    WITH cte AS (
        SELECT id
        FROM outbox
        WHERE status = 'queued'
        ORDER BY created_at ASC
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE outbox o
    SET status = 'sending'
    FROM cte
    WHERE o.id = cte.id
    RETURNING o.*;
$$;

CREATE OR REPLACE VIEW queued_run_input_groups AS
SELECT tenant_id, project_name, MIN(created_at) AS oldest_created_at
FROM run_inputs
WHERE status = 'queued'
GROUP BY tenant_id, project_name;

-- RLS (optional; enforced when using tenant-scoped JWT or GUCs).
ALTER TABLE run_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE active_runs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS run_inputs_tenant_isolation ON run_inputs;
DROP POLICY IF EXISTS outbox_tenant_isolation ON outbox;
DROP POLICY IF EXISTS active_runs_tenant_isolation ON active_runs;

CREATE POLICY run_inputs_tenant_isolation ON run_inputs
    USING (tenant_id = (current_setting('request.jwt.claims', true)::jsonb->>'tenant_id')::bigint);

CREATE POLICY outbox_tenant_isolation ON outbox
    USING (tenant_id = (current_setting('request.jwt.claims', true)::jsonb->>'tenant_id')::bigint);

CREATE POLICY active_runs_tenant_isolation ON active_runs
    USING (tenant_id = (current_setting('request.jwt.claims', true)::jsonb->>'tenant_id')::bigint);

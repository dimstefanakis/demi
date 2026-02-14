CREATE TABLE IF NOT EXISTS pm_heartbeats (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    trigger_type TEXT NOT NULL,
    trigger_event_id BIGINT REFERENCES event_jobs(id),
    triage_result JSONB,
    action_needed BOOLEAN NOT NULL,
    triage_cost_usd DOUBLE PRECISION,
    triage_model TEXT,
    action_result JSONB,
    action_cost_usd DOUBLE PRECISION,
    action_model TEXT,
    total_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pm_heartbeats_tenant
    ON pm_heartbeats (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pm_actions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    heartbeat_id BIGINT REFERENCES pm_heartbeats(id),
    action_type TEXT NOT NULL,
    autonomy_tier TEXT NOT NULL,
    description TEXT NOT NULL,
    execution_run_id BIGINT REFERENCES runs(id),
    outbox_id UUID REFERENCES outbox(id),
    user_response TEXT,
    user_response_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending',
    result_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pm_actions_tenant
    ON pm_actions (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pm_actions_status
    ON pm_actions (status)
    WHERE status IN ('pending', 'executing');

CREATE OR REPLACE VIEW admin_pm_costs AS
SELECT
    t.key AS tenant,
    COUNT(h.id) AS heartbeats,
    COALESCE(SUM(h.total_cost_usd), 0) AS total_cost,
    COALESCE(AVG(h.total_cost_usd), 0) AS avg_cost_per_heartbeat,
    SUM(CASE WHEN h.action_needed THEN 1 ELSE 0 END) AS actions_taken,
    MAX(h.created_at) AS last_heartbeat
FROM pm_heartbeats h
JOIN tenants t ON t.id = h.tenant_id
GROUP BY t.key;

CREATE OR REPLACE VIEW admin_pm_actions_summary AS
SELECT
    action_type,
    autonomy_tier,
    COUNT(*) AS total,
    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
    SUM(CASE WHEN user_response = 'approved' THEN 1 ELSE 0 END) AS approved,
    SUM(CASE WHEN user_response = 'rejected' THEN 1 ELSE 0 END) AS rejected
FROM pm_actions
GROUP BY action_type, autonomy_tier;

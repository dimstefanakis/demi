-- Main database schema for Supabase/Postgres.

CREATE TABLE IF NOT EXISTS tenants (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    workspace_path TEXT,
    session_id TEXT,
    vercel_project_id TEXT,
    last_deploy_url TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    text TEXT,
    raw_json JSONB,
    project_name TEXT,
    status TEXT,
    UNIQUE (tenant_id, provider_message_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    error TEXT,
    total_cost_usd DOUBLE PRECISION,
    usage_json JSONB,
    project_name TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    last_activity_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS billing_orders (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    price_usd DOUBLE PRECISION,
    currency TEXT,
    stripe_session_id TEXT,
    stripe_payment_url TEXT,
    stripe_subscription_id TEXT,
    metadata_json JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS supabase_projects (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL UNIQUE,
    project_ref TEXT,
    project_id TEXT,
    project_name TEXT,
    region TEXT,
    status TEXT,
    api_url TEXT,
    publishable_key TEXT,
    secret_key TEXT,
    anon_key TEXT,
    service_role_key TEXT,
    raw_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS event_jobs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    job_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    run_after TIMESTAMPTZ NOT NULL,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_tenant_status
    ON messages (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_tenant_project_status
    ON messages (tenant_id, project_name, status);
CREATE INDEX IF NOT EXISTS idx_messages_status_received_at
    ON messages (status, received_at);

CREATE INDEX IF NOT EXISTS idx_runs_inflight
    ON runs (tenant_id, project_name, status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_lease
    ON runs (tenant_id, project_name, status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_event_jobs_pending
    ON event_jobs (status, run_after, id);

CREATE INDEX IF NOT EXISTS idx_billing_orders_tenant
    ON billing_orders (tenant_id);
CREATE INDEX IF NOT EXISTS idx_billing_orders_stripe_session
    ON billing_orders (stripe_session_id);
CREATE INDEX IF NOT EXISTS idx_billing_orders_stripe_subscription
    ON billing_orders (stripe_subscription_id);

CREATE INDEX IF NOT EXISTS idx_supabase_projects_tenant
    ON supabase_projects (tenant_id);

CREATE OR REPLACE VIEW pending_message_groups AS
SELECT tenant_id, project_name, MIN(received_at) AS oldest_received_at
FROM messages
WHERE status = 'pending'
GROUP BY tenant_id, project_name;

CREATE OR REPLACE VIEW processing_message_groups AS
SELECT tenant_id, project_name, MIN(received_at) AS oldest_received_at
FROM messages
WHERE status = 'processing'
GROUP BY tenant_id, project_name;

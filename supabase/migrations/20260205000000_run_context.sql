ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS task_path TEXT;

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS session_id TEXT;

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS final_sent_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS tenant_state (
    tenant_id BIGINT NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, namespace, key)
);

CREATE INDEX IF NOT EXISTS tenant_state_tenant_idx
    ON tenant_state (tenant_id);

CREATE TABLE IF NOT EXISTS tenant_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tenant_events_tenant_idx
    ON tenant_events (tenant_id, received_at);

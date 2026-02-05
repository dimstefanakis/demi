CREATE TABLE IF NOT EXISTS message_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id BIGINT NOT NULL,
    direction TEXT NOT NULL,
    provider TEXT,
    tenant_external_id TEXT,
    message_type TEXT,
    text TEXT,
    project_name TEXT,
    run_id BIGINT,
    reply_to_message_id TEXT,
    provider_message_id TEXT,
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS message_events_tenant_idx
    ON message_events (tenant_id, created_at);

ALTER TABLE message_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS message_events_tenant_isolation ON message_events;

CREATE POLICY message_events_tenant_isolation ON message_events
    USING (tenant_id = (current_setting('request.jwt.claims', true)::jsonb->>'tenant_id')::bigint);

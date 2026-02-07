-- Audit raw inbound webhook updates and parse outcomes for diagnostics.

CREATE TABLE IF NOT EXISTS webhook_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    update_id BIGINT,
    tenant_external_id TEXT,
    provider_message_id TEXT,
    message_text TEXT,
    parse_status TEXT NOT NULL,
    parse_error TEXT,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS webhook_updates_provider_update_id_idx
    ON webhook_updates (provider, update_id)
    WHERE update_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS webhook_updates_created_at_idx
    ON webhook_updates (created_at DESC);

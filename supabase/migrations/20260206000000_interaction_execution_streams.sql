CREATE TABLE IF NOT EXISTS interaction_sessions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    project_name TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS interaction_sessions_tenant_idx
    ON interaction_sessions (tenant_id, status, started_at DESC);

CREATE TABLE IF NOT EXISTS interaction_session_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id BIGINT NOT NULL REFERENCES interaction_sessions(id) ON DELETE CASCADE,
    message_id BIGINT,
    provider_message_id TEXT,
    text TEXT,
    assets_json JSONB,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    streamed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS interaction_session_inputs_session_idx
    ON interaction_session_inputs (session_id, created_at);

CREATE TABLE IF NOT EXISTS execution_stream_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id BIGINT NOT NULL,
    run_id BIGINT NOT NULL,
    project_name TEXT,
    message_id BIGINT,
    provider_message_id TEXT,
    text TEXT,
    assets_json JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    streamed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS execution_stream_inputs_run_status_idx
    ON execution_stream_inputs (run_id, status, created_at);

CREATE INDEX IF NOT EXISTS execution_stream_inputs_tenant_idx
    ON execution_stream_inputs (tenant_id, created_at DESC);

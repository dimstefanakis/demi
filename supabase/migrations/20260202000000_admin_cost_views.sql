-- Admin support/finance views + cost tracking helpers.

CREATE INDEX IF NOT EXISTS idx_runs_tenant_started_at
    ON runs (tenant_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_tenant_received_at
    ON messages (tenant_id, received_at DESC);

CREATE OR REPLACE VIEW admin_run_costs AS
WITH interaction_usage AS (
    SELECT
        r.id AS run_id,
        COUNT(*) AS interaction_count,
        COALESCE(SUM((elem->>'total_cost_usd')::double precision), 0) AS interaction_cost_usd,
        COALESCE(SUM((elem->>'input_tokens')::bigint), 0) AS interaction_input_tokens,
        COALESCE(SUM((elem->>'output_tokens')::bigint), 0) AS interaction_output_tokens
    FROM runs r
    LEFT JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN r.usage_json ? 'interaction'
                 AND jsonb_typeof(r.usage_json->'interaction') = 'array'
            THEN r.usage_json->'interaction'
            ELSE '[]'::jsonb
        END
    ) AS elem ON TRUE
    GROUP BY r.id
)
SELECT
    r.id AS run_id,
    r.tenant_id,
    t.provider,
    t.external_id,
    t.key AS tenant_key,
    r.project_name,
    r.status,
    r.started_at,
    r.finished_at,
    r.total_cost_usd,
    m.id AS message_id,
    m.provider_message_id,
    m.received_at AS message_received_at,
    m.text AS message_text,
    CASE
        WHEN r.usage_json ? 'primary'
        THEN (r.usage_json->'primary'->>'input_tokens')::bigint
        ELSE (r.usage_json->>'input_tokens')::bigint
    END AS primary_input_tokens,
    CASE
        WHEN r.usage_json ? 'primary'
        THEN (r.usage_json->'primary'->>'output_tokens')::bigint
        ELSE (r.usage_json->>'output_tokens')::bigint
    END AS primary_output_tokens,
    CASE
        WHEN r.usage_json ? 'primary'
        THEN (r.usage_json->'primary'->>'cache_read_input_tokens')::bigint
        ELSE (r.usage_json->>'cache_read_input_tokens')::bigint
    END AS primary_cache_read_input_tokens,
    CASE
        WHEN r.usage_json ? 'primary'
        THEN (r.usage_json->'primary'->>'cache_creation_input_tokens')::bigint
        ELSE (r.usage_json->>'cache_creation_input_tokens')::bigint
    END AS primary_cache_creation_input_tokens,
    iu.interaction_count,
    iu.interaction_cost_usd,
    iu.interaction_input_tokens,
    iu.interaction_output_tokens
FROM runs r
JOIN tenants t ON t.id = r.tenant_id
LEFT JOIN messages m ON m.id = r.message_id
LEFT JOIN interaction_usage iu ON iu.run_id = r.id;

CREATE OR REPLACE VIEW admin_tenant_overview AS
SELECT
    t.id AS tenant_id,
    t.provider,
    t.external_id,
    t.key AS tenant_key,
    t.created_at,
    t.updated_at,
    last_msg.received_at AS last_message_at,
    last_msg.text AS last_message_text,
    run_stats.run_count,
    run_stats.total_cost_usd,
    run_stats.last_run_at
FROM tenants t
LEFT JOIN LATERAL (
    SELECT m.received_at, m.text
    FROM messages m
    WHERE m.tenant_id = t.id
    ORDER BY m.received_at DESC
    LIMIT 1
) AS last_msg ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) AS run_count,
        COALESCE(SUM(r.total_cost_usd), 0) AS total_cost_usd,
        MAX(r.started_at) AS last_run_at
    FROM runs r
    WHERE r.tenant_id = t.id
) AS run_stats ON TRUE;

CREATE OR REPLACE VIEW admin_tenant_costs_daily AS
SELECT
    tenant_id,
    provider,
    external_id,
    date_trunc('day', started_at) AS day,
    COUNT(*) AS run_count,
    COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd,
    COALESCE(SUM(interaction_cost_usd), 0) AS interaction_cost_usd,
    COALESCE(SUM(primary_input_tokens), 0) AS primary_input_tokens,
    COALESCE(SUM(primary_output_tokens), 0) AS primary_output_tokens,
    COALESCE(SUM(interaction_input_tokens), 0) AS interaction_input_tokens,
    COALESCE(SUM(interaction_output_tokens), 0) AS interaction_output_tokens
FROM admin_run_costs
GROUP BY tenant_id, provider, external_id, date_trunc('day', started_at);

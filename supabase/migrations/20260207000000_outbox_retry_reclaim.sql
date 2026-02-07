-- Add retry metadata for outbox delivery and reclaim stale "sending" rows.

ALTER TABLE outbox
    ADD COLUMN IF NOT EXISTS attempt_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Backfill existing rows to be immediately claimable if they were never sent.
UPDATE outbox
SET next_retry_at = COALESCE(next_retry_at, created_at),
    updated_at = COALESCE(updated_at, created_at)
WHERE status IN ('queued', 'sending');

CREATE INDEX IF NOT EXISTS outbox_claim_idx
    ON outbox (status, next_retry_at, created_at);

CREATE OR REPLACE FUNCTION claim_outbox(
    p_limit INT DEFAULT 25,
    p_stale_sending_seconds INT DEFAULT 120
) RETURNS SETOF outbox
LANGUAGE SQL
AS $$
    WITH cte AS (
        SELECT id
        FROM outbox
        WHERE
            (
                status = 'queued'
                AND COALESCE(next_retry_at, created_at) <= NOW()
            )
            OR (
                status = 'sending'
                AND updated_at <= NOW() - make_interval(secs => GREATEST(1, p_stale_sending_seconds))
            )
        ORDER BY created_at ASC
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE outbox o
    SET status = 'sending',
        updated_at = NOW()
    FROM cte
    WHERE o.id = cte.id
    RETURNING o.*;
$$;

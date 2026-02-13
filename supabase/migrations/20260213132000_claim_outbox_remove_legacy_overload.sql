-- Remove legacy single-argument claim_outbox overload to avoid PostgREST RPC ambiguity.
-- Keep the stale-reclaim signature as the canonical entrypoint.

DROP FUNCTION IF EXISTS claim_outbox(INT);

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

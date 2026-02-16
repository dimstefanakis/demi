-- PM worker no longer records heartbeat/action rows in tables.
-- Keep PM trigger/event plumbing in `event_jobs` + tenant state and drop legacy tables.

DROP TABLE IF EXISTS pm_actions;
DROP TABLE IF EXISTS pm_heartbeats;

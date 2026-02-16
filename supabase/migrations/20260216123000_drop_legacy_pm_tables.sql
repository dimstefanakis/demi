-- PM worker no longer records heartbeat/action rows in tables.
-- Keep PM trigger/event plumbing in `event_jobs` + tenant state and drop legacy tables.

DROP VIEW IF EXISTS admin_pm_actions_summary;
DROP VIEW IF EXISTS admin_pm_costs;

-- Use CASCADE to avoid failures from lingering dependencies in older environments.
DROP TABLE IF EXISTS pm_actions CASCADE;
DROP TABLE IF EXISTS pm_heartbeats CASCADE;

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS interaction_decision_json JSONB;

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS result_summary TEXT;

ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS tool_summary_json JSONB;

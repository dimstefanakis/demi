BEGIN;
SELECT plan(6);

SELECT has_column('public', 'runs', 'task_path');
SELECT has_column('public', 'runs', 'session_id');
SELECT has_column('public', 'runs', 'final_sent_at');
SELECT has_table('public', 'tenant_state');
SELECT has_table('public', 'tenant_events');
SELECT has_column('public', 'tenant_state', 'value_json');

SELECT * FROM finish();
ROLLBACK;

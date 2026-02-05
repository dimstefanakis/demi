from __future__ import annotations

from tests.utils import build_test_db, create_test_tenant


def test_list_recent_runs_filters_project():
    db = build_test_db()
    tenant = create_test_tenant(db)

    run_main = db.create_run(tenant.id, project_name="main")
    run_other = db.create_run(tenant.id, project_name="other")

    recent_main = db.list_recent_runs(tenant.id, project_name="main", limit=5)
    recent_all = db.list_recent_runs(tenant.id, project_name=None, limit=5)

    assert len(recent_main) == 1
    assert int(recent_main[0]["id"]) == run_main
    assert len(recent_all) >= 2
    assert {int(row["id"]) for row in recent_all} >= {run_main, run_other}

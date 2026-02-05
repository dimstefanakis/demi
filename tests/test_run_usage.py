from tests.utils import build_test_db, create_test_tenant


def test_run_usage_persisted():
    db = build_test_db()
    tenant = create_test_tenant(db)
    run_id = db.create_run(tenant.id)

    usage = {"input_tokens": 10, "output_tokens": 5, "model_usage": [{"model": "test"}]}
    db.update_run_usage(run_id, total_cost_usd=0.12, usage=usage)

    row = db.get_run(run_id)
    assert row is not None
    assert float(row["total_cost_usd"]) == 0.12
    assert row["usage_json"] == usage


def test_run_usage_merges_interaction_and_primary():
    db = build_test_db()
    tenant = create_test_tenant(db)
    run_id = db.create_run(tenant.id)

    interaction_usage = {"input_tokens": 5}
    primary_usage = {"output_tokens": 7}

    db.add_run_usage(
        run_id,
        total_cost_usd=0.05,
        usage=interaction_usage,
        usage_key="interaction",
    )
    db.add_run_usage(
        run_id,
        total_cost_usd=0.15,
        usage=primary_usage,
        usage_key="primary",
    )

    row = db.get_run(run_id)
    usage_json = row["usage_json"]

    assert float(row["total_cost_usd"]) == 0.2
    assert usage_json["interaction"] == [interaction_usage]
    assert usage_json["primary"] == [primary_usage]

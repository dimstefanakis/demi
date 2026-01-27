import json

from claudius.db.core import Database


def test_run_usage_persisted(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()

    tenant = db.get_or_create_tenant(provider="telegram", external_id="123")
    run_id = db.create_run(tenant.id)

    usage = {"input_tokens": 10, "output_tokens": 5, "model_usage": [{"model": "test"}]}
    db.update_run_usage(run_id, total_cost_usd=0.12, usage=usage)

    row = db.connect().execute(
        "SELECT total_cost_usd, usage_json FROM runs WHERE id = ?", (run_id,)
    ).fetchone()

    assert row["total_cost_usd"] == 0.12
    assert json.loads(row["usage_json"]) == usage

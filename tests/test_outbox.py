from demi.db.core import Database


def test_outbox_deduplicates_by_correlation(tmp_path):
    db = Database(tmp_path / "main.sqlite")
    db.init()
    tenant = db.get_or_create_tenant("telegram", "555")

    first_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name=None,
        correlation_id="c-1",
        payload={"tenant_external_id": "555", "text": "Hi"},
    )
    second_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name=None,
        correlation_id="c-1",
        payload={"tenant_external_id": "555", "text": "Hi again"},
    )

    assert first_id == second_id
    rows = db.claim_outbox(limit=10)
    assert len(rows) == 1

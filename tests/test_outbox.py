from tests.utils import build_test_db, create_test_tenant


def test_outbox_deduplicates_by_correlation():
    db = build_test_db()
    tenant = create_test_tenant(db)

    first_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name=None,
        correlation_id="c-1",
        payload={"tenant_external_id": tenant.external_id, "text": "Hi"},
    )
    second_id = db.enqueue_outbox(
        tenant_id=tenant.id,
        run_id=None,
        project_name=None,
        correlation_id="c-1",
        payload={"tenant_external_id": tenant.external_id, "text": "Hi again"},
    )

    assert first_id == second_id
    rows = db.list_outbox(tenant_id=tenant.id)
    assert len(rows) == 1

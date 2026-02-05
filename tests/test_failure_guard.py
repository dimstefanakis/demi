from tests.utils import build_test_db, create_test_tenant
from demi.failure_guard import get_block, record_hard_failure


def test_failure_guard_blocks_after_two():
    db = build_test_db()
    tenant = create_test_tenant(db)

    first = record_hard_failure(
        db,
        tenant.id,
        "managed_backend",
        reason="missing_org",
        max_failures=2,
    )
    assert first["blocked"] is False

    second = record_hard_failure(
        db,
        tenant.id,
        "managed_backend",
        reason="missing_org",
        max_failures=2,
    )
    assert second["blocked"] is True
    block = get_block(db, tenant.id, "managed_backend")
    assert block is not None

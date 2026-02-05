from datetime import datetime, timezone

from demi.models import NormalizedMessage
from tests.utils import build_test_db, create_test_tenant, unique_external_id


def test_record_message_idempotency():
    db = build_test_db()
    tenant = create_test_tenant(db, external_id=unique_external_id("tenant"))

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id=unique_external_id("msg"),
        tenant_external_id=tenant.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
    )

    first_id, inserted_first = db.record_message(tenant.id, msg)
    second_id, inserted_second = db.record_message(tenant.id, msg)

    assert inserted_first is True
    assert inserted_second is False
    assert first_id == second_id


def test_same_message_id_across_tenants():
    db = build_test_db()
    shared_message_id = unique_external_id("msg")

    tenant_a = create_test_tenant(db, external_id=unique_external_id("tenant"))
    tenant_b = create_test_tenant(db, external_id=unique_external_id("tenant"))

    msg_a = NormalizedMessage(
        provider="telegram",
        provider_message_id=shared_message_id,
        tenant_external_id=tenant_a.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
    )
    msg_b = NormalizedMessage(
        provider="telegram",
        provider_message_id=shared_message_id,
        tenant_external_id=tenant_b.external_id,
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
    )

    _, inserted_a = db.record_message(tenant_a.id, msg_a)
    _, inserted_b = db.record_message(tenant_b.id, msg_b)
    assert inserted_a is True
    assert inserted_b is True

from claudius.db.core import Database
from claudius.models import NormalizedMessage
from datetime import datetime, timezone


def test_record_message_idempotency(tmp_path):
    db_path = tmp_path / "claudius.sqlite"
    db = Database(db_path)
    db.init()

    tenant = db.get_or_create_tenant(provider="telegram", external_id="987654")

    msg = NormalizedMessage(
        provider="telegram",
        provider_message_id="51",
        tenant_external_id="987654",
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


def test_same_message_id_across_tenants(tmp_path):
    db_path = tmp_path / "claudius.sqlite"
    db = Database(db_path)
    db.init()

    tenant_a = db.get_or_create_tenant(provider="telegram", external_id="111")
    tenant_b = db.get_or_create_tenant(provider="telegram", external_id="222")

    msg_a = NormalizedMessage(
        provider="telegram",
        provider_message_id="99",
        tenant_external_id="111",
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
    )
    msg_b = NormalizedMessage(
        provider="telegram",
        provider_message_id="99",
        tenant_external_id="222",
        received_at=datetime.now(tz=timezone.utc),
        text="Hello",
        images=[],
        raw={},
    )

    _, inserted_a = db.record_message(tenant_a.id, msg_a)
    _, inserted_b = db.record_message(tenant_b.id, msg_b)
    assert inserted_a is True
    assert inserted_b is True

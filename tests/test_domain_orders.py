from tests.utils import build_test_db, create_test_tenant


def test_billing_order_lifecycle():
    db = build_test_db()
    tenant = create_test_tenant(db)

    order_id = db.create_billing_order(
        tenant_id=tenant.id,
        order_type="domain",
        status="quoted",
        price_usd=12.34,
        currency="USD",
        metadata={"domain": "example.com"},
    )

    db.update_billing_order_payment(order_id, stripe_session_id="sess_123", stripe_payment_url="url")
    db.mark_billing_order_paid(order_id, stripe_session_id="sess_123", stripe_subscription_id="sub_123")
    db.update_billing_order_status(order_id, "purchased", metadata={"vercel_status": "ok"})

    order = db.get_billing_order(order_id)
    assert order is not None
    assert order["order_type"] == "domain"
    assert order["status"] == "purchased"
    metadata = order["metadata_json"]
    assert metadata["domain"] == "example.com"
    assert metadata["vercel_status"] == "ok"

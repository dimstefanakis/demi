from claudius.db.core import Database


def test_domain_order_lifecycle(tmp_path):
    db = Database(tmp_path / "claudius.sqlite")
    db.init()
    tenant = db.get_or_create_tenant(provider="telegram", external_id="321")

    order_id = db.create_domain_order(
        tenant_id=tenant.id,
        domain="example.com",
        status="quoted",
        price_usd=12.34,
        currency="USD",
    )

    db.update_domain_order_payment(order_id, stripe_session_id="sess_123", stripe_payment_url="url")
    db.mark_domain_order_paid(order_id, stripe_session_id="sess_123")
    db.mark_domain_order_purchased(order_id, vercel_response={"status": "ok"})

    order = db.get_domain_order(order_id)
    assert order is not None
    assert order.domain == "example.com"
    assert order.status == "purchased"

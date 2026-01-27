from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claudius.config import Settings
from claudius.db.core import Database
from claudius.domains.vercel import DomainPurchaseResult, buy_domain


@dataclass
class DomainPurchaseOutcome:
    order_id: int
    success: bool
    message: str
    result: DomainPurchaseResult | None = None


class DomainService:
    def __init__(self, db: Database, settings: Settings, messenger: Any):
        self.db = db
        self.settings = settings
        self.messenger = messenger

    async def purchase_paid_order(self, order_id: int) -> DomainPurchaseOutcome:
        order = self.db.get_domain_order(order_id)
        if not order:
            return DomainPurchaseOutcome(order_id, False, "Domain order not found.")
        tenant = self.db.get_tenant_by_id(order.tenant_id)
        if not tenant:
            self.db.mark_domain_order_failed(order_id, "tenant_not_found")
            return DomainPurchaseOutcome(order_id, False, "Tenant not found.")

        result = buy_domain(order.domain, self.settings)
        if result.success:
            self.db.mark_domain_order_purchased(order_id, {"output": result.raw_output})
            await self.messenger.send_text(
                tenant.external_id,
                f"Domain purchased: {order.domain}. We'll connect it shortly.",
            )
            return DomainPurchaseOutcome(order_id, True, "Domain purchased.", result=result)

        self.db.mark_domain_order_failed(order_id, result.error or "purchase_failed")
        await self.messenger.send_text(
            tenant.external_id,
            f"Payment received, but the domain purchase failed for {order.domain}.",
        )
        return DomainPurchaseOutcome(order_id, False, "Domain purchase failed.", result=result)

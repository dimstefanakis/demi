from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from claudius.config import Settings


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str
    success_url: str
    cancel_url: str


@dataclass(frozen=True)
class StripeSession:
    session_id: str
    url: str


class StripeClient:
    def __init__(self, config: StripeConfig):
        self.config = config

    async def create_checkout_session(
        self,
        *,
        amount_cents: int,
        currency: str,
        product_name: str,
        metadata: dict[str, str] | None = None,
    ) -> StripeSession:
        data = {
            "mode": "payment",
            "success_url": self.config.success_url,
            "cancel_url": self.config.cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": product_name,
        }
        if metadata:
            for key, value in metadata.items():
                data[f"metadata[{key}]"] = str(value)

        headers = {
            "Authorization": f"Bearer {self.config.secret_key}",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=data,
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Stripe error: {response.status_code} {response.text}"
            )
        payload = response.json()
        return StripeSession(session_id=payload["id"], url=payload["url"])

    async def create_recurring_checkout_session(
        self,
        *,
        amount_cents: int,
        currency: str,
        product_name: str,
        interval: str,
        metadata: dict[str, str] | None = None,
    ) -> StripeSession:
        data = {
            "mode": "subscription",
            "success_url": self.config.success_url,
            "cancel_url": self.config.cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": product_name,
            "line_items[0][price_data][recurring][interval]": interval,
        }
        if metadata:
            for key, value in metadata.items():
                data[f"metadata[{key}]"] = str(value)

        headers = {
            "Authorization": f"Bearer {self.config.secret_key}",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=data,
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(f"Stripe error: {response.status_code} {response.text}")
        payload = response.json()
        return StripeSession(session_id=payload["id"], url=payload["url"])

    async def create_subscription_checkout_session_for_price(
        self,
        *,
        price_id: str,
        metadata: dict[str, str] | None = None,
    ) -> StripeSession:
        data = {
            "mode": "subscription",
            "success_url": self.config.success_url,
            "cancel_url": self.config.cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price]": price_id,
        }
        if metadata:
            for key, value in metadata.items():
                data[f"metadata[{key}]"] = str(value)

        headers = {
            "Authorization": f"Bearer {self.config.secret_key}",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=data,
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(f"Stripe error: {response.status_code} {response.text}")
        payload = response.json()
        return StripeSession(session_id=payload["id"], url=payload["url"])

    async def get_checkout_session(self, session_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.secret_key}",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(f"Stripe error: {response.status_code} {response.text}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("stripe_invalid_response")
        return payload

    def verify_webhook(
        self, payload: bytes, signature_header: str, tolerance: int = 300
    ) -> dict[str, Any]:
        timestamp, signatures = _parse_signature_header(signature_header)
        if timestamp is None or not signatures:
            raise ValueError("Missing Stripe signature.")

        now = int(time.time())
        if abs(now - timestamp) > tolerance:
            raise ValueError("Stripe signature timestamp out of tolerance.")

        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        expected = hmac.new(
            self.config.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not any(hmac.compare_digest(expected, sig) for sig in signatures):
            raise ValueError("Invalid Stripe signature.")

        return json.loads(payload.decode("utf-8"))


def build_stripe_config(settings: Settings) -> StripeConfig | None:
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        return None
    success_url, cancel_url = derive_stripe_urls(settings)
    if not success_url or not cancel_url:
        return None
    return StripeConfig(
        secret_key=settings.stripe_secret_key,
        webhook_secret=settings.stripe_webhook_secret,
        success_url=success_url,
        cancel_url=cancel_url,
    )


def derive_stripe_urls(settings: Settings) -> tuple[str | None, str | None]:
    success_url = settings.stripe_success_url
    cancel_url = settings.stripe_cancel_url
    if (not success_url or not cancel_url) and settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
        success_url = success_url or f"{base}/stripe/success"
        cancel_url = cancel_url or f"{base}/stripe/cancel"
    return success_url, cancel_url


def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    timestamp = None
    signatures: list[str] = []
    if not header:
        return timestamp, signatures
    for part in header.split(","):
        part = part.strip()
        if part.startswith("t="):
            try:
                timestamp = int(part.split("=", 1)[1])
            except ValueError:
                timestamp = None
        elif part.startswith("v1="):
            signatures.append(part.split("=", 1)[1])
    return timestamp, signatures

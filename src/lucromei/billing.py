"""Stripe subscriptions, in TEST MODE ONLY.

- ASSINAR creates (or reuses) a Stripe Checkout session for the monthly BRL price and answers
  with its link (subscription.CheckoutProvider).
- POST /webhooks/stripe verifies Stripe's signature, stores each event once
  (subscription_events.provider_event_id is unique) and queues a "stripe_sync" job.
- The job re-reads the subscription from Stripe (the source of truth, so events arriving out
  of order do no harm) and updates our row. Access is granted only when Stripe reports the
  subscription active with its latest invoice paid.

Live keys are refused: billing stays off unless the key starts with sk_test_ or rk_test_, and
live-mode events are ignored. Turning on real charges needs a deliberate code change.
"""

import hmac
import json
import logging
import time
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from lucromei import subscription
from lucromei.config import Settings
from lucromei.models import Business, Subscription

logger = logging.getLogger(__name__)

API = "https://api.stripe.com/v1/"
API_VERSION = "2024-06-20"
SIGNATURE_TOLERANCE_SECONDS = 300
EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
}
_STATUS = {
    "active": subscription.ACTIVE,
    "trialing": subscription.ACTIVE,
    "past_due": subscription.PAST_DUE,
    "unpaid": subscription.PAST_DUE,
    "incomplete": subscription.PENDING_PAYMENT,
    "incomplete_expired": subscription.EXPIRED,
    "canceled": subscription.CANCELED,
    "paused": subscription.PAST_DUE,
}


class BillingUnavailable(RuntimeError):
    """Stripe could not be reached or answered unexpectedly. Safe to retry."""

    code = "stripe_unavailable"


def is_test_key(key: str) -> bool:
    return key.startswith(("sk_test_", "rk_test_"))


def configured(settings: Settings) -> bool:
    if settings.stripe_secret_key and not is_test_key(settings.stripe_secret_key):
        logger.error("stripe_live_key_refused")
        return False
    return (
        is_test_key(settings.stripe_secret_key)
        and settings.stripe_price_id.startswith("price_")
        and settings.stripe_webhook_secret.startswith("whsec_")
        and settings.public_url.startswith("https://")
    )


class StripeClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not is_test_key(settings.stripe_secret_key):
            raise ValueError("Só chaves de teste do Stripe são aceitas.")
        self._key = settings.stripe_secret_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0))

    def __repr__(self) -> str:  # never show the key
        return "StripeClient()"

    def request(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Stripe-Version": API_VERSION}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._client.request(
                "GET" if data is None else "POST",
                API + path,
                auth=(self._key, ""),
                headers=headers,
                data=data,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise BillingUnavailable() from exc
        if not response.is_success:
            raise BillingUnavailable()
        try:
            body = response.json()
        except ValueError as exc:
            raise BillingUnavailable() from exc
        if body.get("livemode") is True:
            raise BillingUnavailable()  # never act on live-mode objects
        return body


class StripeCheckout:
    """subscription.CheckoutProvider backed by Stripe Checkout (test mode)."""

    def __init__(self, settings: Settings, client: StripeClient) -> None:
        self.settings = settings
        self.client = client

    def checkout_url(self, session: Session, business: Business, sub: Subscription) -> str:
        price = self.client.request(f"prices/{self.settings.stripe_price_id}")
        recurring = price.get("recurring") or {}
        if (
            price.get("currency") != "brl"
            or not price.get("active")
            or price.get("unit_amount") != sub.price_cents
            or recurring.get("interval") != "month"
            or recurring.get("interval_count") != 1
        ):
            logger.error("stripe_price_mismatch")
            raise BillingUnavailable()
        if not sub.provider_customer_id:
            customer = self.client.request(
                "customers",
                data={"metadata[lucromei_business]": business.id},
                idempotency_key=f"customer:{business.id}",
            )
            sub.provider_customer_id = customer["id"]
        if sub.provider_checkout_id:
            existing = self.client.request(f"checkout/sessions/{sub.provider_checkout_id}")
            if existing.get("status") == "open" and existing.get("url"):
                return existing["url"]
        checkout = self.client.request(
            "checkout/sessions",
            data={
                "mode": "subscription",
                "customer": sub.provider_customer_id,
                "line_items[0][price]": self.settings.stripe_price_id,
                "line_items[0][quantity]": "1",
                "client_reference_id": sub.id,
                "metadata[lucromei_subscription]": sub.id,
                "subscription_data[metadata][lucromei_subscription]": sub.id,
                "success_url": self.settings.public_url.rstrip("/") + "/assinatura/obrigado",
                "cancel_url": self.settings.public_url.rstrip("/") + "/assinatura/cancelada",
                "locale": "pt-BR",
            },
            idempotency_key=f"checkout:{sub.id}:{int(time.time() // 3600)}",
        )
        sub.provider = "stripe"
        sub.provider_checkout_id = checkout["id"]
        sub.status = subscription.PENDING_PAYMENT
        sub.updated_at = datetime.now(UTC)
        return checkout["url"]

    def cancel(self, provider_subscription_id: str) -> None:
        self.client.request(f"subscriptions/{provider_subscription_id}/cancel", data={})


def verify_event(body: bytes, header: str, secret: str, *, now: float | None = None) -> dict:
    """Check the Stripe-Signature header (t=..., v1=...) and return the event."""
    try:
        parts = [item.strip().split("=", 1) for item in header.split(",")]
        stamp = next(value for name, value in parts if name == "t")
        if abs((time.time() if now is None else now) - int(stamp)) > SIGNATURE_TOLERANCE_SECONDS:
            raise ValueError
        expected = hmac.new(secret.encode(), stamp.encode() + b"." + body, sha256).hexdigest()
        if not any(name == "v1" and hmac.compare_digest(expected, value) for name, value in parts):
            raise ValueError
        event = json.loads(body)
        if not isinstance(event, dict):
            raise ValueError
        return event
    except (ValueError, StopIteration, TypeError, RecursionError):
        raise ValueError("Assinatura ou corpo do Stripe inválido.") from None


def subscription_reference(event: dict) -> tuple[str | None, str | None]:
    """(Stripe subscription id, our subscription id) named by an event, when present."""
    obj = (event.get("data") or {}).get("object") or {}
    kind = event.get("type", "")
    if kind.startswith("customer.subscription."):
        return obj.get("id"), (obj.get("metadata") or {}).get("lucromei_subscription")
    if kind == "checkout.session.completed":
        return obj.get("subscription"), obj.get("client_reference_id")
    if kind.startswith("invoice."):
        parent = (obj.get("parent") or {}).get("subscription_details") or {}
        return obj.get("subscription") or parent.get("subscription"), (
            parent.get("metadata") or {}
        ).get("lucromei_subscription")
    return None, None


def sync(
    session: Session, client: StripeClient, settings: Settings, stripe_id: str | None,
    our_id: str | None, now: datetime,
) -> Subscription | None:
    """Re-read a subscription from Stripe and mirror its state. Returns it if it became paid."""
    sub = None
    if our_id:
        sub = session.get(Subscription, our_id, with_for_update=True)
    if sub is None and stripe_id:
        sub = session.scalar(
            select(Subscription)
            .where(Subscription.provider_subscription_id == stripe_id)
            .with_for_update()
        )
    if sub is None or not stripe_id:
        return None
    remote = client.request(f"subscriptions/{stripe_id}", params={"expand[]": "latest_invoice"})
    items = (remote.get("items") or {}).get("data") or []
    if (
        remote.get("customer") != sub.provider_customer_id
        or len(items) != 1
        or (items[0].get("price") or {}).get("id") != settings.stripe_price_id
    ):
        logger.warning("stripe_subscription_mismatch")
        return None
    was_paid = subscription.is_paid(sub, now)
    sub.provider = "stripe"
    sub.provider_subscription_id = stripe_id
    sub.status = _STATUS.get(remote.get("status", ""), subscription.PAST_DUE)
    invoice = remote.get("latest_invoice") or {}
    period_end = remote.get("current_period_end") or items[0].get("current_period_end")
    if sub.status == subscription.ACTIVE and invoice.get("status") == "paid" and period_end:
        sub.current_period_end = datetime.fromtimestamp(int(period_end), UTC)
    elif sub.status == subscription.ACTIVE:
        sub.status = subscription.PENDING_PAYMENT
    if sub.status == subscription.CANCELED and sub.canceled_at is None:
        sub.canceled_at = now
    sub.updated_at = now
    logger.info("stripe_subscription_synced", extra={"outcome": sub.status})
    return sub if subscription.is_paid(sub, now) and not was_paid else None

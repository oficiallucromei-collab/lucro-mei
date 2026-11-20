"""Stripe-hosted BRL card subscriptions. LucroMEI stores provider IDs and status."""

import hmac
import json
import time
from datetime import UTC, datetime
from hashlib import sha256

import httpx
from sqlalchemy import select

from lucromei.growth import access, record
from lucromei.models import Acquisition, Business

API_VERSION = "2024-06-20"
EVENTS = {
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
}


class BillingUnavailable(RuntimeError):
    pass


def configured(settings):
    mode = "live" if settings.growth_stripe_live else "test"
    return (
        settings.growth_enabled
        and settings.growth_stripe_key.startswith((f"sk_{mode}_", f"rk_{mode}_"))
        and settings.growth_stripe_price.startswith("price_")
        and settings.growth_stripe_webhook_secret.startswith("whsec_")
        and settings.growth_public_url
    )


def stripe(settings, path, *, data=None, key=None, params=None):
    headers = {"Stripe-Version": API_VERSION}
    if key:
        headers["Idempotency-Key"] = key
    try:
        response = httpx.request(
            "GET" if data is None else "POST",
            "https://api.stripe.com/v1/" + path,
            auth=(settings.growth_stripe_key, ""),
            headers=headers,
            data=data,
            params=params,
            timeout=8,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise BillingUnavailable("Stripe request failed") from None


def billing_reply(profile, settings, now, *, manage, message_id):
    if not configured(settings):
        return "A assinatura ainda não está disponível."
    try:
        if manage or profile.subscription_status in {"active", "past_due", "unpaid", "incomplete"}:
            if not profile.stripe_customer:
                return "Você ainda não tem uma assinatura para gerenciar."
            portal = stripe(
                settings,
                "billing_portal/sessions",
                data={
                    "customer": profile.stripe_customer,
                    "return_url": settings.growth_public_url,
                },
            )
            return f"Gerencie seus pagamentos ou cancele aqui: {portal['url']}"
        price = stripe(settings, f"prices/{settings.growth_stripe_price}")
        if (
            price["currency"] != "brl"
            or not price["active"]
            or price["unit_amount"] != settings.growth_price_cents
            or price["recurring"]["interval"] != "month"
            or price["recurring"]["interval_count"] != 1
            or price["recurring"]["usage_type"] != "licensed"
        ):
            raise BillingUnavailable("Invalid monthly BRL price")
        if not profile.stripe_customer:
            customer = stripe(
                settings,
                "customers",
                data={
                    "metadata[lucromei_business]": profile.business_id,
                },
                key=f"customer:{profile.business_id}",
            )
            profile.stripe_customer = customer["id"]
        if profile.checkout_id:
            existing = stripe(settings, f"checkout/sessions/{profile.checkout_id}")
            if existing["status"] == "open":
                return f"Conclua a assinatura aqui: {existing['url']}"
            if (
                existing["status"] == "complete"
                and profile.subscription_id != existing["subscription"]
            ):
                return "Recebemos seu checkout. Envie MEU PLANO para consultar a confirmação."
        checkout = stripe(
            settings,
            "checkout/sessions",
            data={
                "mode": "subscription",
                "customer": profile.stripe_customer,
                "line_items[0][price]": settings.growth_stripe_price,
                "line_items[0][quantity]": "1",
                "payment_method_types[0]": "card",
                "success_url": settings.growth_public_url + "/assinatura",
                "cancel_url": settings.growth_public_url,
            },
            key=f"checkout:{profile.business_id}:{sha256(message_id.encode()).hexdigest()}",
        )
        profile.checkout_id = checkout["id"]
        return f"Conclua a assinatura aqui: {checkout['url']}"
    except BillingUnavailable:
        return "Não foi possível abrir o pagamento agora. Envie ASSINAR novamente em instantes."


def verify_event(body, signature, secret, *, now=None):
    try:
        parts = [item.strip().split("=", 1) for item in signature.split(",")]
        stamp = next(value for name, value in parts if name == "t")
        if abs((time.time() if now is None else now) - int(stamp)) > 300:
            raise ValueError
        expected = hmac.new(secret.encode(), stamp.encode() + b"." + body, sha256).hexdigest()
        if not any(name == "v1" and hmac.compare_digest(expected, value) for name, value in parts):
            raise ValueError
        return json.loads(body)
    except (ValueError, StopIteration, TypeError, RecursionError):
        raise ValueError("Invalid Stripe signature or body") from None


def sync_event(session, settings, event, now):
    if event.get("type") not in EVENTS or event.get("livemode") is not settings.growth_stripe_live:
        return
    obj = event["data"]["object"]
    profile = (
        session.scalar(
            select(Acquisition).where(Acquisition.stripe_customer == obj.get("customer"))
        )
        if obj.get("customer")
        else None
    )
    if profile is None:
        return
    business_id = session.scalar(
        select(Business.id).where(Business.id == profile.business_id).with_for_update()
    )
    if not business_id:
        return
    session.refresh(profile)
    event_type = event["type"]
    subscription_id = (
        obj["id"]
        if event_type.startswith("customer.subscription.")
        else (
            obj.get("subscription")
            or (obj.get("parent") or {}).get("subscription_details", {}).get("subscription")
        )
    )
    if not subscription_id:
        return
    if profile.subscription_id and profile.subscription_id != subscription_id:
        if profile.subscription_status not in {"canceled", "incomplete_expired"}:
            return
        if not profile.checkout_id:
            return
        checkout = (
            obj
            if event_type == "checkout.session.completed"
            else stripe(settings, f"checkout/sessions/{profile.checkout_id}")
        )
        if (
            checkout["id"] != profile.checkout_id
            or checkout["status"] != "complete"
            or checkout["subscription"] != subscription_id
        ):
            return
    if not record(session, business_id, f"stripe:{event['id']}", now):
        return
    subscription = stripe(
        settings, f"subscriptions/{subscription_id}", params={"expand[]": "latest_invoice"}
    )
    items = subscription["items"]["data"]
    if (
        subscription["customer"] != profile.stripe_customer
        or len(items) != 1
        or items[0]["price"]["id"] != settings.growth_stripe_price
    ):
        return
    was_paid = access(profile, now) == "paid"
    recurring = items[0]["price"]["unit_amount"]
    if profile.subscription_id != subscription_id:
        profile.paid_through = None
    profile.subscription_id = subscription_id
    profile.subscription_status = subscription["status"]
    profile.recurring_cents = recurring
    invoice = subscription.get("latest_invoice")
    if subscription["status"] == "active" and invoice and invoice["status"] == "paid":
        profile.paid_through = datetime.fromtimestamp(subscription["current_period_end"], UTC)
        if not was_paid:
            record(session, business_id, f"mrr_new:{subscription_id}", now, recurring)
        record(session, business_id, "paid_conversion", now)
    if event_type == "invoice.paid" and obj.get("currency") == "brl":
        record(session, business_id, f"paid_invoice:{obj['id']}", now, obj["amount_paid"])
    elif event_type == "invoice.payment_failed":
        record(session, business_id, f"payment_failed:{obj['id']}", now)
    if subscription["status"] == "canceled":
        record(
            session,
            business_id,
            f"canceled:{subscription_id}",
            now,
            recurring if profile.paid_through else 0,
        )

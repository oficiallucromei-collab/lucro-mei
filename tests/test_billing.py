"""Stripe in test mode: checkout link, signed idempotent webhooks, sync, cancel on erasure."""

import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import func, select
from support import (
    TestingSession,
    client,
    onboard,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
)

from lucromei import billing
from lucromei.config import get_settings
from lucromei.main import app
from lucromei.models import Business, Job, Subscription, SubscriptionEvent, Transaction
from lucromei.privacy import SCHEDULED
from lucromei.worker import Worker

WHSEC = "whsec_test_secret"
PERIOD_END = int((datetime.now(UTC) + timedelta(days=30)).timestamp())


class FakeStripe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.price_amount = 2490
        self.subscription_status = "active"
        self.invoice_status = "paid"
        self.price_id = "price_test_1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((request.method, path, form))
        assert request.headers["Stripe-Version"] == billing.API_VERSION
        if path.startswith("prices/"):
            return httpx.Response(
                200,
                json={
                    "id": "price_test_1",
                    "currency": "brl",
                    "active": True,
                    "unit_amount": self.price_amount,
                    "livemode": False,
                    "recurring": {"interval": "month", "interval_count": 1},
                },
            )
        if path == "customers":
            assert request.headers["Idempotency-Key"].startswith("customer:")
            return httpx.Response(200, json={"id": "cus_1", "livemode": False})
        if path == "checkout/sessions":
            assert form["mode"] == "subscription" and form["line_items[0][price]"] == "price_test_1"
            return httpx.Response(
                200,
                json={
                    "id": "cs_test_1",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_1",
                    "status": "open",
                    "livemode": False,
                },
            )
        if path.endswith("/cancel"):
            return httpx.Response(
                200, json={"id": "sub_1", "status": "canceled", "livemode": False}
            )
        if path.startswith("subscriptions/"):
            return httpx.Response(
                200,
                json={
                    "id": "sub_1",
                    "customer": "cus_1",
                    "status": self.subscription_status,
                    "livemode": False,
                    "current_period_end": PERIOD_END,
                    "items": {"data": [{"price": {"id": self.price_id}}]},
                    "latest_invoice": {"status": self.invoice_status},
                },
            )
        return httpx.Response(404, json={})


def configure() -> None:
    settings = get_settings()
    settings.stripe_secret_key = "sk_test_123"
    settings.stripe_webhook_secret = WHSEC
    settings.stripe_price_id = "price_test_1"
    settings.public_url = "https://lucromei.example"


def setup_function() -> None:
    reset_state()
    configure()


def teardown_function() -> None:
    settings = get_settings()
    settings.stripe_secret_key = settings.stripe_webhook_secret = ""
    settings.stripe_price_id = settings.public_url = ""


def runner(fake: FakeStripe) -> Worker:
    worker = Worker(TestingSession, app.state.whatsapp_sender, get_settings())
    stripe = billing.StripeClient(
        get_settings(), client=httpx.Client(transport=httpx.MockTransport(fake))
    )
    worker.checkout = billing.StripeCheckout(get_settings(), stripe)
    return worker


def say(fake: FakeStripe, message_id: str, text: str) -> str:
    signed_webhook(
        whatsapp_payload(message_id, text, timestamp=str(int(time.time()))), process=False
    )
    runner(fake).drain()
    return sent_texts()[-1]


def stripe_post(event: dict, secret: str = WHSEC, stamp: int | None = None):
    body = json.dumps(event).encode()
    stamp = int(time.time()) if stamp is None else stamp
    signature = hmac.new(secret.encode(), f"{stamp}.".encode() + body, sha256).hexdigest()
    return client.post(
        "/webhooks/stripe",
        content=body,
        headers={
            "Stripe-Signature": f"t={stamp},v1={signature}",
            "content-type": "application/json",
        },
    )


def completed_event(event_id: str, subscription_ref: str, livemode: bool = False) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": livemode,
        "data": {
            "object": {
                "id": "cs_test_1",
                "subscription": "sub_1",
                "client_reference_id": subscription_ref,
            }
        },
    }


def test_live_keys_are_refused() -> None:
    settings = get_settings()
    assert billing.configured(settings)
    settings.stripe_secret_key = "sk_live_123"
    assert not billing.configured(settings)
    with pytest.raises(ValueError):
        billing.StripeClient(settings)
    settings.stripe_secret_key = "rk_live_123"
    assert not billing.configured(settings)


def test_signature_verification() -> None:
    body = b'{"id":"evt_1"}'
    stamp = int(time.time())
    good = hmac.new(WHSEC.encode(), f"{stamp}.".encode() + body, sha256).hexdigest()
    assert billing.verify_event(body, f"t={stamp},v1={good}", WHSEC)["id"] == "evt_1"
    for header in (f"t={stamp},v1=00", f"t={stamp - 1000},v1={good}", "", "garbage"):
        with pytest.raises(ValueError):
            billing.verify_event(body, header, WHSEC)


def test_checkout_webhook_sync_and_access_after_trial() -> None:
    business_id = onboard()
    with TestingSession() as session:
        session.get(Business, business_id).trial_ends_at = datetime.now(UTC) - timedelta(days=1)
        session.commit()
    fake = FakeStripe()
    reply = say(fake, "m1", "assinar")
    assert "https://checkout.stripe.com/c/pay/cs_test_1" in reply
    with TestingSession() as session:
        sub = session.scalar(select(Subscription))
        assert (sub.status, sub.provider, sub.provider_customer_id, sub.provider_checkout_id) == (
            "pending_payment",
            "stripe",
            "cus_1",
            "cs_test_1",
        )
        sub_id = sub.id
    assert say(fake, "m2", "Recebi 100 por um corte").startswith("Seu teste grátis terminou")

    assert stripe_post(completed_event("evt_1", sub_id)).json() == {"received": True}
    assert stripe_post(completed_event("evt_1", sub_id)).status_code == 200  # redelivered
    runner(fake).drain()
    with TestingSession() as session:
        sub = session.get(Subscription, sub_id)
        assert sub.status == "active" and sub.provider_subscription_id == "sub_1"
        assert session.scalar(select(func.count(SubscriptionEvent.id))) == 1
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == "stripe_sync")) == 1
    assert sent_texts()[-1].startswith(
        "Pagamento confirmado! Sua assinatura do LucroMEI está ativa até"
    )
    assert say(fake, "m3", "Recebi 100 por um corte").startswith("Entrada registrada")


def test_unpaid_invoice_or_wrong_price_does_not_grant_access() -> None:
    onboard()
    fake = FakeStripe()
    say(fake, "m1", "assinar")
    with TestingSession() as session:
        sub_id = session.scalar(select(Subscription.id))
    fake.invoice_status = "open"
    stripe_post(completed_event("evt_a", sub_id))
    runner(fake).drain()
    with TestingSession() as session:
        assert session.get(Subscription, sub_id).status == "pending_payment"
    fake.invoice_status, fake.price_id = "paid", "price_other"
    stripe_post({**completed_event("evt_b", sub_id)})
    runner(fake).drain()
    with TestingSession() as session:
        assert session.get(Subscription, sub_id).current_period_end is None


def test_bad_signature_live_events_and_unconfigured() -> None:
    onboard()
    assert stripe_post(completed_event("evt_x", "s"), secret="whsec_wrong").status_code == 400
    assert stripe_post(completed_event("evt_live", "s", livemode=True)).status_code == 200
    with TestingSession() as session:
        assert session.scalar(select(func.count(SubscriptionEvent.id))) == 0
    get_settings().stripe_secret_key = ""
    assert stripe_post(completed_event("evt_y", "s")).status_code == 404


def test_price_mismatch_blocks_checkout() -> None:
    onboard()
    fake = FakeStripe()
    fake.price_amount = 9900
    signed_webhook(
        whatsapp_payload("m1", "assinar", timestamp=str(int(time.time()))), process=False
    )
    worker = runner(fake)
    worker.drain()
    assert not any(path == "checkout/sessions" for _, path, _ in fake.calls)
    with TestingSession() as session:
        assert session.scalar(select(Transaction)) is None


def test_account_deletion_cancels_the_stripe_subscription_first() -> None:
    from lucromei.jobs import JobContext
    from lucromei.models import DeletionRequest
    from lucromei.processing import handle_delete_account

    business_id = onboard()
    fake = FakeStripe()
    now = datetime.now(UTC)
    with TestingSession() as session:
        session.add(
            Subscription(
                business_id=business_id,
                status="active",
                provider="stripe",
                price_cents=2490,
                provider_customer_id="cus_1",
                provider_subscription_id="sub_1",
                current_period_end=now + timedelta(days=10),
                created_at=now,
                updated_at=now,
            )
        )
        request = DeletionRequest(
            business_id=business_id,
            subject_digest="d",
            status=SCHEDULED,
            requested_at=now,
            confirm_by=now,
            scheduled_for=now - timedelta(minutes=1),
        )
        session.add(request)
        session.commit()
        handle_delete_account(
            session,
            {"deletion_request_id": request.id},
            JobContext(now=now, services={"checkout": runner(fake).checkout, "max_attempts": 8}),
        )
        session.commit()
        assert session.get(Business, business_id) is None
        assert session.scalar(select(Subscription)) is None
    assert ("POST", "subscriptions/sub_1/cancel", {}) in fake.calls

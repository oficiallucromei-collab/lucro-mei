import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from lucromei import billing, growth_api, main
from lucromei.config import Settings
from lucromei.db import get_session
from lucromei.growth import BRAZIL, access, handle_growth_message
from lucromei.growth_jobs import eligible_message, prune_message_text, run_lifecycle
from lucromei.models import Acquisition, Base, GrowthEvent, InboundMessage, Transaction
from lucromei.service import LedgerService
from lucromei.whatsapp import MockOutboundSender, WhatsAppTextMessage

USER = "5511999999999"
AUTH = ("founder", "x" * 48)


@pytest.fixture
def growth_app(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    settings = Settings(
        growth_enabled=True,
        growth_enforce_trial=True,
        growth_founder_token=AUTH[1],
        growth_whatsapp_number="5511999999900",
        growth_privacy_url="https://example.com/privacy",
        growth_terms_url="https://example.com/terms",
        growth_public_url="https://example.com",
        growth_stripe_key="sk_test_mock",
        growth_stripe_price="price_mock",
        growth_stripe_webhook_secret="whsec_mock",
        whatsapp_app_secret="mock-wa-key",
    )

    def sessions():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(growth_api, "get_settings", lambda: settings)
    sender = MockOutboundSender()
    monkeypatch.setattr(main.app.state, "whatsapp_sender", sender, raising=False)
    main.app.dependency_overrides[get_session] = sessions
    try:
        with TestClient(main.app) as client:
            yield client, sender, settings, engine
    finally:
        main.app.dependency_overrides.pop(get_session)
        engine.dispose()


def whatsapp(client, settings, message_id, text, *, sender=USER, referral=None, stamp=None):
    message = {
        "id": message_id,
        "from": sender,
        "type": "text",
        "text": {"body": text},
        "timestamp": str(int(time.time()) if stamp is None else stamp),
    }
    if referral:
        message["referral"] = {"source_id": referral}
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messages": [message]},
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(settings.whatsapp_app_secret.encode(), body, sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=" + signature}
    )


def stripe_webhook(client, settings, name, obj, identifier):
    stamp = str(int(time.time()))
    body = json.dumps(
        {
            "id": identifier,
            "type": name,
            "livemode": False,
            "data": {"object": obj},
        }
    ).encode()
    signature = hmac.new(
        settings.growth_stripe_webhook_secret.encode(), stamp.encode() + b"." + body, sha256
    ).hexdigest()
    return client.post(
        "/webhooks/stripe", content=body, headers={"Stripe-Signature": f"t={stamp},v1={signature}"}
    )


def test_click_to_activated_and_paid(growth_app, monkeypatch):
    client, sender, settings, engine = growth_app
    page = client.get("/?utm_source=instagram&campaign_id=123")
    assert page.status_code == 200
    assert "R$ 24,90" in page.text and "https://example.com/privacy" in page.text
    assert "campaign_id=123" in page.text
    redirect = client.get(
        "/experimentar?utm_source=instagram&campaign_id=123", follow_redirects=False
    )
    assert redirect.status_code == 303
    prefill = parse_qs(urlparse(redirect.headers["location"]).query)["text"][0]
    assert "instagram" not in redirect.headers["location"]

    for index, text in enumerate(
        [
            prefill,
            "3",
            "Recebi R$150 por um serviço",
            "Gastei R$50 em materiais",
            "Recebi R$20 por um serviço",
            "resumo do mês",
        ]
    ):
        assert whatsapp(client, settings, f"wa{index}", text).status_code == 200
    assert "14 dias" in sender.sent_messages[0].text
    assert "Saldo R$ 150,00" in sender.sent_messages[2].text
    assert "Saldo: R$ 120,00" in sender.sent_messages[-1].text
    assert whatsapp(client, settings, "wa4", "Recebi R$20 por um serviço").json()["duplicates"] == 1
    assert len(sender.sent_messages) == 6
    assert (
        whatsapp(
            client, settings, "wa4", "Recebi R$20 por um serviço", sender="5511888888888"
        ).json()["ignored"]
        == 1
    )
    assert (
        whatsapp(
            client, settings, "other", prefill, sender="5511777777777", referral="456"
        ).status_code
        == 200
    )

    state = {"status": "active", "invoice_status": "paid", "period_days": 28}
    calls = []

    def stripe(settings, path, **kwargs):
        calls.append(path)
        if path == "prices/price_mock":
            return {
                "currency": "brl",
                "active": True,
                "unit_amount": 2490,
                "recurring": {"interval": "month", "interval_count": 1, "usage_type": "licensed"},
            }
        if path == "customers":
            return {"id": "cus_test"}
        if path == "checkout/sessions":
            assert kwargs["data"]["mode"] == "subscription"
            return {"id": "cs_test", "url": "https://checkout.stripe.com/mock"}
        if path == "billing_portal/sessions":
            return {"url": "https://billing.stripe.com/mock"}
        assert path == "subscriptions/sub_test"
        return {
            "customer": "cus_test",
            "status": state["status"],
            "current_period_end": int(time.time()) + state["period_days"] * 86400,
            "latest_invoice": {"status": state["invoice_status"]},
            "items": {"data": [{"price": {"id": "price_mock", "unit_amount": 2490}}]},
        }

    monkeypatch.setattr(billing, "stripe", stripe)
    assert whatsapp(client, settings, "checkout", "assinar").status_code == 200
    assert "checkout.stripe.com/mock" in sender.sent_messages[-1].text
    invoice = {
        "id": "in_test",
        "customer": "cus_test",
        "subscription": "sub_test",
        "currency": "brl",
        "amount_paid": 2490,
    }
    assert stripe_webhook(client, settings, "invoice.paid", invoice, "evt_paid").status_code == 204
    assert stripe_webhook(client, settings, "invoice.paid", invoice, "evt_paid").status_code == 204
    assert calls.count("subscriptions/sub_test") == 1
    assert whatsapp(client, settings, "plan", "meu plano").status_code == 200
    assert sender.sent_messages[-1].text == "Assinatura ativa"
    assert whatsapp(client, settings, "portal", "gerenciar assinatura").status_code == 200
    assert "billing.stripe.com/mock" in sender.sent_messages[-1].text

    today = datetime.now(BRAZIL).date().isoformat()
    csv = f"day,campaign,spend_cents,impressions,clicks\n{today},123,13000,1000,30\n"
    assert client.get("/founder").status_code == 401
    assert client.get("/founder/metrics", auth=("founder", "wrong")).status_code == 401
    assert client.get("/founder", auth=AUTH).status_code == 200
    assert client.post("/founder/campaigns", content=csv, auth=AUTH).status_code == 415
    for _ in range(2):
        result = client.post(
            "/founder/campaigns", content=csv, auth=AUTH, headers={"Content-Type": "text/csv"}
        )
        assert result.json() == {"imported": 1}
    report = client.get(f"/founder/metrics?start={today}&end={today}", auth=AUTH).json()
    assert report["measured"]["trial_signups"] == 2
    assert report["measured"]["activation_rate"] == 0.5
    assert report["measured"]["mrr_now_cents"] == 2490
    assert report["measured"]["revenue_by_source_cents"] == {"instagram": 2490}
    assert report["campaigns"]["123"]["cac_cents"] == 13000
    assert report["experiment"]["alert"] == "budget_warning"

    state.update(status="past_due", invoice_status="open")
    failed = stripe_webhook(
        client, settings, "invoice.payment_failed", invoice | {"id": "in_failed"}, "evt_failed"
    )
    assert failed.status_code == 204
    with Session(engine) as session:
        profile = session.scalar(
            select(Acquisition).where(Acquisition.stripe_customer.is_not(None))
        )
        assert profile.subscription_status == "past_due"
        first_period_end = profile.paid_through
    state.update(status="active", invoice_status="paid", period_days=58)
    renewed = stripe_webhook(
        client, settings, "invoice.paid", invoice | {"id": "in_renewed"}, "evt_renewed"
    )
    assert renewed.status_code == 204
    with Session(engine) as session:
        profile = session.scalar(
            select(Acquisition).where(Acquisition.stripe_customer.is_not(None))
        )
        assert profile.paid_through > first_period_end
    state["status"] = "canceled"
    assert (
        stripe_webhook(
            client,
            settings,
            "customer.subscription.deleted",
            {"id": "sub_test", "customer": "cus_test"},
            "evt_cancel",
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/webhooks/stripe",
            content="{}",
            headers={
                "Stripe-Signature": "t=123,v1=bad",
            },
        ).status_code
        == 400
    )
    with Session(engine) as session:
        profile = session.scalar(
            select(Acquisition).where(Acquisition.stripe_customer.is_not(None))
        )
        assert profile.attribution == {"utm_source": "instagram", "campaign_id": "123"}
        assert profile.subscription_status == "canceled"
        assert session.get(GrowthEvent, (profile.business_id, "payment_failed:in_failed"))
        assert session.get(GrowthEvent, (profile.business_id, "canceled:sub_test"))
        assert session.scalar(select(func.count(Transaction.id))) == 3


def test_trial_expiry_opt_out_and_deduplication(growth_app):
    client, sender, settings, engine = growth_app
    assert whatsapp(client, settings, "welcome", "Olá!").status_code == 200
    assert whatsapp(client, settings, "consent", "aceito lembretes").status_code == 200
    with Session(engine) as session:
        profile = session.scalar(select(Acquisition))
        sent = []
        mocked_sender = SimpleNamespace(send_template=lambda *args: sent.append(args))
        settings.growth_lifecycle_enabled = True
        settings.growth_lifecycle_templates = {"activation_help": "trial_help"}
        daytime = datetime.now(BRAZIL).replace(hour=12, minute=0, second=0) + timedelta(days=1)
        assert run_lifecycle(session, settings, daytime, mocked_sender) == 1
        assert run_lifecycle(session, settings, daytime, mocked_sender) == 0
        assert sent == [(USER, "trial_help")]
        assert eligible_message(profile, {}, daytime.replace(hour=1)) is None
        profile.trial_end = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert whatsapp(client, settings, "stop", "parar").status_code == 200
    assert whatsapp(client, settings, "expired", "Recebi R$100 por um serviço").status_code == 200
    assert "avaliação terminou" in sender.sent_messages[-1].text
    assert whatsapp(client, settings, "export", "exportar meus dados").status_code == 200
    assert whatsapp(client, settings, "delete", "excluir minha conta").status_code == 200
    assert client.get("/founder/privacy-requests").status_code == 401
    assert len(client.get("/founder/privacy-requests", auth=AUTH).json()) == 2
    with Session(engine) as session:
        profile = session.scalar(select(Acquisition))
        assert profile.opt_in_at is None
        assert access(profile, datetime.now(UTC)) == "expired"
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_late_or_reversed_transactions_do_not_trigger_activation(growth_app):
    _, _, settings, engine = growth_app
    trial = datetime.now(UTC) - timedelta(days=3)
    with Session(engine) as session:
        for number, actions in [
            (
                USER,
                [
                    (0, "Oi"),
                    (0, "Recebi R$30"),
                    (0, "desfazer"),
                    (0, "Recebi R$30"),
                    (0, "Recebi R$30"),
                ],
            ),
            (
                "5511999999998",
                [(0, "Oi"), (72, "Recebi R$30"), (72, "Recebi R$30"), (72, "Recebi R$30")],
            ),
        ]:
            for index, (hours, text) in enumerate(actions):
                message = WhatsAppTextMessage(f"{number}:{index}", number, text)
                handle_growth_message(
                    session, settings, message, now=trial + timedelta(hours=hours)
                )
                session.commit()
        assert (
            session.scalar(
                select(func.count()).select_from(GrowthEvent).where(GrowthEvent.name == "activated")
            )
            == 0
        )


def test_verified_privacy_fulfillment_and_trial_hold(growth_app):
    client, sender, settings, engine = growth_app
    settings.growth_trial_hold_key = "separate-key-for-deletion" * 2
    for message_id, body in [
        ("started", "Recebi R$150 por um serviço"),
        ("exported", "exportar meus dados"),
        ("deleted", "excluir minha conta"),
    ]:
        assert whatsapp(client, settings, message_id, body).status_code == 200
    requests = client.get("/founder/privacy-requests", auth=AUTH).json()
    business_id = requests[0]["business_id"]
    path = f"/founder/privacy-requests/{business_id}"
    confirmation = {"identity_verified": True}
    assert client.post(path + "/deletion", json=confirmation).status_code == 401
    assert client.post(path + "/deletion", json={}, auth=AUTH).status_code == 422
    result = client.post(path + "/export", json=confirmation, auth=AUTH)
    assert result.json()["transactions"][0]["amount_cents"] == 15000
    assert result.json()["account"][0]["whatsapp_number"] == USER
    assert client.post(path + "/deletion", json=confirmation, auth=AUTH).json() == {
        "deleted": True,
    }
    assert client.get("/founder/privacy-requests", auth=AUTH).json() == []
    assert whatsapp(client, settings, "restart", "Oi").status_code == 200
    assert "avaliação terminou" in sender.sent_messages[-1].text
    with Session(engine) as session:
        profile = session.scalar(select(Acquisition))
        assert profile.business_id != business_id
        assert profile.trial_start is None
        assert session.scalar(select(func.count(Transaction.id))) == 0


def test_referral_reward_requires_eligible_first_activation(growth_app):
    client, sender, settings, engine = growth_app
    settings.growth_referrals_enabled = True
    assert whatsapp(client, settings, "starter", "Oi").status_code == 200
    assert whatsapp(client, settings, "referrer", "indicar").status_code == 200
    link = sender.sent_messages[-1].text.split()[-1]
    query = urlparse(link).query
    assert "referral_id=" in query

    for index, target in enumerate((USER, "5511777777333", "5511777777444")):
        redirect = client.get("/experimentar?" + query, follow_redirects=False)
        text = parse_qs(urlparse(redirect.headers["location"]).query)["text"][0]
        assert whatsapp(client, settings, f"ref{index}", text, sender=target).status_code == 200
        for step in range(3):
            result = whatsapp(client, settings, f"tx{index}{step}", "Recebi R$30", sender=target)
            assert result.status_code == 200
    with Session(engine) as session:
        days = sorted(
            (p.trial_end - p.trial_start).days for p in session.scalars(select(Acquisition))
        )
        assert days == [14, 21, 21]


def test_stale_message_no_reply_and_missing_spend_is_unknown(growth_app):
    client, sender, settings, _ = growth_app
    stamp = int(time.time()) - 86401
    assert whatsapp(client, settings, "stale", "Oi", stamp=stamp).status_code == 200
    assert not sender.sent_messages
    date = datetime.now(BRAZIL).date()
    result = client.get("/founder/metrics", params={"start": date, "end": date}, auth=AUTH)
    assert result.json()["measured"]["spend_cents"] is None
    assert result.json()["measured"]["cost_per_trial_cents"] is None


def test_rate_limit_for_public_trial_links(growth_app):
    client, _, settings, _ = growth_app
    settings.growth_public_requests_per_minute = 1
    assert client.get("/experimentar", follow_redirects=False).status_code == 303
    response = client.get("/experimentar", follow_redirects=False)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_message_retention_keeps_ledger_and_deduplication(growth_app):
    _, _, _, engine = growth_app
    now = datetime(2026, 11, 20, tzinfo=UTC)
    text = "Recebi R$30"
    with Session(engine) as session:
        ledger = LedgerService(session)
        old = ledger.process(message_id="old", whatsapp_number=USER, text=text)
        ledger.process(message_id="recent", whatsapp_number=USER, text=text)
        messages = {row.external_id: row for row in session.scalars(select(InboundMessage))}
        messages["old"].received_at = now - timedelta(days=30)
        messages["recent"].received_at = now - timedelta(days=29)
        session.commit()

        prune_message_text(session, now)
        prune_message_text(session, now)
        session.commit()

        assert messages["old"].raw_text == ""
        assert messages["recent"].raw_text == text
        retry = ledger.process(message_id="old", whatsapp_number=USER, text=text)
        assert retry.duplicate
        assert retry.transaction == old.transaction
        assert session.scalar(select(func.count(Transaction.id))) == 2

"""End of the free trial: soft block, ASSINAR, and access with an active subscription."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from support import (
    TestingSession,
    drain_jobs,
    onboard,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.config import get_settings
from lucromei.main import app
from lucromei.models import Business, Subscription, Transaction
from lucromei.privacy import delete_business_data
from lucromei.worker import Worker

NOW = datetime.now(UTC)


def setup_function() -> None:
    reset_state()
    get_settings().trial_enforcement_enabled = True
    get_settings().subscription_price_cents = 2490


def end_trial(business_id: str, days_ago: int = 1) -> None:
    with TestingSession() as session:
        business = session.get(Business, business_id)
        business.trial_ends_at = NOW - timedelta(days=days_ago)
        session.commit()


def say(message_id: str, text: str) -> str:
    signed_webhook(whatsapp_payload(message_id, text, timestamp=str(int(NOW.timestamp()))))
    return sent_texts()[-1]


def count_transactions() -> int:
    with TestingSession() as session:
        return len(session.scalars(select(Transaction)).all())


def test_entries_are_recorded_during_the_trial() -> None:
    onboard()
    assert say("m1", "Recebi 100 por um corte").startswith("Entrada registrada")


def test_after_the_trial_new_entries_are_softly_blocked() -> None:
    business_id = onboard()
    say("m0", "Recebi 100 por um corte")
    end_trial(business_id)
    reply = say("m1", "Recebi 50 por um corte")
    assert reply.startswith("Seu teste grátis terminou em ")
    assert "mande ASSINAR" in reply and "R$ 24,90 por mês" in reply
    assert count_transactions() == 1
    # Queries, corrections, help and privacy still work.
    assert "Entradas: R$ 100,00" in say("m2", "resumo do mês")
    assert say("m3", "ultimos lancamentos").startswith("Seu lançamento:")
    assert "ASSINAR" in say("m4", "ajuda")
    assert say("m5", "desfazer") == "Lançamento de R$ 100,00 desfeito."
    assert say("m6", "meus dados").startswith("Seus dados no LucroMEI")
    # Batches and answers to questions are blocked too.
    assert say("m7", "recebi 1 e gastei 2").startswith("Seu teste grátis terminou")
    assert count_transactions() == 1


def test_open_question_cannot_be_answered_after_the_trial() -> None:
    business_id = onboard()
    say("m1", "Paguei 300 para o fornecedor")
    end_trial(business_id)
    assert say("m2", "1").startswith("Seu teste grátis terminou")
    assert count_transactions() == 0


def test_enforcement_can_be_turned_off() -> None:
    business_id = onboard()
    end_trial(business_id)
    get_settings().trial_enforcement_enabled = False
    assert say("m1", "Recebi 100 por um corte").startswith("Entrada registrada")


def test_assinar_without_provider_records_the_request_once_and_charges_nothing() -> None:
    business_id = onboard()
    reply = say("m1", "Assinar")
    assert reply.startswith("Anotei seu interesse na assinatura do LucroMEI (R$ 24,90 por mês).")
    assert "Seu teste grátis continua até" in reply
    assert "Nenhum valor será cobrado sem a sua confirmação." in reply
    say("m2", "quero assinar!")
    with TestingSession() as session:
        rows = session.scalars(select(Subscription)).all()
        assert [(row.business_id, row.status, row.provider, row.price_cents) for row in rows] == [
            (business_id, "requested", "none", 2490)
        ]


class FakeCheckout:
    def __init__(self) -> None:
        self.calls = 0

    def checkout_url(self, session, business, subscription) -> str:
        self.calls += 1
        subscription.status = "pending_payment"
        return "https://checkout.example/abc"


def test_assinar_with_provider_returns_the_checkout_link() -> None:
    business_id = onboard()
    end_trial(business_id)
    checkout = FakeCheckout()
    signed_webhook(
        whatsapp_payload("m1", "assinar", timestamp=str(int(NOW.timestamp()))), process=False
    )
    runner = Worker(TestingSession, app.state.whatsapp_sender, get_settings())
    original = runner.context

    def context():
        ctx = original()
        ctx.services["checkout"] = checkout
        return ctx

    runner.context = context
    runner.drain()
    reply = sent_texts()[-1]
    assert "https://checkout.example/abc" in reply
    assert "teste grátis continua" not in reply
    assert checkout.calls == 1


def test_active_subscription_gives_access_until_period_end() -> None:
    business_id = onboard()
    end_trial(business_id)
    with TestingSession() as session:
        session.add(
            Subscription(
                business_id=business_id,
                status="active",
                provider="manual",
                price_cents=2490,
                current_period_end=NOW + timedelta(days=30),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    assert say("m1", "Recebi 100 por um corte").startswith("Entrada registrada")
    assert say("m2", "assinatura").startswith("Sua assinatura está ativa até ")
    assert "Assinatura: ativa até" in say("m3", "meus dados")
    with TestingSession() as session:
        session.scalar(select(Subscription)).current_period_end = NOW - timedelta(minutes=1)
        session.commit()
    assert say("m4", "Recebi 100 por um corte").startswith("Seu teste grátis terminou")


def test_account_deletion_removes_subscriptions() -> None:
    business_id = onboard()
    say("m1", "assinar")
    with TestingSession() as session:
        delete_business_data(session, session.get(Business, business_id))
        session.commit()
        assert session.scalars(select(Subscription)).all() == []
    drain_jobs()

from datetime import UTC, datetime

import pytest
from support import (
    TestingSession,
    client,
    onboard,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.config import Settings, check_production_settings
from lucromei.models import Business, InboundMessage, TermsAcceptance, Transaction
from lucromei.onboarding import extract_name

NEW_USER = "5521988887777"


def setup_function() -> None:
    reset_state()


def say(message_id: str, text: str, *, sender: str = NEW_USER, timestamp: str = "1795032000"):
    signed_webhook(whatsapp_payload(message_id, text, sender, timestamp=timestamp))
    return sent_texts()[-1]


def business(number: str = NEW_USER) -> Business:
    with TestingSession() as session:
        return session.query(Business).filter_by(whatsapp_number=number).one()


def complete_onboarding(prefix: str = "m") -> str:
    say(f"{prefix}1", "Oi")
    say(f"{prefix}2", "Carlos Souza")
    say(f"{prefix}3", "Barbearia")
    return say(f"{prefix}4", "Aceito")


def test_full_onboarding_records_terms_and_starts_trial() -> None:
    welcome = say("m1", "Oi")
    assert "como você se chama?" in welcome

    ask_type = say("m2", "Meu nome é carlos souza")
    assert ask_type.startswith("Prazer, Carlos!")
    assert "tipo de negócio" in ask_type

    terms = say("m3", "Barbearia")
    assert "Termos de Uso" in terms
    assert "versão 2026-11-21" in terms
    assert "https://lucromei.example/termos" in terms
    assert "responda ACEITO" in terms

    before = datetime.now(UTC)
    done = say("m4", "Aceito")
    assert done.startswith("Tudo certo, Carlos! Seu teste grátis de 14 dias começou hoje")
    assert "Recebi 150 por um corte" in done

    stored = business()
    assert (stored.display_name, stored.business_type, stored.onboarding_step) == (
        "Carlos Souza",
        "Barbearia",
        "active",
    )
    started = stored.trial_started_at.replace(tzinfo=stored.trial_started_at.tzinfo or UTC)
    ends = stored.trial_ends_at.replace(tzinfo=stored.trial_ends_at.tzinfo or UTC)
    assert started >= before.replace(microsecond=0)
    assert (ends - started).days == 14
    with TestingSession() as session:
        acceptance = session.query(TermsAcceptance).one()
        assert acceptance.terms_version == "2026-11-21"
        assert acceptance.source_message_id == "m4"
        assert acceptance.accepted_text == "Aceito"
        assert acceptance.accepted_at is not None


def test_trial_end_date_is_shown_in_sao_paulo_time() -> None:
    # 22:00 in São Paulo on 20/11 is already 21/11 in UTC; 14 days later is 04/12 locally.
    reset_state()
    from lucromei.config import get_settings
    from lucromei.onboarding import handle

    with TestingSession() as session:
        stored = Business(whatsapp_number=NEW_USER, onboarding_step="awaiting_terms")
        session.add(stored)
        session.flush()
        reply = handle(
            session,
            stored,
            message_id="m-late",
            text="aceito",
            now=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
            settings=get_settings(),
        )
    assert "vai até 04/12/2026" in reply


def test_no_transaction_is_recorded_before_terms_are_accepted() -> None:
    first = say("m1", "Recebi 150 por um corte")
    assert "como você se chama?" in first
    assert "Ainda não anotei esse lançamento" in first
    say("m2", "Carlos")
    say("m3", "Barbearia")
    still_waiting = say("m4", "Gastei 80 em produtos")
    assert "responda ACEITO" in still_waiting

    with TestingSession() as session:
        assert session.query(Transaction).count() == 0
        assert session.query(InboundMessage).count() == 0
        assert session.query(TermsAcceptance).count() == 0


def test_transactions_are_recorded_after_acceptance() -> None:
    complete_onboarding()
    reply = say("m5", "Recebi 150 por um corte")
    assert reply == "Entrada registrada: R$ 150,00. Responda 'desfazer' para corrigir."
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1


def test_declining_terms_keeps_ledger_closed() -> None:
    say("m1", "Oi")
    say("m2", "Carlos")
    say("m3", "Barbearia")
    reply = say("m4", "Não aceito")
    assert "não consigo registrar" in reply
    assert business().onboarding_step == "awaiting_terms"
    reply = say("m5", "Recebi 100 por um corte")
    assert "responda ACEITO" in reply
    with TestingSession() as session:
        assert session.query(Transaction).count() == 0


@pytest.mark.parametrize("bad_name", ["150", "Recebi 150", "!!!", "a b c d e f g"])
def test_invalid_name_is_asked_again(bad_name: str) -> None:
    say("m1", "Oi")
    reply = say("m2", bad_name)
    assert reply.startswith("Não consegui entender seu nome")
    assert business().onboarding_step == "awaiting_name"


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Carlos", "Carlos"),
        ("meu nome é joão da silva", "João Da Silva"),
        ("Oi, me chamo Ana.", "Ana"),
        ("sou a Márcia", "Márcia"),
        ("  JULIANA  ", "JULIANA"),
    ],
)
def test_name_extraction(text: str, name: str) -> None:
    assert extract_name(text) == name


def test_redelivered_onboarding_answer_is_not_applied_twice() -> None:
    say("m1", "Oi")
    payload = whatsapp_payload("m2", "Carlos", NEW_USER)
    signed_webhook(payload)
    signed_webhook(payload)  # Meta redelivery must not be read as the business type
    stored = business()
    assert (stored.display_name, stored.business_type) == ("Carlos", None)
    assert stored.onboarding_step == "awaiting_business_type"
    assert len(sent_texts()) == 2


def test_answers_arriving_together_are_applied_in_order() -> None:
    from datetime import timedelta

    from support import post_webhook, worker

    from lucromei.jobs import JobContext, utc_now
    from lucromei.main import app

    post_webhook(whatsapp_payload("m1", "Oi", NEW_USER, timestamp="100"))
    post_webhook(whatsapp_payload("m3", "Barbearia", NEW_USER, timestamp="102"))
    post_webhook(whatsapp_payload("m2", "Carlos", NEW_USER, timestamp="101"))
    queue = worker().queue
    now = utc_now()
    for step in range(6):  # later messages wait a couple of seconds for earlier ones
        services = {"sender": app.state.whatsapp_sender, "max_attempts": 8}
        queue.run_due(JobContext(now=now + timedelta(seconds=3 * step), services=services))
    stored = business()
    assert (stored.display_name, stored.business_type) == ("Carlos", "Barbearia")
    assert stored.onboarding_step == "awaiting_terms"


def test_each_number_has_its_own_onboarding() -> None:
    complete_onboarding()
    other = "5531977776666"
    reply = say("x1", "Recebi 50 por um corte", sender=other)
    assert "como você se chama?" in reply
    assert business(other).onboarding_step == "awaiting_name"
    assert business().onboarding_step == "active"


def test_internal_api_refuses_transactions_before_onboarding() -> None:
    response = client.post(
        "/v1/messages",
        json={"message_id": "api-1", "whatsapp_number": NEW_USER, "text": "Recebi 10"},
    )
    assert response.status_code == 409
    with TestingSession() as session:
        assert session.query(Transaction).count() == 0
        assert session.query(Business).count() == 0


def test_onboarded_business_can_use_internal_api() -> None:
    onboard(NEW_USER)
    response = client.post(
        "/v1/messages",
        json={"message_id": "api-2", "whatsapp_number": NEW_USER, "text": "Recebi 10"},
    )
    assert response.status_code == 201


def test_production_requires_terms_url() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        whatsapp_app_secret="real-secret",
        whatsapp_verify_token="real-token",
        terms_url="",
        privacy_hash_key="k" * 32,
    )
    with pytest.raises(RuntimeError, match="LUCROMEI_TERMS_URL"):
        check_production_settings(settings)
    settings.terms_url = "https://lucromei.com.br/termos"
    check_production_settings(settings)

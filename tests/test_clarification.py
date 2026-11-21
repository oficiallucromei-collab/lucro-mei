from datetime import UTC, timedelta

import pytest
from support import (
    TestingSession,
    client,
    onboard,
    post_webhook,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
    worker,
)

from lucromei.clarification import CANCEL, parse_answer
from lucromei.config import get_settings
from lucromei.domain import AmbiguousTransaction, TransactionType, parse_transaction
from lucromei.jobs import JobContext, utc_now
from lucromei.main import app
from lucromei.models import PendingEntry, Transaction, TransactionEvent

EXPENSE, WITHDRAWAL = TransactionType.EXPENSE, TransactionType.WITHDRAWAL
QUESTION = (
    "Antes de anotar R$ 300,00 (paguei para o fornecedor), me diz: é despesa do negócio ou "
    "retirada pessoal?\n\nResponda 1 para despesa do negócio ou 2 para retirada pessoal."
)


def setup_function() -> None:
    reset_state()
    onboard()


def say(message_id: str, text: str, timestamp: str = "1795032000") -> str:
    signed_webhook(whatsapp_payload(message_id, text, timestamp=timestamp))
    return sent_texts()[-1]


def transactions() -> list[Transaction]:
    with TestingSession() as session:
        return session.query(Transaction).all()


def pending_entries() -> list[PendingEntry]:
    with TestingSession() as session:
        return session.query(PendingEntry).order_by(PendingEntry.created_at).all()


def test_parser_reports_amount_and_candidates_for_ambiguous_payment() -> None:
    with pytest.raises(AmbiguousTransaction) as raised:
        parse_transaction("Paguei 300 para o fornecedor")
    assert raised.value.amount_cents == 30000
    assert raised.value.description == "paguei para o fornecedor"
    assert raised.value.candidates == (EXPENSE, WITHDRAWAL)


def test_ambiguous_message_asks_and_records_nothing() -> None:
    assert say("p1", "Paguei 300 para o fornecedor") == QUESTION
    assert transactions() == []
    [pending] = pending_entries()
    assert (pending.status, pending.amount_cents, pending.candidate_types, pending.origin) == (
        "open",
        30000,
        ["expense", "withdrawal"],
        "rules",
    )


@pytest.mark.parametrize(
    ("answer", "expected_type", "label"),
    [
        ("1", "expense", "Despesa"),
        ("despesa", "expense", "Despesa"),
        ("É do negócio", "expense", "Despesa"),
        ("2", "withdrawal", "Retirada pessoal"),
        ("Retirada pessoal", "withdrawal", "Retirada pessoal"),
        ("pra mim", "withdrawal", "Retirada pessoal"),
    ],
)
def test_answer_records_transaction_with_original_message(
    answer: str, expected_type: str, label: str
) -> None:
    say("p1", "Paguei 300 para o fornecedor", timestamp="1795032000")
    reply = say("p2", answer, timestamp="1795032100")

    assert reply == f"{label} registrada: R$ 300,00. Responda 'desfazer' para corrigir."
    [transaction] = transactions()
    assert (transaction.transaction_type, transaction.amount_cents) == (expected_type, 30000)
    with TestingSession() as session:
        stored = session.get(Transaction, transaction.id)
        assert stored.source_message.external_id == "p1"
        assert stored.source_message.raw_text == "Paguei 300 para o fornecedor"
        # Recorded when the payment was reported, not when it was confirmed.
        occurred_at = stored.occurred_at
        if occurred_at.tzinfo is None:  # SQLite drops the offset; values are stored in UTC
            occurred_at = occurred_at.replace(tzinfo=UTC)
        assert int(occurred_at.timestamp()) == 1795032000
        [event] = session.query(TransactionEvent).all()
        assert event.changes["confirmed_by_message_id"] == "p2"
        assert event.changes["origin"] == "rules"
    [pending] = pending_entries()
    assert (pending.status, pending.resolution_message_id, pending.transaction_id) == (
        "resolved",
        "p2",
        transaction.id,
    )


def test_cancel_records_nothing() -> None:
    say("p1", "Paguei 300 para o fornecedor")
    assert say("p2", "cancelar") == "Tudo bem, não anotei esse lançamento."
    assert transactions() == []
    assert pending_entries()[0].status == "cancelled"


def test_unclear_answer_repeats_the_question() -> None:
    say("p1", "Paguei 300 para o fornecedor")
    reply = say("p2", "hmm não sei")
    assert reply == "Não entendi a resposta. " + QUESTION
    assert transactions() == []
    assert pending_entries()[0].status == "open"


def test_other_transaction_while_waiting_is_recorded_and_question_stays_open() -> None:
    say("p1", "Paguei 300 para o fornecedor")
    reply = say("p2", "Recebi 150 por um corte")
    assert reply.startswith("Entrada registrada: R$ 150,00")
    assert pending_entries()[0].status == "open"
    say("p3", "1")
    assert sorted(t.amount_cents for t in transactions()) == [15000, 30000]


def test_new_ambiguous_message_replaces_the_open_question() -> None:
    say("p1", "Paguei 300 para o fornecedor")
    reply = say("p2", "Paguei 80 para o Pedro")
    assert "R$ 80,00" in reply
    say("p3", "2")
    [transaction] = transactions()
    assert (transaction.amount_cents, transaction.transaction_type) == (8000, "withdrawal")
    assert [p.status for p in pending_entries()] == ["replaced", "resolved"]


def test_answer_after_expiry_records_nothing_and_says_so() -> None:
    get_settings().pending_entry_ttl_hours = 24
    post_webhook(whatsapp_payload("p1", "Paguei 300 para o fornecedor"))
    queue = worker().queue
    now = utc_now()
    services = {"sender": app.state.whatsapp_sender, "max_attempts": 8}
    queue.run_due(JobContext(now=now, services=services))
    queue.run_due(JobContext(now=now, services=services))

    post_webhook(whatsapp_payload("p2", "1", timestamp="1795118500"))
    later = now + timedelta(hours=24, seconds=1)
    queue.run_due(JobContext(now=later, services=services))
    queue.run_due(JobContext(now=later, services=services))

    assert sent_texts()[-1].startswith("Essa pergunta já expirou")
    assert transactions() == []
    assert pending_entries()[0].status == "expired"


def test_answer_just_before_expiry_is_accepted() -> None:
    post_webhook(whatsapp_payload("p1", "Paguei 300 para o fornecedor"))
    queue = worker().queue
    now = utc_now()
    services = {"sender": app.state.whatsapp_sender, "max_attempts": 8}
    queue.run_due(JobContext(now=now, services=services))
    post_webhook(whatsapp_payload("p2", "1", timestamp="1795118300"))
    almost = now + timedelta(hours=23, minutes=59)
    for _ in range(3):
        queue.run_due(JobContext(now=almost, services=services))
    assert [t.amount_cents for t in transactions()] == [30000]


def test_redelivered_answer_records_once() -> None:
    say("p1", "Paguei 300 para o fornecedor")
    answer = whatsapp_payload("p2", "1", timestamp="1795032100")
    signed_webhook(answer)
    signed_webhook(answer)
    assert len(transactions()) == 1
    assert len(sent_texts()) == 2


def test_stray_number_without_question_is_not_recorded() -> None:
    say("p1", "1")
    assert transactions() == []


def test_undo_after_clarification_reverses_the_confirmed_transaction() -> None:
    say("p1", "Paguei 300 para o fornecedor", timestamp="1795032000")
    say("p2", "1", timestamp="1795032100")
    assert say("p3", "desfazer", timestamp="1795032200") == "Lançamento de R$ 300,00 desfeito."


def test_internal_api_still_rejects_ambiguous_messages() -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message_id": "api-amb",
            "whatsapp_number": "+5511999999999",
            "text": "Paguei 300 para o fornecedor",
        },
    )
    assert response.status_code == 422
    assert pending_entries() == []


@pytest.mark.parametrize(
    ("text", "candidates", "expected"),
    [
        ("sim", (EXPENSE,), EXPENSE),
        ("sim", (EXPENSE, WITHDRAWAL), None),  # "yes" does not pick between two options
        ("3", (EXPENSE, WITHDRAWAL), None),
        ("Não", (EXPENSE,), CANCEL),
        ("retirada", (EXPENSE,), None),  # not an option that was offered
        ("Despesa!", (EXPENSE, WITHDRAWAL), EXPENSE),
    ],
)
def test_parse_answer(text: str, candidates: tuple, expected) -> None:
    assert parse_answer(text, candidates) == expected


def test_database_allows_only_one_open_question_per_business() -> None:
    from datetime import datetime

    from sqlalchemy.exc import IntegrityError

    from lucromei.models import Business

    now = datetime.now(UTC)
    with TestingSession() as session:
        business = session.query(Business).one()
        for index in range(2):
            session.add(
                PendingEntry(
                    business_id=business.id,
                    source_message_id=f"dup-{index}",
                    source_text="x",
                    amount_cents=100,
                    description="x",
                    occurred_at=now,
                    candidate_types=["expense", "withdrawal"],
                    status="open",
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

import hmac
import json
from collections.abc import Iterator
from hashlib import sha256

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lucromei.config import get_settings
from lucromei.db import get_session
from lucromei.main import app
from lucromei.models import (
    Base,
    CommandReceipt,
    InboundMessage,
    Transaction,
    TransactionEvent,
)
from lucromei.whatsapp import MockOutboundSender

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)


def override_session() -> Iterator[Session]:
    with TestingSession() as session:
        yield session


app.dependency_overrides[get_session] = override_session
client = TestClient(app)


def setup_function() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    get_settings().whatsapp_verify_token = "verify-test-token"
    get_settings().whatsapp_app_secret = "test-app-secret"
    app.state.whatsapp_sender = MockOutboundSender()


def create_transaction(message_id: str, text: str) -> dict:
    response = client.post(
        "/v1/messages",
        json={
            "message_id": message_id,
            "whatsapp_number": "+5511999999999",
            "text": text,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_health() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_process_message_is_database_idempotent() -> None:
    first = create_transaction("wamid.123", "Recebi 150 por um corte")
    second = create_transaction("wamid.123", "Recebi 999 por outro corte")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["transaction_id"] == first["transaction_id"]
    assert second["amount_cents"] == 15000
    with TestingSession() as session:
        assert session.query(InboundMessage).count() == 1
        assert session.query(Transaction).count() == 1


def test_ambiguous_message_does_not_write_to_ledger() -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message_id": "wamid.ambiguous",
            "whatsapp_number": "+5511999999999",
            "text": "Paguei 50 para João",
        },
    )
    assert response.status_code == 422
    with TestingSession() as session:
        assert session.query(InboundMessage).count() == 0


def test_correction_appends_event_without_overwriting_original() -> None:
    created = create_transaction("wamid.correct", "Gastei 80 em produtos")
    response = client.post(
        f"/v1/transactions/{created['transaction_id']}/corrections",
        json={"amount_cents": 7500, "reason": "Valor digitado incorretamente"},
    )
    assert response.status_code == 200
    assert response.json()["amount_cents"] == 7500
    with TestingSession() as session:
        stored = session.get(Transaction, created["transaction_id"])
        assert stored.amount_cents == 8000
        events = session.query(TransactionEvent).order_by(TransactionEvent.created_at).all()
        assert [event.event_type for event in events] == ["created", "corrected"]


def test_reversal_excludes_transaction_from_summary() -> None:
    income = create_transaction("wamid.income", "Recebi 500 por cortes")
    create_transaction("wamid.expense", "Gastei 125 em produtos")
    params = {"whatsapp_number": "+5511999999999", "year": 2026, "month": 11}
    before = client.get("/v1/summaries/monthly", params=params)
    assert before.json()["operating_balance_cents"] == 37500
    reversed_response = client.post(
        f"/v1/transactions/{income['transaction_id']}/reversal",
        json={"reason": "Lançamento duplicado"},
    )
    assert reversed_response.json()["reversed"] is True
    after = client.get("/v1/summaries/monthly", params=params).json()
    assert after["income_cents"] == 0
    assert after["expense_cents"] == 12500
    assert after["operating_balance_cents"] == -12500


def test_summary_separates_personal_withdrawals() -> None:
    create_transaction("wamid.income", "Recebi 500 por cortes")
    create_transaction("wamid.expense", "Gastei 125 em produtos")
    create_transaction("wamid.withdrawal", "Retirei 100 para mim")
    response = client.get(
        "/v1/summaries/monthly",
        params={"whatsapp_number": "+5511999999999", "year": 2026, "month": 11},
    )
    body = response.json()
    assert body["income_cents"] == 50000
    assert body["expense_cents"] == 12500
    assert body["withdrawal_cents"] == 10000
    assert body["operating_balance_cents"] == 27500
    assert "não representa automaticamente" in body["disclaimer"]


def whatsapp_payload(message_id: str, text: str, sender: str = "5511999999999") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "business-account-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1795032000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def signed_webhook(payload: dict, secret: str = "test-app-secret"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )


def test_whatsapp_webhook_verification() -> None:
    response = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-test-token",
            "hub.challenge": "challenge-123",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge-123"


def test_whatsapp_webhook_rejects_invalid_signature_without_writing() -> None:
    response = signed_webhook(whatsapp_payload("wamid.invalid", "Recebi 100"), "wrong")
    assert response.status_code == 401
    with TestingSession() as session:
        assert session.query(InboundMessage).count() == 0


def test_whatsapp_webhook_normalizes_processes_and_replies() -> None:
    response = signed_webhook(whatsapp_payload("wamid.webhook", "Recebi 150 por um corte"))
    assert response.status_code == 200
    assert response.json() == {
        "received": True,
        "processed": 1,
        "commands": 0,
        "duplicates": 0,
        "clarification_required": 0,
        "ignored": 0,
    }
    sent = app.state.whatsapp_sender.sent_messages
    assert len(sent) == 1
    assert sent[0].recipient == "5511999999999"
    assert "Entrada registrada: R$ 150,00" in sent[0].text


def test_whatsapp_duplicate_delivery_does_not_duplicate_transaction() -> None:
    payload = whatsapp_payload("wamid.duplicate", "Gastei 80 em produtos")
    first = signed_webhook(payload)
    second = signed_webhook(payload)
    assert first.json()["duplicates"] == 0
    assert second.json()["duplicates"] == 1
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1


def test_whatsapp_ambiguous_message_requests_clarification() -> None:
    response = signed_webhook(whatsapp_payload("wamid.ambiguous", "Paguei 50 para João"))
    assert response.json()["clarification_required"] == 1
    assert "Não entendi" in app.state.whatsapp_sender.sent_messages[0].text


def test_whatsapp_undo_is_idempotent_across_duplicate_deliveries() -> None:
    first = create_transaction("wamid.first", "Recebi 100 por um corte")
    second = create_transaction("wamid.second", "Gastei 20 em produtos")
    payload = whatsapp_payload("wamid.undo", "desfazer")

    initial = signed_webhook(payload)
    duplicate = signed_webhook(payload)

    assert initial.json()["commands"] == 1
    assert initial.json()["duplicates"] == 0
    assert duplicate.json()["commands"] == 1
    assert duplicate.json()["duplicates"] == 1
    with TestingSession() as session:
        events = session.query(TransactionEvent).filter_by(event_type="reversed").all()
        assert len(events) == 1
        assert events[0].transaction_id == second["transaction_id"]
        assert events[0].transaction_id != first["transaction_id"]
        assert session.query(CommandReceipt).count() == 1


def test_whatsapp_correction_preserves_original_transaction() -> None:
    created = create_transaction("wamid.original", "Gastei 80 em produtos")

    response = signed_webhook(
        whatsapp_payload("wamid.correction", "corrigir última despesa para R$ 75,50")
    )

    assert response.json()["commands"] == 1
    assert "R$ 75,50" in app.state.whatsapp_sender.sent_messages[-1].text
    with TestingSession() as session:
        stored = session.get(Transaction, created["transaction_id"])
        assert stored.amount_cents == 8000
        corrected = session.query(TransactionEvent).filter_by(event_type="corrected").one()
        assert corrected.changes["amount_cents"] == 7550


def test_typed_correction_targets_latest_matching_transaction() -> None:
    expense = create_transaction("wamid.expense", "Gastei 80 em produtos")
    income = create_transaction("wamid.income", "Recebi 150 por um corte")

    signed_webhook(whatsapp_payload("wamid.fix-expense", "corrigir despesa para 70"))

    with TestingSession() as session:
        corrected = session.query(TransactionEvent).filter_by(event_type="corrected").one()
        assert corrected.transaction_id == expense["transaction_id"]
        assert corrected.transaction_id != income["transaction_id"]


def test_whatsapp_monthly_summary_reply_uses_effective_ledger() -> None:
    create_transaction("wamid.summary-income", "Recebi 500 por cortes")
    create_transaction("wamid.summary-expense", "Gastei 125 em produtos")
    create_transaction("wamid.summary-withdrawal", "Retirei 100 para mim")

    response = signed_webhook(whatsapp_payload("wamid.summary", "Quanto lucrei este mês?"))

    assert response.json()["commands"] == 1
    reply = app.state.whatsapp_sender.sent_messages[-1].text
    assert "entradas R$ 500,00" in reply
    assert "despesas R$ 125,00" in reply
    assert "retiradas R$ 100,00" in reply
    assert "Saldo: R$ 275,00" in reply


def test_command_without_applicable_transaction_is_safely_replayed() -> None:
    payload = whatsapp_payload("wamid.empty-undo", "desfazer")

    signed_webhook(payload)
    signed_webhook(payload)

    sent = app.state.whatsapp_sender.sent_messages
    assert sent[-2].text == "Não encontrei um lançamento ativo para desfazer."
    assert sent[-1].text == sent[-2].text
    with TestingSession() as session:
        assert session.query(CommandReceipt).count() == 1
        assert session.query(TransactionEvent).count() == 0

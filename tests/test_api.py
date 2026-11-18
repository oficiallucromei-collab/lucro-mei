from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lucromei.db import get_session
from lucromei.main import app
from lucromei.models import Base, InboundMessage, Transaction, TransactionEvent

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

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from support import (
    TestingSession,
    client,
    onboard,
    reset_state,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.config import get_settings
from lucromei.main import app
from lucromei.models import (
    CommandReceipt,
    InboundMessage,
    Transaction,
    TransactionEvent,
)

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def current_month_params() -> dict:
    now = datetime.now(SAO_PAULO)
    return {"whatsapp_number": "+5511999999999", "year": now.year, "month": now.month}


def setup_function() -> None:
    reset_state()
    onboard()


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
    params = current_month_params()
    before = client.get("/v1/summaries/monthly", params=params)
    assert before.json()["operating_result_cents"] == 37500
    reversed_response = client.post(
        f"/v1/transactions/{income['transaction_id']}/reversal",
        json={"reason": "Lançamento duplicado"},
    )
    assert reversed_response.json()["reversed"] is True
    after = client.get("/v1/summaries/monthly", params=params).json()
    assert after["income_cents"] == 0
    assert after["expense_cents"] == 12500
    assert after["operating_result_cents"] == -12500


def test_summary_separates_personal_withdrawals() -> None:
    create_transaction("wamid.income", "Recebi 500 por cortes")
    create_transaction("wamid.expense", "Gastei 125 em produtos")
    create_transaction("wamid.withdrawal", "Retirei 100 para mim")
    response = client.get("/v1/summaries/monthly", params=current_month_params())
    body = response.json()
    assert body["income_cents"] == 50000
    assert body["expense_cents"] == 12500
    assert body["withdrawal_cents"] == 10000
    assert body["operating_result_cents"] == 37500
    assert body["net_after_withdrawals_cents"] == 27500
    assert "não representam" in body["disclaimer"]


def test_month_boundaries_follow_sao_paulo_time() -> None:
    created = create_transaction("wamid.late-night", "Recebi 150 por um corte")
    # 01:30 UTC on 1 December is 22:30 on 30 November in São Paulo.
    late_night = datetime(2026, 12, 1, 1, 30, tzinfo=UTC)
    client.post(
        f"/v1/transactions/{created['transaction_id']}/corrections",
        json={"occurred_at": late_night.isoformat(), "reason": "Data do atendimento"},
    )
    params = {"whatsapp_number": "+5511999999999", "year": 2026}

    november = client.get("/v1/summaries/monthly", params={**params, "month": 11}).json()
    december = client.get("/v1/summaries/monthly", params={**params, "month": 12}).json()

    assert november["income_cents"] == 15000
    assert december["income_cents"] == 0
    assert november["period_start"].startswith("2026-11-01T00:00:00-03:00")


def test_internal_api_is_disabled_without_configured_key() -> None:
    get_settings().internal_api_key = ""
    response = client.get("/v1/summaries/monthly", params=current_month_params())
    assert response.status_code == 404


def test_internal_api_rejects_missing_or_wrong_key() -> None:
    anonymous = TestClient(app)
    wrong_key = TestClient(app, headers={"X-Internal-Api-Key": "wrong"})
    assert anonymous.get("/v1/summaries/monthly", params=current_month_params()).status_code == 401
    response = wrong_key.post(
        "/v1/messages",
        json={"message_id": "wamid.x", "whatsapp_number": "+5511999999999", "text": "Recebi 10"},
    )
    assert response.status_code == 401
    with TestingSession() as session:
        assert session.query(InboundMessage).count() == 0


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


def test_whatsapp_webhook_verification_rejects_placeholder_token() -> None:
    get_settings().whatsapp_verify_token = "change-me"
    response = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "change-me",
            "hub.challenge": "challenge-123",
        },
    )
    assert response.status_code == 403


def test_whatsapp_webhook_rejects_invalid_signature_without_writing() -> None:
    response = signed_webhook(whatsapp_payload("wamid.invalid", "Recebi 100"), "wrong")
    assert response.status_code == 401
    with TestingSession() as session:
        assert session.query(InboundMessage).count() == 0


def test_whatsapp_webhook_normalizes_processes_and_replies() -> None:
    response = signed_webhook(whatsapp_payload("wamid.webhook", "Recebi 150 por um corte"))
    assert response.status_code == 200
    assert response.json() == {"received": True, "accepted": 1, "duplicates": 0, "statuses": 0}
    sent = app.state.whatsapp_sender.sent_messages
    assert len(sent) == 1
    assert sent[0].recipient == "5511999999999"
    assert "Entrada registrada: R$ 150,00" in sent[0].text


def test_whatsapp_duplicate_delivery_does_not_duplicate_transaction() -> None:
    payload = whatsapp_payload("wamid.duplicate", "Gastei 80 em produtos")
    first = signed_webhook(payload)
    second = signed_webhook(payload)
    assert first.json()["accepted"] == 1
    assert second.json()["duplicates"] == 1
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1
    sent = app.state.whatsapp_sender.sent_messages
    assert len(sent) == 1
    assert "Despesa registrada: R$ 80,00" in sent[0].text


def test_whatsapp_duplicate_delivery_never_sends_already_registered_notice() -> None:
    payload = whatsapp_payload("wamid.triple", "Recebi 150 por um corte")
    for _ in range(3):
        assert signed_webhook(payload).status_code == 200
    texts = [message.text for message in app.state.whatsapp_sender.sent_messages]
    assert texts == ["Entrada registrada: R$ 150,00. Responda 'desfazer' para corrigir."]
    assert not any("já foi registrada" in text for text in texts)


def test_whatsapp_ambiguous_message_requests_clarification() -> None:
    response = signed_webhook(whatsapp_payload("wamid.ambiguous", "Paguei 50 para João"))
    assert response.status_code == 200
    reply = app.state.whatsapp_sender.sent_messages[0].text
    assert "despesa do negócio ou retirada pessoal?" in reply
    with TestingSession() as session:
        assert session.query(Transaction).count() == 0


def test_whatsapp_undo_is_idempotent_across_duplicate_deliveries() -> None:
    first = create_transaction("wamid.first", "Recebi 100 por um corte")
    second = create_transaction("wamid.second", "Gastei 20 em produtos")
    payload = whatsapp_payload("wamid.undo", "desfazer")

    initial = signed_webhook(payload)
    duplicate = signed_webhook(payload)

    assert initial.json()["accepted"] == 1
    assert duplicate.json() == {"received": True, "accepted": 0, "duplicates": 1, "statuses": 0}
    with TestingSession() as session:
        events = session.query(TransactionEvent).filter_by(event_type="reversed").all()
        assert len(events) == 1
        assert events[0].transaction_id == second["transaction_id"]
        assert events[0].transaction_id != first["transaction_id"]
        assert session.query(CommandReceipt).count() == 1
    assert len(app.state.whatsapp_sender.sent_messages) == 1


def test_whatsapp_correction_preserves_original_transaction() -> None:
    created = create_transaction("wamid.original", "Gastei 80 em produtos")

    response = signed_webhook(
        whatsapp_payload("wamid.correction", "corrigir última despesa para R$ 75,50")
    )

    assert response.status_code == 200
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

    assert response.status_code == 200
    reply = app.state.whatsapp_sender.sent_messages[-1].text
    assert "Entradas: R$ 500,00" in reply
    assert "Despesas do negócio: R$ 125,00" in reply
    assert "Resultado do negócio: R$ 375,00" in reply
    assert "Retiradas pessoais: R$ 100,00" in reply
    assert "Sobra após retiradas: R$ 275,00" in reply
    assert "não são automaticamente" in reply


def test_whatsapp_decimal_dot_amount_is_not_multiplied() -> None:
    signed_webhook(whatsapp_payload("wamid.coffee", "Gastei 12.50 em café"))
    with TestingSession() as session:
        assert session.query(Transaction).one().amount_cents == 1250


def test_whatsapp_quantity_and_total_records_total() -> None:
    signed_webhook(whatsapp_payload("wamid.cuts", "Fiz 3 cortes por 90 reais"))
    with TestingSession() as session:
        assert session.query(Transaction).one().amount_cents == 9000


def test_correction_with_thousands_separator() -> None:
    create_transaction("wamid.big", "Recebi 150 por um serviço")
    signed_webhook(whatsapp_payload("wamid.fix-big", "corrigir para 1.500"))
    assert "R$ 1.500,00" in app.state.whatsapp_sender.sent_messages[-1].text
    with TestingSession() as session:
        corrected = session.query(TransactionEvent).filter_by(event_type="corrected").one()
        assert corrected.changes["amount_cents"] == 150000


def test_command_without_applicable_transaction_is_safely_replayed() -> None:
    payload = whatsapp_payload("wamid.empty-undo", "desfazer")

    signed_webhook(payload)
    signed_webhook(payload)

    sent = app.state.whatsapp_sender.sent_messages
    assert [message.text for message in sent] == [
        "Não encontrei um lançamento ativo para desfazer."
    ]
    with TestingSession() as session:
        assert session.query(CommandReceipt).count() == 1
        assert session.query(TransactionEvent).count() == 0


def test_openapi_schema_and_docs_only_exist_locally() -> None:
    from lucromei.main import openapi_url_for

    assert openapi_url_for("local") == "/openapi.json"
    assert openapi_url_for(" Local ") == "/openapi.json"
    assert openapi_url_for("production") is None
    assert openapi_url_for("staging") is None


def test_plain_postgresql_urls_use_psycopg3() -> None:
    from lucromei.config import Settings, normalize_database_url

    assert normalize_database_url("postgresql://u:p@h/db?sslmode=require") == (
        "postgresql+psycopg://u:p@h/db?sslmode=require"
    )
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
    settings = Settings(_env_file=None, database_url="postgresql://u:p@h/db")
    assert settings.database_url == "postgresql+psycopg://u:p@h/db"

from fastapi.testclient import TestClient

import lucromei.main as main_module
from lucromei.main import app
from lucromei.service import InMemoryLedger

client = TestClient(app)


def setup_function() -> None:
    main_module.ledger = InMemoryLedger()


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_process_message_is_idempotent() -> None:
    payload = {
        "message_id": "wamid.123",
        "whatsapp_number": "+5511999999999",
        "text": "Recebi 150 por um corte",
    }
    first = client.post("/v1/messages", json=payload)
    second = client.post("/v1/messages", json=payload)

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    assert first.json()["amount_cents"] == 15000
    assert second.status_code == 201
    assert second.json()["duplicate"] is True


def test_ambiguous_message_requires_clarification() -> None:
    response = client.post(
        "/v1/messages",
        json={
            "message_id": "wamid.ambiguous",
            "whatsapp_number": "+5511999999999",
            "text": "Paguei 50 para João",
        },
    )
    assert response.status_code == 422
    assert "Não ficou claro" in response.json()["detail"]

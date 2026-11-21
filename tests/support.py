"""Shared test setup: database, API client, signed WhatsApp webhooks and the job worker."""

import hmac
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lucromei import main as main_module
from lucromei.config import get_settings
from lucromei.db import get_session
from lucromei.main import app
from lucromei.models import Base, Business, TermsAcceptance
from lucromei.service import normalize_whatsapp_number
from lucromei.whatsapp import MockOutboundSender
from lucromei.worker import Worker

# CI sets LUCROMEI_TEST_DATABASE_URL to a real PostgreSQL; locally an in-memory SQLite is used.
TEST_DATABASE_URL = os.environ.get("LUCROMEI_TEST_DATABASE_URL")
if TEST_DATABASE_URL:
    engine = create_engine(TEST_DATABASE_URL)
else:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
IS_POSTGRES = engine.dialect.name == "postgresql"
requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES, reason="needs PostgreSQL row locking (set LUCROMEI_TEST_DATABASE_URL)"
)


def override_session() -> Iterator[Session]:
    with TestingSession() as session:
        yield session


app.dependency_overrides[get_session] = override_session
INTERNAL_API_KEY = "test-internal-key"
client = TestClient(app, headers={"X-Internal-Api-Key": INTERNAL_API_KEY})
DEFAULT_SENDER = "5511999999999"


def reset_state() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    settings = get_settings()
    settings.whatsapp_verify_token = "verify-test-token"
    settings.whatsapp_app_secret = "test-app-secret"
    settings.internal_api_key = INTERNAL_API_KEY
    settings.business_timezone = "America/Sao_Paulo"
    settings.worker_mode = "off"
    settings.job_max_attempts = 8
    settings.terms_version = "2026-11-21"
    settings.terms_url = "https://lucromei.example/termos"
    settings.contact_email = ""
    settings.trial_days = 14
    settings.ai_api_key = ""  # tests never call a real model
    settings.ai_daily_calls_per_business = 30
    settings.ai_max_input_chars = 300
    settings.message_text_retention_days = 90
    settings.deletion_grace_days = 7
    settings.deletion_confirm_minutes = 10
    settings.privacy_hash_key = "test-privacy-key-0123456789abcdef"
    app.state.whatsapp_sender = MockOutboundSender()
    app.state.worker = None
    main_module._limiters = None


def onboard(number: str = DEFAULT_SENDER, *, name: str = "Ana") -> str:
    """Create a business that already finished onboarding. Returns its ID."""
    now = datetime.now(UTC)
    with TestingSession() as session:
        business = Business(
            whatsapp_number=normalize_whatsapp_number(number),
            display_name=name,
            business_type="barbearia",
            onboarding_step="active",
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=14),
        )
        session.add(business)
        session.flush()
        session.add(
            TermsAcceptance(
                business_id=business.id,
                terms_version="2026-11-21",
                accepted_at=now,
                source_message_id=f"onboarding-{business.id}",
                accepted_text="aceito",
            )
        )
        session.commit()
        return business.id


def worker(sender=None) -> Worker:
    return Worker(TestingSession, sender or app.state.whatsapp_sender, get_settings())


def drain_jobs(sender=None) -> int:
    return worker(sender).drain()


def whatsapp_payload(
    message_id: str,
    text: str | None,
    sender: str = DEFAULT_SENDER,
    *,
    timestamp: str = "1795032000",
    message_type: str = "text",
) -> dict:
    message = {"from": sender, "id": message_id, "timestamp": timestamp, "type": message_type}
    if message_type == "text":
        message["text"] = {"body": text}
    return webhook_envelope({"messaging_product": "whatsapp", "messages": [message]})


def webhook_envelope(value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "business-account-id",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }


def post_webhook(payload: dict, secret: str = "test-app-secret"):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"content-type": "application/json", "X-Hub-Signature-256": signature},
    )


def signed_webhook(payload: dict, secret: str = "test-app-secret", *, process: bool = True):
    """Deliver a signed webhook, then run the background jobs it created."""
    response = post_webhook(payload, secret)
    if process and response.status_code == 200:
        drain_jobs()
    return response


def sent_texts() -> list[str]:
    return [message.text for message in app.state.whatsapp_sender.sent_messages]

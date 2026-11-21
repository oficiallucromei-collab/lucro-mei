"""Proactive notices outside the 24-hour window: templates, scheduling, opt-out."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from support import (
    TestingSession,
    onboard,
    post_webhook,
    reset_state,
    sent_texts,
    whatsapp_payload,
    worker,
)

from lucromei.config import get_settings
from lucromei.jobs import JobContext
from lucromei.main import app
from lucromei.models import Business, DeletionRequest, OutboundMessage, Subscription, Transaction
from lucromei.processing import handle_maintenance
from lucromei.templates import CATALOG, REMINDER, TRIAL_ENDING
from lucromei.whatsapp import CloudApiOutboundSender, WhatsAppSendError

SP = ZoneInfo("America/Sao_Paulo")
# 10:00 yesterday in São Paulo: inside the default 9h-20h window and already due for the
# queue, whose workers use the real clock.
NOW = (
    (datetime.now(SP) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
).astimezone(UTC)


def label(value: datetime) -> str:
    return value.astimezone(SP).strftime("%d/%m/%Y")


def setup_function() -> None:
    reset_state()
    settings = get_settings()
    settings.notifications_enabled = True
    settings.reminders_enabled = False
    settings.reminder_inactive_days = 7
    settings.notification_start_hour = 9
    settings.notification_end_hour = 20
    settings.trial_enforcement_enabled = True


def teardown_function() -> None:
    get_settings().notifications_enabled = False
    get_settings().reminders_enabled = False


def business(trial_ends_at: datetime, *, started_days_ago: int = 14) -> str:
    business_id = onboard(name="Ana Souza")
    with TestingSession() as session:
        row = session.get(Business, business_id)
        row.trial_ends_at = trial_ends_at
        row.trial_started_at = NOW - timedelta(days=started_days_ago)
        session.commit()
    return business_id


def drain_jobs(sender=None) -> None:
    """Run queued jobs without the worker's own maintenance, which uses the real clock and
    would send notices depending on the time of day the tests run."""
    runner = worker(sender)
    runner.schedule_maintenance = lambda now: None
    runner.drain()


def signed_webhook(payload: dict) -> None:
    assert post_webhook(payload).status_code == 200
    drain_jobs()


def maintenance(now: datetime = NOW) -> None:
    with TestingSession() as session:
        handle_maintenance(session, {}, JobContext(now=now, services={"max_attempts": 8}))
        session.commit()
    drain_jobs()


def outbound() -> list[OutboundMessage]:
    with TestingSession() as session:
        return session.scalars(select(OutboundMessage).order_by(OutboundMessage.created_at)).all()


def sent():
    return app.state.whatsapp_sender.sent_messages


def test_trial_ending_uses_the_template_outside_the_window_and_is_sent_once() -> None:
    business(NOW + timedelta(hours=30))
    maintenance()
    maintenance(NOW + timedelta(hours=1))
    [message] = sent()
    assert message.template == "lucromei_teste_terminando"
    end = label(NOW + timedelta(hours=30))
    assert message.parameters == ["Ana", end]
    [row] = outbound()
    assert row.template_name == "lucromei_teste_terminando"
    assert row.text == TRIAL_ENDING.render(["Ana", end])
    assert row.status == "sent"


def test_trial_ending_is_plain_text_inside_the_window() -> None:
    business(NOW + timedelta(hours=30))
    signed_webhook(
        whatsapp_payload(
            "m1", "resumo do mes", timestamp=str(int((NOW - timedelta(hours=2)).timestamp()))
        )
    )
    maintenance()
    message = sent()[-1]
    assert message.template is None
    assert message.text.startswith(
        f"Olá, Ana! Seu teste grátis do LucroMEI termina em {label(NOW + timedelta(hours=30))}."
    )


def test_trial_ended_notice_and_nothing_for_paying_or_erasing_accounts() -> None:
    business(NOW - timedelta(hours=5))
    maintenance()
    assert [m.template for m in sent()] == ["lucromei_teste_encerrado"]
    reset_state()
    paid = business(NOW + timedelta(hours=30))
    with TestingSession() as session:
        session.add(
            Subscription(
                business_id=paid,
                status="active",
                provider="manual",
                price_cents=2490,
                current_period_end=NOW + timedelta(days=30),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    erasing = onboard("5511888888888")
    with TestingSession() as session:
        session.get(Business, erasing).trial_ends_at = NOW + timedelta(hours=30)
        session.add(
            DeletionRequest(
                business_id=erasing,
                subject_digest="y",
                status="scheduled",
                requested_at=NOW,
                confirm_by=NOW,
                scheduled_for=NOW + timedelta(days=7),
            )
        )
        session.commit()
    maintenance()
    assert sent() == []


@pytest.mark.parametrize("hour", [8, 20, 23])
def test_no_notices_outside_local_hours(hour: int) -> None:
    business(NOW + timedelta(hours=30))
    maintenance(NOW.astimezone(SP).replace(hour=hour, minute=30).astimezone(UTC))
    assert sent() == []


def test_disabled_by_default() -> None:
    get_settings().notifications_enabled = False
    business(NOW + timedelta(hours=30))
    maintenance()
    assert sent() == []


def test_reminder_after_inactivity_once_per_stretch_and_opt_out() -> None:
    get_settings().reminders_enabled = True
    business_id = business(NOW + timedelta(days=20))
    with TestingSession() as session:
        session.query(Business).update({"trial_started_at": NOW - timedelta(days=9)})
        session.commit()
    maintenance()
    maintenance(NOW + timedelta(hours=2))
    [message] = sent()
    assert message.template == REMINDER.name
    assert message.parameters == ["Ana", "9"]
    # The user records something: the next reminder only comes after a new idle stretch.
    with TestingSession() as session:
        session.query(Transaction).delete()
        session.commit()
    signed_webhook(
        whatsapp_payload("m1", "Recebi 10 por um corte", timestamp=str(int(NOW.timestamp())))
    )
    maintenance(NOW + timedelta(days=3))
    assert len([m for m in sent() if m.template == REMINDER.name]) == 1
    # Opt-out
    signed_webhook(whatsapp_payload("m2", "PARAR LEMBRETES", timestamp=str(int(NOW.timestamp()))))
    assert sent_texts()[-1].startswith("Pronto, não vou mais mandar lembretes.")
    with TestingSession() as session:
        assert session.get(Business, business_id).reminders_opt_out_at is not None
    maintenance(NOW + timedelta(days=9))
    assert len([m for m in sent() if m.template == REMINDER.name]) == 1
    signed_webhook(whatsapp_payload("m3", "voltar lembretes", timestamp=str(int(NOW.timestamp()))))
    assert sent_texts()[-1] == "Combinado, os lembretes estão ligados de novo."


def test_cloud_api_template_request_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.t1"}]})

    sender = CloudApiOutboundSender(
        access_token="t",
        phone_number_id="123",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert (
        sender.send_template("5511999999999", "lucromei_teste_terminando", "pt_BR", ["Ana\nX", ""])
        == "wamid.t1"
    )
    body = json.loads(requests[0].content)
    assert body["type"] == "template"
    assert body["template"] == {
        "name": "lucromei_teste_terminando",
        "language": {"code": "pt_BR"},
        "components": [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": "Ana X"}, {"type": "text", "text": "-"}],
            }
        ],
    }


def test_rejected_template_fails_permanently() -> None:
    class RejectingSender:
        sent_messages: list = []

        def send_text(self, recipient, text):
            return "ok"

        def send_template(self, recipient, name, language, parameters):
            raise WhatsAppSendError("meta_132001", retryable=False)

    business(NOW + timedelta(hours=30))
    with TestingSession() as session:
        handle_maintenance(session, {}, JobContext(now=NOW, services={"max_attempts": 8}))
        session.commit()
    drain_jobs(RejectingSender())
    [row] = outbound()
    assert (row.status, row.error_code) == ("failed", "meta_132001")


def test_catalog_templates_have_matching_examples() -> None:
    for template in CATALOG.values():
        placeholders = sum(1 for index in range(1, 10) if "{{" + str(index) + "}}" in template.body)
        assert placeholders == len(template.examples)
        assert not template.body.startswith("{{") and not template.body.endswith("}}")
        assert template.category == "UTILITY" and template.language == "pt_BR"
        assert len(template.body) <= 1024

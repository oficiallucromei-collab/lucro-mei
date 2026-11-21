import json
from types import SimpleNamespace

import httpx
import pytest
from support import (
    TestingSession,
    drain_jobs,
    post_webhook,
    reset_state,
    webhook_envelope,
    whatsapp_payload,
)

from lucromei.main import app
from lucromei.models import DeliveryStatusEvent, OutboundMessage
from lucromei.processing import delivery_status
from lucromei.whatsapp import (
    CloudApiOutboundSender,
    MockOutboundSender,
    WhatsAppSendError,
    build_sender,
)

TOKEN = "EAAG-test-token-never-logged"


def setup_function() -> None:
    reset_state()


def sender_with(handler) -> tuple[CloudApiOutboundSender, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    sender = CloudApiOutboundSender(
        access_token=TOKEN, phone_number_id="1234567890", client=client
    )
    return sender, requests


def meta_error(status: int, code: int) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"message": "x", "type": "OAuthException", "code": code}}
    )


def test_send_text_posts_to_cloud_api_and_returns_message_id() -> None:
    sender, requests = sender_with(
        lambda request: httpx.Response(200, json={"messages": [{"id": "wamid.out-1"}]})
    )

    assert sender.send_text("5511999999999", "Olá!") == "wamid.out-1"

    [request] = requests
    assert str(request.url) == "https://graph.facebook.com/v25.0/1234567890/messages"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "5511999999999",
        "type": "text",
        "text": {"preview_url": False, "body": "Olá!"},
    }


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(500), "http_500", True),
        (httpx.Response(503, text="<html>down</html>"), "http_503", True),
        (httpx.Response(429), "http_429", True),
        (meta_error(429, 130429), "meta_130429", True),  # throughput limit
        (meta_error(400, 131056), "meta_131056", True),  # pair rate limit
        (meta_error(400, 131047), "meta_131047", False),  # 24-hour window closed
        (meta_error(400, 131026), "meta_131026", False),  # undeliverable number
        (meta_error(401, 190), "meta_190", False),  # expired or invalid token
        (meta_error(400, 100), "meta_100", False),  # invalid parameter
        (httpx.Response(403), "http_403", False),
    ],
)
def test_meta_errors_are_classified(response: httpx.Response, code: str, retryable: bool) -> None:
    sender, _ = sender_with(lambda request: response)
    with pytest.raises(WhatsAppSendError) as raised:
        sender.send_text("5511999999999", "Olá!")
    assert (raised.value.code, raised.value.retryable) == (code, retryable)


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "network_error"),
    ],
)
def test_network_failures_are_retryable(exception: Exception, code: str) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise exception

    sender, _ = sender_with(fail)
    with pytest.raises(WhatsAppSendError) as raised:
        sender.send_text("5511999999999", "Olá!")
    assert (raised.value.code, raised.value.retryable) == (code, True)


def test_accepted_message_without_id_is_not_treated_as_failure() -> None:
    sender, _ = sender_with(lambda request: httpx.Response(200, json={"unexpected": True}))
    assert sender.send_text("5511999999999", "Olá!") is None


def test_long_text_is_cut_to_whatsapp_limit() -> None:
    sender, requests = sender_with(
        lambda request: httpx.Response(200, json={"messages": [{"id": "wamid.long"}]})
    )
    sender.send_text("5511999999999", "a" * 5000)
    assert len(json.loads(requests[0].content)["text"]["body"]) == 4096


def test_token_is_never_shown_in_repr() -> None:
    sender, _ = sender_with(lambda request: httpx.Response(200))
    assert TOKEN not in repr(sender)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"access_token": "", "phone_number_id": "123"},
        {"access_token": TOKEN, "phone_number_id": "not-a-number"},
        {"access_token": TOKEN, "phone_number_id": "123", "graph_api_version": "../v1"},
    ],
)
def test_invalid_credentials_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        CloudApiOutboundSender(**kwargs)


def settings(**overrides) -> SimpleNamespace:
    values = {
        "environment": "local",
        "whatsapp_access_token": "",
        "whatsapp_phone_number_id": "",
        "whatsapp_graph_api_version": "v25.0",
        "whatsapp_timeout_seconds": 10.0,
    }
    return SimpleNamespace(**{**values, **overrides})


def test_build_sender_uses_cloud_api_only_with_credentials() -> None:
    assert isinstance(build_sender(settings()), MockOutboundSender)
    configured = build_sender(
        settings(whatsapp_access_token=TOKEN, whatsapp_phone_number_id="1234567890")
    )
    assert isinstance(configured, CloudApiOutboundSender)


def test_production_refuses_to_start_without_credentials() -> None:
    with pytest.raises(RuntimeError):
        build_sender(settings(environment="production"))


def test_worker_stores_whatsapp_message_id_and_retries_after_outage() -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"messages": [{"id": "wamid.reply-1"}]}),
        ]
    )
    sender, requests = sender_with(lambda request: next(responses))
    post_webhook(whatsapp_payload("wamid.in-1", "Recebi 150 por um corte"))

    from datetime import timedelta

    from support import worker

    from lucromei.config import get_settings
    from lucromei.jobs import JobContext, utc_now

    queue = worker(sender).queue
    now = utc_now()

    def context(at):
        return JobContext(
            now=at, services={"sender": sender, "max_attempts": get_settings().job_max_attempts}
        )

    queue.run_due(context(now))
    queue.run_due(context(now))
    queue.run_due(context(now + timedelta(seconds=10)))

    assert len(requests) == 2
    with TestingSession() as session:
        outbound = session.query(OutboundMessage).one()
        assert (outbound.status, outbound.provider_message_id) == ("sent", "wamid.reply-1")


def status_payload(*statuses: dict) -> dict:
    return webhook_envelope({"messaging_product": "whatsapp", "statuses": list(statuses)})


def status(message_id: str, value: str, timestamp: str = "1795032001", **extra) -> dict:
    return {
        "id": message_id,
        "status": value,
        "timestamp": timestamp,
        "recipient_id": "5511999999999",
        **extra,
    }


def test_delivery_statuses_are_recorded_once_and_most_advanced_wins() -> None:
    post_webhook(whatsapp_payload("wamid.in-2", "Recebi 150 por um corte"))
    drain_jobs()
    with TestingSession() as session:
        provider_id = session.query(OutboundMessage).one().provider_message_id

    first = post_webhook(status_payload(status(provider_id, "sent"), status(provider_id, "read")))
    again = post_webhook(status_payload(status(provider_id, "read")))
    post_webhook(status_payload(status(provider_id, "delivered")))  # arrives out of order

    assert first.json()["statuses"] == 2
    assert again.json()["statuses"] == 0
    with TestingSession() as session:
        assert session.query(DeliveryStatusEvent).count() == 3
        assert delivery_status(session, provider_id) == "read"
    assert len(app.state.whatsapp_sender.sent_messages) == 1  # statuses never trigger replies


def test_failed_delivery_keeps_meta_error_code_but_not_recipient() -> None:
    errors = [{"code": 131047, "title": "Re-engagement message"}]
    post_webhook(status_payload(status("wamid.out-x", "failed", errors=errors)))
    with TestingSession() as session:
        event = session.query(DeliveryStatusEvent).one()
        assert (event.status, event.error_code) == ("failed", "meta_131047")
        assert "5511999999999" not in json.dumps(
            {column.name: str(getattr(event, column.name)) for column in event.__table__.columns}
        )
        assert delivery_status(session, "wamid.out-x") == "failed"


def test_status_arriving_before_message_id_is_stored_is_kept() -> None:
    post_webhook(status_payload(status("wamid.not-yet-known", "sent")))
    with TestingSession() as session:
        assert delivery_status(session, "wamid.not-yet-known") == "sent"


def test_unknown_status_values_are_ignored() -> None:
    response = post_webhook(status_payload(status("wamid.y", "deleted"), {"status": "sent"}))
    assert response.json()["statuses"] == 0

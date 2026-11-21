import json
import logging
import sys

import pytest
from support import (
    client,
    drain_jobs,
    onboard,
    post_webhook,
    reset_state,
    whatsapp_payload,
)

from lucromei import observability
from lucromei.observability import JsonFormatter, scrub_event

SECRET_TEXT = "segredo-bolo"
AMOUNT = "98765"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


@pytest.fixture
def logs():
    capture = Capture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    yield capture
    root.removeHandler(capture)
    root.setLevel(previous)


def setup_function() -> None:
    reset_state()
    onboard()


def assert_private(lines: list[str]) -> None:
    joined = "\n".join(lines)
    for forbidden in (SECRET_TEXT, AMOUNT, "987,65", "R$", "5511999999999", "Ana"):
        assert forbidden not in joined, forbidden


def test_formatter_keeps_only_allowlisted_fields_and_drops_exception_messages() -> None:
    logger = logging.getLogger("test.privacy")
    record = logger.makeRecord(
        "test.privacy",
        logging.ERROR,
        __file__,
        1,
        "job_dead",
        (),
        None,
        extra={"job_id": "j1", "text": SECRET_TEXT, "amount_cents": 9876500},
    )
    try:
        raise ValueError(f"INSERT ... ('{SECRET_TEXT}', {AMOUNT})")
    except ValueError:
        record.exc_info = sys.exc_info()

    entry = json.loads(JsonFormatter().format(record))

    assert entry["event"] == "job_dead"
    assert entry["job_id"] == "j1"
    assert "text" not in entry and "amount_cents" not in entry
    assert entry["exc_type"] == "ValueError"
    assert entry["stack"] and all(".py:" in frame for frame in entry["stack"])
    assert_private([json.dumps(entry)])


def test_message_flow_logs_share_correlation_id_and_contain_no_user_data(logs) -> None:
    payload = whatsapp_payload("wamid.private", f"Recebi {AMOUNT} por {SECRET_TEXT}")
    import hmac
    from hashlib import sha256

    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"test-app-secret", body, sha256).hexdigest()
    response = client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-Request-ID": "req-abc12345",
        },
    )
    assert response.headers["X-Request-ID"] == "req-abc12345"
    drain_jobs()

    entries = logs.entries()
    events = {entry["event"] for entry in entries}
    assert {"http_request", "whatsapp_webhook_received", "job_done"} <= events
    for entry in entries:
        if entry["event"] in {"http_request", "whatsapp_webhook_received"} or entry.get(
            "job_kind"
        ) in {"process_message", "send_message"}:
            assert entry["correlation_id"] == "req-abc12345", entry
    job_kinds = {entry.get("job_kind") for entry in entries if entry["event"] == "job_done"}
    assert {"process_message", "send_message"} <= job_kinds
    assert_private(logs.lines)


def test_invalid_request_id_is_replaced() -> None:
    response = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    generated = response.headers["X-Request-ID"]
    assert len(generated) == 32 and generated.isalnum()


def test_request_log_uses_route_template_without_query_string(logs) -> None:
    client.get(
        "/v1/summaries/monthly",
        params={"whatsapp_number": "+5511999999999", "year": 2026, "month": 11},
    )
    [entry] = [entry for entry in logs.entries() if entry["event"] == "http_request"]
    assert entry["path"] == "/v1/summaries/monthly"
    assert entry["status_code"] == 200
    assert entry["method"] == "GET"
    assert_private(logs.lines)


def test_job_failure_logs_error_class_but_not_its_message(logs, monkeypatch) -> None:
    import lucromei.processing as processing

    def explode(*args, **kwargs):
        raise RuntimeError(f"failed while storing {SECRET_TEXT} {AMOUNT}")

    monkeypatch.setattr(processing, "queue_reply", explode)
    post_webhook(whatsapp_payload("wamid.fail", "Recebi 10 por um corte"))
    drain_jobs()

    [entry] = [entry for entry in logs.entries() if entry["event"] == "job_retry_scheduled"]
    assert entry["error"] == "RuntimeError"
    assert entry["job_kind"] == "process_message"
    assert_private(logs.lines)


def test_sentry_event_is_scrubbed() -> None:
    token = observability.correlation_id.set("req-sentry01")
    try:
        event = scrub_event(
            {
                "request": {
                    "method": "POST",
                    "url": "https://api.example/v1/summaries/monthly?whatsapp_number=5511999999999",
                    "data": {"text": SECRET_TEXT},
                    "query_string": "whatsapp_number=5511999999999",
                    "headers": {"X-Internal-Api-Key": "secret"},
                    "cookies": {"a": "b"},
                },
                "extra": {"text": SECRET_TEXT},
                "user": {"id": "5511999999999"},
                "exception": {
                    "values": [
                        {
                            "type": "IntegrityError",
                            "value": f"duplicate key ({SECRET_TEXT}, {AMOUNT})",
                            "stacktrace": {"frames": [{"function": "f", "vars": {"t": AMOUNT}}]},
                        }
                    ]
                },
                "logentry": {"message": "job_dead", "params": [SECRET_TEXT]},
                "breadcrumbs": {
                    "values": [
                        {"category": "log", "message": "job_done", "data": {"text": AMOUNT}}
                    ]
                },
            }
        )
    finally:
        observability.correlation_id.reset(token)

    assert event["request"] == {
        "method": "POST",
        "url": "https://api.example/v1/summaries/monthly",
    }
    assert event["exception"]["values"][0]["type"] == "IntegrityError"
    assert event["tags"]["correlation_id"] == "req-sentry01"
    assert_private([json.dumps(event)])
    assert "secret" not in json.dumps(event)


def test_sentry_is_off_without_dsn_and_private_with_it(monkeypatch) -> None:
    import sentry_sdk

    captured: dict = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))

    assert observability.init_sentry("", environment="local") is False
    assert captured == {}

    assert observability.init_sentry("https://key@o1.ingest.sentry.io/1", environment="prod")
    assert captured["send_default_pii"] is False
    assert captured["include_local_variables"] is False
    assert captured["max_request_body_size"] == "never"
    assert captured["before_send"] is scrub_event
    assert captured["traces_sample_rate"] == 0.0


def test_uvicorn_access_log_is_disabled() -> None:
    observability.configure_logging("INFO")
    assert logging.getLogger("uvicorn.access").disabled is True

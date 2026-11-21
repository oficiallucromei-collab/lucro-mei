import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from support import (
    TestingSession,
    drain_jobs,
    onboard,
    post_webhook,
    requires_postgres,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
    worker,
)

from lucromei.config import get_settings
from lucromei.jobs import (
    DEAD,
    DONE,
    PENDING,
    RUNNING,
    JobContext,
    RetryPolicy,
    enqueue,
    utc_now,
)
from lucromei.main import app
from lucromei.models import InboundEvent, Job, OutboundMessage, Transaction
from lucromei.processing import (
    NOT_UNDERSTOOD_REPLY,
    TEXT_ONLY_REPLY,
    handle_process_message,
)
from lucromei.whatsapp import MockOutboundSender, WhatsAppSendError
from lucromei.worker import Worker


def setup_function() -> None:
    reset_state()
    onboard()


class FlakySender(MockOutboundSender):
    """Fails the first `failures` sends with the given error, then succeeds."""

    def __init__(self, failures: int, *, retryable: bool = True, code: str = "http_503"):
        super().__init__()
        self.failures = failures
        self.retryable = retryable
        self.code = code
        self.calls = 0

    def send_text(self, recipient: str, text: str) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise WhatsAppSendError(self.code, retryable=self.retryable)
        return super().send_text(recipient, text)


def context(sender, now=None) -> JobContext:
    return JobContext(
        now=now or utc_now(),
        services={"sender": sender, "max_attempts": get_settings().job_max_attempts},
    )


def aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; they are stored in UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def jobs(kind: str | None = None) -> list[Job]:
    with TestingSession() as session:
        query = session.query(Job)
        if kind:
            query = query.filter_by(kind=kind)
        return query.order_by(Job.created_at).all()


def test_webhook_records_event_and_returns_before_processing() -> None:
    response = post_webhook(whatsapp_payload("wamid.fast", "Recebi 150 por um corte"))

    assert response.status_code == 200
    assert response.json() == {"received": True, "accepted": 1, "duplicates": 0, "statuses": 0}
    assert sent_texts() == []
    with TestingSession() as session:
        event = session.query(InboundEvent).one()
        assert (event.external_id, event.status, event.sender) == (
            "wamid.fast",
            "received",
            "5511999999999",
        )
        assert session.query(Transaction).count() == 0
    [job] = jobs()
    assert (job.kind, job.status, job.attempts) == ("process_message", PENDING, 0)


def test_redelivery_before_processing_creates_a_single_job() -> None:
    payload = whatsapp_payload("wamid.twice", "Recebi 150 por um corte")
    post_webhook(payload)
    second = post_webhook(payload)

    assert second.json() == {"received": True, "accepted": 0, "duplicates": 1, "statuses": 0}
    assert len(jobs("process_message")) == 1
    drain_jobs()
    assert len(sent_texts()) == 1


def test_redelivered_ambiguous_message_is_answered_once() -> None:
    payload = whatsapp_payload("wamid.ambiguous-twice", "Paguei 50 para João")
    signed_webhook(payload)
    signed_webhook(payload)
    assert sent_texts() == [NOT_UNDERSTOOD_REPLY]


def test_non_text_message_gets_text_only_reply() -> None:
    signed_webhook(whatsapp_payload("wamid.audio", None, message_type="audio"))
    assert sent_texts() == [TEXT_ONLY_REPLY]
    with TestingSession() as session:
        assert session.query(Transaction).count() == 0


def test_invalid_json_with_valid_signature_is_rejected() -> None:
    import hmac
    from hashlib import sha256

    from support import client

    body = b"{not json"
    signature = "sha256=" + hmac.new(b"test-app-secret", body, sha256).hexdigest()
    response = client.post(
        "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert response.status_code == 400


def test_backoff_doubles_and_is_capped() -> None:
    policy = RetryPolicy(base_seconds=5, max_seconds=60)
    delays = [policy.delay(attempt).total_seconds() for attempt in range(1, 7)]
    assert delays == [5, 10, 20, 40, 60, 60]


def test_send_failure_retries_with_backoff_without_rewriting_ledger() -> None:
    sender = FlakySender(failures=2)
    post_webhook(whatsapp_payload("wamid.retry", "Recebi 150 por um corte"))
    queue = worker(sender).queue
    start = utc_now()

    queue.run_due(context(sender, start))  # process_message
    queue.run_due(context(sender, start))  # first send fails
    [send_job] = jobs("send_message")
    assert (send_job.status, send_job.attempts, send_job.last_error) == (
        PENDING,
        1,
        "WhatsAppSendError:http_503",
    )
    assert abs((aware(send_job.run_after) - start).total_seconds() - 5) < 1

    assert queue.run_due(context(sender, start + timedelta(seconds=2))) == 0  # not due yet
    queue.run_due(context(sender, start + timedelta(seconds=6)))  # second failure
    [send_job] = jobs("send_message")
    retry_at = aware(send_job.run_after)
    assert abs((retry_at - (start + timedelta(seconds=6))).total_seconds() - 10) < 1

    queue.run_due(context(sender, start + timedelta(seconds=20)))  # succeeds
    [send_job] = jobs("send_message")
    assert send_job.status == DONE
    assert sender.calls == 3
    assert len(sender.sent_messages) == 1
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1
        outbound = session.query(OutboundMessage).one()
        assert (outbound.status, outbound.provider_message_id) == ("sent", "mock.1")


def test_send_gives_up_after_max_attempts() -> None:
    get_settings().job_max_attempts = 3
    sender = FlakySender(failures=99)
    post_webhook(whatsapp_payload("wamid.dead", "Recebi 150 por um corte"))
    queue = worker(sender).queue
    now = utc_now()
    for step in range(10):
        queue.run_due(context(sender, now + timedelta(hours=step)))

    [send_job] = jobs("send_message")
    assert (send_job.status, send_job.attempts) == (DEAD, 3)
    assert sender.calls == 3
    with TestingSession() as session:
        outbound = session.query(OutboundMessage).one()
        assert (outbound.status, outbound.error_code) == ("failed", "http_503")
        assert session.query(Transaction).count() == 1


def test_permanent_send_error_is_not_retried() -> None:
    sender = FlakySender(failures=99, retryable=False, code="meta_131026")
    post_webhook(whatsapp_payload("wamid.invalid-number", "Recebi 150 por um corte"))
    drain_jobs(sender)
    [send_job] = jobs("send_message")
    assert (send_job.status, send_job.attempts) == (DEAD, 1)
    assert sender.calls == 1
    with TestingSession() as session:
        assert session.query(OutboundMessage).one().error_code == "meta_131026"


def test_processing_crash_rolls_back_and_retries(monkeypatch) -> None:
    import lucromei.processing as processing

    original = processing.queue_reply
    calls = {"n": 0}

    def crash_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return original(*args, **kwargs)

    monkeypatch.setattr(processing, "queue_reply", crash_once)
    sender = app.state.whatsapp_sender
    post_webhook(whatsapp_payload("wamid.crash", "Recebi 150 por um corte"))
    queue = worker().queue
    now = utc_now()

    queue.run_due(context(sender, now))
    with TestingSession() as session:
        # The ledger write was rolled back together with the failed job.
        assert session.query(Transaction).count() == 0
        assert session.query(InboundEvent).one().status == "received"
    [job] = jobs("process_message")
    assert (job.status, job.last_error) == (PENDING, "RuntimeError")

    for step in range(1, 4):
        queue.run_due(context(sender, now + timedelta(minutes=step)))
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1
        assert session.query(InboundEvent).one().status == "processed"
    assert len(sender.sent_messages) == 1


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_finish() -> None:
    post_webhook(whatsapp_payload("wamid.lease", "Recebi 150 por um corte"))
    sender = app.state.whatsapp_sender
    queue = worker().queue
    now = utc_now()

    [stale] = queue.claim(limit=10, now=now)  # this worker then "dies"
    assert queue.claim(limit=10, now=now + timedelta(seconds=30)) == []  # lease still held
    [fresh] = queue.claim(limit=10, now=now + timedelta(seconds=121))
    assert fresh.id == stale.id and fresh.attempts == 2

    assert queue.run(fresh, context(sender)) == DONE
    assert queue.run(stale, context(sender)) == RUNNING  # stale worker: no effect
    [job] = jobs("process_message")
    assert job.status == DONE
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1
        assert session.query(OutboundMessage).count() == 1


def test_lease_expiry_on_last_attempt_marks_job_dead() -> None:
    get_settings().job_max_attempts = 1
    post_webhook(whatsapp_payload("wamid.last", "Recebi 150 por um corte"))
    queue = worker().queue
    now = utc_now()
    queue.claim(limit=10, now=now)
    assert queue.claim(limit=10, now=now + timedelta(seconds=121)) == []
    [job] = jobs("process_message")
    assert (job.status, job.last_error) == (DEAD, "lease_expired")
    with TestingSession() as session:
        assert session.query(InboundEvent).one().status == "failed"


def test_messages_from_one_sender_are_processed_in_order() -> None:
    post_webhook(whatsapp_payload("wamid.first", "Recebi 100 por um corte", timestamp="100"))
    post_webhook(whatsapp_payload("wamid.second", "desfazer", timestamp="101"))
    sender = app.state.whatsapp_sender
    queue = worker().queue
    now = utc_now()
    first, second = queue.claim(limit=10, now=now)
    assert first.payload["external_id"] == "wamid.first"

    # The worker reaches the later message first: it must wait, without losing an attempt.
    assert queue.run(second, context(sender, now)) == PENDING
    [waiting] = [job for job in jobs() if job.id == second.id]
    assert waiting.attempts == 0
    assert queue.run(first, context(sender, now)) == DONE
    queue.run_due(context(sender, now + timedelta(seconds=3)))
    queue.run_due(context(sender, now + timedelta(seconds=3)))

    assert sent_texts() == [
        "Entrada registrada: R$ 100,00. Responda 'desfazer' para corrigir.",
        "Lançamento de R$ 100,00 desfeito.",
    ]


def test_failed_message_does_not_block_later_messages() -> None:
    get_settings().job_max_attempts = 1
    post_webhook(whatsapp_payload("wamid.broken", "Recebi 100 por um corte", timestamp="100"))
    with TestingSession() as session:
        session.query(Job).update({"payload": {"external_id": "wamid.broken", "x": 1}})
        session.commit()
    queue = worker().queue
    now = utc_now()
    queue.claim(limit=10, now=now)
    queue.claim(limit=10, now=now + timedelta(seconds=121))  # dies on lease expiry
    post_webhook(whatsapp_payload("wamid.after", "Recebi 50 por um corte", timestamp="101"))
    drain_jobs()
    assert sent_texts() == ["Entrada registrada: R$ 50,00. Responda 'desfazer' para corrigir."]


def test_embedded_worker_processes_webhook_in_background() -> None:
    get_settings().worker_poll_seconds = 30  # only the wake-up can make this test fast
    background = Worker(TestingSession, app.state.whatsapp_sender, get_settings())
    background.start()
    app.state.worker = background
    try:
        post_webhook(whatsapp_payload("wamid.bg", "Recebi 150 por um corte"))
        deadline = time.monotonic() + 5
        while not sent_texts() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        background.stop()
        app.state.worker = None
        get_settings().worker_poll_seconds = 1.0
    assert sent_texts() == ["Entrada registrada: R$ 150,00. Responda 'desfazer' para corrigir."]


@requires_postgres
def test_skip_locked_lets_other_workers_pass_a_locked_job() -> None:
    with TestingSession() as session:
        enqueue(session, "noop", {}, dedupe_key="a", max_attempts=3)
        enqueue(session, "noop", {}, dedupe_key="b", max_attempts=3)
        session.commit()
    queue = worker().queue
    with TestingSession() as holder:
        locked = holder.query(Job).filter_by(dedupe_key="a").with_for_update().one()
        started = time.monotonic()
        claimed = queue.claim(limit=10)
        assert time.monotonic() - started < 2  # did not wait for the lock
        assert [job.id for job in claimed] != [locked.id]
        assert len(claimed) == 1
        holder.rollback()


@requires_postgres
def test_concurrent_workers_claim_each_job_exactly_once() -> None:
    with TestingSession() as session:
        for index in range(40):
            enqueue(session, "noop", {}, dedupe_key=f"job-{index}", max_attempts=3)
        session.commit()
    queue = worker().queue
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def claim_loop() -> None:
        barrier.wait()
        while batch := queue.claim(limit=3):
            with lock:
                claimed.extend(job.id for job in batch)

    threads = [threading.Thread(target=claim_loop) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 40
    assert len(set(claimed)) == 40


@requires_postgres
def test_concurrent_processing_of_the_same_event_writes_once() -> None:
    post_webhook(whatsapp_payload("wamid.race", "Recebi 150 por um corte"))
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def process() -> None:
        with TestingSession() as session:
            barrier.wait()
            try:
                handle_process_message(
                    session, {"external_id": "wamid.race"}, context(app.state.whatsapp_sender)
                )
                session.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                session.rollback()

    threads = [threading.Thread(target=process) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with TestingSession() as session:
        assert session.query(Transaction).count() == 1
        assert session.query(OutboundMessage).count() == 1


@pytest.mark.parametrize("sender_format", ["+55 11 99999-9999", "5511999999999"])
def test_sender_number_is_normalized(sender_format: str) -> None:
    signed_webhook(whatsapp_payload("wamid.norm", "Recebi 10 por um corte", sender=sender_format))
    with TestingSession() as session:
        assert session.query(InboundEvent).one().sender == "5511999999999"

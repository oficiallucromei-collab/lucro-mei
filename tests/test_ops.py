from support import (
    engine,
    onboard,
    requires_postgres,
    reset_state,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.ops import main, pilot_check


def setup_function() -> None:
    reset_state()


@requires_postgres
def test_pilot_check_runs_every_query_and_prints_no_personal_data(capsys) -> None:
    onboard(name="Fulano Secreto")
    signed_webhook(whatsapp_payload("m1", "Recebi 150 por um corte"))
    signed_webhook(whatsapp_payload("m2", "Paguei 300 para o fornecedor"))
    assert pilot_check(engine, 24 * 365 * 10) == 0
    out = capsys.readouterr().out
    assert "ERRO" not in out
    assert "Fulano" not in out and "5511999999999" not in out and "corte" not in out
    assert "income | 1 | None" in out


def test_cli_requires_a_command(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])


def test_operator_tools(capsys) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from support import TestingSession

    from lucromei import ops
    from lucromei.models import Business, DeletionRequest, Job, OutboundMessage, Subscription

    now = datetime.now(UTC)
    business_id = onboard("5511987654321", name="Fulano Secreto")
    with TestingSession() as session:
        assert ops.find_business(session, "4321") == 0
        assert ops.find_business(session, "12a4") == 2
        assert ops.show_business(session, business_id, now) == 0
        assert ops.extend_trial(session, business_id, 10, now) == 0
        assert ops.extend_trial(session, business_id, 500, now) == 1
        trial_end = session.get(Business, business_id).trial_ends_at
        assert trial_end.replace(tzinfo=UTC) > now + timedelta(days=23)
        assert ops.grant_subscription(session, business_id, 30, now) == 0
        sub = session.scalar(select(Subscription))
        assert (sub.status, sub.provider, sub.price_cents) == ("active", "manual", 0)
    out = capsys.readouterr().out
    assert business_id in out and "Fulano" not in out and "5511987654321" not in out

    # requeue-dead: a failed reply gets another full set of attempts.
    with TestingSession() as session:
        session.add(
            OutboundMessage(
                id="o1",
                recipient="5511987654321",
                text="x",
                status="failed",
                error_code="meta_190",
                created_at=now,
            )
        )
        session.add(
            Job(
                kind="send_message",
                dedupe_key="send_message:o1",
                payload={"outbound_id": "o1"},
                status="dead",
                attempts=8,
                max_attempts=8,
                run_after=now,
                finished_at=now,
            )
        )
        session.commit()
        assert ops.requeue_dead(session, "send_message", 1, now) == 0
        job = session.scalar(select(Job).where(Job.dedupe_key == "send_message:o1"))
        assert (job.status, job.attempts) == ("pending", 0)
        assert session.get(OutboundMessage, "o1").status == "queued"

    # reapply-deletions: an account restored from a backup is erased again.
    with TestingSession() as session:
        session.add(
            DeletionRequest(
                business_id=business_id,
                subject_digest="d",
                status="completed",
                requested_at=now,
                confirm_by=now,
                completed_at=now,
            )
        )
        session.commit()
    with Session(engine) as session:
        assert ops.reapply_deletions(session, now) == 0
    with TestingSession() as session:
        assert session.get(Business, business_id) is None


def test_support_command(capsys) -> None:
    from support import sent_texts

    from lucromei.config import get_settings

    onboard()
    get_settings().contact_email = "suporte@example.com"
    signed_webhook(whatsapp_payload("s1", "Suporte"))
    reply = sent_texts()[-1]
    assert reply.startswith(
        "Para falar com uma pessoa do LucroMEI, mande um e-mail para suporte@example.com"
    )
    get_settings().contact_email = ""

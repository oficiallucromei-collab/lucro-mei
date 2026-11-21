"""Retention in bounded batches, and the queue with several workers."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from support import TestingSession, drain_jobs, reset_state

from lucromei.config import get_settings
from lucromei.jobs import JobContext
from lucromei.models import InboundEvent, Job
from lucromei.processing import RETENTION, handle_maintenance

OLD = datetime.now(UTC) - timedelta(days=200)


def setup_function() -> None:
    reset_state()


def add_old_events(count: int) -> None:
    with TestingSession() as session:
        for index in range(count):
            session.add(
                InboundEvent(
                    external_id=f"old-{index}",
                    sender="5511999999999",
                    message_type="text",
                    text="Recebi 10",
                    sent_at=OLD,
                    status="processed",
                    received_at=OLD,
                )
            )
        session.commit()


def remaining_text() -> int:
    with TestingSession() as session:
        return session.scalar(
            select(func.count(InboundEvent.id)).where(InboundEvent.text.is_not(None))
        )


def test_retention_works_through_a_backlog_in_bounded_batches(monkeypatch) -> None:
    monkeypatch.setattr("lucromei.processing.RETENTION_PAUSE_SECONDS", 0)
    get_settings().retention_batch_size = 7
    add_old_events(30)
    with TestingSession() as session:
        handle_maintenance(
            session, {}, JobContext(now=datetime.now(UTC), services={"max_attempts": 8})
        )
        session.commit()
    drain_jobs()
    assert remaining_text() == 0
    with TestingSession() as session:
        runs = session.scalar(select(func.count(Job.id)).where(Job.kind == RETENTION))
    assert runs >= 5  # 30 rows / 7 per batch, plus the run that finds nothing left
    get_settings().retention_batch_size = 2000


def test_retention_without_backlog_runs_once() -> None:
    add_old_events(3)
    with TestingSession() as session:
        handle_maintenance(
            session, {}, JobContext(now=datetime.now(UTC), services={"max_attempts": 8})
        )
        session.commit()
    drain_jobs()
    assert remaining_text() == 0
    with TestingSession() as session:
        assert session.scalar(select(func.count(Job.id)).where(Job.kind == RETENTION)) == 1

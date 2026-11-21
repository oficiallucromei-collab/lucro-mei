"""Signals the alerts are built on: log severity and the queue_health event."""

import json
import logging
from datetime import UTC, datetime, timedelta

from support import TestingSession, reset_state, worker

from lucromei.jobs import enqueue, queue_health
from lucromei.models import Job
from lucromei.observability import JsonFormatter


def setup_function() -> None:
    reset_state()


def test_json_logs_carry_cloud_logging_severity() -> None:
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "job_dead", None, None)
    entry = json.loads(JsonFormatter().format(record))
    assert entry["severity"] == "ERROR" and entry["event"] == "job_dead"
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "queue_health", None, None)
    record.oldest_due_seconds = 42
    record.text = "Recebi 150"  # not allowlisted: must never be logged
    entry = json.loads(JsonFormatter().format(record))
    assert entry["severity"] == "INFO" and entry["oldest_due_seconds"] == 42
    assert "text" not in entry


def test_queue_health_measures_lag_and_dead_jobs() -> None:
    now = datetime.now(UTC)
    with TestingSession() as session:
        assert queue_health(session, now, dead_since=now - timedelta(hours=1)).due_jobs == 0
        enqueue(
            session,
            "noop",
            {},
            dedupe_key="a",
            max_attempts=3,
            run_after=now - timedelta(seconds=90),
        )
        enqueue(
            session,
            "noop",
            {},
            dedupe_key="b",
            max_attempts=3,
            run_after=now - timedelta(seconds=10),
        )
        enqueue(
            session, "noop", {}, dedupe_key="c", max_attempts=3, run_after=now + timedelta(hours=1)
        )
        enqueue(session, "noop", {}, dedupe_key="d", max_attempts=3)
        dead = session.query(Job).filter_by(dedupe_key="d").one()
        dead.status, dead.finished_at = "dead", now - timedelta(minutes=5)
        session.commit()
        health = queue_health(session, now, dead_since=now - timedelta(hours=1))
    assert health.due_jobs == 2
    assert 89 <= health.oldest_due_seconds <= 91
    assert health.dead_jobs == 1


def test_worker_logs_queue_health_once_a_minute(caplog) -> None:
    runner = worker()
    with caplog.at_level(logging.INFO, logger="lucromei.worker"):
        runner.run_once()
        runner.run_once()
    events = [r for r in caplog.records if r.msg == "queue_health"]
    assert len(events) == 1
    assert events[0].due_jobs >= 0 and events[0].oldest_due_seconds >= 0

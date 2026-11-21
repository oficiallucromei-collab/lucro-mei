"""A small job queue stored in PostgreSQL.

Workers claim jobs with SELECT ... FOR UPDATE SKIP LOCKED, so several workers (or several
Cloud Run instances) never take the same job. A claimed job holds a lease; if its worker dies,
the lease expires and another worker picks the job up again. Failures are retried with
exponential backoff up to a limit, after which the job is marked dead and kept for inspection.

Handlers must be idempotent: a job can run more than once (lease expiry, crash before commit).
A handler's database writes and the job's completion are committed in the same transaction.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from lucromei.models import Job, new_id
from lucromei.observability import correlation_id

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
DONE = "done"
DEAD = "dead"


class PermanentJobError(Exception):
    """Retrying cannot help (e.g. an invalid recipient number). The job goes straight to dead."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RetryLater(Exception):
    """The job is not ready yet (e.g. an earlier message must be handled first).

    It is rescheduled without counting as a failed attempt.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry in {seconds}s")
        self.seconds = seconds


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_seconds: float = 5.0
    max_seconds: float = 900.0

    def delay(self, attempts: int) -> timedelta:
        """Backoff after the given number of failed attempts: 5s, 10s, 20s, ... capped."""
        exponent = max(0, attempts - 1)
        return timedelta(seconds=min(self.base_seconds * (2**exponent), self.max_seconds))


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    correlation_id: str | None = None


@dataclass(slots=True)
class JobContext:
    now: datetime
    services: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Session, dict[str, Any], JobContext], None]
DeadHook = Callable[[Session, dict[str, Any], str], None]


def utc_now() -> datetime:
    return datetime.now(UTC)


def insert_ignore(session: Session, model: type, values: dict[str, Any]) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING. Returns True when a new row was written.

    Unlike catching IntegrityError, this does not abort the surrounding transaction.
    """

    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    table = model.__table__
    primary_key = list(table.primary_key.columns)[0]
    statement = insert(table).values(**values).on_conflict_do_nothing().returning(primary_key)
    # rowcount is unreliable here (-1 with some drivers); RETURNING yields a row only on insert.
    return session.execute(statement).first() is not None


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str,
    max_attempts: int,
    run_after: datetime | None = None,
) -> bool:
    """Add a job unless one with the same dedupe key exists. Does not commit."""

    now = utc_now()
    return insert_ignore(
        session,
        Job,
        {
            "id": new_id(),
            "kind": kind,
            "dedupe_key": dedupe_key,
            "payload": payload,
            "status": PENDING,
            "attempts": 0,
            "max_attempts": max_attempts,
            "run_after": run_after or now,
            "correlation_id": correlation_id.get(),
            "created_at": now,
            "updated_at": now,
        },
    )


@dataclass(frozen=True, slots=True)
class QueueHealth:
    due_jobs: int
    oldest_due_seconds: int
    dead_jobs: int


def queue_health(session: Session, now: datetime, *, dead_since: datetime) -> QueueHealth:
    """How far behind the queue is: due jobs not yet taken, and jobs that gave up."""
    due = and_(Job.status == PENDING, Job.run_after <= now)
    count, oldest = session.execute(
        select(func.count(Job.id), func.min(Job.run_after)).where(due)
    ).one()
    dead = session.scalar(
        select(func.count(Job.id)).where(Job.status == DEAD, Job.finished_at >= dead_since)
    )
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    lag = int((now - oldest).total_seconds()) if oldest is not None else 0
    return QueueHealth(due_jobs=count, oldest_due_seconds=max(0, lag), dead_jobs=dead or 0)


class JobQueue:
    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        handlers: dict[str, Handler],
        dead_hooks: dict[str, DeadHook] | None = None,
        lease: timedelta = timedelta(seconds=120),
        lock_during_run: frozenset[str] = frozenset(),
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        self.session_factory = session_factory
        self.handlers = handlers
        self.dead_hooks = dead_hooks or {}
        self.lease = lease
        self.lock_during_run = lock_during_run
        self.retry_policy = retry_policy

    def claim(self, *, limit: int, now: datetime | None = None) -> list[ClaimedJob]:
        """Take up to `limit` due jobs, including jobs whose worker's lease expired."""

        now = now or utc_now()
        with self.session_factory() as session:
            due = or_(
                and_(Job.status == PENDING, Job.run_after <= now),
                and_(Job.status == RUNNING, Job.locked_until < now),
            )
            jobs = session.scalars(
                select(Job)
                .where(due)
                .order_by(Job.run_after, Job.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            claimed: list[ClaimedJob] = []
            for job in jobs:
                if job.attempts >= job.max_attempts:
                    # Its last attempt was in flight when the worker died.
                    self._mark_dead(session, job, "lease_expired", now)
                    continue
                job.status = RUNNING
                job.attempts += 1
                job.locked_until = now + self.lease
                claimed.append(
                    ClaimedJob(
                        job.id, job.kind, dict(job.payload), job.attempts, job.correlation_id
                    )
                )
            session.commit()
            return claimed

    def run(self, claimed: ClaimedJob, context: JobContext) -> str:
        """Run one claimed job. Returns its new status."""

        token = correlation_id.set(claimed.correlation_id)
        try:
            return self._run(claimed, context)
        finally:
            correlation_id.reset(token)

    def _run(self, claimed: ClaimedJob, context: JobContext) -> str:
        handler = self.handlers.get(claimed.kind)
        started = time.monotonic()
        with self.session_factory() as session:
            try:
                if handler is None:
                    raise PermanentJobError("unknown_job_kind")
                if claimed.kind in self.lock_during_run:
                    # A live, bounded external call must not be repeated by a lease reclaimer.
                    if self._lock_if_still_ours(session, claimed) is None:
                        return RUNNING
                handler(session, claimed.payload, context)
                job = self._lock_if_still_ours(session, claimed)
                if job is None:
                    session.rollback()
                    return RUNNING
                job.status = DONE
                job.locked_until = None
                job.finished_at = context.now
                session.commit()
                logger.info(
                    "job_done",
                    extra={
                        "job_id": claimed.id,
                        "job_kind": claimed.kind,
                        "attempts": claimed.attempts,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                return DONE
            except Exception as exc:  # noqa: BLE001 - a job failure must never stop the worker
                session.rollback()
                return self._record_failure(session, claimed, exc, context.now)

    def run_due(self, context: JobContext, *, limit: int = 10) -> int:
        claimed = self.claim(limit=limit, now=context.now)
        for job in claimed:
            self.run(job, context)
        return len(claimed)

    def _record_failure(
        self, session: Session, claimed: ClaimedJob, exc: Exception, now: datetime
    ) -> str:
        job = self._lock_if_still_ours(session, claimed)
        if job is None:
            session.rollback()
            return RUNNING
        job.locked_until = None
        if isinstance(exc, RetryLater):
            job.status = PENDING
            job.attempts -= 1
            job.run_after = now + timedelta(seconds=exc.seconds)
            session.commit()
            return PENDING
        error = _describe(exc)
        if isinstance(exc, PermanentJobError) or job.attempts >= job.max_attempts:
            self._mark_dead(session, job, error, now)
            session.commit()
            logger.error(
                "job_dead",
                extra=_job_log_fields(job, error),
            )
            return DEAD
        job.status = PENDING
        job.last_error = error
        job.run_after = now + self.retry_policy.delay(job.attempts)
        session.commit()
        logger.warning(
            "job_retry_scheduled",
            extra=_job_log_fields(job, error),
        )
        return PENDING

    def _mark_dead(self, session: Session, job: Job, error: str, now: datetime) -> None:
        job.status = DEAD
        job.last_error = error
        job.locked_until = None
        job.finished_at = now
        if hook := self.dead_hooks.get(job.kind):
            hook(session, dict(job.payload), error)

    @staticmethod
    def _lock_if_still_ours(session: Session, claimed: ClaimedJob) -> Job | None:
        """Lock the job row, unless another worker re-claimed it after our lease expired."""

        job = session.scalar(select(Job).where(Job.id == claimed.id).with_for_update())
        if job is None or job.status != RUNNING or job.attempts != claimed.attempts:
            return None
        return job


def _describe(exc: Exception) -> str:
    """Error class and code only. Exception messages may contain user text, so they are dropped."""

    code = getattr(exc, "code", None)
    name = type(exc).__name__
    return f"{name}:{code}"[:500] if code else name[:500]


def _job_log_fields(job: Job, error: str) -> dict[str, Any]:
    return {"job_id": job.id, "job_kind": job.kind, "attempts": job.attempts, "error": error}

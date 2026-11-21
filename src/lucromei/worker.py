"""Run background jobs: embedded in the web process or as `python -m lucromei.worker`."""

import logging
import threading
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from lucromei.ai import MessageInterpreter, build_interpreter
from lucromei.config import Settings, get_settings
from lucromei.jobs import JobContext, JobQueue, utc_now
from lucromei.observability import (
    configure_logging,
    correlation_id,
    init_sentry,
    new_correlation_id,
)
from lucromei.processing import DEAD_HOOKS, HANDLERS, enqueue_maintenance
from lucromei.whatsapp import OutboundSender

logger = logging.getLogger(__name__)


def build_queue(session_factory: sessionmaker, settings: Settings) -> JobQueue:
    return JobQueue(
        session_factory,
        handlers=HANDLERS,
        dead_hooks=DEAD_HOOKS,
        lease=timedelta(seconds=settings.job_lease_seconds),
    )


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        sender: OutboundSender,
        settings: Settings | None = None,
        interpreter: MessageInterpreter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.queue = build_queue(session_factory, self.settings)
        self.sender = sender
        self.interpreter = interpreter if interpreter is not None else build_interpreter(
            self.settings
        )
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._maintenance_bucket: int | None = None

    def context(self) -> JobContext:
        return JobContext(
            now=utc_now(),
            services={
                "sender": self.sender,
                "interpreter": self.interpreter,
                "max_attempts": self.settings.job_max_attempts,
            },
        )

    def run_once(self) -> int:
        """Claim and run one batch of due jobs. Returns how many were claimed."""
        context = self.context()
        self.schedule_maintenance(context.now)
        return self.queue.run_due(context, limit=self.settings.worker_batch_size)

    def schedule_maintenance(self, now) -> None:
        """Enqueue the periodic maintenance job (deduplicated per interval across workers)."""
        bucket = int(now.timestamp()) // self.settings.maintenance_interval_seconds
        if bucket == self._maintenance_bucket:
            return
        token = correlation_id.set(new_correlation_id())  # its own ID: no request created it
        try:
            self._enqueue_maintenance(now)
        finally:
            correlation_id.reset(token)
        self._maintenance_bucket = bucket

    def _enqueue_maintenance(self, now) -> None:
        with self.queue.session_factory() as session:
            enqueue_maintenance(
                session, now, interval_seconds=self.settings.maintenance_interval_seconds
            )
            session.commit()

    def drain(self, max_batches: int = 100) -> int:
        """Run batches until nothing is due. Used by tests and the one-shot CLI."""
        total = 0
        for _ in range(max_batches):
            claimed = self.run_once()
            total += claimed
            if claimed == 0:
                break
        return total

    def wake(self) -> None:
        """Skip the rest of the poll interval: new work was just enqueued."""
        self._wake.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = self.run_once()
            except Exception:  # noqa: BLE001 - e.g. database briefly unavailable
                logger.exception("worker_batch_failed")
                claimed = 0
            if claimed == 0:
                self._wake.wait(self.settings.worker_poll_seconds)
                self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.run_forever, name="lucromei-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)


def main() -> None:  # pragma: no cover - process entry point
    import argparse

    from lucromei.db import SessionLocal
    from lucromei.whatsapp import build_sender

    parser = argparse.ArgumentParser(description="Process LucroMEI background jobs.")
    parser.add_argument("--once", action="store_true", help="drain due jobs and exit")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    init_sentry(
        settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )
    worker = Worker(SessionLocal, build_sender(settings))
    if args.once:
        worker.drain()
    else:
        worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()

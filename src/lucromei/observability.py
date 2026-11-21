"""Structured logs with a correlation ID, and optional Sentry.

Privacy rule: logs and error reports never contain message text, amounts, names or phone
numbers. To make that hold by construction:

- only allowlisted `extra` fields are emitted; anything else passed to a logger is dropped;
- exception messages are dropped (database errors embed SQL parameters, i.e. user data); only
  the exception type and the code location of each frame are kept;
- Sentry runs without PII, local variables or request bodies, and the same scrubbing is
  applied to every event before it is sent.
"""

import contextvars
import json
import logging
import re
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

# Fields that may appear in logs. They are identifiers, counters, codes and timings only.
SAFE_FIELDS = frozenset(
    {
        "accepted",
        "attempts",
        "audio_duration_ms",
        "business_id",
        "duplicates",
        "duration_ms",
        "error",
        "job_id",
        "job_kind",
        "latency_ms",
        "method",
        "outcome",
        "path",
        "status_code",
        "statuses",
    }
)
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def accept_correlation_id(header_value: str | None) -> str:
    """Reuse a caller's request ID if it is a plain token, otherwise make a new one."""

    if header_value and _CORRELATION_ID_RE.fullmatch(header_value):
        return header_value
    return new_correlation_id()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # Event names only: the message is the format string, never interpolated arguments.
            "event": str(record.msg)[:200],
        }
        if cid := correlation_id.get():
            entry["correlation_id"] = cid
        for key in SAFE_FIELDS:
            if key in record.__dict__:
                entry[key] = _safe_value(record.__dict__[key])
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _exc, tb = record.exc_info
            entry["exc_type"] = exc_type.__name__
            entry["stack"] = [
                f"{frame.filename}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(tb)
            ][-15:]
        return json.dumps(entry, ensure_ascii=False)


def _safe_value(value: Any) -> Any:
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:200]


def configure_logging(level: str = "INFO") -> None:
    """Send every log record (ours and uvicorn's) through the JSON formatter. Idempotent."""

    root = logging.getLogger()
    handler = next(
        (h for h in root.handlers if getattr(h, "_lucromei", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler._lucromei = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter())
        for existing in list(root.handlers):
            root.removeHandler(existing)
        root.addHandler(handler)
    root.setLevel(level.upper())
    # HTTP library messages can embed signed media URLs, including in their format strings.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # uvicorn's access log prints query strings, which can carry phone numbers. Requests are
    # logged by the app's own middleware instead.
    logging.getLogger("uvicorn.access").disabled = True


# --- Sentry ---------------------------------------------------------------------------------

_SAFE_REQUEST_KEYS = {"method", "url"}


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Remove anything that could carry user data from a Sentry event."""

    request = event.get("request")
    if isinstance(request, dict):
        cleaned = {key: request[key] for key in _SAFE_REQUEST_KEYS if key in request}
        if isinstance(cleaned.get("url"), str):
            cleaned["url"] = cleaned["url"].split("?", 1)[0]
        event["request"] = cleaned
    for key in ("extra", "user", "contexts_user"):
        event.pop(key, None)
    exception = event.get("exception")
    if isinstance(exception, dict):
        for value in exception.get("values") or []:
            value["value"] = ""  # exception messages can embed SQL parameters
            for frame in (value.get("stacktrace") or {}).get("frames") or []:
                frame.pop("vars", None)
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        logentry.pop("params", None)
        logentry["formatted"] = str(logentry.get("message", ""))[:200]
    if (message := event.get("message")) and isinstance(message, str):
        event["message"] = message[:200]
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        breadcrumbs["values"] = [
            scrub_breadcrumb(crumb) for crumb in breadcrumbs.get("values") or []
        ]
    if cid := correlation_id.get():
        event.setdefault("tags", {})["correlation_id"] = cid
    return event


def scrub_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    data = crumb.get("data")
    safe = {k: v for k, v in data.items() if k in SAFE_FIELDS} if isinstance(data, dict) else {}
    return {
        "type": crumb.get("type"),
        "category": crumb.get("category"),
        "level": crumb.get("level"),
        "timestamp": crumb.get("timestamp"),
        "message": str(crumb.get("message") or "")[:200],
        "data": safe,
    }


def init_sentry(dsn: str, *, environment: str, traces_sample_rate: float = 0.0) -> bool:
    """Start Sentry when a DSN is configured. Returns whether it is active."""

    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.httpx import HttpxIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=traces_sample_rate,
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
        disabled_integrations=[HttpxIntegration],
    )
    return True

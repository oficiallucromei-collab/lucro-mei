import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.orm import Session

from lucromei.config import check_production_settings, get_settings, is_configured_secret
from lucromei.db import SessionLocal, get_session
from lucromei.domain import ParseError
from lucromei.jobs import insert_ignore
from lucromei.models import InboundEvent, utc_now
from lucromei.observability import (
    accept_correlation_id,
    configure_logging,
    correlation_id,
    init_sentry,
)
from lucromei.onboarding import OnboardingRequired
from lucromei.processing import enqueue_inbound_processing, record_delivery_statuses
from lucromei.schemas import (
    CorrectionRequest,
    HealthResponse,
    InboundMessage,
    MonthlySummaryResponse,
    ReversalRequest,
    TransactionResponse,
    TransactionStateResponse,
)
from lucromei.service import (
    LedgerError,
    LedgerService,
    TransactionNotFound,
    normalize_whatsapp_number,
)
from lucromei.whatsapp import (
    InvalidWebhookSignature,
    build_sender,
    extract_inbound_messages,
    extract_statuses,
    verify_signature,
)
from lucromei.worker import Worker

logger = logging.getLogger(__name__)
SessionDep = Annotated[Session, Depends(get_session)]



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    worker = None
    if settings.worker_mode == "embedded":
        worker = Worker(SessionLocal, app.state.whatsapp_sender, settings)
        worker.start()
    app.state.worker = worker
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
        app.state.worker = None


app = FastAPI(
    title="LucroMEI API",
    version="0.4.0",
    description="WhatsApp-first financial assistant for Brazilian microentrepreneurs.",
    lifespan=lifespan,
)
configure_logging(get_settings().log_level)
init_sentry(
    get_settings().sentry_dsn,
    environment=get_settings().environment,
    traces_sample_rate=get_settings().sentry_traces_sample_rate,
)
check_production_settings(get_settings())
app.state.whatsapp_sender = build_sender(get_settings())
app.state.worker = None

@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """Give every request a correlation ID and log it without query strings or bodies."""

    cid = accept_correlation_id(request.headers.get("X-Request-ID"))
    token = correlation_id.set(cid)
    started = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = cid
        return response
    finally:
        route = request.scope.get("route")
        logger.info(
            "http_request",
            extra={
                "method": request.method,
                # The route template ("/v1/transactions/{transaction_id}/reversal"), never the
                # query string, which can carry a phone number.
                "path": getattr(route, "path", "unmatched"),
                "status_code": status_code,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        correlation_id.reset(token)



def require_internal_api_key(
    api_key: Annotated[str | None, Header(alias="X-Internal-Api-Key")] = None,
) -> None:
    """Guard the /v1 endpoints until real end-user authentication exists.

    They accept any WhatsApp number or transaction ID, so without this guard anyone could read
    or change any customer's ledger. With no key configured they are disabled (404).
    """

    expected = get_settings().internal_api_key
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if api_key is None or not _secrets_match(api_key, expected):
        raise HTTPException(status_code=401, detail="Chave de acesso interna inválida.")


def _secrets_match(received: str, expected: str) -> bool:
    # Bytes, because compare_digest raises TypeError on non-ASCII str input.
    return hmac.compare_digest(received.encode(), expected.encode())


internal_api = APIRouter(prefix="/v1", dependencies=[Depends(require_internal_api_key)])


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/webhooks/whatsapp")
def verify_whatsapp_webhook(
    mode: Annotated[str, Query(alias="hub.mode")],
    token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> Response:
    expected = get_settings().whatsapp_verify_token
    if (
        mode != "subscribe"
        or not is_configured_secret(expected)
        or not _secrets_match(token, expected)
    ):
        raise HTTPException(status_code=403, detail="Token de verificação inválido.")
    return Response(content=challenge, media_type="text/plain")


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    session: SessionDep,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict[str, int | bool]:
    """Validate, record and acknowledge. Processing and replies happen in the background.

    Meta retries webhooks that are not answered quickly, so nothing slow happens here: each
    message is stored once (its Meta ID is the idempotency key) together with its processing
    job, in a single transaction, and the request returns 200.
    """

    body = await request.body()
    try:
        verify_signature(body, signature, get_settings().whatsapp_app_secret)
    except InvalidWebhookSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Corpo do webhook inválido.") from exc

    messages = extract_inbound_messages(payload)
    accepted = duplicates = 0
    now = utc_now()
    for message in messages:
        created = insert_ignore(
            session,
            InboundEvent,
            {
                "external_id": message.message_id[:255],
                "sender": normalize_whatsapp_number(message.sender)[:32],
                "message_type": message.message_type,
                "text": message.text,
                "sent_at": message.sent_at or now,
                "status": "received",
                "received_at": now,
            },
        )
        if created:
            enqueue_inbound_processing(
                session, message.message_id[:255], max_attempts=get_settings().job_max_attempts
            )
            accepted += 1
        else:
            duplicates += 1
    statuses = record_delivery_statuses(session, extract_statuses(payload), now)
    session.commit()
    logger.info(
        "whatsapp_webhook_received",
        extra={"accepted": accepted, "duplicates": duplicates, "statuses": statuses},
    )
    if accepted and (worker := getattr(request.app.state, "worker", None)) is not None:
        worker.wake()
    return {
        "received": True,
        "accepted": accepted,
        "duplicates": duplicates,
        "statuses": statuses,
    }


@internal_api.post(
    "/messages",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
def process_message(
    payload: InboundMessage,
    session: SessionDep,
) -> TransactionResponse:
    try:
        result = LedgerService(session).process(
            message_id=payload.message_id,
            whatsapp_number=payload.whatsapp_number,
            text=payload.text,
        )
    except OnboardingRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    transaction = result.transaction
    return TransactionResponse(
        transaction_id=transaction.id,
        message_id=result.message_id,
        transaction_type=transaction.transaction_type,
        amount_cents=transaction.amount_cents,
        description=transaction.description,
        occurred_at=transaction.occurred_at,
        duplicate=result.duplicate,
    )


@internal_api.post(
    "/transactions/{transaction_id}/corrections",
    response_model=TransactionStateResponse,
)
def correct_transaction(
    transaction_id: str,
    payload: CorrectionRequest,
    session: SessionDep,
) -> TransactionStateResponse:
    try:
        transaction = LedgerService(session).correct(
            transaction_id,
            amount_cents=payload.amount_cents,
            transaction_type=payload.transaction_type,
            description=payload.description,
            occurred_at=payload.occurred_at,
            reason=payload.reason,
        )
    except TransactionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _state_response(transaction)


@internal_api.post(
    "/transactions/{transaction_id}/reversal",
    response_model=TransactionStateResponse,
)
def reverse_transaction(
    transaction_id: str,
    payload: ReversalRequest,
    session: SessionDep,
) -> TransactionStateResponse:
    try:
        transaction = LedgerService(session).reverse(transaction_id, reason=payload.reason)
    except TransactionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _state_response(transaction)


@internal_api.get("/summaries/monthly", response_model=MonthlySummaryResponse)
def monthly_summary(
    whatsapp_number: str,
    session: SessionDep,
    year: Annotated[int, Query(ge=2020, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> MonthlySummaryResponse:
    summary = LedgerService(session).monthly_summary(whatsapp_number, year, month)
    return MonthlySummaryResponse(
        whatsapp_number=whatsapp_number,
        period_start=summary.period_start,
        period_end=summary.period_end,
        income_cents=summary.totals.income_cents,
        expense_cents=summary.totals.expense_cents,
        withdrawal_cents=summary.totals.withdrawal_cents,
        operating_result_cents=summary.totals.operating_result_cents,
        net_after_withdrawals_cents=summary.totals.net_after_withdrawals_cents,
    )


app.include_router(internal_api)


def _state_response(transaction) -> TransactionStateResponse:
    return TransactionStateResponse(
        transaction_id=transaction.id,
        transaction_type=transaction.transaction_type,
        amount_cents=transaction.amount_cents,
        description=transaction.description,
        occurred_at=transaction.occurred_at,
        reversed=transaction.reversed,
    )

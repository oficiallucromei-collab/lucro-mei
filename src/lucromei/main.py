import hmac
import json
import re
from datetime import UTC, datetime
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from lucromei.config import get_settings, on_cloud_run
from lucromei.conversation import ConversationService
from lucromei.db import get_session
from lucromei.domain import ParseError
from lucromei.growth import claim_reply, handle_growth_message
from lucromei.growth_api import router as growth_router
from lucromei.schemas import (
    CorrectionRequest,
    HealthResponse,
    InboundMessage,
    MonthlySummaryResponse,
    ReversalRequest,
    TransactionResponse,
    TransactionStateResponse,
)
from lucromei.service import LedgerError, LedgerService, MessageOwnerMismatch, TransactionNotFound
from lucromei.whatsapp import (
    CloudApiOutboundSender,
    InvalidWebhookSignature,
    MockOutboundSender,
    extract_text_messages,
    format_confirmation,
    verify_signature,
)

MAX_WEBHOOK_BYTES = 3 * 1024 * 1024
SIGNATURE_PATTERN = re.compile(r"sha256=[0-9a-f]{64}")

SessionDep = Annotated[Session, Depends(get_session)]

settings = get_settings()
app = FastAPI(
    title="LucroMEI API",
    openapi_url="/openapi.json" if settings.environment.strip().lower() == "local" else None,
    version="0.3.0",
    description="WhatsApp-first financial assistant for Brazilian microentrepreneurs.",
)
if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
    app.state.whatsapp_sender = CloudApiOutboundSender(
        access_token=settings.whatsapp_access_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        graph_api_version=settings.whatsapp_graph_api_version,
    )
else:
    app.state.whatsapp_sender = MockOutboundSender()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/webhooks/whatsapp")
def verify_whatsapp_webhook(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
) -> Response:
    verify_token = get_settings().whatsapp_verify_token
    if (
        mode != "subscribe"
        or not verify_token
        or verify_token == "change-me"
        or not hmac.compare_digest(token.encode(), verify_token.encode())
    ):
        raise HTTPException(status_code=403, detail="Token de verificação inválido.")
    return Response(
        content=challenge, media_type="text/plain", headers={"Cache-Control": "no-store"}
    )


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    session: SessionDep,
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> dict[str, int | bool]:
    if not signature or not SIGNATURE_PATTERN.fullmatch(signature):
        raise HTTPException(status_code=401, detail="Assinatura do webhook ausente ou inválida.")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload too large.")
        body.extend(chunk)
    try:
        verify_signature(bytes(body), signature, get_settings().whatsapp_app_secret)
    except InvalidWebhookSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON.") from exc
    messages = extract_text_messages(payload, settings.whatsapp_phone_number_id)
    processed = commands = duplicates = clarification_required = 0
    service = LedgerService(session)
    for message in messages:
        growth_settings = get_settings()
        if growth_settings.growth_enabled:
            try:
                duplicate = await run_in_threadpool(
                    process_growth_reply,
                    session,
                    growth_settings,
                    message,
                    app.state.whatsapp_sender,
                )
            except MessageOwnerMismatch:
                continue
            except IntegrityError:
                raise HTTPException(status_code=503, detail="Retry message.") from None
            commands += 1
            duplicates += int(duplicate)
            continue
        try:
            command = ConversationService(session).handle(
                message_id=message.message_id,
                whatsapp_number=message.sender,
                text=message.text,
            )
        except MessageOwnerMismatch:
            continue
        if command is not None:
            commands += 1
            duplicates += int(command.duplicate)
            await run_in_threadpool(
                app.state.whatsapp_sender.send_text, message.sender, command.reply
            )
            continue
        try:
            result = service.process(
                message_id=message.message_id,
                whatsapp_number=message.sender,
                text=message.text,
            )
        except MessageOwnerMismatch:
            continue
        except ParseError:
            clarification_required += 1
            await run_in_threadpool(
                app.state.whatsapp_sender.send_text,
                message.sender,
                "Não entendi se isso é entrada, despesa ou retirada. "
                "Envie uma operação por mensagem.",
            )
            continue
        processed += 1
        duplicates += int(result.duplicate)
        await run_in_threadpool(
            app.state.whatsapp_sender.send_text,
            message.sender,
            format_confirmation(
                result.transaction.transaction_type.value,
                result.transaction.amount_cents,
                result.duplicate,
            ),
        )
    return {
        "received": True,
        "processed": processed,
        "commands": commands,
        "duplicates": duplicates,
        "clarification_required": clarification_required,
        "ignored": max(0, len(messages) - processed - commands - clarification_required),
    }


def process_growth_reply(session, settings, message, sender):
    try:
        reply, duplicate = handle_growth_message(session, settings, message)
        session.commit()
        now = datetime.now(UTC)
        within_window = (
            message.timestamp is not None and -300 <= now.timestamp() - message.timestamp < 86400
        ) or (message.timestamp is None and settings.environment == "local")
        if reply and within_window and claim_reply(session, message, now):
            sender.send_text(message.sender, reply)
        session.commit()
        return duplicate
    except Exception:
        session.rollback()
        raise


def require_local_ledger_api() -> None:
    if on_cloud_run() or get_settings().environment.strip().lower() != "local":
        raise HTTPException(status_code=404, detail="Not found")


ledger_api = APIRouter(prefix="/v1", dependencies=[Depends(require_local_ledger_api)])


@ledger_api.post(
    "/messages", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED
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
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MessageOwnerMismatch as exc:
        raise HTTPException(status_code=409, detail="Message identifier conflict.") from exc
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


@ledger_api.post(
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


@ledger_api.post(
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


@ledger_api.get("/summaries/monthly", response_model=MonthlySummaryResponse)
def monthly_summary(
    whatsapp_number: str,
    session: SessionDep,
    year: int = Query(ge=2020, le=2100),
    month: int = Query(ge=1, le=12),
) -> MonthlySummaryResponse:
    summary = LedgerService(session).monthly_summary(whatsapp_number, year, month)
    return MonthlySummaryResponse(
        whatsapp_number=whatsapp_number,
        period_start=summary.period_start,
        period_end=summary.period_end,
        income_cents=summary.income_cents,
        expense_cents=summary.expense_cents,
        withdrawal_cents=summary.withdrawal_cents,
        operating_balance_cents=summary.operating_balance_cents,
    )


def _state_response(transaction) -> TransactionStateResponse:
    return TransactionStateResponse(
        transaction_id=transaction.id,
        transaction_type=transaction.transaction_type,
        amount_cents=transaction.amount_cents,
        description=transaction.description,
        occurred_at=transaction.occurred_at,
        reversed=transaction.reversed,
    )


app.include_router(ledger_api)

app.include_router(growth_router)

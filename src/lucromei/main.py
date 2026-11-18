from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from lucromei.config import get_settings
from lucromei.conversation import ConversationService
from lucromei.db import get_session
from lucromei.domain import ParseError
from lucromei.schemas import (
    CorrectionRequest,
    HealthResponse,
    InboundMessage,
    MonthlySummaryResponse,
    ReversalRequest,
    TransactionResponse,
    TransactionStateResponse,
)
from lucromei.service import LedgerError, LedgerService, TransactionNotFound
from lucromei.whatsapp import (
    InvalidWebhookSignature,
    MockOutboundSender,
    extract_text_messages,
    format_confirmation,
    verify_signature,
)

SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI(
    title="LucroMEI API",
    version="0.3.0",
    description="WhatsApp-first financial assistant for Brazilian microentrepreneurs.",
)
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
    if mode != "subscribe" or not hmac_compare(token, get_settings().whatsapp_verify_token):
        raise HTTPException(status_code=403, detail="Token de verificação inválido.")
    return Response(content=challenge, media_type="text/plain")


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    session: SessionDep,
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> dict[str, int | bool]:
    body = await request.body()
    try:
        verify_signature(body, signature, get_settings().whatsapp_app_secret)
    except InvalidWebhookSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    payload = await request.json()
    messages = extract_text_messages(payload)
    processed = commands = duplicates = clarification_required = 0
    service = LedgerService(session)
    for message in messages:
        command = ConversationService(session).handle(
            message_id=message.message_id,
            whatsapp_number=message.sender,
            text=message.text,
        )
        if command is not None:
            commands += 1
            duplicates += int(command.duplicate)
            app.state.whatsapp_sender.send_text(message.sender, command.reply)
            continue
        try:
            result = service.process(
                message_id=message.message_id,
                whatsapp_number=message.sender,
                text=message.text,
            )
        except ParseError:
            clarification_required += 1
            app.state.whatsapp_sender.send_text(
                message.sender,
                "Não entendi se isso é entrada, despesa ou retirada. "
                "Envie uma operação por mensagem.",
            )
            continue
        processed += 1
        duplicates += int(result.duplicate)
        app.state.whatsapp_sender.send_text(
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


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


@app.post("/v1/messages", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED)
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


@app.post(
    "/v1/transactions/{transaction_id}/corrections",
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


@app.post(
    "/v1/transactions/{transaction_id}/reversal",
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


@app.get("/v1/summaries/monthly", response_model=MonthlySummaryResponse)
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

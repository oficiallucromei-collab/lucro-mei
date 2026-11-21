import hmac
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

from lucromei.config import get_settings, is_configured_secret
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

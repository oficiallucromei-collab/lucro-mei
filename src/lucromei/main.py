from fastapi import FastAPI, HTTPException, status

from lucromei.domain import ParseError
from lucromei.schemas import HealthResponse, InboundMessage, TransactionResponse
from lucromei.service import ledger

app = FastAPI(
    title="LucroMEI API",
    version="0.1.0",
    description="WhatsApp-first financial assistant for Brazilian microentrepreneurs.",
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/v1/messages",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
def process_message(payload: InboundMessage) -> TransactionResponse:
    try:
        result = ledger.process(
            message_id=payload.message_id,
            whatsapp_number=payload.whatsapp_number,
            text=payload.text,
        )
    except ParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    transaction = result.transaction
    return TransactionResponse(
        message_id=result.message_id,
        transaction_type=transaction.transaction_type,
        amount_cents=transaction.amount_cents,
        description=transaction.description,
        occurred_at=transaction.occurred_at,
        duplicate=result.duplicate,
    )

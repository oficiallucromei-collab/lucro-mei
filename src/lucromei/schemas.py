from datetime import datetime

from pydantic import BaseModel, Field

from lucromei.domain import TransactionType


class HealthResponse(BaseModel):
    status: str


class InboundMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=255)
    whatsapp_number: str = Field(min_length=8, max_length=32)
    text: str = Field(min_length=1, max_length=2000)


class TransactionResponse(BaseModel):
    transaction_id: str
    message_id: str
    transaction_type: TransactionType
    amount_cents: int
    description: str
    occurred_at: datetime
    duplicate: bool = False


class CorrectionRequest(BaseModel):
    amount_cents: int | None = Field(default=None, gt=0)
    transaction_type: TransactionType | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)
    occurred_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)


class ReversalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class TransactionStateResponse(BaseModel):
    transaction_id: str
    transaction_type: TransactionType
    amount_cents: int
    description: str
    occurred_at: datetime
    reversed: bool


class MonthlySummaryResponse(BaseModel):
    whatsapp_number: str
    period_start: datetime
    period_end: datetime
    income_cents: int
    expense_cents: int
    withdrawal_cents: int
    operating_balance_cents: int
    disclaimer: str = (
        "O saldo operacional não considera obrigações futuras e não representa automaticamente "
        "lucro tributável ou valor seguro para retirada."
    )


class ErrorResponse(BaseModel):
    detail: str

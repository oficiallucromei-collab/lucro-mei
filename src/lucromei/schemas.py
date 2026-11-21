from datetime import datetime

from pydantic import BaseModel, Field

from lucromei.domain import TransactionType
from lucromei.money import MAX_AMOUNT_CENTS


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
    amount_cents: int | None = Field(default=None, gt=0, le=MAX_AMOUNT_CENTS)
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
    operating_result_cents: int = Field(description="Entradas menos despesas do negócio.")
    net_after_withdrawals_cents: int = Field(
        description="Resultado do negócio menos as retiradas pessoais."
    )
    disclaimer: str = (
        "Os valores não consideram obrigações futuras, reservas ou impostos e não representam "
        "automaticamente lucro tributável ou valor seguro para retirada."
    )


class ErrorResponse(BaseModel):
    detail: str

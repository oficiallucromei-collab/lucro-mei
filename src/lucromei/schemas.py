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
    message_id: str
    transaction_type: TransactionType
    amount_cents: int
    description: str
    occurred_at: datetime
    duplicate: bool = False


class ErrorResponse(BaseModel):
    detail: str

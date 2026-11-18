from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    whatsapp_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    transaction: Mapped["Transaction"] = relationship(back_populates="source_message")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="ck_transactions_positive_amount"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("inbound_messages.id"), unique=True)
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    source_message: Mapped[InboundMessage] = relationship(back_populates="transaction")
    events: Mapped[list["TransactionEvent"]] = relationship(
        back_populates="transaction",
        order_by="TransactionEvent.created_at, TransactionEvent.id",
        cascade="all, delete-orphan",
    )


class TransactionEvent(Base):
    __tablename__ = "transaction_events"
    __table_args__ = (Index("ix_transaction_events_transaction_id", "transaction_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    transaction: Mapped[Transaction] = relationship(back_populates="events")

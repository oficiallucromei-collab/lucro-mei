from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    display_name: Mapped[str | None] = mapped_column(String(60))
    business_type: Mapped[str | None] = mapped_column(String(80))
    # new -> awaiting_name -> awaiting_business_type -> awaiting_terms -> active.
    # Nothing is written to the ledger before "active", which requires accepting the terms.
    onboarding_step: Mapped[str] = mapped_column(
        String(30), nullable=False, default="new", server_default="new"
    )
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TermsAcceptance(Base):
    """Proof that a business accepted a given version of the terms. Append-only."""

    __tablename__ = "terms_acceptances"
    __table_args__ = (
        UniqueConstraint("business_id", "terms_version", name="uq_terms_acceptances_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The WhatsApp message in which the user answered, and what they wrote.
    source_message_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    accepted_text: Mapped[str] = mapped_column(String(100), nullable=False)


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), index=True)
    command_type: Mapped[str] = mapped_column(String(20), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
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


# SQLite only auto-increments INTEGER PRIMARY KEY columns; PostgreSQL gets a BIGINT identity.
_SEQUENCE_ID = BigInteger().with_variant(Integer, "sqlite")


class InboundEvent(Base):
    """A WhatsApp message exactly as received, recorded before any processing.

    The Meta message ID is the idempotency key: a redelivered webhook finds the existing row and
    is ignored. `id` grows in arrival order and breaks ties between messages sent in the same
    second, so each sender's messages are processed in order.
    """

    __tablename__ = "inbound_events"
    __table_args__ = (Index("ix_inbound_events_sender_status", "sender", "status"),)
    id: Mapped[int] = mapped_column(_SEQUENCE_ID, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    sender: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # received -> processed, or failed when processing gave up.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundMessage(Base):
    """A reply waiting to be, or already, sent. At most one reply per inbound message."""

    __tablename__ = "outbound_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient: Mapped[str] = mapped_column(String(32), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), unique=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # queued -> sent, or failed when sending gave up.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    provider_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    """Background work stored in PostgreSQL and claimed with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # pending -> running -> done, or dead after the last failed attempt.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Error class and code only: never message text or amounts.
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryStatusEvent(Base):
    """A delivery receipt from WhatsApp (sent, delivered, read, failed) for a message we sent.

    Append-only: a receipt can arrive before the send job has stored the WhatsApp message ID, so
    receipts are kept on their own and joined by provider_message_id when needed.
    """

    __tablename__ = "whatsapp_status_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_message_id", "status", name="uq_whatsapp_status_events_message_status"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

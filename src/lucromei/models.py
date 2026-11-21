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
from sqlalchemy import text as sql_text
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
    # Set when the user answered PARAR LEMBRETES. Service notices (trial end) still go out.
    reminders_opt_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    media_id: Mapped[str | None] = mapped_column(String(64))
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
    # Set for proactive notices sent outside the 24-hour window: an approved Meta template and
    # its parameters. `text` then holds the rendered wording, for support and MEUS DADOS.
    template_name: Mapped[str | None] = mapped_column(String(64))
    template_language: Mapped[str | None] = mapped_column(String(10))
    template_params: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    # A document built when the message is sent, e.g. {"kind": "ledger_csv", "year", "month"};
    # `text` is then its caption. The file itself is never stored.
    attachment: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
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
    # Correlation ID of the request that created the job, restored while the job runs.
    correlation_id: Mapped[str | None] = mapped_column(String(64))
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


class PendingEntry(Base):
    """A transaction waiting for the user to confirm its type before it is recorded.

    Nothing reaches the ledger until the user answers. At most one entry per business is open
    at a time; a newer ambiguous message replaces the older question.
    """

    __tablename__ = "pending_entries"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_pending_entries_positive_amount"),
        Index(
            "uq_pending_entries_one_open_per_business",
            "business_id",
            unique=True,
            postgresql_where=sql_text("status = 'open'"),
            sqlite_where=sql_text("status = 'open'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candidate_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # "rules" (deterministic parser) or "ai" (AI fallback, always confirmed by the user).
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="rules")
    # "single" (one entry, the user picks its type) or "batch" (several entries from one
    # message, previewed in `items` and recorded together after SIM).
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="single", server_default="single"
    )
    # Batch only: [{"transaction_type", "amount_cents", "description", "occurred_at"}, ...].
    # Erased with the other message text after the retention period.
    items: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True))
    # open -> resolved | cancelled | expired | replaced
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_message_id: Mapped[str | None] = mapped_column(String(255))
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))


class AIUsage(Base):
    """One call to the AI fallback: tokens, cost and outcome. Never the message text."""

    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_business_created", "business_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    inbound_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Millionths of a US dollar, rounded up. Integer, like every other amount.
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # understood | not_a_transaction | rejected | refused | error
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeletionRequest(Base):
    """A user's request to erase all their data (LGPD), and the proof that it was carried out.

    No foreign key and no phone number: the row outlives the business it describes.
    `subject_digest` is an HMAC of the WhatsApp number, enough to show that a given person's
    request was handled without keeping the number itself.
    """

    __tablename__ = "deletion_requests"
    __table_args__ = (
        Index("ix_deletion_requests_status_scheduled", "status", "scheduled_for"),
        Index("ix_deletion_requests_business", "business_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subject_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # awaiting_confirmation -> scheduled -> completed, or expired / cancelled
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirm_by: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(Base):
    """A business's paid plan after the free trial. One row per subscription attempt.

    requested -> pending_payment -> active -> past_due / canceled / expired. Only "active" with
    a current_period_end in the future gives access after the trial. Status changes are driven
    by the payment provider's signed webhooks or by an operator ("manual").
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_business_status", "business_id", "status"),
        CheckConstraint("price_cents >= 0", name="ck_subscriptions_price"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # none (no provider yet) | stripe | manual
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    provider_customer_id: Mapped[str | None] = mapped_column(String(255))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_checkout_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

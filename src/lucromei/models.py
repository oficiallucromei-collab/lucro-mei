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


class Acquisition(Base):
    __tablename__ = "acquisitions"
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    segment: Mapped[str | None] = mapped_column(String(32))
    trial_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_count: Mapped[int] = mapped_column(Integer)
    activation_hours: Mapped[int] = mapped_column(Integer)
    last_inbound: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    stripe_customer: Mapped[str | None] = mapped_column(String(255), unique=True)
    checkout_id: Mapped[str | None] = mapped_column(String(255))
    subscription_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32))
    paid_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recurring_cents: Mapped[int | None] = mapped_column(Integer)


class AcquisitionClick(Base):
    __tablename__ = "acquisition_clicks"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSON)
    claimed: Mapped[bool] = mapped_column(default=False)


class GrowthEvent(Base):
    __tablename__ = "growth_events"
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    amount_cents: Mapped[int | None] = mapped_column(Integer)


class CampaignDay(Base):
    __tablename__ = "campaign_days"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    campaign: Mapped[str] = mapped_column(String(80), primary_key=True)
    spend_cents: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    clicks: Mapped[int] = mapped_column(Integer)


class LandingDay(Base):
    __tablename__ = "landing_days"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    visits: Mapped[int] = mapped_column(Integer)


class GrowthAudit(Base):
    __tablename__ = "growth_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(String(50))
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TrialHold(Base):
    __tablename__ = "trial_holds"
    identity_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class GrowthRateBucket(Base):
    __tablename__ = "growth_rate_buckets"
    route: Mapped[str] = mapped_column(String(20), primary_key=True)
    minute: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)

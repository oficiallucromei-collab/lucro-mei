"""Background handlers: turn a received WhatsApp message into a reply, then send the reply.

Processing and sending are separate jobs so that a WhatsApp outage only retries the send and
never re-runs the ledger write. Each step is idempotent:

- process_message runs at most once per inbound event (the event row is locked and marked
  processed in the same transaction as the ledger write and the queued reply);
- a reply is unique per inbound message, so it can never be queued twice;
- send_message only sends replies that are still queued.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from lucromei import ai, clarification, onboarding
from lucromei.config import get_settings
from lucromei.conversation import ConversationService
from lucromei.domain import AmbiguousTransaction, ParseError
from lucromei.jobs import JobContext, PermanentJobError, RetryLater, enqueue, insert_ignore
from lucromei.models import (
    AIUsage,
    Business,
    DeliveryStatusEvent,
    InboundEvent,
    OutboundMessage,
    new_id,
)
from lucromei.service import LedgerService
from lucromei.whatsapp import (
    OutboundSender,
    WhatsAppSendError,
    WhatsAppStatus,
    format_confirmation,
)

logger = logging.getLogger(__name__)

PROCESS_MESSAGE = "process_message"
SEND_MESSAGE = "send_message"

NOT_UNDERSTOOD_REPLY = (
    "Não entendi se isso é entrada, despesa ou retirada. Envie uma operação por mensagem."
)
TEXT_ONLY_REPLY = (
    "Por enquanto eu só entendo mensagens de texto. "
    "Envie, por exemplo: Recebi 150 por um corte."
)
# How long to wait before re-checking when an earlier message from the same sender is pending.
ORDERING_RETRY_SECONDS = 2.0


def enqueue_inbound_processing(session: Session, external_id: str, *, max_attempts: int) -> bool:
    return enqueue(
        session,
        PROCESS_MESSAGE,
        {"external_id": external_id},
        dedupe_key=f"{PROCESS_MESSAGE}:{external_id}",
        max_attempts=max_attempts,
    )


def handle_process_message(session: Session, payload: dict[str, Any], ctx: JobContext) -> None:
    event = session.scalar(
        select(InboundEvent)
        .where(InboundEvent.external_id == payload["external_id"])
        .with_for_update()
    )
    if event is None:
        raise PermanentJobError("inbound_event_missing")
    if event.status != "received":
        return  # Already handled by an earlier run of this job.
    if _has_earlier_pending_message(session, event):
        # Conversations are stateful (onboarding, clarifications): keep each sender in order.
        raise RetryLater(ORDERING_RETRY_SECONDS)

    reply = build_reply(session, event, ctx)
    queue_reply(
        session,
        recipient=event.sender,
        in_reply_to=event.external_id,
        text=reply,
        max_attempts=ctx.services["max_attempts"],
        now=ctx.now,
    )
    event.status = "processed"
    event.processed_at = ctx.now


def build_reply(session: Session, event: InboundEvent, ctx: JobContext) -> str:
    if event.text is None:
        return TEXT_ONLY_REPLY
    ledger = LedgerService(session)
    business = ledger.get_or_create_business(event.sender)
    # Serialize conversation state changes for this business across workers.
    session.refresh(business, with_for_update=True)
    if not onboarding.is_active(business):
        return onboarding.handle(
            session,
            business,
            message_id=event.external_id,
            text=event.text,
            now=ctx.now,
            settings=get_settings(),
        )
    settings = get_settings()
    pending, expired = clarification.current_pending(session, business, ctx.now)
    if pending is not None:
        answer = clarification.parse_answer(event.text, clarification.candidates_of(pending))
        if answer is clarification.CANCEL:
            clarification.close(
                pending, clarification.CANCELLED, ctx.now, resolution_message_id=event.external_id
            )
            return clarification.CANCELLED_REPLY
        if answer is not None:
            transaction = ledger.record_pending(
                pending, answer, resolution_message_id=event.external_id
            )
            clarification.close(
                pending,
                clarification.RESOLVED,
                ctx.now,
                resolution_message_id=event.external_id,
                transaction_id=transaction.id,
            )
            return format_confirmation(transaction.transaction_type.value, transaction.amount_cents)
    elif expired is not None and clarification.looks_like_answer(event.text):
        return clarification.EXPIRED_REPLY

    command = ConversationService(session).handle(
        message_id=event.external_id,
        whatsapp_number=event.sender,
        text=event.text,
        now=ctx.now,
        commit=False,
    )
    if command is not None:
        return command.reply
    try:
        result = ledger.process(
            message_id=event.external_id,
            whatsapp_number=event.sender,
            text=event.text,
            occurred_at=event.sent_at,
            commit=False,
        )
    except AmbiguousTransaction as ambiguity:
        new_pending = clarification.create_pending(
            session,
            business,
            source_message_id=event.external_id,
            source_text=event.text,
            amount_cents=ambiguity.amount_cents,
            description=ambiguity.description,
            occurred_at=event.sent_at,
            candidates=ambiguity.candidates,
            origin="rules",
            now=ctx.now,
            ttl=timedelta(hours=settings.pending_entry_ttl_hours),
        )
        return clarification.question(new_pending)
    except ParseError:
        if pending is not None:
            return "Não entendi a resposta. " + clarification.question(pending)
        return ai_fallback(session, business, event, ctx) or NOT_UNDERSTOOD_REPLY
    return format_confirmation(
        result.transaction.transaction_type.value, result.transaction.amount_cents
    )


def ai_fallback(
    session: Session, business: Business, event: InboundEvent, ctx: JobContext
) -> str | None:
    """Ask the AI to read a message the rules could not. Returns a confirmation question.

    The suggestion becomes a pending entry: nothing is recorded until the user answers SIM.
    """

    interpreter = ctx.services.get("interpreter")
    settings = get_settings()
    text = event.text or ""
    if interpreter is None or len(text) > settings.ai_max_input_chars:
        return None
    since = ctx.now - timedelta(days=1)
    calls_today = session.scalar(
        select(func.count(AIUsage.id)).where(
            AIUsage.business_id == business.id, AIUsage.created_at >= since
        )
    )
    if calls_today >= settings.ai_daily_calls_per_business:
        logger.warning("ai_daily_limit_reached", extra={"business_id": business.id})
        return None

    result = interpreter.interpret(text)
    session.add(
        AIUsage(
            business_id=business.id,
            inbound_message_id=event.external_id,
            model=result.model[:64],
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_micro_usd=ai.cost_micro_usd(
                result.input_tokens,
                result.output_tokens,
                input_price=settings.ai_input_price_micro_usd_per_mtok,
                output_price=settings.ai_output_price_micro_usd_per_mtok,
            ),
            latency_ms=result.latency_ms,
            outcome=result.outcome,
            created_at=ctx.now,
        )
    )
    logger.info(
        "ai_fallback_used",
        extra={"outcome": result.outcome, "latency_ms": result.latency_ms},
    )
    if result.outcome != ai.UNDERSTOOD or result.suggestion is None:
        return None
    pending = clarification.create_pending(
        session,
        business,
        source_message_id=event.external_id,
        source_text=text,
        amount_cents=result.suggestion.amount_cents,
        description=_description_from_user_text(text),
        occurred_at=event.sent_at,
        candidates=result.suggestion.candidates,
        origin="ai",
        now=ctx.now,
        ttl=timedelta(hours=settings.pending_entry_ttl_hours),
    )
    return clarification.question(pending)


def _description_from_user_text(text: str) -> str:
    """The user's own words, never the model's: the AI cannot inject text into the ledger."""

    return " ".join(text.lower().split())[:120] or "lançamento"


def queue_reply(
    session: Session,
    *,
    recipient: str,
    in_reply_to: str | None,
    text: str,
    max_attempts: int,
    now: datetime,
) -> None:
    outbound_id = new_id()
    created = insert_ignore(
        session,
        OutboundMessage,
        {
            "id": outbound_id,
            "recipient": recipient,
            "in_reply_to": in_reply_to,
            "text": text,
            "status": "queued",
            "created_at": now,
        },
    )
    if not created:
        return  # This message already has a reply.
    enqueue(
        session,
        SEND_MESSAGE,
        {"outbound_id": outbound_id},
        dedupe_key=f"{SEND_MESSAGE}:{outbound_id}",
        max_attempts=max_attempts,
        run_after=now,
    )


def handle_send_message(session: Session, payload: dict[str, Any], ctx: JobContext) -> None:
    outbound = session.scalar(
        select(OutboundMessage)
        .where(OutboundMessage.id == payload["outbound_id"])
        .with_for_update()
    )
    if outbound is None:
        raise PermanentJobError("outbound_message_missing")
    if outbound.status != "queued":
        return
    sender: OutboundSender = ctx.services["sender"]
    try:
        provider_message_id = sender.send_text(outbound.recipient, outbound.text)
    except WhatsAppSendError as exc:
        # The transaction is rolled back on failure; the code is kept on the job and copied to
        # the message by mark_outbound_failed if sending gives up.
        if not exc.retryable:
            raise PermanentJobError(exc.code) from exc
        raise
    outbound.status = "sent"
    outbound.provider_message_id = provider_message_id
    outbound.error_code = None
    outbound.sent_at = ctx.now


def mark_event_failed(session: Session, payload: dict[str, Any], error: str) -> None:
    event = session.scalar(
        select(InboundEvent).where(InboundEvent.external_id == payload.get("external_id"))
    )
    if event is not None and event.status == "received":
        event.status = "failed"


def mark_outbound_failed(session: Session, payload: dict[str, Any], error: str) -> None:
    outbound = session.get(OutboundMessage, payload.get("outbound_id"))
    if outbound is not None and outbound.status == "queued":
        outbound.status = "failed"
        # "WhatsAppSendError:http_503" -> "http_503"
        outbound.error_code = error.split(":", 1)[-1][:64]


HANDLERS = {PROCESS_MESSAGE: handle_process_message, SEND_MESSAGE: handle_send_message}
DEAD_HOOKS = {PROCESS_MESSAGE: mark_event_failed, SEND_MESSAGE: mark_outbound_failed}


def _has_earlier_pending_message(session: Session, event: InboundEvent) -> bool:
    earlier = or_(
        InboundEvent.sent_at < event.sent_at,
        and_(InboundEvent.sent_at == event.sent_at, InboundEvent.id < event.id),
    )
    # Messages more than a day older are not waited for: a stuck message must not block the
    # sender forever.
    horizon = event.sent_at - timedelta(days=1)
    return (
        session.scalar(
            select(InboundEvent.id)
            .where(
                InboundEvent.sender == event.sender,
                InboundEvent.status == "received",
                InboundEvent.id != event.id,
                InboundEvent.sent_at >= horizon,
                earlier,
            )
            .limit(1)
        )
        is not None
    )


def record_delivery_statuses(
    session: Session, statuses: list[WhatsAppStatus], now: datetime
) -> int:
    """Store delivery receipts once each (Meta also redelivers these). Does not commit."""

    recorded = 0
    for status in statuses:
        recorded += insert_ignore(
            session,
            DeliveryStatusEvent,
            {
                "id": new_id(),
                "provider_message_id": status.provider_message_id,
                "status": status.status,
                "error_code": status.error_code,
                "occurred_at": status.occurred_at,
                "received_at": now,
            },
        )
        if status.status == "failed":
            logger.warning("whatsapp_delivery_failed", extra={"error": status.error_code})
    return recorded


# A message can be both delivered and read; the most advanced receipt wins. Failed is final.
_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}


def delivery_status(session: Session, provider_message_id: str) -> str | None:
    """The most advanced delivery receipt received for a message we sent, if any."""

    statuses = session.scalars(
        select(DeliveryStatusEvent.status).where(
            DeliveryStatusEvent.provider_message_id == provider_message_id
        )
    ).all()
    return max(statuses, key=_STATUS_RANK.__getitem__, default=None)

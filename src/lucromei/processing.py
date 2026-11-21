"""Background handlers: turn a received WhatsApp message into a reply, then send the reply.

Processing and sending are separate jobs so that a WhatsApp outage only retries the send and
never re-runs the ledger write. Each step is idempotent:

- process_message runs at most once per inbound event (the event row is locked and marked
  processed in the same transaction as the ledger write and the queued reply);
- a reply is unique per inbound message, so it can never be queued twice;
- send_message only sends replies that are still queued.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from lucromei.conversation import ConversationService
from lucromei.domain import ParseError
from lucromei.jobs import JobContext, PermanentJobError, RetryLater, enqueue, insert_ignore
from lucromei.models import InboundEvent, OutboundMessage, new_id
from lucromei.service import LedgerService
from lucromei.whatsapp import OutboundSender, WhatsAppSendError, format_confirmation

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
        result = LedgerService(session).process(
            message_id=event.external_id,
            whatsapp_number=event.sender,
            text=event.text,
            occurred_at=event.sent_at,
            commit=False,
        )
    except ParseError:
        return NOT_UNDERSTOOD_REPLY
    return format_confirmation(
        result.transaction.transaction_type.value, result.transaction.amount_cents
    )


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

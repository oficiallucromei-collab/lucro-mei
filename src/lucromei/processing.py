"""Background handlers: turn a received WhatsApp message into a reply, then send the reply.

Processing and sending are separate jobs so that a WhatsApp outage only retries the send and
never re-runs the ledger write. Each step is idempotent:

- process_message runs at most once per inbound event (the event row is locked and marked
  processed in the same transaction as the ledger write and the queued reply);
- a reply is unique per inbound message, so it can never be queued twice;
- send_message only sends replies that are still queued.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from lucromei import ai, clarification, onboarding, privacy
from lucromei.audio import AudioError
from lucromei.config import get_business_timezone, get_settings
from lucromei.conversation import ConversationService
from lucromei.domain import AmbiguousTransaction, InvalidDate, ParseError
from lucromei.jobs import JobContext, PermanentJobError, RetryLater, enqueue, insert_ignore, utc_now
from lucromei.models import (
    AIUsage,
    Business,
    DeliveryStatusEvent,
    InboundEvent,
    OutboundMessage,
    new_id,
)
from lucromei.service import EffectiveTransaction, LedgerService
from lucromei.whatsapp import (
    OutboundSender,
    WhatsAppSendError,
    WhatsAppStatus,
    format_confirmation,
)

logger = logging.getLogger(__name__)

PROCESS_MESSAGE = "process_message"
TRANSCRIBE_AUDIO = "transcribe_audio"
AUDIO_CLARIFY_REPLY = (
    "Não consegui entender seu áudio. Pode tentar novamente ou enviar a informação por texto?"
)
AUDIO_ONBOARDING_REPLY = "Conclua o cadastro por texto para poder enviar áudios."
AUDIO_SIZE_REPLY = "Seu áudio ficou muito grande. Envie um áudio menor ou uma mensagem de texto."
AUDIO_FORMAT_REPLY = "Esse formato de áudio não é compatível. Grave um áudio pelo WhatsApp."
AUDIO_ATTEMPTS = 3
SEND_MESSAGE = "send_message"
MAINTENANCE = "maintenance"
DELETE_ACCOUNT = "delete_account"

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


def enqueue_audio_transcription(session: Session, external_id: str) -> None:
    enqueue(
        session,
        TRANSCRIBE_AUDIO,
        {"external_id": external_id},
        dedupe_key=f"{TRANSCRIBE_AUDIO}:{external_id}",
        max_attempts=AUDIO_ATTEMPTS,
    )


def handle_audio_transcription(session: Session, payload: dict[str, Any], ctx: JobContext) -> None:
    event = session.scalar(
        select(InboundEvent)
        .where(InboundEvent.external_id == payload["external_id"])
        .with_for_update()
    )
    if event is None or event.status != "received" or event.text is not None:
        return
    if _has_earlier_pending_message(session, event):
        raise RetryLater(ORDERING_RETRY_SECONDS)

    transcriber = ctx.services.get("transcriber")
    if transcriber is None:
        _reply_to_audio_failure(session, event, TEXT_ONLY_REPLY, ctx.now)
        return
    business = session.scalar(select(Business).where(Business.whatsapp_number == event.sender))
    if not onboarding.is_active(business):
        _reply_to_audio_failure(session, event, AUDIO_ONBOARDING_REPLY, ctx.now)
        return

    try:
        if event.media_id is None:
            raise AudioError("missing_media_id")
        result = transcriber.transcribe(event.media_id)
    except AudioError as exc:
        logger.warning("audio_processing_failed", extra={"error": exc.code})
        if exc.retryable:
            raise
        reply = {
            "audio_too_large": AUDIO_SIZE_REPLY,
            "unsupported_audio": AUDIO_FORMAT_REPLY,
        }.get(exc.code, AUDIO_CLARIFY_REPLY)
        if exc.code == "audio_too_long":
            limit = get_settings().whatsapp_audio_max_duration_seconds
            unit = "segundo" if limit == 1 else "segundos"
            reply = (
                "Seu áudio ficou um pouco longo. "
                f"Envie um áudio de até {limit} {unit}, por favor."
            )
        _reply_to_audio_failure(session, event, reply, ctx.now)
        return

    event.text = result.text
    event.media_id = None
    enqueue_inbound_processing(
        session, event.external_id, max_attempts=ctx.services["max_attempts"]
    )


def _reply_to_audio_failure(
    session: Session, event: InboundEvent, reply: str, now: datetime
) -> None:
    event.media_id = None
    event.status = "failed"
    event.processed_at = now
    queue_reply(
        session,
        recipient=event.sender,
        in_reply_to=event.external_id,
        text=reply,
        max_attempts=get_settings().job_max_attempts,
        now=now,
    )


def mark_audio_failed(session: Session, payload: dict[str, Any], error: str) -> None:
    event = session.scalar(
        select(InboundEvent)
        .where(InboundEvent.external_id == payload["external_id"])
        .with_for_update()
    )
    if event is not None and event.status == "received":
        _reply_to_audio_failure(session, event, AUDIO_CLARIFY_REPLY, utc_now())


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
    parts = [reply] if isinstance(reply, str) else reply
    for index, text in enumerate(parts, 1):
        queue_reply(
            session,
            recipient=event.sender,
            in_reply_to=_part_key(event.external_id, index),
            text=text,
            max_attempts=ctx.services["max_attempts"],
            now=ctx.now,
        )
    event.status = "processed"
    event.processed_at = ctx.now


def build_reply(session: Session, event: InboundEvent, ctx: JobContext) -> str | list[str]:
    """The reply to one message: a single text, or several parts sent in order."""
    if event.text is None:
        return TEXT_ONLY_REPLY
    ledger = LedgerService(session)
    business = ledger.get_or_create_business(event.sender)
    # Serialize conversation state changes for this business across workers.
    session.refresh(business, with_for_update=True)
    privacy_reply = privacy.handle(
        session, business, text=event.text, now=ctx.now, settings=get_settings()
    )
    if privacy_reply is not None:
        return privacy_reply.parts
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
            return _confirmation(transaction, event.sent_at)
    elif expired is not None and clarification.looks_like_answer(event.text):
        return clarification.EXPIRED_REPLY

    command = ConversationService(session).handle(
        message_id=event.external_id,
        whatsapp_number=event.sender,
        text=event.text,
        # Periods ("hoje", "essa semana") are relative to when the user sent the message.
        now=_aware(event.sent_at),
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
            retain_source_text=event.message_type != "audio",
        )
    except AmbiguousTransaction as ambiguity:
        new_pending = clarification.create_pending(
            session,
            business,
            source_message_id=event.external_id,
            source_text=event.text if event.message_type != "audio" else "",
            amount_cents=ambiguity.amount_cents,
            description=ambiguity.description,
            occurred_at=ambiguity.occurred_at or event.sent_at,
            candidates=ambiguity.candidates,
            origin="rules",
            now=ctx.now,
            ttl=timedelta(hours=settings.pending_entry_ttl_hours),
        )
        return clarification.question(new_pending)
    except InvalidDate as exc:
        return f"{exc} Nada foi anotado."
    except ParseError:
        if pending is not None:
            return "Não entendi a resposta. " + clarification.question(pending)
        return ai_fallback(session, business, event, ctx) or NOT_UNDERSTOOD_REPLY
    return _confirmation(result.transaction, event.sent_at)


def _confirmation(transaction: EffectiveTransaction, sent_at: datetime) -> str:
    """Confirm an entry, naming its day when it is not the day the message was sent."""
    tz = get_business_timezone()
    day = _aware(transaction.occurred_at).astimezone(tz).date()
    sent_day = _aware(sent_at).astimezone(tz).date()
    return format_confirmation(
        transaction.transaction_type.value,
        transaction.amount_cents,
        occurred_on=day if day != sent_day else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
        source_text=text if event.message_type != "audio" else "",
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
    if (previous := _previous_part(session, outbound)) is not None and previous.status == "queued":
        raise RetryLater(1.0)  # parts of a long reply go out in order
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


def _part_key(external_id: str, index: int) -> str:
    """Reply key: the inbound message ID for the first part, "<id>#<n>" for the next ones."""
    return external_id if index == 1 else f"{external_id}#{index}"


def _previous_part(session: Session, outbound: OutboundMessage) -> OutboundMessage | None:
    key = outbound.in_reply_to or ""
    base, separator, index = key.rpartition("#")
    if not separator or not index.isdigit() or int(index) < 2:
        return None
    previous_key = base if int(index) == 2 else f"{base}#{int(index) - 1}"
    return session.scalar(
        select(OutboundMessage).where(OutboundMessage.in_reply_to == previous_key)
    )


def enqueue_maintenance(session: Session, now: datetime, *, interval_seconds: int) -> bool:
    """Schedule this period's maintenance once, however many workers ask for it."""
    bucket = int(now.timestamp()) // interval_seconds
    return enqueue(
        session,
        MAINTENANCE,
        {},
        dedupe_key=f"{MAINTENANCE}:{interval_seconds}:{bucket}",
        max_attempts=3,
        run_after=now,
    )


def handle_maintenance(session: Session, payload: dict[str, Any], ctx: JobContext) -> None:
    settings = get_settings()
    for request_id in privacy.due_deletions(session, ctx.now):
        enqueue(
            session,
            DELETE_ACCOUNT,
            {"deletion_request_id": request_id},
            dedupe_key=f"{DELETE_ACCOUNT}:{request_id}",
            max_attempts=ctx.services["max_attempts"],
            run_after=ctx.now,
        )
    purged = privacy.purge_old_text(session, ctx.now, settings)
    summary = ",".join(f"{table}={count}" for table, count in purged.items())
    logger.info("retention_purge", extra={"outcome": summary})


def handle_delete_account(session: Session, payload: dict[str, Any], ctx: JobContext) -> None:
    privacy.execute_deletion(session, payload["deletion_request_id"], ctx.now)


def mark_event_failed(session: Session, payload: dict[str, Any], error: str) -> None:
    event = session.scalar(
        select(InboundEvent).where(InboundEvent.external_id == payload.get("external_id"))
    )
    if event is not None and event.status == "received":
        if event.message_type == "audio":
            _reply_to_audio_failure(session, event, AUDIO_CLARIFY_REPLY, utc_now())
        else:
            event.status = "failed"


def mark_outbound_failed(session: Session, payload: dict[str, Any], error: str) -> None:
    outbound = session.get(OutboundMessage, payload.get("outbound_id"))
    if outbound is not None and outbound.status == "queued":
        outbound.status = "failed"
        # "WhatsAppSendError:http_503" -> "http_503"
        outbound.error_code = error.split(":", 1)[-1][:64]


HANDLERS = {
    PROCESS_MESSAGE: handle_process_message,
    TRANSCRIBE_AUDIO: handle_audio_transcription,
    SEND_MESSAGE: handle_send_message,
    MAINTENANCE: handle_maintenance,
    DELETE_ACCOUNT: handle_delete_account,
}
DEAD_HOOKS = {
    PROCESS_MESSAGE: mark_event_failed,
    TRANSCRIBE_AUDIO: mark_audio_failed,
    SEND_MESSAGE: mark_outbound_failed,
}


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

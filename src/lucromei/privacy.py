"""LGPD: let users see and erase their data, and keep raw message text only for a while.

- "meus dados" returns everything stored about the user, over WhatsApp.
- "apagar meus dados" asks for an explicit confirmation (APAGAR), then blocks the account for a
  grace period during which the user can cancel; after it, every row tied to the user is
  deleted. Only an anonymous proof remains (an HMAC of the number and the dates).
- Raw message text (received messages, sent replies, command receipts, closed questions) is
  erased after the retention period. Ledger entries, including the text that created them,
  stay while the account exists: they are the audit trail the user's summaries come from.
"""

import hashlib
import hmac
import logging
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from lucromei.config import Settings, get_business_timezone
from lucromei.models import (
    AIUsage,
    Business,
    CommandReceipt,
    DeletionRequest,
    DeliveryStatusEvent,
    InboundEvent,
    InboundMessage,
    Job,
    OutboundMessage,
    PendingEntry,
    Subscription,
    TermsAcceptance,
    Transaction,
    TransactionEvent,
)
from lucromei.money import format_brl
from lucromei.service import effective_transaction
from lucromei.subscription import export_lines as subscription_export_lines

logger = logging.getLogger(__name__)

AWAITING_CONFIRMATION = "awaiting_confirmation"
SCHEDULED = "scheduled"
COMPLETED = "completed"
EXPIRED = "expired"
CANCELLED = "cancelled"

EXPORT_COMMANDS = {
    "meus dados",
    "ver meus dados",
    "exportar meus dados",
    "exportar dados",
    "baixar meus dados",
    "quais dados voce tem",
    "quais dados voces tem",
}
DELETE_COMMANDS = {
    "apagar meus dados",
    "excluir meus dados",
    "deletar meus dados",
    "apagar minha conta",
    "excluir minha conta",
    "deletar minha conta",
    "apagar tudo",
}
CONFIRM_DELETE = "apagar"
CANCEL_DELETE_COMMANDS = {
    "cancelar exclusao",
    "cancelar a exclusao",
    "nao apagar",
    "nao apague",
    "manter minha conta",
    "desistir",
}
# One WhatsApp message holds 4096 characters; leave room for the "(parte x/y)" header.
MAX_PART_LENGTH = 3500
MAX_EXPORT_PARTS = 10
TYPE_LABELS = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada pessoal"}


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(without_accents.split()).strip(" .!?")


def subject_digest(whatsapp_number: str, key: str) -> str:
    return hmac.new(key.encode(), whatsapp_number.encode(), hashlib.sha256).hexdigest()


# --- conversation -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrivacyReply:
    parts: list[str]


def handle(
    session: Session, business: Business, *, text: str, now: datetime, settings: Settings
) -> PrivacyReply | None:
    """Handle privacy commands and the deletion flow. None if the message is not for us.

    Runs with the business row locked, before onboarding and the ledger, so it also works for
    users who never finished onboarding, and so a scheduled deletion blocks everything else.
    """

    command = normalize(text)
    request = _latest_request(session, business.id)

    if request is not None and request.status == AWAITING_CONFIRMATION:
        if _aware(request.confirm_by) > now and command == CONFIRM_DELETE:
            request.status = SCHEDULED
            request.confirmed_at = now
            request.scheduled_for = now + timedelta(days=settings.deletion_grace_days)
            return PrivacyReply([_scheduled_message(request.scheduled_for)])
        request.status = EXPIRED  # any other answer, or a late one, drops the request

    if command in EXPORT_COMMANDS:
        return PrivacyReply(export_parts(session, business, now, settings))

    if request is not None and request.status == SCHEDULED:
        if command in CANCEL_DELETE_COMMANDS:
            request.status = CANCELLED
            request.cancelled_at = now
            return PrivacyReply(["Pronto, cancelei a exclusão. Sua conta continua ativa."])
        return PrivacyReply([_blocked_message(request.scheduled_for)])

    if command in DELETE_COMMANDS:
        session.add(
            DeletionRequest(
                business_id=business.id,
                subject_digest=subject_digest(business.whatsapp_number, _hash_key(settings)),
                status=AWAITING_CONFIRMATION,
                requested_at=now,
                confirm_by=now + timedelta(minutes=settings.deletion_confirm_minutes),
            )
        )
        return PrivacyReply(
            [
                "Você pediu para apagar seus dados. Isso remove seu cadastro, todos os "
                "lançamentos e o histórico de mensagens, e não tem como desfazer depois.\n\n"
                f"A exclusão acontece {settings.deletion_grace_days} dias depois da "
                "confirmação; até lá sua conta fica bloqueada e você pode desistir.\n\n"
                "Se quiser uma cópia antes, mande MEUS DADOS.\n\n"
                f"Para confirmar, responda APAGAR nos próximos "
                f"{settings.deletion_confirm_minutes} minutos."
            ]
        )
    return None


def _scheduled_message(scheduled_for: datetime) -> str:
    return (
        f"Combinado. Seus dados serão apagados em {_local_date(scheduled_for)}. Até lá sua conta "
        "fica bloqueada e não vou registrar novos lançamentos.\n\n"
        "Para desistir, responda CANCELAR EXCLUSÃO. Depois da exclusão não vou te mandar outra "
        "mensagem."
    )


def _blocked_message(scheduled_for: datetime | None) -> str:
    when = _local_date(scheduled_for) if scheduled_for else "breve"
    return (
        f"Sua conta está marcada para ser apagada em {when} e por isso não registro nada novo. "
        "Para desistir, responda CANCELAR EXCLUSÃO. Para uma cópia dos seus dados, mande "
        "MEUS DADOS."
    )


def _latest_request(session: Session, business_id: str) -> DeletionRequest | None:
    return session.scalar(
        select(DeletionRequest)
        .where(
            DeletionRequest.business_id == business_id,
            DeletionRequest.status.in_([AWAITING_CONFIRMATION, SCHEDULED]),
        )
        .order_by(DeletionRequest.requested_at.desc())
        .limit(1)
    )


# --- export -------------------------------------------------------------------------------


def export_parts(
    session: Session, business: Business, now: datetime, settings: Settings
) -> list[str]:
    """Everything stored about the user, split into WhatsApp-sized messages."""

    lines = [
        f"Seus dados no LucroMEI em {_local_date(now)}:",
        f"Número do WhatsApp: +{business.whatsapp_number}",
        f"Nome: {business.display_name or 'não informado'}",
        f"Tipo de negócio: {business.business_type or 'não informado'}",
        f"Cadastro criado em: {_local_date(business.created_at)}",
    ]
    for acceptance in session.scalars(
        select(TermsAcceptance)
        .where(TermsAcceptance.business_id == business.id)
        .order_by(TermsAcceptance.accepted_at)
    ):
        lines.append(
            f"Termos aceitos: versão {acceptance.terms_version} em "
            f"{_local_date(acceptance.accepted_at)}"
        )
    if business.trial_started_at and business.trial_ends_at:
        lines.append(
            f"Teste grátis: de {_local_date(business.trial_started_at)} a "
            f"{_local_date(business.trial_ends_at)}"
        )

    lines += subscription_export_lines(session, business, now)
    transactions = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.events))
        .where(Transaction.business_id == business.id)
        .order_by(Transaction.occurred_at, Transaction.created_at)
    ).all()
    lines += ["", f"Lançamentos ({len(transactions)}):"]
    if not transactions:
        lines.append("nenhum lançamento registrado")
    for transaction in transactions:
        item = effective_transaction(transaction)
        notes = []
        if any(event.event_type == "corrected" for event in transaction.events):
            notes.append("corrigido")
        if item.reversed:
            notes.append("desfeito")
        suffix = f" ({', '.join(notes)})" if notes else ""
        lines.append(
            f"{_local_date(item.occurred_at)} {TYPE_LABELS[item.transaction_type.value]} "
            f"{format_brl(item.amount_cents)} - {item.description}{suffix}"
        )
    lines += [
        "",
        f"Também guardo, por até {settings.message_text_retention_days} dias, o texto das "
        "mensagens trocadas aqui, para suporte. Para apagar tudo, mande APAGAR MEUS DADOS.",
    ]
    return _split(lines)


def _split(lines: list[str]) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in lines:
        line = line[:MAX_PART_LENGTH]
        if current and len(current) + 1 + len(line) > MAX_PART_LENGTH:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    if len(parts) > MAX_EXPORT_PARTS:
        parts = parts[:MAX_EXPORT_PARTS]
        parts[-1] += (
            "\n\nA lista é longa demais para o WhatsApp e foi cortada aqui. Para receber a "
            "lista completa, fale com o suporte do LucroMEI."
        )
    if len(parts) > 1:
        parts = [f"(parte {i}/{len(parts)})\n{part}" for i, part in enumerate(parts, 1)]
    return parts


# --- deletion -----------------------------------------------------------------------------


def due_deletions(session: Session, now: datetime) -> list[str]:
    return list(
        session.scalars(
            select(DeletionRequest.id).where(
                DeletionRequest.status == SCHEDULED, DeletionRequest.scheduled_for <= now
            )
        )
    )


def execute_deletion(session: Session, request_id: str, now: datetime) -> bool:
    """Erase everything tied to the request's business. Does not commit. Idempotent."""

    request = session.get(DeletionRequest, request_id)
    if request is None or request.status != SCHEDULED:
        return False
    # Lock the business first, in the same order as message processing, to avoid deadlocks
    # with a "cancelar exclusão" arriving at the same moment.
    business = session.scalar(
        select(Business).where(Business.id == request.business_id).with_for_update()
    )
    session.refresh(request, with_for_update=True)
    if request.status != SCHEDULED or _aware(request.scheduled_for) > now:
        return False
    if business is not None:
        delete_business_data(session, business)
    request.status = COMPLETED
    request.completed_at = now
    logger.info("privacy_deletion_completed", extra={"business_id": request.business_id})
    return True


def delete_business_data(session: Session, business: Business) -> None:
    business_id, number = business.id, business.whatsapp_number
    transaction_ids = select(Transaction.id).where(Transaction.business_id == business_id)
    provider_ids = select(OutboundMessage.provider_message_id).where(
        OutboundMessage.recipient == number, OutboundMessage.provider_message_id.is_not(None)
    )
    statements = [
        delete(TransactionEvent).where(TransactionEvent.transaction_id.in_(transaction_ids)),
        delete(PendingEntry).where(PendingEntry.business_id == business_id),
        delete(Transaction).where(Transaction.business_id == business_id),
        delete(InboundMessage).where(InboundMessage.business_id == business_id),
        delete(CommandReceipt).where(CommandReceipt.business_id == business_id),
        delete(AIUsage).where(AIUsage.business_id == business_id),
        delete(TermsAcceptance).where(TermsAcceptance.business_id == business_id),
        delete(Subscription).where(Subscription.business_id == business_id),
        delete(DeliveryStatusEvent).where(DeliveryStatusEvent.provider_message_id.in_(provider_ids)),
        delete(OutboundMessage).where(OutboundMessage.recipient == number),
        delete(InboundEvent).where(InboundEvent.sender == number),
    ]
    for statement in statements:
        session.execute(statement.execution_options(synchronize_session=False))
    session.delete(business)
    session.flush()


# --- retention ----------------------------------------------------------------------------


def purge_old_text(session: Session, now: datetime, settings: Settings) -> dict[str, int]:
    """Erase raw message text older than the retention period and drop old finished jobs."""

    cutoff = now - timedelta(days=settings.message_text_retention_days)
    results = {
        "inbound_events": session.execute(
            update(InboundEvent)
            .where(InboundEvent.received_at < cutoff, InboundEvent.text.is_not(None))
            .values(text=None)
        ).rowcount,
        "outbound_messages": session.execute(
            update(OutboundMessage)
            .where(OutboundMessage.created_at < cutoff, OutboundMessage.text != "")
            .values(text="")
        ).rowcount,
        "command_receipts": session.execute(
            update(CommandReceipt)
            .where(CommandReceipt.created_at < cutoff, CommandReceipt.response_text != "")
            .values(response_text="")
        ).rowcount,
        # Open questions keep their text until they are answered or expire.
        "pending_entries": session.execute(
            update(PendingEntry)
            .where(
                PendingEntry.created_at < cutoff,
                PendingEntry.status != "open",
                PendingEntry.source_text != "",
            )
            .values(source_text="")
        ).rowcount,
        "pending_batches": session.execute(
            update(PendingEntry)
            .where(
                PendingEntry.created_at < cutoff,
                PendingEntry.status != "open",
                PendingEntry.items.is_not(None),
            )
            .values(items=None)
        ).rowcount,
        "jobs": session.execute(
            delete(Job).where(
                ((Job.status == "done") & (Job.finished_at < now - timedelta(days=7)))
                | ((Job.status == "dead") & (Job.finished_at < cutoff))
            )
        ).rowcount,
    }
    return results


def _hash_key(settings: Settings) -> str:
    # Local development has no key; production refuses to start without one.
    return settings.privacy_hash_key or "lucromei-local-development-only"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _local_date(value: datetime) -> str:
    return _aware(value).astimezone(get_business_timezone()).strftime("%d/%m/%Y")

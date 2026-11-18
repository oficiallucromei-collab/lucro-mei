import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from lucromei.domain import ParsedTransaction, TransactionType, parse_transaction
from lucromei.models import Business, InboundMessage, Transaction, TransactionEvent


class LedgerError(ValueError):
    pass


class TransactionNotFound(LedgerError):
    pass


@dataclass(frozen=True, slots=True)
class EffectiveTransaction:
    id: str
    transaction_type: TransactionType
    amount_cents: int
    description: str
    occurred_at: datetime
    reversed: bool = False


@dataclass(frozen=True, slots=True)
class ProcessedMessage:
    message_id: str
    whatsapp_number: str
    transaction: EffectiveTransaction
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class Summary:
    period_start: datetime
    period_end: datetime
    income_cents: int
    expense_cents: int
    withdrawal_cents: int

    @property
    def operating_balance_cents(self) -> int:
        return self.income_cents - self.expense_cents - self.withdrawal_cents


def effective_transaction(transaction: Transaction) -> EffectiveTransaction:
    current = EffectiveTransaction(
        id=transaction.id,
        transaction_type=TransactionType(transaction.transaction_type),
        amount_cents=transaction.amount_cents,
        description=transaction.description,
        occurred_at=transaction.occurred_at,
    )
    for event in transaction.events:
        if event.event_type == "corrected":
            changes = event.changes
            current = replace(
                current,
                transaction_type=TransactionType(
                    changes.get("transaction_type", current.transaction_type)
                ),
                amount_cents=changes.get("amount_cents", current.amount_cents),
                description=changes.get("description", current.description),
                occurred_at=datetime.fromisoformat(changes["occurred_at"])
                if changes.get("occurred_at")
                else current.occurred_at,
            )
        elif event.event_type == "reversed":
            current = replace(current, reversed=True)
    return current


class LedgerService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def process(self, *, message_id: str, whatsapp_number: str, text: str) -> ProcessedMessage:
        if existing := self._find_message(message_id):
            return self._processed(existing, whatsapp_number, duplicate=True)
        parsed = parse_transaction(text)
        canonical_number = normalize_whatsapp_number(whatsapp_number)
        business = self.session.scalar(
            select(Business).where(Business.whatsapp_number == canonical_number)
        )
        if business is None:
            business = Business(whatsapp_number=canonical_number)
            self.session.add(business)
            self.session.flush()
        inbound = InboundMessage(
            external_id=message_id,
            business_id=business.id,
            raw_text=text,
        )
        transaction = Transaction(
            business_id=business.id,
            source_message=inbound,
            transaction_type=parsed.transaction_type.value,
            amount_cents=parsed.amount_cents,
            description=parsed.description,
            occurred_at=parsed.occurred_at,
        )
        transaction.events.append(TransactionEvent(event_type="created", changes=_snapshot(parsed)))
        self.session.add(transaction)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self._find_message(message_id)
            if existing is None:
                raise
            return self._processed(existing, whatsapp_number, duplicate=True)
        return ProcessedMessage(
            message_id,
            whatsapp_number,
            effective_transaction(transaction),
        )

    def correct(
        self,
        transaction_id: str,
        *,
        amount_cents=None,
        transaction_type=None,
        description=None,
        occurred_at=None,
        reason=None,
        commit: bool = True,
    ) -> EffectiveTransaction:
        transaction = self._get_transaction(transaction_id)
        if effective_transaction(transaction).reversed:
            raise LedgerError("A transação já foi desfeita.")
        changes = {}
        if amount_cents is not None:
            if amount_cents <= 0:
                raise LedgerError("O valor deve ser maior que zero.")
            changes["amount_cents"] = amount_cents
        if transaction_type is not None:
            changes["transaction_type"] = transaction_type.value
        if description is not None:
            changes["description"] = description
        if occurred_at is not None:
            changes["occurred_at"] = occurred_at.isoformat()
        if not changes:
            raise LedgerError("Informe ao menos um campo para corrigir.")
        transaction.events.append(
            TransactionEvent(event_type="corrected", changes=changes, reason=reason)
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return effective_transaction(transaction)

    def reverse(
        self, transaction_id: str, *, reason=None, commit: bool = True
    ) -> EffectiveTransaction:
        transaction = self._get_transaction(transaction_id)
        if effective_transaction(transaction).reversed:
            raise LedgerError("A transação já foi desfeita.")
        transaction.events.append(
            TransactionEvent(event_type="reversed", changes={}, reason=reason)
        )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return effective_transaction(transaction)

    def latest_transaction(
        self,
        whatsapp_number: str,
        transaction_type: TransactionType | None = None,
    ) -> EffectiveTransaction | None:
        canonical_number = normalize_whatsapp_number(whatsapp_number)
        business = self.session.scalar(
            select(Business).where(Business.whatsapp_number == canonical_number)
        )
        if business is None:
            return None
        transactions = self.session.scalars(
            select(Transaction)
            .options(selectinload(Transaction.events))
            .where(Transaction.business_id == business.id)
            .order_by(Transaction.occurred_at.desc(), Transaction.created_at.desc())
        ).all()
        for transaction in transactions:
            current = effective_transaction(transaction)
            if not current.reversed and (
                transaction_type is None or current.transaction_type is transaction_type
            ):
                return current
        return None

    def monthly_summary(self, whatsapp_number: str, year: int, month: int) -> Summary:
        start = datetime(year, month, 1, tzinfo=UTC)
        end = (
            datetime(year + 1, 1, 1, tzinfo=UTC)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=UTC)
        )
        canonical_number = normalize_whatsapp_number(whatsapp_number)
        business = self.session.scalar(
            select(Business).where(Business.whatsapp_number == canonical_number)
        )
        if business is None:
            return Summary(start, end, 0, 0, 0)
        transactions = self.session.scalars(
            select(Transaction)
            .options(selectinload(Transaction.events))
            .where(Transaction.business_id == business.id)
        ).all()
        included = []
        for transaction in transactions:
            item = effective_transaction(transaction)
            occurred_at = item.occurred_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            if not item.reversed and start <= occurred_at < end:
                included.append(item)

        def total(kind: TransactionType) -> int:
            return sum(item.amount_cents for item in included if item.transaction_type is kind)

        return Summary(
            start,
            end,
            total(TransactionType.INCOME),
            total(TransactionType.EXPENSE),
            total(TransactionType.WITHDRAWAL),
        )

    def _find_message(self, message_id: str) -> InboundMessage | None:
        return self.session.scalar(
            select(InboundMessage)
            .options(selectinload(InboundMessage.transaction).selectinload(Transaction.events))
            .where(InboundMessage.external_id == message_id)
        )

    def _get_transaction(self, transaction_id: str) -> Transaction:
        transaction = self.session.scalar(
            select(Transaction)
            .options(selectinload(Transaction.events))
            .where(Transaction.id == transaction_id)
        )
        if transaction is None:
            raise TransactionNotFound("Transação não encontrada.")
        return transaction

    def _processed(
        self,
        inbound: InboundMessage,
        number: str,
        *,
        duplicate: bool,
    ) -> ProcessedMessage:
        return ProcessedMessage(
            inbound.external_id,
            number,
            effective_transaction(inbound.transaction),
            duplicate,
        )


def _snapshot(parsed: ParsedTransaction) -> dict[str, str | int]:
    return {
        "transaction_type": parsed.transaction_type.value,
        "amount_cents": parsed.amount_cents,
        "description": parsed.description,
        "occurred_at": parsed.occurred_at.isoformat(),
    }


def normalize_whatsapp_number(number: str) -> str:
    """Store WhatsApp identifiers as digits, independent of a leading plus sign."""
    return re.sub(r"\D", "", number)

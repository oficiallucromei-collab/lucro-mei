import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucromei.domain import TransactionType
from lucromei.models import Business, CommandReceipt
from lucromei.service import LedgerService, normalize_whatsapp_number


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_type: str
    reply: str
    duplicate: bool = False


_CORRECTION_RE = re.compile(
    r"^corrigir(?:\s+(?:a|o))?(?:\s+(?:ultima|ultimo))?"
    r"(?:\s+(entrada|despesa|retirada))?\s+(?:para|pra)\s+"
    r"(?:r\$\s*)?([\d.,]+)$"
)
_SUMMARY_COMMANDS = {
    "quanto lucrei este mes",
    "quanto lucrei no mes",
    "resumo do mes",
    "resumo deste mes",
    "resumo mensal",
}
_TYPE_WORDS = {
    "entrada": TransactionType.INCOME,
    "despesa": TransactionType.EXPENSE,
    "retirada": TransactionType.WITHDRAWAL,
}


class ConversationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ledger = LedgerService(session)

    def handle(
        self,
        *,
        message_id: str,
        whatsapp_number: str,
        text: str,
        now: datetime | None = None,
    ) -> CommandResult | None:
        if receipt := self._find_receipt(message_id):
            return self._result(receipt, duplicate=True)

        normalized = _normalize(text)
        if normalized in {"desfazer", "desfazer ultima", "desfazer ultimo"}:
            command_type = "undo"
            transaction = self.ledger.latest_transaction(whatsapp_number)
            if transaction is None:
                reply = "Não encontrei um lançamento ativo para desfazer."
            else:
                self.ledger.reverse(
                    transaction.id,
                    reason="Desfeito por comando no WhatsApp",
                    commit=False,
                )
                reply = f"Lançamento de {_money(transaction.amount_cents)} desfeito."
        elif match := _CORRECTION_RE.fullmatch(normalized):
            command_type = "correction"
            transaction_type = _TYPE_WORDS.get(match.group(1))
            amount_cents = _parse_amount(match.group(2))
            if amount_cents is None:
                return None
            transaction = self.ledger.latest_transaction(whatsapp_number, transaction_type)
            if transaction is None:
                reply = "Não encontrei um lançamento ativo para corrigir."
            else:
                self.ledger.correct(
                    transaction.id,
                    amount_cents=amount_cents,
                    reason="Corrigido por comando no WhatsApp",
                    commit=False,
                )
                reply = f"Lançamento corrigido para {_money(amount_cents)}."
        elif normalized.rstrip(" ?!") in _SUMMARY_COMMANDS:
            command_type = "summary"
            current = now or datetime.now(UTC)
            summary = self.ledger.monthly_summary(whatsapp_number, current.year, current.month)
            reply = (
                f"Resumo do mês: entradas {_money(summary.income_cents)}, "
                f"despesas {_money(summary.expense_cents)}, retiradas "
                f"{_money(summary.withdrawal_cents)}. Saldo: "
                f"{_money(summary.operating_balance_cents)}."
            )
        else:
            return None

        business = self._get_or_create_business(whatsapp_number)
        receipt = CommandReceipt(
            external_id=message_id,
            business_id=business.id,
            command_type=command_type,
            response_text=reply,
        )
        self.session.add(receipt)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self._find_receipt(message_id)
            if existing is None:
                raise
            return self._result(existing, duplicate=True)
        return CommandResult(command_type, reply)

    def _find_receipt(self, message_id: str) -> CommandReceipt | None:
        return self.session.scalar(
            select(CommandReceipt).where(CommandReceipt.external_id == message_id)
        )

    def _get_or_create_business(self, whatsapp_number: str) -> Business:
        canonical_number = normalize_whatsapp_number(whatsapp_number)
        business = self.session.scalar(
            select(Business).where(Business.whatsapp_number == canonical_number)
        )
        if business is None:
            business = Business(whatsapp_number=canonical_number)
            self.session.add(business)
            self.session.flush()
        return business

    @staticmethod
    def _result(receipt: CommandReceipt, *, duplicate: bool) -> CommandResult:
        return CommandResult(receipt.command_type, receipt.response_text, duplicate)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(without_accents.split())


def _parse_amount(raw: str) -> int | None:
    cleaned = raw.strip()
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    cents = int(value * 100)
    return cents if cents > 0 else None


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    reais, centavos = divmod(absolute, 100)
    formatted_reais = f"{reais:,}".replace(",", ".")
    return f"{sign}R$ {formatted_reais},{centavos:02d}"

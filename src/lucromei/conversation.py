import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucromei.domain import PeriodTotals, TransactionType
from lucromei.models import CommandReceipt
from lucromei.money import InvalidAmount, format_brl, parse_brl_amount
from lucromei.service import LedgerService


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
_MONTH_NAMES = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)
SUMMARY_DISCLAIMER = (
    "Esses valores não consideram contas futuras, reservas ou impostos, então não são "
    "automaticamente o quanto você pode tirar com segurança."
)
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
        commit: bool = True,
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
                reply = f"Lançamento de {format_brl(transaction.amount_cents)} desfeito."
        elif match := _CORRECTION_RE.fullmatch(normalized):
            command_type = "correction"
            transaction_type = _TYPE_WORDS.get(match.group(1))
            try:
                amount_cents = parse_brl_amount(match.group(2))
            except InvalidAmount:
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
                reply = f"Lançamento corrigido para {format_brl(amount_cents)}."
        elif normalized.rstrip(" ?!") in _SUMMARY_COMMANDS:
            command_type = "summary"
            year, month = self.ledger.current_month(now)
            summary = self.ledger.monthly_summary(whatsapp_number, year, month)
            reply = format_monthly_summary(year, month, summary.totals)
        else:
            return None

        business = self.ledger.get_or_create_business(whatsapp_number)
        receipt = CommandReceipt(
            external_id=message_id,
            business_id=business.id,
            command_type=command_type,
            response_text=reply,
        )
        self.session.add(receipt)
        if not commit:
            self.session.flush()
            return CommandResult(command_type, reply)
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

    @staticmethod
    def _result(receipt: CommandReceipt, *, duplicate: bool) -> CommandResult:
        return CommandResult(receipt.command_type, receipt.response_text, duplicate)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(without_accents.split())


def format_monthly_summary(year: int, month: int, totals: PeriodTotals) -> str:
    return "\n".join(
        [
            f"Resumo de {_MONTH_NAMES[month - 1]}/{year}:",
            f"Entradas: {format_brl(totals.income_cents)}",
            f"Despesas do negócio: {format_brl(totals.expense_cents)}",
            f"Resultado do negócio: {format_brl(totals.operating_result_cents)}",
            f"Retiradas pessoais: {format_brl(totals.withdrawal_cents)}",
            f"Sobra após retiradas: {format_brl(totals.net_after_withdrawals_cents)}",
            "",
            SUMMARY_DISCLAIMER,
        ]
    )

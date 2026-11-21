import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucromei.domain import PeriodTotals, TransactionType
from lucromei.models import CommandReceipt
from lucromei.money import InvalidAmount, format_brl, parse_brl_amount
from lucromei.service import (
    EffectiveTransaction,
    LedgerService,
    local_day_bounds,
    local_week_bounds,
)


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
# "resumo da semana", "quanto ganhei essa semana?", "como foi hoje", "resumo do mês passado".
_SUMMARY_TRIGGERS = sorted(
    {
        "resumo",
        "balanco",
        "quanto ganhei",
        "quanto eu ganhei",
        "quanto lucrei",
        "quanto eu lucrei",
        "quanto faturei",
        "quanto eu faturei",
        "quanto vendi",
        "quanto eu vendi",
        "quanto recebi",
        "quanto eu recebi",
        "quanto entrou",
        "quanto gastei",
        "quanto eu gastei",
        "quanto sobrou",
        "como foi",
        "como esta",
        "como ta",
        "qual o resultado",
        "qual foi o resultado",
    },
    key=len,
    reverse=True,
)
_SUMMARY_FILLERS = {
    "o", "a", "de", "do", "da", "no", "na", "nesse", "nessa", "neste", "nesta", "esse", "essa",
    "este", "esta", "deste", "desta", "desse", "dessa", "meu", "minha", "ate", "agora", "ai",
}
_PERIOD_WORDS = {
    "hoje": "day",
    "dia": "day",
    "diario": "day",
    "semana": "week",
    "semanal": "week",
    "mes": "month",
    "mensal": "month",
}
_LIST_COMMANDS = {
    "ultimos lancamentos",
    "ultimos",
    "ultimas",
    "meus lancamentos",
    "lancamentos",
    "ver lancamentos",
    "ultimas movimentacoes",
    "movimentacoes",
    "extrato",
    "historico",
}
_HELP_COMMANDS = {"ajuda", "menu", "comandos", "o que voce faz", "como funciona", "help"}
RECENT_LIMIT = 10
HELP_TEXT = (
    "Me mande suas movimentações, por exemplo:\n"
    "• Recebi 150 por um corte\n"
    "• Gastei 80 em produtos\n"
    "• Retirei 200 para mim\n"
    "• Ontem recebi 200 (ou: dia 15 recebi 200)\n\n"
    "Consultas:\n"
    "• resumo de hoje / da semana / do mês\n"
    "• últimos lançamentos\n\n"
    "Correções: DESFAZER apaga o último lançamento e CORRIGIR PARA 120 muda o valor dele.\n"
    "Privacidade: MEUS DADOS mostra tudo o que guardo e APAGAR MEUS DADOS apaga sua conta."
)
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
        elif (period := match_summary_period(normalized)) is not None:
            command_type = "summary"
            reply = self._summary_reply(whatsapp_number, period, now)
        elif normalized.rstrip(" ?!.") in _LIST_COMMANDS:
            command_type = "list"
            recent = self.ledger.recent_transactions(whatsapp_number, RECENT_LIMIT)
            reply = format_recent(recent, self.ledger.tz)
        elif normalized.rstrip(" ?!.") in _HELP_COMMANDS:
            command_type = "help"
            reply = HELP_TEXT
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

    def _summary_reply(
        self, whatsapp_number: str, period: tuple[str, int], now: datetime | None
    ) -> str:
        kind, back = period
        today = (now or datetime.now(UTC)).astimezone(self.ledger.tz).date()
        if kind == "month":
            year, month = today.year, today.month - back
            while month < 1:
                year, month = year - 1, month + 12
            summary = self.ledger.monthly_summary(whatsapp_number, year, month)
            return format_monthly_summary(year, month, summary.totals)
        if kind == "week":
            start, end = local_week_bounds(today - timedelta(weeks=back), self.ledger.tz)
            last_day = (end - timedelta(days=1)).date()
            label = "da semana passada" if back else "da semana"
            title = f"Resumo {label} ({_short(start.date())} a {_short(last_day)}):"
        else:
            day = today - timedelta(days=back)
            start, end = local_day_bounds(day, self.ledger.tz)
            label = "de ontem" if back else "de hoje"
            title = f"Resumo {label} ({_short(day)}):"
        summary = self.ledger.period_summary(whatsapp_number, start, end)
        return format_period_summary(title, summary.totals)

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


def match_summary_period(normalized: str) -> tuple[str, int] | None:
    """("day" | "week" | "month", periods back) for a summary request, or None.

    "resumo" alone means this month, as before. "passado/passada" and "ontem" go one back.
    """

    text = normalized.strip(" ?!.")
    for trigger in _SUMMARY_TRIGGERS:
        if text == trigger or text.startswith(trigger + " "):
            rest = text[len(trigger) :].split()
            break
    else:
        return None
    words = [word for word in rest if word not in _SUMMARY_FILLERS]
    if not words:
        return ("month", 0) if trigger in {"resumo", "balanco"} else None
    if words == ["ontem"]:
        return ("day", 1)
    if len(words) == 2 and words[1] in {"passado", "passada", "anterior"}:
        period = _PERIOD_WORDS.get(words[0])
        return (period, 1) if period in {"week", "month"} else None
    if len(words) == 1 and words[0] in _PERIOD_WORDS:
        return (_PERIOD_WORDS[words[0]], 0)
    return None


def format_recent(items: list[EffectiveTransaction], tz: ZoneInfo) -> str:
    if not items:
        return "Você ainda não tem lançamentos. Mande, por exemplo: Recebi 150 por um corte."
    labels = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada"}
    heading = f"Seus últimos {len(items)} lançamentos:" if len(items) > 1 else "Seu lançamento:"
    lines = [heading]
    for item in items:
        occurred = item.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        lines.append(
            f"{_short(occurred.astimezone(tz).date())} {labels[item.transaction_type.value]} "
            f"{format_brl(item.amount_cents)} - {item.description[:60]}"
        )
    lines += ["", "Para desfazer o último, mande DESFAZER."]
    return "\n".join(lines)


def _short(day: date) -> str:
    return day.strftime("%d/%m")


def format_period_summary(title: str, totals: PeriodTotals) -> str:
    return "\n".join(
        [
            title,
            f"Entradas: {format_brl(totals.income_cents)}",
            f"Despesas do negócio: {format_brl(totals.expense_cents)}",
            f"Resultado do negócio: {format_brl(totals.operating_result_cents)}",
            f"Retiradas pessoais: {format_brl(totals.withdrawal_cents)}",
            f"Sobra após retiradas: {format_brl(totals.net_after_withdrawals_cents)}",
            "",
            SUMMARY_DISCLAIMER,
        ]
    )


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

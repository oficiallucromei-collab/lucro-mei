"""Spreadsheet export and the monthly report.

"planilha" sends the month's entries as a CSV document over WhatsApp (opens in Excel and Google
Sheets: ";" separator, comma decimals, UTF-8 with BOM). "relatório do mês" is a richer text
summary: totals, the change from the previous month and where the money went. Both only
describe what the user recorded; they never give accounting, tax or investment advice.
"""

import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from lucromei.domain import TransactionType
from lucromei.models import Business, Transaction
from lucromei.money import format_brl
from lucromei.service import effective_transaction, local_month_bounds

MONTHS = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
TYPE_LABELS = {"income": "Entrada", "expense": "Despesa", "withdrawal": "Retirada pessoal"}
DISCLAIMER = (
    "Este relatório só soma o que você anotou. Não é orientação contábil, fiscal ou de "
    "investimentos e não considera impostos, reservas ou contas futuras."
)
_SPREADSHEET_RE = re.compile(
    r"^(?:me (?:manda|mande|envia|envie) (?:a |uma )?)?(?:planilha|exportar planilha|exportar|"
    r"baixar planilha|excel)(?: (?:do|de|deste|desse|da) (mes|mes passado|mes anterior))?$"
)
_REPORT_RE = re.compile(
    r"^relatorio(?: (?:do|de|deste|desse|mensal)?\s*(mes|mes passado|mes anterior)?)?$"
)


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file the send job builds at send time (nothing is stored on disk)."""

    kind: str
    year: int
    month: int

    def as_dict(self) -> dict[str, int | str]:
        return {"kind": self.kind, "year": self.year, "month": self.month}


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().strip())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(plain.split()).strip(" .!?")


def _month_back(today: date, back: int) -> tuple[int, int]:
    year, month = today.year, today.month - back
    while month < 1:
        year, month = year - 1, month + 12
    return year, month


def match_spreadsheet(text: str, today: date) -> Attachment | None:
    match = _SPREADSHEET_RE.fullmatch(normalize(text))
    if match is None:
        return None
    back = 1 if match.group(1) and match.group(1) != "mes" else 0
    year, month = _month_back(today, back)
    return Attachment("ledger_csv", year, month)


def match_report(text: str, today: date) -> tuple[int, int] | None:
    match = _REPORT_RE.fullmatch(normalize(text))
    if match is None:
        return None
    back = 1 if match.group(1) and match.group(1) != "mes" else 0
    return _month_back(today, back)


def month_label(year: int, month: int) -> str:
    return f"{MONTHS[month - 1]}/{year}"


def _month_items(session: Session, business: Business, year: int, month: int, tz):
    start, end = local_month_bounds(year, month, tz)
    rows = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.events))
        .where(Transaction.business_id == business.id)
        .order_by(Transaction.occurred_at, Transaction.created_at)
    ).all()
    items = []
    for row in rows:
        item = effective_transaction(row)
        occurred = item.occurred_at if item.occurred_at.tzinfo else item.occurred_at.replace(
            tzinfo=UTC
        )
        if start <= occurred < end:
            corrected = any(event.event_type == "corrected" for event in row.events)
            items.append((item, occurred.astimezone(tz), corrected))
    items.sort(key=lambda entry: entry[1])
    return items


def ledger_csv(session: Session, business: Business, year: int, month: int, tz) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Data", "Tipo", "Valor (R$)", "Descrição", "Situação"])
    for item, occurred, corrected in _month_items(session, business, year, month, tz):
        status = "desfeito" if item.reversed else ("corrigido" if corrected else "")
        reais, cents = divmod(item.amount_cents, 100)
        writer.writerow(
            [
                occurred.strftime("%d/%m/%Y"),
                TYPE_LABELS[item.transaction_type.value],
                f"{reais},{cents:02d}",
                _safe_cell(item.description),
                status,
            ]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _safe_cell(value: str) -> str:
    """Stop spreadsheet apps from running a description as a formula (CSV injection)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def csv_filename(year: int, month: int) -> str:
    return f"lucromei-{year}-{month:02d}.csv"


def spreadsheet_caption(year: int, month: int) -> str:
    return (
        f"Sua planilha de {month_label(year, month)}. Abre no Excel ou no Google Planilhas. "
        "Lançamentos desfeitos aparecem marcados e não entram nas somas do resumo."
    )


def monthly_report(session: Session, business: Business, year: int, month: int, tz) -> str:
    current = [entry for entry in _month_items(session, business, year, month, tz)
               if not entry[0].reversed]
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    previous = [entry for entry in _month_items(session, business, prev_year, prev_month, tz)
                if not entry[0].reversed]

    def total(entries, kind: TransactionType) -> int:
        return sum(item.amount_cents for item, _, _ in entries if item.transaction_type is kind)

    income = total(current, TransactionType.INCOME)
    expense = total(current, TransactionType.EXPENSE)
    withdrawal = total(current, TransactionType.WITHDRAWAL)
    lines = [
        f"Relatório de {month_label(year, month)}",
        f"Lançamentos: {len(current)}",
        f"Entradas: {format_brl(income)}{_change(income, total(previous, TransactionType.INCOME))}",
        f"Despesas do negócio: {format_brl(expense)}"
        f"{_change(expense, total(previous, TransactionType.EXPENSE))}",
        f"Resultado do negócio: {format_brl(income - expense)}",
        f"Retiradas pessoais: {format_brl(withdrawal)}",
        f"Sobra após retiradas: {format_brl(income - expense - withdrawal)}",
    ]
    days = Counter()
    for item, occurred, _ in current:
        if item.transaction_type is TransactionType.INCOME:
            days[occurred.date()] += item.amount_cents
    if days:
        best, amount = max(days.items(), key=lambda pair: (pair[1], pair[0]))
        lines.append(f"Dia com mais entradas: {best.strftime('%d/%m')} ({format_brl(amount)})")
    spending = Counter()
    for item, _, _ in current:
        if item.transaction_type is TransactionType.EXPENSE:
            spending[item.description[:40]] += item.amount_cents
    if spending:
        lines += ["", "Maiores despesas:"]
        for description, amount in spending.most_common(3):
            lines.append(f"• {description}: {format_brl(amount)}")
    lines += ["", "Para a lista completa, mande PLANILHA.", "", DISCLAIMER]
    return "\n".join(lines)


def _change(current: int, previous: int) -> str:
    if previous <= 0:
        return ""
    percent = round((current - previous) * 100 / previous)
    if percent == 0:
        return " (igual ao mês anterior)"
    sign = "+" if percent > 0 else ""
    return f" ({sign}{percent}% em relação ao mês anterior)"


def local_today(now: datetime, tz) -> date:
    return (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(tz).date()

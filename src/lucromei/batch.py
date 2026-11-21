"""Several entries in one message: preview them, wait for SIM, then record all at once.

"Recebi 150 por um corte\\nGastei 30 em produtos" (or "...; ..." or "recebi 150 e gastei 30")
becomes a pending batch. Nothing is recorded until the user confirms. On SIM every entry is
written in the same database transaction as the reply, so either all of them are recorded or
none is. Each entry's idempotency key is "<message id>#<n>", so a batch can never be recorded
twice, even if the confirmation is processed again.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from lucromei.domain import (
    AmbiguousTransaction,
    InvalidDate,
    ParsedTransaction,
    ParseError,
    TransactionType,
    parse_transaction,
)
from lucromei.models import PendingEntry
from lucromei.money import format_brl

MAX_ITEMS = 10
BATCH = "batch"
SINGLE = "single"

_LINE_SPLIT_RE = re.compile(r"[\n;]+")
# Split "recebi 150 e gastei 30" / "recebi 150, gastei 30" right before the next entry's verb.
_VERBS = (
    r"recebi|ganhei|entrou|vendi|fiz|gastei|paguei|comprei|custou|retirei|tirei|saquei"
)
_INLINE_SPLIT_RE = re.compile(
    rf"\s*(?:,|\be\b|\btambem\b|\btambém\b)\s+"
    rf"(?=(?:(?:ontem|anteontem|hoje|dia\s+\d{{1,2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?)\s+)?"
    rf"(?:{_VERBS})\b)",
    re.IGNORECASE,
)
LABELS = {
    TransactionType.INCOME: "Entrada",
    TransactionType.EXPENSE: "Despesa",
    TransactionType.WITHDRAWAL: "Retirada",
}


@dataclass(frozen=True, slots=True)
class Plan:
    """The outcome of reading a multi-entry message."""

    items: list[ParsedTransaction]
    problems: list[str]


def split_entries(text: str) -> list[str]:
    segments: list[str] = []
    for line in _LINE_SPLIT_RE.split(text):
        for part in _INLINE_SPLIT_RE.split(line):
            part = part.strip(" .,-•*")
            if part:
                segments.append(part)
    return segments


def is_batch_message(text: str, *, now: datetime, tz: ZoneInfo) -> bool:
    """Several entries, and not a message the single-entry parser already understands."""
    if len(split_entries(text)) < 2:
        return False
    try:
        parse_transaction(text, now=now, tz=tz)
    except ParseError:
        return True
    return False


def plan(text: str, *, now: datetime, tz: ZoneInfo) -> Plan:
    segments = split_entries(text)
    if len(segments) > MAX_ITEMS:
        return Plan([], [f"Mande até {MAX_ITEMS} lançamentos por mensagem."])
    items: list[ParsedTransaction] = []
    problems: list[str] = []
    for index, segment in enumerate(segments, 1):
        try:
            items.append(parse_transaction(segment, now=now, tz=tz))
        except AmbiguousTransaction:
            problems.append(
                f"Linha {index}: não sei se é despesa do negócio ou retirada pessoal. "
                "Mande essa separada."
            )
        except InvalidDate as exc:
            problems.append(f"Linha {index}: {exc}")
        except ParseError:
            problems.append(f"Linha {index}: não entendi o tipo ou o valor.")
    return Plan(items, problems)


def problems_reply(problems: list[str]) -> str:
    return (
        "Não anotei nada desta mensagem:\n"
        + "\n".join(problems)
        + "\n\nCorrija e mande de novo, uma linha por lançamento."
    )


def serialize(items: list[ParsedTransaction]) -> list[dict[str, str | int]]:
    return [
        {
            "transaction_type": item.transaction_type.value,
            "amount_cents": item.amount_cents,
            "description": item.description[:500],
            "occurred_at": item.occurred_at.isoformat(),
        }
        for item in items
    ]


def preview(pending: PendingEntry, tz: ZoneInfo, sent_day=None) -> str:
    items = pending.items or []
    lines = [f"Vou anotar {len(items)} lançamentos:"]
    totals = {kind: 0 for kind in TransactionType}
    for index, item in enumerate(items, 1):
        kind = TransactionType(item["transaction_type"])
        totals[kind] += item["amount_cents"]
        day = datetime.fromisoformat(item["occurred_at"]).astimezone(tz).date()
        when = f" ({day.strftime('%d/%m')})" if sent_day is not None and day != sent_day else ""
        lines.append(
            f"{index}. {LABELS[kind]} {format_brl(item['amount_cents'])} - "
            f"{item['description'][:60]}{when}"
        )
    lines += ["", totals_line(totals), "", "Responda SIM para anotar todos ou NÃO para cancelar."]
    return "\n".join(lines)


def totals_line(totals: dict[TransactionType, int]) -> str:
    parts = []
    if totals[TransactionType.INCOME]:
        parts.append(f"entradas {format_brl(totals[TransactionType.INCOME])}")
    if totals[TransactionType.EXPENSE]:
        parts.append(f"despesas {format_brl(totals[TransactionType.EXPENSE])}")
    if totals[TransactionType.WITHDRAWAL]:
        parts.append(f"retiradas {format_brl(totals[TransactionType.WITHDRAWAL])}")
    return "Total: " + ", ".join(parts) + "."

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum


class TransactionType(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    WITHDRAWAL = "withdrawal"


@dataclass(frozen=True, slots=True)
class ParsedTransaction:
    transaction_type: TransactionType
    amount_cents: int
    description: str
    occurred_at: datetime
    confidence: Decimal


MAX_AMOUNT_CENTS = 2_147_483_647

_AMOUNT_RE = re.compile(r"(?:r\$\s*)?(\d+(?:[.,]\d{1,2})?)", re.IGNORECASE)
_INCOME_WORDS = ("recebi", "ganhei", "entrou", "vendi", "fiz")
_EXPENSE_WORDS = ("gastei", "paguei", "comprei", "custou", "despesa")
_WITHDRAWAL_WORDS = ("retirei", "tirei", "saquei", "pró-labore", "pro-labore")


class ParseError(ValueError):
    """Raised when a message cannot be safely converted into a transaction."""


def parse_transaction(message: str, *, now: datetime | None = None) -> ParsedTransaction:
    """Parse high-confidence Portuguese transaction messages without an LLM.

    Ambiguous messages are rejected instead of guessing. A future AI parser can be used only
    as a structured fallback and must satisfy the same validation rules.
    """

    normalized = " ".join(message.lower().strip().split())
    if not normalized:
        raise ParseError("A mensagem está vazia.")

    # A payment to a person may be a business expense or a personal withdrawal.
    # The ledger must ask instead of inferring the user's intent.
    if normalized.startswith("paguei ") and " para " in normalized:
        raise ParseError("Não ficou claro se é entrada, despesa ou retirada pessoal.")

    matches = list(_AMOUNT_RE.finditer(normalized))
    if len(matches) != 1:
        raise ParseError("Informe exatamente um valor por mensagem.")

    groups = [
        (TransactionType.INCOME, any(word in normalized for word in _INCOME_WORDS)),
        (TransactionType.EXPENSE, any(word in normalized for word in _EXPENSE_WORDS)),
        (TransactionType.WITHDRAWAL, any(word in normalized for word in _WITHDRAWAL_WORDS)),
    ]
    detected = [kind for kind, present in groups if present]
    if len(detected) != 1:
        raise ParseError("Não ficou claro se é entrada, despesa ou retirada pessoal.")

    raw_amount = matches[0].group(1).replace(".", "").replace(",", ".")
    try:
        decimal_amount = Decimal(raw_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ParseError("O valor informado é inválido.") from exc

    amount_cents = int(decimal_amount * 100)
    if amount_cents <= 0:
        raise ParseError("O valor deve ser maior que zero.")

    if amount_cents > MAX_AMOUNT_CENTS:
        raise ParseError("O valor informado excede o limite permitido.")

    description = (normalized[: matches[0].start()] + normalized[matches[0].end() :]).strip(" .,-")
    description = description or detected[0].value
    if len(description) > 500:
        raise ParseError("A descrição excede o limite de 500 caracteres.")

    return ParsedTransaction(
        transaction_type=detected[0],
        amount_cents=amount_cents,
        description=description,
        occurred_at=now or datetime.now(UTC),
        confidence=Decimal("1.00"),
    )


def calculate_balance(transactions: list[ParsedTransaction]) -> int:
    income = sum(
        t.amount_cents for t in transactions if t.transaction_type is TransactionType.INCOME
    )
    outflows = sum(
        t.amount_cents
        for t in transactions
        if t.transaction_type in {TransactionType.EXPENSE, TransactionType.WITHDRAWAL}
    )
    return income - outflows

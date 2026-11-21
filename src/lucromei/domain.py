import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from lucromei.money import AMOUNT_TOKEN, InvalidAmount, parse_brl_amount


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


@dataclass(frozen=True, slots=True)
class PeriodTotals:
    """Deterministic totals for a period. The only place these figures are defined.

    - operating_result: what the business produced (income minus business expenses).
    - net_after_withdrawals: what is left after the owner's personal withdrawals.

    Neither value is a guaranteed amount the owner can safely withdraw: future obligations,
    reserves and taxes are not considered.
    """

    income_cents: int
    expense_cents: int
    withdrawal_cents: int

    @property
    def operating_result_cents(self) -> int:
        return self.income_cents - self.expense_cents

    @property
    def net_after_withdrawals_cents(self) -> int:
        return self.operating_result_cents - self.withdrawal_cents


# A number, optionally preceded by "R$". Lookarounds stop matches inside words or larger numbers.
_NUMBER_RE = re.compile(rf"(?<![\w])(?P<prefix>r\$\s*)?(?P<number>{AMOUNT_TOKEN})(?![\w])")
_CURRENCY_SUFFIX_RE = re.compile(r"^\s*(?:reais|real|conto|contos)\b")
_TOTAL_PRICE_PREFIX_RE = re.compile(r"\bpor\s*$")
_UNIT_PRICE_PREFIX_RE = re.compile(r"\bde\s*$")
_EACH_RE = re.compile(r"\bcada\b")

_INCOME_RE = re.compile(r"\b(?:recebi|ganhei|entrou|vendi|fiz)\b")
_EXPENSE_RE = re.compile(r"\b(?:gastei|paguei|comprei|custou|despesa)\b")
_WITHDRAWAL_RE = re.compile(r"\b(?:retirei|tirei|saquei|pro[- ]labore)\b")


class ParseError(ValueError):
    """Raised when a message cannot be safely converted into a transaction."""


class AmbiguousTransaction(ParseError):
    """The amount is clear but the type is not: the user must choose among the candidates."""

    def __init__(
        self,
        message: str,
        *,
        amount_cents: int,
        description: str,
        candidates: tuple[TransactionType, ...],
    ) -> None:
        super().__init__(message)
        self.amount_cents = amount_cents
        self.description = description
        self.candidates = candidates


def parse_transaction(message: str, *, now: datetime | None = None) -> ParsedTransaction:
    """Parse high-confidence Portuguese transaction messages without an LLM.

    Ambiguous messages are rejected instead of guessing. A future AI parser can be used only
    as a structured fallback and must satisfy the same validation rules.
    """

    normalized = " ".join(message.lower().strip().split())
    if not normalized:
        raise ParseError("A mensagem está vazia.")
    folded = _fold_accents(normalized)

    # A payment to a person may be a business expense or a personal withdrawal.
    # The ledger must ask instead of inferring the user's intent.
    if folded.startswith("paguei ") and " para " in folded:
        amount_cents, description = _amount_and_description(normalized, "pagamento")
        raise AmbiguousTransaction(
            "Não ficou claro se é despesa do negócio ou retirada pessoal.",
            amount_cents=amount_cents,
            description=description,
            candidates=(TransactionType.EXPENSE, TransactionType.WITHDRAWAL),
        )

    groups = [
        (TransactionType.INCOME, _INCOME_RE.search(folded)),
        (TransactionType.EXPENSE, _EXPENSE_RE.search(folded)),
        (TransactionType.WITHDRAWAL, _WITHDRAWAL_RE.search(folded)),
    ]
    detected = [kind for kind, match in groups if match]
    if len(detected) != 1:
        raise ParseError("Não ficou claro se é entrada, despesa ou retirada pessoal.")

    amount_cents, description = _amount_and_description(normalized, detected[0].value)

    return ParsedTransaction(
        transaction_type=detected[0],
        amount_cents=amount_cents,
        description=description,
        occurred_at=now or datetime.now(UTC),
        confidence=Decimal("1.00"),
    )


def _amount_and_description(normalized: str, fallback: str) -> tuple[int, str]:
    amount_match = _select_amount(normalized)
    try:
        amount_cents = parse_brl_amount(amount_match.group("number"))
    except InvalidAmount as exc:
        raise ParseError(str(exc)) from exc
    description = (normalized[: amount_match.start()] + normalized[amount_match.end() :]).strip(
        " .,-"
    )
    return amount_cents, " ".join(description.split())[:500] or fallback


def _select_amount(normalized: str) -> re.Match[str]:
    """Pick the single number that is the transaction amount.

    One number: it is the amount. Several numbers ("Fiz 3 cortes por 90 reais"): exactly one
    must be marked as money (R$, "reais", or "por <valor>"); the rest are quantities. A unit
    price ("3 cortes de 30 reais", "30 cada") is rejected, because the total is not explicit.
    """

    matches = list(_NUMBER_RE.finditer(normalized))
    if not matches:
        raise ParseError("Informe o valor do lançamento.")
    if len(matches) == 1:
        return matches[0]

    unit_price_error = "Informe o valor total do lançamento, não o valor de cada item."
    if _EACH_RE.search(normalized):
        raise ParseError(unit_price_error)
    marked = [match for match in matches if _is_money_marked(normalized, match)]
    if len(marked) != 1:
        raise ParseError("Informe exatamente um valor por mensagem.")
    amount = marked[0]

    quantity_before_amount = any(match.start() < amount.start() for match in matches)
    if quantity_before_amount and _UNIT_PRICE_PREFIX_RE.search(normalized[: amount.start()]):
        raise ParseError(unit_price_error)
    return amount


def _is_money_marked(normalized: str, match: re.Match[str]) -> bool:
    return bool(
        match.group("prefix")
        or _CURRENCY_SUFFIX_RE.match(normalized[match.end() :])
        or _TOTAL_PRICE_PREFIX_RE.search(normalized[: match.start()])
    )


def _fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

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
    # True when the user named a day other than today ("ontem recebi 200").
    backdated: bool = False


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


class InvalidDate(ParseError):
    """The message names a day that cannot be used. The message is safe to show the user."""


class AmbiguousTransaction(ParseError):
    """The amount is clear but the type is not: the user must choose among the candidates."""

    def __init__(
        self,
        message: str,
        *,
        amount_cents: int,
        description: str,
        candidates: tuple[TransactionType, ...],
        occurred_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.amount_cents = amount_cents
        self.description = description
        self.candidates = candidates
        self.occurred_at = occurred_at


# Dates the user can attach to a transaction: "ontem", "anteontem", "dia 15", "15/11",
# "15/11/2026". Backdated entries are anchored at noon local time so they always fall on the
# named day, whatever the time zone offset.
MAX_BACKDATE_DAYS = 366
FUTURE_MEANS_LAST_YEAR_DAYS = 60
_RELATIVE_DATE_RE = re.compile(r"(?<!\w)(antes de ontem|anteontem|ontem|hoje)(?!\w)")
_SLASH_DATE_RE = re.compile(
    r"(?<![\w/.,])(?:(?:n?o\s+)?dia\s+)?(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\w/.,])"
)
_DAY_OF_MONTH_RE = re.compile(r"(?<!\w)(?:n?o\s+)?dia\s+(\d{1,2})(?![\w/.,])")
_RELATIVE_DAYS = {"hoje": 0, "ontem": 1, "anteontem": 2, "antes de ontem": 2}


def extract_date(normalized: str, today: date) -> tuple[date | None, str]:
    """Find the day a message refers to and return it with the text minus the date words.

    Returns (None, text) when no day is named. Raises ParseError for impossible, future or
    too old dates, or when more than one day is named.
    """

    found: list[tuple[re.Match[str], date]] = []
    for match in _RELATIVE_DATE_RE.finditer(normalized):
        found.append((match, today - timedelta(days=_RELATIVE_DAYS[match.group(1)])))
    for match in _SLASH_DATE_RE.finditer(normalized):
        day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
        if year is None:
            candidate = _safe_date(today.year, month, day)
            # "25/12" typed in January is last December; "30/11" typed on 21/11 is a mistake.
            if (candidate - today).days > FUTURE_MEANS_LAST_YEAR_DAYS:
                candidate = _safe_date(today.year - 1, month, day)
        else:
            full_year = int(year) + 2000 if len(year) == 2 else int(year)
            candidate = _safe_date(full_year, month, day)
        found.append((match, candidate))
    slash_spans = [match.span() for match, _ in found]
    for match in _DAY_OF_MONTH_RE.finditer(normalized):
        if any(start <= match.start() < end for start, end in slash_spans):
            continue
        day = int(match.group(1))
        year, month = today.year, today.month
        if day > today.day:
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        found.append((match, _safe_date(year, month, day)))

    if not found:
        return None, normalized
    if len(found) > 1:
        raise InvalidDate("Informe só uma data por lançamento.")
    match, day = found[0]
    if day > today:
        raise InvalidDate("Não registro lançamentos com data no futuro.")
    if (today - day).days > MAX_BACKDATE_DAYS:
        raise InvalidDate("Essa data é antiga demais. Informe uma data do último ano.")
    remaining = normalized[: match.start()] + " " + normalized[match.end() :]
    return day, " ".join(remaining.split())


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise InvalidDate("Essa data não existe.") from exc


def local_noon(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=tz)


def parse_transaction(
    message: str, *, now: datetime | None = None, tz: ZoneInfo | None = None
) -> ParsedTransaction:
    """Parse high-confidence Portuguese transaction messages without an LLM.

    Ambiguous messages are rejected instead of guessing. A future AI parser can be used only
    as a structured fallback and must satisfy the same validation rules.

    `now` is when the message was sent; a named day ("ontem", "dia 15") moves the transaction
    to noon of that day in the business's time zone `tz`.
    """

    now = now or datetime.now(UTC)
    tz = tz or ZoneInfo("America/Sao_Paulo")
    normalized = " ".join(message.lower().strip().split())
    if not normalized:
        raise ParseError("A mensagem está vazia.")
    aware_now = now if now.tzinfo else now.replace(tzinfo=UTC)
    today = aware_now.astimezone(tz).date()
    day, normalized = extract_date(normalized, today)
    backdated = day is not None and day != today
    occurred_at = local_noon(day, tz) if backdated else now
    if not normalized:
        raise ParseError("Informe o valor do lançamento.")
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
            occurred_at=occurred_at,
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
        occurred_at=occurred_at,
        confidence=Decimal("1.00"),
        backdated=backdated,
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

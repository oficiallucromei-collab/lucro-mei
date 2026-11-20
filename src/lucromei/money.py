"""Brazilian Real (BRL) parsing and formatting.

This is the single place where user-typed amounts become integer centavos. Every channel
(WhatsApp messages, conversational commands, future clients) must go through it so that the
same text always produces the same amount.

Brazilian convention: "." groups thousands and "," separates centavos. Users also type
"12.50" (a dot followed by exactly two digits), which is accepted as a decimal amount because
a thousands group always has three digits. Anything that cannot be read unambiguously is
rejected instead of guessed.
"""

import re

# Transactions are stored in a 32-bit integer column; this cap keeps every value far below
# that limit while being well above any plausible single MEI transaction.
MAX_AMOUNT_CENTS = 1_000_000_000  # R$ 10.000.000,00

# A number as typed by a user: digits with optional "." or "," separators. Validation of the
# grouping happens in parse_brl_amount, so this pattern is intentionally permissive.
AMOUNT_TOKEN = r"\d+(?:[.,]\d+)*"

_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(?:\.\d{3})+$")
_THOUSANDS_WITH_DECIMAL = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{1,2}$")
_PLAIN_WITH_DECIMAL = re.compile(r"^\d+[.,]\d{1,2}$")
_PLAIN_INTEGER = re.compile(r"^\d+$")


class InvalidAmount(ValueError):
    """Raised when a typed amount cannot be read as a single unambiguous BRL value."""


def parse_brl_amount(raw: str) -> int:
    """Convert a typed BRL amount into integer centavos.

    Accepted: "150", "1500", "1.500", "1.500,00", "12,5", "12,50", "12.50", "R$ 80,50".
    Rejected: "1,500" (three digits after the decimal comma), "1.234.56", "1,234.56",
    zero, and values above MAX_AMOUNT_CENTS.
    """

    cleaned = re.sub(r"^r\$\s*", "", raw.strip(), flags=re.IGNORECASE)
    if _PLAIN_INTEGER.fullmatch(cleaned):
        reais, centavos = cleaned, ""
    elif _THOUSANDS_ONLY.fullmatch(cleaned):
        reais, centavos = cleaned.replace(".", ""), ""
    elif _THOUSANDS_WITH_DECIMAL.fullmatch(cleaned):
        integer_part, centavos = cleaned.split(",")
        reais = integer_part.replace(".", "")
    elif _PLAIN_WITH_DECIMAL.fullmatch(cleaned):
        reais, centavos = re.split(r"[.,]", cleaned)
    else:
        raise InvalidAmount("O valor informado é inválido.")

    cents = int(reais) * 100 + int(centavos.ljust(2, "0") or "0")
    if cents <= 0:
        raise InvalidAmount("O valor deve ser maior que zero.")
    if cents > MAX_AMOUNT_CENTS:
        raise InvalidAmount("O valor informado é maior que o limite permitido.")
    return cents


def format_brl(cents: int) -> str:
    """Format integer centavos as "R$ 1.234,56" using integer arithmetic only."""

    sign = "-" if cents < 0 else ""
    reais, centavos = divmod(abs(cents), 100)
    return f"{sign}R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"

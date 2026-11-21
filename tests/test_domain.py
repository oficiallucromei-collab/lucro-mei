from datetime import UTC, datetime

import pytest

from lucromei.domain import ParseError, PeriodTotals, TransactionType, parse_transaction

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("message", "kind", "amount"),
    [
        ("Recebi 150 reais por um corte e barba", TransactionType.INCOME, 15000),
        ("Gastei R$ 80,50 em produtos", TransactionType.EXPENSE, 8050),
        ("Retirei 200 para mim", TransactionType.WITHDRAWAL, 20000),
        # Brazilian thousands separators and decimal formats.
        ("Recebi R$ 1.500,00 de um cliente", TransactionType.INCOME, 150000),
        ("Recebi 1.500 reais", TransactionType.INCOME, 150000),
        ("Entrou 1.234,56 no pix", TransactionType.INCOME, 123456),
        ("Gastei 12.50 em café", TransactionType.EXPENSE, 1250),
        ("Gastei R$80,5", TransactionType.EXPENSE, 8050),
        # A quantity next to an explicit total.
        ("Fiz 3 cortes por 90 reais", TransactionType.INCOME, 9000),
        ("Recebi 150 reais em 2 cortes", TransactionType.INCOME, 15000),
        ("Vendi 2 kits por 60", TransactionType.INCOME, 6000),
        ("Comprei 10 lâminas por 35", TransactionType.EXPENSE, 3500),
        # Accents and punctuation do not change the result.
        ("Pró-labore de 2.000", TransactionType.WITHDRAWAL, 200000),
        ("Tirei 200 pro mercado.", TransactionType.WITHDRAWAL, 20000),
    ],
)
def test_parse_supported_transactions(message: str, kind: TransactionType, amount: int) -> None:
    parsed = parse_transaction(message, now=NOW)
    assert parsed.transaction_type is kind
    assert parsed.amount_cents == amount
    assert parsed.occurred_at == NOW


@pytest.mark.parametrize(
    "message",
    [
        "Paguei 50 para João",
        "Recebi 100 e gastei 40",
        "Recebi 100 e 50",
        "Recebi 100 reais e 50 reais",
        "Olá",
        "Recebi",
        # Unit prices: the total is not explicit.
        "Fiz 3 cortes de 30 reais",
        "Vendi 2 kits, 30 cada",
        # Unreadable or out-of-range amounts.
        "Recebi 1,500",
        "Recebi 1,234.56",
        "Recebi 0",
        "Recebi 20000000",
    ],
)
def test_rejects_ambiguous_messages(message: str) -> None:
    with pytest.raises(ParseError):
        parse_transaction(message, now=NOW)


def test_keywords_match_whole_words_only() -> None:
    # "fizemos" must not be read as the income keyword "fiz".
    with pytest.raises(ParseError):
        parse_transaction("Fizemos 100", now=NOW)


def test_period_totals_separate_business_result_from_withdrawals() -> None:
    totals = PeriodTotals(income_cents=50000, expense_cents=12500, withdrawal_cents=10000)
    assert totals.operating_result_cents == 37500
    assert totals.net_after_withdrawals_cents == 27500

from datetime import UTC, datetime

import pytest

from lucromei.domain import ParseError, TransactionType, calculate_balance, parse_transaction

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("message", "kind", "amount"),
    [
        ("Recebi 150 reais por um corte e barba", TransactionType.INCOME, 15000),
        ("Gastei R$ 80,50 em produtos", TransactionType.EXPENSE, 8050),
        ("Retirei 200 para mim", TransactionType.WITHDRAWAL, 20000),
    ],
)
def test_parse_supported_transactions(message: str, kind: TransactionType, amount: int) -> None:
    parsed = parse_transaction(message, now=NOW)
    assert parsed.transaction_type is kind
    assert parsed.amount_cents == amount
    assert parsed.occurred_at == NOW


@pytest.mark.parametrize(
    "message",
    ["Paguei 50 para João", "Recebi 100 e gastei 40", "Recebi 100 e 50", "Olá"],
)
def test_rejects_ambiguous_messages(message: str) -> None:
    with pytest.raises(ParseError):
        parse_transaction(message, now=NOW)


def test_calculate_balance_separates_income_and_outflows() -> None:
    transactions = [
        parse_transaction("Recebi 500", now=NOW),
        parse_transaction("Gastei 125", now=NOW),
        parse_transaction("Retirei 100", now=NOW),
    ]
    assert calculate_balance(transactions) == 27500

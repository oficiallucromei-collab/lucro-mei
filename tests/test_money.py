import pytest

from lucromei.money import MAX_AMOUNT_CENTS, InvalidAmount, format_brl, parse_brl_amount


@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("150", 15000),
        ("1500", 150000),
        ("1.500", 150000),
        ("1.500,00", 150000),
        ("1.234.567", 123456700),
        ("1.234,56", 123456),
        ("12.505", 1250500),
        ("12,5", 1250),
        ("12,50", 1250),
        ("12.50", 1250),
        ("0,05", 5),
        ("R$ 80,50", 8050),
        ("r$80", 8000),
        ("10.000.000,00", MAX_AMOUNT_CENTS),
    ],
)
def test_parse_brl_amount(raw: str, cents: int) -> None:
    assert parse_brl_amount(raw) == cents


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "1,500", "1.234.56", "1,234.56", "12,505", "-5", "0", "0,00", "10.000.000,01"],
)
def test_parse_brl_amount_rejects_ambiguous_or_invalid(raw: str) -> None:
    with pytest.raises(InvalidAmount):
        parse_brl_amount(raw)


@pytest.mark.parametrize(
    ("cents", "text"),
    [(0, "R$ 0,00"), (5, "R$ 0,05"), (15000, "R$ 150,00"), (123456789, "R$ 1.234.567,89")],
)
def test_format_brl(cents: int, text: str) -> None:
    assert format_brl(cents) == text


def test_format_brl_negative() -> None:
    assert format_brl(-12500) == "-R$ 125,00"

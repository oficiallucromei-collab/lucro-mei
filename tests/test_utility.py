"""Daily use: backdated entries, day/week/month summaries, recent entries and help."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from support import (
    TestingSession,
    onboard,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.conversation import match_summary_period
from lucromei.domain import AmbiguousTransaction, ParseError, extract_date, parse_transaction
from lucromei.models import Transaction

SP = ZoneInfo("America/Sao_Paulo")


def ts(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> str:
    """Unix timestamp of a São Paulo local time, as Meta sends it."""
    return str(int(datetime(year, month, day, hour, minute, tzinfo=SP).timestamp()))


def setup_function() -> None:
    reset_state()
    onboard()


def say(message_id: str, text: str, timestamp: str) -> str:
    signed_webhook(whatsapp_payload(message_id, text, timestamp=timestamp))
    return sent_texts()[-1]


def local_days() -> list[date]:
    with TestingSession() as session:
        rows = session.query(Transaction).order_by(Transaction.created_at).all()
        return [
            (row.occurred_at if row.occurred_at.tzinfo else row.occurred_at.replace(tzinfo=UTC))
            .astimezone(SP)
            .date()
            for row in rows
        ]


# --- dates in messages ---------------------------------------------------------------------

TODAY = date(2026, 11, 21)


@pytest.mark.parametrize(
    ("text", "expected_day", "expected_rest"),
    [
        ("ontem recebi 200", date(2026, 11, 20), "recebi 200"),
        ("recebi 200 ontem", date(2026, 11, 20), "recebi 200"),
        ("anteontem gastei 50", date(2026, 11, 19), "gastei 50"),
        ("antes de ontem gastei 50", date(2026, 11, 19), "gastei 50"),
        ("hoje recebi 80", TODAY, "recebi 80"),
        ("dia 15 recebi 100", date(2026, 11, 15), "recebi 100"),
        ("no dia 15 recebi 100", date(2026, 11, 15), "recebi 100"),
        ("dia 25 recebi 100", date(2026, 10, 25), "recebi 100"),  # not yet: previous month
        ("15/11 recebi 100", date(2026, 11, 15), "recebi 100"),
        ("recebi 100 em 15/11/2026", date(2026, 11, 15), "recebi 100 em"),
        ("recebi 100 em 15/11/26", date(2026, 11, 15), "recebi 100 em"),
        ("recebi 150 por um corte", None, "recebi 150 por um corte"),
        ("fiz 3 cortes por 90 reais", None, "fiz 3 cortes por 90 reais"),
    ],
)
def test_extract_date(text: str, expected_day: date | None, expected_rest: str) -> None:
    assert extract_date(text, TODAY) == (expected_day, expected_rest)


def test_extract_date_in_january_rolls_back_to_december() -> None:
    assert extract_date("dia 30 recebi 10", date(2027, 1, 5))[0] == date(2026, 12, 30)
    assert extract_date("25/12 recebi 10", date(2027, 1, 5))[0] == date(2026, 12, 25)


@pytest.mark.parametrize(
    "text",
    [
        "31/11 recebi 100",  # no such day
        "dia 31 recebi 100",  # 31/10 exists, but test a month without it below
        "22/11/2026 recebi 100",  # future
        "01/01/2025 recebi 100",  # older than a year
        "ontem e anteontem recebi 100",  # two dates
    ],
)
def test_extract_date_rejects(text: str) -> None:
    today = date(2026, 12, 1) if text.startswith("dia 31") else TODAY  # 31/11 does not exist
    with pytest.raises(ParseError):
        extract_date(text, today)


def test_backdated_entry_is_anchored_at_local_noon_across_midnight_utc() -> None:
    # 23:30 on 20/11 in São Paulo is already 21/11 in UTC: "ontem" must still mean 19/11.
    now = datetime(2026, 11, 20, 23, 30, tzinfo=SP).astimezone(UTC)
    parsed = parse_transaction("ontem recebi 200", now=now, tz=SP)
    assert parsed.backdated
    assert parsed.occurred_at == datetime(2026, 11, 19, 12, 0, tzinfo=SP)
    assert parsed.amount_cents == 20000
    assert parsed.description == "recebi"
    assert not parse_transaction("hoje recebi 200", now=now, tz=SP).backdated


def test_ambiguous_payment_keeps_its_date() -> None:
    now = datetime(2026, 11, 21, 10, tzinfo=SP)
    with pytest.raises(AmbiguousTransaction) as raised:
        parse_transaction("ontem paguei 300 para o fornecedor", now=now, tz=SP)
    assert raised.value.occurred_at == datetime(2026, 11, 20, 12, tzinfo=SP)


def test_whatsapp_backdated_entry_and_undo() -> None:
    sent = ts(2026, 11, 21, 9)
    assert say("m1", "Recebi 100 por um corte", sent).startswith("Entrada registrada: R$ 100,00")
    reply = say("m2", "ontem recebi 200", sent)
    assert reply == (
        "Entrada registrada em 20/11/2026: R$ 200,00. Responda 'desfazer' para corrigir."
    )
    assert local_days() == [date(2026, 11, 21), date(2026, 11, 20)]
    # "desfazer" undoes the entry just recorded, even though its date is older.
    assert say("m3", "desfazer", sent) == "Lançamento de R$ 200,00 desfeito."


def test_whatsapp_rejects_future_date_without_recording() -> None:
    reply = say("m1", "dia 30/11 recebi 100", ts(2026, 11, 21))
    assert reply == "Não registro lançamentos com data no futuro. Nada foi anotado."
    assert local_days() == []


def test_ambiguous_backdated_payment_is_recorded_on_its_day() -> None:
    sent = ts(2026, 11, 21, 9)
    assert say("m1", "ontem paguei 300 para o fornecedor", sent).startswith("Antes de anotar")
    assert say("m2", "1", sent).startswith("Despesa registrada em 20/11/2026: R$ 300,00")
    assert local_days() == [date(2026, 11, 20)]


# --- summaries ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("resumo do mes", ("month", 0)),
        ("resumo deste mes", ("month", 0)),
        ("resumo mensal", ("month", 0)),
        ("quanto lucrei este mes", ("month", 0)),
        ("quanto lucrei no mes", ("month", 0)),
        ("resumo", ("month", 0)),
        ("resumo do mes passado", ("month", 1)),
        ("quanto ganhei essa semana?", ("week", 0)),
        ("quanto eu ganhei nesta semana", ("week", 0)),
        ("resumo da semana", ("week", 0)),
        ("resumo semanal", ("week", 0)),
        ("resumo da semana passada", ("week", 1)),
        ("quanto ganhei hoje", ("day", 0)),
        ("como foi hoje?", ("day", 0)),
        ("resumo de ontem", ("day", 1)),
        ("quanto gastei esse mes", ("month", 0)),
        ("quanto ganhei", None),
        ("resumo do ano", None),
        ("resumo de ontem passado", None),
        ("resumos", None),
        ("recebi 150 por um corte", None),
    ],
)
def test_match_summary_period(text: str, expected) -> None:
    assert match_summary_period(text) == expected


def test_weekly_summary_counts_monday_to_now_in_local_time() -> None:
    # Week of Monday 16/11 to Sunday 22/11/2026.
    say("a", "Recebi 100 por um corte", ts(2026, 11, 15, 23, 59))  # Sunday before: excluded
    say("b", "Recebi 200 por um corte", ts(2026, 11, 16, 0, 1))  # Monday 00:01 local
    say("c", "Gastei 50 em produtos", ts(2026, 11, 18))
    say("d", "Retirei 30 para mim", ts(2026, 11, 20))
    reply = say("e", "quanto ganhei essa semana?", ts(2026, 11, 21, 18))
    assert reply.startswith("Resumo da semana (16/11 a 22/11):")
    assert "Entradas: R$ 200,00" in reply
    assert "Despesas do negócio: R$ 50,00" in reply
    assert "Resultado do negócio: R$ 150,00" in reply
    assert "Retiradas pessoais: R$ 30,00" in reply
    assert "Sobra após retiradas: R$ 120,00" in reply
    last_week = say("f", "resumo da semana passada", ts(2026, 11, 21, 18))
    assert last_week.startswith("Resumo da semana passada (09/11 a 15/11):")
    assert "Entradas: R$ 100,00" in last_week


def test_daily_summary_and_backdated_entries() -> None:
    say("a", "Recebi 100 por um corte", ts(2026, 11, 21, 8))
    say("b", "ontem recebi 40", ts(2026, 11, 21, 8))
    today = say("c", "resumo de hoje", ts(2026, 11, 21, 20))
    assert today.startswith("Resumo de hoje (21/11):")
    assert "Entradas: R$ 100,00" in today
    yesterday = say("d", "resumo de ontem", ts(2026, 11, 21, 20))
    assert yesterday.startswith("Resumo de ontem (20/11):")
    assert "Entradas: R$ 40,00" in yesterday


def test_previous_month_summary_wraps_the_year() -> None:
    say("a", "Recebi 70 por um corte", ts(2026, 12, 31, 22))
    reply = say("b", "resumo do mes passado", ts(2027, 1, 2))
    assert reply.startswith("Resumo de dezembro/2026:")
    assert "Entradas: R$ 70,00" in reply


# --- recent entries and help ----------------------------------------------------------------


def test_recent_entries_newest_first_without_undone() -> None:
    assert say("a", "ultimos lancamentos", ts(2026, 11, 21)).startswith(
        "Você ainda não tem lançamentos"
    )
    say("b", "Recebi 100 por um corte", ts(2026, 11, 20))
    say("c", "Gastei 30 em produtos", ts(2026, 11, 21))
    say("d", "Retirei 99 para mim", ts(2026, 11, 21))
    say("e", "desfazer", ts(2026, 11, 21))
    say("f", "ontem recebi 50", ts(2026, 11, 21))
    reply = say("g", "Últimos lançamentos", ts(2026, 11, 21))
    assert reply.splitlines()[:4] == [
        "Seus últimos 3 lançamentos:",
        "20/11 Entrada R$ 50,00 - recebi",
        "21/11 Despesa R$ 30,00 - gastei em produtos",
        "20/11 Entrada R$ 100,00 - recebi por um corte",
    ]
    assert "R$ 99,00" not in reply


def test_recent_entries_limit_and_duplicate_delivery() -> None:
    for index in range(12):
        say(f"r{index}", f"Recebi {index + 1} por um corte", ts(2026, 11, 21))
    reply = say("list", "extrato", ts(2026, 11, 21))
    assert reply.startswith("Seus últimos 10 lançamentos:")
    assert "R$ 12,00" in reply and "R$ 2,00" not in reply.replace("R$ 12,00", "")
    count = len(sent_texts())
    signed_webhook(whatsapp_payload("list", "extrato", timestamp=ts(2026, 11, 21)))
    assert len(sent_texts()) == count  # redelivered webhook: no second reply


def test_help_lists_commands() -> None:
    reply = say("h", "ajuda", ts(2026, 11, 21))
    assert "resumo de hoje / da semana / do mês" in reply
    assert "últimos lançamentos" in reply
    assert "APAGAR MEUS DADOS" in reply

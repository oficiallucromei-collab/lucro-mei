"""Several entries in one message: preview, confirmation, one all-or-nothing write."""

import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from support import (
    TestingSession,
    onboard,
    post_webhook,
    requires_postgres,
    reset_state,
    sent_texts,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.batch import is_batch_message, split_entries
from lucromei.config import get_settings
from lucromei.jobs import JobContext, RetryLater
from lucromei.main import app
from lucromei.models import InboundMessage, PendingEntry, Transaction, TransactionEvent
from lucromei.processing import handle_process_message
from lucromei.service import LedgerService

SP = ZoneInfo("America/Sao_Paulo")


def ts(day: int = 21, hour: int = 12) -> str:
    return str(int(datetime(2026, 11, day, hour, tzinfo=SP).timestamp()))


def setup_function() -> None:
    reset_state()
    onboard()


def say(message_id: str, text: str, timestamp: str | None = None) -> str:
    signed_webhook(whatsapp_payload(message_id, text, timestamp=timestamp or ts()))
    return sent_texts()[-1]


def recorded() -> list[tuple[str, str, int]]:
    with TestingSession() as session:
        rows = session.execute(
            select(
                InboundMessage.external_id, Transaction.transaction_type, Transaction.amount_cents
            )
            .join(Transaction, Transaction.source_message_id == InboundMessage.id)
            .order_by(InboundMessage.external_id)
        ).all()
        return [tuple(row) for row in rows]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Recebi 150 por um corte\nGastei 30 em produtos",
            ["Recebi 150 por um corte", "Gastei 30 em produtos"],
        ),
        ("recebi 150; gastei 30;", ["recebi 150", "gastei 30"]),
        ("recebi 150 e gastei 30", ["recebi 150", "gastei 30"]),
        ("recebi 150, ontem gastei 30", ["recebi 150", "ontem gastei 30"]),
        ("recebi 150 também paguei 20 de luz", ["recebi 150", "paguei 20 de luz"]),
        ("• recebi 10\n• recebi 20\n\n", ["recebi 10", "recebi 20"]),
        ("Fiz 3 cortes e barba por 90 reais", ["Fiz 3 cortes e barba por 90 reais"]),
        ("recebi 150 de corte e barba", ["recebi 150 de corte e barba"]),
    ],
)
def test_split_entries(text: str, expected: list[str]) -> None:
    assert split_entries(text) == expected


def test_single_messages_are_never_batches() -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=SP)
    assert not is_batch_message("Fiz 3 cortes por 90 reais", now=now, tz=SP)
    assert not is_batch_message("Recebi 150 por um corte", now=now, tz=SP)
    assert is_batch_message("recebi 150 e gastei 30", now=now, tz=SP)


def test_preview_then_sim_records_all_entries_once() -> None:
    preview = say("b1", "Recebi 150 por um corte\nontem gastei 30 em produtos\nRetirei 50 para mim")
    assert preview == (
        "Vou anotar 3 lançamentos:\n"
        "1. Entrada R$ 150,00 - recebi por um corte\n"
        "2. Despesa R$ 30,00 - gastei em produtos (20/11)\n"
        "3. Retirada R$ 50,00 - retirei para mim\n\n"
        "Total: entradas R$ 150,00, despesas R$ 30,00, retiradas R$ 50,00.\n\n"
        "Responda SIM para anotar todos ou NÃO para cancelar."
    )
    assert recorded() == []
    reply = say("b2", "sim")
    assert reply.startswith("Pronto, anotei 3 lançamentos. Total: entradas R$ 150,00")
    assert recorded() == [
        ("b1#1", "income", 15000),
        ("b1#2", "expense", 3000),
        ("b1#3", "withdrawal", 5000),
    ]
    with TestingSession() as session:
        pending = session.scalar(select(PendingEntry))
        assert (pending.kind, pending.status, pending.resolution_message_id) == (
            "batch",
            "resolved",
            "b2",
        )
        created = session.scalars(select(TransactionEvent.changes)).all()
        assert {change["confirmed_by_message_id"] for change in created} == {"b2"}
        assert sorted(change["batch_index"] for change in created) == [1, 2, 3]
    # A redelivered confirmation and a stray second SIM record nothing more.
    signed_webhook(whatsapp_payload("b2", "sim", timestamp=ts()))
    say("b3", "sim")
    assert len(recorded()) == 3


def test_nao_cancels_the_whole_batch() -> None:
    say("b1", "recebi 150 e gastei 30")
    assert say("b2", "não") == "Tudo bem, não anotei nenhum desses lançamentos."
    assert recorded() == []


def test_other_answer_keeps_batch_open_and_new_entry_replaces_it() -> None:
    say("b1", "recebi 150 e gastei 30")
    assert say("b2", "Recebi 10 por um corte").startswith("Entrada registrada: R$ 10,00")
    assert say("b3", "sim").startswith("Pronto, anotei 2 lançamentos")
    say("b4", "recebi 1 e recebi 2")  # opens a new batch...
    say("b5", "Paguei 300 para o fornecedor")  # ...replaced by a single question
    with TestingSession() as session:
        statuses = session.scalars(select(PendingEntry.status).order_by(PendingEntry.created_at))
        assert list(statuses) == ["resolved", "replaced", "open"]


def test_batch_with_problems_records_nothing_and_names_the_lines() -> None:
    reply = say(
        "b1", "recebi 150\nobrigado\npaguei 300 para o fornecedor\namanhã recebi 5 em 30/11"
    )
    assert reply.startswith(
        "Não anotei nada desta mensagem:\nLinha 2: não entendi o tipo ou o valor."
    )
    assert "Linha 3: não sei se é despesa do negócio ou retirada pessoal." in reply
    assert "Linha 4: Não registro lançamentos com data no futuro." in reply
    assert recorded() == []
    with TestingSession() as session:
        assert session.scalar(select(PendingEntry)) is None


def test_too_many_entries() -> None:
    text = "\n".join(f"recebi {n}" for n in range(1, 12))
    assert "Mande até 10 lançamentos por mensagem." in say("b1", text)
    assert recorded() == []


def test_undo_after_batch_reverses_every_entry_of_that_message() -> None:
    say("s1", "Recebi 99 por um corte")
    say("b1", "recebi 150 e gastei 30")
    say("b2", "sim")
    assert say("u1", "desfazer") == "Desfiz os 2 lançamentos da mesma mensagem (soma de R$ 180,00)."
    summary = say("r1", "resumo de hoje")
    assert "Entradas: R$ 99,00" in summary
    assert "Despesas do negócio: R$ 0,00" in summary


def test_expired_batch_is_not_recorded() -> None:
    say("b1", "recebi 150 e gastei 30")
    with TestingSession() as session:
        pending = session.scalar(select(PendingEntry))
        pending.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    assert "expirou" in say("b2", "sim")
    assert recorded() == []


def test_failure_mid_batch_rolls_back_every_entry() -> None:
    say("b1", "recebi 150 e gastei 30")
    with TestingSession() as session:
        pending = session.scalar(select(PendingEntry))
        items = [dict(item) for item in pending.items]
        items[1]["amount_cents"] = 0  # violates the ledger's positive-amount constraint
        pending.items = items
        session.commit()
    get_settings().job_max_attempts = 1
    say("b2", "sim")
    assert recorded() == []
    with TestingSession() as session:
        assert session.scalar(select(PendingEntry.status)) == "open"


def test_record_batch_is_idempotent() -> None:
    say("b1", "recebi 150 e gastei 30")
    with TestingSession() as session:
        pending = session.scalar(select(PendingEntry))
        ledger = LedgerService(session)
        first = ledger.record_batch(pending, resolution_message_id="x")
        second = ledger.record_batch(pending, resolution_message_id="y")
        session.commit()
        assert [item.id for item in first] == [item.id for item in second]
    assert len(recorded()) == 2


def test_retention_erases_closed_batch_items() -> None:
    from lucromei.privacy import purge_old_text

    say("b1", "recebi 150 e gastei 30")
    say("b2", "não")
    with TestingSession() as session:
        session.query(PendingEntry).update({"created_at": datetime.now(UTC) - timedelta(days=91)})
        session.commit()
        purged = purge_old_text(session, datetime.now(UTC), get_settings())
        session.commit()
        assert purged["pending_batches"] == 1
        assert session.scalar(select(PendingEntry)).items is None


@requires_postgres
def test_concurrent_confirmations_record_the_batch_once() -> None:
    say("b1", "recebi 150 e gastei 30")
    post_webhook(whatsapp_payload("c1", "sim", timestamp=ts()))
    post_webhook(whatsapp_payload("c2", "sim", timestamp=ts()))
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def process(message_id: str) -> None:
        with TestingSession() as session:
            barrier.wait()
            try:
                handle_process_message(
                    session,
                    {"external_id": message_id},
                    JobContext(
                        now=datetime.now(UTC),
                        services={"sender": app.state.whatsapp_sender, "max_attempts": 8},
                    ),
                )
                session.commit()
            except RetryLater:
                session.rollback()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                session.rollback()

    threads = [threading.Thread(target=process, args=(key,)) for key in ("c1", "c2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert [row[0] for row in recorded()] == ["b1#1", "b1#2"]

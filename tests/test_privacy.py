import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from support import (
    DEFAULT_SENDER,
    TestingSession,
    onboard,
    post_webhook,
    reset_state,
    sent_texts,
    whatsapp_payload,
    worker,
)

from lucromei import privacy
from lucromei.config import Settings, check_production_settings, get_settings
from lucromei.jobs import JobContext, enqueue
from lucromei.main import app
from lucromei.models import (
    AIUsage,
    Business,
    CommandReceipt,
    DeletionRequest,
    DeliveryStatusEvent,
    InboundEvent,
    InboundMessage,
    Job,
    OutboundMessage,
    PendingEntry,
    TermsAcceptance,
    Transaction,
    TransactionEvent,
)
from lucromei.processing import enqueue_maintenance

OTHER = "5531977776666"
START = datetime(2026, 11, 21, 15, 0, tzinfo=UTC)
_clock = {"ts": 1795032000}


def setup_function() -> None:
    reset_state()
    onboard()
    onboard(OTHER, name="Bia")
    _clock["ts"] = 1795032000


def context(now: datetime) -> JobContext:
    return JobContext(
        now=now,
        services={"sender": app.state.whatsapp_sender, "interpreter": None, "max_attempts": 8},
    )


def run_jobs(now: datetime) -> None:
    queue = worker().queue
    for step in range(12):  # later messages and reply parts wait a little for earlier ones
        queue.run_due(context(now + timedelta(seconds=3 * step)))


def say(text: str, *, now: datetime = START, sender: str = DEFAULT_SENDER) -> list[str]:
    before = len(sent_texts())
    _clock["ts"] += 60
    message_id = f"wamid.{_clock['ts']}.{sender[-4:]}"
    post_webhook(whatsapp_payload(message_id, text, sender, timestamp=str(_clock["ts"])))
    run_jobs(now)
    return sent_texts()[before:]


def count(model, **filters) -> int:
    with TestingSession() as session:
        return session.query(model).filter_by(**filters).count()


def business_id(number: str = DEFAULT_SENDER) -> str:
    with TestingSession() as session:
        return session.query(Business).filter_by(whatsapp_number=number).one().id


# --- export -------------------------------------------------------------------------------


def test_export_lists_profile_terms_and_effective_transactions() -> None:
    say("Recebi 150 por um corte")
    say("Gastei 80 em produtos")
    say("corrigir despesa para 75,50")
    say("Retirei 100 para mim")
    say("desfazer")

    [export] = say("Meus dados")

    assert "Número do WhatsApp: +5511999999999" in export
    assert "Nome: Ana" in export
    assert "Tipo de negócio: barbearia" in export
    assert "Termos aceitos: versão 2026-11-21" in export
    assert "Lançamentos (3):" in export
    assert "Entrada R$ 150,00 - recebi por um corte" in export
    assert "Despesa R$ 75,50 - gastei em produtos (corrigido)" in export
    assert "Retirada pessoal R$ 100,00 - retirei para mim (desfeito)" in export
    assert "APAGAR MEUS DADOS" in export


def test_export_works_before_onboarding_is_finished() -> None:
    [welcome] = say("Oi", sender="5521900001111")
    assert "como você se chama?" in welcome
    [export] = say("meus dados", sender="5521900001111")
    assert "Nome: não informado" in export
    assert "nenhum lançamento registrado" in export


def _add_transactions(number: str, how_many: int) -> None:
    with TestingSession() as session:
        owner = session.query(Business).filter_by(whatsapp_number=number).one()
        for index in range(how_many):
            inbound = InboundMessage(
                external_id=f"bulk-{index}", business_id=owner.id, raw_text="x"
            )
            transaction = Transaction(
                business_id=owner.id,
                source_message=inbound,
                transaction_type="income",
                amount_cents=1000 + index,
                description=f"corte numero {index} com uma descricao razoavelmente longa",
                occurred_at=START - timedelta(minutes=index),
            )
            transaction.events.append(TransactionEvent(event_type="created", changes={}))
            session.add(transaction)
        session.commit()


def test_long_export_is_split_into_ordered_parts() -> None:
    _add_transactions(DEFAULT_SENDER, 150)
    parts = say("meus dados")
    assert len(parts) > 1
    for index, part in enumerate(parts, 1):
        assert part.startswith(f"(parte {index}/{len(parts)})")
        assert len(part) <= 4096
    assert "Lançamentos (150):" in parts[0]


def test_very_long_export_is_capped_and_says_so() -> None:
    _add_transactions(DEFAULT_SENDER, 900)
    parts = say("meus dados")
    assert len(parts) == privacy.MAX_EXPORT_PARTS
    assert "foi cortada aqui" in parts[-1]


def test_reply_parts_wait_for_the_previous_part() -> None:
    _add_transactions(DEFAULT_SENDER, 150)
    _clock["ts"] += 60
    post_webhook(whatsapp_payload("wamid.export", "meus dados", timestamp=str(_clock["ts"])))
    queue = worker().queue
    queue.run_due(context(START))  # process: queues every part
    sends = queue.claim(limit=20, now=START)
    assert len(sends) > 2
    # The worker happens to reach the last part first: it must wait.
    assert queue.run(sends[-1], context(START)) == "pending"
    for job in sends[:-1]:
        queue.run(job, context(START))
    run_jobs(START + timedelta(seconds=5))
    texts = sent_texts()
    assert [text.split(")")[0] for text in texts] == [
        f"(parte {i}/{len(texts)}" for i in range(1, len(texts) + 1)
    ]


# --- deletion -----------------------------------------------------------------------------


def request_deletion(now: datetime = START) -> None:
    [warning] = say("Apagar meus dados", now=now)
    assert "não tem como desfazer" in warning
    assert "responda APAGAR" in warning
    [scheduled] = say("APAGAR", now=now + timedelta(minutes=2))
    assert "Seus dados serão apagados em 28/11/2026" in scheduled


def test_confirmed_deletion_blocks_account_then_erases_everything_after_grace() -> None:
    say("Recebi 150 por um corte")
    say("Paguei 300 para o fornecedor")  # leaves an open question (pending entry)
    say("Recebi 50 por um corte", sender=OTHER)
    with TestingSession() as session:
        owner = session.query(Business).filter_by(whatsapp_number=DEFAULT_SENDER).one()
        session.add(
            AIUsage(
                business_id=owner.id,
                inbound_message_id="x",
                model="m",
                outcome="understood",
                created_at=START,
            )
        )
        provider_id = session.query(OutboundMessage).filter_by(
            recipient=DEFAULT_SENDER
        ).first().provider_message_id
        session.add(DeliveryStatusEvent(provider_message_id=provider_id, status="read"))
        session.commit()
    say("resumo do mês")  # command receipt
    owner_id = business_id()

    request_deletion()

    [blocked] = say("Recebi 999 por um corte", now=START + timedelta(days=1))
    assert "marcada para ser apagada em 28/11/2026" in blocked
    assert count(Transaction, business_id=owner_id) == 1  # nothing new recorded
    assert "Lançamentos (1):" in say("meus dados", now=START + timedelta(days=1))[0]

    # Maintenance before the grace period ends does nothing.
    run_maintenance(START + timedelta(days=6))
    assert count(Business, id=owner_id) == 1

    run_maintenance(START + timedelta(days=7, minutes=5))

    for model, filters in [
        (Business, {"id": owner_id}),
        (Transaction, {"business_id": owner_id}),
        (InboundMessage, {"business_id": owner_id}),
        (CommandReceipt, {"business_id": owner_id}),
        (PendingEntry, {"business_id": owner_id}),
        (TermsAcceptance, {"business_id": owner_id}),
        (AIUsage, {"business_id": owner_id}),
        (InboundEvent, {"sender": DEFAULT_SENDER}),
        (OutboundMessage, {"recipient": DEFAULT_SENDER}),
        (DeliveryStatusEvent, {"provider_message_id": provider_id}),
    ]:
        assert count(model, **filters) == 0, model.__name__
    with TestingSession() as session:
        remaining_ids = {t.id for t in session.query(Transaction)}
        assert session.query(TransactionEvent).filter(
            TransactionEvent.transaction_id.notin_(remaining_ids)
        ).count() == 0
        request = session.query(DeletionRequest).one()
        expected = hmac.new(
            get_settings().privacy_hash_key.encode(), DEFAULT_SENDER.encode(), hashlib.sha256
        ).hexdigest()
        assert (request.status, request.subject_digest) == ("completed", expected)
        assert request.completed_at is not None
        stored = " ".join(str(getattr(request, c.name)) for c in request.__table__.columns)
        assert DEFAULT_SENDER not in stored
    # The other customer is untouched.
    assert count(Business, whatsapp_number=OTHER) == 1
    assert count(Transaction, business_id=business_id(OTHER)) == 1


def run_maintenance(now: datetime) -> None:
    with TestingSession() as session:
        enqueue(session, "maintenance", {}, dedupe_key=f"m-{now.isoformat()}", max_attempts=3)
        session.commit()
    run_jobs(now)


def test_same_number_starts_over_after_deletion() -> None:
    request_deletion()
    run_maintenance(START + timedelta(days=8))
    [welcome] = say("Recebi 150 por um corte", now=START + timedelta(days=8))
    assert "como você se chama?" in welcome
    assert count(Transaction) == 0
    assert count(Business, whatsapp_number=DEFAULT_SENDER, onboarding_step="awaiting_name") == 1


def test_cancel_during_grace_period_keeps_the_account() -> None:
    say("Recebi 150 por um corte")
    request_deletion()
    [cancelled] = say("cancelar exclusão", now=START + timedelta(days=2))
    assert cancelled.startswith("Pronto, cancelei a exclusão")
    [recorded] = say("Gastei 20 em produtos", now=START + timedelta(days=2))
    assert recorded.startswith("Despesa registrada")
    run_maintenance(START + timedelta(days=10))
    assert count(Transaction, business_id=business_id()) == 2
    with TestingSession() as session:
        assert session.query(DeletionRequest).one().status == "cancelled"


def test_late_confirmation_does_not_schedule_deletion() -> None:
    say("apagar meus dados")
    [reply] = say("APAGAR", now=START + timedelta(minutes=11))
    assert "Seus dados serão apagados" not in reply
    with TestingSession() as session:
        assert session.query(DeletionRequest).one().status == "expired"


def test_any_other_message_drops_the_pending_confirmation() -> None:
    say("apagar meus dados")
    [reply] = say("Recebi 40 por um corte", now=START + timedelta(minutes=1))
    assert reply.startswith("Entrada registrada: R$ 40,00")
    with TestingSession() as session:
        assert session.query(DeletionRequest).one().status == "expired"
    [later] = say("APAGAR", now=START + timedelta(minutes=2))
    assert "Seus dados serão apagados" not in later


def test_deletion_does_not_run_if_cancelled_after_being_queued() -> None:
    request_deletion()
    with TestingSession() as session:
        request_id = session.query(DeletionRequest).one().id
        assert privacy.due_deletions(session, START + timedelta(days=8)) == [request_id]
    say("cancelar exclusão", now=START + timedelta(days=8))
    with TestingSession() as session:
        assert privacy.execute_deletion(session, request_id, START + timedelta(days=8)) is False
    assert count(Business, id=business_id()) == 1


def test_deletion_is_idempotent() -> None:
    request_deletion()
    later = START + timedelta(days=8)
    with TestingSession() as session:
        request_id = session.query(DeletionRequest).one().id
        assert privacy.execute_deletion(session, request_id, later) is True
        session.commit()
    with TestingSession() as session:
        assert privacy.execute_deletion(session, request_id, later) is False


def test_production_requires_privacy_hash_key() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        whatsapp_app_secret="real-secret",
        whatsapp_verify_token="real-token",
        terms_url="https://lucromei.com.br/termos",
        privacy_hash_key="short",
    )
    with pytest.raises(RuntimeError, match="LUCROMEI_PRIVACY_HASH_KEY"):
        check_production_settings(settings)


# --- retention ----------------------------------------------------------------------------


def test_retention_erases_old_message_text_but_keeps_ledger_and_recent_text() -> None:
    old, recent = START - timedelta(days=91), START - timedelta(days=10)
    owner_id = business_id()
    with TestingSession() as session:
        for label, when in (("old", old), ("recent", recent)):
            session.add(
                InboundEvent(
                    external_id=f"ev-{label}",
                    sender=DEFAULT_SENDER,
                    message_type="text",
                    text=f"texto {label}",
                    sent_at=when,
                    status="processed",
                    received_at=when,
                )
            )
            session.add(
                OutboundMessage(
                    recipient=DEFAULT_SENDER,
                    in_reply_to=f"ev-{label}",
                    text=f"resposta {label}",
                    status="sent",
                    created_at=when,
                )
            )
            session.add(
                CommandReceipt(
                    external_id=f"cmd-{label}",
                    business_id=owner_id,
                    command_type="summary",
                    response_text=f"resumo {label}",
                    created_at=when,
                )
            )
            for status in ("resolved", "open"):
                session.add(
                    PendingEntry(
                        business_id=owner_id,
                        source_message_id=f"pe-{label}-{status}",
                        source_text=f"paguei {label}",
                        amount_cents=100,
                        description="x",
                        occurred_at=when,
                        candidate_types=["expense"],
                        status=status if label == "old" or status == "resolved" else "expired",
                        created_at=when,
                        expires_at=when + timedelta(days=1),
                    )
                )
        inbound = InboundMessage(external_id="ledger-old", business_id=owner_id, raw_text="Recebi")
        session.add(
            Transaction(
                business_id=owner_id,
                source_message=inbound,
                transaction_type="income",
                amount_cents=100,
                description="x",
                occurred_at=old,
            )
        )
        for key, status, finished in (
            ("done-old", "done", START - timedelta(days=8)),
            ("done-new", "done", START - timedelta(days=2)),
            ("dead-old", "dead", old),
            ("dead-new", "dead", recent),
        ):
            session.add(
                Job(
                    kind="noop",
                    dedupe_key=key,
                    payload={},
                    status=status,
                    attempts=1,
                    max_attempts=3,
                    run_after=finished,
                    finished_at=finished,
                )
            )
        session.commit()

    with TestingSession() as session:
        purged = privacy.purge_old_text(session, START, get_settings())
        session.commit()

    assert purged["inbound_events"] == 1 and purged["outbound_messages"] == 1
    with TestingSession() as session:
        events = {e.external_id: e.text for e in session.query(InboundEvent)}
        assert events == {"ev-old": None, "ev-recent": "texto recent"}
        replies = {o.in_reply_to: o.text for o in session.query(OutboundMessage)}
        assert replies == {"ev-old": "", "ev-recent": "resposta recent"}
        receipts = {r.external_id: r.response_text for r in session.query(CommandReceipt)}
        assert receipts == {"cmd-old": "", "cmd-recent": "resumo recent"}
        pendings = {p.source_message_id: p.source_text for p in session.query(PendingEntry)}
        assert pendings["pe-old-resolved"] == ""
        assert pendings["pe-old-open"] == "paguei old"  # still waiting for an answer
        assert pendings["pe-recent-resolved"] == "paguei recent"
        assert session.query(InboundMessage).filter_by(external_id="ledger-old").one().raw_text
        assert {j.dedupe_key for j in session.query(Job)} == {"done-new", "dead-new"}


def test_maintenance_is_scheduled_once_per_interval_across_workers() -> None:
    with TestingSession() as session:
        for offset, expected in ((0, True), (30, False), (60, True)):
            when = START + timedelta(minutes=offset)
            assert enqueue_maintenance(session, when, interval_seconds=3600) is expected
        session.commit()
    assert count(Job, kind="maintenance") == 2

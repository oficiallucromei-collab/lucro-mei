# ruff: noqa: E501 - one SQL statement per line reads better here
"""Operator tools, run as the lucromei-ops Cloud Run job (runtime database role).

    gcloud run jobs execute lucromei-ops --region=southamerica-east1 --wait \
      --args=pilot-check,--hours,2

Output goes to Cloud Logging. Every command prints counts and identifiers only, never message
text, amounts tied to a person, names or phone numbers.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from lucromei import privacy, subscription
from lucromei.config import get_settings
from lucromei.models import (
    Business,
    DeletionRequest,
    InboundEvent,
    Job,
    OutboundMessage,
    Subscription,
    Transaction,
)

CHECKS = [
    ("Cadastros por etapa", "SELECT onboarding_step, count(*) FROM businesses GROUP BY 1"),
    (
        "Aceites dos termos por versão",
        "SELECT terms_version, count(*) FROM terms_acceptances GROUP BY 1",
    ),
    (
        "Mensagens recebidas (período) por tipo e status",
        "SELECT message_type, status, count(*) FROM inbound_events WHERE received_at >= :since GROUP BY 1, 2",
    ),
    (
        "Respostas (período) por status e erro",
        "SELECT status, coalesce(error_code, '-'), coalesce(template_name, 'texto'), count(*) "
        "FROM outbound_messages WHERE created_at >= :since GROUP BY 1, 2, 3",
    ),
    (
        "Recibos de entrega (período) por status e erro",
        "SELECT status, coalesce(error_code, '-'), count(*) FROM whatsapp_status_events "
        "WHERE received_at >= :since GROUP BY 1, 2",
    ),
    (
        "Respostas enviadas sem recibo 'delivered'/'read' (período)",
        "SELECT count(*) FROM outbound_messages o WHERE o.status = 'sent' AND o.created_at >= :since "
        "AND NOT EXISTS (SELECT 1 FROM whatsapp_status_events s WHERE s.provider_message_id = "
        "o.provider_message_id AND s.status IN ('delivered', 'read'))",
    ),
    (
        "Lançamentos (período) por tipo",
        "SELECT transaction_type, count(*), sum(1) FILTER (WHERE EXISTS (SELECT 1 FROM "
        "transaction_events e WHERE e.transaction_id = t.id AND e.event_type = 'reversed')) AS desfeitos "
        "FROM transactions t WHERE created_at >= :since GROUP BY 1",
    ),
    (
        "Eventos do livro-caixa (período)",
        "SELECT event_type, count(*) FROM transaction_events WHERE created_at >= :since GROUP BY 1",
    ),
    (
        "Mensagens com mais de um lançamento (deve ser 0)",
        "SELECT count(*) FROM (SELECT source_message_id FROM transactions GROUP BY 1 HAVING count(*) > 1) d",
    ),
    (
        "Perguntas e lotes (período)",
        "SELECT kind, status, count(*) FROM pending_entries WHERE created_at >= :since GROUP BY 1, 2",
    ),
    (
        "Comandos (período)",
        "SELECT command_type, count(*) FROM command_receipts WHERE created_at >= :since GROUP BY 1",
    ),
    ("Jobs por tipo e status", "SELECT kind, status, count(*) FROM jobs GROUP BY 1, 2"),
    (
        "Jobs mortos (período): tipo e erro",
        "SELECT kind, last_error, count(*) FROM jobs WHERE status = 'dead' AND finished_at >= :since GROUP BY 1, 2",
    ),
    (
        "Fila atrasada: jobs pendentes vencidos há mais de 1 minuto",
        "SELECT count(*) FROM jobs WHERE status = 'pending' AND run_after < now() - interval '1 minute'",
    ),
    ("Pedidos de exclusão por status", "SELECT status, count(*) FROM deletion_requests GROUP BY 1"),
    (
        "Assinaturas por status",
        "SELECT status, provider, count(*) FROM subscriptions GROUP BY 1, 2",
    ),
]


def pilot_check(engine, hours: int) -> int:
    """Aggregate checks after a pilot run."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    failures = 0
    with engine.connect() as connection:
        for title, sql in CHECKS:
            print(f"== {title}")
            try:
                rows = connection.execute(text(sql), {"since": since}).all()
            except Exception as exc:  # noqa: BLE001 - report and continue
                connection.rollback()
                failures += 1
                print(f"   ERRO: {type(exc).__name__}")
                continue
            for row in rows or [("(nenhum)",)]:
                print("   " + " | ".join(str(value) for value in row))
    return 1 if failures else 0


def find_business(session: Session, last4: str) -> int:
    """Businesses whose number ends in these 4 digits: id, step and dates only."""
    if not (len(last4) == 4 and last4.isdigit()):
        print("Informe exatamente os 4 últimos dígitos.")
        return 2
    rows = session.scalars(
        select(Business).where(Business.whatsapp_number.like(f"%{last4}")).limit(20)
    ).all()
    for business in rows:
        print(f"{business.id} | {business.onboarding_step} | criado {business.created_at:%Y-%m-%d}")
    if not rows:
        print("(nenhum)")
    return 0


def show_business(session: Session, business_id: str, now: datetime) -> int:
    business = session.get(Business, business_id)
    if business is None:
        print("Negócio não encontrado.")
        return 1
    sub = subscription.current_subscription(session, business)
    counts = {
        "lançamentos": session.scalar(
            select(func.count(Transaction.id)).where(Transaction.business_id == business.id)
        ),
        "mensagens": session.scalar(
            select(func.count(InboundEvent.id)).where(
                InboundEvent.sender == business.whatsapp_number
            )
        ),
        "respostas com falha": session.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.recipient == business.whatsapp_number,
                OutboundMessage.status == "failed",
            )
        ),
    }
    print(f"id: {business.id}")
    print(f"etapa: {business.onboarding_step}")
    print(f"teste: {_date(business.trial_started_at)} a {_date(business.trial_ends_at)}")
    print(
        f"acesso para novos lançamentos: {subscription.has_access(session, business, now, get_settings())}"
    )
    print(
        f"assinatura: {sub.status + ' até ' + _date(sub.current_period_end) if sub else 'nenhuma'}"
    )
    print(f"lembretes desligados: {business.reminders_opt_out_at is not None}")
    for name, value in counts.items():
        print(f"{name}: {value}")
    return 0


def extend_trial(session: Session, business_id: str, days: int, now: datetime) -> int:
    business = session.get(Business, business_id, with_for_update=True)
    if business is None or business.trial_ends_at is None or not 1 <= days <= 90:
        print("Negócio sem teste ativo, ou dias fora de 1 a 90.")
        return 1
    base = max(_aware(business.trial_ends_at), now)
    business.trial_ends_at = base + timedelta(days=days)
    session.commit()
    print(f"teste estendido até {_date(business.trial_ends_at)}")
    return 0


def grant_subscription(session: Session, business_id: str, days: int, now: datetime) -> int:
    """Manual access (e.g. a pilot customer). Charges nothing; recorded as provider 'manual'."""
    business = session.get(Business, business_id, with_for_update=True)
    if business is None or not 1 <= days <= 366:
        print("Negócio não encontrado, ou dias fora de 1 a 366.")
        return 1
    sub = subscription.current_subscription(session, business)
    if sub is None:
        sub = Subscription(
            business_id=business.id,
            status=subscription.REQUESTED,
            provider="manual",
            price_cents=0,
            created_at=now,
            updated_at=now,
        )
        session.add(sub)
    base = max(_aware(sub.current_period_end), now) if sub.current_period_end else now
    sub.status, sub.provider = (
        subscription.ACTIVE,
        sub.provider if sub.provider != "none" else "manual",
    )
    sub.current_period_end = base + timedelta(days=days)
    sub.updated_at = now
    session.commit()
    print(f"assinatura {sub.provider} ativa até {_date(sub.current_period_end)}")
    return 0


def requeue_dead(session: Session, kind: str, hours: int, now: datetime) -> int:
    """Give dead jobs of one kind another full set of attempts (after fixing the cause)."""
    jobs = session.scalars(
        select(Job).where(
            Job.status == "dead", Job.kind == kind, Job.finished_at >= now - timedelta(hours=hours)
        )
    ).all()
    for job in jobs:
        job.status, job.attempts, job.run_after = "pending", 0, now
        job.locked_until = job.finished_at = None
        if kind == "send_message":
            outbound = session.get(OutboundMessage, job.payload.get("outbound_id"))
            if outbound is not None and outbound.status == "failed":
                outbound.status, outbound.error_code = "queued", None
    session.commit()
    print(f"{len(jobs)} job(s) {kind} reenfileirado(s)")
    return 0


def reapply_deletions(session: Session, now: datetime) -> int:
    """After restoring a backup: erase again every account whose deletion was completed."""
    completed = session.scalars(
        select(DeletionRequest).where(DeletionRequest.status == privacy.COMPLETED)
    ).all()
    erased = 0
    for request in completed:
        business = session.get(Business, request.business_id)
        if business is not None:
            privacy.delete_business_data(session, business)
            erased += 1
    session.commit()
    print(
        f"{len(completed)} exclusão(ões) concluída(s); {erased} conta(s) restaurada(s) apagada(s) de novo"
    )
    return 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _date(value: datetime | None) -> str:
    return _aware(value).strftime("%Y-%m-%d") if value else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lucromei.ops")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("pilot-check", help="contagens do banco depois de um teste")
    check.add_argument("--hours", type=int, default=24)
    find = commands.add_parser("find-business", help="ids pelos 4 últimos dígitos do número")
    find.add_argument("last4")
    show = commands.add_parser("business", help="situação de um negócio (sem dados pessoais)")
    show.add_argument("business_id")
    extend = commands.add_parser("extend-trial", help="estender o teste grátis")
    extend.add_argument("business_id")
    extend.add_argument("--days", type=int, required=True)
    grant = commands.add_parser("grant-subscription", help="acesso manual, sem cobrança")
    grant.add_argument("business_id")
    grant.add_argument("--days", type=int, required=True)
    requeue = commands.add_parser("requeue-dead", help="reenfileirar jobs dead")
    requeue.add_argument("--kind", required=True)
    requeue.add_argument("--hours", type=int, default=24)
    commands.add_parser("reapply-deletions", help="refazer exclusões depois de restaurar backup")
    args = parser.parse_args(argv)
    engine = create_engine(get_settings().database_url)
    if args.command == "pilot-check":
        return pilot_check(engine, args.hours)
    now = datetime.now(UTC)
    with Session(engine) as session:
        if args.command == "find-business":
            return find_business(session, args.last4)
        if args.command == "business":
            return show_business(session, args.business_id, now)
        if args.command == "extend-trial":
            return extend_trial(session, args.business_id, args.days, now)
        if args.command == "grant-subscription":
            return grant_subscription(session, args.business_id, args.days, now)
        if args.command == "requeue-dead":
            return requeue_dead(session, args.kind, args.hours, now)
        if args.command == "reapply-deletions":
            return reapply_deletions(session, now)
    return 2


if __name__ == "__main__":
    sys.exit(main())

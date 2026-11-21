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

from sqlalchemy import create_engine, text

from lucromei.config import get_settings

CHECKS = [
    ("Cadastros por etapa", "SELECT onboarding_step, count(*) FROM businesses GROUP BY 1"),
    ("Aceites dos termos por versão", "SELECT terms_version, count(*) FROM terms_acceptances GROUP BY 1"),
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
    ("Comandos (período)", "SELECT command_type, count(*) FROM command_receipts WHERE created_at >= :since GROUP BY 1"),
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
    ("Assinaturas por status", "SELECT status, provider, count(*) FROM subscriptions GROUP BY 1, 2"),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lucromei.ops")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("pilot-check", help="contagens do banco depois de um teste")
    check.add_argument("--hours", type=int, default=24)
    args = parser.parse_args(argv)
    engine = create_engine(get_settings().database_url)
    if args.command == "pilot-check":
        return pilot_check(engine, args.hours)
    return 2


if __name__ == "__main__":
    sys.exit(main())

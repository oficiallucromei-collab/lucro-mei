"""Spreadsheet export as a WhatsApp document and the monthly report."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from support import onboard, reset_state, sent_texts, signed_webhook, whatsapp_payload

from lucromei.main import app
from lucromei.reports import match_report, match_spreadsheet
from lucromei.whatsapp import CloudApiOutboundSender, WhatsAppSendError

SP = ZoneInfo("America/Sao_Paulo")


def ts(month: int, day: int, hour: int = 12) -> str:
    return str(int(datetime(2026, month, day, hour, tzinfo=SP).timestamp()))


def setup_function() -> None:
    reset_state()
    onboard()


def say(message_id: str, text: str, timestamp: str) -> str:
    signed_webhook(whatsapp_payload(message_id, text, timestamp=timestamp))
    return sent_texts()[-1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("planilha", (2026, 11)),
        ("Planilha do mês", (2026, 11)),
        ("me manda a planilha do mes passado", (2026, 10)),
        ("exportar", (2026, 11)),
        ("excel", (2026, 11)),
        ("planilha bonita", None),
        ("recebi 150", None),
    ],
)
def test_match_spreadsheet(text, expected) -> None:
    found = match_spreadsheet(text, date(2026, 11, 21))
    assert (found.year, found.month) == expected if expected else found is None


def test_match_report() -> None:
    assert match_report("relatório do mês", date(2027, 1, 3)) == (2027, 1)
    assert match_report("relatorio do mes passado", date(2027, 1, 3)) == (2026, 12)
    assert match_report("relatorio mensal", date(2027, 1, 3)) == (2027, 1)
    assert match_report("relatório de vendas", date(2027, 1, 3)) is None


def test_spreadsheet_is_sent_as_a_csv_document() -> None:
    say("a", "Recebi 1.500,50 por um corte", ts(11, 3))
    say("b", "=HYPERLINK gastei 30", ts(11, 4))
    say("c", "Retirei 99 para mim", ts(11, 5))
    say("d", "desfazer", ts(11, 5))
    say("e", "Recebi 70 por um corte", ts(10, 30))  # previous month: not in this file
    say("f", "planilha", ts(11, 21))
    message = app.state.whatsapp_sender.sent_messages[-1]
    assert message.filename == "lucromei-2026-11.csv"
    assert message.text.startswith("Sua planilha de novembro/2026.")
    content = message.document.decode("utf-8")
    assert content.startswith("﻿Data;Tipo;Valor (R$);Descrição;Situação\r\n")
    rows = content.lstrip("﻿").splitlines()
    assert rows[1] == "03/11/2026;Entrada;1500,50;recebi por um corte;"
    assert rows[2] == "04/11/2026;Despesa;30,00;'=hyperlink gastei;"  # formula neutralized
    assert rows[3] == "05/11/2026;Retirada pessoal;99,00;retirei para mim;desfeito"
    assert len(rows) == 4


def test_monthly_report_compares_with_previous_month_without_advice() -> None:
    say("a", "Recebi 100 por um corte", ts(10, 10))
    say("b", "Recebi 150 por um corte", ts(11, 3))
    say("c", "Recebi 50 por uma barba", ts(11, 3))
    say("d", "Gastei 30 em produtos", ts(11, 4))
    say("e", "Gastei 20 em produtos", ts(11, 6))
    say("f", "Gastei 25 de luz", ts(11, 7))
    report = say("g", "relatório do mês", ts(11, 21))
    assert report.startswith("Relatório de novembro/2026\nLançamentos: 5\n")
    assert "Entradas: R$ 200,00 (+100% em relação ao mês anterior)" in report
    assert "Despesas do negócio: R$ 75,00\n" in report
    assert "Dia com mais entradas: 03/11 (R$ 200,00)" in report
    assert "• gastei em produtos: R$ 50,00" in report
    assert "Não é orientação contábil, fiscal ou de investimentos" in report


def test_cloud_api_document_upload_then_send() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "media-1"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.doc"}]})

    sender = CloudApiOutboundSender(
        access_token="t",
        phone_number_id="123",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert sender.send_document("5511", "a.csv", b"x;y", "text/csv", "legenda") == "wamid.doc"
    upload, send = requests
    assert upload.url.path == "/v25.0/123/media"
    assert b'name="messaging_product"' in upload.content and b"x;y" in upload.content
    assert json.loads(send.content)["document"] == {
        "id": "media-1",
        "filename": "a.csv",
        "caption": "legenda",
    }


def test_cloud_api_document_upload_errors_are_classified() -> None:
    sender = CloudApiOutboundSender(
        access_token="t",
        phone_number_id="123",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"error": {"code": 131053}})
            )
        ),
    )
    with pytest.raises(WhatsAppSendError) as raised:
        sender.send_document("5511", "a.csv", b"x", "text/csv", "c")
    assert raised.value.code == "meta_131053" and not raised.value.retryable

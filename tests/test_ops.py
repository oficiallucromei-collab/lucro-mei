from support import (
    engine,
    onboard,
    requires_postgres,
    reset_state,
    signed_webhook,
    whatsapp_payload,
)

from lucromei.ops import main, pilot_check


def setup_function() -> None:
    reset_state()


@requires_postgres
def test_pilot_check_runs_every_query_and_prints_no_personal_data(capsys) -> None:
    onboard(name="Fulano Secreto")
    signed_webhook(whatsapp_payload("m1", "Recebi 150 por um corte"))
    signed_webhook(whatsapp_payload("m2", "Paguei 300 para o fornecedor"))
    assert pilot_check(engine, 24 * 365 * 10) == 0
    out = capsys.readouterr().out
    assert "ERRO" not in out
    assert "Fulano" not in out and "5511999999999" not in out and "corte" not in out
    assert "income | 1 | None" in out


def test_cli_requires_a_command(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])

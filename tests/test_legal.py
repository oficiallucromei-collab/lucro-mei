from support import client, reset_state

from lucromei.config import get_settings


def setup_function() -> None:
    reset_state()


def test_terms_page_is_public_html_with_current_figures() -> None:
    settings = get_settings()
    settings.trial_days = 14
    settings.deletion_grace_days = 7
    settings.terms_version = "2026-11-21"
    response = client.get("/termos", headers={"X-Internal-Api-Key": ""})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    body = response.text
    assert "Termos de Uso do LucroMEI" in body
    assert "Versão 2026-11-21" in body
    assert "14 dias de teste grátis" in body
    assert "7 dias depois da confirmação" in body
    assert "orientação contábil, fiscal" in body
    assert 'href="/privacidade"' in body


def test_privacy_page_lists_retention_commands_and_processors() -> None:
    settings = get_settings()
    settings.message_text_retention_days = 90
    settings.deletion_grace_days = 7
    settings.contact_email = "contato@example.com"
    body = client.get("/privacidade").text
    assert "até 90 dias" in body
    assert "tudo é apagado 7 dias depois" in body
    for command in ("MEUS DADOS", "APAGAR MEUS DADOS", "CANCELAR EXCLUSÃO"):
        assert command in body
    for processor in ("Meta", "Google Cloud", "Neon", "Anthropic", "OpenAI"):
        assert f"<strong>{processor}" in body
    assert "JR Tech" in body
    assert 'mailto:contato@example.com' in body


def test_contact_falls_back_to_whatsapp_and_is_escaped() -> None:
    settings = get_settings()
    settings.contact_email = ""
    assert "pelo próprio WhatsApp" in client.get("/privacidade").text
    settings.contact_email = '"><script>x</script>'
    body = client.get("/termos").text
    assert "<script>x" not in body
    settings.contact_email = ""

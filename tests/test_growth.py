"""Landing page, one-use campaign codes, first-touch attribution and the funnel."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from support import (
    TestingSession,
    client,
    engine,
    onboard,
    requires_postgres,
    reset_state,
    signed_webhook,
    whatsapp_payload,
)

from lucromei import growth
from lucromei.config import get_settings
from lucromei.models import Acquisition, AcquisitionClick, Business
from lucromei.privacy import delete_business_data

NEW = "5511912345678"


def setup_function() -> None:
    reset_state()
    get_settings().whatsapp_display_number = "5511900000000"


def teardown_function() -> None:
    get_settings().whatsapp_display_number = ""


def click(query: str = "utm_source=meta&utm_campaign=barbeiros-sp") -> str:
    response = client.get(f"/experimentar?{query}", follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.netloc == "wa.me" and location.path == "/5511900000000"
    text = parse_qs(location.query)["text"][0]
    return text


def test_landing_page_keeps_campaign_tags_in_the_button() -> None:
    body = client.get("/?utm_source=meta&utm_campaign=sp&email=a@b.c").text
    assert "Saiba quanto sobra do seu negócio." in body
    assert 'href="/experimentar?utm_source=meta&amp;utm_campaign=sp"' in body
    assert "a@b.c" not in body
    assert "R$ 24,90 por mês" in body and 'href="/privacidade"' in body
    get_settings().whatsapp_display_number = ""
    assert "Experimentar grátis" not in client.get("/").text
    assert client.get("/experimentar", follow_redirects=False).status_code == 404


def test_code_is_claimed_once_by_the_first_message() -> None:
    text = click()
    assert text.startswith("Oi! Quero experimentar o LucroMEI (código ")
    signed_webhook(whatsapp_payload("w1", text, sender=NEW))
    signed_webhook(whatsapp_payload("w2", text, sender="5511955555555"))  # same code again
    with TestingSession() as session:
        rows = {
            row.source + ":" + (row.campaign or "-")
            for row in session.scalars(select(Acquisition))
        }
        assert rows == {"meta:barbeiros-sp", "direto:-"}
        assert session.scalar(select(AcquisitionClick.claimed_at)) is not None


def test_direct_first_message_and_stale_code() -> None:
    text = click("utm_source=google")
    with TestingSession() as session:
        session.query(AcquisitionClick).update(
            {"created_at": datetime.now(UTC) - timedelta(days=8)}
        )
        session.commit()
    signed_webhook(whatsapp_payload("w1", text, sender=NEW))
    with TestingSession() as session:
        assert session.scalar(select(Acquisition.source)) == "direto"


def test_tags_are_identifier_like_only() -> None:
    assert growth.clean_tags({"utm_source": "meta", "utm_campaign": "a b@c"}) == {
        "utm_source": "meta"
    }


def test_deletion_removes_attribution() -> None:
    signed_webhook(whatsapp_payload("w1", click(), sender=NEW))
    with TestingSession() as session:
        business = session.scalar(select(Business).where(Business.whatsapp_number == NEW))
        delete_business_data(session, business)
        session.commit()
        assert session.scalar(select(Acquisition)) is None
        assert session.scalar(select(AcquisitionClick.business_id)) is None


@requires_postgres
def test_funnel_counts(capsys) -> None:
    from lucromei.ops import funnel

    signed_webhook(whatsapp_payload("w1", click(), sender=NEW))
    onboard()  # an older business without attribution is not counted
    assert funnel(engine, 30) == 0
    out = capsys.readouterr().out
    assert "meta | barbeiros-sp | 1 | 1 | 0 | 0 | 0" in out
    assert NEW not in out

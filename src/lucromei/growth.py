# ruff: noqa: E501 - the landing page HTML reads better unwrapped
"""Acquisition: landing page, campaign attribution and the funnel.

- "/" is the landing page; its button goes to "/experimentar?utm_source=...", which stores the
  campaign tags under a random one-use code and redirects to WhatsApp with the message
  "Quero experimentar o LucroMEI (código XXXX)".
- On a business's first message, onboarding claims the code (within 7 days, once) and keeps
  the tags in `acquisitions`. No phone number, name or message text is stored there.
- `python -m lucromei.ops funnel` reports, per campaign: clicks, conversations started,
  onboarded, activated (3+ entries in the first 48 hours) and paying.

The free trial and onboarding are the existing ones; nothing here changes access.
"""

import re
import secrets
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lucromei.config import Settings
from lucromei.models import Acquisition, AcquisitionClick, Business
from lucromei.money import format_brl

CLAIM_WINDOW = timedelta(days=7)
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CODE_RE = re.compile(r"\(c[oó]digo ([A-Za-z0-9_-]{6,16})\)", re.IGNORECASE)


def clean_tags(params) -> dict[str, str]:
    """Identifier-like campaign tags only; anything else (e-mails, free text) is dropped."""
    return {key: params[key] for key in UTM_KEYS if _TAG_RE.fullmatch(params.get(key) or "")}


def new_click(session: Session, tags: dict[str, str], now: datetime) -> str:
    code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
    session.add(AcquisitionClick(code=code, tags=tags or {"utm_source": "direto"}, created_at=now))
    return code


def whatsapp_link(number: str, code: str) -> str:
    text = f"Oi! Quero experimentar o LucroMEI (código {code})"
    return f"https://wa.me/{number}?{urlencode({'text': text})}"


def claim(session: Session, business: Business, text: str, now: datetime) -> None:
    """Attribute a new business to the campaign whose code is in its first message."""
    if session.scalar(select(Acquisition.id).where(Acquisition.business_id == business.id)):
        return
    tags = {"utm_source": "direto"}
    if match := _CODE_RE.search(text or ""):
        claimed = session.execute(
            update(AcquisitionClick)
            .where(
                AcquisitionClick.code == match.group(1),
                AcquisitionClick.claimed_at.is_(None),
                AcquisitionClick.created_at >= now - CLAIM_WINDOW,
            )
            .values(claimed_at=now, business_id=business.id)
            .returning(AcquisitionClick.tags)
        ).scalar_one_or_none()
        if claimed:
            tags = claimed
    session.add(
        Acquisition(
            business_id=business.id,
            source=tags.get("utm_source", "direto"),
            medium=tags.get("utm_medium"),
            campaign=tags.get("utm_campaign"),
            content=tags.get("utm_content"),
            created_at=now,
        )
    )


def landing_html(settings: Settings, query: dict[str, str]) -> str:
    price = format_brl(settings.subscription_price_cents)
    cta = ""
    if settings.whatsapp_display_number:
        href = escape("/experimentar?" + urlencode(clean_tags(query)), quote=True)
        cta = f'<a class="cta" href="{href}">Experimentar grátis no WhatsApp</a>'
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Anote entradas, despesas e retiradas pelo WhatsApp e saiba quanto sobra do seu negócio.">
<title>LucroMEI | Controle financeiro do MEI pelo WhatsApp</title>
<style>
:root{{color-scheme:light dark;--ink:#122f29;--green:#145743;--paper:#f4faf5;--card:#fff}}
@media (prefers-color-scheme: dark){{:root{{--ink:#e4efe9;--green:#7fd1ad;--paper:#0f1a16;--card:#18261f}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:1rem/1.6 system-ui,sans-serif}}
main,footer{{width:min(60rem,100% - 2rem);margin:auto}}section{{padding:1.75rem 0}}
h1{{font-size:clamp(2.2rem,6vw,4rem);line-height:1.05;letter-spacing:-.04em;margin:1.5rem 0 1rem}}
.chat{{background:var(--card);border-radius:1rem;padding:1rem;max-width:26rem}}
.chat p{{margin:.4rem 0;padding:.6rem .8rem;border-radius:.7rem;background:var(--paper)}}
.cta{{display:inline-block;background:var(--green);color:var(--paper);padding:.8rem 1.2rem;border-radius:.5rem;text-decoration:none;font-weight:700}}
a{{color:var(--green)}}footer{{padding:2rem 0;font-size:.95rem}}
</style></head><body><main>
<section><h1>Saiba quanto sobra do seu negócio.</h1>
<p>Mande pelo WhatsApp o que entrou e o que saiu. O LucroMEI anota e mostra o resumo do dia, da
semana e do mês. Sem planilha, sem aplicativo novo, sem senha de banco.</p>{cta}</section>
<section class="chat"><p><strong>Você:</strong> Recebi 150 por um corte</p>
<p><strong>LucroMEI:</strong> Entrada registrada: R$ 150,00.</p>
<p><strong>Você:</strong> quanto ganhei essa semana?</p></section>
<section><h2>{settings.trial_days} dias grátis, depois {price} por mês</h2>
<p>Sem cartão para testar. A assinatura só começa se você mandar ASSINAR e concluir o pagamento.
O LucroMEI não dá orientação contábil, fiscal ou de investimentos.</p>{cta}</section>
</main><footer><a href="/termos">Termos de uso</a> · <a href="/privacidade">Privacidade</a> ·
<a href="/exclusao-de-dados">Excluir meus dados</a></footer></body></html>"""

import csv
import hmac
import io
import json
import secrets
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from string import Template
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from lucromei.billing import BillingUnavailable, configured, sync_event, verify_event
from lucromei.config import Settings, get_settings
from lucromei.conversation import _money
from lucromei.db import get_session
from lucromei.growth import BRAZIL, TAG, insert_for
from lucromei.models import (
    AcquisitionClick,
    Business,
    CampaignDay,
    GrowthAudit,
    GrowthEvent,
    GrowthRateBucket,
    LandingDay,
)
from lucromei.privacy import PrivacyConflict, fulfill
from lucromei.reporting import build_report, default_period, parse_day, recommendations

router = APIRouter()
basic = HTTPBasic(auto_error=False)
LANDING = Template((Path(__file__).parent / "templates" / "landing.html").read_text())
ATTRIBUTES = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "campaign_id",
    "ad_id",
    "referral_id",
)


def growth_settings():
    settings = get_settings()
    if not settings.growth_enabled:
        raise HTTPException(404)
    return settings


SessionDep = Annotated[Session, Depends(get_session)]
GrowthSettings = Annotated[Settings, Depends(growth_settings)]


def require_founder(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)],
    settings: GrowthSettings,
):
    token = settings.growth_founder_token
    if len(token) < 32:
        raise HTTPException(404)
    if (
        not credentials
        or credentials.username != "founder"
        or not hmac.compare_digest(credentials.password.encode(), token.encode())
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="LucroMEI founder"'})
    return settings


FounderSettings = Annotated[Settings, Depends(require_founder)]


def admit_public_request(session, settings, route):
    minute = int(datetime.now(UTC).timestamp()) // 60
    bucket = GrowthRateBucket
    count = session.execute(
        insert_for(session, bucket)
        .values(route=route, minute=minute, count=1)
        .on_conflict_do_update(
            index_elements=["route", "minute"],
            set_={"count": bucket.count + 1},
            where=bucket.count < settings.growth_public_requests_per_minute,
        )
        .returning(bucket.count)
    ).scalar_one_or_none()
    if count is None:
        raise HTTPException(429, headers={"Retry-After": "60"})


def tags(request):
    return {
        key: value
        for key in ATTRIBUTES
        if (value := request.query_params.get(key)) and TAG.fullmatch(value)
    }


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, session: SessionDep, settings: GrowthSettings):
    admit_public_request(session, settings, "landing")
    day = datetime.now(BRAZIL).date().isoformat()
    session.execute(
        insert_for(session, LandingDay)
        .values(day=day, visits=1)
        .on_conflict_do_update(index_elements=["day"], set_={"visits": LandingDay.visits + 1})
    )
    session.commit()
    cta = ""
    if settings.growth_whatsapp_number:
        url = escape("/experimentar?" + urlencode(tags(request)), quote=True)
        cta = f'<a class="cta" href="{url}">Experimentar grátis no WhatsApp</a>'
    legal = " · ".join(
        f'<a href="{escape(url, quote=True)}">{label}</a>'
        for url, label in (
            (settings.growth_privacy_url, "Privacidade"),
            (settings.growth_terms_url, "Termos de uso"),
        )
        if url
    )
    return HTMLResponse(
        LANDING.substitute(
            cta=cta,
            legal=legal,
            price=_money(settings.growth_price_cents),
            trial=settings.growth_trial_days,
        ),
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
    )


@router.get("/experimentar")
def trial_link(request: Request, session: SessionDep, settings: GrowthSettings):
    if not settings.growth_whatsapp_number:
        raise HTTPException(404)
    admit_public_request(session, settings, "trial_link")
    token = secrets.token_urlsafe(24)
    session.add(
        AcquisitionClick(
            token=token,
            created_at=datetime.now(UTC),
            attribution=tags(request) or {"utm_source": "direct"},
        )
    )
    session.commit()
    query = urlencode({"text": "Quero experimentar LucroMEI " + token})
    return RedirectResponse(
        f"https://wa.me/{settings.growth_whatsapp_number}?{query}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/assinatura", response_class=HTMLResponse)
def subscription_return(settings: GrowthSettings):
    return (
        '<html lang="pt-BR"><meta charset="utf-8"><title>Assinatura LucroMEI</title>'
        "<p>Volte ao WhatsApp e envie MEU PLANO para consultar a confirmação.</p></html>"
    )


async def limited_body(request, limit):
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > limit:
            raise HTTPException(413)
        body.extend(part)
    return bytes(body)


@router.post("/webhooks/stripe", status_code=204)
async def stripe_callback(
    request: Request,
    session: SessionDep,
    settings: GrowthSettings,
):
    if not configured(settings):
        raise HTTPException(404)
    body = await limited_body(request, 256 * 1024)
    try:
        event = verify_event(
            body, request.headers.get("Stripe-Signature", ""), settings.growth_stripe_webhook_secret
        )
        if not isinstance(event, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid webhook") from None
    try:
        await run_in_threadpool(sync_event, session, settings, event, datetime.now(UTC))
        session.commit()
    except BillingUnavailable:
        session.rollback()
        raise HTTPException(503, "Billing provider temporarily unavailable") from None
    return Response(status_code=204)


@router.get("/founder/metrics")
def founder_metrics(
    session: SessionDep,
    settings: FounderSettings,
    start: date | None = None,
    end: date | None = None,
):
    default_start, default_end = default_period(datetime.now(UTC))
    end = end or default_end
    start = start or (default_start if end == default_end else end)
    if not date(2020, 1, 1) <= start <= end <= datetime.now(BRAZIL).date():
        raise HTTPException(422, "Invalid period")
    if (end - start).days > 365:
        raise HTTPException(422, "Select at most 366 days")
    report = build_report(session, settings, start, end)
    report["recommendations"] = recommendations(report, settings.growth_report_language)
    return JSONResponse(report, headers={"Cache-Control": "no-store"})


def display_metric(key, value):
    if value is None:
        return "Sem dados"
    if isinstance(value, dict):
        return (
            "<dl>"
            + "".join(
                f"<dt>{escape(label.replace('_', ' '))}</dt><dd>{display_metric(label, item)}</dd>"
                for label, item in value.items()
            )
            + "</dl>"
            if value
            else "Sem registros"
        )
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if isinstance(value, int | float) and ("cents" in key or "_by_source" in key):
        return _money(round(value))
    if isinstance(value, float) and (key == "rate" or key.endswith("_rate")):
        return f"{value:.1%}"
    return escape(str(value))


@router.get("/founder", response_class=HTMLResponse)
def founder_dashboard(
    session: SessionDep,
    settings: FounderSettings,
    start: date | None = None,
    end: date | None = None,
):
    report = json.loads(founder_metrics(session, settings, start, end).body)
    period = report["period"]
    rows = []
    for name, group in (
        ("Funil e receita", report["measured"]),
        ("Estimativas", report["estimated"]),
        ("Campanhas", report["campaigns"]),
        ("Indicações", report["referrals"]),
        ("Experimento", report["experiment"]),
    ):
        rows.append(f"<h2>{name}</h2><dl>")
        for key, val in group.items():
            label = escape(key.replace("_", " "))
            rendered = display_metric(key, val)
            rows.append(f"<dt>{label}</dt><dd>{rendered}</dd>")
        rows.append("</dl>")
    ideas = "".join(f"<li>{escape(idea)}</li>" for idea in report["recommendations"])
    return HTMLResponse(
        '<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>LucroMEI · Aquisição</title><meta name="robots" content="noindex">'
        "<style>body{font:1rem/1.55 system-ui;max-width:65rem;margin:2rem auto;"
        "padding:0 1rem;color:#173e34}dl{display:grid;grid-template-columns:minmax(0,1fr) "
        "minmax(0,2fr);gap:.6rem;background:#eff7f2;padding:1rem}dt{font-weight:600}"
        "dd{margin:0;overflow-wrap:anywhere}input,button{font:inherit}</style>"
        "<h1>Aquisição e receita</h1><p>Resultados do período e MRR atual.</p>"
        f'<form><label>Início <input type="date" name="start" value="{period["start"]}">'
        f'</label> <label>Fim <input type="date" name="end" value="{period["end"]}"></label>'
        " <button>Atualizar</button></form>"
        + "".join(rows)
        + f"<h2>Hipóteses para testar</h2><ul>{ideas}</ul></html>",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


@router.post("/founder/campaigns")
async def import_campaigns(
    request: Request,
    session: SessionDep,
    settings: FounderSettings,
):
    # Browser Basic credentials may be sent cross-site. Accept only non-simple content types.
    if request.headers.get("content-type", "").split(";")[0] != "text/csv":
        raise HTTPException(415, "Use text/csv")
    body = await limited_body(request, 128 * 1024)
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace")), strict=True)
    columns = ["day", "campaign", "spend_cents", "impressions", "clicks"]
    if reader.fieldnames != columns:
        raise HTTPException(422, ",".join(columns))
    writes = {}
    try:
        for row in reader:
            parsed = parse_day(row["day"])
            if not date(2020, 1, 1) <= parsed <= datetime.now(BRAZIL).date():
                raise ValueError
            if not TAG.fullmatch(row["campaign"]) or None in row:
                raise ValueError
            numbers = {key: int(row[key]) for key in columns[2:]}
            if any(not 0 <= val <= 2_000_000_000 for val in numbers.values()):
                raise ValueError
            identifier = row["day"], row["campaign"]
            if identifier in writes:
                raise ValueError
            writes[identifier] = numbers
    except (ValueError, TypeError, csv.Error):
        raise HTTPException(422, f"Invalid CSV row {reader.line_num}") from None
    if not writes or len(writes) > 2000:
        raise HTTPException(422, "Expected 1–2000 rows")
    for (day, campaign), numbers in sorted(writes.items()):
        session.execute(
            insert_for(session, CampaignDay)
            .values(day=day, campaign=campaign, **numbers)
            .on_conflict_do_update(index_elements=["day", "campaign"], set_=numbers)
        )
    session.add(GrowthAudit(action="campaign_csv_upsert"))
    session.commit()
    return {"imported": len(writes)}


@router.get("/founder/privacy-requests")
def privacy_requests(session: SessionDep, settings: FounderSettings):
    rows = session.execute(
        select(
            GrowthEvent.name,
            GrowthEvent.happened_at,
            Business.id,
            Business.whatsapp_number,
        )
        .join(Business, Business.id == GrowthEvent.business_id)
        .where(GrowthEvent.name.startswith("requested_", autoescape=True))
        .order_by(GrowthEvent.happened_at.desc())
        .limit(200)
    ).all()
    session.add(GrowthAudit(action="privacy_requests_accessed"))
    session.commit()
    return JSONResponse(
        [
            {
                "kind": name.split(":", 1)[0],
                "at": happened.isoformat(),
                "business_id": identifier,
                "whatsapp": number,
            }
            for name, happened, identifier, number in rows
        ],
        headers={"Cache-Control": "no-store"},
    )


@router.post("/founder/privacy-requests/{business_id}/{action}")
async def resolve_privacy_request(
    business_id: UUID,
    action: Literal["export", "deletion"],
    request: Request,
    session: SessionDep,
    settings: FounderSettings,
):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Use application/json")
    try:
        body = json.loads(await limited_body(request, 1024))
    except ValueError:
        raise HTTPException(422, "Invalid confirmation") from None
    if body != {"identity_verified": True}:
        raise HTTPException(422, "Confirm identity verification")
    try:
        result = await run_in_threadpool(
            fulfill, session, settings, str(business_id), action, datetime.now(UTC)
        )
        session.commit()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except (PrivacyConflict, BillingUnavailable) as exc:
        raise HTTPException(409, str(exc)) from None
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})
